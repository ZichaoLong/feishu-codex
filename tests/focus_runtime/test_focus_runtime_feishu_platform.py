from __future__ import annotations

import ast
import pathlib
import unittest
from types import SimpleNamespace

import bot.focus_runtime.feishu_platform as feishu_platform_module
import bot.focus_runtime.runtime as focus_runtime_module
from bot.focus_runtime.feishu_platform import FeishuPlatform
from bot.runtime_card_publisher import RuntimeCardPublisher


_ROOT_PATH = pathlib.Path(focus_runtime_module.__file__).resolve()
_OWNER_PATH = pathlib.Path(feishu_platform_module.__file__).resolve()
_EXTRACTED_ROOT_METHODS = {
    "_runtime_card_publisher",
    "_resolve_chat_type",
    "_is_group_chat",
    "_resolve_binding_chat_display_name",
    "_group_actor_open_id",
    "_message_reply_in_thread",
    "_is_group_admin_actor",
    "_group_command_admin_denial_text",
    "_interaction_actor_allowed",
    "_reply_text",
    "_reply_text_get_id",
    "_reply_card",
    "_claim_reserved_execution_card",
}
_PUBLIC_PLATFORM_METHODS = {
    "runtime_card_publisher",
    "resolve_chat_type",
    "is_group_chat",
    "resolve_binding_chat_display_name",
    "group_actor_open_id",
    "message_reply_in_thread",
    "is_group_admin_actor",
    "group_command_admin_denial_text",
    "interaction_actor_allowed",
    "reply_text",
    "reply_text_get_id",
    "reply_card",
    "claim_reserved_execution_card",
}
_GROUP_COMMAND_ADMIN_DENIAL_TEXT = (
    "群里的 `/` 命令仅管理员可用；已授权成员请直接提问或显式 mention 触发机器人。"
)


def _class_node(path: pathlib.Path, class_name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )


def _method_node(owner: ast.ClassDef, method_name: str) -> ast.FunctionDef:
    return next(
        node
        for node in owner.body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )


class _PlatformBotFake:
    def __init__(self) -> None:
        self.app_id = "app-a"
        self.calls: list[tuple] = []
        self.message_contexts: dict[str, dict] = {}
        self.chat_type_cache: dict[str, object] = {}
        self.chat_type_fetch: dict[str, object] = {}
        self.cached_sender_names: dict[str, str] = {}
        self.sender_names: dict[str, str] = {}
        self.cached_chat_names: dict[str, str] = {}
        self.refreshed_chat_names: dict[str, str] = {}
        self.fallback_chat_names: dict[str, str] = {}
        self.admin_result = False
        self.allowed_result = False
        self.reply_result: object = True
        self.reply_id_result: object = "message-created"
        self.claim_result: object = "reserved-card"

    def get_message_context(self, message_id: str) -> dict:
        self.calls.append(("get_message_context", message_id))
        return self.message_contexts.get(message_id, {})

    def lookup_chat_type(self, chat_id: str) -> object:
        self.calls.append(("lookup_chat_type", chat_id))
        return self.chat_type_cache.get(chat_id, "")

    def fetch_runtime_chat_type(self, chat_id: str) -> object:
        self.calls.append(("fetch_runtime_chat_type", chat_id))
        return self.chat_type_fetch.get(chat_id, "")

    def lookup_cached_sender_name(self, sender_id: str) -> str:
        self.calls.append(("lookup_cached_sender_name", sender_id))
        return self.cached_sender_names.get(sender_id, "")

    def get_sender_display_name(self, **kwargs) -> str:
        self.calls.append(("get_sender_display_name", kwargs))
        return self.sender_names.get(str(kwargs.get("open_id", "")), "")

    def lookup_chat_display_name(self, chat_id: str) -> str:
        self.calls.append(("lookup_chat_display_name", chat_id))
        return self.cached_chat_names.get(chat_id, "")

    def refresh_chat_display_name(self, chat_id: str) -> str:
        self.calls.append(("refresh_chat_display_name", chat_id))
        return self.refreshed_chat_names.get(chat_id, "")

    def get_chat_display_name(self, chat_id: str) -> str:
        self.calls.append(("get_chat_display_name", chat_id))
        return self.fallback_chat_names.get(chat_id, "")

    def is_group_admin(self, *, open_id: str) -> bool:
        self.calls.append(("is_group_admin", open_id))
        return self.admin_result

    def is_group_user_allowed(self, chat_id: str, *, open_id: str) -> bool:
        self.calls.append(("is_group_user_allowed", chat_id, open_id))
        return self.allowed_result

    def reply(self, *args, **kwargs) -> object:
        self.calls.append(("reply", args, kwargs))
        return self.reply_result

    def reply_get_id(self, *args, **kwargs) -> object:
        self.calls.append(("reply_get_id", args, kwargs))
        return self.reply_id_result

    def reply_card(self, *args, **kwargs) -> None:
        self.calls.append(("reply_card", args, kwargs))

    def claim_reserved_execution_card(self, trigger_message_id: str) -> object:
        self.calls.append(
            ("claim_reserved_execution_card", trigger_message_id)
        )
        return self.claim_result


