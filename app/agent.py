#!/usr/bin/env python3
"""yi.app 的本地 agent：把 yi 的能力暴露成 HTTP + SSE。

三件事：

1. 不重新实现任何东西——买机器、装服务端、连接全部调用 yi 的既有代码。
2. 状态只有一份——yi.status.snapshot() 是唯一来源，CLI 和界面读同一份。
3. 后台只有一个调和循环——不断把实际状态收敛到期望状态（内核崩了、机器换了、
   系统代理被关了，都会自己纠正）。守护线程不直接做业务，只调用 proxy.reconcile()。
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import logging
import os
import queue
import re
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_package_root() -> str | None:
    """找出 `yi` 包在哪。

    这个文件有**两种**摆放方式，必须都能跑：
      源码布局  <repo>/app/agent.py            → 包在 <repo>/src/yi
      打包布局  <app>/Contents/Resources/agent.py → 包在 Resources/src/yi
    只按源码布局写死 `dirname(HERE)/src` 的话，装进 App 之后 import 就失败——
    而这类失败在开发机上会被"我自己装过 yi"掩盖掉，非等到干净环境才暴露。
    """
    for candidate in (
        os.path.join(os.path.dirname(HERE), "src"),  # 源码布局
        os.path.join(HERE, "src"),  # 打包布局
    ):
        if os.path.isdir(os.path.join(candidate, "yi")):
            return candidate
    return None


_PACKAGE_ROOT = _locate_package_root()
if _PACKAGE_ROOT:
    sys.path.insert(0, _PACKAGE_ROOT)

from yi import __version__, proxy, rules, state, status  # noqa: E402
from yi.cli import (  # noqa: E402
    cmd_connect,
    cmd_disconnect,
    cmd_down,
    cmd_rules,
    cmd_selftest,
    cmd_up,
    cmd_watch,
)

UI_DIR = os.path.join(HERE, "ui")
RECONCILE_INTERVAL = 8.0


class EventBus:
    """环形缓冲 + 实时订阅。

    缓冲很重要：界面通常是在动作开始之后才连上来的，没有回放就会看到一片空白。
    """

    def __init__(self, history: int = 400) -> None:
        self._lock = threading.Lock()
        self._history: list[dict[str, Any]] = []
        self._subscribers: list[queue.Queue] = []
        self._limit = history

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        event = dict(payload)
        event["kind"] = kind
        event["ts"] = datetime.now(UTC).strftime("%H:%M:%S")
        with self._lock:
            self._history.append(event)
            del self._history[: max(0, len(self._history) - self._limit)]
            for subscriber in list(self._subscribers):
                with contextlib.suppress(queue.Full):
                    subscriber.put_nowait(event)

    def subscribe(self) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            for event in self._history:
                with contextlib.suppress(queue.Full):
                    subscriber.put_nowait(event)
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._lock:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(subscriber)

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history)


BUS = EventBus()

_STAGE_PATTERNS: list[Any] = [
    (re.compile(r"使用 AccessKey"), "auth", "已认证阿里云账号"),
    (re.compile(r"选中镜像"), "image", "选择系统镜像"),
    (re.compile(r"复用交换机|默认交换机"), "network", "准备网络（交换机 / 安全组）"),
    (re.compile(r"安全组.*(就绪|满足|放行)"), "firewall", "配置安全组"),
    (re.compile(r"尝试创建"), "create", "创建竞价实例"),
    (re.compile(r"已提交，等待 Running"), "boot", "等待实例启动"),
    (re.compile(r"显式启动它"), "boot", "实例以停止态创建，正在启动它"),
    (re.compile(r"调用 AllocatePublicIpAddress"), "ip", "分配公网 IP"),
    (re.compile(r"等待 cloud-init"), "install", "安装 Xray 服务端"),
    (re.compile(r"最终伪装目标"), "ready", "服务端就绪"),
    (re.compile(r"回滚"), "rollback", "失败，回滚清理"),
]


class BusHandler(logging.Handler):
    """把 yi 的日志同时喂给事件总线（界面因此能看到后端真实在做什么）。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            return
        event: dict[str, Any] = {"level": record.levelname, "message": message}
        for pattern, stage, label in _STAGE_PATTERNS:
            if pattern.search(message):
                event["stage"] = stage
                event["stage_label"] = label
                break
        BUS.publish("log", event)


