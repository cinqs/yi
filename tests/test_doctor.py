"""The --api preflight must name the exact step that would break `up`."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import sandbox  # noqa: E402,F401  (隔离 YI_HOME，必须在导入 yi 之前)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_ecs_request import CapturingEcs  # noqa: E402

from yi import cli, state  # noqa: E402
from yi.aliyun import AliyunError, Credentials  # noqa: E402

# 形如真实 ID 的假值：只为覆盖"用户自带资源"这条代码路径，不代表任何真实资产。
VSWITCH = "vsw-00000000000000000000"
GROUP = "sg-00000000000000000000"


def healthy_responses(zone="cn-hongkong-b"):
    return {
        "DescribeRegions": {"Regions": {"Region": [{"RegionId": "cn-hongkong"}]}},
        "DescribeZones": {"Zones": {"Zone": [{"ZoneId": zone}, {"ZoneId": "cn-hongkong-c"}]}},
        "DescribeVSwitches": {
            "VSwitches": {
                "VSwitch": [
                    {"VSwitchId": VSWITCH, "VpcId": "vpc-1", "ZoneId": zone, "CidrBlock": "172.16.0.0/24"}
                ]
            }
        },
        "DescribeSecurityGroups": {
            "SecurityGroups": {
                "SecurityGroup": [{"SecurityGroupId": GROUP, "VpcId": "vpc-1", "SecurityGroupName": "mine"}]
            }
        },
        "DescribeSecurityGroupAttribute": {
            "Permissions": {
                "Permission": [{"IpProtocol": "tcp", "PortRange": "22/22", "SourceCidrIp": "203.0.113.7/32"}]
            }
        },
        "DescribeImages": {
            "Images": {
                "Image": [
                    {
                        "ImageName": "ubuntu_22_04_x64_20G_alibase_20240819.vhd",
                        "ImageId": "m-1",
                        "Architecture": "x86_64",
                        "CreationTime": "2024-08-19T00:00Z",
                    }
                ]
            }
        },
        "DescribeKeyPairs": {"KeyPairs": {"KeyPair": []}},
        "DescribeSpotPriceHistory": {"SpotPrices": {"SpotPriceType": [{"SpotPrice": "0.09"}]}},
    }


def findings_map(findings):
    return {item["check"]: item for item in findings}


class ApiPreflightTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(state.load_config(), vswitch_id=VSWITCH, security_group_id=GROUP)

    def test_healthy_account_passes_every_step(self):
        ecs = CapturingEcs(healthy_responses())
        with mock.patch.object(cli, "EcsClient", return_value=ecs):
            findings = cli._api_preflight(Credentials("LTAI", "s", "test"), self.config)
        failed = [item for item in findings if not item["ok"]]
        self.assertEqual(failed, [], f"unexpected failures: {failed}")
        checks = findings_map(findings)
        self.assertIn("cn-hongkong-b", checks["vswitch-zone"]["detail"])
        self.assertIn("443 已放行: 否", checks["sg-ingress-rules"]["detail"])
        self.assertTrue(checks["ecs:DescribeSpotPriceHistory"]["ok"])
        self.assertIn("0.135", checks["ecs:DescribeSpotPriceHistory"]["detail"])
        self.assertIn("×1.5", checks["ecs:DescribeSpotPriceHistory"]["detail"])

    def test_zone_conflicting_with_config_is_reported(self):
        # vSwitch lives in -c but the user pinned zones=[-b]: up would refuse, so say so first
        self.config["zones"] = ["cn-hongkong-b"]
        responses = healthy_responses(zone="cn-hongkong-c")
        ecs = CapturingEcs(responses)
        with mock.patch.object(cli, "EcsClient", return_value=ecs):
            findings = cli._api_preflight(Credentials("LTAI", "s", "test"), self.config)
        checks = findings_map(findings)
        self.assertFalse(checks["vswitch-zone-config"]["ok"])
        self.assertIn("cn-hongkong-c", checks["vswitch-zone-config"]["detail"])

    def test_zone_outside_the_region_is_reported(self):
        responses = healthy_responses(zone="cn-hongkong-z")
        # the region only really has -b/-c; a vSwitch claiming anything else is bogus
        responses["DescribeZones"] = {
            "Zones": {"Zone": [{"ZoneId": "cn-hongkong-b"}, {"ZoneId": "cn-hongkong-c"}]}
        }
        ecs = CapturingEcs(responses)
        with mock.patch.object(cli, "EcsClient", return_value=ecs):
            findings = cli._api_preflight(Credentials("LTAI", "s", "test"), self.config)
        checks = findings_map(findings)
        self.assertFalse(checks["vswitch-zone"]["ok"])

    def test_missing_vswitch_points_at_the_id(self):
        ecs = CapturingEcs(
            {
                "DescribeRegions": {"Regions": {"Region": [{"RegionId": "cn-hongkong"}]}},
                "DescribeZones": {"Zones": {"Zone": [{"ZoneId": "cn-hongkong-b"}]}},
                "DescribeVSwitches": {"VSwitches": {"VSwitch": []}},
            }
        )
        with mock.patch.object(cli, "EcsClient", return_value=ecs):
            findings = cli._api_preflight(Credentials("LTAI", "s", "test"), self.config)
        checks = findings_map(findings)
        self.assertFalse(checks["ecs:DescribeVSwitches"]["ok"])
        self.assertIn(VSWITCH, checks["ecs:DescribeVSwitches"]["detail"])

    def test_missing_ram_action_is_named(self):
        responses = healthy_responses()
        responses["DescribeSecurityGroupAttribute"] = AliyunError(
            "Forbidden.RAM", "not authorized", action="DescribeSecurityGroupAttribute"
        )
        ecs = CapturingEcs(responses)
        with mock.patch.object(cli, "EcsClient", return_value=ecs):
            findings = cli._api_preflight(Credentials("LTAI", "s", "test"), self.config)
        checks = findings_map(findings)
        self.assertFalse(checks["ecs:DescribeSecurityGroupAttribute"]["ok"])
        self.assertIn("RAM", checks["ecs:DescribeSecurityGroupAttribute"]["detail"])


if __name__ == "__main__":
    unittest.main()
