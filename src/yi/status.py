"""全局状态的**唯一来源**。

以前 CLI、agent、UI 各自拼一套判断（机器在不在、连没连、内核活没活），
三处逻辑一旦不一致，界面就会撒谎。这里把它收敛成一个状态机 + 一个快照函数。

状态机：

    no_machine ──up──▶ installing ──就绪──▶ ready ──connect──▶ connected
        ▲                  │                    ▲                   │
        │              安装超时                │              内核挂了/出口不对
        │                  ▼                    │                   ▼
        └──down──▶ install_failed            ready            degraded

    机器被回收：reclaimed ──watch 自动重建──▶ installing
    有操作在跑：busy（provisioning / destroying / connecting / …）

优先级从上到下：busy > reclaimed > install_failed > installing > connected >
degraded > ready > no_machine。判断顺序很重要——"正在装"必须盖过"已就绪"，
"被回收"必须盖过"已连接"。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from . import proxy, state

log = logging.getLogger("yi.status")

# 安装多久没就绪就认为失败了（cloud-init 正常 1–3 分钟）
INSTALL_TIMEOUT_SECONDS = 8 * 60

# 云端核对结果缓存：UI 每 3 秒问一次，不能每次都打阿里云
_CLOUD: dict[str, Any] = {"ts": 0.0, "instance_id": None, "exists": None}
CLOUD_TTL = 20.0


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return parsed


def machine_exists_on_cloud(instance_id: str, region: str, force: bool = False) -> bool | None:
    """机器还在不在（带缓存）。None = 查不到（网络/权限问题），别据此下结论。"""
    now = time.time()
    if not force and _CLOUD["instance_id"] == instance_id and now - _CLOUD["ts"] < CLOUD_TTL:
        return _CLOUD["exists"]
    try:
        from .aliyun import EcsClient, load_credentials

        config = state.load_config()
        client = EcsClient(load_credentials(config.get("aliyun_profile", "default")), region)
        exists = client.instance_exists(instance_id)
    except Exception as exc:  # noqa: BLE001 - 核对失败不该让整个状态不可用
        log.warning("实例状态核对失败: %s", exc)
        return _CLOUD["exists"] if _CLOUD["instance_id"] == instance_id else None
    _CLOUD.update({"ts": now, "instance_id": instance_id, "exists": exists})
    return exists


def forget_cloud_cache() -> None:
    _CLOUD.update({"ts": 0.0, "instance_id": None, "exists": None})


def snapshot(
    busy: str | None = None,
    daemons: dict[str, Any] | None = None,
    check_cloud: bool = True,
    verify_exit: bool = False,
) -> dict[str, Any]:
    """当前一切状态。纯数据，给 CLI / agent / UI 共用。"""
    config = state.load_config()
    current = state.load_state() or {}
    info = current.get("server_info") or {}
    pstat = proxy.status()

    instance_id = current.get("instance_id")
    created = _parse_time(current.get("created_at"))
    hours = None
    if created:
        hours = round((datetime.now(UTC) - created).total_seconds() / 3600.0, 2)

    machine: dict[str, Any] = {
        "exists": bool(instance_id),
        "id": instance_id,
        "ip": current.get("public_ip"),
        "zone": current.get("zone_id"),
        "type": current.get("instance_type"),
        "created_at": current.get("created_at"),
        "hours": hours,
        "ready": bool(current.get("ready")),
        "spot": current.get("spot_strategy"),
        "price_limit": current.get("spot_price_limit"),
        "region": current.get("region") or config["region"],
    }

    reclaimed = False
    if instance_id and check_cloud and not busy:
        exists = machine_exists_on_cloud(instance_id, machine["region"])
        if exists is False:
            reclaimed = True
            machine.update({"exists": False, "reclaimed": True, "last_ip": machine["ip"]})

    connection = dict(pstat)
    connection.update(
        {
            "enabled": bool(pstat.get("running")),
            "http_port": pstat.get("port"),
            "socks_port": pstat.get("port"),
            "auto_reconnect": bool((daemons or {}).get("reconnect")),
        }
    )

    install_age = None
    if created:
        install_age = (datetime.now(UTC) - created).total_seconds()

    if busy:
        stage = "busy"
    elif reclaimed:
        stage = "reclaimed"
    elif machine["exists"] and not machine["ready"]:
        stage = "install_failed" if (install_age or 0) > INSTALL_TIMEOUT_SECONDS else "installing"
    elif connection["running"]:
        stage = "connected"
    elif machine["ready"]:
        stage = "ready"
    else:
        stage = "no_machine"

    result: dict[str, Any] = {
        "state": stage,
        "busy": busy,
        "machine": machine,
        "server": {
            "ready": bool(info),
            "address": info.get("address"),
            "port": info.get("port"),
            "sni": info.get("sni"),
            "xray_version": info.get("xray_version"),
            "selftest": info.get("selftest"),
            "identity": (info.get("uuid") or "")[:8],
        },
        "connection": connection,
        "daemons": daemons or {},
        "install_age_seconds": int(install_age) if install_age else None,
        "install_timeout": INSTALL_TIMEOUT_SECONDS,
        "config": {
            "region": config["region"],
            "zones": config["zones"],
            "instance_types": config["instance_types"],
            "spot_strategy": config["spot_strategy"],
            "spot_duration": config["spot_duration"],
            "spot_bid_multiplier": config.get("spot_bid_multiplier", 1.5),
            "reality_dests": config.get("reality_dests"),
            "xray_port": config["xray_port"],
            "xray_version": config["xray_version"],
            "budget": config["budget"],
            "domain": config.get("domain"),
            "subdomain": config.get("subdomain"),
            "mixed_port": config.get("mixed_port") or proxy.DEFAULT_MIXED_PORT,
        },
    }

    if verify_exit and connection["running"]:
        result["verify"] = proxy.verify(connection["port"])
        if stage == "connected" and not result["verify"]["ok"]:
            result["state"] = "degraded"
    return result


def summary_line(snap: dict[str, Any]) -> str:
    """一句话人话，给 CLI 和日志用。"""
    stage = snap["state"]
    machine = snap["machine"]
    if stage == "busy":
        return "正在执行：{}".format(snap.get("busy"))
    if stage == "reclaimed":
        return "机器已被回收（{}），等待自动重建".format(machine.get("last_ip") or "?")
    if stage == "install_failed":
        return "安装失败：机器已开 {} 秒仍未就绪，建议重建".format(snap.get("install_age_seconds"))
    if stage == "installing":
        return "正在安装服务端（{} 秒）".format(snap.get("install_age_seconds"))
    if stage == "connected":
        return "已连接 · 出口 {}".format(snap["server"].get("address"))
    if stage == "degraded":
        return "连接异常：内核在跑，但出口校验没过"
    if stage == "ready":
        return "机器就绪，尚未连接"
    return "还没有机器"
