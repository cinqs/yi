"""App 打包后的目录布局，必须能自己找到 `yi` 包。

这个文件的存在理由值得写下来：`agent.py` 有两种摆放方式 ——

    源码布局  <repo>/app/agent.py              包在 <repo>/src/yi
    打包布局  <App>/Contents/Resources/agent.py  包在 Resources/src/yi（同级！）

第一版只按源码布局找了 `dirname(HERE)/src`，于是装进 App 之后 import 直接失败。
**本机完全测不出来**：开发机上跑过 `pip install -e .`，`import yi` 永远成功，
恰好把这个错误盖住了。直到 CI 上用干净解释器跑才炸。

所以这里用 `python -S`（禁用 site-packages）起子进程 —— 一个真正看不见
已安装 `yi` 的解释器。没有这一步，这个测试会永远是绿的，等于没写。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IMPORT_PROBE = """
import importlib.util
spec = importlib.util.spec_from_file_location("agent", {path!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(mod.UI_DIR)
"""


class BundleLayoutTests(unittest.TestCase):
    def probe(self, agent_path: str, cwd: str) -> subprocess.CompletedProcess:
        # PYTHONPATH 清空 + `-S`：确保子进程里没有任何现成的 yi 可用
        env = dict(os.environ, PYTHONPATH="", YI_HOME=os.path.join(cwd, "home"))
        return subprocess.run(
            [sys.executable, "-S", "-c", IMPORT_PROBE.format(path=agent_path)],
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            timeout=90,
        )

    def test_source_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.probe(os.path.join(ROOT, "app", "agent.py"), tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), os.path.join(ROOT, "app", "ui"))

    def test_bundled_layout_puts_the_package_next_to_the_agent(self):
        """App 里 agent.py 和 src/ 是**同级**，不是上一级 —— 最容易写错的地方。"""
        with tempfile.TemporaryDirectory() as tmp:
            resources = os.path.join(tmp, "Resources")
            shutil.copytree(os.path.join(ROOT, "app", "ui"), os.path.join(resources, "ui"))
            os.makedirs(os.path.join(resources, "src"))
            shutil.copytree(os.path.join(ROOT, "src", "yi"), os.path.join(resources, "src", "yi"))
            shutil.copy(os.path.join(ROOT, "app", "agent.py"), os.path.join(resources, "agent.py"))

            result = self.probe(os.path.join(resources, "agent.py"), tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), os.path.join(resources, "ui"))


if __name__ == "__main__":
    unittest.main()
