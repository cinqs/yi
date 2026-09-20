import os
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from yi import state  # noqa: E402


class TomlTests(unittest.TestCase):
    def test_our_writer_round_trips_through_the_stdlib_parser(self):
        """We write TOML by hand, so it has to survive the official parser."""
        payload = {
            "region": "cn-hongkong",
            "disk_size": 40,
            "spot_price_limit": 0.15,
            "budget": {"max_hours": 720, "max_gb": 300},
            "zones": ["cn-hongkong-b", "cn-hongkong-c"],
            "verbose": True,
        }
        parsed = tomllib.loads(state.dump_toml(payload))
        self.assertEqual(parsed["region"], "cn-hongkong")
        self.assertEqual(parsed["disk_size"], 40)
        self.assertEqual(parsed["spot_price_limit"], 0.15)
        self.assertEqual(parsed["zones"], ["cn-hongkong-b", "cn-hongkong-c"])
        self.assertTrue(parsed["verbose"])
        self.assertEqual(parsed["budget"]["max_gb"], 300)

    def test_default_config_survives_our_writer(self):
        parsed = tomllib.loads(state.dump_toml(state.DEFAULT_CONFIG))
        self.assertEqual(parsed["region"], "cn-hongkong")
        self.assertEqual(parsed["xray_version"], "latest")
        self.assertIsInstance(parsed["reality_dests"], list)
        self.assertEqual(parsed["budget"]["max_hours"], 720)

    def test_broken_config_reports_a_clear_error(self):
        tmp = tempfile.TemporaryDirectory()
        saved = os.environ.get("YI_HOME")
        os.environ["YI_HOME"] = tmp.name
        try:
            with open(state.config_path(), "w", encoding="utf-8") as handle:
                handle.write("this is not toml\n")
            with self.assertRaises(SystemExit) as ctx:
                state.load_config()
            self.assertIn("解析失败", str(ctx.exception))
        finally:
            if saved is None:
                os.environ.pop("YI_HOME", None)
            else:
                os.environ["YI_HOME"] = saved
            tmp.cleanup()

    def test_deep_merge_nested(self):
        merged = state.deep_merge({"a": 1, "b": {"c": 2, "d": 3}}, {"b": {"c": 9}})
        self.assertEqual(merged, {"a": 1, "b": {"c": 9, "d": 3}})


class HomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = os.environ.get("YI_HOME")
        os.environ["YI_HOME"] = self.tmp.name

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("YI_HOME", None)
        else:
            os.environ["YI_HOME"] = self._saved
        self.tmp.cleanup()

    def test_private_write_permissions(self):
        state.ensure_home()
        path = os.path.join(state.profiles_dir(), "secret.txt")
        state.write_private(path, "hunter2\n")
        self.assertEqual(state.file_mode(path), 0o600)

    def test_config_save_and_load(self):
        config = state.load_config()
        config["region"] = "cn-hongkong"
        config["budget"]["max_gb"] = 42
        state.save_config(config)
        self.assertTrue(os.path.exists(state.config_path()))
        reloaded = state.load_config()
        self.assertEqual(reloaded["budget"]["max_gb"], 42)
        self.assertEqual(reloaded["xray_port"], 443)

    def test_state_round_trip(self):
        state.ensure_home()
        state.save_state({"instance_id": "i-123", "server_info": {"port": 443}})
        self.assertEqual(state.load_state()["instance_id"], "i-123")


if __name__ == "__main__":
    unittest.main()
