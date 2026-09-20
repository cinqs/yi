"""凭据持久化：让竞价实例重建之后，客户端不用重新导入配置。

为什么需要它
------------
抢占式实例被回收是常态。如果每次重建都生成一套新的 UUID / REALITY 密钥，
手机和电脑上的配置就全部失效，必须重新导入——一天回收两次就是两次手工操作。

把凭据存在本地（`~/.config/yi/identity.json`，0600），重建时注入新实例，
客户端那边什么都不用动。

代价（要在文档里说清楚）
------------------------
REALITY 私钥以前只存在于服务器上，现在会在你本机落盘。它能用来冒充"你这台服务器"，
但还需要同时知道 IP 和端口，而且它跟你的阿里云账号凭据无关。可接受，但不是零风险。

密钥怎么生成
------------
用 openssl 生成 X25519 密钥对（macOS 自带，实测与 Xray 完全兼容）：
    openssl genpkey -algorithm X25519
私钥/公钥都取 DER 的最后 32 字节，转 base64url。
没有 openssl 时返回 None，交给服务端现场生成（这种情况凭据就无法持久化）。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import uuid as _uuid
from typing import Any

from . import state

log = logging.getLogger("yi.identity")


class IdentityError(RuntimeError):
    pass


def identity_path() -> str:
    return os.path.join(state.home_dir(), "identity.json")


def load_identity() -> dict[str, Any] | None:
    path = identity_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("凭据文件坏了（%s），将重新生成: %s", path, exc)
        return None
    required = ("uuid", "short_id", "private_key", "public_key")
    if not all(data.get(key) for key in required):
        log.warning("凭据文件缺字段，将重新生成: %s", path)
        return None
    return data


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_keypair() -> dict[str, str] | None:
    """返回 {"private_key", "public_key"}；openssl 不可用时返回 None。"""
    try:
        created = subprocess.run(
            ["openssl", "genpkey", "-algorithm", "X25519"],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("调用 openssl 失败: %s", exc)
        return None
    if created.returncode != 0 or not created.stdout:
        log.warning("openssl 生成 X25519 失败: %s", created.stderr.decode("utf-8", "replace")[:200])
        return None

    pem = created.stdout
    try:
        private = subprocess.run(
            ["openssl", "pkey", "-outform", "DER"],
            input=pem,
            capture_output=True,
            timeout=15,
            check=True,
        ).stdout[-32:]
        public = subprocess.run(
            ["openssl", "pkey", "-pubout", "-outform", "DER"],
            input=pem,
            capture_output=True,
            timeout=15,
            check=True,
        ).stdout[-32:]
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("从 openssl 输出里取密钥失败: %s", exc)
        return None
    if len(private) != 32 or len(public) != 32:
        log.warning("openssl 返回的密钥长度不对（%d/%d）", len(private), len(public))
        return None
    return {"private_key": _b64url(private), "public_key": _b64url(public)}


def new_identity() -> dict[str, Any]:
    pair = generate_keypair()
    if pair is None:
        raise IdentityError(
            "本机没有可用的 openssl，无法在本地生成 REALITY 密钥。\n"
            "  · macOS 自带 openssl，出现这个说明 PATH 有问题\n"
            "  · 也可以直接跳过：不持久化凭据，每次重建由服务器现场生成"
            "（代价是客户端要重新导入配置）"
        )
    return {
        "uuid": str(_uuid.uuid4()),
        "short_id": os.urandom(8).hex(),
        "private_key": pair["private_key"],
        "public_key": pair["public_key"],
    }


def ensure_identity(rotate: bool = False) -> dict[str, Any]:
    """拿到要用的凭据；没有就生成一套并存盘。"""
    state.ensure_home()
    if not rotate:
        existing = load_identity()
        if existing:
            return existing
    identity = new_identity()
    _save(identity)
    log.info("已生成并保存凭据到 %s（重建机器时会复用这套，客户端无需重新导入）", identity_path())
    return identity


def _save(identity: dict[str, Any]) -> None:
    state.write_private(identity_path(), json.dumps(identity, indent=2) + "\n", 0o600)


def fingerprint(identity: dict[str, Any]) -> str:
    """给日志用：只露 UUID 前 8 位，别把私钥打进日志。"""
    return str(identity.get("uuid") or "")[:8]
