import base64
import json
import os
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import sandbox  # noqa: E402,F401  (隔离 YI_HOME，必须在导入 yi 之前)

from yi import configgen, state  # noqa: E402

INFO = {
    "ready": True,
    "created_at": "2026-09-19T10:00:00Z",
    "xray_version": "1.8.24",
    "address": "47.52.1.2",
    "port": 443,
    "uuid": "11111111-2222-3333-4444-555555555555",
    "flow": "xtls-rprx-vision",
    "security": "reality",
    "sni": "www.microsoft.com",
    "fingerprint": "chrome",
    "public_key": "PUBKEY_abc-123",
    "short_id": "deadbeefcafe0001",
}


class LinkTests(unittest.TestCase):
    def test_link_shape(self):
        link = configgen.build_link(INFO, "HK-Spot")
        parsed = urlparse(link)
        self.assertEqual(parsed.scheme, "vless")
        self.assertEqual(parsed.username, INFO["uuid"])
        self.assertEqual(parsed.hostname, "47.52.1.2")
        self.assertEqual(parsed.port, 443)
        query = parse_qs(parsed.query)
        self.assertEqual(query["security"], ["reality"])
        self.assertEqual(query["flow"], ["xtls-rprx-vision"])
        self.assertEqual(query["pbk"], [INFO["public_key"]])
        self.assertEqual(query["sid"], [INFO["short_id"]])
        self.assertEqual(parsed.fragment, "HK-Spot")

    def test_subscription_blob_round_trip(self):
        link = configgen.build_link(INFO)
        blob = configgen.subscription_blob([link])
        self.assertEqual(base64.b64decode(blob).decode("utf-8").strip(), link)


class ProfileTests(unittest.TestCase):
    def test_singbox_is_valid_json_and_matches_server(self):
        payload = json.loads(configgen.render_singbox(INFO))
        outbound = payload["outbounds"][0]
        self.assertEqual(outbound["server"], INFO["address"])
        self.assertEqual(outbound["uuid"], INFO["uuid"])
        self.assertTrue(outbound["tls"]["reality"]["enabled"])
        self.assertEqual(outbound["tls"]["reality"]["public_key"], INFO["public_key"])
        self.assertEqual(payload["inbounds"][0]["type"], "tun")

    def test_mihomo_contains_reality_options(self):
        text = configgen.render_mihomo(INFO)
        self.assertIn("type: vless", text)
        self.assertIn("public-key: {}".format(INFO["public_key"]), text)
        self.assertIn('short-id: "{}"'.format(INFO["short_id"]), text)
        self.assertIn("MATCH,PROXY", text)

    def test_write_profiles_are_private(self):
        tmp = tempfile.TemporaryDirectory()
        saved = os.environ.get("YI_HOME")
        os.environ["YI_HOME"] = tmp.name
        try:
            paths = configgen.write_profiles(INFO, "HK-Spot")
            self.assertEqual(sorted(paths), sorted(configgen.PROFILE_FILES))
            for path in paths.values():
                self.assertTrue(os.path.exists(path))
                self.assertEqual(state.file_mode(path), 0o600)
        finally:
            if saved is None:
                os.environ.pop("YI_HOME", None)
            else:
                os.environ["YI_HOME"] = saved
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
