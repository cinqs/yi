"""yi command line entry point."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from typing import Any

from . import __version__, configgen, identity, proxy, ssh, state
from .aliyun import AlidnsClient, AliyunError, EcsClient, load_credentials
from .bootstrap import render_user_data

log = logging.getLogger("yi")

SPOT_STRATEGIES = ("SpotAsPriceGo", "SpotWithPriceLimit", "NoSpot")
RETRYABLE_CREATE_CODES = {
    "InstanceType.StockNotEnough",
    "InvalidInstanceType.ValueNotSupported",
    "InvalidInstanceType.NotSupported",
    "Zone.NotOnSale",
    "InvalidZoneId.NotFound",
    "OperationDenied.NoStock",
    "InvalidSpotPriceLimit.LessThanMarketPrice",
    "QuotaExceeded",
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log_path = _setup_logging(bool(args.verbose))
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 1
    try:
        log.info("yi %s: %s", __version__, " ".join(sys.argv[1:]) or "(无参数)")
        return int(args.func(args) or 0)
    except AliyunError as exc:
        log.error("云 API 错误: %s (action=%s)", exc, exc.action or "?")
        print(f"云 API 错误: {exc}", file=sys.stderr)
        hint = exc.hint()
        if hint:
            log.error("提示: %s", hint)
            print(f"提示: {hint}", file=sys.stderr)
        log.error("完整日志: %s", log_path)
        print(f"完整日志: {log_path}", file=sys.stderr)
        return 2
    except ssh.SshError as exc:
        log.error("SSH 错误: %s", exc)
        print(f"SSH 错误: {exc}", file=sys.stderr)
        log.error("完整日志: %s", log_path)
        print(f"完整日志: {log_path}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code:
            log.error("中止（exit %s）: %s", code, exc.code)
            print(f"完整日志: {log_path}", file=sys.stderr)
        return code
    except Exception:
        log.exception("未预期的错误")
        print(f"未预期的错误。完整堆栈已写入: {log_path}", file=sys.stderr)
        print("把这个文件发给我就能定位。", file=sys.stderr)
        return 4


def _setup_logging(verbose: bool) -> str:
    """Log to stderr *and* to a file, so a failure is never lost with the scrollback."""
    # 输出到管道/文件时 Python 默认块缓冲，守护进程（watch）里"就绪/新配置"会卡在
    # 缓冲区里不显示。CLI 的输出必须逐行可见。
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
        sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    logs_dir = os.path.join(state.home_dir(), "logs")
    try:
        os.makedirs(logs_dir, mode=0o700, exist_ok=True)
        log_path = os.path.join(logs_dir, "yi-{}.log".format(datetime.now().strftime("%Y%m%d")))
        # create it ourselves so the first byte written is never world-readable
        if not os.path.exists(log_path):
            os.close(os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600))
        os.chmod(log_path, 0o600)
        handlers = [
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(log_path, encoding="utf-8"),
        ]
    except OSError:
        log_path = "(无法写入日志文件)"
        handlers = [logging.StreamHandler(sys.stderr)]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    return log_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yi",
        description="一键在阿里云香港买竞价实例并搭好 VLESS/REALITY 代理",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"驿 · Yi {__version__} （yi）",
    )
    parser.add_argument("--verbose", action="store_true", help="打印调试日志")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="写入本地配置并生成 SSH 密钥")
    p_init.add_argument("--region", default=None)
    p_init.add_argument("--instance-types", default=None, help="逗号分隔的候选规格")
    p_init.add_argument("--profile", default=None, help="~/.aliyun/config.json 里的 profile 名")
    p_init.add_argument("--force", action="store_true", help="覆盖已有配置")
    p_init.add_argument("--check", action="store_true", help="顺便校验 AccessKey 可用")
    p_init.set_defaults(func=cmd_init)

    p_up = sub.add_parser("up", help="创建竞价实例并完成端到端就绪")
    p_up.add_argument("--dry-run", action="store_true", help="只打印将要创建的资源，不调用写接口")
    p_up.add_argument("--force-recreate", action="store_true", help="即使已有实例也重新创建")
    p_up.add_argument("--instance-type", default=None, help="只用这一个规格，不做 fallback")
    p_up.add_argument("--zone", default=None, help="只用这一个可用区")
    p_up.add_argument(
        "--spot-strategy",
        choices=SPOT_STRATEGIES,
        default=None,
        help="覆盖竞价策略；NoSpot = 普通按量付费（不会被回收，适合排查问题）",
    )
    p_up.add_argument(
        "--price-limit", type=float, default=None, help="元/小时，设置后改用 SpotWithPriceLimit"
    )
    p_up.add_argument("--ssh-from", default=None, help="允许 SSH 的 CIDR，默认取本机公网 IP /32")
    p_up.add_argument("--no-rollback", action="store_true", help="失败时保留已创建资源，便于排查")
    p_up.add_argument("--timeout", type=float, default=600.0, help="等待就绪的秒数")
    p_up.add_argument("--no-qr", action="store_true")
    p_up.add_argument(
        "--new-identity", action="store_true", help="重新生成一套凭据（客户端需要重新导入配置）"
    )
    p_up.set_defaults(func=cmd_up)

    p_status = sub.add_parser("status", help="查看实例状态、流量与到期信息")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_sub = sub.add_parser("sub", help="重新生成客户端配置")
    p_sub.add_argument("--refresh", action="store_true", help="先通过 SSH 从实例重新读取参数")
    p_sub.add_argument("--qr", action="store_true", help="额外打印二维码")
    p_sub.add_argument("--json", action="store_true")
    p_sub.set_defaults(func=cmd_sub)

    p_ssh = sub.add_parser("ssh", help="用 yi 的密钥登录实例")
    p_ssh.add_argument("remote_command", nargs="*", help="可选：要执行的远程命令")
    p_ssh.set_defaults(func=cmd_ssh)

    p_down = sub.add_parser("down", help="销毁实例与安全组")
    p_down.add_argument("--orphans", action="store_true", help="不管状态文件，按名称前缀搜索并清理")
    p_down.add_argument("--yes", action="store_true", help="不交互确认")
    p_down.set_defaults(func=cmd_down)

    p_doctor = sub.add_parser("doctor", help="本地与远端连通性诊断")
    p_doctor.add_argument("--api", action="store_true", help="校验 AccessKey 与权限")
    p_doctor.add_argument("--json", action="store_true")
    p_doctor.set_defaults(func=cmd_doctor)

    p_selftest = sub.add_parser(
        "selftest", help="在服务器上回环自测：自己当客户端连自己，验证 VLESS/REALITY 真的能用"
    )
    p_selftest.set_defaults(func=cmd_selftest)

    p_connect = sub.add_parser("connect", help="连接：起代理内核并打开系统代理")
    p_connect.add_argument("--port", type=int, default=proxy.DEFAULT_MIXED_PORT)
    p_connect.add_argument(
        "--no-system-proxy", action="store_true", help="只起内核，不改系统代理（不需要管理员密码）"
    )
    p_connect.add_argument("--json", action="store_true")
    p_connect.set_defaults(func=cmd_connect)

    p_disconnect = sub.add_parser("disconnect", help="断开：停内核并还原系统代理")
    p_disconnect.add_argument("--port", type=int, default=proxy.DEFAULT_MIXED_PORT)
    p_disconnect.add_argument("--json", action="store_true")
    p_disconnect.set_defaults(func=cmd_disconnect)

    p_proxy = sub.add_parser("proxy-status", help="代理状态与出口 IP 校验")
    p_proxy.add_argument("--port", type=int, default=proxy.DEFAULT_MIXED_PORT)
    p_proxy.add_argument("--json", action="store_true")
    p_proxy.set_defaults(func=cmd_proxy_status)

    p_kernel = sub.add_parser("fetch-kernel", help="下载代理内核 mihomo（不入库，用到才取）")
    p_kernel.add_argument("--url", help="直接给出压缩包地址（绕墙或指定版本时用）")
    p_kernel.add_argument("--force", action="store_true", help="已存在也重新下载")
    p_kernel.set_defaults(func=cmd_fetch_kernel)

    p_watch = sub.add_parser("watch", help="守护：竞价实例被回收后自动重建")
    p_watch.add_argument("--interval", type=float, default=60.0)
    p_watch.add_argument("--once", action="store_true", help="只检查一次")
    p_watch.set_defaults(func=cmd_watch)
    return parser


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _emit(payload: dict[str, Any], args) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str))


# Ordered by what actually works from a mainland-China connection. api.ipify.org
# was measured to be unreachable from the user's machine, so it is not first.
IP_ECHO_URLS = (
    "http://members.3322.org/dyndns/getip",
    "https://myip.ipip.net",
    "https://ifconfig.me/ip",
    "https://api.ip.sb/ip",
    "https://ipv4.icanhazip.com",
    "https://api.ipify.org",
)

_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def _extract_ipv4(text: str) -> str | None:
    """Pull the first usable public IPv4 out of whatever the echo service returned."""
    for candidate in _IPV4_RE.findall(text or ""):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.version == 4 and not (
            address.is_private or address.is_loopback or address.is_reserved or address.is_link_local
        ):
            return str(address)
    return None


def _my_public_ip(timeout: float = 6.0) -> str | None:
    """Best-effort detection of this machine's public IPv4, with several fallbacks."""
    for url in IP_ECHO_URLS:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - any failure just means "try the next one"
            log.debug("公网 IP 探测失败: %s", url)
            continue
        found = _extract_ipv4(body)
        if found:
            log.info("本机公网 IP: %s（来自 %s）", found, url)
            return found
        log.warning("%s 返回了无法解析的内容: %r", url, body.strip()[:80])

    found = _public_ip_via_dns()
    if found:
        log.info("本机公网 IP: %s（来自 DNS 查询）", found)
    return found


