"""Locks down the exact request yi sends to Alibaba Cloud.

This is the part of the project that costs money if it is wrong, so the request
shape is asserted offline: spot strategy, the VSwitchId requirement, the security
group rules and the cloud-init payload.
"""

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import sandbox  # noqa: E402,F401  (隔离 YI_HOME，必须在导入 yi 之前)

from yi import state  # noqa: E402
from yi.aliyun import AliyunError, Credentials, EcsClient  # noqa: E402
from yi.bootstrap import render_user_data  # noqa: E402

ALIYUN_USERDATA_LIMIT = 16 * 1024


class CapturingEcs(EcsClient):
    def __init__(self, responses=None):
        super().__init__(Credentials("LTAITEST", "secret", "test"), "cn-hongkong")
        self.sent = []
        self.responses = responses or {}

    def call(self, action, params=None, **kwargs):
        merged = dict(params or {})
        merged.update(kwargs)
        self.sent.append((action, merged))
        response = self.responses.get(action, {})
        # lets a test say "this action fails" without writing a bespoke fake
        if isinstance(response, BaseException):
            raise response
        return response

    def sent_of(self, action):
        return [params for name, params in self.sent if name == action]


def create_kwargs(**overrides):
    base = dict(
        image_id="ubuntu_22_04_x64_20G_alibase.vhd",
        instance_type="ecs.e-c1m1.large",
        zone_id="cn-hongkong-b",
        vswitch_id="vsw-123",
        security_group_id="sg-123",
        key_pair_name="yi",
        user_data=render_user_data(state.load_config()),
        instance_name="yi-hk",
        bandwidth_out=100,
        disk_category="cloud_essd",
        disk_size=40,
        spot_strategy="SpotAsPriceGo",
    )
    base.update(overrides)
    return base


class CreateInstanceRequestTests(unittest.TestCase):
    def test_spot_postpaid_pay_by_traffic(self):
        ecs = CapturingEcs({"CreateInstance": {"InstanceId": "i-test"}})
        instance_id = ecs.create_spot_instance(**create_kwargs())
        self.assertEqual(instance_id, "i-test")

        params = ecs.sent_of("CreateInstance")[0]
        self.assertEqual(params["SpotStrategy"], "SpotAsPriceGo")
        self.assertEqual(params["InstanceChargeType"], "PostPaid")
        self.assertEqual(params["InternetChargeType"], "PayByTraffic")
        self.assertEqual(params["InternetMaxBandwidthOut"], 100)
        self.assertEqual(params["SystemDisk.Category"], "cloud_essd")
        self.assertEqual(params["SystemDisk.Size"], 40)
        self.assertEqual(params["SystemDisk.DeleteWithInstance"], "true")
        # CreateInstance rejects InternetMaxBandwidthIn=-1 (that is a RunInstances
        # spelling) and DeletionProtection would fight our own `down`, so neither
        # may ever be sent.
        self.assertNotIn("InternetMaxBandwidthIn", params)
        self.assertNotIn("DeletionProtection", params)
        self.assertEqual(params["VSwitchId"], "vsw-123")
        self.assertEqual(params["ZoneId"], "cn-hongkong-b")
        self.assertEqual(params["Amount"], 1)
        # spot instances must never be created through RunInstances
        self.assertNotIn("RunInstances", [action for action, _ in ecs.sent])

    def test_inbound_bandwidth_only_sent_when_explicitly_configured(self):
        ecs = CapturingEcs({"CreateInstance": {"InstanceId": "i-test"}})
        ecs.create_spot_instance(**create_kwargs(bandwidth_in=50))
        self.assertEqual(ecs.sent_of("CreateInstance")[0]["InternetMaxBandwidthIn"], 50)

    def test_hostname_is_sanitised_for_the_api_rules(self):
        ecs = CapturingEcs({"CreateInstance": {"InstanceId": "i-test"}})
        ecs.create_spot_instance(**create_kwargs(instance_name="_bad name_"))
        params = ecs.sent_of("CreateInstance")[0]
        self.assertEqual(params["InstanceName"], "_bad name_")
        self.assertEqual(params["HostName"], "bad-name")
        self.assertTrue(all(ch.isalnum() or ch == "-" for ch in params["HostName"]))

    def test_userdata_is_base64_cloud_init_within_limit(self):
        ecs = CapturingEcs({"CreateInstance": {"InstanceId": "i-test"}})
        ecs.create_spot_instance(**create_kwargs())
        encoded = ecs.sent_of("CreateInstance")[0]["UserData"]
        script = base64.b64decode(encoded).decode("utf-8")
        self.assertTrue(script.startswith("#!/bin/bash"))
        self.assertIn("xray", script)
        self.assertLess(len(script.encode("utf-8")), ALIYUN_USERDATA_LIMIT)

    def test_price_limit_switches_strategy_and_sets_duration(self):
        ecs = CapturingEcs({"CreateInstance": {"InstanceId": "i-test"}})
        ecs.create_spot_instance(
            **create_kwargs(spot_strategy="SpotWithPriceLimit", spot_price_limit=0.2, spot_duration=1)
        )
        params = ecs.sent_of("CreateInstance")[0]
        self.assertEqual(params["SpotStrategy"], "SpotWithPriceLimit")
        self.assertEqual(params["SpotPriceLimit"], 0.2)
        self.assertEqual(params["SpotDuration"], 1)

    def test_no_protection_period_without_price_limit(self):
        """SpotDuration is invalid under SpotAsPriceGo, so it must be dropped."""
        ecs = CapturingEcs({"CreateInstance": {"InstanceId": "i-test"}})
        ecs.create_spot_instance(**create_kwargs(spot_strategy="SpotAsPriceGo", spot_duration=1))
        self.assertNotIn("SpotDuration", ecs.sent_of("CreateInstance")[0])

    def test_price_limit_strategy_requires_a_price(self):
        ecs = CapturingEcs()
        with self.assertRaises(AliyunError):
            ecs.create_spot_instance(**create_kwargs(spot_strategy="SpotWithPriceLimit", spot_price_limit=0))

    def test_nospot_omits_every_spot_parameter(self):
        ecs = CapturingEcs({"CreateInstance": {"InstanceId": "i-test"}})
        ecs.create_spot_instance(**create_kwargs(spot_strategy="NoSpot", spot_duration=1))
        params = ecs.sent_of("CreateInstance")[0]
        self.assertNotIn("SpotStrategy", params)
        self.assertNotIn("SpotDuration", params)
        self.assertNotIn("SpotPriceLimit", params)


