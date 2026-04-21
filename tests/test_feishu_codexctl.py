import unittest

from bot.feishu_codexctl import _build_parser, _thread_target_params


class FeishuCodexCtlTests(unittest.TestCase):
    def test_binding_clear_accepts_binding_id(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["binding", "clear", "p2p:ou_user:chat-1"])

        self.assertEqual(args.binding_id, "p2p:ou_user:chat-1")

    def test_binding_clear_all_accepts_no_args(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["binding", "clear-all"])

        self.assertEqual(args.resource, "binding")
        self.assertEqual(args.action, "clear-all")

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

    def test_parser_accepts_global_instance_selector(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["--instance", "corp-b", "service", "status"])

        self.assertEqual(args.instance, "corp-b")
        self.assertEqual(args.resource, "service")
        self.assertEqual(args.action, "status")

    def test_thread_import_accepts_explicit_thread_id(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "import", "--thread-id", "thread-1"])

        self.assertEqual(_thread_target_params(args), {"thread_id": "thread-1"})

    def test_thread_revoke_accepts_explicit_thread_name(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "revoke", "--thread-name", "demo"])

        self.assertEqual(_thread_target_params(args), {"thread_name": "demo"})