def _public_ip_via_dns() -> str | None:
    """Last resort that needs no HTTP: ask a public resolver who we are."""
    for argv in (
        ["dig", "+short", "myip.opendns.com", "@resolver1.opendns.com"],
        ["dig", "+short", "TXT", "o-o.myaddr.l.google.com", "@8.8.8.8"],
        ["dig", "+short", "whoami.akamai.net", "@ns1-1.akamaitech.net"],
    ):
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=6)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0:
            found = _extract_ipv4(completed.stdout)
            if found:
                return found
    return None


def _normalize_cidr(value: str) -> str:
    """Validate an SSH allow-list entry before Aliyun ever sees it.

    Alibaba Cloud rejects anything odd with the very unhelpful
    ``InvalidParam.SourceCidrIp: The specified parameter SourceCidrIp is not valid``,
    and ``SourceCidrIp`` is IPv4-only. Detected addresses can legitimately turn out
    to be IPv6 on a dual-stack Mac, so this is checked here instead.
    """
    text = (value or "").strip()
    if not text:
        raise SystemExit("SSH 白名单为空。用 --ssh-from <IP>/32 指定，或写 auto 让它自动探测。")
    host, _, prefix = text.partition("/")
    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError as exc:
        raise SystemExit(
            f"SSH 白名单 {value!r} 不是合法 IP。\n"
            "  · 用 --ssh-from <你的IPv4>/32，例如 --ssh-from 47.52.1.2/32\n"
            "  · 公网 IP 可以在这里查：https://myip.ipip.net"
        ) from exc
    if address.version == 6:
        raise SystemExit(
            f"SSH 白名单是 IPv6（{address}），但阿里云安全组的 SourceCidrIp 只接受 IPv4。\n"
            "  · 用 --ssh-from <你的IPv4>/32 指定\n"
            "  · 或先关掉本机 IPv6 再跑（探测到的就会是 IPv4）"
        )
    if not prefix:
        return f"{address}/32"
    try:
        return str(ipaddress.ip_network(text, strict=False))
    except ValueError as exc:
        raise SystemExit(f"SSH 白名单 {value!r} 的前缀不合法（示例：{address}/32）。") from exc


def _client(config: dict[str, Any], region: str | None = None) -> EcsClient:
    credentials = load_credentials(config.get("aliyun_profile", "default"))
    return EcsClient(credentials, region or config["region"])


def _load_state_or_die() -> dict[str, Any]:
    current = state.load_state()
    if not current or not current.get("instance_id"):
        raise SystemExit("还没有实例。先跑 `yi up`。")
    return current


def _server_info_from_state(current: dict[str, Any]) -> dict[str, Any]:
    info = current.get("server_info")
    if not info:
        raise SystemExit("状态文件里没有服务端参数。跑 `yi sub --refresh` 重新读取。")
    return info


def _ensure_server_info(
    current: dict[str, Any], config: dict[str, Any], timeout: float = 180.0
) -> dict[str, Any]:
    """Make sure the cached server parameters exist, pulling them over SSH if not."""
    if current.get("server_info"):
        return _normalize_server_info(current, config)
    info = ssh.wait_ready(
        current["public_ip"],
        key=current.get("ssh_key_path") or state.key_path(),
        user=current.get("ssh_user") or config["ssh_user"],
        timeout=timeout,
    )
    current["server_info"] = info
    current = _normalize_server_info(current, config)
    state.save_state(current)
    return current