class DeleteRetryTests(unittest.TestCase):
    def test_delete_retries_while_initializing(self):
        """A rollback that gives up during Initializing leaves a billing instance."""
        ecs = CapturingEcs(
            {"DeleteInstance": AliyunError("IncorrectInstanceStatus.Initializing", "still starting")}
        )
        self.assertFalse(ecs.delete_instance_when_ready("i-test", timeout=0.6, interval=0.2))

    def test_delete_stops_retrying_on_a_real_error(self):
        # The dangerous case: DeleteInstance says NotFound, but the instance is still
        # listed and therefore still billing. Never report success here.
        ecs = CapturingEcs(
            {
                "DeleteInstance": AliyunError("InvalidInstanceId.NotFound", "gone"),
                "DescribeInstances": {
                    "Instances": {"Instance": [{"InstanceId": "i-test", "Status": "Running"}]}
                },
            }
        )
        self.assertFalse(ecs.delete_instance_when_ready("i-test", timeout=0.8, interval=0.1))

    def test_three_consecutive_misses_mean_it_is_really_gone(self):
        ecs = CapturingEcs(
            {
                "DeleteInstance": AliyunError("InvalidInstanceId.NotFound", "gone"),
                "DescribeInstances": {"Instances": {"Instance": []}},
            }
        )
        self.assertTrue(ecs.delete_instance_when_ready("i-test", timeout=5, interval=0.05))
        self.assertGreaterEqual(len(ecs.sent_of("DescribeInstances")), 3)

    def test_delete_succeeds_first_try(self):
        ecs = CapturingEcs({"DeleteInstance": {}})
        self.assertTrue(ecs.delete_instance_when_ready("i-test", timeout=5, interval=0.1))
        self.assertEqual(len(ecs.sent_of("DeleteInstance")), 1)

    def test_not_found_keeps_retrying_while_the_instance_is_still_listed(self):
        """Observed on a live account: DeleteInstance answered NotFound while a plain
        DescribeInstances still returned the instance. Retrying is the right move."""
        ecs = CapturingEcs(
            {
                "DeleteInstance": AliyunError("InvalidInstanceId.NotFound", "not here"),
                "DescribeInstances": {
                    "Instances": {"Instance": [{"InstanceId": "i-test", "Status": "Running"}]}
                },
            }
        )
        self.assertFalse(ecs.delete_instance_when_ready("i-test", timeout=0.8, interval=0.2))
        self.assertGreaterEqual(len(ecs.sent_of("DeleteInstance")), 2)


