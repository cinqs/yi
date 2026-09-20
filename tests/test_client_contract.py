"""The contract that keeps Android and macOS working.

The server writes one set of parameters into server-info.json. Every client
artifact must be derived from exactly those parameters, otherwise one platform
silently stops connecting. These tests fail loudly when the two drift apart.
"""

import json
import os
import re
import sys
import unittest
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import sandbox  # noqa: E402,F401  (隔离 YI_HOME，必须在导入 yi 之前)

from yi import configgen, state  # noqa: E402
from yi.bootstrap import render_user_data  # noqa: E402

INFO = {
    "ready": True,
    "created_at": "2026-09-19T10:00:00Z",
    "xray_version": "1.8.24",
    "protocol": "vless",
    "transport": "tcp",
    "security": "reality",
    "address": "47.52.1.2",
    "port": 443,
    "uuid": "11111111-2222-3333-4444-555555555555",
    "flow": "xtls-rprx-vision",
    "sni": "www.microsoft.com",
    "fingerprint": "chrome",
    "public_key": "PUBKEYabc123",
    "short_id": "deadbeefcafe0001",
}


def _mihomo_fields(text):
    def grab(pattern, default=None):
        match = re.search(pattern, text, re.MULTILINE)
        return match.group(1) if match else default

    return {
        "server": grab(r"^\s*server:\s*(\S+)$"),
        "port": int(grab(r"^\s*port:\s*(\d+)$")),
        "uuid": grab(r"^\s*uuid:\s*(\S+)$"),
        "flow": grab(r"^\s*flow:\s*(\S+)$"),
        "sni": grab(r"^\s*servername:\s*(\S+)$"),
        "fingerprint": grab(r"^\s*client-fingerprint:\s*(\S+)$"),
        "public_key": grab(r"^\s*public-key:\s*(\S+)$"),
        "short_id": grab(r'^\s*short-id:\s*"?([^"\s]*)"?$'),
    }


class ClientContractTests(unittest.TestCase):
    def test_every_client_agrees_with_server(self):
        link = urlparse(configgen.build_link(INFO))
        query = parse_qs(link.query)
        mihomo = _mihomo_fields(configgen.render_mihomo(INFO))
        singbox = json.loads(configgen.render_singbox(INFO))["outbounds"][0]
        reality = singbox["tls"]["reality"]

        self.assertEqual(link.hostname, INFO["address"])
        self.assertEqual(link.port, INFO["port"])
        self.assertEqual(link.username, INFO["uuid"])
        self.assertEqual(query["pbk"], [INFO["public_key"]])
        self.assertEqual(query["sid"], [INFO["short_id"]])
        self.assertEqual(query["sni"], [INFO["sni"]])
        self.assertEqual(query["flow"], [INFO["flow"]])

        self.assertEqual(mihomo["server"], INFO["address"])
        self.assertEqual(mihomo["port"], INFO["port"])
        self.assertEqual(mihomo["uuid"], INFO["uuid"])
        self.assertEqual(mihomo["flow"], INFO["flow"])
        self.assertEqual(mihomo["sni"], INFO["sni"])
        self.assertEqual(mihomo["fingerprint"], INFO["fingerprint"])
        self.assertEqual(mihomo["public_key"], INFO["public_key"])
        self.assertEqual(mihomo["short_id"], INFO["short_id"])

        self.assertEqual(singbox["server"], INFO["address"])
        self.assertEqual(singbox["server_port"], INFO["port"])
        self.assertEqual(singbox["uuid"], INFO["uuid"])
        self.assertEqual(singbox["flow"], INFO["flow"])
        self.assertEqual(singbox["tls"]["server_name"], INFO["sni"])
        self.assertEqual(singbox["tls"]["utls"]["fingerprint"], INFO["fingerprint"])
        self.assertEqual(reality["public_key"], INFO["public_key"])
        self.assertEqual(reality["short_id"], INFO["short_id"])

    def test_server_config_uses_the_same_flow(self):
        config = state.load_config()
        script = render_user_data(config)
        self.assertIn('"flow": "{}"'.format(INFO["flow"]), script)
        self.assertNotIn("{{", script)

    def test_disabling_flow_removes_it_everywhere(self):
        info = dict(INFO, flow="")

        link = urlparse(configgen.build_link(info))
        self.assertNotIn("flow", parse_qs(link.query))

        mihomo = _mihomo_fields(configgen.render_mihomo(info))
        self.assertIsNone(mihomo["flow"])

        singbox = json.loads(configgen.render_singbox(info))["outbounds"][0]
        self.assertNotIn("flow", singbox)

        config = dict(state.load_config(), vless_flow="")
        script = render_user_data(config)
        # the inbound must carry no flow at all; server-info records it as empty
        self.assertIn('"clients": [{ "id": "${UUID}" }]', script)
        self.assertNotIn('"flow": "xtls-rprx-vision"', script)
        self.assertIn('"flow": ""', script)
        self.assertIn("server-info.json", script)

    def test_server_info_flow_field_matches_config(self):
        config = dict(state.load_config(), vless_flow="xtls-rprx-vision")
        script = render_user_data(config)
        self.assertIn('"flow": "xtls-rprx-vision"', script)


if __name__ == "__main__":
    unittest.main()
