"""AccessKey 的读写与掩码展示。

两条硬规矩，各有一组用例守着：

1. **密钥只进不出** —— `describe()` 返回的东西里永远不能有 secret。
2. **不验证不落盘** —— 写错的密钥要等到下次 `up` 才炸，那时已经花掉几分钟和
   一次实例创建。所以保存前必须先真调一次接口。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sandbox  # noqa: E402,F401  (隔离 YI_HOME，必须在导入 yi 之前)

from yi import aliyun, credentials  # noqa: E402

ENV_KEYS = [k for pair in credentials.ENV_VARS for k in pair]


class MaskTests(unittest.TestCase):
    def test_masks_the_middle(self):
        self.assertEqual(credentials.mask("LTAIEXAMPLEKEY0000PSF1"), "LTAI****PSF1")

    def test_short_values_are_fully_hidden(self):
        self.assertEqual(credentials.mask("abc"), "***")
        self.assertEqual(credentials.mask(""), "")
        self.assertEqual(credentials.mask(None), "")


class DescribeTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        # 环境变量优先级最高，会盖掉文件；测文件路径时必须先清掉
        patcher = mock.patch.dict(os.environ, {k: "" for k in ENV_KEYS})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ENV_KEYS:
            os.environ.pop(key, None)

    def test_nothing_configured(self):
        info = credentials.describe(home=self.home.name)
        self.assertFalse(info["configured"])
        self.assertEqual(info["access_key_id"], "")
        self.assertIn("还没有配置文件", info["source_label"])

    def test_reads_and_masks_the_file(self):
        path = credentials.save("LTAIEXAMPLEKEY0000PSF1", "s3cret", home=self.home.name)
        info = credentials.describe(home=self.home.name)
        self.assertTrue(info["configured"])
        self.assertEqual(info["access_key_id"], "LTAI****PSF1")
        self.assertIn(path, info["source_label"])

    def test_never_returns_the_secret(self):
        credentials.save("LTAIEXAMPLEKEY0000PSF1", "super-secret-value", home=self.home.name)
        blob = json.dumps(credentials.describe(home=self.home.name))
        self.assertNotIn("super-secret-value", blob)

    def test_env_wins_and_says_so(self):
        """环境变量优先 —— 不把这个说清楚，用户会以为"我改了文件怎么没用"。"""
        credentials.save("LTAIEXAMPLEKEY0000PSF1", "file-secret", home=self.home.name)
        with mock.patch.dict(
            os.environ,
            {"ALIBABA_CLOUD_ACCESS_KEY_ID": "LTAIEXAMPLEENV0009999", "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "x"},
        ):
            info = credentials.describe(home=self.home.name)
        self.assertTrue(info["env_override"])
        self.assertEqual(info["source"], "env")
        self.assertEqual(info["access_key_id"], "LTAI****9999")
        self.assertIn("优先级最高", info["source_label"])


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)

    def test_writes_0600_and_round_trips_through_the_loader(self):
        path = credentials.save("LTAIEXAMPLEKEY0000PSF1", "s3cret", home=self.home.name)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        creds = aliyun.load_credentials("default", home=self.home.name)
        self.assertEqual(creds.access_key_id, "LTAIEXAMPLEKEY0000PSF1")
        self.assertEqual(creds.access_key_secret, "s3cret")

    def test_keeps_other_profiles(self):
        """用户可能还有别的账号配在同一个文件里，只动我们这一条。"""
        path = credentials.save("LTAIEXAMPLEOTHER0011", "other", profile="other", home=self.home.name)
        credentials.save("LTAIEXAMPLEKEY0000PSF1", "s3cret", profile="yi", home=self.home.name)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        names = {p["name"] for p in data["profiles"]}
        self.assertEqual(names, {"other", "yi"})
        self.assertEqual(data["current"], "yi")

    def test_resaving_the_same_profile_replaces_it(self):
        home = self.home.name
        profile = credentials.describe(home=home)["profile"]
        credentials.save("LTAIEXAMPLEKEY0000PSF1", "first", profile=profile, home=home)
        credentials.save("LTAIEXAMPLEKEY0000PSF1", "second", profile=profile, home=home)
        with open(credentials.config_path(home), encoding="utf-8") as handle:
            profiles = json.load(handle)["profiles"]
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["access_key_secret"], "second")

    def test_rejects_empty_or_obviously_wrong_input(self):
        for bad in (("", "s"), ("LTAIEXAMPLEKEY0000PSF1", ""), ("nope", "s")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    credentials.save(*bad, home=self.home.name)


class VerifyBeforeSaveTests(unittest.TestCase):
    """保存前必须先验证 —— 这里从"调用顺序"这一层锁住它。"""

    def test_verify_calls_describe_regions_with_the_supplied_key(self):
        with mock.patch.object(aliyun, "EcsClient") as client:
            client.return_value.describe_regions.return_value = ["cn-hongkong"]
            regions = credentials.verify("LTAIEXAMPLEKEY0000PSF1", "s", "cn-hongkong")
        self.assertEqual(regions, ["cn-hongkong"])
        passed = client.call_args[0][0]
        self.assertEqual(passed.access_key_id, "LTAIEXAMPLEKEY0000PSF1")
        self.assertEqual(passed.access_key_secret, "s")

    def test_verify_propagates_a_bad_key_instead_of_swallowing_it(self):
        with mock.patch.object(aliyun, "EcsClient") as client:
            client.return_value.describe_regions.side_effect = aliyun.AliyunError(
                "InvalidAccessKeyId.NotFound", "no"
            )
            with self.assertRaises(aliyun.AliyunError):
                credentials.verify("LTAIEXAMPLEKEY0000PSF1", "s")


if __name__ == "__main__":
    unittest.main()
