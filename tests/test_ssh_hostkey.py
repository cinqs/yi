"""阿里云会把回收的 IP 再分配出去，于是同一个 IP 上会换一台机器、换一把主机密钥。

`StrictHostKeyChecking=accept-new` 对"变更过的密钥"是拒绝的——这个策略本身没错
（防 MITM），但对"我们自己刚建的实例"就成了假警报：wait_ready 会一直重试到超时。
实测就是这么卡了 8 分钟。这里锁住"识别并清掉旧记录后重试一次"的行为。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sandbox  # noqa: E402,F401

from yi import ssh, state  # noqa: E402


class Completed:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class HostKeyChangeTests(unittest.TestCase):
    def setUp(self):
        state.ensure_home()
        with open(state.known_hosts_path(), "w", encoding="utf-8") as handle:
            handle.write("1.2.3.4 ssh-ed25519 AAAAold\n")

    def test_changed_key_is_forgotten_and_retried_once(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "ssh":
                if len([item for item in calls if item[0] == "ssh"]) == 1:
                    return Completed(255, stderr="WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!")
                return Completed(0, stdout="server-info\n")
            return Completed(0)

        with mock.patch.object(ssh.subprocess, "run", side_effect=fake_run):
            output = ssh.run("1.2.3.4", "cat /root/yi/server-info.json")

        self.assertEqual(output, "server-info\n")
        ssh_calls = [item for item in calls if item[0] == "ssh"]
        self.assertEqual(len(ssh_calls), 2, "应该重试一次")
        keygen_calls = [item for item in calls if item[0] == "ssh-keygen"]
        self.assertEqual(len(keygen_calls), 1, "应该清掉旧记录")
        self.assertIn("-R", keygen_calls[0])
        self.assertIn(state.known_hosts_path(), keygen_calls[0])

    def test_ordinary_failure_is_not_retried(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return Completed(255, stderr="Permission denied (publickey).")

        with mock.patch.object(ssh.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(ssh.SshError):
                ssh.run("1.2.3.4", "true")
        self.assertEqual([item for item in calls if item[0] == "ssh-keygen"], [])
        self.assertEqual(len([item for item in calls if item[0] == "ssh"]), 1)

    def test_check_false_does_not_raise_for_other_errors(self):
        with mock.patch.object(ssh.subprocess, "run", return_value=Completed(1, stderr="boom")):
            self.assertEqual(ssh.run("1.2.3.4", "true", check=False), "")


if __name__ == "__main__":
    unittest.main()
