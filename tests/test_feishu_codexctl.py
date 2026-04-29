import io
import unittest
from contextlib import redirect_stdout

from bot.feishu_codexctl import _build_parser, _thread_target_params


class FeishuCodexCtlTests(unittest.TestCase):
    def test_top_level_help_includes_operator_guidance(self) -> None:
        parser = _build_parser()
        rendered = parser.format_help()

        self.assertIn("本地查看 / 管理面", rendered)
        self.assertIn("不是第二个 Codex 前端", rendered)
        self.assertIn("除 `instance list` 外", rendered)
        self.assertIn("binding clear", rendered)
        self.assertIn("常用命令:", rendered)
        self.assertIn("feishu-codexctl --instance corp-a service status", rendered)

    def test_thread_help_includes_scope_and_selector_guidance(self) -> None:
        parser = _build_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["thread", "--help"])

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("Thread 管理面", rendered)
        self.assertIn("`list` 默认列当前目录线程", rendered)
        self.assertIn("`--thread-id` 或 `--thread-name`", rendered)
        self.assertIn("thread commands", rendered)
        self.assertIn("unsubscribe", rendered)
        self.assertIn("persisted thread", rendered)

    def test_binding_help_includes_clear_semantics(self) -> None:
        parser = _build_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["binding", "--help"])

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("Binding 管理面", rendered)
        self.assertIn("Feishu 本地 bookmark", rendered)
        self.assertIn("不等于 `unsubscribe`", rendered)

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

    def test_thread_list_defaults_to_cwd_scope(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "list"])

        self.assertEqual(args.resource, "thread")
        self.assertEqual(args.action, "list")
        self.assertEqual(args.scope, "cwd")
        self.assertEqual(args.cwd, "")

    def test_thread_list_accepts_global_scope_and_explicit_cwd(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "list", "--scope", "global", "--cwd", "/tmp/project"])

        self.assertEqual(args.scope, "global")
        self.assertEqual(args.cwd, "/tmp/project")

    def test_thread_unsubscribe_accepts_explicit_thread_id(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "unsubscribe", "--thread-id", "thread-1"])

        self.assertEqual(_thread_target_params(args), {"thread_id": "thread-1"})

    def test_thread_unsubscribe_accepts_explicit_thread_name(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "unsubscribe", "--thread-name", "demo"])

        self.assertEqual(_thread_target_params(args), {"thread_name": "demo"})

    def test_parser_accepts_global_instance_selector(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["--instance", "corp-b", "service", "status"])

        self.assertEqual(args.instance, "corp-b")
        self.assertEqual(args.resource, "service")
        self.assertEqual(args.action, "status")
