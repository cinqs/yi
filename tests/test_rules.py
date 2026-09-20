"""社区规则集的取用逻辑。

这里的两个约定值钱，错了会在真机上以奇怪的方式表现出来：

1. **缓存目录必须是 `mihomo/ruleset/`**。生成的配置里写的是
   `path: ./ruleset/x.yaml`，内核按自己的工作目录（`mihomo -d`）解析。
   放到别处就是"文件明明在，内核却说没有"。
2. **空文件要当成失败**。镜像返回一个空页时，下载是"成功"的，内核也能启动，
   但那几条 RULE-SET 全都匹配不上 —— 表现是"规则配了完全没生效"。宁可不覆盖。

下载用本地 `file://` 假镜像测，不碰网络。
"""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sandbox  # noqa: E402,F401  (隔离 YI_HOME，必须在导入 yi 之前)

from yi import proxy, rules  # noqa: E402


def make_mirror(root: str, contents: dict[str, str]) -> str:
    os.makedirs(root, exist_ok=True)
    for name, text in contents.items():
        with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    return "file://" + root


PAYLOAD = "payload:\n  - 'example.com'\n  - 'example.org'\n"


class IsolatedHome(unittest.TestCase):
    """每个用例一个干净的 YI_HOME。

    否则前一个用例下下来的规则集会让后一个用例里的 `needs_update()` 变成 False,
    两个用例的结果互相决定 —— 那是最难查的一类测试失败。
    """

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._saved_home = os.environ.get("YI_HOME")
        os.environ["YI_HOME"] = self._home.name

    def tearDown(self):
        if self._saved_home is None:
            os.environ.pop("YI_HOME", None)
        else:
            os.environ["YI_HOME"] = self._saved_home
        self._home.cleanup()


class LayoutTests(IsolatedHome):
    def test_cache_lives_under_the_kernels_working_directory(self):
        """配置里是相对路径，所以缓存位置不是"随便挑一个"—— 是契约。"""
        self.assertEqual(rules.ruleset_dir(), os.path.join(proxy.proxy_dir(), "ruleset"))
        self.assertTrue(rules.local_path("direct").endswith("mihomo/ruleset/direct.yaml"))


class CountTests(IsolatedHome):
    def test_counts_payload_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(PAYLOAD)
            self.assertEqual(rules.count_rules(path), 2)

    def test_missing_file_counts_as_zero(self):
        self.assertEqual(rules.count_rules("/nonexistent/nope.yaml"), 0)


class StatusTests(IsolatedHome):
    def test_nothing_downloaded_means_update_is_needed(self):
        self.assertTrue(rules.needs_update())
        self.assertTrue(all(not item["present"] for item in rules.status()))


class FetchTests(IsolatedHome):
    def test_downloads_every_set_from_the_first_working_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = make_mirror(
                os.path.join(tmp, "mirror"),
                {rs.filename: PAYLOAD for rs in rules.RULE_SETS},
            )
            summary = rules.fetch_all(
                {"ruleset_mirrors": [mirror], "ruleset_max_age_hours": 1},
                autodetect=False,
                progress=lambda _m: None,
            )
            self.assertEqual(len(summary["fetched"]), len(rules.RULE_SETS))
            self.assertEqual(summary["failed"], [])
            for rs in rules.RULE_SETS:
                self.assertTrue(os.path.exists(rules.local_path(rs.name)), rs.name)
            self.assertFalse(rules.needs_update({"ruleset_max_age_hours": 24}))

    def test_falls_through_to_the_next_mirror(self):
        """一个镜像挂掉不该让整批失败 —— 换下一个继续。"""
        with tempfile.TemporaryDirectory() as tmp:
            good = make_mirror(
                os.path.join(tmp, "good"),
                {rs.filename: PAYLOAD for rs in rules.RULE_SETS},
            )
            dead = "file://" + os.path.join(tmp, "does-not-exist")
            summary = rules.fetch_all(
                {"ruleset_mirrors": [dead, good], "ruleset_max_age_hours": 1},
                autodetect=False,
                progress=lambda _m: None,
            )
            self.assertEqual(summary["failed"], [])
            self.assertEqual(len(summary["fetched"]), len(rules.RULE_SETS))

    def test_all_mirrors_dead_is_reported_not_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dead = "file://" + os.path.join(tmp, "nope")
            summary = rules.fetch_all(
                {"ruleset_mirrors": [dead], "ruleset_max_age_hours": 1},
                autodetect=False,
                progress=lambda _m: None,
            )
            self.assertEqual(summary["fetched"], [])
            self.assertEqual(len(summary["failed"]), len(rules.RULE_SETS))

    def test_empty_file_is_treated_as_failure(self):
        """镜像返回空内容时，"下载成功"是个谎 —— 用下去会让规则全部失效。"""
        with tempfile.TemporaryDirectory() as tmp:
            mirror = make_mirror(os.path.join(tmp, "empty"), {"direct.txt": "\n"})
            with self.assertRaises(rules.RulesError):
                rules.fetch_one(rules.RuleSet("direct", "domain", "direct.txt"), mirror)
            self.assertFalse(os.path.exists(rules.local_path("direct")))

    def test_force_redownloads_fresh_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = make_mirror(
                os.path.join(tmp, "m"),
                {"direct.txt": PAYLOAD},
            )
            config = {"ruleset_mirrors": [mirror], "ruleset_max_age_hours": 999}
            rules.fetch_all(config, autodetect=False, progress=lambda _m: None)
            # 第二次：文件还新鲜，应该跳过
            summary = rules.fetch_all(config, autodetect=False, progress=lambda _m: None)
            self.assertNotIn("direct", summary["fetched"])
            # --force 才会重下
            summary = rules.fetch_all(config, autodetect=False, force=True, progress=lambda _m: None)
            self.assertIn("direct", summary["fetched"])


