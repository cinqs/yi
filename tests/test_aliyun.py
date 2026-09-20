import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from yi.aliyun import (  # noqa: E402
    AliyunError,
    Credentials,
    canonical_query,
    load_credentials,
    percent_encode,
    sign,
)


class SignatureTests(unittest.TestCase):
    def test_percent_encode_keeps_tilde(self):
        self.assertEqual(percent_encode("~"), "~")
        self.assertEqual(percent_encode("a b"), "a%20b")
        self.assertEqual(percent_encode("a/b"), "a%2Fb")
        self.assertEqual(percent_encode("a+b"), "a%2Bb")

    def test_canonical_query_is_sorted(self):
        query = canonical_query({"b": "2", "a": "1", "c": None})
        self.assertEqual(query, "a=1&b=2")

    def test_signature_is_base64_sha1_hmac(self):
        signature = sign({"Action": "DescribeRegions", "Format": "JSON"}, "secret")
        self.assertEqual(len(signature), 28)  # base64 of 20 raw bytes
        self.assertEqual(signature, sign({"Format": "JSON", "Action": "DescribeRegions"}, "secret"))
        self.assertNotEqual(signature, sign({"Action": "DescribeRegions", "Format": "JSON"}, "other"))


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            key: os.environ.pop(key, None)
            for key in (
                "ALIBABA_CLOUD_ACCESS_KEY_ID",
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
                "ALIYUN_ACCESS_KEY_ID",
                "ALIYUN_ACCESS_KEY_SECRET",
            )
        }

    def tearDown(self):
        for key, value in self._saved.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)

    def test_env_wins(self):
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"] = "LTAIEXAMPLE"
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"] = "secretvalue"
        creds = load_credentials()
        self.assertEqual(creds.source, "env")
        self.assertEqual(creds.masked_id(), "LTAI****MPLE")

    def test_profile_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, ".aliyun"))
            path = os.path.join(tmp, ".aliyun", "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "current": "work",
                        "profiles": [
                            {
                                "name": "work",
                                "mode": "AK",
                                "access_key_id": "LTAIWORK",
                                "access_key_secret": "shh",
                            }
                        ],
                    },
                    fh,
                )
            creds = load_credentials("work", home=tmp)
            self.assertEqual(creds.access_key_id, "LTAIWORK")
            self.assertIn("config.json#work", creds.source)

    def test_missing_profile_reports_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, ".aliyun"))
            with open(os.path.join(tmp, ".aliyun", "config.json"), "w", encoding="utf-8") as fh:
                json.dump({"profiles": [{"name": "a"}, {"name": "b"}]}, fh)
            with self.assertRaises(AliyunError) as ctx:
                load_credentials("nope", home=tmp)
            self.assertIn("a, b", str(ctx.exception))


class ErrorHintTests(unittest.TestCase):
    def test_known_hint(self):
        error = AliyunError("InstanceType.StockNotEnough", "no stock", "req-1", 400, "CreateInstance")
        self.assertIn("库存", error.hint())
        self.assertIn("req-1", str(error))

    def test_retryable(self):
        self.assertTrue(AliyunError("Throttling", "slow").is_retryable())
        self.assertFalse(AliyunError("InvalidParameter", "bad").is_retryable())


class CredentialsModelTests(unittest.TestCase):
    def test_mask_short_id(self):
        self.assertEqual(Credentials("abc", "s", "env").masked_id(), "****")


if __name__ == "__main__":
    unittest.main()
