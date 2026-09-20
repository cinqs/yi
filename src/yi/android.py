"""把 Android 客户端交到用户手上。

为什么有这个模块：Android 端我们**不自研客户端**——用成熟的 v2rayNG。
但它的下载页在 GitHub Releases 上，国内经常打不开，于是"能不能用上代理"的
第一步就卡住了：机器买好了、服务端装好了、订阅也生成了，人却装不上客户端。

所以这里只做一件事：把官方签名过的 APK 取回来。
不重新打包、不重新签名、不改一行代码 —— 那样只会削弱可信度。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.request
from typing import Any

from . import __version__


class AndroidError(RuntimeError):
    pass


RELEASE_API = "https://api.github.com/repos/2dust/v2rayNG/releases/latest"
PACKAGE_NAME = "com.v2ray.ang"
# App 外壳里的 agent 端口（app/agent.py 与 app/macos/main.swift 用的是同一个）。
AGENT_PORT = 8765

# 主流手机都是 arm64；32 位和老 x86 设备仍给出口，只是不做默认。
SUPPORTED_ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64", "x86")
# playstore 渠道走 Google 的签名与更新；fdroid 渠道不含 Google 相关依赖。
DEFAULT_FLAVOR = "playstore"


def pick_apk_asset(
    release: dict[str, Any],
    abi: str = "arm64-v8a",
    flavor: str = DEFAULT_FLAVOR,
) -> str | None:
    """从 release 里挑出「指定架构 + 指定渠道」的那个 APK。

    纯函数，选错了用户会装上一个跑不起来的包，所以在
    ``tests/test_android.py`` 里单独钉死。
    """
    if abi not in SUPPORTED_ABIS:
        raise AndroidError(f"不支持的架构 {abi!r}，可选：{'、'.join(SUPPORTED_ABIS)}")

    suffix = f"_{abi}.apk"
    candidates = []
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "")
        # `.apk.sig` 也以 .apk 开头，但必须以 .apk 结尾才算包
        if not name.startswith("v2rayNG_") or not name.endswith(suffix):
            continue
        is_fdroid = "-fdroid_" in name
        # 先按用户要的渠道找；找不到再退到另一个渠道，
        # 因为上游偶尔只发其中一个（两个渠道功能一样，装哪个都能用）。
        candidates.append((0 if is_fdroid == (flavor == "fdroid") else 1, name))
    if not candidates:
        return None
    return sorted(candidates)[0][1]


def _fetch_release(api_url: str, timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(
        api_url,
        headers={"User-Agent": f"yi/{__version__}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def download_apk(
    dest_dir: str,
    abi: str = "arm64-v8a",
    flavor: str = DEFAULT_FLAVOR,
    url: str | None = None,
    api_url: str | None = None,
) -> tuple[str, str, str]:
    """下载官方 APK。返回 ``(路径, sha256, 版本号)``。

    ``url`` 直接给 APK 地址（绕墙或钉住版本时用）；不给就去 GitHub 查最新发布。
    """
    os.makedirs(dest_dir, exist_ok=True)

    if url:
        asset_url = url
        asset_name = os.path.basename(url.split("?")[0]) or "v2rayNG.apk"
        tag = ""
    else:
        try:
            release = _fetch_release(api_url or RELEASE_API)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise AndroidError(
                f"连不上 GitHub 取发布信息（{exc}）。\n"
                "  国内网络常见。有两种绕法：\n"
                "    1. 先连上代理再跑：./yi connect && ./yi android\n"
                "    2. 直接给出 APK 地址：./yi android --url <地址>"
            ) from exc

        tag = release.get("tag_name") or ""
        asset_name = pick_apk_asset(release, abi=abi, flavor=flavor)
        if not asset_name:
            raise AndroidError(f"{tag} 里没有 {abi} 的 APK。用 --abi 换一个架构，或 --url 手工指定。")
        asset_url = next(
            str(a.get("browser_download_url")) for a in release["assets"] if a.get("name") == asset_name
        )

    dest = os.path.join(dest_dir, asset_name)
    tmp = dest + ".part"
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(asset_url, headers={"User-Agent": f"yi/{__version__}"})
        with urllib.request.urlopen(request, timeout=300) as response, open(tmp, "wb") as out:
            while chunk := response.read(1 << 16):
                digest.update(chunk)
                out.write(chunk)
        shutil.move(tmp, dest)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise AndroidError(f"下载 APK 失败：{exc}") from exc
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    return dest, digest.hexdigest(), tag


def next_steps(apk_path: str, subscription_url: str | None) -> str:
    """装完之后照着做就行，不用回去翻 README。"""
    lines = [
        "",
        "接下来：",
        f"  1. 把 {os.path.basename(apk_path)} 传到手机（AirDrop / 数据线 / 网盘都行），",
        "     点开允许「安装未知来源应用」。",
        "  2. 打开 v2rayNG → 右下角 + → 从剪贴板导入。",
    ]
    if subscription_url:
        lines += [
            "  3. 或者用订阅（机器换 IP 时手机端不用重配）：",
            f"     v2rayNG → 订阅 → 添加订阅 → 粘贴：{subscription_url}",
            "     手机要和这台电脑在同一个 Wi-Fi 下。",
        ]
    lines.append("")
    lines.append("说明：这个 APK 是 v2rayNG 官方发布、官方签名的原版，")
    lines.append("      这里只是替你把它下下来（GitHub Releases 国内常常打不开）。")
    return "\n".join(lines)


def running_subscription_url(timeout: float = 2.0) -> str | None:
    """本地 agent 在跑的话，把它的订阅地址要过来；不在跑就返回 None。

    不自己拼这个地址：订阅只在 agent 活着的时候有效，而且拼错一个字段用户
    就会对着一个死链排查半天。直接问它是唯一不会说谎的做法。
    """
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{AGENT_PORT}/api/state")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            snapshot = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None

    sub = snapshot.get("subscription") or {}
    if not sub.get("available") or not sub.get("token"):
        return None
    return f"http://{sub.get('lan_ip')}:{AGENT_PORT}/sub/{sub['token']}"
