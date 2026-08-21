import json
import pathlib
import tempfile
import threading

from tests.focus_runtime.codex_handler_fakes import _runtime_state
from tests.execution_page_test_support import set_execution_page_state as _set_pages
from bot.adapters.base import (
    ThreadGoalSummary,
    ThreadSnapshot,
    ThreadSummary,
)
from bot.codex_protocol.client import (
    CodexRpcError,
)
from bot.thread_runtime_authority import (
    ThreadResumeLocalFailurePolicy,
)

from tests.focus_runtime.codex_handler_test_harness import (
    CodexHandlerHarness,
    _DISPLAY_DEBUG_CONTACT_COMMAND,
    _DISPLAY_INIT_COMMAND,
    _DISPLAY_LOCAL_RESUME_COMMAND,
    _DISPLAY_RESUME_COMMAND,
)


class CodexHandlerThreadCommandTests(CodexHandlerHarness):
    def test_resume_by_name_uses_exact_name_match(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="vscode",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        resumed: list[str] = []

        def fake_resume_thread(thread_id: str, **kwargs):
            resumed.append(thread_id)
            return ThreadSnapshot(
                summary=thread,
                effective_model="gpt-test",
                effective_reasoning_effort=None,
            )

        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.resume_thread = fake_resume_thread

        resolved = handler._feishu_continuation.resolve_resume_target("demo")
        snapshot = handler._feishu_thread_sessions.resume_and_commit_feishu_binding(
            "ou_user",
            "c1",
            resolved.thread_id,
            original_arg="demo",
            summary=resolved,
            failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
        )

        self.assertEqual(snapshot.summary.thread_id, "thread-1")
        self.assertEqual(resumed, ["thread-1"])

    def test_resume_by_name_lists_threads_across_all_providers(self) -> None:
        handler, _ = self._make_handler()
        captured_kwargs = {}
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="vscode",
            status="notLoaded",
            model_provider="provider2_api",
        )

        def fake_list_threads_all(**kwargs):
            captured_kwargs.update(kwargs)
            return [thread]

        handler._adapter.list_threads_all = fake_list_threads_all
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.resume_thread = lambda thread_id, **kwargs: ThreadSnapshot(summary=thread)

        handler._feishu_continuation.resolve_resume_target("demo")

        self.assertEqual(captured_kwargs["model_providers"], [])

    def test_resume_by_name_multiple_matches_returns_error(self) -> None:
        handler, _ = self._make_handler()
        thread_1 = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project-a",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=2,
            source="vscode",
            status="notLoaded",
        )
        thread_2 = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project-b",
            name="demo",
            preview="world",
            created_at=0,
            updated_at=1,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread_1, thread_2]

        with self.assertRaisesRegex(ValueError, "匹配到多个同名线程"):
            handler._feishu_continuation.resolve_resume_target("demo")

    def test_resume_command_for_not_loaded_thread_resumes_directly_and_syncs_runtime_settings(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
            service_name="codex-tui",
        )

        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        state = _runtime_state(handler, "ou_user", "c1")
        state["approval_policy"] = "never"
        state["permissions_profile_id"] = ":danger-full-access"
        state["model"] = "gpt-5.5"
        state["reasoning_effort"] = "high"

        handler.handle_message("ou_user", "c1", "/resume demo")

        _, pending_card = bot.cards[0]
        self.assertEqual(pending_card["header"]["title"]["content"], "Codex 正在恢复线程")
        self.assertIn("正在恢复：`demo`", pending_card["elements"][0]["content"])
        handler._runtime_call(lambda: None)
        self.assertEqual(
            handler._adapter.resume_thread_calls[-1],
            {
                "thread_id": "thread-1",
                "config_overrides": {"model_reasoning_effort": "high"},
                "model": "gpt-5.5",
                "model_provider": None,
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
            },
        )
        self.assertEqual(
            handler._adapter.update_thread_settings_calls[-1],
            {
                "thread_id": "thread-1",
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
                "model": "gpt-5.5",
                "reasoning_effort": "high",
            },
        )

    def test_resume_command_for_unloaded_active_goal_requires_confirm(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )

        handler.handle_message("ou_user", "c1", "/resume demo")

        _, confirm_card = bot.cards[-1]
        self.assertEqual(confirm_card["header"]["title"]["content"], "Codex 恢复线程确认")
        content = confirm_card["elements"][0]["content"]
        self.assertIn("persisted goal 当前是 `active`", content)
        actions = self._first_action(confirm_card)["actions"]
        self.assertEqual([item["text"]["content"] for item in actions], ["按当前设置恢复并保持 paused", "直接恢复"])
        self.assertEqual(handler._adapter.resume_thread_calls, [])

    def test_resume_confirm_pause_active_goal_restores_thread_and_keeps_goal_paused(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        state = _runtime_state(handler, "ou_user", "c1")
        state["approval_policy"] = "never"
        state["permissions_profile_id"] = ":danger-full-access"
        state["model"] = "gpt-5.5"
        state["reasoning_effort"] = "high"

        handler.handle_message("ou_user", "c1", "/resume demo")
        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-resume",
            {
                "action": "resume_thread_confirm",
                "thread_id": "thread-1",
                "thread_title": "demo",
                "pause_active_goal_on_resume": "true",
                "origin": "command",
            },
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 正在恢复线程")
        handler._runtime_call(lambda: None)

        self.assertEqual(
            handler._adapter.resume_thread_calls[-1],
            {
                "thread_id": "thread-1",
                "config_overrides": {"model_reasoning_effort": "high"},
                "model": "gpt-5.5",
                "model_provider": None,
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
            },
        )
        self.assertEqual(
            handler._adapter.operation_log[-3:],
            [
                ("set_thread_goal", "thread-1", "paused"),
                ("resume_thread", "thread-1", "gpt-5.5"),
                ("update_thread_settings", "thread-1", "gpt-5.5"),
            ],
        )
        self.assertEqual(handler._adapter.thread_goals["thread-1"].status, "paused")
        _, final_card = handler._feishu_platform.bot.cards[-1]
        self.assertIn("persisted goal 仍保持 `paused`", final_card["elements"][0]["content"])

    def test_resume_confirm_direct_resume_skips_strict_pause_but_syncs_followup_settings(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        state = _runtime_state(handler, "ou_user", "c1")
        state["approval_policy"] = "never"
        state["permissions_profile_id"] = ":danger-full-access"
        state["model"] = "gpt-5.5"
        state["reasoning_effort"] = "high"

        handler.handle_message("ou_user", "c1", "/resume demo")
        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-resume",
            {
                "action": "resume_thread_confirm",
                "thread_id": "thread-1",
                "thread_title": "demo",
                "pause_active_goal_on_resume": "",
                "origin": "command",
            },
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 正在恢复线程")
        handler._runtime_call(lambda: None)

        self.assertEqual(
            handler._adapter.resume_thread_calls[-1],
            {
                "thread_id": "thread-1",
                "config_overrides": None,
                "model": None,
                "model_provider": None,
                "approval_policy": None,
                "permissions_profile_id": None,
            },
        )
        self.assertEqual(
            handler._adapter.update_thread_settings_calls[-1],
            {
                "thread_id": "thread-1",
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
                "model": "gpt-5.5",
                "reasoning_effort": "high",
            },
        )
        self.assertEqual(handler._adapter.set_thread_goal_calls, [])

    def test_resume_command_ignores_goal_confirm_when_goals_feature_disabled(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)

        def fake_get_thread_goal(thread_id: str):
            raise CodexRpcError("thread/goal/get", {"code": -32602, "message": "goals feature is disabled"})

        handler._adapter.get_thread_goal = fake_get_thread_goal

        handler.handle_message("ou_user", "c1", "/resume demo")

        self.assertTrue(bot.cards)
        self.assertNotEqual(bot.cards[0][1]["header"]["title"]["content"], "Codex 恢复线程确认")
        self.assertEqual(handler._adapter.resume_thread_calls[-1]["thread_id"], "thread-1")

    def test_threads_card_resume_ignores_goal_confirm_when_goals_feature_disabled(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)

        def fake_get_thread_goal(thread_id: str):
            raise CodexRpcError("thread/goal/get", {"code": -32602, "message": "goals feature is disabled"})

        handler._adapter.get_thread_goal = fake_get_thread_goal

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "resume_thread", "thread_id": "thread-1", "thread_title": "demo"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程")
        self.assertIn("正在恢复线程", response["card"]["elements"][0]["content"])

    def test_resume_confirm_pause_active_goal_rolls_back_when_settings_sync_fails(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        handler._adapter.update_thread_settings = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sync failed"))

        handler.handle_message("ou_user", "c1", "/resume demo")
        self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-resume",
            {
                "action": "resume_thread_confirm",
                "thread_id": "thread-1",
                "thread_title": "demo",
                "pause_active_goal_on_resume": "true",
                "origin": "command",
            },
        ))
        handler._runtime_call(lambda: None)

        self.assertEqual(
            handler._adapter.set_thread_goal_calls[-2:],
            [
                {
                    "thread_id": "thread-1",
                    "objective": None,
                    "status": "paused",
                    "token_budget": None,
                },
                {
                    "thread_id": "thread-1",
                    "objective": None,
                    "status": "active",
                    "token_budget": None,
                },
            ],
        )
        self.assertEqual(handler._adapter.thread_goals["thread-1"].status, "active")
        self.assertIn("恢复线程后同步当前会话设置失败", bot.replies[-1][1])

    def test_threads_card_mentions_global_resume_scope(self) -> None:
        handler, bot = self._make_handler()
        captured_kwargs = {}

        def fake_list_threads_all(**kwargs):
            captured_kwargs.update(kwargs)
            return []

        handler._adapter.list_threads_all = fake_list_threads_all

        handler.handle_message("ou_user", "c1", "/threads")

        self.assertEqual(captured_kwargs["model_providers"], [])
        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        content = card["elements"][0]["content"]
        self.assertIn("跨 provider 汇总", content)
        self.assertIn(f"`{_DISPLAY_RESUME_COMMAND}`", content)
        self.assertIn(f"`{_DISPLAY_LOCAL_RESUME_COMMAND}`", content)
        self.assertIn("`focusctl thread list --scope cwd`", content)

    def test_threads_card_uses_trisection_layout_for_row_actions(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        handler.handle_message("ou_user", "c1", "/threads")

        _, card = bot.cards[0]
        action_elements = self._action_elements(card)
        row_action = action_elements[0]
        self.assertEqual(row_action["layout"], "trisection")
        self.assertEqual(len(row_action["actions"]), 2)
        self.assertEqual(row_action["actions"][0]["text"]["content"], "恢复")
        self.assertEqual(row_action["actions"][1]["text"]["content"], "归档")
        bottom_action = action_elements[-1]
        self.assertTrue(any(btn["text"]["content"] == "收起" for btn in bottom_action["actions"]))

    def test_threads_card_marks_current_thread_in_button_text(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        state = _runtime_state(handler, "ou_user", "c1")
        with handler._lock:
            state["current_thread_id"] = "thread-1"
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        handler.handle_message("ou_user", "c1", "/threads")

        _, card = bot.cards[0]
        self.assertNotIn("**当前**", card["elements"][2]["content"])
        row_action = self._action_elements(card)[0]
        self.assertEqual(row_action["actions"][0]["text"]["content"], "当前")
        self.assertEqual(row_action["actions"][0]["type"], "primary")

    def test_threads_command_rejects_extra_args(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/threads extra")

        self.assertEqual(bot.cards, [])
        self.assertIn("用法：`/threads`", bot.replies[-1][1])
        self.assertIn("不接受额外参数", bot.replies[-1][1])

    def test_named_instance_threads_command_shares_global_current_dir_threads(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir, instance_name="corp-b")
        runtime = handler._binding_runtime.resolve_session("ou_user", "c1")
        thread_1 = ThreadSummary(
            thread_id="thread-1",
            cwd=runtime.working_dir,
            name="one",
            preview="hello",
            created_at=0,
            updated_at=2,
            source="cli",
            status="idle",
        )
        thread_2 = ThreadSummary(
            thread_id="thread-2",
            cwd=runtime.working_dir,
            name="two",
            preview="world",
            created_at=0,
            updated_at=1,
            source="cli",
            status="idle",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread_1, thread_2]

        handler.handle_message("ou_user", "c1", "/threads")

        _, card = bot.cards[-1]
        content = "\n".join(
            element["content"]
            for element in card["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("thread-1", content)
        self.assertIn("thread-2", content)

    def test_named_instance_resume_accepts_global_thread_id(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, _ = self._make_handler(data_dir=data_dir, instance_name="corp-b")
        thread = ThreadSummary(
            thread_id="019d2e94-a475-7bc1-b2f7-a3ce37628ede",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._adapter.thread_snapshots[(thread.thread_id, None)] = ThreadSnapshot(summary=thread)

        snapshot = handler._feishu_thread_sessions.resume_and_commit_feishu_binding(
            "ou_user",
            "c1",
            thread.thread_id,
            original_arg=thread.thread_id,
            summary=thread,
            failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
        )

        self.assertEqual(snapshot.summary.thread_id, thread.thread_id)
        self.assertEqual(handler._adapter.resume_thread_calls[-1]["thread_id"], thread.thread_id)

    def test_close_threads_card_action_returns_closed_card(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "close_threads_card"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程（已收起）")
        action = self._first_action(response["card"])
        self.assertEqual(action["actions"][0]["text"]["content"], "展开线程列表")

    def test_reopen_threads_card_action_returns_threads_card(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "reopen_threads_card"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程")

    def test_show_more_threads_action_expands_all_rows(self) -> None:
        handler, _ = self._make_handler({"threads_initial_limit": 1})
        threads = [
            ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="one",
                preview="",
                created_at=0,
                updated_at=3,
                source="cli",
                status="idle",
            ),
            ThreadSummary(
                thread_id="thread-2",
                cwd="/tmp/project",
                name="two",
                preview="",
                created_at=0,
                updated_at=2,
                source="cli",
                status="idle",
            ),
            ThreadSummary(
                thread_id="thread-3",
                cwd="/tmp/project",
                name="three",
                preview="",
                created_at=0,
                updated_at=1,
                source="cli",
                status="idle",
            ),
        ]
        handler._adapter.list_threads_all = lambda **kwargs: threads

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "show_more_threads"},
        ))

        content = "\n".join(
            element.get("content", "")
            for element in response["card"]["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("thread-1", content)
        self.assertIn("thread-2", content)
        self.assertIn("thread-3", content)
        bottom_action = self._action_elements(response["card"])[-1]
        self.assertFalse(any(btn["text"]["content"] == "更多" for btn in bottom_action["actions"]))
        self.assertEqual(response["toast"], "已展开全部线程。")

    def test_expanded_threads_card_stays_expanded_after_archive(self) -> None:
        handler, _ = self._make_handler({"threads_initial_limit": 1})
        threads = [
            ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="one",
                preview="",
                created_at=0,
                updated_at=3,
                source="cli",
                status="idle",
            ),
            ThreadSummary(
                thread_id="thread-2",
                cwd="/tmp/project",
                name="two",
                preview="",
                created_at=0,
                updated_at=2,
                source="cli",
                status="idle",
            ),
            ThreadSummary(
                thread_id="thread-3",
                cwd="/tmp/project",
                name="three",
                preview="",
                created_at=0,
                updated_at=1,
                source="cli",
                status="idle",
            ),
        ]

        def _list_threads_all(**kwargs):
            return [thread for thread in threads if thread.thread_id != "thread-3"]

        handler._adapter.list_threads_all = lambda **kwargs: threads
        handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "show_more_threads"},
        )
        handler._adapter.list_threads_all = _list_threads_all
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(
            summary=next(thread for thread in threads if thread.thread_id == thread_id)
        )

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "archive_thread", "thread_id": "thread-3"},
        ))

        content = "\n".join(
            element.get("content", "")
            for element in response["card"]["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("thread-1", content)
        self.assertIn("thread-2", content)
        self.assertNotIn("thread-3", content)
        bottom_action = self._action_elements(response["card"])[-1]
        self.assertFalse(any(btn["text"]["content"] == "更多" for btn in bottom_action["actions"]))

    def test_expanded_threads_card_stays_expanded_after_rename(self) -> None:
        handler, _ = self._make_handler({"threads_initial_limit": 1})
        threads = [
            ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="one",
                preview="",
                created_at=0,
                updated_at=3,
                source="cli",
                status="idle",
            ),
            ThreadSummary(
                thread_id="thread-2",
                cwd="/tmp/project",
                name="two",
                preview="",
                created_at=0,
                updated_at=2,
                source="cli",
                status="idle",
            ),
            ThreadSummary(
                thread_id="thread-3",
                cwd="/tmp/project",
                name="three",
                preview="",
                created_at=0,
                updated_at=1,
                source="cli",
                status="idle",
            ),
        ]

        def _rename_thread(thread_id: str, name: str) -> None:
            for index, thread in enumerate(threads):
                if thread.thread_id == thread_id:
                    threads[index] = ThreadSummary(
                        thread_id=thread.thread_id,
                        cwd=thread.cwd,
                        name=name,
                        preview=thread.preview,
                        created_at=thread.created_at,
                        updated_at=thread.updated_at,
                        source=thread.source,
                        status=thread.status,
                        active_flags=list(thread.active_flags),
                        path=thread.path,
                        model_provider=thread.model_provider,
                        service_name=thread.service_name,
                    )
                    return
            raise AssertionError(f"unexpected thread_id: {thread_id}")

        def _read_thread(thread_id: str, include_turns: bool = False) -> ThreadSnapshot:
            del include_turns
            thread = next(item for item in threads if item.thread_id == thread_id)
            return ThreadSnapshot(summary=thread)

        handler._adapter.list_threads_all = lambda **kwargs: threads
        handler._adapter.read_thread = _read_thread
        handler._adapter.rename_thread = _rename_thread

        handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "show_more_threads"},
        )
        handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "show_rename_form", "thread_id": "thread-2"},
        )

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {
                "action": "rename_thread",
                "thread_id": "thread-2",
                "_form_value": {"rename_title": "two-renamed"},
            },
        ))

        content = "\n".join(
            element.get("content", "")
            for element in response["card"]["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("thread-1", content)
        self.assertIn("thread-2", content)
        self.assertIn("thread-3", content)
        self.assertIn("two-renamed", content)
        bottom_action = self._action_elements(response["card"])[-1]
        self.assertFalse(any(btn["text"]["content"] == "更多" for btn in bottom_action["actions"]))
        self.assertEqual(response["toast"], "已重命名。")

    def test_resume_target_on_runtime_submit_refreshes_threads_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
            service_name="codex-tui",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        handler._runtime_submit(
            handler._threads_ui_domain._resume_target_on_runtime,
            "ou_user",
            "c1",
            "thread-1",
            original_arg="thread-1",
            summary=thread,
            message_id="msg-session",
            refresh_threads_message_id="msg-session",
        )
        handler._runtime_call(lambda: None)

        self.assertTrue(any(message_id == "msg-session" for message_id, _ in bot.patches))

    def test_resume_target_on_runtime_rejects_if_binding_became_running_before_runtime_executes(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)
        state = _runtime_state(handler, "ou_user", "c1")
        started = threading.Event()
        release = threading.Event()

        def block_runtime() -> None:
            started.set()
            self.assertTrue(release.wait(timeout=1))

        handler._runtime_submit(block_runtime)
        self.assertTrue(started.wait(timeout=1))
        handler._runtime_submit(
            handler._threads_ui_domain._resume_target_on_runtime,
            "ou_user",
            "c1",
            "thread-1",
            summary=thread,
            pause_active_goal_on_resume=True,
            message_id="msg-session",
        )
        with handler._lock:
            _set_pages(state, current_message_id="msg-turn")
            state["current_turn_id"] = "turn-1"
            state["running"] = True
            state["awaiting_local_turn_started"] = False
        release.set()
        handler._runtime_call(lambda: None)

        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(handler._adapter.set_thread_goal_calls, [])
        self.assertEqual(handler._adapter.thread_goals["thread-1"].status, "active")
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["current_thread_id"], "")
        self.assertIn("当前线程仍在执行，暂不切换。", bot.replies[-1][1])

    def test_expanded_threads_card_stays_expanded_after_resume_refresh(self) -> None:
        handler, bot = self._make_handler({"threads_initial_limit": 1})
        threads = [
            ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="one",
                preview="",
                created_at=0,
                updated_at=3,
                source="cli",
                status="notLoaded",
            ),
            ThreadSummary(
                thread_id="thread-2",
                cwd="/tmp/project",
                name="two",
                preview="",
                created_at=0,
                updated_at=2,
                source="cli",
                status="idle",
            ),
            ThreadSummary(
                thread_id="thread-3",
                cwd="/tmp/project",
                name="three",
                preview="",
                created_at=0,
                updated_at=1,
                source="cli",
                status="idle",
            ),
        ]
        def _read_thread(thread_id: str, include_turns: bool = False) -> ThreadSnapshot:
            del include_turns
            thread = next(item for item in threads if item.thread_id == thread_id)
            return ThreadSnapshot(summary=thread)

        handler._adapter.list_threads_all = lambda **kwargs: threads
        handler._adapter.read_thread = _read_thread
        handler._adapter.resume_thread = lambda thread_id, **kwargs: ThreadSnapshot(summary=_read_thread(thread_id).summary)

        handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "show_more_threads"},
        )

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "resume_thread", "thread_id": "thread-1", "thread_title": "one"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程")
        pending_content = response["card"]["elements"][0]["content"]
        self.assertIn("正在恢复线程", pending_content)
        self.assertNotIn("toast", response)
        handler._runtime_call(lambda: None)

        patched = json.loads(next(content for message_id, content in bot.patches if message_id == "msg-session"))
        content = "\n".join(
            element.get("content", "")
            for element in patched["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("thread-1", content)
        self.assertIn("thread-2", content)
        self.assertIn("thread-3", content)
        bottom_action = self._action_elements(patched)[-1]
        self.assertFalse(any(btn["text"]["content"] == "更多" for btn in bottom_action["actions"]))

    def test_help_overview_is_layered(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 工作台")
        content = card["elements"][0]["content"]
        self.assertIn("目录：", content)
        self.assertIn("线程：`未绑定`", content)
        self.assertIn("推送：`", content)
        self.assertIn("本轮：权限 `Full` | 模型 `Auto` | 推理 `Auto`", content)
        action_elements = self._action_elements(card)
        self.assertEqual(action_elements[0]["layout"], "bisected")
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["开始", "线程设置"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["本轮设置", "连接状态"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["群聊设置", "更多"],
        )

    def test_commands_lists_common_navigation_commands(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/commands")

        reply = bot.replies[-1][1]
        self.assertIn("常用命令列表", reply)
        self.assertIn("`/commands`", reply)
        self.assertIn("`/help [overview|start|thread-settings|turn|connection|group|more]`", reply)
        self.assertIn("`/status`", reply)
        self.assertIn("`/goal [show|text|set 〈objective〉|pause|resume|clear]`", reply)
        self.assertIn("`/compact`", reply)
        self.assertIn("`/detach`", reply)
        self.assertIn("`/attach [binding|thread|service]`", reply)
        self.assertIn(f"`{_DISPLAY_RESUME_COMMAND}`", reply)
        self.assertIn("`/group-mode [assistant|mention-only|all]`", reply)
        self.assertIn("`/reset-backend`", reply)
        self.assertIn("`/last text`", reply)
        self.assertIn("`/model [name|auto]`", reply)
        self.assertIn("`/effort [auto|value]`", reply)
        self.assertIn(f"`{_DISPLAY_INIT_COMMAND}`", reply)
        self.assertIn(f"`{_DISPLAY_DEBUG_CONTACT_COMMAND}`", reply)
        self.assertNotIn("`/cancel`", reply)

    def test_commands_rejects_extra_args(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/commands extra")

        self.assertIn("用法：`/commands`", bot.replies[-1][1])
