import unittest

from bot.feishu_codexctl import _build_parser, _thread_target_params


class FeishuCodexCtlTests(unittest.TestCase):
    def test_thread_status_accepts_explicit_thread_id(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "status", "--thread-id", "thread-1"])

        self.assertEqual(_thread_target_params(args), {"thread_id": "thread-1"})

    def test_thread_status_accepts_explicit_thread_name(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "status", "--thread-name", "demo"])

        self.assertEqual(_thread_target_params(args), {"thread_name": "demo"})

    def test_thread_status_requires_explicit_selector(self) -> None:
        parser = _build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["thread", "status"])

    def test_thread_status_rejects_both_selectors(self) -> None:
        parser = _build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["thread", "status", "--thread-id", "thread-1", "--thread-name", "demo"])

    def test_thread_bindings_accepts_explicit_thread_id(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "bindings", "--thread-id", "thread-1"])

        self.assertEqual(_thread_target_params(args), {"thread_id": "thread-1"})

    def test_thread_bindings_accepts_explicit_thread_name(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "bindings", "--thread-name", "demo"])

        self.assertEqual(_thread_target_params(args), {"thread_name": "demo"})

    def test_thread_release_accepts_explicit_thread_id(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "release-feishu-runtime", "--thread-id", "thread-1"])

        self.assertEqual(_thread_target_params(args), {"thread_id": "thread-1"})

    def test_thread_release_accepts_explicit_thread_name(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "release-feishu-runtime", "--thread-name", "demo"])

        self.assertEqual(_thread_target_params(args), {"thread_name": "demo"})
