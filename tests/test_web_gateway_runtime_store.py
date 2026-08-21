import io
import json
import pathlib
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from bot.network_contract import parse_owned_web_gateway_endpoint
from bot.runtime_admin.cli import _open_web
from bot.stores.web_gateway_runtime_store import WebGatewayRuntimeStore


class WebGatewayRuntimeStoreTests(unittest.TestCase):
    @staticmethod
    def _write_runtime(
        data_dir: pathlib.Path,
        *,
        endpoint: object = "http://127.0.0.1:1234",
        owner_process_identity: object = "incarnation",
        owner_pid: object = 123,
    ) -> pathlib.Path:
        path = data_dir / "web_gateway_runtime.json"
        path.write_text(
            json.dumps(
                {
                    "endpoint": endpoint,
                    "bootstrap_token": "secret",
                    "owner_pid": owner_pid,
                    "owner_process_identity": owner_process_identity,
                    "started_at": 10,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_owned_endpoint_requires_exact_canonical_numeric_loopback_origin(self):
        for endpoint in (
            "http://127.0.0.1:1",
            "http://127.0.0.1:65535",
            "http://[::1]:8766",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(
                    parse_owned_web_gateway_endpoint(endpoint).origin,
                    endpoint,
                )

        rejected = (
            "",
            " http://127.0.0.1:1234",
            "http://127.0.0.1:1234 ",
            "http://127.0.0.1:12 34",
            "HTTP://127.0.0.1:1234",
            "https://127.0.0.1:1234",
            "http://localhost:1234",
            "http://example.com:1234",
            "http://127.0.0.2:1234",
            "http://[::ffff:127.0.0.1]:1234",
            "http://user@127.0.0.1:1234",
            "http://user:password@127.0.0.1:1234",
            "http://127.0.0.1",
            "http://127.0.0.1:0",
            "http://127.0.0.1:65536",
            "http://127.0.0.1:01234",
            "http://127.0.0.1:1234/",
            "http://127.0.0.1:1234/path",
            "http://127.0.0.1:1234?",
            "http://127.0.0.1:1234?query",
            "http://127.0.0.1:1234#",
            "http://127.0.0.1:1234#fragment",
        )
        for endpoint in rejected:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    parse_owned_web_gateway_endpoint(endpoint)

        with self.assertRaises(ValueError):
            parse_owned_web_gateway_endpoint(1234)  # type: ignore[arg-type]

    def test_invalid_runtime_discovery_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            data_dir = pathlib.Path(raw)
            path = data_dir / "web_gateway_runtime.json"
            path.write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "web_gateway_runtime.json"):
                WebGatewayRuntimeStore(data_dir).load()

            self.assertTrue(path.exists())

    def test_runtime_requires_canonical_endpoint_and_nonempty_stored_identity(self):
        invalid_fields = (
            ("endpoint", "https://example.com:1234"),
            ("endpoint", "http://127.0.0.1:1234/"),
            ("owner_process_identity", None),
            ("owner_process_identity", ""),
            ("owner_process_identity", " incarnation "),
            ("owner_process_identity", 123),
        )
        for field, value in invalid_fields:
            with self.subTest(field=field, value=value):
                with tempfile.TemporaryDirectory() as raw:
                    data_dir = pathlib.Path(raw)
                    kwargs = {field: value}
                    path = self._write_runtime(data_dir, **kwargs)
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "web_gateway_runtime.json",
                    ):
                        WebGatewayRuntimeStore(data_dir).load()
                    self.assertTrue(path.exists())

    def test_save_requires_identity_without_overwriting_current_publication(self):
        with tempfile.TemporaryDirectory() as raw:
            data_dir = pathlib.Path(raw)
            store = WebGatewayRuntimeStore(data_dir)
            with patch(
                "bot.stores.web_gateway_runtime_store.process_identity",
                return_value="current-incarnation",
            ):
                store.save(
                    endpoint="http://127.0.0.1:1234",
                    bootstrap_token="old",
                    owner_pid=123,
                )

            for invalid_identity in ("", " current-incarnation "):
                with self.subTest(invalid_identity=invalid_identity):
                    with patch(
                        "bot.stores.web_gateway_runtime_store.process_identity",
                        return_value=invalid_identity,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "process incarnation"):
                            store.save(
                                endpoint="http://127.0.0.1:5678",
                                bootstrap_token="new",
                                owner_pid=123,
                            )

            with patch(
                "bot.stores.web_gateway_runtime_store.process_identity",
                return_value="current-incarnation",
            ):
                with patch(
                    "bot.stores.web_gateway_runtime_store.process_exists",
                    return_value=True,
                ):
                    runtime = store.load()
            self.assertIsNotNone(runtime)
            assert runtime is not None
            self.assertEqual(runtime.endpoint, "http://127.0.0.1:1234")
            self.assertEqual(runtime.bootstrap_token, "old")

    def test_pid_reuse_prunes_stale_bootstrap_capability(self):
        with tempfile.TemporaryDirectory() as raw:
            data_dir = pathlib.Path(raw)
            store = WebGatewayRuntimeStore(data_dir)
            with patch(
                "bot.stores.web_gateway_runtime_store.process_identity",
                return_value="old-incarnation",
            ):
                store.save(
                    endpoint="http://127.0.0.1:1234",
                    bootstrap_token="secret",
                    owner_pid=123,
                )

            with patch(
                "bot.stores.web_gateway_runtime_store.process_identity",
                return_value="new-incarnation",
            ) as identity:
                with patch(
                    "bot.stores.web_gateway_runtime_store.process_exists",
                    return_value=True,
                ) as exists:
                    self.assertIsNone(store.load())

            exists.assert_called_once_with(123)
            identity.assert_called_once_with(123)
            self.assertFalse((data_dir / "web_gateway_runtime.json").exists())

    def test_unknown_current_identity_retains_record_without_publishing(self):
        with tempfile.TemporaryDirectory() as raw:
            data_dir = pathlib.Path(raw)
            path = self._write_runtime(data_dir)
            with patch(
                "bot.stores.web_gateway_runtime_store.process_identity",
                return_value="",
            ):
                with patch(
                    "bot.stores.web_gateway_runtime_store.process_exists",
                    return_value=True,
                ):
                    self.assertIsNone(WebGatewayRuntimeStore(data_dir).load())
            self.assertTrue(path.exists())

    def test_unconfirmed_liveness_retains_matching_record_without_publishing(self):
        with tempfile.TemporaryDirectory() as raw:
            data_dir = pathlib.Path(raw)
            path = self._write_runtime(data_dir)
            with patch(
                "bot.stores.web_gateway_runtime_store.process_identity",
                return_value="incarnation",
            ) as identity:
                with patch(
                    "bot.stores.web_gateway_runtime_store.process_exists",
                    return_value=False,
                ):
                    self.assertIsNone(WebGatewayRuntimeStore(data_dir).load())
            identity.assert_not_called()
            self.assertTrue(path.exists())

    def test_noncanonical_current_identity_is_unknown_and_retains_record(self):
        with tempfile.TemporaryDirectory() as raw:
            data_dir = pathlib.Path(raw)
            path = self._write_runtime(data_dir)
            with patch(
                "bot.stores.web_gateway_runtime_store.process_identity",
                return_value=" incarnation ",
            ):
                with patch(
                    "bot.stores.web_gateway_runtime_store.process_exists",
                    return_value=True,
                ):
                    self.assertIsNone(WebGatewayRuntimeStore(data_dir).load())
            self.assertTrue(path.exists())

    def test_ipv4_ipv6_round_trip_and_owner_guarded_clear(self):
        for endpoint in ("http://127.0.0.1:1234", "http://[::1]:1234"):
            with self.subTest(endpoint=endpoint):
                with tempfile.TemporaryDirectory() as raw:
                    store = WebGatewayRuntimeStore(pathlib.Path(raw))
                    with patch(
                        "bot.stores.web_gateway_runtime_store.process_identity",
                        return_value="incarnation",
                    ):
                        with patch(
                            "bot.stores.web_gateway_runtime_store.process_exists",
                            return_value=True,
                        ):
                            store.save(
                                endpoint=endpoint,
                                bootstrap_token="secret",
                                owner_pid=123,
                                started_at=10,
                            )
                            runtime = store.load()
                            self.assertIsNotNone(runtime)
                            assert runtime is not None
                            self.assertEqual(runtime.endpoint, endpoint)
                            store.clear(owner_pid=999)
                            self.assertIsNotNone(store.load())
                            store.clear(owner_pid=123)
                            self.assertIsNone(store.load())

    def test_stale_reader_cannot_delete_new_runtime_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            data_dir = pathlib.Path(raw)
            stale_store = WebGatewayRuntimeStore(data_dir)
            fresh_store = WebGatewayRuntimeStore(data_dir)
            with patch(
                "bot.stores.web_gateway_runtime_store.process_identity",
                return_value="old-incarnation",
            ):
                stale_store.save(
                    endpoint="http://127.0.0.1:1",
                    bootstrap_token="old",
                    owner_pid=101,
                )
            stale_check_entered = threading.Event()
            release_stale_check = threading.Event()

            def process_identity(pid: int) -> str:
                if pid == 101:
                    stale_check_entered.set()
                    self.assertTrue(release_stale_check.wait(timeout=1))
                    return "replacement-incarnation"
                if pid == 202:
                    return "fresh-incarnation"
                return ""

            with patch(
                "bot.stores.web_gateway_runtime_store.process_identity",
                side_effect=process_identity,
            ):
                with patch(
                    "bot.stores.web_gateway_runtime_store.process_exists",
                    return_value=True,
                ):
                    loader = threading.Thread(target=stale_store.load)
                    loader.start()
                    self.assertTrue(stale_check_entered.wait(timeout=1))
                    saver = threading.Thread(
                        target=lambda: fresh_store.save(
                            endpoint="http://127.0.0.1:2",
                            bootstrap_token="new",
                            owner_pid=202,
                        )
                    )
                    saver.start()
                    release_stale_check.set()
                    loader.join(timeout=1)
                    saver.join(timeout=1)

                    self.assertFalse(loader.is_alive())
                    self.assertFalse(saver.is_alive())
                    runtime = fresh_store.load()

            self.assertIsNotNone(runtime)
            assert runtime is not None
            self.assertEqual(runtime.endpoint, "http://127.0.0.1:2")
            self.assertEqual(runtime.bootstrap_token, "new")

    def test_open_web_invalid_record_has_zero_url_and_browser_effect(self):
        invalid_records = (
            {"endpoint": "https://example.com:1234"},
            {"owner_process_identity": None},
        )
        for overrides in invalid_records:
            with self.subTest(overrides=overrides):
                with tempfile.TemporaryDirectory() as raw:
                    data_dir = pathlib.Path(raw)
                    self._write_runtime(data_dir, **overrides)
                    stdout = io.StringIO()
                    with patch(
                        "bot.runtime_admin.cli.webbrowser.open",
                    ) as mock_open:
                        with redirect_stdout(stdout):
                            with self.assertRaises(RuntimeError):
                                _open_web(data_dir, no_browser=False)
                    self.assertEqual(stdout.getvalue(), "")
                    mock_open.assert_not_called()

    def test_open_web_unknown_identity_has_zero_effect_and_retains_record(self):
        with tempfile.TemporaryDirectory() as raw:
            data_dir = pathlib.Path(raw)
            path = self._write_runtime(data_dir)
            stdout = io.StringIO()
            with patch(
                "bot.stores.web_gateway_runtime_store.process_identity",
                return_value="",
            ):
                with patch(
                    "bot.stores.web_gateway_runtime_store.process_exists",
                    return_value=True,
                ):
                    with patch("bot.runtime_admin.cli.webbrowser.open") as mock_open:
                        with redirect_stdout(stdout):
                            with self.assertRaisesRegex(
                                ValueError,
                                "尚未发布 Focus Web Gateway",
                            ):
                                _open_web(data_dir, no_browser=False)
            self.assertEqual(stdout.getvalue(), "")
            mock_open.assert_not_called()
            self.assertTrue(path.exists())

    def test_open_web_unconfirmed_liveness_has_zero_effect_and_retains_record(self):
        with tempfile.TemporaryDirectory() as raw:
            data_dir = pathlib.Path(raw)
            path = self._write_runtime(data_dir)
            stdout = io.StringIO()
            with patch(
                "bot.stores.web_gateway_runtime_store.process_identity",
                return_value="incarnation",
            ) as identity:
                with patch(
                    "bot.stores.web_gateway_runtime_store.process_exists",
                    return_value=False,
                ):
                    with patch("bot.runtime_admin.cli.webbrowser.open") as mock_open:
                        with redirect_stdout(stdout):
                            with self.assertRaisesRegex(
                                ValueError,
                                "尚未发布 Focus Web Gateway",
                            ):
                                _open_web(data_dir, no_browser=False)
            identity.assert_not_called()
            self.assertEqual(stdout.getvalue(), "")
            mock_open.assert_not_called()
            self.assertTrue(path.exists())

    def test_open_web_ipv6_no_browser_prints_validated_url_only(self):
        with tempfile.TemporaryDirectory() as raw:
            data_dir = pathlib.Path(raw)
            store = WebGatewayRuntimeStore(data_dir)
            with patch(
                "bot.stores.web_gateway_runtime_store.process_identity",
                return_value="incarnation",
            ):
                with patch(
                    "bot.stores.web_gateway_runtime_store.process_exists",
                    return_value=True,
                ):
                    store.save(
                        endpoint="http://[::1]:8766",
                        bootstrap_token="token/value",
                        owner_pid=123,
                    )
                    stdout = io.StringIO()
                    with patch("bot.runtime_admin.cli.webbrowser.open") as mock_open:
                        with redirect_stdout(stdout):
                            result = _open_web(data_dir, no_browser=True)

            self.assertEqual(result, 0)
            self.assertEqual(
                stdout.getvalue().strip(),
                "http://[::1]:8766/#token=token%2Fvalue",
            )
            mock_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