class DescribeFallbackTests(unittest.TestCase):
    def test_ensure_running_starts_an_instance_that_came_up_stopped(self):
        """Measured on this account: freshly created instances (spot and pay-as-you-go)
        can sit in Stopped and never recover until StartInstance is called."""

        class FakeEcs(CapturingEcs):
            def __init__(self):
                super().__init__()
                self.statuses = ["Pending", "Stopped", "Stopped", "Running"]

            def instance_status(self, instance_id):
                return self.statuses.pop(0) if self.statuses else "Running"

            def instance_details(self, instance_id):
                return {"status": "Stopped", "stopped_mode": "KeepCharging", "lock_reasons": []}

        ecs = FakeEcs()
        ecs.ensure_running("i-test", timeout=5, interval=0.01)
        self.assertEqual(len(ecs.sent_of("StartInstance")), 1)

    def test_ensure_running_does_not_start_a_running_instance(self):
        class FakeEcs(CapturingEcs):
            def instance_status(self, instance_id):
                return "Running"

        ecs = FakeEcs()
        ecs.ensure_running("i-test", timeout=1, interval=0.01)
        self.assertEqual(ecs.sent_of("StartInstance"), [])

    def test_ensure_public_ip_allocates_when_missing(self):
        class FakeEcs(CapturingEcs):
            def __init__(self):
                super().__init__({"AllocatePublicIpAddress": {"IpAddress": "47.52.1.9"}})
                self.calls = 0

            def public_ip(self, instance_id):
                self.calls += 1
                return None if self.calls == 1 else "47.52.1.9"

        ecs = FakeEcs()
        self.assertEqual(ecs.ensure_public_ip("i-test", timeout=5, interval=0.01), "47.52.1.9")
        self.assertEqual(len(ecs.sent_of("AllocatePublicIpAddress")), 1)

    def test_ensure_public_ip_returns_existing_ip_without_allocating(self):
        class FakeEcs(CapturingEcs):
            def public_ip(self, instance_id):
                return "47.52.1.2"

        ecs = FakeEcs()
        self.assertEqual(ecs.ensure_public_ip("i-test"), "47.52.1.2")
        self.assertEqual(ecs.sent_of("AllocatePublicIpAddress"), [])

    def test_filtered_query_falls_back_to_listing(self):
        ecs = CapturingEcs(
            {
                "DescribeInstances": {
                    "Instances": {"Instance": [{"InstanceId": "i-other"}, {"InstanceId": "i-want"}]}
                }
            }
        )
        # the fake always returns the same body, so the first (filtered) call would
        # wrongly report i-other — assert the helper only trusts an exact ID match
        self.assertEqual(ecs.find_in_listing("i-want")["InstanceId"], "i-want")
        self.assertIsNone(ecs.find_in_listing("i-missing"))


class SecurityGroupRequestTests(unittest.TestCase):
    def test_rules_match_the_client_requirements(self):
        ecs = CapturingEcs()
        ecs.authorize_egress_all("sg-123")
        ecs.authorize("sg-123", "tcp", "443/443", "0.0.0.0/0", "VLESS/REALITY")
        ecs.authorize("sg-123", "tcp", "22/22", "203.0.113.7/32", "SSH from admin")

        egress = ecs.sent_of("AuthorizeSecurityGroupEgress")[0]
        self.assertEqual(egress["DestCidrIp"], "0.0.0.0/0")
        self.assertEqual(egress["IpProtocol"], "all")

        rules = ecs.sent_of("AuthorizeSecurityGroup")
        self.assertEqual(rules[0]["PortRange"], "443/443")
        self.assertEqual(rules[0]["SourceCidrIp"], "0.0.0.0/0")
        self.assertEqual(rules[1]["PortRange"], "22/22")
        self.assertEqual(rules[1]["SourceCidrIp"], "203.0.113.7/32")


class FindImageTests(unittest.TestCase):
    def test_uses_prefix_query_and_picks_newest(self):
        responses = {
            "DescribeImages": {
                "Images": {
                    "Image": [
                        {
                            "ImageName": "ubuntu_22_04_x64_20G_alibase_20230101.vhd",
                            "ImageId": "m-old",
                            "Architecture": "x86_64",
                            "CreationTime": "2023-01-01T00:00Z",
                        },
                        {
                            "ImageName": "ubuntu_22_04_x64_20G_alibase_20240819.vhd",
                            "ImageId": "m-new",
                            "Architecture": "x86_64",
                            "CreationTime": "2024-08-19T00:00Z",
                        },
                        {
                            "ImageName": "ubuntu_22_04_x64_20G_alibase_20250101.vhd",
                            "ImageId": "m-arm",
                            "Architecture": "arm64",
                            "CreationTime": "2025-01-01T00:00Z",
                        },
                    ]
                }
            }
        }
        ecs = CapturingEcs(responses)
        self.assertEqual(ecs.find_image(["ubuntu_22_04_x64"]), "m-new")
        sent = ecs.sent_of("DescribeImages")[0]
        self.assertEqual(sent["ImageName"], "ubuntu_22_04_x64*")
        self.assertEqual(sent["ImageOwnerAlias"], "system")
        self.assertEqual(sent["OSType"], "linux")

    def test_falls_back_to_the_next_filter(self):
        responses = {"DescribeImages": {"Images": {"Image": []}}}
        ecs = CapturingEcs(responses)
        with self.assertRaises(AliyunError) as ctx:
            ecs.find_image(["nope_1", "nope_2"])
        self.assertEqual(ctx.exception.code, "ImageNotFound")
        self.assertEqual(
            [item["ImageName"] for item in ecs.sent_of("DescribeImages")],
            ["nope_1*", "nope_2*"],
        )


if __name__ == "__main__":
    unittest.main()
