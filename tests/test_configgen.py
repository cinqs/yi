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

    def test_mihomo_bypasses_lan_before_anything_else(self):
        """局域网必须先于 MATCH,PROXY 命中。

        少了这几条，访问 NAS / 打印机 / 路由器管理页会被兜底规则抓走，
        绕到香港再回来 —— 结果是打不开，而且把内网地址交给了代理。
        """
        rules = configgen.mihomo_rules()
        match_at = rules.index("  - MATCH,PROXY")
        for cidr in ("192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "127.0.0.0/8"):
            rule = f"  - IP-CIDR,{cidr},DIRECT,no-resolve"
            self.assertIn(rule, rules)
            self.assertLess(rules.index(rule), match_at, f"{cidr} 必须排在 MATCH 之前")
        self.assertLess(rules.index("  - IP-CIDR6,fc00::/7,DIRECT,no-resolve"), match_at)

    def test_ip_rules_say_no_resolve(self):
        """IP-CIDR 不带 no-resolve 会为了匹配域名而去解析 DNS —— 白白多一次查询，
        而且在 fake-ip 下拿到的是假地址，等于白解析。"""
        for rule in configgen.mihomo_rules():
            if "IP-CIDR," in rule or "IP-CIDR6," in rule:
                self.assertTrue(rule.endswith(",no-resolve"), rule)

    def test_matcher_is_last(self):
        """兜底规则必须在最后 —— 放前面会让后面所有规则失效。"""
        self.assertEqual(configgen.mihomo_rules()[-1], "  - MATCH,PROXY")

    def test_cn_rules_use_domain_first_then_geoip(self):
        rules = configgen.mihomo_rules()
        self.assertIn("  - DOMAIN-SUFFIX,cn,DIRECT", rules)
        self.assertIn("  - GEOIP,CN,DIRECT", rules)
        # 域名规则先于 GEOIP：域名是字符串比对，不用解析，首包更快
        self.assertLess(rules.index("  - DOMAIN-SUFFIX,cn,DIRECT"), rules.index("  - GEOIP,CN,DIRECT"))

    def test_custom_direct_domains_are_used(self):
        rules = configgen.mihomo_rules(["Example.COM", ".foo.cn", "example.com", ""])
        self.assertIn("  - DOMAIN-SUFFIX,example.com,DIRECT", rules)
        self.assertIn("  - DOMAIN-SUFFIX,foo.cn,DIRECT", rules)
        # 重复与空值被清掉：同一个域名出现两次是纯粹的噪音
        self.assertEqual(rules.count("  - DOMAIN-SUFFIX,example.com,DIRECT"), 1)

    def test_empty_direct_domains_means_only_the_builtin_rules(self):
        rules = configgen.mihomo_rules([])
        self.assertNotIn("  - DOMAIN-SUFFIX,qq.com,DIRECT", rules)
        self.assertIn("  - DOMAIN-SUFFIX,cn,DIRECT", rules)

    def test_fake_ip_does_not_swallow_local_names(self):
        """fake-ip 会代理掉所有域名解析，.local/.lan 被它接走的话，
        AirDrop、打印机、投屏会突然找不到设备，而用户想不到是代理干的。"""
        text = configgen.render_mihomo(INFO)
        self.assertIn("fake-ip-filter:", text)
        for pattern in ("'*.lan'", "'*.local'"):
            self.assertIn(pattern, text)

    def test_generated_config_drops_optional_controller_lines_cleanly(self):
        """没有控制接口时，配置里不该留下空行或占位符。"""
        text = configgen.render_mihomo(INFO)
        self.assertNotIn("{", text)
        self.assertNotIn("\n\n\n", text)

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