# --------------------------------------------------------------------------
# 串行化：所有会改状态的操作排队执行，避免互相踩
# --------------------------------------------------------------------------
class Runtime:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.busy: str | None = None
        self.last_error: str | None = None

    def run(self, name: str, func) -> None:
        with self.lock:
            if self.busy:
                raise RuntimeError(f"正在执行 {self.busy}，请等它结束")
            self.busy = name
        BUS.publish("action", {"action": name, "phase": "start"})

        def worker() -> None:
            try:
                func()
                self.last_error = None
                BUS.publish("action", {"action": name, "phase": "done"})
            except (Exception, SystemExit) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logging.getLogger("yi.app").error("%s 失败: %s", name, exc)
                BUS.publish("action", {"action": name, "phase": "error", "error": str(exc)})
            finally:
                status.forget_cloud_cache()
                with self.lock:
                    self.busy = None

        threading.Thread(target=worker, name="yi-" + name, daemon=True).start()


RUNTIME = Runtime()

# 竞价守护（可开关）：机器被回收后自动重建
WATCH: dict[str, Any] = {"enabled": False, "interval": 60.0, "thread": None}
TRAFFIC: dict[str, Any] = {"available": False}


def _traffic_loop() -> None:
    """每 2 秒采样一次内核的累计流量，算出实时速率。"""
    last = None
    last_ts = time.time()
    while True:
        time.sleep(2.0)
        now = time.time()
        try:
            sample = proxy.traffic()
        except Exception:
            continue
        if not sample.get("available"):
            TRAFFIC.clear()
            TRAFFIC["available"] = False
            last = None
            continue
        elapsed = max(0.5, now - last_ts)
        if last:
            sample["download_rate"] = max(
                0, int((sample["download_total"] - last["download_total"]) / elapsed)
            )
            sample["upload_rate"] = max(0, int((sample["upload_total"] - last["upload_total"]) / elapsed))
        else:
            sample["download_rate"] = 0
            sample["upload_rate"] = 0
        last, last_ts = sample, now
        TRAFFIC.clear()
        TRAFFIC.update(sample)


def subscription_token() -> str:
    """订阅地址里的随机 token（等于凭证，别外传）。"""
    config = state.load_config()
    token = config.get("sub_token")
    if not token:
        token = os.urandom(12).hex()
        config["sub_token"] = token
        state.save_config(config)
    return str(token)


