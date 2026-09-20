"""`yi watch` is the part that has to keep working while nobody is looking.

The decision it makes — "the instance is gone, rebuild it" — is only as good as the
API call behind it. DescribeInstanceStatus was measured returning another instance's
status when its filter was ignored, so watch must decide from the plain listing and
must not act on a single bad answer.
"""

import argparse
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sandbox  # noqa: E402,F401
from test_ecs_request import CapturingEcs  # noqa: E402

from yi import cli, state  # noqa: E402
from yi.aliyun import AliyunError  # noqa: E402


def watch_args(once=True, interval=10.0):
    return argparse.Namespace(once=once, interval=interval)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.environ["YI_HOME"]

    def _run(self, ecs, instance_id="i-test"):
        state.save_state({"instance_id": instance_id, "region": "cn-hongkong"})
        with mock.patch.object(cli, "_client", return_value=ecs):
            with mock.patch.object(cli, "_recreate_safely") as recreate:
                with contextlib.redirect_stderr(io.StringIO()):
                    cli.cmd_watch(watch_args())
        return recreate

    def test_a_single_miss_does_not_trigger_a_rebuild(self):
        ecs = CapturingEcs({"DescribeInstances": {"Instances": {"Instance": []}}})
        recreate = self._run(ecs)
        recreate.assert_not_called()

    def test_healthy_instance_never_triggers_a_rebuild(self):
        ecs = CapturingEcs(
            {
                "DescribeInstances": {
                    "Instances": {"Instance": [{"InstanceId": "i-test", "Status": "Running"}]}
                }
            }
        )
        recreate = self._run(ecs)
        recreate.assert_not_called()

    def test_two_consecutive_misses_trigger_a_rebuild(self):
        """--once stops after one poll, so drive the real loop and break out of it."""
        ecs = CapturingEcs({"DescribeInstances": {"Instances": {"Instance": []}}})
        state.save_state({"instance_id": "i-test", "region": "cn-hongkong"})
        sleeps = {"n": 0}

        def fake_sleep(_seconds):
            sleeps["n"] += 1
            if sleeps["n"] >= 2:
                raise KeyboardInterrupt

        with mock.patch.object(cli, "_client", return_value=ecs):
            with mock.patch.object(cli, "_recreate_safely") as recreate:
                with mock.patch.object(cli.time, "sleep", fake_sleep):
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(KeyboardInterrupt):
                            cli.cmd_watch(argparse.Namespace(once=False, interval=10.0))
        recreate.assert_called_once()


class RecreateFailureTests(unittest.TestCase):
    def test_recreate_failure_is_swallowed_so_the_daemon_lives(self):
        with mock.patch.object(cli, "_recreate", side_effect=AliyunError("NoCapacity", "no stock")):
            with contextlib.redirect_stderr(io.StringIO()):
                cli._recreate_safely({}, argparse.Namespace(interval=60.0, once=True))

    def test_recreate_failure_does_not_swallow_unexpected_errors(self):
        with mock.patch.object(cli, "_recreate", side_effect=ValueError("boom")):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(ValueError):
                    cli._recreate_safely({}, argparse.Namespace(interval=60.0, once=True))


if __name__ == "__main__":
    unittest.main()
