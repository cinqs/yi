"""Thin wrapper around the system ssh client.

Using the OpenSSH binary instead of paramiko keeps the tool dependency-free and
means the user's own agent/config behaviour applies when they run ``yi ssh``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from typing import Any

from . import state

log = logging.getLogger("yi.ssh")


class SshError(RuntimeError):
    pass


def _options(key: str) -> list[str]:
    return [
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={state.known_hosts_path()}",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "ServerAliveInterval=15",
        "-i",
        key,
    ]


def run(
    host: str,
    command: str,
    key: str | None = None,
    user: str = "root",
    timeout: float = 30.0,
    check: bool = True,
) -> str:
    if shutil.which("ssh") is None:
        raise SshError("找不到 ssh 命令")
    key = key or state.key_path()
    argv = ["ssh"] + _options(key) + [f"{user}@{host}", command]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        # 阿里云会把回收的 IP 重新分配给新实例，于是同一个 IP 的主机密钥会变。
        # `accept-new` 对"变更过的密钥"是拒绝的（这是对的，防 MITM），但对
        # 我们自己刚创建的实例来说，这只是"换了台新机器"。清掉旧记录重试一次。
        if completed.returncode != 0 and "REMOTE HOST IDENTIFICATION HAS CHANGED" in (completed.stderr or ""):
            log.warning("%s 的 SSH 主机密钥变了（这个 IP 以前是别的机器），清掉旧记录重试", host)
            forget_host(host)
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SshError(f"ssh 到 {host} 超时") from exc
    if check and completed.returncode != 0:
        raise SshError(
            "ssh 到 {} 失败（exit {}）: {}".format(
                host, completed.returncode, (completed.stderr or "").strip()[:400]
            )
        )
    return completed.stdout


def forget_host(host: str) -> None:
    """删掉 known_hosts 里某个主机的旧记录（只动我们自己的那个文件）。"""
    path = state.known_hosts_path()
    if not os.path.exists(path):
        return
    try:
        subprocess.run(
            ["ssh-keygen", "-R", host, "-f", path],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("清理 known_hosts 失败: %s", exc)


def interactive(host: str, key: str | None = None, user: str = "root") -> int:
    key = key or state.key_path()
    argv = ["ssh"] + _options(key) + [f"{user}@{host}"]
    return subprocess.call(argv)


def wait_ready(
    host: str,
    key: str | None = None,
    user: str = "root",
    timeout: float = 420.0,
    interval: float = 6.0,
    progress=None,
) -> dict[str, Any]:
    """Block until cloud-init has written a *ready* server-info.json."""
    deadline = time.time() + timeout
    last_error = "cloud-init 还没写完 /root/yi/server-info.json"
    while time.time() < deadline:
        if progress:
            progress(timeout - (deadline - time.time()))
        try:
            raw = run(host, "cat /root/yi/server-info.json 2>/dev/null", key=key, user=user, check=False)
            payload = raw.strip()
            if payload:
                info = json.loads(payload)
                if info.get("ready"):
                    return info
                last_error = "服务端自检未通过: {}".format(info.get("error") or info)
        except (SshError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(interval)
    raise SshError(f"等待实例就绪超时: {last_error}")


def tail_bootstrap_log(host: str, key: str | None = None, user: str = "root", lines: int = 40) -> str:
    return run(
        host,
        f"tail -n {int(lines)} /var/log/yi-bootstrap.log 2>/dev/null || true",
        key=key,
        user=user,
        check=False,
    )


def read_remote(host: str, path: str, key: str | None = None, user: str = "root") -> str:
    return run(host, f"cat {path}", key=key, user=user, check=False)


def ensure_key_permissions() -> None:
    path = state.key_path()
    if os.path.exists(path):
        os.chmod(path, 0o600)