def lan_ip() -> str:
    """本机在局域网里的地址——手机要靠它访问这个订阅地址。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("223.5.5.5", 80))
            return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def subscription_payload() -> str:
    """给手机用的订阅内容：base64 的节点列表（就是 vless 链接）。"""
    from yi import configgen

    current = state.load_state() or {}
    info = current.get("server_info")
    if not info:
        return ""
    link = configgen.build_link(info, current.get("label") or "HK-Spot")
    return configgen.subscription_blob([link])


def _watch_loop() -> None:
    while WATCH["enabled"]:
        if not RUNTIME.busy:
            try:
                cmd_watch(argparse.Namespace(once=True, interval=WATCH["interval"]))
            except Exception as exc:
                BUS.publish("action", {"action": "watch", "phase": "error", "error": str(exc)})
        slept = 0.0
        while WATCH["enabled"] and slept < WATCH["interval"]:
            time.sleep(1.0)
            slept += 1.0


def set_watch(enabled: bool, interval: float | None = None) -> None:
    # 期望值写进配置：重启 agent 后还要记得用户的选择
    config = state.load_config()
    config["watch_enabled"] = bool(enabled)
    state.save_config(config)
    if interval:
        WATCH["interval"] = max(15.0, float(interval))
    if enabled and not (WATCH["thread"] and WATCH["thread"].is_alive()):
        WATCH["enabled"] = True
        WATCH["thread"] = threading.Thread(target=_watch_loop, name="yi-watch", daemon=True)
        WATCH["thread"].start()
        BUS.publish(
            "action", {"action": "watch", "phase": "done", "message": "自动重建已开启：机器被回收会自动重建"}
        )
    elif not enabled:
        WATCH["enabled"] = False
        BUS.publish("action", {"action": "watch", "phase": "done", "message": "自动重建已关闭"})


def _reconcile_loop() -> None:
    """唯一的后台循环：把实际状态收敛到期望状态。"""
    log = logging.getLogger("yi.app")
    messages = {
        "started": "内核没在跑，已自动启动",
        "restarted": "服务端已更新（机器被重建过），代理已自动切换",
        "stopped": "已按期望断开代理",
        "proxy-on": "系统代理被关了，已自动恢复",
        "proxy-off": "系统代理已还原",
    }
    while True:
        time.sleep(RECONCILE_INTERVAL)
        if RUNTIME.busy:
            continue
        try:
            result = proxy.reconcile()
            actions = result.get("actions") or []
            if not actions:
                continue
            log.info("调和：%s", "、".join(actions))
            BUS.publish(
                "action",
                {
                    "action": "reconcile",
                    "phase": "done",
                    "message": "；".join(messages.get(item, item) for item in actions),
                },
            )
            if "restarted" in actions:
                check = proxy.verify()
                BUS.publish(
                    "action",
                    {
                        "action": "verify",
                        "phase": "done" if check["ok"] else "error",
                        "message": "出口 {}".format(check["actual_exit"] or "不通"),
                    },
                )
        except Exception as exc:
            log.warning("调和失败: %s", exc)


def start_background() -> None:
    threading.Thread(target=_reconcile_loop, name="yi-reconcile", daemon=True).start()
    threading.Thread(target=_traffic_loop, name="yi-traffic", daemon=True).start()
    threading.Thread(target=_state_watch_loop, name="yi-statewatch", daemon=True).start()
    # 自动重建默认开：这是这个方案值钱的地方，不该让用户自己去开
    if state.load_config().get("watch_enabled", True):
        set_watch(True)
    BUS.publish("agent", {"message": "后台调和已启动"})


def _state_signature() -> tuple:
    """便宜的状态指纹：只读缓存过的东西，不上阿里云。

    用来判断"有没有变化值得推给界面"。界面因此不用再每 3 秒盲轮询——
    状态一变，通常 1 秒内就出现在屏幕上。
    """
    current = state.load_state() or {}
    info = current.get("server_info") or {}
    return (
        current.get("instance_id"),
        bool(current.get("ready")),
        info.get("address"),
        info.get("sni"),
        proxy.running_pid(),
        proxy.system_proxy_enabled(),
        RUNTIME.busy,
        RUNTIME.last_error,
        WATCH["enabled"],
        TRAFFIC.get("connections"),
        TRAFFIC.get("download_total"),
    )


def _state_watch_loop() -> None:
    last = None
    while True:
        time.sleep(1.0)
        try:
            signature = _state_signature()
        except Exception:
            continue
        if signature != last:
            last = signature
            BUS.publish("state", {"changed": True})


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
def build_state() -> dict[str, Any]:
    snap = status.snapshot(
        busy=RUNTIME.busy,
        daemons={
            "watch": bool(WATCH["enabled"]),
            "reconcile": True,
            "reconnect": proxy.desired_connected(),
        },
    )
    snap["version"] = __version__
    snap["last_error"] = RUNTIME.last_error
    snap["events"] = BUS.history()[-80:]
    snap["traffic"] = dict(TRAFFIC)
    snap["subscription"] = {
        "token": subscription_token(),
        "lan_ip": lan_ip(),
        "available": bool((state.load_state() or {}).get("server_info")),
    }
    return snap


def _up_args(force: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        dry_run=False,
        force_recreate=force,
        instance_type=None,
        zone=None,
        price_limit=None,
        ssh_from=None,
        no_rollback=False,
        timeout=600.0,
        no_qr=True,
        spot_strategy=None,
        new_identity=False,
    )


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "yi-agent/" + __version__
    # HTTP/1.1 才有 keep-alive：一次页面加载要取 HTML、图标、状态、事件流四个东西，
    # 走 HTTP/1.0 就是四次 TCP 建连（本地也要几百微秒 × 4，还得排队）。
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[access] %s\n" % (fmt % args))

    # -- 统一的响应出口：gzip + 正确的 Content-Length（HTTP/1.1 必需） -------
    def _send(
        self,
        body: bytes,
        content_type: str,
        status_code: int = 200,
        etag: str | None = None,
        cache_control: str | None = None,
        head_only: bool = False,
    ) -> None:
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        encoding = None
        wants_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        if (
            wants_gzip
            and len(body) > 512
            and (content_type.startswith("text/") or "json" in content_type or "javascript" in content_type)
        ):
            body = gzip.compress(body, 6)
            encoding = "gzip"

        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        if encoding:
            self.send_header("Content-Encoding", encoding)
        if etag:
            self.send_header("ETag", etag)
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json(self, payload: Any, status_code: int = 200, head_only: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        # 状态是实时数据，别缓存；但压缩能省下大半带宽
        self._send(
            body,
            "application/json; charset=utf-8",
            status_code,
            cache_control="no-store",
            head_only=head_only,
        )

    def _serve_index(self) -> None:
        """首页：把当前状态**内联**进 HTML。

        这样浏览器渲染出来就是真实数据，不用先画骨架屏再等一次 fetch——
        省掉一次往返，首屏不再"跳一下"。
        """
        path = os.path.join(UI_DIR, "index.html")
        try:
            with open(path, encoding="utf-8") as handle:
                html = handle.read()
            initial = json.dumps(build_state(), ensure_ascii=False, default=str)
            marker = '<script id="boot" type="application/json">null</script>'
            html = html.replace(marker, f'<script id="boot" type="application/json">{initial}</script>')
            body = html.encode("utf-8")
        except OSError:
            self._json({"error": "ui missing"}, 500)
            return
        self._send(
            body, "text/html; charset=utf-8", cache_control="no-store", head_only=(self.command == "HEAD")
        )

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except ValueError:
            return {}

    def _serve_file(self, relative: str) -> None:
        path = os.path.join(UI_DIR, relative)
        if not os.path.isfile(path):
            self._json({"error": "not found"}, 404)
            return
        with open(path, "rb") as handle:
            body = handle.read()
        mime = "text/html; charset=utf-8"
        if path.endswith(".js"):
            mime = "application/javascript; charset=utf-8"
        elif path.endswith(".css"):
            mime = "text/css; charset=utf-8"
        stat = os.stat(path)
        etag = f'W/"{int(stat.st_mtime)}-{len(body)}"'
        self._send(body, mime, etag=etag, cache_control="no-cache", head_only=(self.command == "HEAD"))

    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?")[0]
        if route in ("/", "/index.html"):
            self._serve_index()
        elif route.startswith("/sub/"):
            # 手机订阅入口：http://<本机局域网IP>:8765/sub/<token>
            # 不需要域名、不需要证书、零成本；机器换 IP 时这里的内容自动跟着变
            if route[5:] != subscription_token():
                self._json({"error": "not found"}, 404)
                return
            payload = subscription_payload()
            if not payload:
                self._json({"error": "还没有可用的服务端"}, 503)
                return
            body = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif route == "/api/state":
            self._json(build_state())
        elif route == "/api/rules":
            self._json(rules_snapshot())
        elif route == "/icon.png":
            # 界面标题栏用同一枚图标（和 App 图标保持一套视觉）
            for name in ("icon-256.png", "icon-512.png", "icon-1024.png"):
                path = os.path.join(HERE, "assets", name)
                if os.path.isfile(path):
                    with open(path, "rb") as handle:
                        body = handle.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Cache-Control", "max-age=86400")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            self._json({"error": "no icon"}, 404)
        elif route == "/api/events":
            self._stream_events()
        elif route == "/api/sub-qr.png":
            self._serve_sub_qr()
        elif route.startswith("/ui/"):
            self._serve_file(route[4:])
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?")[0]
        payload = self._body()
        try:
            handler = {
                "/api/provision": self._provision,
                "/api/destroy": self._destroy,
                "/api/selftest": self._selftest,
                "/api/watch": self._watch,
                "/api/config": self._config,
                "/api/connect": self._connect,
                "/api/disconnect": self._disconnect,
                "/api/verify": self._verify,
                "/api/rules/custom": self._rules_add,
                "/api/rules/custom/delete": self._rules_delete,
                "/api/rules/community/update": self._rules_update,
            }.get(route)
            if handler is None:
                self._json({"error": "not found"}, 404)
                return
            handler(payload)
        except RuntimeError as exc:
            self._json({"ok": False, "error": str(exc)}, 409)

    def _provision(self, payload: dict[str, Any]) -> None:
        RUNTIME.run("provision", lambda: cmd_up(_up_args(bool(payload.get("force")))))
        self._json({"ok": True})

    def _destroy(self, payload: dict[str, Any]) -> None:
        proxy.set_desired_connected(False)
        RUNTIME.run(
            "destroy",
            lambda: cmd_down(argparse.Namespace(yes=True, orphans=False, keep_security_group=False)),
        )
        self._json({"ok": True})

    def _selftest(self, payload: dict[str, Any]) -> None:
        RUNTIME.run("selftest", lambda: cmd_selftest(argparse.Namespace()))
        self._json({"ok": True})

    def _watch(self, payload: dict[str, Any]) -> None:
        set_watch(bool(payload.get("enabled", True)), payload.get("interval"))
        self._json({"ok": True, "watch": WATCH["enabled"]})

    def _config(self, payload: dict[str, Any]) -> None:
        self._json({"ok": True, "config": _update_config(payload)})

    def _connect(self, payload: dict[str, Any]) -> None:
        use_system_proxy = not bool(payload.get("no_system_proxy"))
        arguments = argparse.Namespace(
            port=int(payload.get("port") or proxy.resolve_port()),
            no_system_proxy=not use_system_proxy,
            json=False,
        )

        def action() -> None:
            proxy.set_desired_connected(True)
            if cmd_connect(arguments):
                BUS.publish(
                    "action",
                    {
                        "action": "connect",
                        "phase": "warn",
                        "message": "内核已启动，出口校验未通过；调和循环会自动重试",
                    },
                )

        RUNTIME.run("connect", action)
        self._json({"ok": True})

    def _disconnect(self, payload: dict[str, Any]) -> None:
        proxy.set_desired_connected(False)
        RUNTIME.run(
            "disconnect", lambda: cmd_disconnect(argparse.Namespace(port=proxy.resolve_port(), json=False))
        )
        self._json({"ok": True})

    def _verify(self, payload: dict[str, Any]) -> None:
        self._json(proxy.verify(int(payload.get("port") or proxy.resolve_port())))

    # ── 分流规则 ────────────────────────────────────────────────────────
    # 用户只能增删**自己的**规则；社区规则集是只读的（上游维护，混进去下次
    # 更新就被覆盖，只会变成"我明明加了却不生效"）。
    def _rules_add(self, payload: dict[str, Any]) -> None:
        items, rule = rules.add_custom_rule(str(payload.get("rule") or ""))
        self._json({"ok": True, "rule": rule, "custom": items, "applied": apply_rules_change()})

    def _rules_delete(self, payload: dict[str, Any]) -> None:
        items, rule = rules.remove_custom_rule(str(payload.get("rule") or ""))
        self._json({"ok": True, "rule": rule, "custom": items, "applied": apply_rules_change()})

    def _rules_update(self, payload: dict[str, Any]) -> None:
        """一键更新社区规则集。耗时下载扔到后台，界面靠事件流看进度。"""
        if RUNTIME.busy:
            raise RuntimeError("正在忙别的，等它做完再更新规则集")
        args = argparse.Namespace(
            update=True,
            force=bool(payload.get("force")),
            mirror=None,
            proxy=None,
            no_proxy=False,
            no_reload=False,
            json=False,
        )
        RUNTIME.run("rules", lambda: cmd_rules(args))
        self._json({"ok": True})

    def _serve_sub_qr(self) -> None:
        """手机订阅二维码。装了 qrcode 才提供，没有就让界面显示纯文本地址。"""
        try:
            import io

            import qrcode

            port = self.server.server_address[1]
            url = f"http://{lan_ip()}:{port}/sub/{subscription_token()}"
            buffer = io.BytesIO()
            try:
                # 纯 Python 的 PNG 后端（只需要 pypng），不依赖 Pillow
                from qrcode.image.pure import PyPNGImage

                qrcode.make(url, image_factory=PyPNGImage).save(buffer)
            except Exception:
                qrcode.make(url).save(buffer, format="PNG")
            body = buffer.getvalue()
        except Exception as exc:
            self._json({"error": f"二维码不可用: {exc}"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream_events(self) -> None:
        subscriber = BUS.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                try:
                    event = subscriber.get(timeout=15)
                    chunk = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    chunk = ": keepalive\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(subscriber)


# HEAD 复用 GET 的逻辑（只是不写 body）。必须写在类定义**之后**——
# 类体里引用 do_GET 会因为"那时还没定义"直接 NameError（刚踩）。
Handler.do_HEAD = Handler.do_GET


def _update_config(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "instance_types",
        "zones",
        "spot_bid_multiplier",
        "reality_dests",
        "xray_port",
        "xray_version",
        "domain",
        "subdomain",
    }
    config = state.load_config()
    for key, value in (payload or {}).items():
        if key in allowed:
            config[key] = value
    if isinstance(payload.get("budget"), dict):
        budget = dict(config.get("budget") or {})
        budget.update(payload["budget"])
        config["budget"] = budget
    state.save_config(config)
    return {key: config.get(key) for key in sorted(allowed)} | {"budget": config.get("budget")}


def rules_snapshot() -> dict[str, Any]:
    """界面「规则」页要的全部东西：自己的规则 + 社区规则集的状态。"""
    config = state.load_config()
    targets = dict(rules.RULE_TARGETS)
    return {
        "enabled": bool(config.get("ruleset_enabled", True)),
        "dir": rules.ruleset_dir(),
        "max_age_hours": rules.stale_hours(config),
        "interval_hours": int(config.get("ruleset_interval_hours") or 24),
        "needs_update": rules.needs_update(config),
        # 界面上的下拉选项由后端给，免得两边各写一份、改一边忘一边
        "types": [{"name": name, "hint": hint} for name, hint in rules.CUSTOM_TYPES],
        "actions": [{"name": name, "hint": hint} for name, hint in rules.CUSTOM_ACTIONS],
        "custom": rules.custom_rules(config),
        "community": [{**item, "target": targets.get(item["name"], "")} for item in rules.status(config)],
    }


def apply_rules_change() -> str:
    """规则改了就让内核立刻用上，返回一句人话。

    **重新生成配置时必须用 resolve_port()**，也就是这台机器正在用的端口 ——
    这里踩过一次：用了默认端口 7897 而实际在 7899，重载后内核换了端口监听，
    系统代理还指着旧端口，用户的网直接断了。
    """
    current = state.load_state() or {}
    info = current.get("server_info")
    if not info:
        return "还没有服务端信息，等机器就绪后会自动带上"
    try:
        proxy.write_config(info, label=current.get("label") or "HK-Spot")
    except Exception as exc:  # noqa: BLE001 - 生成失败不该让"规则已保存"变成报错
        return f"规则已保存，但内核配置生成失败：{exc}"
    if not proxy.running_pid():
        return "已保存，下次连接生效"
    try:
        proxy.reload_config()
    except Exception as exc:  # noqa: BLE001
        return f"已保存，但内核重载失败：{exc}"
    return "已生效"


def _pick_port(preferred: int) -> int:
    """agent 端口：优先用约定的那个；被别的程序占了就换一个。"""
    for port in range(preferred, preferred + 20):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _QuietServer(ThreadingHTTPServer):
    """浏览器刷新时经常把连接掐掉，默认会把整段堆栈打进日志——噪音太大。"""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yi-agent", description="yi.app 的本地后端")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--print-port", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("yi").addHandler(BusHandler())
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(line_buffering=True)

    port = _pick_port(args.port)
    state.ensure_home()
    state.write_private(os.path.join(state.home_dir(), "agent.port"), str(port), 0o600)
    if port != args.port:
        logging.getLogger("yi.app").warning("端口 %d 被占用，agent 改用 %d", args.port, port)

    httpd = _QuietServer((args.host, port), Handler)
    if args.print_port:
        print(port, flush=True)
    logging.getLogger("yi.app").info("agent 已启动: http://127.0.0.1:%d/", port)
    start_background()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
