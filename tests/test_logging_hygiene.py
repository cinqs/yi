"""A logging call that mixes `{}` placeholders with %-style args raises at runtime.

That is exactly what happened on the first live run: the "安全组已满足要求" line
blew up with a TypeError *after* the 443 rule had already been handled, which buried
the real Aliyun error underneath a logging traceback. Static check, so it cannot
recur anywhere in the package.
"""

import ast
import os
import pathlib
import unittest

SRC = pathlib.Path(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "yi"))


def logging_calls():
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_logger = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "log"
            )
            if is_logger and node.args:
                yield path.name, node.lineno, node


class LoggingHygieneTests(unittest.TestCase):
    def test_no_brace_placeholders_with_logging_args(self):
        offenders = []
        for name, lineno, node in logging_calls():
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if "{}" in first.value and len(node.args) > 1:
                    offenders.append(f"{name}:{lineno} {first.value!r}")
        self.assertEqual(
            offenders,
            [],
            "这些日志把 {} 和 %-参数混用了，运行时会抛 TypeError:\n  " + "\n  ".join(offenders),
        )

    def test_no_brace_placeholders_with_kwargs(self):
        offenders = []
        for name, lineno, node in logging_calls():
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if "{}" in first.value and node.keywords:
                    offenders.append(f"{name}:{lineno}")
        self.assertEqual(offenders, [])

    def test_the_scanner_actually_finds_log_calls(self):
        found = list(logging_calls())
        self.assertGreater(len(found), 20, "扫描器没找到日志调用，说明它失效了")


if __name__ == "__main__":
    unittest.main()
