"""Reusing pre-existing vSwitch / security group instead of creating our own.

The user supplies a vSwitch and a security group that already exist in
cn-hongkong. Two properties matter and are asserted here:

1. we never create a second security group, and
2. we never delete a security group we did not create.
"""

import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from test_ecs_request import CapturingEcs  # noqa: E402

from yi import cli, state  # noqa: E402
from yi.aliyun import AliyunError  # noqa: E402

# 形如真实 ID 的假值：只为覆盖"用户自带资源"这条代码路径，不代表任何真实资产。
VSWITCH = "vsw-00000000000000000000"
GROUP = "sg-00000000000000000000"


def vswitch_response(zone="cn-hongkong-b", vpc="vpc-j6c0001"):
    return {
        "VSwitches": {
            "VSwitch": [
                {
                    "VSwitchId": VSWITCH,
                    "VpcId": vpc,
                    "ZoneId": zone,
                    "CidrBlock": "172.16.0.0/24",
                }
            ]
        }
    }


def group_response(vpc="vpc-j6c0001", name="my-existing-sg"):
    return {
        "SecurityGroups": {
            "SecurityGroup": [{"SecurityGroupId": GROUP, "VpcId": vpc, "SecurityGroupName": name}]
        }
    }


def up_args(**overrides):
    base = dict(
        zone=None,
        instance_type=None,
        price_limit=None,
        ssh_from=None,
        dry_run=False,
        force_recreate=False,
        no_rollback=False,
        timeout=60.0,
        no_qr=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class DirectionAwareEcs(CapturingEcs):
    """The real API filters rules by Direction; the fake has to as well."""

    def __init__(self, ingress, egress, responses=None):
        super().__init__(responses or {})
        self._ingress = ingress
        self._egress = egress

    def call(self, action, params=None, **kwargs):
        if action == "DescribeSecurityGroupAttribute":
            merged = dict(params or {})
            merged.update(kwargs)
            self.sent.append((action, merged))
            rules = self._egress if merged.get("Direction") == "egress" else self._ingress
            return {"Permissions": {"Permission": rules}}
        return super().call(action, params, **kwargs)


class ZoneCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(state.load_config(), vswitch_id=VSWITCH, security_group_id=GROUP)
        self.plan = cli._plan(self.config, up_args())

    def test_pinned_vswitch_pins_the_zone(self):
        ecs = CapturingEcs({"DescribeVSwitches": vswitch_response(zone="cn-hongkong-c")})
        candidates = cli._zone_candidates(ecs, self.config, self.plan)
        self.assertEqual(len(candidates), 1)
        zone, network = candidates[0]
        self.assertEqual(zone, "cn-hongkong-c")
        self.assertEqual(network["vswitch_id"], VSWITCH)
        self.assertEqual(ecs.sent_of("DescribeVpcs"), [])
        self.assertEqual(ecs.sent_of("DescribeVSwitches")[0]["VSwitchId"], VSWITCH)

    def test_conflicting_zone_is_rejected_loudly(self):
        ecs = CapturingEcs({"DescribeVSwitches": vswitch_response(zone="cn-hongkong-b")})
        config = dict(self.config)
        plan = cli._plan(config, up_args(zone="cn-hongkong-d"))
        with self.assertRaises(AliyunError) as ctx:
            cli._zone_candidates(ecs, config, plan)
        self.assertEqual(ctx.exception.code, "ZoneVSwitchMismatch")

    def test_without_a_pinned_vswitch_it_discovers_per_zone(self):
        ecs = CapturingEcs(
            {
                "DescribeVpcs": {"Vpcs": {"Vpc": [{"VpcId": "vpc-default"}]}},
                "DescribeVSwitches": {"VSwitches": {"VSwitch": [{"VSwitchId": "vsw-auto"}]}},
            }
        )
        config = dict(self.config, vswitch_id="")
        plan = cli._plan(config, up_args())
        candidates = cli._zone_candidates(ecs, config, plan)
        self.assertEqual([zone for zone, _ in candidates], plan["zones"])
        self.assertEqual(candidates[0][1]["vswitch_id"], "vsw-auto")


class SecurityGroupReuseTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(state.load_config(), security_group_id=GROUP)

    def test_borrowed_group_is_never_created_or_owned(self):
        ecs = CapturingEcs(
            {
                "DescribeSecurityGroups": group_response(),
                "DescribeSecurityGroupAttribute": {"Permissions": {"Permission": []}},
            }
        )
        group_id, owned = cli._resolve_security_group(
            ecs, self.config, "vpc-j6c0001", cli._plan(self.config, up_args()), "203.0.113.7/32"
        )
        self.assertEqual(group_id, GROUP)
        self.assertFalse(owned)
        self.assertEqual(ecs.sent_of("CreateSecurityGroup"), [])

        rules = ecs.sent_of("AuthorizeSecurityGroup")
        self.assertEqual(
            [(rule["PortRange"], rule["SourceCidrIp"]) for rule in rules],
            [("443/443", "0.0.0.0/0"), ("22/22", "203.0.113.7/32")],
        )
        self.assertEqual(len(ecs.sent_of("AuthorizeSecurityGroupEgress")), 1)

    def test_existing_rules_are_not_duplicated(self):
        existing = [
            {"IpProtocol": "tcp", "PortRange": "443/443", "SourceCidrIp": "0.0.0.0/0"},
            {"IpProtocol": "tcp", "PortRange": "22/22", "SourceCidrIp": "203.0.113.7/32"},
        ]
        ecs = DirectionAwareEcs(
            ingress=existing,
            egress=[{"IpProtocol": "all", "DestCidrIp": "0.0.0.0/0"}],
            responses={"DescribeSecurityGroups": group_response()},
        )
        cli._resolve_security_group(
            ecs, self.config, "vpc-j6c0001", cli._plan(self.config, up_args()), "203.0.113.7/32"
        )
        self.assertEqual(ecs.sent_of("AuthorizeSecurityGroup"), [])
        self.assertEqual(ecs.sent_of("AuthorizeSecurityGroupEgress"), [])

    def test_vpc_mismatch_is_rejected(self):
        ecs = CapturingEcs({"DescribeSecurityGroups": group_response(vpc="vpc-other")})
        with self.assertRaises(AliyunError) as ctx:
            cli._resolve_security_group(
                ecs, self.config, "vpc-j6c0001", cli._plan(self.config, up_args()), "203.0.113.7/32"
            )
        self.assertEqual(ctx.exception.code, "SecurityGroupVpcMismatch")

    def test_without_config_it_creates_and_owns_a_group(self):
        ecs = CapturingEcs({"CreateSecurityGroup": {"SecurityGroupId": "sg-new"}})
        config = dict(self.config, security_group_id="")
        group_id, owned = cli._resolve_security_group(
            ecs, config, "vpc-j6c0001", cli._plan(config, up_args()), "203.0.113.7/32"
        )
        self.assertEqual(group_id, "sg-new")
        self.assertTrue(owned)
        self.assertEqual(len(ecs.sent_of("AuthorizeSecurityGroup")), 2)


class DownKeepsBorrowedResourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = os.environ.get("YI_HOME")
        os.environ["YI_HOME"] = self.tmp.name
        state.ensure_home()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("YI_HOME", None)
        else:
            os.environ["YI_HOME"] = self._saved
        self.tmp.cleanup()

    def test_down_deletes_instance_but_not_the_borrowed_group(self):
        state.save_state(
            {
                "instance_id": "i-borrowed",
                "security_group_id": GROUP,
                "security_group_owned": False,
                "public_ip": "47.52.1.2",
            }
        )
        ecs = CapturingEcs()
        with mock.patch.object(cli, "_client", return_value=ecs):
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.cmd_down(argparse.Namespace(yes=True, orphans=False, keep_security_group=False))
        self.assertEqual(code, 0)
        self.assertEqual(len(ecs.sent_of("DeleteInstance")), 1)
        self.assertEqual(ecs.sent_of("DeleteSecurityGroup"), [])
        self.assertIsNone(state.load_state())

    def test_down_deletes_a_group_we_created(self):
        state.save_state(
            {
                "instance_id": "i-ours",
                "security_group_id": "sg-ours",
                "security_group_owned": True,
                "public_ip": "47.52.1.3",
            }
        )
        ecs = CapturingEcs()
        with mock.patch.object(cli, "_client", return_value=ecs):
            with contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_down(argparse.Namespace(yes=True, orphans=False, keep_security_group=False))
        self.assertEqual(ecs.sent_of("DeleteSecurityGroup")[0]["SecurityGroupId"], "sg-ours")


if __name__ == "__main__":
    unittest.main()
