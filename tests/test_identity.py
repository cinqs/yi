"""凭据持久化：竞价实例重建后，客户端不该需要重新导入配置。

这是"自动 IP 管理"能不能真正无感的关键——只持久化 UUID 不够，REALITY 密钥对
也必须跟着复用它才认得出同一台"服务器"。
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sandbox  # noqa: E402,F401

from yi import bootstrap, identity, state  # noqa: E402


class IdentityTests(unittest.TestCase):
    def setUp(self):
        state.ensure_home()

    def test_ensure_creates_and_reuses(self):
        first = identity.ensure_identity()
        second = identity.ensure_identity()
        self.assertEqual(first["uuid"], second["uuid"])
        self.assertEqual(first["private_key"], second["private_key"])
        self.assertEqual(len(first["short_id"]), 16)

    def test_identity_file_is_private(self):
        identity.ensure_identity()
        self.assertEqual(state.file_mode(identity.identity_path()), 0o600)

    def test_rotate_changes_everything(self):
        first = identity.ensure_identity()
        rotated = identity.ensure_identity(rotate=True)
        self.assertNotEqual(first["uuid"], rotated["uuid"])
        self.assertNotEqual(first["private_key"], rotated["private_key"])

    @unittest.skipIf(shutil.which("openssl") is None, "本机没有 openssl")
    def test_generated_pair_is_accepted_by_xray(self):
        """openssl 生成的 X25519 必须能被 REALITY 用，否则持久化就是假的。"""
        pair = identity.generate_keypair()
        self.assertIsNotNone(pair)
        xray = os.path.expanduser("~/.config/yi/mac/xray")
        if not os.path.exists(xray):
            self.skipTest("本机没有 xray 可用来交叉验证")
        out = subprocess.run(
            [xray, "x25519", "-i", pair["private_key"]], capture_output=True, text=True, timeout=15
        ).stdout
        self.assertIn(pair["public_key"], out, "xray 由私钥推出的公钥与 openssl 的不一致")

    def test_broken_file_is_regenerated_not_fatal(self):
        identity.ensure_identity()
        with open(identity.identity_path(), "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        fresh = identity.ensure_identity()
        self.assertTrue(fresh["uuid"])
        # 坏文件已被可用的新文件取代
        with open(identity.identity_path(), encoding="utf-8") as handle:
            self.assertTrue(json.load(handle)["uuid"])


class BootstrapInjectionTests(unittest.TestCase):
    def setUp(self):
        self.config = state.load_config()
        self.identity = {
            "uuid": "11111111-2222-3333-4444-555555555555",
            "short_id": "aabbccddeeff0011",
            "private_key": "PRIVKEY_abc",
            "public_key": "PUBKEY_def",
        }

    def test_injected_credentials_land_in_the_script(self):
        script = bootstrap.render_user_data(self.config, self.identity)
        self.assertIn('UUID="11111111-2222-3333-4444-555555555555"', script)
        self.assertIn('SHORT_ID="aabbccddeeff0011"', script)
        self.assertIn('PRIVATE_KEY="PRIVKEY_abc"', script)
        self.assertIn('PUBLIC_KEY="PUBKEY_def"', script)
        self.assertNotIn("{{", script)

    def test_without_identity_the_script_still_generates_its_own(self):
        script = bootstrap.render_user_data(self.config, None)
        self.assertIn("未注入凭据", script)
        self.assertIn("xray x25519", script)
        self.assertIn('UUID=""', script)
        self.assertNotIn("{{", script)

    def test_client_config_uses_the_injected_credentials(self):
        script = bootstrap.render_user_data(self.config, self.identity)
        # 回环自检用的客户端配置必须和注入的凭据一致，否则自检会失败
        self.assertIn('"publicKey": "${PUBLIC_KEY}"', script)
        self.assertIn('"shortId": "${SHORT_ID}"', script)


if __name__ == "__main__":
    unittest.main()
