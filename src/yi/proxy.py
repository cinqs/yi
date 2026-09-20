"""连接管理：用 mihomo 内核把本机接到那台香港机器上。

分层原则：**协议和代理内核不自己写**。mihomo（Clash Meta）是成熟内核，
我们只负责三件事：生成它的配置、起停它、开系统代理。

系统代理要改 macOS 网络设置，必须管理员权限。这里用 `osascript ... with
administrator privileges` 弹系统原生授权框——比常驻一个特权守护进程干净，
代价是每次开关都会问一次密码（可以后续用 SMJobBless/helper 优化）。
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from typing import Any

from . import __version__, configgen, state

log = logging.getLogger("yi.proxy")

DEFAULT_MIXED_PORT = 7897

# 期望状态：用户点"连接"就把 want_connected 置 true，点"断开"置 false。
# 真正的连接由 reconcile() 不断收敛过去——内核崩了、机器换了、系统代理被人手动关了，
# 都会自己纠正回来。这比"点一下做一串动作"可靠得多（那些动作任何一步失败就烂在那）。


class ProxyError(RuntimeError):
    pass


def proxy_dir() -> str:
    return os.path.join(state.home_dir(), "mihomo")


def config_path() -> str:
    return os.path.join(proxy_dir(), "config.yaml")


def pid_path() -> str:
    return os.path.join(proxy_dir(), "mihomo.pid")


def log_path() -> str:
    return os.path.join(proxy_dir(), "mihomo.log")


def applied_path() -> str:
    return os.path.join(proxy_dir(), "applied.json")


def find_free_port(preferred: int = DEFAULT_MIXED_PORT, span: int = 40) -> int:
    """找一个没人监听的端口。7897 是 Clash Verge 的默认口，很容易撞上。"""
    for port in range(preferred, preferred + span):
        if not _port_open(port):
            return port
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def resolve_port(preferred: int | None = None) -> int:
    """确定混合端口。

    关键：**内核如果正在跑，就用它实际监听的端口**——绝不能再"探测一下"。
    第一次实现就是在这里翻车的：start() 起内核在 7898，紧接着 reconcile 又去
    resolve_port()，把内核自己占的 7898 当成"端口冲突"，换成了 7899，
    于是系统代理指向 7899 这个没人监听的端口，**整台机器断网**。
    """
    if running_pid():
        applied = _applied()
        if applied.get("port"):
            return int(applied["port"])
    config = state.load_config()
    saved = int(preferred or config.get("mixed_port") or DEFAULT_MIXED_PORT)
    if not _port_open(saved):
        return saved
    port = find_free_port(saved + 1)
    log.warning("端口 %d 已被占用（可能是 Clash Verge 之类），改用 %d", saved, port)
    config["mixed_port"] = port
    state.save_config(config)
    return port


def desired_connected() -> bool:
    config = state.load_config()
    if "want_connected" in config:
        return bool(config["want_connected"])
    # 老配置里没有这个字段（声明式期望状态是后来加的）。
    # 此时以"内核是否在跑"为准，否则升级上来的一次调和会直接把用户的连接掐掉。
    return running_pid() is not None


def set_desired_connected(value: bool) -> None:
    config = state.load_config()
    config["want_connected"] = bool(value)
    state.save_config(config)


def _fingerprint(info: dict[str, Any] | None) -> str | None:
    if not info:
        return None
    return "{}|{}|{}|{}".format(
        info.get("address"), info.get("uuid"), info.get("public_key"), info.get("sni")
    )


def _applied_fingerprint() -> str | None:
    return _applied().get("fingerprint")


def _applied() -> dict[str, Any]:
    import json

    try:
        with open(applied_path(), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _set_applied(fingerprint: str | None, port: int | None = None) -> None:
    import json

    if fingerprint is None:
        payload: dict[str, Any] = {"fingerprint": None, "port": None}
    else:
        payload = {"fingerprint": fingerprint, "port": port or _applied().get("port")}
    state.write_private(applied_path(), json.dumps(payload) + "\n", 0o600)


def _set_applied_fingerprint(value: str | None) -> None:
    """兼容旧调用：只更新指纹。"""
    _set_applied(value, _applied().get("port"))


def mihomo_path() -> str | None:
    """内核位置：环境变量 > 工具自带目录 > PATH。"""
    override = os.environ.get("YI_MIHOMO")
    if override and os.path.exists(override):
        return override
    bundled = os.path.join(state.home_dir(), "bin", "mihomo")
    if os.path.exists(bundled):
        return bundled
    return shutil.which("mihomo") or shutil.which("clash-meta")


# --------------------------------------------------------------------------
# 内核下载
#
# 不把 mihomo 二进制放进仓库：它有几十 MB，而且每个平台/架构各一份，
# 塞进 git 会把这项目变胖几十倍，还得跟着上游升级。改成"用到时自己取一份"。
# --------------------------------------------------------------------------

MIHOMO_RELEASE_API = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
# 走不通时的备用入口（国内直连 GitHub 常常失败，但 api.github.com 一般可达）
MIHOMO_MIRROR_HINT = "https://ghproxy.net/"


def kernel_target_path() -> str:
    return os.path.join(state.home_dir(), "bin", "mihomo")


def go_arch(machine: str | None = None) -> str:
    """把 ``platform.machine()`` 映射成 Go 的架构名（mihomo 的产物按 Go 命名）。"""
    m = (machine or platform.machine()).lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("i386", "i686", "x86"):
        return "386"
    return m


def go_os(sysname: str | None = None) -> str:
    s = (sysname or platform.system()).lower()
    return {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(s, s)


def kernel_asset_name(
    release: dict[str, Any], machine: str | None = None, sysname: str | None = None
) -> str | None:
    """从 GitHub release 的 JSON 里挑出适配本机的那个压缩包。

    纯函数，不碰网络也不碰磁盘 —— 选错了用户会下载到一个跑不起来的二进制，
    所以这段逻辑单独测（``tests/test_kernel_fetch.py``）。
    """
    want_os, want_arch = go_os(sysname), go_arch(machine)
    assets = release.get("assets") or []
    best: tuple[int, str] | None = None
    for asset in assets:
        name = str(asset.get("name") or "")
        if not name.startswith("mihomo-"):
            continue
        if "-compatible" in name or "go1" in name:
            continue
        # 形如 mihomo-darwin-arm64-v1.19.2.gz / ....zip
        parts = name.split("-")
        if len(parts) < 4:
            continue
        if parts[1] != want_os or parts[2] != want_arch:
            continue
        if name.endswith(".gz"):
            score = 0  # 单文件，解压即用
        elif name.endswith(".zip"):
            score = 1  # 需要再解一层，Windows 上常见
        else:
            continue
        if best is None or score < best[0]:
            best = (score, name)
    return best[1] if best else None


def _fetch_json(url: str, timeout: float = 20.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"yi/{__version__}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_kernel(url: str | None = None, api_url: str | None = None) -> str:
    """下载 mihomo 内核到 ``~/.config/yi/bin/mihomo``，返回落盘路径。

    ``url`` 直接给压缩包的地址（绕墙、或想用特定版本时用）；
    不给就去 GitHub 查最新 release。
    """
    target = kernel_target_path()
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)

    if url:
        asset_url, asset_name = url, os.path.basename(url.split("?")[0])
    else:
        try:
            release = _fetch_json(api_url or MIHOMO_RELEASE_API)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ProxyError(
                f"连不上 GitHub 取内核列表（{exc}）。\n"
                f"  国内网络常见。可以直接给出压缩包地址绕过去，例如：\n"
                f"    yi fetch-kernel --url {MIHOMO_MIRROR_HINT}"
                f"https://github.com/MetaCubeX/mihomo/releases/download/v1.x.y/mihomo-"
                f"{go_os()}-{go_arch()}-v1.x.y.gz"
            ) from exc
        asset_name = kernel_asset_name(release)
        if not asset_name:
            tag = release.get("tag_name") or "?"
            raise ProxyError(
                f"{tag} 里没有适配 {go_os()}/{go_arch()} 的内核包。"
                f"用 --url 手工指定，或设 YI_MIHOMO 指向已有的 mihomo。"
            )
        asset_url = next(
            str(a.get("browser_download_url")) for a in release["assets"] if a.get("name") == asset_name
        )

    log.info("下载内核 %s", asset_name)
    tmp = target + ".part"
    try:
        request = urllib.request.Request(asset_url, headers={"User-Agent": f"yi/{__version__}"})
        with urllib.request.urlopen(request, timeout=120) as response, open(tmp, "wb") as out:
            shutil.copyfileobj(response, out)

        if asset_name.endswith(".gz"):
            with gzip.open(tmp, "rb") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        elif asset_name.endswith(".zip"):
            with zipfile.ZipFile(tmp) as archive:
                member = next(
                    (n for n in archive.namelist() if n.endswith("mihomo") or n.endswith("mihomo.exe")),
                    None,
                )
                if not member:
                    raise ProxyError(f"{asset_name} 里没找到可执行文件")
                with archive.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        else:
            shutil.move(tmp, target)
    except (urllib.error.URLError, OSError, TimeoutError, gzip.BadGzipFile) as exc:
        raise ProxyError(f"下载内核失败：{exc}") from exc
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    os.chmod(target, 0o755)
    return target


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def running_pid() -> int | None:
    if not os.path.exists(pid_path()):
        return None
    try:
        with open(pid_path(), encoding="utf-8") as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def write_config(info: dict[str, Any], port: int = DEFAULT_MIXED_PORT, label: str = "HK-Spot") -> str:
    state.ensure_home()
    os.makedirs(proxy_dir(), mode=0o700, exist_ok=True)
    # 控制接口走 unix socket：不占端口、不会和别的东西撞（状态查询/流量统计要用）
    yaml_text = configgen.render_mihomo(info, label, mixed_port=port, controller_socket=controller_socket())
    state.write_private(config_path(), yaml_text, 0o600)
    return config_path()


def controller_socket() -> str:
    return os.path.join(proxy_dir(), "mihomo.sock")


def stat(query: str, timeout: float = 4.0) -> dict[str, Any] | None:
    """通过控制 socket 问内核要状态（连接数、累计流量）。"""
    sock = controller_socket()
    if not os.path.exists(sock):
        return None
    try:
        completed = subprocess.run(
            [
                "curl",
                "-sS",
                "--max-time",
                str(int(timeout)),
                "--unix-socket",
                sock,
                "http://localhost" + query,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    import json

    try:
        return json.loads(completed.stdout)
    except ValueError:
        return None


def traffic() -> dict[str, Any]:
    """累计流量 + 当前连接数（界面上"确实在做事"的证据）。"""
    data = stat("/connections")
    if data is None:
        return {"available": False}
    connections = data.get("connections") or []
    return {
        "available": True,
        "download_total": data.get("downloadTotal") or 0,
        "upload_total": data.get("uploadTotal") or 0,
        "connections": len(connections),
    }


def start(info: dict[str, Any] | None = None, port: int | None = None, label: str = "HK-Spot") -> int:
    """起 mihomo。返回 pid。"""
    binary = mihomo_path()
    if not binary:
        raise ProxyError(
            "找不到代理内核 mihomo。\n"
            "  ./yi fetch-kernel          自动下载一份（需要能访问 GitHub）\n"
            f"  或者手工放到 {kernel_target_path()}，\n"
            "  或者设 YI_MIHOMO 指向已有的 mihomo。"
        )
    if running_pid():
        stop()

    if info is None:
        current = state.load_state() or {}
        info = current.get("server_info")
    if not info:
        raise ProxyError("还没有服务端参数，先跑一次 up（或等自动重建完成）")

    # 这次是要**新起**一个内核，所以可以挑端口（此时 running_pid 已经是空的）
    port = resolve_port(port)
    write_config(info, port, label)
    with open(log_path(), "ab") as logfile:
        process = subprocess.Popen(
            [binary, "-d", proxy_dir(), "-f", config_path()],
            stdout=logfile,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    state.write_private(pid_path(), str(process.pid), 0o600)

    deadline = time.time() + 10
    while time.time() < deadline:
        if process.poll() is not None:
            raise ProxyError(f"mihomo 启动即退出，看日志: {log_path()}")
        if _port_open(port):
            _set_applied(_fingerprint(info), port)
            log.info("mihomo 已就绪，混合端口 %d (pid %d)", port, process.pid)
            return process.pid
        time.sleep(0.3)
    raise ProxyError(f"mihomo 起来了但端口 {port} 没监听，看日志: {log_path()}")


def stop() -> bool:
    pid = running_pid()
    if pid is None:
        if os.path.exists(pid_path()):
            os.remove(pid_path())
        return False
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    for _ in range(20):
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    if os.path.exists(pid_path()):
        os.remove(pid_path())
    _set_applied_fingerprint(None)
    log.info("mihomo 已停止")
    return True


def reconcile(service: str | None = None) -> dict[str, Any]:
    """把实际状态收敛到期望状态。**这是连接可靠性的核心**。

    处理四种偏差：
      1. 期望连、内核没跑（崩了/被杀了）→ 起
      2. 期望连、内核在跑但配置指向旧服务端（竞价重建过）→ 用新配置重启
      3. 期望连、系统代理被人手动关了 → 重新打开
      4. 期望断、内核还在跑 → 停掉并还原系统代理
    """
    want = desired_connected()
    current = state.load_state() or {}
    info = current.get("server_info")
    running = running_pid() is not None
    actions: list[str] = []

    if not want:
        if running:
            stop()
            actions.append("stopped")
        if system_proxy_enabled():
            try:
                set_system_proxy(False, resolve_port(), service)
                actions.append("proxy-off")
            except ProxyError as exc:
                log.warning("还原系统代理失败: %s", exc)
        return {"want": want, "actions": actions}

    if not info:
        # 期望连但还没有服务端参数：把系统代理收回来，别让用户挂在死端口上
        if system_proxy_enabled():
            with contextlib.suppress(ProxyError):
                set_system_proxy(False)
                actions.append("proxy-off")
        return {"want": want, "actions": ["waiting-for-server"]}

    if not running or _applied_fingerprint() != _fingerprint(info):
        action = "restarted" if running else "started"
        try:
            start(info, None, current.get("label") or "HK-Spot")
            actions.append(action)
        except ProxyError as exc:
            # 起不来就别把系统代理指过去——否则用户整机断网（踩过）
            log.error("启动内核失败：%s", exc)
            if system_proxy_enabled():
                with contextlib.suppress(ProxyError):
                    set_system_proxy(False)
                    actions.append("proxy-off")
            actions.append("start-failed")
            return {"want": want, "actions": actions}

    port = resolve_port()
    if not system_proxy_enabled():
        # **开之前先确认端口真的在监听**：这是"永不把系统代理指向死端口"的兜底
        if not _port_open(port):
            log.error("端口 %d 没有监听，拒绝设置系统代理", port)
            actions.append("port-not-listening")
        else:
            try:
                set_system_proxy(True, port, service)
                actions.append("proxy-on")
            except ProxyError as exc:
                log.warning("系统代理没设上: %s", exc)
    return {"want": want, "actions": actions}


# --------------------------------------------------------------------------
# 系统代理（需要管理员权限）
# --------------------------------------------------------------------------
def network_service() -> str:
    """当前主网络服务名（Wi-Fi / 以太网 …）。"""
    try:
        completed = subprocess.run(
            ["networksetup", "-listallnetworkservices"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProxyError(f"读不到网络服务列表: {exc}") from exc
    lines = [line.strip() for line in completed.stdout.splitlines()[1:] if line.strip()]
    if not lines:
        raise ProxyError("没有可用的网络服务")
    # 第一条就是当前主服务（系统顺序即优先级）
    return lines[0].lstrip("*").strip()


def _admin_shell(command: str) -> subprocess.CompletedProcess:
    """通过系统授权框以管理员身份执行一条 shell 命令。"""
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    script = f'do shell script "{escaped}" with administrator privileges'
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120)


def set_system_proxy(
    enabled: bool, port: int = DEFAULT_MIXED_PORT, service: str | None = None
) -> dict[str, Any]:
    service = service or network_service()
    if enabled:
        # 硬保险：绝不把系统代理指向一个没人监听的端口。
        # 一旦指错，用户**整台机器**的浏览器都连不上（这个坑踩过一次，很难查）。
        if not _port_open(port):
            raise ProxyError(f"端口 {port} 没有程序在监听，拒绝设置系统代理（否则会断掉整机网络）")
        commands = [
            f'networksetup -setwebproxy "{service}" 127.0.0.1 {port}',
            f'networksetup -setsecurewebproxy "{service}" 127.0.0.1 {port}',
            f'networksetup -setsocksfirewallproxy "{service}" 127.0.0.1 {port}',
            f'networksetup -setproxybypassdomains "{service}" "*.local" "169.254/16" "127.0.0.1" '
            '"localhost" "10.0.0.0/8" "172.16.0.0/12" "192.168.0.0/16"',
        ]
    else:
        commands = [
            f'networksetup -setwebproxystate "{service}" off',
            f'networksetup -setsecurewebproxystate "{service}" off',
            f'networksetup -setsocksfirewallproxystate "{service}" off',
        ]
    for command in commands:
        # 实测：管理员账户直接跑 networksetup 就能改，不用密码。先免密试一次，
        # 真的被拒了才弹系统授权框——能不让用户输密码就别让。
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            log.info("免密改代理失败，改用管理员授权")
            result = _admin_shell(command)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            if "User canceled" in message or "-128" in message:
                raise ProxyError("用户取消了管理员授权")
            raise ProxyError(f"设置系统代理失败: {message[:200]}")
    log.info("系统代理已%s（%s, 端口 %d）", "开启" if enabled else "关闭", service, port)
    _SYS_PROXY.update({"ts": time.time(), "value": enabled})
    return {"service": service, "enabled": enabled, "port": port}


_SYS_PROXY: dict[str, Any] = {"ts": 0.0, "value": False}


def system_proxy_enabled(ttl: float = 2.0) -> bool:
    """系统代理开没开。

    读它要起一个 `scutil` 子进程（十几毫秒），而状态接口每秒都可能被问一次。
    缓存两秒：界面上的开关晚两秒更新没人看得出来，省下来的却是每次请求的固定开销。
    """
    now = time.time()
    if now - float(_SYS_PROXY["ts"]) < ttl:
        return bool(_SYS_PROXY["value"])
    try:
        completed = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5)
        value = "HTTPEnable : 1" in completed.stdout
    except (OSError, subprocess.SubprocessError):
        value = bool(_SYS_PROXY["value"])
    _SYS_PROXY.update({"ts": now, "value": value})
    return value


# --------------------------------------------------------------------------
# 验证：真的从香港出去了吗
# --------------------------------------------------------------------------
ECHO_URLS = ("https://api.ip.sb/ip", "https://ipinfo.io/ip", "https://ifconfig.me/ip")


def exit_ip(port: int | None = None, timeout: float = 12.0) -> str | None:
    port = port or resolve_port()
    for url in ECHO_URLS:
        try:
            completed = subprocess.run(
                ["curl", "-sS", "--max-time", str(int(timeout)), "-x", f"http://127.0.0.1:{port}", url],
                capture_output=True,
                text=True,
                timeout=timeout + 3,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        value = (completed.stdout or "").strip()
        if completed.returncode == 0 and value and len(value) < 64:
            return value
    return None


def verify(port: int | None = None) -> dict[str, Any]:
    """检查代理是否真的生效：出口 IP 是否等于服务端 IP。"""
    port = port or resolve_port()
    current = state.load_state() or {}
    info = current.get("server_info") or {}
    expected = info.get("address")
    actual = exit_ip(port)
    return {
        "ok": bool(actual and expected and actual == expected),
        "expected_exit": expected,
        "actual_exit": actual,
        "port": port,
        "note": (
            "出口与服务端一致"
            if actual and expected and actual == expected
            else ("代理没生效或出口不对" if actual else "走代理访问不通")
        ),
    }


def status(port: int | None = None) -> dict[str, Any]:
    """注意：端口要按**实际在用**的那个报，不能用写死的默认值（踩过：界面显示 7897，
    实际在 7899，用户按界面上的端口去排查会一头雾水）。"""
    port = port or resolve_port()
    pid = running_pid()
    return {
        "running": pid is not None,
        "pid": pid,
        "port": port,
        "listening": _port_open(port) if pid else False,
        "system_proxy": system_proxy_enabled(),
        "kernel": mihomo_path(),
        "config": config_path(),
    }