class OrderingTests(IsolatedHome):
    def test_reject_is_first_and_direct_is_configured(self):
        """顺序即语义：先挡广告，最后才轮到"被墙的"。"""
        targets = dict(rules.RULE_TARGETS)
        names = [name for name, _ in rules.RULE_TARGETS]
        self.assertEqual(names[0], "reject")
        self.assertEqual(targets["reject"], "REJECT")
        self.assertEqual(targets["direct"], "DIRECT")
        self.assertEqual(targets["gfw"], "PROXY")
        self.assertEqual(sorted(targets), sorted(rs.name for rs in rules.RULE_SETS))

    def test_default_mirrors_do_not_rely_on_a_blocked_host_first(self):
        """`raw.githubusercontent.com` 国内实测直接超时，不能排在第一位。"""
        self.assertNotIn("raw.githubusercontent.com", rules.DEFAULT_MIRRORS[0])
        self.assertEqual(rules.DEFAULT_MIRRORS[-1], rules.UPSTREAM)


class AutoRefreshTriggerTests(IsolatedHome):
    """调和循环里的"过期就补一次"什么时候该触发。

    这几个用例必须把下载本身 mock 掉：真正去下就是拿网络测测试，忽快忽慢。
    """

    def test_does_nothing_when_kernel_is_not_running(self):
        """内核没跑就别去取：既没有可用代理，取回来也没人读。"""
        with mock.patch.object(proxy, "running_pid", return_value=None):
            self.assertFalse(proxy._refresh_rules_if_stale())

    def test_does_nothing_when_the_cache_is_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = make_mirror(os.path.join(tmp, "m"), {rs.filename: PAYLOAD for rs in rules.RULE_SETS})
            home = os.environ["YI_HOME"]
            with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
                fh.write(f'ruleset_mirrors = ["{mirror}"]\n')
            rules.fetch_all(autodetect=False, progress=lambda _m: None)
            with (
                mock.patch.object(proxy, "running_pid", return_value=1),
                mock.patch.object(rules, "fetch_all") as fetch,
            ):
                self.assertFalse(proxy._refresh_rules_if_stale())
                fetch.assert_not_called()

    def test_refreshes_in_the_background_when_stale(self):
        with (
            mock.patch.object(proxy, "running_pid", return_value=1),
            mock.patch.object(rules, "fetch_all", return_value={"fetched": [], "failed": []}) as fetch,
        ):
            self.assertTrue(proxy._refresh_rules_if_stale())
            for _ in range(50):  # 等后台线程跑完，别用固定 sleep
                if fetch.called:
                    break
                time.sleep(0.02)
            fetch.assert_called_once()

    def test_disabled_in_config_means_no_work(self):
        home = os.environ["YI_HOME"]
        with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write("ruleset_enabled = false\n")
        with (
            mock.patch.object(proxy, "running_pid", return_value=1),
            mock.patch.object(rules, "fetch_all") as fetch,
        ):
            self.assertFalse(proxy._refresh_rules_if_stale())
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
