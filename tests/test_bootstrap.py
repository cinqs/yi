import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import sandbox  # noqa: E402,F401  (隔离 YI_HOME，必须在导入 yi 之前)

from yi import state  # noqa: E402
from yi.bootstrap import render_user_data  # noqa: E402


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.config = state.load_config()

    def test_no_unresolved_placeholders(self):
        script = render_user_data(self.config)
        self.assertNotIn("{{", script)
        self.assertNotIn("}}", script)

    def test_shebang_and_settings(self):
        self.config["xray_port"] = 8443
        self.config["xray_version"] = "v9.9.9"
        script = render_user_data(self.config)
        self.assertTrue(script.startswith("#!/bin/bash"))
        self.assertIn("v9.9.9", script)
        self.assertIn("8443", script)
        self.assertIn("xtls-rprx-vision", script)
        self.assertIn("bbr", script)

    def test_server_info_is_written_last(self):
        script = render_user_data(self.config)
        self.assertIn("server-info.json.tmp", script)
        self.assertLess(
            script.index("server-info.json.tmp"), script.index("mv /root/yi/server-info.json.tmp")
        )

    def test_selftest_client_config_is_valid_json_after_shell_expansion(self):
        """The loopback client config is built by a shell heredoc, so expand it
        the way bash would and make sure Xray will actually accept the JSON."""
        script = render_user_data(self.config)
        match = re.search(r"cat > /root/yi/client-test\.json <<EOF\n(.*?)\nEOF\n", script, re.S)
        self.assertIsNotNone(match, "找不到 client-test.json 的生成片段")
        expanded = (
            match.group(1)
            .replace("${UUID}", "11111111-2222-3333-4444-555555555555")
            .replace("${PUBLIC_KEY}", "PUBKEY_abc-123")
            .replace("${SHORT_ID}", "deadbeefcafe0001")
        )
        payload = json.loads(expanded)
        outbound = payload["outbounds"][0]
        self.assertEqual(outbound["protocol"], "vless")
        self.assertEqual(outbound["settings"]["vnext"][0]["address"], "127.0.0.1")
        self.assertTrue(outbound["streamSettings"]["realitySettings"]["publicKey"])
        self.assertEqual(payload["inbounds"][0]["port"], 10808)

    def test_selftest_script_exists_and_runs_before_server_info(self):
        script = render_user_data(self.config)
        self.assertIn("/opt/yi/selftest.sh", script)
        self.assertIn("--socks5-hostname 127.0.0.1:10808", script)
        self.assertIn('"selftest": "${SELFTEST_RESULT}"', script)
        self.assertLess(
            script.index("SELFTEST_RESULT=$(/opt/yi/selftest.sh"),
            script.index("cat > /root/yi/server-info.json.tmp"),
        )

    def test_reality_dests_are_tried_in_order(self):
        """The dest cannot be hardcoded: REALITY breaks when the dest's certificate
        chain is longer than its buffer (www.microsoft.com, 8273 bytes, measured)."""
        self.config["reality_dests"] = ["first.example", "second.example"]
        script = render_user_data(self.config)
        self.assertIn("for DEST in first.example second.example; do", script)
        self.assertIn('write_server_config "${DEST}"', script)
        self.assertIn('"dest": "${dest}:443"', script)
        self.assertIn('"serverNames": ["${dest}"]', script)
        # the winning dest is what lands in server-info.json for the clients
        self.assertIn('"sni": "${WINNER_DEST}"', script)
        self.assertIn('WINNER_DEST="${DEST}"', script)

    def test_temp_server_config_ends_with_json(self):
        """xray picks the config format from the file extension; a `.tmp` path makes
        every candidate look invalid (measured on a live box)."""
        script = render_user_data(self.config)
        match = re.search(r"local tmpcfg=(\S+)", script)
        self.assertIsNotNone(match, "write_server_config 里没有 tmpcfg")
        self.assertTrue(
            match.group(1).endswith(".json"),
            "临时配置必须以 .json 结尾，否则 xray run -test 会报 Failed to get format",
        )
        self.assertIn('xray run -test -config "$tmpcfg"', script)

    def test_selftest_result_is_trimmed_before_comparison(self):
        """`tr -c '[:print:]' ' '` leaves a trailing space, so "ok " never equals "ok"
        and the dest loop would try every candidate instead of stopping at the first good one."""
        script = render_user_data(self.config)
        capture = re.search(r"SELFTEST_RESULT=\$\(.*\)\n", script).group(0)
        self.assertIn("awk '{print $1}'", capture)

    def test_default_dest_is_not_microsoft(self):
        script = render_user_data(state.load_config())
        self.assertIn("for DEST in www.cloudflare.com", script)
        self.assertNotIn("for DEST in www.microsoft.com", script)

    def test_legacy_single_sni_config_still_renders(self):
        legacy = dict(self.config)
        legacy.pop("reality_dests", None)
        legacy["reality_sni"] = "legacy.example"
        script = render_user_data(legacy)
        self.assertIn("for DEST in legacy.example; do", script)


if __name__ == "__main__":
    unittest.main()
