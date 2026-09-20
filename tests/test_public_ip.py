"""Public-IP detection: the first provider a mainland connection cannot reach
must never be the one that decides the outcome.

api.ipify.org was measured unreachable (HTTP 000) from the user's Mac while
myip.ipip.net / 3322 / ifconfig.me all answered, so the ordering and the parsing
of non-plain-text responses are both part of the contract.
"""

import io
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from yi import cli  # noqa: E402


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def responder(mapping, failures=()):
    """urlopen stand-in: `failures` are URLs that raise, mapping gives bodies."""

    def fake(request, timeout=None):
        url = getattr(request, "full_url", request)
        if url in failures:
            raise urllib.error.URLError("blocked")
        if url in mapping:
            return Response(mapping[url].encode("utf-8"))
        raise urllib.error.URLError("no route")

    return fake


class ExtractTests(unittest.TestCase):
    def test_plain_body(self):
        self.assertEqual(cli._extract_ipv4("47.52.1.2\n"), "47.52.1.2")

    def test_chinese_provider_wording(self):
        body = "当前 IP：47.52.1.2  来自于：中国 广东 电信\n"
        self.assertEqual(cli._extract_ipv4(body), "47.52.1.2")

    def test_dns_txt_quotes(self):
        self.assertEqual(cli._extract_ipv4('"47.52.1.2"\n'), "47.52.1.2")

    def test_private_and_garbage_rejected(self):
        self.assertIsNone(cli._extract_ipv4("192.168.1.1"))
        self.assertIsNone(cli._extract_ipv4("10.0.0.5"))
        self.assertIsNone(cli._extract_ipv4("no ip here"))
        self.assertIsNone(cli._extract_ipv4("999.1.1.1"))

    def test_ipv6_is_rejected_because_sourcecidrip_is_ipv4_only(self):
        self.assertIsNone(cli._extract_ipv4("240e:3b3:1234:5678::1"))
        self.assertIsNone(cli._extract_ipv4("240e:3b3:1234::1/64"))


class DetectionTests(unittest.TestCase):
    def test_falls_through_blocked_provider(self):
        mapping = {
            cli.IP_ECHO_URLS[0]: "当前 IP：47.52.1.2  来自于：中国 广东 电信",
        }
        with mock.patch.object(cli.urllib.request, "urlopen", responder(mapping)):
            self.assertEqual(cli._my_public_ip(), "47.52.1.2")

    def test_skips_the_blocked_one_and_still_succeeds(self):
        blocked = cli.IP_ECHO_URLS[0]
        mapping = {cli.IP_ECHO_URLS[1]: "47.52.1.9"}
        with mock.patch.object(cli.urllib.request, "urlopen", responder(mapping, failures={blocked})):
            self.assertEqual(cli._my_public_ip(), "47.52.1.9")

    def test_unparseable_response_moves_on(self):
        mapping = {
            cli.IP_ECHO_URLS[0]: "<html>error</html>",
            cli.IP_ECHO_URLS[1]: "47.52.1.11",
        }
        with mock.patch.object(cli.urllib.request, "urlopen", responder(mapping)):
            self.assertEqual(cli._my_public_ip(), "47.52.1.11")

    def test_everything_failing_falls_back_to_dns_then_returns_none(self):
        with mock.patch.object(cli.urllib.request, "urlopen", responder({}, failures=set(cli.IP_ECHO_URLS))):
            with mock.patch.object(cli, "_public_ip_via_dns", return_value="47.52.1.99"):
                self.assertEqual(cli._my_public_ip(), "47.52.1.99")
            with mock.patch.object(cli, "_public_ip_via_dns", return_value=None):
                self.assertIsNone(cli._my_public_ip())

    def test_ipify_is_not_first(self):
        self.assertNotIn("ipify", cli.IP_ECHO_URLS[0])


class NormalizeCidrTests(unittest.TestCase):
    def test_bare_ipv4_becomes_slash_32(self):
        self.assertEqual(cli._normalize_cidr("47.52.1.2"), "47.52.1.2/32")

    def test_existing_prefix_is_kept(self):
        self.assertEqual(cli._normalize_cidr("47.52.1.2/24"), "47.52.1.0/24")
        self.assertEqual(cli._normalize_cidr("0.0.0.0/0"), "0.0.0.0/0")

    def test_ipv6_is_refused_with_an_explanation(self):
        """This is the exact shape of the InvalidParam.SourceCidrIp failure."""
        with self.assertRaises(SystemExit) as ctx:
            cli._normalize_cidr("240e:3b3:1234:5678::1/128")
        self.assertIn("IPv4", str(ctx.exception))

    def test_garbage_is_refused(self):
        with self.assertRaises(SystemExit):
            cli._normalize_cidr("不是IP")
        with self.assertRaises(SystemExit):
            cli._normalize_cidr("")
        with self.assertRaises(SystemExit):
            cli._normalize_cidr("47.52.1.2/99")


if __name__ == "__main__":
    unittest.main()
