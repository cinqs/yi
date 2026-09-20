"""给新用户取代理内核时的选择逻辑。

mihomo 的 release 里每个平台/架构一个包，还有 `-compatible` 之类容易误选的变体。
**选错的表现是"下载成功了但跑不起来"**，而真机上下载一次要几十 MB ——
所以这段写成纯函数，不碰网络也不碰磁盘，在这里把它钉死。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sandbox  # noqa: E402,F401  (隔离 YI_HOME，必须在导入 yi 之前)

from yi import proxy  # noqa: E402


def release(*names):
    """拼一个形状和 GitHub API 一致的 release JSON。"""
    return {
        "tag_name": "v1.19.2",
        "assets": [{"name": n, "browser_download_url": f"https://example.invalid/{n}"} for n in names],
    }


# 真实的 mihomo release 大概长这样，含几个容易误选的变体
REAL_ASSETS = [
    "mihomo-darwin-amd64-v1.19.2.gz",
    "mihomo-darwin-arm64-v1.19.2.gz",
    "mihomo-darwin-amd64-compatible-v1.19.2.gz",
    "mihomo-freebsd-amd64-v1.19.2.gz",
    "mihomo-linux-amd64-v1.19.2.gz",
    "mihomo-linux-arm64-v1.19.2.gz",
    "mihomo-linux-amd64-go120-v1.19.2.gz",
    "mihomo-windows-amd64-v1.19.2.zip",
    "mihomo-darwin-arm64-v1.19.2.zip",
    "mihomo-source-v1.19.2.tar.gz",
]


class ArchTests(unittest.TestCase):
    def test_go_arch_covers_what_macos_and_linux_report(self):
        self.assertEqual(proxy.go_arch("x86_64"), "amd64")
        self.assertEqual(proxy.go_arch("AMD64"), "amd64")
        self.assertEqual(proxy.go_arch("arm64"), "arm64")
        self.assertEqual(proxy.go_arch("aarch64"), "arm64")

    def test_go_os_mapping(self):
        self.assertEqual(proxy.go_os("Darwin"), "darwin")
        self.assertEqual(proxy.go_os("Linux"), "linux")


class AssetPickTests(unittest.TestCase):
    def test_apple_silicon(self):
        self.assertEqual(
            proxy.kernel_asset_name(release(*REAL_ASSETS), machine="arm64", sysname="Darwin"),
            "mihomo-darwin-arm64-v1.19.2.gz",
        )

    def test_intel_mac(self):
        self.assertEqual(
            proxy.kernel_asset_name(release(*REAL_ASSETS), machine="x86_64", sysname="Darwin"),
            "mihomo-darwin-amd64-v1.19.2.gz",
        )

    def test_linux_arm(self):
        self.assertEqual(
            proxy.kernel_asset_name(release(*REAL_ASSETS), machine="aarch64", sysname="Linux"),
            "mihomo-linux-arm64-v1.19.2.gz",
        )

    def test_prefers_plain_over_compatible_and_gz_over_zip(self):
        """`-compatible` 是给老 CPU 的降级版；`.gz` 解压即用，比 `.zip` 少一层。"""
        picked = proxy.kernel_asset_name(release(*REAL_ASSETS), machine="x86_64", sysname="Darwin")
        self.assertNotIn("compatible", picked)
        self.assertNotIn("go120", picked)
        self.assertTrue(picked.endswith(".gz"))

    def test_windows_falls_back_to_zip(self):
        self.assertEqual(
            proxy.kernel_asset_name(release(*REAL_ASSETS), machine="x86_64", sysname="Windows"),
            "mihomo-windows-amd64-v1.19.2.zip",
        )

    def test_returns_none_instead_of_picking_something_wrong(self):
        """没有匹配的必须返回 None —— 随便挑一个 = 用户下到跑不起来的二进制。"""
        self.assertIsNone(proxy.kernel_asset_name(release(*REAL_ASSETS), machine="riscv64", sysname="Linux"))
        self.assertIsNone(proxy.kernel_asset_name(release(), machine="arm64", sysname="Darwin"))

    def test_ignores_source_tarball(self):
        self.assertIsNone(
            proxy.kernel_asset_name(
                release("mihomo-source-v1.19.2.tar.gz"), machine="arm64", sysname="Darwin"
            )
        )


class TargetPathTests(unittest.TestCase):
    def test_kernel_lands_under_the_data_dir(self):
        self.assertTrue(proxy.kernel_target_path().endswith("bin/mihomo"))
        self.assertTrue(proxy.kernel_target_path().startswith(sandbox.SANDBOX_HOME))


if __name__ == "__main__":
    unittest.main()
