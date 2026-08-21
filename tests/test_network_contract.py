import unittest

from bot.network_contract import parse_trusted_proxy_external_origin


class TrustedProxyExternalOriginTests(unittest.TestCase):
    def test_canonical_https_origin_projects_exact_authority(self) -> None:
        default_port = parse_trusted_proxy_external_origin(
            "https://focus.example.test"
        )
        custom_port = parse_trusted_proxy_external_origin(
            "https://focus.example.test:8443"
        )
        ipv6 = parse_trusted_proxy_external_origin(
            "https://[2001:db8::1]:8443"
        )
        ipv4 = parse_trusted_proxy_external_origin("https://203.0.113.10")

        self.assertEqual(default_port.host, "focus.example.test")
        self.assertIsNone(default_port.port)
        self.assertEqual(default_port.authority, "focus.example.test")
        self.assertEqual(default_port.origin, "https://focus.example.test")
        self.assertEqual(custom_port.port, 8443)
        self.assertEqual(custom_port.authority, "focus.example.test:8443")
        self.assertEqual(ipv6.host, "2001:db8::1")
        self.assertEqual(ipv6.authority, "[2001:db8::1]:8443")
        self.assertEqual(ipv4.host, "203.0.113.10")
        self.assertEqual(ipv4.authority, "203.0.113.10")

    def test_non_string_origin_is_rejected(self) -> None:
        for value in (None, 123, False):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "字符串"):
                    parse_trusted_proxy_external_origin(value)  # type: ignore[arg-type]

    def test_noncanonical_or_non_origin_shapes_are_rejected(self) -> None:
        invalid_values = (
            "",
            " https://focus.example.test",
            "https://focus.example.test ",
            "https://focus. example.test",
            "http://focus.example.test",
            "HTTPS://focus.example.test",
            "https://FOCUS.example.test",
            "https://*.example.test",
            "https://focus.example.test:443",
            "https://focus.example.test:08443",
            "https://user@focus.example.test",
            "https://user:secret@focus.example.test",
            "https://focus.example.test/",
            "https://focus.example.test/path",
            "https://focus.example.test?query=1",
            "https://focus.example.test#fragment",
            "https:///missing-host",
            "https://focus.example.test:0",
            "https://focus.example.test:65536",
            "https://focus.example.test:not-a-port",
            "https://éxample.test",
            "https://%65xample.test",
            "https://focus.example.test\\x",
            "https://-focus.example.test",
            "https://focus-.example.test",
            "https://localhost",
            "https://focus.localhost",
            "https://localhost.",
            "https://focus.localhost.",
            "https://0.0.0.0",
            "https://[::]",
            "https://127.0.0.2",
            "https://127.0.0.1.",
            "https://127.1",
            "https://0177.0.0.1",
            "https://0x7f000001",
            "https://2130706433",
            "https://focus.1",
            "https://example.0x10",
            "https://[::1]",
            "https://[::ffff:127.0.0.1]",
            "https://[2001:0db8::1]",
            "https://[2001:db8:0:0:0:0:0:1]",
            "https://[::ffff:192.0.2.1]",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_trusted_proxy_external_origin(value)


if __name__ == "__main__":
    unittest.main()
