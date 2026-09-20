"""Local config/state handling.

Everything lives under ``$YI_HOME`` (default ``~/.config/yi``) with 0700/0600
permissions, because the generated client profiles contain credentials
(the VLESS UUID is the only thing standing between a stranger and your proxy).

Config is TOML. We write it by hand (``dump_toml``) and read it with the standard
library's ``tomllib`` — no third-party dependency, and the round-trip is covered by
tests so a hand-written writer can't drift away from the spec.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from typing import Any

# 中国大陆常见站点里**不是 .cn 结尾**的那些，走直连。
#
# 为什么要有这份名单：`GEOIP,CN,DIRECT` 也管用，但它得先把域名解析成真实 IP
# 才能判断；在 fake-ip 模式下，每个新域名都要多一次解析，首包明显变慢。
# 域名规则是字符串比对，直接命中、不用解析。两者配合：
# 这份名单覆盖常用的，GEOIP 兜住剩下的。
#
# 想加就加（写 `example.com` 这种后缀，不用带 `.` 前缀）。
DEFAULT_DIRECT_DOMAINS = [
    # 腾讯
    "qq.com",
    "tencent.com",
    "gtimg.com",
    "weixin.com",
    # 百度 / 阿里
    "baidu.com",
    "bdstatic.com",
    "alipay.com",
    "taobao.com",
    "tmall.com",
    "alicdn.com",
    "alibaba.com",
    "aliyun.com",
    "aliyuncs.com",
    "dingtalk.com",
    # 电商 / 生活
    "jd.com",
    "360buyimg.com",
    "pinduoduo.com",
    "meituan.com",
    "dianping.com",
    "ctrip.com",
    "qunar.com",
    "ele.me",
    # 内容 / 社区
    "bilibili.com",
    "hdslb.com",
    "iqiyi.com",
    "youku.com",
    "douyin.com",
    "toutiao.com",
    "bytedance.com",
    "kuaishou.com",
    "xiaohongshu.com",
    "zhihu.com",
    "douban.com",
    "weibo.com",
    "sina.com.cn",
    "sohu.com",
    "163.com",
    "126.net",
    "netease.com",
    "ximalaya.com",
    # 硬件 / 云 / 开发者
    "xiaomi.com",
    "mi.com",
    "huawei.com",
    "hicloud.com",
    "csdn.net",
    "gitee.com",
    "cnblogs.com",
    "oschina.net",
]


DEFAULT_CONFIG: dict[str, Any] = {
    "region": "cn-hongkong",
    "zones": ["cn-hongkong-b", "cn-hongkong-c", "cn-hongkong-d"],
    "instance_types": ["ecs.e-c1m1.large", "ecs.t6-c1m1.large", "ecs.u1-c1m1.large"],
    "image_name_filters": ["ubuntu_22_04_x64", "ubuntu_22_04"],
    "disk_category": "cloud_essd",
    "disk_size": 40,
    "bandwidth_out": 100,
    # 入方向带宽：0 = 不传该参数（用阿里云默认值）。CreateInstance 不接受 -1。
    "bandwidth_in": 0,
    # SpotWithPriceLimit + spot_duration>0 才能买到保护期；SpotAsPriceGo 买不到，
    # 代价是实例可能刚创建就被回收（实测踩过）。
    "spot_strategy": "SpotWithPriceLimit",
    "spot_duration": 1,
    # 0 = 自动：查当前市场价峰值 × 1.15 作为出价上限。
    "spot_price_limit": 0.0,
    # 自动出价的上浮倍数。出价只是"能不能拿到/保住"的门槛，留足余量很便宜。
    "spot_bid_multiplier": 1.5,
    # **服务端版本必须 ≥ 客户端版本**：REALITY 会把客户端版本写进握手，
    # 服务端按 maxClientVer 校验；实测服务端 1.8.24 会被 26.x 客户端直接拒绝
    # （客户端报 "received real certificate"）。而客户端版本我们控制不了
    # （v2rayNG / mihomo 自带内核），所以服务端默认装最新版。
    # 想复现环境就写死成具体版本号，例如 "v26.3.27"。
    "xray_version": "latest",
    "xray_port": 443,
    "vless_flow": "xtls-rprx-vision",
    # 直连的域名后缀。见上面 DEFAULT_DIRECT_DOMAINS 里的说明。
    "direct_domains": list(DEFAULT_DIRECT_DOMAINS),
    # 社区维护的分流规则集（Loyalsoldier/clash-rules）。
    # 关掉它只剩内置的基础规则——排查"是不是规则集的锅"时用。
    "ruleset_enabled": True,
    # 镜像顺序。默认是国内实测可用的几个；留空数组则用内置默认顺序。
    "ruleset_mirrors": [],  # 见 yi.rules.DEFAULT_MIRRORS
    # 内核自己去更新的周期（小时）。写成 0 表示不主动更新，只用手动/本地那份。
    "ruleset_interval_hours": 24,
    # 本地缓存多久算过期（小时）。超过就由 `./yi rules --update` 或守护重取。
    "ruleset_max_age_hours": 168,  # 一周
    # REALITY 的伪装目标（"偷证书"的对象）。硬性要求：
    #   1) 从香港可达，支持 TLS1.3 + HTTP/2；
    #   2) **证书链要短** —— REALITY 会把 dest 的真实证书链抓下来、替换签名后发给客户端，
    #      证书链超过它的缓冲上限就会握手装不完（www.microsoft.com 现在 8273 字节，
    #      实测直接失败）。所以这里是**候选列表**，bootstrap 会逐个自检，用第一个真正通的。
    #   3) 在中国大陆不该是被墙的站点（dl.google.com 技术上可用，但做 SNI 会被盯上，故不列为默认）。
    "reality_dests": [
        "www.cloudflare.com",
        "www.bing.com",
        "www.apple.com",
        "www.microsoft.com",
    ],
    "reality_fingerprint": "chrome",
    "allow_ssh_from": "auto",
    # 复用已有网络资源。**默认留空** = 让 yi 走"自动发现默认 VPC / 交换机"或
    # "自动创建安全组"的路径，开箱即用、不依赖任何人的账号。
    #
    # 如果你在控制台里已经有一台机器、一个 VPC 或一个安全组，可以在这里填上 ID，
    # yi 就会复用它（并给它补上缺的 443/22 规则）。这类资源属于**你自己的资产**：
    # `down` 与失败回滚都只删自己创建的东西，判断依据是 state.json 的
    # `security_group_owned` 标记，所以填进来的 ID 永远不会被删掉。
    "vswitch_id": "",
    "security_group_id": "",
    "key_pair_name": "yi",
    "instance_name": "yi-hk",
    "aliyun_profile": "default",
    "ssh_user": "root",
    "budget": {"max_hours": 720, "max_gb": 300},
}


def home_dir() -> str:
    """数据目录。老版本叫 ~/.config/vpnctl，第一次运行会自动搬过来，不丢配置和密钥。"""
    override = os.environ.get("YI_HOME")
    if override:
        return os.path.abspath(override)
    new = os.path.join(os.path.expanduser("~"), ".config", "yi")
    legacy = os.path.join(os.path.expanduser("~"), ".config", "vpnctl")
    if not os.path.exists(new) and os.path.exists(legacy):
        import shutil

        try:
            shutil.move(legacy, new)
        except OSError:
            return os.path.abspath(legacy)
    return os.path.abspath(new)


def config_path() -> str:
    return os.path.join(home_dir(), "config.toml")


def state_path() -> str:
    return os.path.join(home_dir(), "state.json")


def profiles_dir() -> str:
    return os.path.join(home_dir(), "profiles")


def key_path() -> str:
    return os.path.join(home_dir(), "id_ed25519")


def known_hosts_path() -> str:
    return os.path.join(home_dir(), "known_hosts")


def ensure_home() -> str:
    root = home_dir()
    os.makedirs(root, mode=0o700, exist_ok=True)
    _chmod(root, 0o700)
    os.makedirs(profiles_dir(), mode=0o700, exist_ok=True)
    return root


def _chmod(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def write_private(path: str, data: str, mode: int = 0o600) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
    _chmod(tmp, mode)
    os.replace(tmp, path)
    _chmod(path, mode)


def write_json(path: str, payload: dict[str, Any], mode: int = 0o600) -> None:
    write_private(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", mode)


# --------------------------------------------------------------------------
# TOML：写用自己拼（保证输出我们可控），读用标准库
# --------------------------------------------------------------------------
def dump_toml(payload: dict[str, Any]) -> str:
    lines: list[str] = []

    def scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, list):
            return "[{}]".format(", ".join(scalar(item) for item in value))
        return json.dumps(str(value))

    for key in sorted(payload):
        value = payload[key]
        if isinstance(value, dict):
            continue
        lines.append(f"{key} = {scalar(value)}")
    for key in sorted(payload):
        value = payload[key]
        if not isinstance(value, dict):
            continue
        lines.append("")
        lines.append(f"[{key}]")
        for sub in sorted(value):
            lines.append(f"{sub} = {scalar(value[sub])}")
    return "\n".join(lines).strip() + "\n"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> dict[str, Any]:
    """读配置。带 mtime 缓存——一次请求里这个函数会被调用好几次，
    每次都做一次 TOML 解析是纯浪费。"""
    path = config_path()
    cached = _CACHE.get("config")
    if cached and cached[0] == _mtime(path):
        return cached[1]
    if not os.path.exists(path):
        return dict(DEFAULT_CONFIG)
    try:
        with open(path, "rb") as fh:
            parsed = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"配置文件解析失败: {exc}") from exc
    merged = deep_merge(DEFAULT_CONFIG, parsed)
    _CACHE["config"] = (_mtime(path), merged)
    return merged


_CACHE: dict[str, Any] = {}


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def save_config(config: dict[str, Any]) -> str:
    ensure_home()
    path = config_path()
    write_private(path, dump_toml(config))
    _CACHE.pop("config", None)
    return path


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def load_state() -> dict[str, Any] | None:
    path = state_path()
    cached = _CACHE.get("state")
    if cached and cached[0] == _mtime(path):
        return cached[1]
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    _CACHE["state"] = (_mtime(path), payload)
    return payload


def save_state(state: dict[str, Any]) -> str:
    ensure_home()
    path = state_path()
    write_json(path, state)
    _CACHE.pop("state", None)
    return path


def ensure_ssh_key() -> str:
    """Create a local ed25519 key. The private half never leaves this machine."""
    import subprocess

    ensure_home()
    path = key_path()
    if os.path.exists(path) and os.path.exists(path + ".pub"):
        _chmod(path, 0o600)
        return path
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "yi", "-f", path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _chmod(path, 0o600)
    _chmod(path + ".pub", 0o644)
    return path


def public_key_body() -> str:
    path = ensure_ssh_key()
    with open(path + ".pub", encoding="utf-8") as fh:
        return fh.read().strip()


def file_mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)