def _normalize_server_info(current: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """服务端报告的公网 IP 不可靠（阿里云元数据经常返回 404），以 API 拿到的为准。"""
    info = current.get("server_info") or {}
    public_ip = current.get("public_ip")
    if not public_ip:
        return current
    if not info.get("address"):
        log.warning("服务端没从元数据拿到公网 IP，用 API 返回的 %s 补上", public_ip)
    elif info["address"] != public_ip:
        log.warning("服务端报告的公网 IP 是 %s，API 返回 %s，以 API 为准", info["address"], public_ip)
    else:
        return current
    info["address"] = public_ip
    current["server_info"] = info
    return current


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_init(args) -> int:
    config = state.load_config()
    if os.path.exists(state.config_path()) and not args.force:
        log.info("配置已存在: %s（用 --force 覆盖）", state.config_path())
    else:
        if args.region:
            config["region"] = args.region
        if args.instance_types:
            config["instance_types"] = [
                item.strip() for item in args.instance_types.split(",") if item.strip()
            ]
        if args.profile:
            config["aliyun_profile"] = args.profile
        path = state.save_config(config)
        print(f"已写入配置: {path}")

    key = state.ensure_ssh_key()
    print(f"SSH 私钥: {key} (600)")

    if args.check:
        ecs = _client(config)
        regions = ecs.describe_regions()
        print(f"AccessKey 可用，可见区域 {len(regions)} 个")
        print("目标区域 {} 在可用列表内: {}".format(config["region"], config["region"] in regions))
    print("\n下一步: yi up --dry-run  然后  yi up")
    return 0


def _plan(config: dict[str, Any], args) -> dict[str, Any]:
    zones = [args.zone] if args.zone else list(config["zones"])
    types = [args.instance_type] if args.instance_type else list(config["instance_types"])
    strategy = config["spot_strategy"]
    override = getattr(args, "spot_strategy", None)
    if override:
        strategy = override
    price_limit = (
        args.price_limit if args.price_limit is not None else float(config.get("spot_price_limit") or 0)
    )
    duration = int(config.get("spot_duration") or 0)
    if strategy == "NoSpot":
        # 普通按量付费没有被回收这回事，也就没有保护期可言
        duration = 0
        price_limit = 0.0
    if price_limit and strategy == "SpotAsPriceGo":
        strategy = "SpotWithPriceLimit"
    # A protection period is only purchasable under SpotWithPriceLimit, so asking
    # for one implies the strategy (see _spot_summary for the user-facing wording).
    if duration and strategy == "SpotAsPriceGo":
        strategy = "SpotWithPriceLimit"
    return {
        "region": config["region"],
        "zones": zones,
        "instance_types": types,
        "spot_strategy": strategy,
        "spot_price_limit": price_limit,
        "spot_duration": duration,
        "spot_summary": _spot_summary(
            strategy, price_limit, duration, float(config.get("spot_bid_multiplier") or 1.5)
        ),
        "bandwidth_out": int(config["bandwidth_out"]),
        "disk": "{} {}GiB".format(config["disk_category"], config["disk_size"]),
        "xray_version": config["xray_version"],
        "port": int(config["xray_port"]),
        "reality_dests": config.get("reality_dests") or [config.get("reality_sni") or "www.cloudflare.com"],
        "image_filters": config["image_name_filters"],
        "flow": config.get("vless_flow") or "(disabled)",
        "vswitch_id": config.get("vswitch_id") or "(自动发现默认交换机)",
        "security_group_id": config.get("security_group_id") or "(自动创建)",
    }


def _spot_summary(strategy: str, price_limit: float, duration: int, multiplier: float = 1.5) -> str:
    if strategy == "NoSpot":
        return "NoSpot：普通按量付费，不会被回收，但单价高于竞价"
    if strategy != "SpotWithPriceLimit":
        return "SpotAsPriceGo：跟随市场价，没有保护期，可能刚买就被回收"
    bid = f"{price_limit:.3f} 元/小时" if price_limit else f"自动（市场价峰值 × {multiplier:.1f}）"
    protection = f"{duration} 小时保护期" if duration else "无保护期"
    return f"SpotWithPriceLimit：出价上限 {bid}，{protection}"


def _resolve_bid(
    ecs: EcsClient,
    strategy: str,
    price_limit: float,
    instance_type: str,
    zone_id: str,
    multiplier: float = 1.5,
) -> Any:
    """Work out the bid, degrading to SpotAsPriceGo (loudly) if the price is unknown."""
    if strategy != "SpotWithPriceLimit":
        return strategy, 0.0
    if price_limit > 0:
        return strategy, price_limit
    market = ecs.spot_price(instance_type, zone_id)
    if not market:
        log.warning("拿不到市场价，退回 SpotAsPriceGo —— 这次没有保护期，仍可能刚创建就被回收")
        return "SpotAsPriceGo", 0.0
    bid = round(market * max(1.0, multiplier), 3)
    log.info("出价上限 %.3f 元/小时（市场价峰值 %.3f × %.1f），含保护期", bid, market, multiplier)
    return "SpotWithPriceLimit", bid


def _zone_candidates(ecs: EcsClient, config: dict[str, Any], plan: dict[str, Any]) -> list[Any]:
    """[(zone_id, network)] to try, honoring a pinned vSwitch if one is configured.

    A vSwitch belongs to exactly one zone, so reusing one collapses the
    multi-AZ fallback to a single zone and we must say so out loud.
    """
    pinned = str(config.get("vswitch_id") or "").strip()
    if pinned:
        network = ecs.describe_vswitch(pinned)
        if plan["zones"] and network["zone_id"] not in plan["zones"]:
            raise AliyunError(
                "ZoneVSwitchMismatch",
                "交换机 {} 在可用区 {}，与配置的 zones {} 冲突；用 --zone {} 或清空 vswitch_id。".format(
                    pinned, network["zone_id"], plan["zones"], network["zone_id"]
                ),
            )
        log.info(
            "复用交换机 %s（可用区 %s，VPC %s，网段 %s）",
            network["vswitch_id"],
            network["zone_id"],
            network["vpc_id"],
            network["cidr_block"] or "?",
        )
        return [(network["zone_id"], network)]
    return [(zone, ecs.default_vswitch(zone)) for zone in plan["zones"]]


def _resolve_security_group(
    ecs: EcsClient,
    config: dict[str, Any],
    vpc_id: str,
    plan: dict[str, Any],
    ssh_from: str,
) -> Any:
    """Returns (group_id, owned). Borrowed groups are never deleted by us."""
    existing = str(config.get("security_group_id") or "").strip()
    port_range = "{}/{}".format(config["xray_port"], config["xray_port"])
    if existing:
        group = ecs.security_group(existing)
        group_vpc = str(group.get("VpcId") or "")
        if group_vpc and vpc_id and group_vpc != vpc_id:
            raise AliyunError(
                "SecurityGroupVpcMismatch",
                f"安全组 {existing} 属于 VPC {group_vpc}，交换机在 VPC {vpc_id}，两者必须一致。",
            )
        log.info("复用安全组 %s（%s）", existing, group.get("SecurityGroupName") or "未命名")
        ecs.ensure_egress_all(existing)
        if not ecs.ensure_ingress(existing, "tcp", port_range, "0.0.0.0/0", "yi VLESS/REALITY"):
            log.info("安全组 %s 已满足要求", existing)
        ecs.ensure_ingress(existing, "tcp", "22/22", ssh_from, "yi SSH from admin")
        return existing, False

    group_id = ecs.create_security_group(
        vpc_id,
        "{}-sg".format(config["instance_name"]),
        "yi managed security group",
    )
    ecs.authorize_egress_all(group_id)
    ecs.authorize(group_id, "tcp", port_range, "0.0.0.0/0", "yi VLESS/REALITY")
    ecs.authorize(group_id, "tcp", "22/22", ssh_from, "yi SSH from admin")
    log.info("已创建安全组 %s（SSH 仅放行 %s）", group_id, ssh_from)
    return group_id, True


def cmd_up(args) -> int:
    config = state.load_config()
    state.ensure_home()
    key = state.ensure_ssh_key()

    plan = _plan(config, args)
    ssh_from = args.ssh_from or config.get("allow_ssh_from")
    if not ssh_from or ssh_from == "auto":
        ssh_from = _my_public_ip() or "auto"
    if ssh_from != "auto":
        ssh_from = _normalize_cidr(ssh_from)
    plan["ssh_from"] = ssh_from

    if args.dry_run:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        print("\n(dry-run：未调用任何写接口)")
        return 0

    if ssh_from == "auto":
        raise SystemExit(
            f"无法自动探测本机公网 IP（已试过 {len(IP_ECHO_URLS)} 个 HTTP 服务和 DNS 兜底）。\n"
            "  · 打开 https://myip.ipip.net 或 https://cip.cc 看你的公网 IP\n"
            "  · 然后：yi up --ssh-from <你的IP>/32\n"
            "  · 想跳过白名单（SSH 仍然只认密钥，但会对全网开放）：--ssh-from 0.0.0.0/0"
        )

    ecs = _client(config)
    existing = state.load_state()
    if existing and existing.get("instance_id") and not args.force_recreate:
        try:
            instance = ecs.describe_instance(existing["instance_id"])
            if instance and instance.get("Status") in ("Running", "Starting"):
                print(
                    "已有可用实例 {}（{}）。用 --force-recreate 才会重建。".format(
                        existing["instance_id"], instance.get("Status")
                    )
                )
                existing = _ensure_server_info(existing, config, args.timeout)
                return _finish_up(existing, config, args)
        except AliyunError as exc:
            if exc.code not in ("InvalidInstanceId.NotFound", "InvalidInstance.NotFound"):
                raise
            log.warning("状态文件里的实例已不存在（可能是竞价回收）")

    credentials = load_credentials(config.get("aliyun_profile", "default"))
    log.info("使用 AccessKey %s（来自 %s）", credentials.masked_id(), credentials.source)

    image_id = ecs.find_image(config["image_name_filters"])
    state.public_key_body()  # ensures the key exists before importing
    ecs.import_key_pair(config["key_pair_name"], state.public_key_body())

    ident = identity.ensure_identity(rotate=bool(getattr(args, "new_identity", False)))
    log.info("凭据指纹 %s（重建机器会复用它，客户端无需重新导入）", identity.fingerprint(ident))
    user_data = render_user_data(config, ident)
    instance_id: str | None = None
    group_id: str | None = None
    group_owned = False
    created: dict[str, Any] = {}
    try:
        last_error: AliyunError | None = None
        candidates = _zone_candidates(ecs, config, plan)
        group_id, group_owned = _resolve_security_group(
            ecs, config, candidates[0][1]["vpc_id"], plan, ssh_from
        )
        for zone, network in candidates:
            for instance_type in plan["instance_types"]:
                try:
                    log.info("尝试创建 %s @ %s …", instance_type, zone)
                    bid_strategy, bid_limit = _resolve_bid(
                        ecs,
                        plan["spot_strategy"],
                        plan["spot_price_limit"],
                        instance_type,
                        zone,
                        float(config.get("spot_bid_multiplier") or 1.5),
                    )
                    instance_id = ecs.create_spot_instance(
                        image_id=image_id,
                        instance_type=instance_type,
                        zone_id=zone,
                        vswitch_id=network["vswitch_id"],
                        security_group_id=group_id,
                        key_pair_name=config["key_pair_name"],
                        user_data=user_data,
                        instance_name=config["instance_name"],
                        bandwidth_out=plan["bandwidth_out"],
                        disk_category=config["disk_category"],
                        disk_size=int(config["disk_size"]),
                        spot_strategy=bid_strategy,
                        spot_duration=plan["spot_duration"],
                        spot_price_limit=bid_limit,
                        bandwidth_in=int(config.get("bandwidth_in") or 0),
                    )
                    created = {
                        "zone_id": zone,
                        "instance_type": instance_type,
                        "image_id": image_id,
                        "vpc_id": network["vpc_id"],
                        "vswitch_id": network["vswitch_id"],
                        "spot_strategy": bid_strategy,
                        "spot_price_limit": bid_limit,
                    }
                    break
                except AliyunError as exc:
                    last_error = exc
                    if exc.code in RETRYABLE_CREATE_CODES:
                        log.warning("%s 不可用（%s），换下一个组合", instance_type, exc.code)
                        continue
                    raise
            if instance_id:
                break
        if not instance_id:
            raise last_error or AliyunError("NoCapacity", "所有规格/可用区组合都创建失败。")

        instance_state = {
            "region": config["region"],
            "instance_id": instance_id,
            "security_group_id": group_id,
            "security_group_owned": group_owned,
            "key_pair_name": config["key_pair_name"],
            "public_ip": None,
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ssh_user": config["ssh_user"],
            "ssh_key_path": key,
            "label": "{}-{}".format(config["instance_name"], instance_id[-4:]),
            "ready": False,
        }
        instance_state.update(created)
        # Persist before waiting: an instance that exists but never becomes ready
        # must still be findable by `yi down`, or it bills silently.
        state.save_state(instance_state)

        log.info("实例 %s 已提交，等待 Running…", instance_id)
        ecs.ensure_running(instance_id, timeout=min(300.0, args.timeout))
        public_ip = ecs.ensure_public_ip(instance_id)
        instance_state["public_ip"] = public_ip
        state.save_state(instance_state)
        log.info("公网 IP: %s，等待 cloud-init 完成 Xray 安装…", public_ip)

        info = ssh.wait_ready(
            public_ip,
            key=key,
            user=config["ssh_user"],
            timeout=args.timeout,
            progress=_progress_logger(),
        )
        instance_state["server_info"] = info
        instance_state["ready"] = True
        instance_state = _normalize_server_info(instance_state, config)
        state.save_state(instance_state)
        return _finish_up(instance_state, config, args)
    except Exception:
        if not args.no_rollback:
            _rollback(ecs, instance_id, group_id if group_owned else None)
        raise


def _progress_logger():
    last = [0.0]

    def report(elapsed: float) -> None:
        if elapsed - last[0] >= 30:
            last[0] = elapsed
            log.info("仍在等待服务端就绪…（%ds）", int(elapsed))

    return report


def _rollback(ecs: EcsClient, instance_id: str | None, group_id: str | None) -> None:
    if not instance_id and not group_id:
        log.info("回滚：本次没有需要清理的资源（失败发生在创建之前）")
        return
    log.warning("回滚：清理本次创建的资源")
    if instance_id:
        try:
            if not ecs.delete_instance_when_ready(instance_id, timeout=150.0):
                log.error("删除实例失败，实例仍在计费，请手工清理: %s", instance_id)
        except AliyunError as exc:
            log.error("删除实例失败（需手工清理）: %s", exc)
    if group_id:
        try:
            ecs.delete_security_group(group_id)
            log.info("已删除安全组 %s", group_id)
        except AliyunError as exc:
            log.error("删除安全组失败（可能仍被实例占用）: %s", exc)
    try:
        if state.load_state():
            os.remove(state.state_path())
    except OSError:
        pass


def _finish_up(instance_state: dict[str, Any], config: dict[str, Any], args) -> int:
    info = _server_info_from_state(instance_state)
    label = instance_state.get("label") or "HK-Spot"
    paths = configgen.write_profiles(info, label)
    link = configgen.build_link(info, label)
    selftest = str(info.get("selftest") or "unknown").strip()

    print("")
    print("=== 就绪 ===")
    print("服务端: {}:{}".format(info["address"], info["port"]))
    print(
        "实例:   {} @ {} ({})".format(
            instance_state["instance_id"],
            instance_state.get("zone_id", "?"),
            instance_state.get("instance_type", "?"),
        )
    )
    if selftest == "ok":
        print("服务端自检: 通过（已在服务器上回环验证 VLESS/REALITY 可用）")
    else:
        print(f"服务端自检: 未通过 -> {selftest}")
        print("  复现: yi ssh 'bash /opt/yi/selftest.sh'")
        print("  客户端大概率也连不上，先解决这个再导入配置。")
    print("")
    print("分享链接:")
    print(link)
    if not args.no_qr:
        print("")
        from .qr import print_qr

        print_qr(link)
    print("")
    print("客户端文件:")
    for name in configgen.PROFILE_FILES:
        print(f"  {paths[name]}")
    print("")
    print("Android: 复制 v2rayng-subscription.txt 内容 -> v2rayNG -> '+' -> 从剪贴板导入")
    print("macOS:   Clash Verge Rev -> 配置 -> 粘贴 mihomo.yaml -> 打开系统代理")
    return 0


def cmd_status(args) -> int:
    current = _load_state_or_die()
    config = state.load_config()
    ecs = _client(config, current.get("region"))
    instance: dict[str, Any] = {}
    status = "Unknown"
    try:
        status = ecs.instance_status(current["instance_id"])
        if status != "Gone":
            instance = ecs.describe_instance(current["instance_id"])
    except AliyunError as exc:
        if exc.code not in ("InvalidInstanceId.NotFound", "InvalidInstance.NotFound"):
            raise
        status = "Gone"

    created = _parse_time(current.get("created_at"))
    hours = 0.0
    if created:
        hours = (datetime.now(UTC) - created).total_seconds() / 3600.0
    tx_bytes = 0
    if status == "Running" and created:
        try:
            tx_bytes = ecs.internet_tx_bytes(current["instance_id"], created, datetime.now(UTC))
        except AliyunError as exc:
            log.debug("流量查询失败: %s", exc)

    payload = {
        "instance_id": current["instance_id"],
        "status": status,
        "public_ip": current.get("public_ip"),
        "zone_id": current.get("zone_id"),
        "instance_type": current.get("instance_type"),
        "created_at": current.get("created_at"),
        "hours_running": round(hours, 2),
        "internet_tx_bytes": tx_bytes,
        "budget_max_hours": config["budget"]["max_hours"],
        "budget_max_gb": config["budget"]["max_gb"],
        "server_port": (current.get("server_info") or {}).get("port"),
        "xray_version": (current.get("server_info") or {}).get("xray_version"),
    }
    if args.json:
        _emit(payload, args)
        return 0

    print("实例:   {} ({})".format(current["instance_id"], status))
    print("公网 IP: {}".format(current.get("public_ip") or instance.get("PublicIpAddress") or "?"))
    print("区域:   {} @ {}".format(current.get("zone_id"), current.get("instance_type")))
    print(f"已运行: {hours:.1f} 小时")
    print(f"出流量: {round(tx_bytes / 1024.0 / 1024 / 1024, 3)} GB（云监控口径）")
    print(
        "Xray:   {} : {}".format(
            (current.get("server_info") or {}).get("xray_version", "?"),
            (current.get("server_info") or {}).get("port", "?"),
        )
    )
    if status == "Gone":
        print("\n实例已经不存在（竞价被回收或已被删除）。跑 `yi watch` 可自动重建。")
    return 0


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def cmd_sub(args) -> int:
    current = _load_state_or_die()
    config = state.load_config()
    if args.refresh or not current.get("server_info"):
        current["server_info"] = None  # 强制重读
        current = _ensure_server_info(current, config, timeout=60.0)
    info = _server_info_from_state(current)
    label = current.get("label") or "HK-Spot"
    paths = configgen.write_profiles(info, label)
    link = configgen.build_link(info, label)
    if args.json:
        _emit({"link": link, "files": paths, "server": info}, args)
        return 0
    print(link)
    if args.qr:
        from .qr import print_qr

        print_qr(link)
    for name in configgen.PROFILE_FILES:
        print(f"{name}: {paths[name]}")
    if config.get("domain") and config.get("subdomain"):
        client = AlidnsClient(load_credentials(config.get("aliyun_profile", "default")))
        record_id = client.upsert_a_record(config["domain"], config["subdomain"], info["address"])
        print(
            "DNS: {}.{} -> {}（RecordId {}）".format(
                config["subdomain"], config["domain"], info["address"], record_id
            )
        )
    return 0


def cmd_ssh(args) -> int:
    current = _load_state_or_die()
    config = state.load_config()
    host = current["public_ip"]
    key = current.get("ssh_key_path") or state.key_path()
    user = current.get("ssh_user") or config["ssh_user"]
    if args.remote_command:
        sys.stdout.write(ssh.run(host, " ".join(args.remote_command), key=key, user=user, check=False))
        return 0
    return ssh.interactive(host, key=key, user=user)


def cmd_down(args) -> int:
    config = state.load_config()
    cleaned: list[str] = []

    # 先断开本地：否则机器没了、系统代理还指着死端口，用户会以为"网断了"
    try:
        if proxy.running_pid():
            proxy.stop()
            cleaned.append("本地代理内核")
    except Exception as exc:  # noqa: BLE001 - 清理失败不该挡住销毁
        log.warning("停止代理内核失败: %s", exc)
    if proxy.system_proxy_enabled():
        try:
            proxy.set_system_proxy(False)
            cleaned.append("系统代理")
        except proxy.ProxyError as exc:
            log.warning("还原系统代理失败（记得手动关）: %s", exc)

    current = state.load_state()
    if not (current and current.get("instance_id")) and not args.orphans:
        print("没有需要清理的资源。")
        return 0

    ecs = _client(config)
    if current and current.get("instance_id"):
        if not args.yes:
            answer = input("确认删除实例 {}？[y/N] ".format(current["instance_id"]))
            if answer.strip().lower() not in ("y", "yes"):
                print("已取消")
                return 1
        try:
            ecs.delete_instance(current["instance_id"])
            cleaned.append("instance:{}".format(current["instance_id"]))
        except AliyunError as exc:
            if exc.code not in ("InvalidInstanceId.NotFound", "InvalidInstance.NotFound"):
                raise
        _wait_gone(ecs, current["instance_id"])
        if current.get("security_group_id") and not args.keep_security_group:
            if current.get("security_group_owned", True):
                try:
                    ecs.delete_security_group(current["security_group_id"])
                    cleaned.append("security-group:{}".format(current["security_group_id"]))
                except AliyunError as exc:
                    log.warning("安全组删除失败（可能仍被占用）: %s", exc)
            else:
                print("安全组 {} 是你自己的资源，已保留（不删除）".format(current["security_group_id"]))
        if os.path.exists(state.state_path()):
            os.remove(state.state_path())

    if args.orphans:
        cleaned.extend(_clean_orphans(ecs, config))

    if not cleaned:
        print("没有需要清理的资源。")
        return 0
    print("已清理: {}".format(", ".join(cleaned)))
    return 0


def _wait_gone(ecs: EcsClient, instance_id: str, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = ecs.instance_status(instance_id)
        except AliyunError:
            return
        if status in ("Gone", "Deleted", "Stopped"):
            return
        time.sleep(5)
    log.warning("实例 %s 在 %ds 内没有完全消失，稍后可用 yi down --orphans 复查", instance_id, int(timeout))


def _clean_orphans(ecs: EcsClient, config: dict[str, Any]) -> list[str]:
    cleaned: list[str] = []
    name = config["instance_name"]
    data = ecs.call(
        "DescribeInstances",
        RegionId=ecs.region,
        InstanceName=name,
        PageSize=100,
    )
    instances = data.get("Instances", {}).get("Instance", [])
    if isinstance(instances, dict):
        instances = [instances]
    for instance in instances:
        instance_id = instance.get("InstanceId")
        if not instance_id:
            continue
        ecs.delete_instance(instance_id)
        cleaned.append(f"orphan-instance:{instance_id}")

    groups = (
        ecs.call(
            "DescribeSecurityGroups",
            RegionId=ecs.region,
            SecurityGroupName=f"{name}-sg",
            PageSize=100,
        )
        .get("SecurityGroups", {})
        .get("SecurityGroup", [])
    )
    if isinstance(groups, dict):
        groups = [groups]
    for group in groups:
        group_id = group.get("SecurityGroupId")
        if not group_id:
            continue
        if group_id == config.get("security_group_id"):
            log.info("跳过 %s：这是配置里指定的自有安全组", group_id)
            continue
        try:
            ecs.delete_security_group(group_id)
            cleaned.append(f"orphan-security-group:{group_id}")
        except AliyunError as exc:
            log.warning("清理安全组 %s 失败: %s", group_id, exc)
    return cleaned


def cmd_doctor(args) -> int:
    config = state.load_config()
    findings: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, fatal: bool = True) -> None:
        findings.append({"check": name, "ok": ok, "detail": detail, "fatal": fatal})

    add("config", os.path.exists(state.config_path()), state.config_path())
    add("ssh-key", os.path.exists(state.key_path()), state.key_path())
    if os.path.exists(state.key_path()):
        add(
            "ssh-key-mode", state.file_mode(state.key_path()) == 0o600, oct(state.file_mode(state.key_path()))
        )

    credentials = None
    try:
        credentials = load_credentials(config.get("aliyun_profile", "default"))
        add("credentials", True, f"{credentials.masked_id()}（{credentials.source}）")
    except AliyunError as exc:
        add("credentials", False, str(exc))

    if args.api and credentials is not None:
        findings.extend(_api_preflight(credentials, config))

    ip = _my_public_ip()
    add("public-ip", bool(ip), ip or "探测失败")

    current = state.load_state()
    if current and current.get("public_ip"):
        host = current["public_ip"]
        reachable = _tcp_check(
            host, int((current.get("server_info") or {}).get("port") or config["xray_port"])
        )
        add("server-port", reachable, "{}:{}".format(host, config["xray_port"]))

    if args.json:
        _emit({"findings": findings}, args)
        return 0 if all(item["ok"] for item in findings) else 1
    for finding in findings:
        print("[{}] {:<26} {}".format("ok" if finding["ok"] else "!!", finding["check"], finding["detail"]))
    failed = [item for item in findings if not item["ok"]]
    if failed:
        print(f"\n有 {len(failed)} 项没过。第一个红叉就是 up 会卡住的地方。")
    return 0 if not failed else 1


def cmd_selftest(args) -> int:
    """Prove the server works without a phone: connect to it from itself."""
    current = _load_state_or_die()
    config = state.load_config()
    host = current.get("public_ip")
    if not host:
        raise SystemExit("状态里没有公网 IP，先跑 `yi up`。")
    output = ssh.run(
        host,
        "bash /opt/yi/selftest.sh 2>&1 || true",
        key=current.get("ssh_key_path") or state.key_path(),
        user=current.get("ssh_user") or config["ssh_user"],
        check=False,
    ).strip()
    first_line = output.splitlines()[0] if output else "(无输出)"
    if first_line == "ok":
        print("自检通过：服务端已在本机回环验证 VLESS/REALITY 可用")
        return 0
    print(f"自检失败: {first_line}", file=sys.stderr)
    print(output, file=sys.stderr)
    print(
        "\n这通常意味着服务端本身连不出去（安全组出方向、目标站点被墙、Xray 版本问题）。",
        file=sys.stderr,
    )
    return 1


def cmd_fetch_kernel(args) -> int:
    """取一份代理内核。

    内核（mihomo）有几十 MB、每个平台各一份，放仓库里会让项目胖几十倍，
    所以改成用到时自己取。这也是 `connect` 之前唯一需要手工准备的东西。
    """
    existing = proxy.mihomo_path()
    target = proxy.kernel_target_path()
    if existing and not args.force:
        print(f"内核已就绪：{existing}")
        print("要重新下载就加 --force。")
        return 0

    print(f"目标位置：{target}")
    print(f"平台架构：{proxy.go_os()}/{proxy.go_arch()}")
    try:
        path = proxy.fetch_kernel(url=args.url)
    except proxy.ProxyError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    size = os.path.getsize(path)
    print(f"\n完成：{path}（{size / 1024 / 1024:.1f} MB）")
    print("接下来：./yi connect")
    return 0


def cmd_connect(args) -> int:
    current = _load_state_or_die()
    config = state.load_config()
    current = _ensure_server_info(current, config)
    info = current["server_info"]
    label = current.get("label") or "HK-Spot"

    pid = proxy.start(info, None, label)
    proxy.set_desired_connected(True)
    port = proxy.resolve_port()
    result: dict[str, Any] = {"pid": pid, "port": port, "server": info["address"]}

    if not args.no_system_proxy:
        try:
            result["system_proxy"] = proxy.set_system_proxy(True, port)
        except proxy.ProxyError as exc:
            proxy.stop()
            raise SystemExit(f"系统代理没设成功（已回退）：{exc}") from exc

    check = proxy.verify(port)
    if not check["ok"]:
        # 内核刚起来时第一次校验可能瞬时失败（DNS/连接建立中），再给一次机会
        time.sleep(3)
        check = proxy.verify(port)
    result["verify"] = check
    if args.json:
        _emit(result, args)
        return 0 if check["ok"] else 1

    print(f"代理内核已启动（pid {pid}，混合端口 {port}）")
    if not args.no_system_proxy:
        print("系统代理已开启（{}）".format(result["system_proxy"]["service"]))
    print("服务端: {}".format(info["address"]))
    print("出口校验: {}".format(check["note"]))
    if check["actual_exit"]:
        print("  实际出口 IP: {}（期望 {}）".format(check["actual_exit"], check["expected_exit"]))
    return 0 if check["ok"] else 1


def cmd_disconnect(args) -> int:
    # 先撤销期望状态，再动手：这样即使中途失败，后台的调和循环也会把剩下的收尾
    proxy.set_desired_connected(False)
    stopped = proxy.stop()
    restored: dict[str, Any] = {}
    try:
        restored = proxy.set_system_proxy(False, proxy.resolve_port())
    except proxy.ProxyError as exc:
        log.warning("还原系统代理失败（可能需要手动关）: %s", exc)
        restored = {"error": str(exc)}
    payload = {"stopped": stopped, "system_proxy": restored}
    if args.json:
        _emit(payload, args)
        return 0
    print("代理内核已停" if stopped else "代理内核本来就没在跑")
    if "error" in restored:
        print("系统代理还原失败：{}".format(restored["error"]))
    else:
        print("系统代理已还原")
    return 0


def cmd_proxy_status(args) -> int:
    current = state.load_state() or {}
    info = current.get("server_info") or {}
    payload = proxy.status(args.port)
    payload["server"] = info.get("address")
    payload["node"] = current.get("label")
    if payload["running"]:
        payload["verify"] = proxy.verify(args.port)
    if args.json:
        _emit(payload, args)
        return 0
    print("内核:     {}".format("运行中 (pid {})".format(payload["pid"]) if payload["running"] else "未运行"))
    print("端口:     {} {}".format(payload["port"], "监听中" if payload["listening"] else "未监听"))
    print("系统代理: {}".format("已开启" if payload["system_proxy"] else "已关闭"))
    print("服务端:   {}".format(payload["server"] or "—"))
    if payload.get("verify"):
        print(
            "出口:     {}（{}）".format(payload["verify"]["actual_exit"] or "不通", payload["verify"]["note"])
        )
    return 0


def _api_preflight(credentials, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk exactly the API calls `up` makes, in order, without creating anything."""
    out: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        out.append({"check": name, "ok": ok, "detail": detail, "fatal": True})

    ecs = EcsClient(credentials, config["region"])

    regions = ecs.describe_regions()
    add("ecs:DescribeRegions", config["region"] in regions, f"共 {len(regions)} 个区域")
    if config["region"] not in regions:
        return out

    zones = [str(z.get("ZoneId")) for z in ecs.describe_zones()]
    add(
        "ecs:DescribeZones",
        bool(zones),
        "{} 有 {} 个可用区: {}".format(config["region"], len(zones), ", ".join(zones)),
    )

    vpc_id = ""
    vswitch_id = str(config.get("vswitch_id") or "").strip()
    if vswitch_id:
        try:
            info = ecs.describe_vswitch(vswitch_id)
            vpc_id = info["vpc_id"]
            add(
                "ecs:DescribeVSwitches",
                True,
                "{} 在 {} / {}（{}）".format(
                    vswitch_id, info["zone_id"], info["vpc_id"], info["cidr_block"] or "?"
                ),
            )
            if info["zone_id"] not in zones:
                add("vswitch-zone", False, "交换机所在 {} 不在该区域的可用区列表里".format(info["zone_id"]))
            elif info["zone_id"] not in config["zones"]:
                add(
                    "vswitch-zone-config",
                    False,
                    "交换机在 {}，但配置的 zones={}；用 --zone {} 或清空 vswitch_id".format(
                        info["zone_id"], config["zones"], info["zone_id"]
                    ),
                )
            else:
                add("vswitch-zone", True, "可用区 {} 在配置的 zones 内".format(info["zone_id"]))
        except AliyunError as exc:
            add("ecs:DescribeVSwitches", False, f"{exc}（检查 vswitch_id 和 region 是否匹配）")

    group_id = str(config.get("security_group_id") or "").strip()
    if group_id:
        try:
            group = ecs.security_group(group_id)
            group_vpc = str(group.get("VpcId") or "")
            add(
                "ecs:DescribeSecurityGroups",
                True,
                "{}（{} / {}）".format(
                    group_id, group.get("SecurityGroupName") or "未命名", group_vpc or "?"
                ),
            )
            if vpc_id and group_vpc and group_vpc != vpc_id:
                add("sg-vpc-match", False, f"安全组在 {group_vpc}，交换机在 {vpc_id}")
            elif vpc_id:
                add("sg-vpc-match", True, "与交换机同 VPC")
            try:
                rules = ecs.security_group_rules(group_id, "ingress")
                wanted = "{}/{}".format(config["xray_port"], config["xray_port"])
                open_443 = any(
                    str(r.get("IpProtocol") or "").lower() == "tcp"
                    and str(r.get("PortRange") or "") == wanted
                    for r in rules
                )
                add(
                    "sg-ingress-rules",
                    True,
                    "现有 {} 条入方向规则，443 已放行: {}".format(
                        len(rules), "是" if open_443 else "否（up 会自动补）"
                    ),
                )
            except AliyunError as exc:
                add("ecs:DescribeSecurityGroupAttribute", False, f"{exc}（RAM 策略需要加这个 action）")
        except AliyunError as exc:
            add("ecs:DescribeSecurityGroups", False, str(exc))

    try:
        filters = config["image_name_filters"]
        image_id = ecs.find_image(filters)
        add("ecs:DescribeImages", True, f"{filters} -> {image_id}")
    except AliyunError as exc:
        add("ecs:DescribeImages", False, str(exc))

    try:
        name = config["key_pair_name"]
        add(
            "ecs:DescribeKeyPairs",
            True,
            "密钥对 {!r} {}".format(
                name, "已存在" if ecs.key_pair_exists(name) else "不存在（up 会自动导入）"
            ),
        )
    except AliyunError as exc:
        add("ecs:DescribeKeyPairs", False, str(exc))

    if str(config.get("spot_strategy")) == "SpotWithPriceLimit" or int(config.get("spot_duration") or 0):
        instance_type = (config["instance_types"] or ["?"])[0]
        zone_id = (config["zones"] or ["?"])[0]
        price = ecs.spot_price(instance_type, zone_id)
        add(
            "ecs:DescribeSpotPriceHistory",
            price is not None,
            "{} @ {} 市场价峰值 {} 元/小时 -> 出价 {:.3f}（×{:.1f}）".format(
                instance_type,
                zone_id,
                price,
                round(price * float(config.get("spot_bid_multiplier") or 1.5), 3),
                float(config.get("spot_bid_multiplier") or 1.5),
            )
            if price
            else "拿不到市场价，up 会退回 SpotAsPriceGo（没有保护期，可能刚创建就被回收）",
        )
    return out


def _tcp_check(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def cmd_watch(args) -> int:
    config = state.load_config()
    missing = 0
    while True:
        current = state.load_state()
        if not current or not current.get("instance_id"):
            log.info("没有实例记录，执行 up")
            missing = 0
            _recreate_safely(config, args)
        else:
            ecs = _client(config, current.get("region"))
            instance_id = current["instance_id"]
            # 用**裸列表**判断"还在不在"。`DescribeInstanceStatus` 在某些 POP 上会忽略
            # InstanceIds 过滤、返回别的实例的状态（今天实测过），拿它做决策会误判。
            present: dict[str, Any] | None = None
            try:
                present = ecs.find_in_listing(instance_id)
            except AliyunError as exc:
                log.warning("查询状态失败: %s", exc)

            if present is None:
                missing += 1
                log.warning("第 %d 次查不到实例 %s（可能是竞价回收）", missing, instance_id)
                # 连续两次都查不到才动手，避免单次 API 抖动导致误重建
                if missing >= 2:
                    log.warning("连续 %d 次查不到，判定已被回收，开始重建", missing)
                    missing = 0
                    _recreate_safely(config, args)
            else:
                missing = 0
                log.info("实例 %s 状态 %s，正常", instance_id, present.get("Status"))
                _budget_guard(config, current)
        if args.once:
            return 0
        time.sleep(max(10.0, args.interval))


def _recreate_safely(config: dict[str, Any], args) -> None:
    """重建失败不能让守护进程退出——竞价被回收是常态，重试才是它的职责。"""
    try:
        _recreate(config, args)
    except (AliyunError, ssh.SshError) as exc:
        log.error("重建失败: %s", exc)
        log.error("守护继续运行，等下一轮重试（间隔 %s 秒）", args.interval)


def _recreate(config: dict[str, Any], args) -> None:
    recreate_args = argparse.Namespace(
        dry_run=False,
        force_recreate=True,
        instance_type=None,
        zone=None,
        price_limit=None,
        ssh_from=None,
        no_rollback=False,
        timeout=600.0,
        no_qr=False,
    )
    cmd_up(recreate_args)


def _budget_guard(config: dict[str, Any], current: dict[str, Any]) -> None:
    created = _parse_time(current.get("created_at"))
    if not created:
        return
    hours = (datetime.now(UTC) - created).total_seconds() / 3600.0
    max_hours = float(config["budget"]["max_hours"])
    if hours > max_hours:
        log.warning("已运行 %.1fh，超过预算上限 %.0fh —— 建议 yi down", hours, max_hours)


if __name__ == "__main__":
    sys.exit(main())
