"""控制面（阿里云 API + 内核生命周期）的几条安全线。

这个文件是一次真实事故的产物：更新规则集时重新生成了内核配置，用了默认端口
而不是正在用的端口，重载后内核换了端口监听 —— 系统代理还指着旧端口，网就断了。
顺着这条线查下去，又发现两个更贵的问题。三条都钉在这里。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sandbox  # noqa: E402,F401  (隔离 YI_HOME，必须在导入 yi 之前)

from yi import aliyun, cli, proxy, state  # noqa: E402

INFO = {
    "address": "47.1.1.1",
    "port": 443,
    "uuid": "11111111-2222-3333-4444-555555555555",
    "flow": "xtls-rprx-vision",
    "security": "reality",
    "sni": "www.cloudflare.com",
    "fingerprint": "chrome",
    "public_key": "SX0lGu6DocFHMW_kssSc-TACG4wpGc-UbT9mH0TKObc",
    "short_id": "deadbeefcafe0001",
}


class ControlPlaneNeverGoesThroughTheProxy(unittest.TestCase):
    """阿里云 API 调用不能走系统代理。

    走代理有三个问题，第二个是这次真实踩到的：本地代理一停，所有 API 调用变成
    `ECONNREFUSED`，而看门狗会把它当成"实例被回收"，进而重建机器。
    urllib 会读取并**缓存** macOS 的系统代理设置，所以必须显式清空。
    """

    def test_api_opener_has_no_proxies(self):
        """要保证的是"**即使系统代理开着，这个 opener 也不用它**"。

        （注意别去断言"handlers 里有一个 ProxyHandler"——`ProxyHandler({})`
        本来就不注册任何代理方法，那样断言会失败，而且断言错了东西。）
        """
        with mock.patch(
            "urllib.request.getproxies",
            return_value={"http": "http://127.0.0.1:7899", "https": "http://127.0.0.1:7899"},
        ):
            leaked = [h for h in aliyun._NO_PROXY_OPENER.handlers if getattr(h, "proxies", None)]
        self.assertEqual(leaked, [], "系统代理漏进了控制面的请求")

    def test_call_does_not_use_the_default_opener(self):
        """一旦改回 urllib.request.urlopen，代理又会被悄悄带上。"""
        client = aliyun.EcsClient(
            aliyun.Credentials(access_key_id="LTAIEXAMPLE", access_key_secret="x", source="test"),
            region="cn-hongkong",
            timeout=1,
            max_retries=0,
        )
        client._sleep = lambda *_a: None
        with (
            mock.patch.object(aliyun, "_NO_PROXY_OPENER") as opener,
            mock.patch("urllib.request.urlopen") as default_urlopen,
        ):
            opener.open.side_effect = aliyun.urllib.error.URLError("stop here")
            with self.assertRaises(aliyun.AliyunError):
                client.call("DescribeRegions")
            opener.open.assert_called_once()
            default_urlopen.assert_not_called()


class NetworkErrorsAreNotMissingInstances(unittest.TestCase):
    """**查失败 ≠ 实例没了。**

    以前网络错误也算进"查不到"的计数，两次就判定竞价回收并重建 ——
    API 抖两下，用户白花一笔钱、IP 白换一次。这个测试跑三轮失败，
    断言一次重建都没发生。
    """

    def setUp(self):
        state.ensure_home()
        state.save_state({"instance_id": "i-1", "public_ip": "47.1.1.1", "server_info": INFO, "ready": True})

    def test_three_network_failures_do_not_trigger_a_rebuild(self):
        class Boom:
            def find_in_listing(self, _instance_id):
                raise aliyun.AliyunError("NetworkError", "connection refused")

        class StopLoop(Exception):
            pass

        rounds = {"n": 0}

        def fake_sleep(_seconds):
            rounds["n"] += 1
            if rounds["n"] >= 3:
                raise StopLoop

        args = mock.Mock(once=False, interval=10.0)
        with (
            mock.patch.object(cli, "_client", return_value=Boom()),
            mock.patch.object(cli, "_recreate_safely") as recreate,
            mock.patch.object(cli, "_budget_guard"),
            mock.patch("time.sleep", side_effect=fake_sleep),
            mock.patch.object(cli.log, "warning") as warn,
        ):
            with self.assertRaises(StopLoop):
                cli.cmd_watch(args)
            recreate.assert_not_called()
            # 而且不能对外宣称"查不到实例"——那会把排查方向带偏
            messages = " ".join(str(c.args[0]) for c in warn.call_args_list)
            self.assertIn("查询状态失败", messages)
            self.assertNotIn("查不到实例", messages)


class RegeneratingConfigKeepsThePort(unittest.TestCase):
    """重新生成配置时必须用**正在用的**端口，不能用模块里的默认值。

    这是这次断网事故的直接原因：默认值 7897、实际 7899，重载后内核换到 7897，
    而系统代理还指着 7899。
    """

    def test_write_config_without_a_port_uses_the_resolved_one(self):
        state.ensure_home()
        with mock.patch.object(proxy, "resolve_port", return_value=7899) as resolve:
            path = proxy.write_config(INFO)
            resolve.assert_called_once()
        with open(path, encoding="utf-8") as fh:
            self.assertIn("mixed-port: 7899", fh.read())
        self.assertNotIn(proxy.DEFAULT_MIXED_PORT, (7899,))


class DeadListenerIsRecovered(unittest.TestCase):
    """进程活着 ≠ 在服务。

    重载配置可能让内核丢掉原端口的监听，而 `running_pid()` 依然有值 ——
    这时调和循环如果只看进程，就会对"系统代理指着死端口"视而不见。
    """

    def setUp(self):
        state.ensure_home()
        state.save_state({"instance_id": "i-1", "public_ip": "47.1.1.1", "server_info": INFO, "ready": True})

    def test_reconcile_restarts_when_the_port_is_not_listening(self):
        proxy.set_desired_connected(True)
        proxy._set_applied_fingerprint(proxy._fingerprint(INFO))
        with (
            mock.patch.object(proxy, "running_pid", return_value=1),
            mock.patch.object(proxy, "start") as start,
            mock.patch.object(proxy, "stop") as stop,
            mock.patch.object(proxy, "system_proxy_enabled", return_value=False),
            mock.patch.object(proxy, "set_system_proxy"),
            mock.patch.object(proxy, "resolve_port", return_value=7899),
            mock.patch.object(proxy, "_port_open", return_value=False),
            mock.patch.object(proxy, "_refresh_rules_if_stale", return_value=False),
        ):
            result = proxy.reconcile()
        self.assertIn("restarted-dead-listener", result["actions"])
        stop.assert_called_once()
        start.assert_called_once()

    def test_healthy_kernel_is_left_alone(self):
        proxy.set_desired_connected(True)
        proxy._set_applied_fingerprint(proxy._fingerprint(INFO))
        with (
            mock.patch.object(proxy, "running_pid", return_value=1),
            mock.patch.object(proxy, "start") as start,
            mock.patch.object(proxy, "stop"),
            mock.patch.object(proxy, "system_proxy_enabled", return_value=True),
            mock.patch.object(proxy, "set_system_proxy"),
            mock.patch.object(proxy, "resolve_port", return_value=7899),
            mock.patch.object(proxy, "_port_open", return_value=True),
            mock.patch.object(proxy, "_refresh_rules_if_stale", return_value=False),
        ):
            result = proxy.reconcile()
        self.assertEqual(result["actions"], [])
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
