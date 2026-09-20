"""让**真实的 mihomo 内核**来验收我们生成的配置。

为什么必须有这一层：字符串断言只能证明"我把这行写进去了"，证明不了"内核认这行"。
一个缩进错、一个拼错的规则类型、一个不能为空却空了的字段 —— 字符串测试全绿，
真机上 `connect` 直接起不来。这两个的差距，只有真跑一次内核才能抹平。

有 mihomo 和地理数据库就真跑 `mihomo -t`；没有就跳过（CI 上通常没有）。
"""

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sandbox  # noqa: E402,F401  (隔离 YI_HOME，必须在导入 yi 之前)

from yi import configgen, proxy  # noqa: E402


def reality_key() -> str:
    """一个形状合法的 REALITY 公钥：32 字节 x25519，base64 **无填充**（43 字符）。

    不能随便写个占位字符串 —— 内核会先校验它，失败的报错和"规则写错"长得一样，
    会让人白白查半天。
    """
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def geodata_dir() -> str | None:
    """找一份 geoip 数据库。

    测试跑在沙箱 YI_HOME 里，里面没有数据库；而缺了它内核会尝试联网下载，
    那就变成"测网络"而不是"测配置"了。所以去开发者真实的数据目录里借一份，
    借不到就跳过。
    """
    home = os.path.expanduser("~")
    for candidate in (
        os.path.join(home, ".config", "yi", "mihomo"),
        os.path.join(home, ".config", "mihomo"),
    ):
        if os.path.exists(os.path.join(candidate, "geoip.metadb")):
            return candidate
    return None


def find_kernel() -> str | None:
    """找 mihomo 二进制。

    不能只用 `proxy.mihomo_path()`：测试里 `sandbox` 把 YI_HOME 指到了临时目录，
    工具自带的那份内核在真实目录里，于是永远找不到、这层校验就永远被跳过 ——
    一个永远跳过的测试等于没有测试。
    """
    found = proxy.mihomo_path()
    if found:
        return found
    home = os.path.expanduser("~")
    for candidate in (
        os.path.join(home, ".config", "yi", "bin", "mihomo"),
        os.path.join(home, ".config", "mihomo", "mihomo"),
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("mihomo") or shutil.which("clash-meta")


class KernelValidationTests(unittest.TestCase):
    def setUp(self):
        binary = find_kernel()
        if not binary:
            self.skipTest("没有 mihomo 内核，跳过：这一层校验需要真内核")
        self.binary = binary
        source = geodata_dir()
        if not source:
            self.skipTest("没有 geoip 数据库，跳过：缺了它内核会去联网下载")
        self.data_dir = tempfile.mkdtemp(prefix="yi-mihomo-")
        self.addCleanup(shutil.rmtree, self.data_dir, True)
        # 只需要数据库，别把开发机正在用的 config.yaml / 缓存也拖进来
        shutil.copy(os.path.join(source, "geoip.metadb"), self.data_dir)

    def _render_and_test(self, **kwargs) -> subprocess.CompletedProcess:
        info = {
            "address": "203.0.113.10",
            "port": 443,
            "uuid": "11111111-2222-3333-4444-555555555555",
            "flow": "xtls-rprx-vision",
            "security": "reality",
            "sni": "www.cloudflare.com",
            "fingerprint": "chrome",
            "public_key": reality_key(),
            "short_id": "deadbeefcafe0001",
        }
        path = os.path.join(self.data_dir, "config.yaml")
        with open(path, "w") as fh:
            fh.write(configgen.render_mihomo(info, "HK-Spot", **kwargs))
        return subprocess.run(
            [self.binary, "-t", "-d", self.data_dir, "-f", path],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_kernel_accepts_the_default_profile(self):
        result = self._render_and_test(mixed_port=7899)
        self.assertEqual(
            result.returncode,
            0,
            f"内核拒绝了生成的配置：\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("test is successful", result.stdout + result.stderr)

    def test_kernel_accepts_the_profile_without_a_control_socket(self):
        """命令行与 App 两种调用方式生成的配置都要能用。"""
        result = self._render_and_test(mixed_port=7899, controller_socket="")
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")

    def test_kernel_accepts_a_user_extended_direct_domain_list(self):
        """用户往 direct_domains 里加了域名（哪怕是奇怪的写法）也要能用。"""
        result = self._render_and_test(
            mixed_port=7899,
            controller_socket="/tmp/yi-test.sock",
            direct_domains=["example.com", ".Example.ORG", "内网.local"],
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
