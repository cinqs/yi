"""阿里云 AccessKey 的读写与"掩码展示"。

写在包里而不是界面里：`tools/set-credentials.sh`、App、以后可能的别处都得用
同一套格式，分散开就会出现"界面写进去了但 CLI 读不到"这种最难查的问题。

两条硬规矩：

1. **密钥永远不往回传**。界面只能看到掩码，改的时候是整体覆盖，不做"回显再编辑"。
2. **文件权限 0600，且保留其它 profile**。用户可能还有别的账号配在里面，
   我们只动自己那一条。
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import aliyun

DEFAULT_PROFILE = "default"

# 环境变量优先于文件 —— 和 aliyun.load_credentials 的取舍保持一致。
ENV_VARS = (
    ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET"),
    ("ALIYUN_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_SECRET"),
)


def config_path(home: str | None = None) -> str:
    return os.path.join(home or os.path.expanduser("~"), ".aliyun", "config.json")


def mask(access_key_id: str | None) -> str:
    """`LTAIEXAMPLEKEY0000PSF1` → `LTAI****PSF1`。

    日志里一直是这个写法（`使用 AccessKey LTAI****PsF1（来自 env）`），
    界面上也保持一样，用户一眼能把两处对上。
    """
    value = str(access_key_id or "")
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}****{value[-4:]}"


def _from_env() -> tuple[str, str] | None:
    for id_var, secret_var in ENV_VARS:
        key_id, secret = os.environ.get(id_var), os.environ.get(secret_var)
        if key_id and secret:
            return key_id, secret
    return None


def describe(profile: str = DEFAULT_PROFILE, home: str | None = None) -> dict[str, Any]:
    """给界面看的凭据概况。**不含密钥**。"""
    env = _from_env()
    path = config_path(home)
    info: dict[str, Any] = {
        "profile": profile,
        "path": path,
        "configured": False,
        "access_key_id": "",
        "source": "",
        "source_label": "",
        "env_override": bool(env),
    }

    if env:
        # 环境变量优先级最高：就算文件里也配了，生效的也是它。
        # 不把这个说清楚，用户会以为"我改了文件怎么没用"。
        info.update(
            configured=True,
            access_key_id=mask(env[0]),
            source="env",
            source_label="环境变量（优先级最高，会盖过配置文件）",
        )
        return info

    if not os.path.exists(path):
        info["source_label"] = f"还没有配置文件（{path}）"
        return info

    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        info["source_label"] = f"配置文件读不了：{exc}"
        return info

    chosen = _pick_profile(payload, profile)
    if chosen is None:
        names = "、".join(sorted(str(p.get("name")) for p in payload.get("profiles") or []))
        info["source_label"] = f"文件里没有 profile「{profile}」（现有：{names or '无'}）"
        return info

    info.update(
        configured=True,
        access_key_id=mask(chosen.get("access_key_id")),
        source=f"{path}#{chosen.get('name')}",
        source_label=f"{path} 里的 profile「{chosen.get('name')}」",
        # 回传**实际生效**的 profile 名：界面保存时原样送回来，就覆盖同一条，
        # 而不是新建一个叫 default 的、把旧的那条留在那里吃灰。
        profile=str(chosen.get("name") or profile),
    )
    return info


def _pick_profile(payload: dict[str, Any], profile: str) -> dict[str, Any] | None:
    """和 aliyun.load_credentials 用同一套挑选逻辑，免得两边"看到的账号不一样"。"""
    profiles = [p for p in (payload.get("profiles") or []) if isinstance(p, dict)]
    for item in profiles:
        if item.get("name") == profile:
            return item
    current = payload.get("current")
    if profile == DEFAULT_PROFILE and current:
        for item in profiles:
            if item.get("name") == current:
                return item
    if len(profiles) == 1:
        return profiles[0]
    return None


def save(
    access_key_id: str,
    access_key_secret: str,
    profile: str = DEFAULT_PROFILE,
    home: str | None = None,
) -> str:
    """写入 `~/.aliyun/config.json`，保留其它 profile。返回文件路径。

    格式和 `tools/set-credentials.sh` 完全一致（它就是 aliyun CLI 的格式）。
    """
    key_id = str(access_key_id or "").strip()
    secret = str(access_key_secret or "").strip()
    if not key_id or not secret:
        raise ValueError("AccessKey ID 和 Secret 都要填")
    if key_id.startswith("LTAI") is False and len(key_id) < 16:
        raise ValueError("AccessKey ID 看起来不对（通常以 LTAI 开头）")

    path = config_path(home)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)

    data: dict[str, Any] = {"current": profile, "profiles": [], "meta": ""}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                existing = json.load(handle)
        except (OSError, ValueError):
            existing = None
        if isinstance(existing, dict):
            data.update({k: v for k, v in existing.items() if k != "profiles"})
            data["profiles"] = [item for item in (existing.get("profiles") or []) if isinstance(item, dict)]

    data["profiles"] = [item for item in data["profiles"] if item.get("name") != profile]
    data["profiles"].append(
        {
            "name": profile,
            "mode": "AK",
            "access_key_id": key_id,
            "access_key_secret": secret,
        }
    )
    data["current"] = profile

    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def verify(access_key_id: str, access_key_secret: str, region: str = "cn-hongkong") -> list[str]:
    """拿这对凭据真调一次最轻的接口，返回区域列表；失败抛 AliyunError。

    只写文件不验证的话，写错的密钥要等到下次 `up` 才炸 —— 而那时候已经花掉
    几分钟和一次实例创建。
    """
    creds = aliyun.Credentials(str(access_key_id or "").strip(), str(access_key_secret or "").strip(), "输入")
    client = aliyun.EcsClient(creds, region or "cn-hongkong")
    return client.describe_regions()
