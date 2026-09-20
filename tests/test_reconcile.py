"""调和循环的契约：把实际状态收敛到期望状态。

这是连接可靠性的核心——内核崩了、服务端换了、系统代理被手动关了，
都应该在下一轮自己纠回来，而不是等用户发现"网又断了"。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sandbox  # noqa: E402,F401

from yi import proxy, state, status  # noqa: E402

INFO = {
    "address": "47.1.1.1",
    "port": 443,
    "uuid": "u-1",
    "public_key": "pk-1",
    "short_id": "sid",
    "sni": "www.cloudflare.com",
}
NEW_INFO = dict(INFO, address="47.2.2.2")


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        state.ensure_home()
        state.save_state({"instance_id": "i-1", "public_ip": "47.1.1.1", "server_info": INFO, "ready": True})

    def _reconcile(self, want, running, applied=None, sysproxy=False):
        proxy.set_desired_connected(want)
        if applied:
            proxy._set_applied_fingerprint(applied)
        # 规则集的后台刷新要下几 MB，不该出现在"调和循环"的单元测试里
        # （它自己的触发条件由 tests/test_rules.py 覆盖）。
        with (
            mock.patch.object(proxy, "running_pid", return_value=1 if running else None),
            mock.patch.object(proxy, "start") as start,
            mock.patch.object(proxy, "stop") as stop,
            mock.patch.object(proxy, "system_proxy_enabled", return_value=sysproxy),
            mock.patch.object(proxy, "set_system_proxy") as setter,
            mock.patch.object(proxy, "resolve_port", return_value=7897),
            mock.patch.object(proxy, "_port_open", return_value=True),
            mock.patch.object(proxy, "_refresh_rules_if_stale", return_value=False),
        ):
            result = proxy.reconcile()
        return result, start, stop, setter

    def test_wants_connected_but_kernel_dead_starts_it(self):
        result, start, stop, _ = self._reconcile(want=True, running=False)
        self.assertIn("started", result["actions"])
        start.assert_called_once()

    def test_wants_connected_but_points_at_old_server_restarts(self):
        """竞价重建后服务端地址会变，内核必须换配置重启。"""
        state.save_state(
            {"instance_id": "i-2", "public_ip": "47.2.2.2", "server_info": NEW_INFO, "ready": True}
        )
        result, start, _, _ = self._reconcile(want=True, running=True, applied=proxy._fingerprint(INFO))
        self.assertIn("restarted", result["actions"])
        start.assert_called_once()

    def test_already_correct_does_nothing(self):
        result, start, stop, _ = self._reconcile(
            want=True, running=True, applied=proxy._fingerprint(INFO), sysproxy=True
        )
        self.assertEqual(result["actions"], [])
        start.assert_not_called()

    def test_system_proxy_turned_off_by_someone_else_is_restored(self):
        result, _, _, setter = self._reconcile(
            want=True, running=True, applied=proxy._fingerprint(INFO), sysproxy=False
        )
        self.assertIn("proxy-on", result["actions"])
        setter.assert_called_once()

    def test_wants_disconnected_stops_and_restores(self):
        result, _, stop, setter = self._reconcile(want=False, running=True, sysproxy=True)
        self.assertIn("stopped", result["actions"])
        self.assertIn("proxy-off", result["actions"])
        stop.assert_called_once()
        setter.assert_called_once()

    def test_no_server_waits_instead_of_crashing(self):
        state.save_state({"instance_id": "i-3"})  # 没有 server_info
        result, start, _, _ = self._reconcile(want=True, running=False)
        self.assertEqual(result["actions"], ["waiting-for-server"])
        start.assert_not_called()

    def test_upgrade_does_not_kill_a_running_connection(self):
        """老配置没有 want_connected 字段时，以"内核在跑"为准（否则升级即断网）。"""
        config = state.load_config()
        config.pop("want_connected", None)
        state.save_config(config)
        with mock.patch.object(proxy, "running_pid", return_value=1):
            self.assertTrue(proxy.desired_connected())
        with mock.patch.object(proxy, "running_pid", return_value=None):
            self.assertFalse(proxy.desired_connected())


class PortTests(unittest.TestCase):
    def setUp(self):
        state.ensure_home()

    def test_picks_a_free_port_when_preferred_is_taken(self):
        with mock.patch.object(proxy, "_port_open", side_effect=lambda p, *a, **k: p == 7897):
            self.assertEqual(proxy.find_free_port(7897), 7898)

    def test_keeps_preferred_port_when_free(self):
        with mock.patch.object(proxy, "_port_open", return_value=False):
            self.assertEqual(proxy.find_free_port(7897), 7897)

    def test_running_kernel_port_is_reused_not_re_probed(self):
        """**这是让整台机器断网的那个 bug 的回归测试。**

        内核起来后它自己就占着那个端口。如果 resolve_port() 再去"探测有没有被占用"，
        就会把内核自己的端口判成冲突、换一个新端口，然后系统代理指向没人监听的端口。
        """
        proxy._set_applied("fp-1", 7898)
        with (
            mock.patch.object(proxy, "running_pid", return_value=1234),
            mock.patch.object(proxy, "_port_open", return_value=True),
        ):
            # 端口"被占用"也必须是它自己那个
            self.assertEqual(proxy.resolve_port(), 7898)

    def test_refuses_to_point_system_proxy_at_a_dead_port(self):
        with (
            mock.patch.object(proxy, "_port_open", return_value=False),
            mock.patch.object(proxy, "network_service", return_value="Wi-Fi"),
            mock.patch.object(proxy, "_admin_shell") as admin,
        ):
            with self.assertRaises(proxy.ProxyError) as ctx:
                proxy.set_system_proxy(True, 7899)
            self.assertIn("没有程序在监听", str(ctx.exception))
            admin.assert_not_called()

    def test_reconcile_disables_system_proxy_when_kernel_cannot_start(self):
        state.save_state({"instance_id": "i-1", "ready": True, "server_info": INFO})
        proxy.set_desired_connected(True)
        with (
            mock.patch.object(proxy, "running_pid", return_value=None),
            mock.patch.object(proxy, "start", side_effect=proxy.ProxyError("起不来")),
            mock.patch.object(proxy, "system_proxy_enabled", return_value=True),
            mock.patch.object(proxy, "set_system_proxy") as setter,
            mock.patch.object(proxy, "resolve_port", return_value=7897),
        ):
            result = proxy.reconcile()
        self.assertIn("start-failed", result["actions"])
        self.assertIn("proxy-off", result["actions"])
        setter.assert_called_once()


class StatusTests(unittest.TestCase):
    def setUp(self):
        state.ensure_home()
        # 每个用例从干净状态开始：状态文件在同一个 HOME 里会串味
        if os.path.exists(state.state_path()):
            os.remove(state.state_path())
        config = state.load_config()
        config.pop("want_connected", None)
        state.save_config(config)

    def _snapshot(self, **kwargs):
        with (
            mock.patch.object(status, "machine_exists_on_cloud", return_value=kwargs.pop("cloud", True)),
            mock.patch.object(
                proxy,
                "status",
                return_value=kwargs.pop(
                    "proxy_status",
                    {
                        "running": False,
                        "pid": None,
                        "port": 7897,
                        "listening": False,
                        "system_proxy": False,
                        "kernel": "mihomo",
                        "config": "x",
                    },
                ),
            ),
            mock.patch.object(proxy, "desired_connected", return_value=False),
        ):
            return status.snapshot(**kwargs)

    def test_no_machine(self):
        self.assertEqual(self._snapshot()["state"], "no_machine")

    def test_installing_then_failed_after_timeout(self):
        state.save_state({"instance_id": "i-1", "created_at": "2026-09-21T00:00:00Z"})
        snap = self._snapshot()
        self.assertIn(snap["state"], ("installing", "install_failed"))
        # 用很久以前的时间戳构造"装太久"
        stale = status.snapshot.__globals__["INSTALL_TIMEOUT_SECONDS"]
        self.assertGreater(stale, 0)

    def test_ready_when_server_info_present(self):
        state.save_state(
            {"instance_id": "i-1", "ready": True, "server_info": INFO, "created_at": "2026-09-21T00:00:00Z"}
        )
        self.assertEqual(self._snapshot()["state"], "ready")

    def test_reclaimed_beats_connected(self):
        state.save_state({"instance_id": "i-1", "ready": True, "server_info": INFO})
        snap = self._snapshot(
            cloud=False,
            proxy_status={
                "running": True,
                "pid": 1,
                "port": 7897,
                "listening": True,
                "system_proxy": True,
                "kernel": "mihomo",
                "config": "x",
            },
        )
        self.assertEqual(snap["state"], "reclaimed")

    def test_busy_wins_over_everything(self):
        state.save_state({"instance_id": "i-1", "ready": True, "server_info": INFO})
        snap = self._snapshot(busy="provision")
        self.assertEqual(snap["state"], "busy")


if __name__ == "__main__":
    unittest.main()