def _attached_platform(bot=None) -> tuple[FeishuPlatform, object]:
    attached_bot = bot if bot is not None else _PlatformBotFake()
    platform = FeishuPlatform()
    platform.attach(attached_bot)
    return platform, attached_bot


class FeishuPlatformBoundaryTests(unittest.TestCase):
    def test_exact_platform_methods_leave_root_and_live_on_owner(self) -> None:
        root = _class_node(_ROOT_PATH, "FocusRuntime")
        owner = _class_node(_OWNER_PATH, "FeishuPlatform")
        root_methods = {
            node.name
            for node in root.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        root_extracted_method_references = {
            node.attr
            for node in ast.walk(root)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in _EXTRACTED_ROOT_METHODS
        }
        root_bot_references = {
            node.attr
            for node in ast.walk(root)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr == "bot"
        }
        owner_methods = {
            node.name
            for node in owner.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertEqual(len(_EXTRACTED_ROOT_METHODS), 13)
        self.assertEqual(root_methods & _EXTRACTED_ROOT_METHODS, set())
        self.assertEqual(root_extracted_method_references, set())
        self.assertEqual(root_bot_references, set())
        self.assertEqual(
            owner_methods,
            {"__init__", "bot", "attach"} | _PUBLIC_PLATFORM_METHODS,
        )
        self.assertEqual(len(_PUBLIC_PLATFORM_METHODS), 13)

    def test_owner_stores_only_attached_bot_identity(self) -> None:
        owner = _class_node(_OWNER_PATH, "FeishuPlatform")
        stored_attrs = {
            node.attr
            for node in ast.walk(owner)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }

        self.assertEqual(stored_attrs, {"_bot"})

    def test_platform_is_composed_before_binding_runtime_manager(self) -> None:
        root = _class_node(_ROOT_PATH, "FocusRuntime")
        initializer = _method_node(root, "__init__")
        platform_calls = [
            node
            for node in ast.walk(initializer)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "FeishuPlatform"
        ]
        binding_manager_calls = [
            node
            for node in ast.walk(initializer)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "BindingRuntimeManager"
        ]

        self.assertEqual(len(platform_calls), 1)
        self.assertEqual(len(binding_manager_calls), 1)
        self.assertLess(platform_calls[0].lineno, binding_manager_calls[0].lineno)

    def test_lifecycle_reads_attached_bot_app_id_only_when_callback_runs(
        self,
    ) -> None:
        root = _class_node(_ROOT_PATH, "FocusRuntime")
        initializer = _method_node(root, "__init__")
        activation_calls = [
            node
            for node in ast.walk(initializer)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ServiceRuntimeActivationPorts"
        ]

        self.assertEqual(len(activation_calls), 1)
        activation_keywords = {
            keyword.arg: keyword.value
            for keyword in activation_calls[0].keywords
            if keyword.arg is not None
        }
        self.assertEqual(
            ast.dump(
                activation_keywords["prepare_owned_state"],
                include_attributes=False,
            ),
            ast.dump(
                ast.parse(
                    "lambda: service_authority.prepare_owned_state("
                    "feishu_platform.bot.app_id)",
                    mode="eval",
                ).body,
                include_attributes=False,
            ),
        )

    def test_start_attaches_then_registers_terminal_resolver_then_starts(
        self,
    ) -> None:
        root = _class_node(_ROOT_PATH, "FocusRuntime")
        start = _method_node(root, "start")

        self.assertGreaterEqual(len(start.body), 4)
        self.assertEqual(
            ast.dump(start.body[0], include_attributes=False),
            ast.dump(
                ast.parse(
                    "self._feishu_platform.attach(bot)",
                    mode="exec",
                ).body[0],
                include_attributes=False,
            ),
        )
        self.assertEqual(
            ast.dump(start.body[1], include_attributes=False),
            ast.dump(
                ast.parse(
                    "set_terminal_result_text_resolver = getattr("
                    "bot, 'set_terminal_result_text_resolver', None)",
                    mode="exec",
                ).body[0],
                include_attributes=False,
            ),
        )
        self.assertEqual(
            ast.dump(start.body[2], include_attributes=False),
            ast.dump(
                ast.parse(
                    "if callable(set_terminal_result_text_resolver):\n"
                    "    set_terminal_result_text_resolver(\n"
                    "        self._terminal_results.resolve_terminal_result_text\n"
                    "    )",
                    mode="exec",
                ).body[0],
                include_attributes=False,
            ),
        )
        lifecycle_try = start.body[3]
        self.assertIsInstance(lifecycle_try, ast.Try)
        assert isinstance(lifecycle_try, ast.Try)
        self.assertEqual(
            ast.dump(lifecycle_try.body[0], include_attributes=False),
            ast.dump(
                ast.parse(
                    "self._service_runtime_lifecycle.start()",
                    mode="exec",
                ).body[0],
                include_attributes=False,
            ),
        )


class FeishuPlatformAttachmentTests(unittest.TestCase):
    def test_attach_is_identity_stable_and_rejects_a_different_bot(self) -> None:
        platform = FeishuPlatform()
        bot_a = object()
        bot_b = object()

        self.assertIsNone(platform.bot)
        platform.attach(bot_a)
        self.assertIs(platform.bot, bot_a)
        platform.attach(bot_a)
        self.assertIs(platform.bot, bot_a)

        with self.assertRaisesRegex(
            RuntimeError,
            "^Focus runtime is already attached to another platform adapter$",
        ):
            platform.attach(bot_b)
        self.assertIs(platform.bot, bot_a)


class FeishuPlatformResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, attached_bot = _attached_platform()
        self.bot = attached_bot
        assert isinstance(self.bot, _PlatformBotFake)

    def test_chat_type_precedence_short_circuits_at_each_authority(self) -> None:
        self.bot.message_contexts["message-1"] = {"chat_type": " group "}
        self.bot.chat_type_cache["chat-1"] = "p2p"
        self.bot.chat_type_fetch["chat-1"] = "p2p"

        self.assertEqual(
            self.platform.resolve_chat_type("chat-1", "message-1"),
            "group",
        )
        self.assertEqual(
            self.bot.calls,
            [("get_message_context", "message-1")],
        )

        self.bot.calls.clear()
        self.bot.message_contexts["message-2"] = {}
        self.bot.chat_type_cache["chat-2"] = " p2p "
        self.bot.chat_type_fetch["chat-2"] = "group"

        self.assertEqual(
            self.platform.resolve_chat_type("chat-2", "message-2"),
            "p2p",
        )
        self.assertEqual(
            self.bot.calls,
            [
                ("get_message_context", "message-2"),
                ("lookup_chat_type", "chat-2"),
            ],
        )

        self.bot.calls.clear()
        self.bot.chat_type_cache["chat-3"] = ""
        self.bot.chat_type_fetch["chat-3"] = " group "

        self.assertEqual(self.platform.resolve_chat_type("chat-3"), "group")
        self.assertEqual(
            self.bot.calls,
            [
                ("lookup_chat_type", "chat-3"),
                ("fetch_runtime_chat_type", "chat-3"),
            ],
        )

        self.bot.calls.clear()
        self.assertEqual(self.platform.resolve_chat_type("chat-missing"), "")
        self.assertEqual(
            self.bot.calls,
            [
                ("lookup_chat_type", "chat-missing"),
                ("fetch_runtime_chat_type", "chat-missing"),
            ],
        )

    def test_display_name_resolution_covers_cached_and_refreshed_paths(self) -> None:
        self.bot.cached_sender_names["ou-user"] = "Cached User"
        self.bot.sender_names["ou-user"] = "Fresh User"
        self.bot.cached_chat_names["chat-group"] = "Cached Group"
        self.bot.refreshed_chat_names["chat-group"] = "Fresh Group"

        self.assertEqual(
            self.platform.resolve_binding_chat_display_name(
                binding_kind="p2p",
                sender_id="ou-user",
                chat_id="chat-p2p",
            ),
            "Cached User",
        )
        self.assertEqual(
            self.platform.resolve_binding_chat_display_name(
                binding_kind="p2p",
                sender_id="ou-user",
                chat_id="chat-p2p",
                refresh_names=True,
            ),
            "Fresh User",
        )
        self.assertEqual(
            self.platform.resolve_binding_chat_display_name(
                binding_kind="group",
                sender_id="ou-user",
                chat_id="chat-group",
            ),
            "Cached Group",
        )
        self.assertEqual(
            self.platform.resolve_binding_chat_display_name(
                binding_kind="group",
                sender_id="ou-user",
                chat_id="chat-group",
                refresh_names=True,
            ),
            "Fresh Group",
        )
        self.assertEqual(
            self.bot.calls,
            [
                ("lookup_cached_sender_name", "ou-user"),
                (
                    "get_sender_display_name",
                    {"open_id": "ou-user", "sender_type": "user"},
                ),
                ("lookup_chat_display_name", "chat-group"),
                ("refresh_chat_display_name", "chat-group"),
            ],
        )

    def test_group_refresh_falls_back_for_older_bot_and_unknown_kind_is_empty(
        self,
    ) -> None:
        get_chat_display_name_calls: list[str] = []
        legacy_bot = SimpleNamespace(
            get_chat_display_name=lambda chat_id: (
                get_chat_display_name_calls.append(chat_id) or "Legacy Group"
            )
        )
        platform, _ = _attached_platform(legacy_bot)

        self.assertEqual(
            platform.resolve_binding_chat_display_name(
                binding_kind="group",
                sender_id="ou-user",
                chat_id="chat-group",
                refresh_names=True,
            ),
            "Legacy Group",
        )
        self.assertEqual(get_chat_display_name_calls, ["chat-group"])
        self.assertEqual(
            platform.resolve_binding_chat_display_name(
                binding_kind="unknown",
                sender_id="ou-user",
                chat_id="chat-group",
                refresh_names=True,
            ),
            "",
        )
        self.assertEqual(get_chat_display_name_calls, ["chat-group"])

    def test_group_actor_uses_operator_then_message_sender_then_explicit_sender(
        self,
    ) -> None:
        self.bot.message_contexts["message-1"] = {
            "sender_open_id": " context-user "
        }

        self.assertEqual(
            self.platform.group_actor_open_id(
                "message-1",
                " operator-user ",
                " explicit-user ",
            ),
            "operator-user",
        )
        self.assertEqual(self.bot.calls, [])

        self.assertEqual(
            self.platform.group_actor_open_id(
                "message-1",
                "   ",
                " explicit-user ",
            ),
            "context-user",
        )
        self.assertEqual(
            self.bot.calls,
            [("get_message_context", "message-1")],
        )

        self.bot.calls.clear()
        self.bot.message_contexts["message-1"] = {"sender_open_id": "  "}
        self.assertEqual(
            self.platform.group_actor_open_id(
                "message-1",
                sender_open_id=" explicit-user ",
            ),
            "explicit-user",
        )

    def test_message_reply_in_thread_requires_a_nonempty_thread_id(self) -> None:
        self.bot.message_contexts["message-flat"] = {"thread_id": "  "}
        self.bot.message_contexts["message-thread"] = {"thread_id": " om-1 "}

        self.assertFalse(self.platform.message_reply_in_thread(""))
        self.assertEqual(self.bot.calls, [])
        self.assertFalse(self.platform.message_reply_in_thread("message-flat"))
        self.assertTrue(self.platform.message_reply_in_thread("message-thread"))

    def test_group_admin_rule_allows_non_group_and_checks_group_actor(self) -> None:
        self.bot.chat_type_cache["chat-p2p"] = "p2p"

        self.assertTrue(
            self.platform.is_group_admin_actor(
                "chat-p2p",
                sender_open_id="ou-user",
            )
        )
        self.assertFalse(
            any(call[0] == "is_group_admin" for call in self.bot.calls)
        )

        self.bot.calls.clear()
        self.bot.chat_type_cache["chat-group"] = "group"
        self.bot.admin_result = True
        self.assertTrue(
            self.platform.is_group_admin_actor(
                "chat-group",
                operator_open_id=" ou-admin ",
            )
        )
        self.assertIn(("is_group_admin", "ou-admin"), self.bot.calls)

        self.bot.calls.clear()
        self.bot.admin_result = False
        self.assertFalse(
            self.platform.is_group_admin_actor(
                "chat-group",
                sender_open_id="ou-member",
            )
        )
        self.assertIn(("is_group_admin", "ou-member"), self.bot.calls)

    def test_group_command_denial_is_exact_and_only_for_non_admin_group_actor(
        self,
    ) -> None:
        self.bot.chat_type_cache.update(
            {"chat-p2p": "p2p", "chat-group": "group"}
        )

        self.assertEqual(
            self.platform.group_command_admin_denial_text(
                "chat-p2p",
                sender_open_id="ou-member",
            ),
            "",
        )
        self.bot.admin_result = True
        self.assertEqual(
            self.platform.group_command_admin_denial_text(
                "chat-group",
                sender_open_id="ou-admin",
            ),
            "",
        )
        self.bot.admin_result = False
        self.assertEqual(
            self.platform.group_command_admin_denial_text(
                "chat-group",
                sender_open_id="ou-member",
            ),
            _GROUP_COMMAND_ADMIN_DENIAL_TEXT,
        )

    def test_interaction_ignores_sender_and_uses_group_allowlist_only_for_group(
        self,
    ) -> None:
        self.bot.chat_type_cache.update(
            {"chat-p2p": "p2p", "chat-group": "group"}
        )

        self.assertTrue(
            self.platform.interaction_actor_allowed(
                "ignored-sender",
                "chat-p2p",
                "ou-actor",
            )
        )
        self.assertFalse(
            any(call[0] == "is_group_user_allowed" for call in self.bot.calls)
        )

        self.bot.calls.clear()
        self.bot.allowed_result = True
        self.assertTrue(
            self.platform.interaction_actor_allowed(
                "different-ignored-sender",
                "chat-group",
                "ou-actor",
            )
        )
        self.assertEqual(
            self.bot.calls[-1],
            ("is_group_user_allowed", "chat-group", "ou-actor"),
        )
        self.bot.allowed_result = False
        self.assertFalse(
            self.platform.interaction_actor_allowed(
                "ignored-sender",
                "chat-group",
                "ou-actor",
            )
        )


class FeishuPlatformReplyRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, attached_bot = _attached_platform()
        self.bot = attached_bot
        assert isinstance(self.bot, _PlatformBotFake)
        self.bot.message_contexts.update(
            {
                "message-group": {"chat_type": "group"},
                "message-p2p": {"chat_type": "p2p"},
            }
        )
        self.bot.chat_type_cache["chat-group"] = "group"

    def test_text_reply_adds_parent_fields_only_for_group_message(self) -> None:
        self.bot.reply_result = []
        result = self.platform.reply_text(
            "chat-group",
            "hello",
            message_id="message-group",
            reply_in_thread=True,
        )

        self.assertIs(result, False)
        self.assertEqual(
            self.bot.calls[-1],
            (
                "reply",
                ("chat-group", "hello"),
                {
                    "parent_message_id": "message-group",
                    "reply_in_thread": True,
                },
            ),
        )

        self.bot.calls.clear()
        self.bot.reply_result = object()
        result = self.platform.reply_text(
            "chat-p2p",
            "hello",
            message_id="message-p2p",
            reply_in_thread=True,
        )

        self.assertIs(result, True)
        self.assertEqual(
            self.bot.calls[-1],
            ("reply", ("chat-p2p", "hello"), {}),
        )

        self.bot.calls.clear()
        self.platform.reply_text(
            "chat-group",
            "hello",
            reply_in_thread=True,
        )
        self.assertEqual(
            self.bot.calls[-1],
            ("reply", ("chat-group", "hello"), {}),
        )

    def test_text_reply_id_routes_and_normalizes_optional_result(self) -> None:
        self.bot.reply_id_result = "  message-created  "
        self.assertEqual(
            self.platform.reply_text_get_id(
                "chat-group",
                "hello",
                message_id="message-group",
                reply_in_thread=True,
            ),
            "message-created",
        )
        self.assertEqual(
            self.bot.calls[-1],
            (
                "reply_get_id",
                ("chat-group", "hello"),
                {
                    "parent_message_id": "message-group",
                    "reply_in_thread": True,
                },
            ),
        )

        self.bot.calls.clear()
        self.bot.reply_id_result = 42
        self.assertEqual(
            self.platform.reply_text_get_id(
                "chat-p2p",
                "hello",
                message_id="message-p2p",
                reply_in_thread=True,
            ),
            "42",
        )
        self.assertEqual(
            self.bot.calls[-1],
            ("reply_get_id", ("chat-p2p", "hello"), {}),
        )

        self.bot.calls.clear()
        self.bot.reply_id_result = None
        self.assertEqual(
            self.platform.reply_text_get_id("chat-group", "hello"),
            "",
        )
        self.assertEqual(
            self.bot.calls[-1],
            ("reply_get_id", ("chat-group", "hello"), {}),
        )

    def test_text_reply_id_is_empty_when_bot_lacks_optional_api(self) -> None:
        bot = SimpleNamespace(
            get_message_context=lambda _message_id: {"chat_type": "group"},
            lookup_chat_type=lambda _chat_id: "group",
            fetch_runtime_chat_type=lambda _chat_id: "group",
        )
        platform, _ = _attached_platform(bot)

        self.assertEqual(
            platform.reply_text_get_id(
                "chat-group",
                "hello",
                message_id="message-group",
                reply_in_thread=True,
            ),
            "",
        )

    def test_card_reply_adds_parent_fields_only_for_group_message(self) -> None:
        card = {"type": "card"}
        self.platform.reply_card(
            "chat-group",
            card,
            message_id="message-group",
            reply_in_thread=True,
        )
        self.assertEqual(
            self.bot.calls[-1],
            (
                "reply_card",
                ("chat-group", card),
                {
                    "parent_message_id": "message-group",
                    "reply_in_thread": True,
                },
            ),
        )

        self.bot.calls.clear()
        self.platform.reply_card(
            "chat-p2p",
            card,
            message_id="message-p2p",
            reply_in_thread=True,
        )
        self.assertEqual(
            self.bot.calls[-1],
            ("reply_card", ("chat-p2p", card), {}),
        )

        self.bot.calls.clear()
        self.platform.reply_card(
            "chat-group",
            card,
            reply_in_thread=True,
        )
        self.assertEqual(
            self.bot.calls[-1],
            ("reply_card", ("chat-group", card), {}),
        )


class FeishuPlatformOptionalCapabilityTests(unittest.TestCase):
    def test_claim_reserved_card_short_circuits_and_normalizes_result(self) -> None:
        platform, attached_bot = _attached_platform()
        bot = attached_bot
        assert isinstance(bot, _PlatformBotFake)

        self.assertEqual(platform.claim_reserved_execution_card(""), "")
        self.assertEqual(bot.calls, [])

        bot.claim_result = "  reserved-message  "
        self.assertEqual(
            platform.claim_reserved_execution_card("trigger-1"),
            "reserved-message",
        )
        self.assertEqual(
            bot.calls[-1],
            ("claim_reserved_execution_card", "trigger-1"),
        )

        bot.claim_result = None
        self.assertEqual(
            platform.claim_reserved_execution_card("trigger-2"),
            "",
        )

    def test_claim_reserved_card_is_empty_when_bot_lacks_optional_api(self) -> None:
        platform, _ = _attached_platform(SimpleNamespace())

        self.assertEqual(
            platform.claim_reserved_execution_card("trigger-1"),
            "",
        )

    def test_runtime_card_publisher_factory_is_fresh_and_uses_attached_bot(
        self,
    ) -> None:
        bot = object()
        platform, _ = _attached_platform(bot)

        first = platform.runtime_card_publisher()
        second = platform.runtime_card_publisher()

        self.assertIsInstance(first, RuntimeCardPublisher)
        self.assertIsInstance(second, RuntimeCardPublisher)
        self.assertIsNot(first, second)
        self.assertIs(first._bot, bot)
        self.assertIs(second._bot, bot)
