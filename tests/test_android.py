"""从 GitHub release 里挑 Android APK 的选择逻辑。

选错的代价是"用户手机上装了个跑不起来的包"，而且他多半会以为是代理的问题。
所以这段是纯函数，不碰网络，在这里钉死。
"""

import json
import os
import sys
import unittest
from unittest import mock
from urllib.error import URLError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sandbox  # noqa: E402,F401  (隔离 YI_HOME，必须在导入 yi 之前)

from yi import android  # noqa: E402


def release(*names):
    return {
        "tag_name": "2.2.6",
        "assets": [{"name": n, "browser_download_url": f"https://example.invalid/{n}"} for n in names],
    }


# 照抄 v2rayNG 2.2.6 实际发布的资产名
REAL_ASSETS = [
    "v2rayN-public-key.asc",
    "v2rayNG_2.2.6-fdroid_arm64-v8a.apk",
    "v2rayNG_2.2.6-fdroid_arm64-v8a.apk.sig",
    "v2rayNG_2.2.6-fdroid_armeabi-v7a.apk",
    "v2rayNG_2.2.6-fdroid_x86.apk",
    "v2rayNG_2.2.6-fdroid_x86_64.apk",
    "v2rayNG_2.2.6_arm64-v8a.apk",
    "v2rayNG_2.2.6_arm64-v8a.apk.sig",
    "v2rayNG_2.2.6_armeabi-v7a.apk",
    "v2rayNG_2.2.6_x86.apk",
    "v2rayNG_2.2.6_x86_64.apk",
]


class PickTests(unittest.TestCase):
    def test_default_is_arm64_playstore(self):
        self.assertEqual(
            android.pick_apk_asset(release(*REAL_ASSETS)),
            "v2rayNG_2.2.6_arm64-v8a.apk",
        )

    def test_fdroid_flavor_is_a_different_file(self):
        self.assertEqual(
            android.pick_apk_asset(release(*REAL_ASSETS), flavor="fdroid"),
            "v2rayNG_2.2.6-fdroid_arm64-v8a.apk",
        )

    def test_every_supported_abi_is_reachable(self):
        for abi in android.SUPPORTED_ABIS:
            picked = android.pick_apk_asset(release(*REAL_ASSETS), abi=abi)
            self.assertIsNotNone(picked, abi)
            self.assertTrue(picked.endswith(f"_{abi}.apk"), picked)

    def test_never_picks_the_signature_file(self):
        """`.apk.sig` 也以 .apk 开头。挑错的话用户下到的是一个 171 字节的签名。"""
        for abi in android.SUPPORTED_ABIS:
            self.assertFalse(android.pick_apk_asset(release(*REAL_ASSETS), abi=abi).endswith(".sig"))

    def test_falls_back_to_the_other_flavor(self):
        """上游偶尔只发一个渠道；两个渠道功能一样，装哪个都能用。"""
        only_fdroid = [n for n in REAL_ASSETS if "-fdroid" in n]
        self.assertEqual(
            android.pick_apk_asset(release(*only_fdroid), flavor="playstore"),
            "v2rayNG_2.2.6-fdroid_arm64-v8a.apk",
        )

    def test_returns_none_instead_of_guessing(self):
        self.assertIsNone(android.pick_apk_asset(release("v2rayN-public-key.asc")))
        self.assertIsNone(android.pick_apk_asset(release()))

    def test_rejects_unknown_abi(self):
        with self.assertRaises(android.AndroidError):
            android.pick_apk_asset(release(*REAL_ASSETS), abi="mips")


class SubscriptionUrlTests(unittest.TestCase):
    """订阅地址只在 agent 活着的时候有效。

    这几个用例必须 mock 掉网络：直接连本机 8765 的话，测试结果会取决于
    "开发者此刻有没有开着 App" —— 那种测试今天绿明天红，比没有还糟。
    """

    @staticmethod
    def _response(payload: dict) -> mock.MagicMock:
        response = mock.MagicMock()
        response.read.return_value = json.dumps(payload).encode("utf-8")
        response.__enter__ = lambda s: s
        response.__exit__ = lambda s, *args: False
        return response

    def test_no_agent_means_no_url(self):
        """连不上就返回 None —— 不能瞎给一个打不开的地址让用户去排查。"""
        with mock.patch("urllib.request.urlopen", side_effect=URLError("connection refused")):
            self.assertIsNone(android.running_subscription_url())

    def test_url_is_built_from_the_agents_own_numbers(self):
        response = self._response(
            {"subscription": {"available": True, "token": "t0k", "lan_ip": "192.0.2.7"}}
        )
        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(android.running_subscription_url(), "http://192.0.2.7:8765/sub/t0k")

    def test_not_provisioned_means_no_url(self):
        """机器还没买的时候没有订阅可给。"""
        response = self._response({"subscription": {"available": False, "token": "t0k"}})
        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertIsNone(android.running_subscription_url())

    def test_garbage_response_is_not_fatal(self):
        response = self._response({"unexpected": "shape"})
        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertIsNone(android.running_subscription_url())


if __name__ == "__main__":
    unittest.main()
