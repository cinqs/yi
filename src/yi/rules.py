"""社区维护的分流规则集。

自己手写直连清单走不远：`.cn` 之外还有一大堆国内站点，而且每天都在变。
社区有人专门在做这件事（Loyalsoldier/clash-rules 等），做得比我们好 —— 那就用他们的。

但直接接上去有两个坑，这个模块就是来处理这两个坑的：

1. **国内下不动**。规则集挂在 GitHub 上（`raw.githubusercontent.com` 实测直接超时）。
   所以这里：默认走可用镜像、必要时走**我们自己的代理**去取、取回来落在本地缓存。
   内核启动时读缓存，不依赖外网可达。

2. **失败是静默的**。实测过：规则集下载失败时 mihomo 照样正常启动，只是那几条
   `RULE-SET` 一条都匹配不上 —— 表现是"规则看着配了，其实一点没生效"，而且
   没有任何报错。所以状态由我们自己记录，`./yi rules` 能说实话。

这也是为什么内置的基础规则（局域网直连、GEOIP,CN）**一直保留**：
规则集是增强，不是依赖。它挂了，你也不会因此把内网流量送去香港。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import __version__, state

log = logging.getLogger("yi.rules")


class RulesError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuleSet:
    name: str  # 配置里的键名，也是缓存文件名
    behavior: str  # domain / ipcidr
    filename: str  # 上游仓库里的文件名


# 上游：Loyalsoldier/clash-rules 的 release 分支。
# 选它的理由：纯文本 YAML、能直接打开看、社区用得多、更新勤。
UPSTREAM = "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release"

# 镜像顺序 = 实测的国内可达性顺序（2026-09 实测）：
#   raw.githubusercontent.com  超时不可达
#   cdn.jsdelivr.net           2.5s ✅
#   testingcf.jsdelivr.net     1.6s ✅
#   ghproxy.net                2.3s ✅
#   gh-proxy.com               7.5s ✅
# 最后仍然把上游本体放进去：在国外或者连着代理时它才是最快的。
DEFAULT_MIRRORS = (
    "https://cdn.jsdelivr.net/gh/Loyalsoldier/clash-rules@release",
    "https://testingcf.jsdelivr.net/gh/Loyalsoldier/clash-rules@release",
    "https://ghproxy.net/https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release",
    "https://gh-proxy.com/https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release",
    UPSTREAM,
)

RULE_SETS: tuple[RuleSet, ...] = (
    RuleSet("reject", "domain", "reject.txt"),
    RuleSet("private", "domain", "private.txt"),
    RuleSet("lancidr", "ipcidr", "lancidr.txt"),
    RuleSet("icloud", "domain", "icloud.txt"),
    RuleSet("apple", "domain", "apple.txt"),
    RuleSet("google", "domain", "google.txt"),
    RuleSet("proxy", "domain", "proxy.txt"),
    RuleSet("direct", "domain", "direct.txt"),
    RuleSet("cncidr", "ipcidr", "cncidr.txt"),
    RuleSet("gfw", "domain", "gfw.txt"),
    RuleSet("greatfire", "domain", "greatfire.txt"),
    RuleSet("telegramcidr", "ipcidr", "telegramcidr.txt"),
)

# 每个规则集在 rules: 里的目标和先后。
#
# **顺序是有讲究的**，照社区公认的排法：广告/追踪挡在最前，"被墙的"放最后
# （前面都没命中才轮到它），中间按服务分流。改顺序前先想清楚会不会让某条规则
# 永远轮不到。
RULE_TARGETS: tuple[tuple[str, str], ...] = (
    ("reject", "REJECT"),
    ("private", "DIRECT"),
    ("lancidr", "DIRECT"),
    ("icloud", "DIRECT"),
    ("apple", "DIRECT"),
    ("google", "PROXY"),
    ("proxy", "PROXY"),
    ("direct", "DIRECT"),
    ("cncidr", "DIRECT"),
    ("gfw", "PROXY"),
    ("greatfire", "PROXY"),
    ("telegramcidr", "PROXY"),
)


def ruleset_dir() -> str:
    """规则集缓存目录。

    必须是 **mihomo 工作目录下的 `ruleset/`**：生成的配置里写的是
    `path: ./ruleset/<name>.yaml`，内核按自己的工作目录（`mihomo -d`）解析它。
    放到别处就会"文件明明在，内核却说没有"。
    """
    return os.path.join(state.home_dir(), "mihomo", "ruleset")


def local_path(name: str) -> str:
    return os.path.join(ruleset_dir(), f"{name}.yaml")


def mirrors(config: dict[str, Any] | None = None) -> list[str]:
    """镜像顺序：配置里写的优先，没写或写空了用实测过的默认顺序。"""
    configured = (config or {}).get("ruleset_mirrors")
    if isinstance(configured, (list, tuple)) and configured:
        return [str(m).rstrip("/") for m in configured]
    return list(DEFAULT_MIRRORS)


def _opener(proxy: str | None) -> urllib.request.OpenerDirector:
    handlers: list[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def detect_proxy() -> str | None:
    """内核在跑就用它当代理去取规则集。

    这是最可靠的一条路：镜像随时可能挂，但我们自己那台香港机器的连通性是
    已知的。所以 `--update` 会优先走它。
    """
    from . import proxy  # 延迟导入：避免 state→rules→proxy 的环形依赖

    if not proxy.running_pid():
        return None
    port = proxy.resolve_port()
    return f"http://127.0.0.1:{port}"


def count_rules(path: str) -> int:
    """数一下 payload 里有几条。用来区分"下下来了"和"下下来是个空文件"。"""
    if not os.path.exists(path):
        return 0
    total = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("'") or stripped.startswith('"'):
                total += 1
    return total


def status(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """每个规则集：在不在本地、多少条、什么时候取的、多大。"""
    out = []
    for rs in RULE_SETS:
        path = local_path(rs.name)
        exists = os.path.exists(path)
        out.append(
            {
                "name": rs.name,
                "behavior": rs.behavior,
                "path": path,
                "present": exists,
                "rules": count_rules(path) if exists else 0,
                "bytes": os.path.getsize(path) if exists else 0,
                "age_hours": round((time.time() - os.path.getmtime(path)) / 3600, 1) if exists else None,
            }
        )
    return out


def stale_hours(config: dict[str, Any] | None = None) -> float:
    cfg = config or state.load_config()
    return float(cfg.get("ruleset_max_age_hours") or 168)  # 默认一周


def needs_update(config: dict[str, Any] | None = None) -> bool:
    """有缺失、或者是空文件、或者太旧 —— 都算需要更新。"""
    limit = stale_hours(config)
    for item in status(config):
        if not item["present"] or item["rules"] == 0:
            return True
        if item["age_hours"] is not None and item["age_hours"] > limit:
            return True
    return False


def fetch_one(
    rule_set: RuleSet,
    mirror: str,
    proxy: str | None = None,
    timeout: float = 45.0,
) -> int:
    """从指定镜像取一个规则集，成功返回条数。失败抛 RulesError。"""
    url = f"{mirror.rstrip('/')}/{rule_set.filename}"
    dest = local_path(rule_set.name)
    os.makedirs(ruleset_dir(), mode=0o700, exist_ok=True)
    tmp = dest + ".part"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": f"yi/{__version__}"})
        with _opener(proxy).open(request, timeout=timeout) as response, open(tmp, "wb") as out:
            shutil.copyfileobj(response, out)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RulesError(f"{url} → {exc}") from exc

    # 下载"成功"但内容是空的（镜像返回一个空页）也要当成失败：
    # 否则内核用着一个空规则集，表现是"规则完全没生效"，还查不出原因。
    count = count_rules(tmp)
    if count == 0:
        os.remove(tmp)
        raise RulesError(f"{url} → 内容是空的，不像规则集")
    shutil.move(tmp, dest)
    os.chmod(dest, 0o600)
    return count


def fetch_all(
    config: dict[str, Any] | None = None,
    proxy: str | None = None,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
    autodetect: bool = True,
) -> dict[str, Any]:
    """把全部规则集取到本地。

    对每个规则集，**按镜像顺序逐个试**：一个镜像能取到 direct.txt 不代表能取到
    reject.txt，所以失败就换下一个，而不是整批放弃。

    `proxy=None` + `autodetect=True` 表示"内核在跑就走它"；要**强制直连**必须
    传 `autodetect=False` —— 只传 `proxy=None` 是不够的，那和"没指定"长得一样。
    """
    cfg = config or state.load_config()
    say = progress or (lambda _msg: None)
    use_proxy = detect_proxy() if (proxy is None and autodetect) else proxy
    if use_proxy:
        say(f"经代理取规则集：{use_proxy}")

    candidates = mirrors(cfg)
    fetched: list[str] = []
    failed: list[dict[str, str]] = []
    bytes_total = 0
    rules_total = 0

    for rule_set in RULE_SETS:
        path = local_path(rule_set.name)
        if not force and os.path.exists(path) and count_rules(path) > 0:
            age_h = (time.time() - os.path.getmtime(path)) / 3600
            if age_h <= stale_hours(cfg):
                say(f"  跳过 {rule_set.name}（{age_h:.0f} 小时前取的，还新鲜）")
                continue

        last_error = ""
        for mirror in candidates:
            try:
                count = fetch_one(rule_set, mirror, proxy=use_proxy)
            except RulesError as exc:
                last_error = str(exc)
                continue
            size = os.path.getsize(path)
            bytes_total += size
            rules_total += count
            fetched.append(rule_set.name)
            say(f"  ✓ {rule_set.name:<13} {count:>6} 条  {size / 1024:>7.0f} KB  ← {mirror.split('/')[2]}")
            break
        else:
            failed.append({"name": rule_set.name, "error": last_error})
            say(f"  ✗ {rule_set.name:<13} 全部镜像都失败：{last_error}")

    return {
        "fetched": fetched,
        "failed": failed,
        "bytes": bytes_total,
        "rules": rules_total,
        "proxy": use_proxy,
    }


def reload_running_kernel(timeout: float = 5.0) -> bool:
    """让正在跑的内核重新读一遍配置。

    为什么需要：内核启动时如果规则集还没下下来，它内存里那份是空的；我们事后
    把文件写进去，它**不会**立刻发现（要等它自己的 interval）。
    能重载就重载，不能就算了 —— 用户下次重连也会生效，不值得为它冒风险。
    """
    from . import proxy  # 延迟导入，理由同 detect_proxy

    if not proxy.running_pid():
        return False
    try:
        proxy.reload_config(timeout=timeout)
        return True
    except Exception as exc:  # noqa: BLE001 - 重载失败不该影响取规则集这件正事
        log.info("内核重载失败（不影响规则集本身）：%s", exc)
        return False
