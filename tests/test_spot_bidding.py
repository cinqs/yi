"""Bidding strategy: buy the protection period whenever the price is knowable.

The live failure this guards against: the first real instance came up and was
`Stopped` six seconds later because the config asked for `spot_duration = 1`
while the code silently used `SpotAsPriceGo`, which cannot carry a protection
period at all.
"""

import argparse
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import sandbox  # noqa: E402,F401  (隔离 YI_HOME，必须在导入 yi 之前)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_ecs_request import CapturingEcs  # noqa: E402

from yi import cli, state  # noqa: E402
from yi.aliyun import AliyunError, Credentials, EcsClient  # noqa: E402


def up_args(**overrides):
    base = dict(zone=None, instance_type=None, price_limit=None)
    base.update(overrides)
    return argparse.Namespace(**base)


class PlanDefaultsTests(unittest.TestCase):
    def test_default_config_buys_a_protection_period(self):
        config = state.load_config()
        self.assertEqual(config["spot_strategy"], "SpotWithPriceLimit")
        self.assertGreater(config["spot_duration"], 0)

    def test_asking_for_a_duration_implies_the_limit_strategy(self):
        """duration is only purchasable under SpotWithPriceLimit, so never ignore it."""
        config = dict(state.load_config(), spot_strategy="SpotAsPriceGo", spot_duration=1)
        plan = cli._plan(config, up_args())
        self.assertEqual(plan["spot_strategy"], "SpotWithPriceLimit")
        self.assertIn("保护期", plan["spot_summary"])

    def test_price_limit_flag_wins(self):
        plan = cli._plan(state.load_config(), up_args(price_limit=0.25))
        self.assertEqual(plan["spot_price_limit"], 0.25)
        self.assertIn("0.250", plan["spot_summary"])

    def test_summary_warns_when_there_is_no_protection(self):
        config = dict(state.load_config(), spot_duration=0, spot_strategy="SpotAsPriceGo")
        summary = cli._plan(config, up_args())["spot_summary"]
        self.assertIn("没有保护期", summary)


class ResolveBidTests(unittest.TestCase):
    def test_explicit_limit_is_used_as_is(self):
        ecs = CapturingEcs()
        self.assertEqual(
            cli._resolve_bid(ecs, "SpotWithPriceLimit", 0.3, "t", "z"), ("SpotWithPriceLimit", 0.3)
        )
        self.assertEqual(ecs.sent, [])

    def test_market_price_becomes_a_bid_with_headroom(self):
        ecs = CapturingEcs(
            {
                "DescribeSpotPriceHistory": {
                    "SpotPrices": {
                        "SpotPriceType": [
                            {"SpotPrice": "0.10", "Timestamp": "2026-09-19T00:00Z"},
                            {"SpotPrice": "0.12", "Timestamp": "2026-09-19T01:00Z"},
                        ]
                    }
                }
            }
        )
        strategy, bid = cli._resolve_bid(ecs, "SpotWithPriceLimit", 0, "ecs.e-c1m1.large", "cn-hongkong-b")
        self.assertEqual(strategy, "SpotWithPriceLimit")
        self.assertAlmostEqual(bid, round(0.12 * 1.5, 3))
        sent = ecs.sent_of("DescribeSpotPriceHistory")[0]
        self.assertEqual(sent["ZoneId"], "cn-hongkong-b")
        self.assertEqual(sent["InstanceType"], "ecs.e-c1m1.large")

    def test_multiplier_is_configurable(self):
        ecs = CapturingEcs(
            {"DescribeSpotPriceHistory": {"SpotPrices": {"SpotPriceType": [{"SpotPrice": "0.02"}]}}}
        )
        _, bid = cli._resolve_bid(ecs, "SpotWithPriceLimit", 0, "t", "z", multiplier=3.0)
        self.assertAlmostEqual(bid, 0.06)

    def test_unavailable_price_degrades_loudly(self):
        ecs = CapturingEcs(
            {
                "DescribeSpotPriceHistory": AliyunError(
                    "Forbidden.RAM", "no", action="DescribeSpotPriceHistory"
                )
            }
        )
        strategy, bid = cli._resolve_bid(ecs, "SpotWithPriceLimit", 0, "t", "z")
        self.assertEqual(strategy, "SpotAsPriceGo")
        self.assertEqual(bid, 0.0)

    def test_empty_price_history_degrades(self):
        ecs = CapturingEcs({"DescribeSpotPriceHistory": {"SpotPrices": {"SpotPriceType": []}}})
        self.assertEqual(cli._resolve_bid(ecs, "SpotWithPriceLimit", 0, "t", "z")[0], "SpotAsPriceGo")


class PriceParsingTests(unittest.TestCase):
    def test_peak_of_recent_history_is_used(self):
        ecs = CapturingEcs(
            {
                "DescribeSpotPriceHistory": {
                    "SpotPrices": {
                        "SpotPriceType": [
                            {"SpotPrice": "0.05"},
                            {"SpotPrice": "0.19"},
                            {"SpotPrice": "not-a-number"},
                        ]
                    }
                }
            }
        )
        self.assertAlmostEqual(ecs.spot_price("ecs.e-c1m1.large", "cn-hongkong-b"), 0.19)

    def test_single_point_response_is_handled(self):
        """Aliyun returns a dict instead of a list when there is exactly one point."""
        ecs = CapturingEcs(
            {"DescribeSpotPriceHistory": {"SpotPrices": {"SpotPriceType": {"SpotPrice": "0.08"}}}}
        )
        self.assertAlmostEqual(ecs.spot_price("t", "z"), 0.08)

    def test_api_failure_returns_none(self):
        ecs = CapturingEcs({"DescribeSpotPriceHistory": AliyunError("Throttling.User", "slow")})
        self.assertIsNone(ecs.spot_price("t", "z"))


class EarlyStopDetectionTests(unittest.TestCase):
    class FakeEcs(EcsClient):
        def __init__(self, statuses, details=None):
            super().__init__(Credentials("LTAI", "s", "test"), "cn-hongkong")
            self.statuses = list(statuses)
            self.details = details or {}

        def instance_status(self, instance_id):
            return self.statuses.pop(0) if self.statuses else "Stopped"

        def instance_details(self, instance_id):
            return self.details

        def describe_instance(self, instance_id):
            return {"Status": "Running"}

    def test_stopped_immediately_after_creation_fails_fast(self):
        ecs = self.FakeEcs(
            ["Pending", "Stopped"],
            details={"status": "Stopped", "stopped_mode": "StopCharging", "lock_reasons": []},
        )
        with self.assertRaises(AliyunError) as ctx:
            ecs.wait_running("i-test", timeout=1, interval=0)
        self.assertEqual(ctx.exception.code, "InstanceStoppedEarly")
        self.assertIn("StopCharging", str(ctx.exception))
        self.assertIn("抢占", ctx.exception.hint())

    def test_running_is_returned_normally(self):
        ecs = self.FakeEcs(["Pending", "Running"])
        self.assertEqual(ecs.wait_running("i-test", timeout=5, interval=0)["Status"], "Running")


if __name__ == "__main__":
    unittest.main()
