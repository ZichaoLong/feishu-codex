import json
import threading

from tests.focus_runtime.codex_handler_fakes import (
    _bind_authoritative_thread,
)
from tests.focus_runtime.codex_handler_fakes import _runtime_state
from tests.execution_page_test_support import set_execution_page_state as _set_pages
from bot.cards import build_ask_user_card, build_execution_card
from bot.adapters.base import (
    ThreadGoalSummary,
    ThreadSnapshot,
    ThreadSummary,
)
from bot.codex_protocol.client import (
    CodexRpcError,
)
from bot.execution_transcript import ExecutionReplySegment

from tests.focus_runtime.codex_handler_test_harness import (
    CodexHandlerHarness,
)


class CodexHandlerGoalTests(CodexHandlerHarness):
    def test_turn_plan_updated_sends_then_patches_plan_card(self) -> None:
        handler, bot = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        with handler._lock:
            _set_pages(state, current_message_id="exec-1")
            state["current_turn_id"] = "turn-1"

        self._dispatch_adapter_notification(
            handler,
            "turn/plan/updated",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "explanation": "先规划再执行。",
                "plan": [{"step": "确认需求", "status": "pending"}],
            }
        )

        self.assertEqual(len(bot.reply_refs), 1)
        first_card = json.loads(bot.reply_refs[0][2])
        self.assertEqual(first_card["header"]["title"]["content"], "Codex 计划 turn-1…")
        self.assertTrue(
            any("确认需求" in element.get("content", "") for element in first_card["elements"])
        )

        self._dispatch_adapter_notification(
            handler,
            "turn/plan/updated",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "explanation": "先规划再执行。",
                "plan": [{"step": "确认需求", "status": "completed"}],
            }
        )

        self.assertEqual(len(bot.patches), 1)
        patched_card = json.loads(bot.patches[0][1])
        self.assertTrue(
            any("[x] 确认需求" in element.get("content", "") for element in patched_card["elements"])
        )

    def test_plan_item_completion_sends_plan_card(self) -> None:
        handler, bot = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        with handler._lock:
            _set_pages(state, current_message_id="exec-1")
            state["current_turn_id"] = "turn-1"

        self._dispatch_adapter_notification(
            handler,
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"type": "plan", "text": "1. 先确认需求\n2. 再实现"},
            }
        )

        self.assertEqual(len(bot.reply_refs), 1)
        card = json.loads(bot.reply_refs[0][2])
        self.assertIn("计划正文", card["elements"][0]["content"])
        self.assertIn("先确认需求", card["elements"][0]["content"])

    def test_custom_user_input_is_hidden_for_option_only_questions(self) -> None:
        card = build_ask_user_card(
            "req-1",
            [
                {
                    "id": "q1",
                    "header": "步骤确认",
                    "question": "请选择下一步。",
                    "options": [{"label": "确认步骤", "description": ""}, {"label": "暂缓步骤", "description": ""}],
                    "isOther": False,
                }
            ],
        )

        self.assertFalse(any(element.get("tag") == "form" for element in card["elements"]))

    def test_execution_card_uses_process_title_without_help_hint(self) -> None:
        card = build_execution_card("", [], running=True)

        self.assertEqual(card["header"]["title"]["content"], "Codex 执行过程（执行中）")
        self.assertNotIn("/help", json.dumps(card, ensure_ascii=False))

    def test_terminal_empty_execution_card_shows_minimal_placeholder(self) -> None:
        card = build_execution_card("", [], running=False)

        self.assertEqual(card["header"]["title"]["content"], "Codex 执行过程")
        self.assertEqual(card["body"]["elements"], [{"tag": "markdown", "content": "无"}])

    def test_execution_card_sanitizes_embedded_image_markdown_in_runtime_text(self) -> None:
        card = build_execution_card(
            "命令输出：![日志图](/tmp/log.png)",
            [ExecutionReplySegment("assistant", "![示意图](/tmp/demo.png)\n\n已生成。")],
            running=False,
        )

        card_json = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("![示意图](/tmp/demo.png)", card_json)
        self.assertNotIn("![日志图](/tmp/log.png)", card_json)
        self.assertIn("【图片】示意图", card_json)
        self.assertIn("路径：`/tmp/demo.png`", card_json)
        self.assertIn("路径：`/tmp/log.png`", card_json)

    def test_execution_card_sanitizes_markdown_links_to_visible_urls(self) -> None:
        card = build_execution_card(
            "参考：[示例地图链接](https://maps.example.invalid/shanghai/live)",
            [
                ExecutionReplySegment(
                    "assistant",
                    "[示例扩散条件图](https://weather.example.invalid/china/dispersion-24h)",
                )
            ],
            running=False,
        )

        card_json = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("[示例地图链接](", card_json)
        self.assertNotIn("[示例扩散条件图](", card_json)
        self.assertIn("示例地图链接 (https://maps.example.invalid/shanghai/live)", card_json)
        self.assertIn(
            "示例扩散条件图 (https://weather.example.invalid/china/dispersion-24h)",
            card_json,
        )

    def test_execution_card_sanitizes_markdown_headings_to_visible_labels(self) -> None:
        card = build_execution_card(
            "# 过程标题",
            [ExecutionReplySegment("assistant", "## 回复小节\n\n- 条目")],
            running=False,
        )

        card_json = json.dumps(card, ensure_ascii=False)
        self.assertIn("【标题】 过程标题", card_json)
        self.assertIn("【小节】 回复小节", card_json)
        self.assertNotIn("# 过程标题", card_json)
        self.assertNotIn("## 回复小节", card_json)

    def test_status_includes_user_facing_summary(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/status")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 当前状态")
        content = card["elements"][0]["content"]
        self.assertIn("权限基线：`Danger Full Access`", content)
        self.assertIn("审批策略：`never`", content)
        self.assertNotIn("Codex 协作模式", content)
        self.assertIn("Codex effort override：`auto`", content)
        self.assertNotIn("新 thread seed profile", content)
        self.assertNotIn("当前 provider", content)
        self.assertNotIn("binding：", content)

    def test_status_hides_runtime_debug_fields(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        handler.handle_message("ou_user", "c1", "/status")

        _, card = bot.cards[-1]
        content = card["elements"][0]["content"]
        self.assertIn("权限基线：`Danger Full Access`", content)
        self.assertNotIn("Codex 协作模式", content)
        self.assertIn("Codex effort override：`auto`", content)
        self.assertNotIn("startup profile", content)
        self.assertNotIn("binding：", content)
        self.assertNotIn("feishu runtime：", content)
        self.assertNotIn("backend thread status：", content)
        self.assertNotIn("交互 owner：", content)
        self.assertNotIn("re-profile possible：", content)
        self.assertNotIn("unsubscribe：", content)
        self.assertNotIn("当前直接提问：", content)

    def test_bind_thread_backfills_goal_projection_from_backend(self) -> None:
        handler, _ = self._make_handler()
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
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )

        _bind_authoritative_thread(handler, "ou_user", "c1", thread)

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["goal_objective"], "ship goal support")
        self.assertEqual(state["goal_status"], "active")
        self.assertEqual(state["goal_token_budget"], 100)

    def test_status_shows_goal_summary_when_available(self) -> None:
        handler, bot = self._make_handler()
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
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )

        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler.handle_message("ou_user", "c1", "/status")

        _, card = bot.cards[-1]
        content = card["elements"][0]["content"]
        self.assertIn("当前 goal：`active`", content)
        self.assertIn("goal 摘要：预算：`100`；已用 tokens：`12`；时长：`34s`", content)

    def test_goal_read_rejects_thread_spawn_before_goal_rpc(self) -> None:
        handler, _ = self._make_handler()
        child = ThreadSummary(
            thread_id="child-1",
            cwd="/tmp/project",
            name="child",
            preview="",
            created_at=0,
            updated_at=0,
            source="subAgent",
            status="idle",
            parent_thread_id="root-1",
            subagent_kind="threadSpawn",
        )
        handler._adapter.thread_snapshots[("child-1", None)] = ThreadSnapshot(
            summary=child
        )
        handler._adapter.get_thread_goal = lambda _thread_id: (_ for _ in ()).throw(
            AssertionError("ThreadSpawn must be rejected before any goal RPC")
        )

        with self.assertRaisesRegex(ValueError, "ThreadSpawn"):
            handler._feishu_continuation.get_thread_goal("child-1")

        self.assertEqual(
            handler._adapter.read_thread_calls,
            [{"thread_id": "child-1", "include_turns": False}],
        )

    def test_goal_command_supports_show_set_pause_resume_and_clear(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)

        handler.handle_message("ou_user", "c1", "/goal")
        _, show_card = bot.cards[-1]
        self.assertIn("当前 thread 暂无 goal。", show_card["elements"][0]["content"])

        handler.handle_message("ou_user", "c1", "/goal set ship goal support")
        _, set_card = bot.cards[-1]
        self.assertIn("已设置当前 thread goal。", set_card["elements"][0]["content"])
        self.assertIn("目标：ship goal support", set_card["elements"][0]["content"])
        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["goal_objective"], "ship goal support")
        self.assertEqual(state["goal_status"], "active")
        self.assertTrue(
            handler._runtime_call(
                handler._feishu_root_operations.reconcile_terminal,
                "thread-1",
            )
        )

        card_count = len(bot.cards)
        handler.handle_message("ou_user", "c1", "/goal text")
        self.assertEqual(len(bot.cards), card_count)
        goal_text = bot.replies[-1][1]
        self.assertIn("thread: thread-1", goal_text)
        self.assertIn("title: demo", goal_text)
        self.assertIn("status: active", goal_text)
        self.assertIn("objective:\nship goal support", goal_text)

        handler.handle_message("ou_user", "c1", "/goal pause")
        _, pause_card = bot.cards[-1]
        self.assertIn("状态：`paused`", pause_card["elements"][0]["content"])
        self.assertEqual(state["goal_status"], "paused")

        pending_count = len(bot.cards)
        handler.handle_message("ou_user", "c1", "/goal resume")
        pending_cards = [
            card
            for _, card in bot.cards[pending_count:]
            if "正在同步 thread、goal 与当前会话设置" in card["elements"][0]["content"]
        ]
        self.assertTrue(pending_cards)
        handler._runtime_call(lambda: None)
        _, resume_card = bot.cards[-1]
        self.assertIn("状态：`active`", resume_card["elements"][0]["content"])
        self.assertEqual(state["goal_status"], "active")
        self.assertTrue(
            handler._runtime_call(
                handler._feishu_root_operations.reconcile_terminal,
                "thread-1",
            )
        )

        handler.handle_message("ou_user", "c1", "/goal clear")
        _, clear_card = bot.cards[-1]
        self.assertIn("已清除当前 thread goal。", clear_card["elements"][0]["content"])
        self.assertEqual(state["goal_objective"], "")
        self.assertEqual(state["goal_status"], "")

    def test_goal_card_action_can_pause_and_clear_goal(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
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
        handler._feishu_continuation.project_goal(
            "ou_user",
            "c1",
            "",
            handler._adapter.thread_goals["thread-1"],
        )

        pause_response = self._unpack_card_response(
            handler.handle_card_action("ou_user", "c1", "msg-goal", {"action": "goal_pause"})
        )
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["goal_status"], "paused")
        self.assertEqual(pause_response["toast"], "已暂停 goal。")
        self.assertIn("状态：`paused`", pause_response["card"]["elements"][0]["content"])

        clear_response = self._unpack_card_response(
            handler.handle_card_action("ou_user", "c1", "msg-goal", {"action": "goal_clear"})
        )
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["goal_objective"], "")
        self.assertEqual(clear_response["toast"], "已清除 goal。")
        self.assertIn("当前 thread 暂无 goal。", clear_response["card"]["elements"][0]["content"])

    def test_goal_set_detached_requires_confirm_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        state = _runtime_state(handler, "ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        handler.handle_message("ou_user", "c1", "/goal set ship goal support")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex Goal")
        content = card["elements"][0]["content"]
        self.assertIn("当前会话处于 `detached`。", content)
        self.assertIn("目标：ship goal support", content)
        actions = self._first_action(card)["actions"]
        self.assertEqual([item["text"]["content"] for item in actions], ["恢复推送并继续", "保持 detached"])
        self.assertNotIn("thread-1", handler._adapter.thread_goals)

    def test_goal_resume_detached_without_goal_fails_before_confirm_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        state = _runtime_state(handler, "ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        handler.handle_message("ou_user", "c1", "/goal resume")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex Goal 操作失败")
        self.assertIn("当前 thread 没有可恢复的 goal。", card["elements"][0]["content"])

    def test_goal_resume_detached_with_goals_disabled_fails_before_confirm_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        state = _runtime_state(handler, "ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        def fake_get_thread_goal(thread_id: str):
            raise CodexRpcError("thread/goal/get", {"code": -32602, "message": "goals feature is disabled"})

        handler._adapter.get_thread_goal = fake_get_thread_goal

        handler.handle_message("ou_user", "c1", "/goal resume")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex Goal 操作失败")
        self.assertIn("当前 backend 未启用 goal 功能。", card["elements"][0]["content"])

    def test_goal_resume_detached_confirm_can_keep_detached(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="paused",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        handler._feishu_continuation.project_goal(
            "ou_user",
            "c1",
            "",
            handler._adapter.thread_goals["thread-1"],
        )
        state = _runtime_state(handler, "ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        confirm_response = self._unpack_card_response(
            handler.handle_card_action("ou_user", "c1", "msg-goal", {"action": "goal_resume"})
        )
        self.assertIn("当前会话处于 `detached`。", confirm_response["card"]["elements"][0]["content"])
        self.assertIn("状态：`active`", confirm_response["card"]["elements"][0]["content"])

        apply_response = self._unpack_card_response(
            handler.handle_card_action(
                "ou_user",
                "c1",
                "msg-goal",
                {
                    "action": "goal_apply_confirm",
                    "thread_id": "thread-1",
                    "status": "active",
                    "attach_binding": "",
                },
            )
        )
        self.assertIn("正在同步 thread、goal 与当前会话设置", apply_response["card"]["elements"][0]["content"])
        handler._runtime_call(lambda: None)
        _, final_card = handler._feishu_platform.bot.cards[-1]
        self.assertIn("当前 thread goal。", final_card["elements"][0]["content"])
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"], "detached")
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["goal_status"], "active")
        self.assertTrue(
            self._feishu_root_snapshot(
                handler,
                "thread-1",
            ).submission_outcome_unknown
        )

    def test_detached_goal_resume_blank_owner_commit_failure_compensates(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship safely",
            status="paused",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
        )
        state = _runtime_state(handler, "ou_user", "c1")
        state["feishu_runtime_state"] = "detached"
        handler._service_runtime_authority.release_service_thread_runtime_lease(
            "thread-1"
        )
        handler._feishu_root_operations.commit_resume_owner = (
            lambda _admission: (_ for _ in ()).throw(
                TimeoutError("submission owner commit timed out")
            )
        )

        handler._runtime_call(
            handler._goal_domain.resume_goal_on_runtime,
            "ou_user",
            "c1",
            "thread-1",
            False,
        )

        self.assertEqual(handler._adapter.unsubscribe_thread_calls, ["thread-1"])
        self.assertEqual(self._service_runtime_holder_ids(handler, "thread-1"), ())
        self._wait_until(
            lambda: handler._interaction_lease_store.load("thread-1") is None
        )
        self.assertIsNone(handler._interaction_lease_store.load("thread-1"))
        self.assertEqual(handler._adapter.thread_goals["thread-1"].status, "paused")
        self.assertEqual(state["feishu_runtime_state"], "detached")
        self.assertIn("submission owner commit timed out", bot.cards[-1][1]["elements"][0]["content"])

    def test_detached_goal_resume_owner_failure_retains_exact_local_effect(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="continue later",
            status="awaitingExternalResult",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
        )
        state = _runtime_state(handler, "ou_user", "c1")
        state["feishu_runtime_state"] = "detached"
        handler._service_runtime_authority.release_service_thread_runtime_lease(
            "thread-1"
        )
        handler._feishu_root_operations.commit_resume_owner = (
            lambda _admission: (_ for _ in ()).throw(
                RuntimeError("submission owner commit failed")
            )
        )

        handler._runtime_call(
            handler._goal_domain.resume_goal_on_runtime,
            "ou_user",
            "c1",
            "thread-1",
            False,
        )

        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])
        self.assertTrue(self._service_runtime_holder_ids(handler, "thread-1"))
        self.assertEqual(
            self._feishu_root_snapshot(handler, "thread-1").pending_admission_count,
            1,
        )
        self.assertEqual(
            len(
                self._feishu_root_snapshot(
                    handler,
                    "thread-1",
                ).continuation_generations
            ),
            1,
        )
        self.assertEqual(state["feishu_runtime_state"], "detached")
        self.assertIn("submission owner commit failed", bot.cards[-1][1]["elements"][0]["content"])

    def test_goal_resume_cold_thread_injects_runtime_permissions_and_updates_loaded_settings(self) -> None:
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
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="paused",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        state = _runtime_state(handler, "ou_user", "c1")
        state["approval_policy"] = "on-request"
        state["permissions_profile_id"] = ":workspace"
        state["model"] = "gpt-5.4"
        state["reasoning_effort"] = "high"

        handler.handle_message("ou_user", "c1", "/goal resume")
        handler._runtime_call(lambda: None)

        self.assertEqual(
            handler._adapter.resume_thread_calls[-1],
            {
                "thread_id": "thread-1",
                "config_overrides": {"model_reasoning_effort": "high"},
                "model": "gpt-5.4",
                "model_provider": None,
                "approval_policy": "on-request",
                "permissions_profile_id": ":workspace",
            },
        )
        self.assertEqual(
            handler._adapter.update_thread_settings_calls[-1],
            {
                "thread_id": "thread-1",
                "approval_policy": "on-request",
                "permissions_profile_id": ":workspace",
                "model": "gpt-5.4",
                "reasoning_effort": "high",
            },
        )
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["goal_status"], "active")
        self.assertIn("状态：`active`", bot.cards[-1][1]["elements"][0]["content"])

    def test_goal_resume_cold_active_goal_pauses_before_resume_then_reactivates(self) -> None:
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
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
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

        handler.handle_message("ou_user", "c1", "/goal resume")
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
        self.assertEqual(
            handler._adapter.operation_log[-4:],
            [
                ("set_thread_goal", "thread-1", "paused"),
                ("resume_thread", "thread-1", "gpt-5.5"),
                ("update_thread_settings", "thread-1", "gpt-5.5"),
                ("set_thread_goal", "thread-1", "active"),
            ],
        )
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["goal_status"], "active")

    def test_goal_resume_cold_active_goal_rolls_back_pause_on_failure(self) -> None:
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
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
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

        handler.handle_message("ou_user", "c1", "/goal resume")
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
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["goal_status"], "active")
        self.assertIn("sync failed", bot.cards[-1][1]["elements"][0]["content"])

    def test_goal_resume_unknown_restore_is_not_settled_twice(self) -> None:
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
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
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
        original_set_thread_goal = handler._adapter.set_thread_goal

        def set_thread_goal(thread_id: str, **kwargs):
            if kwargs.get("status") == "active":
                raise TimeoutError("restore outcome unknown")
            return original_set_thread_goal(thread_id, **kwargs)

        handler._adapter.set_thread_goal = set_thread_goal
        handler._adapter.update_thread_settings = lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(RuntimeError("settings sync failed"))
        settlements = []
        original_settle_failure = handler._feishu_resume_settlement.settle_failure

        def settle_failure(command):
            settlements.append(command)
            return original_settle_failure(command)

        handler._feishu_resume_settlement.settle_failure = settle_failure

        handler.handle_message("ou_user", "c1", "/goal resume")
        handler._runtime_call(lambda: None)

        self.assertEqual(len(settlements), 1)
        self.assertEqual(settlements[0].owner_disposition, "leave_unchanged")
        self.assertTrue(
            self._feishu_root_snapshot(
                handler,
                "thread-1",
            ).submission_outcome_unknown
        )

    def test_goal_resume_fails_closed_when_goals_feature_disabled(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)

        def fake_get_thread_goal(thread_id: str):
            raise CodexRpcError("thread/goal/get", {"code": -32602, "message": "goals feature is disabled"})

        handler._adapter.get_thread_goal = fake_get_thread_goal

        pending_count = len(bot.cards)
        handler.handle_message("ou_user", "c1", "/goal resume")

        self.assertEqual(len(bot.cards), pending_count + 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex Goal 操作失败")
        self.assertIn("当前 backend 未启用 goal 功能。", card["elements"][0]["content"])

    def test_goal_resume_card_action_acknowledges_immediately_then_attaches_in_background(self) -> None:
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
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="paused",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        state = _runtime_state(handler, "ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        response = self._unpack_card_response(
            handler.handle_card_action(
                "ou_user",
                "c1",
                "msg-goal",
                {
                    "action": "goal_apply_confirm",
                    "thread_id": "thread-1",
                    "status": "active",
                    "attach_binding": "true",
                },
            )
        )

        self.assertIn("正在同步 thread、goal 与当前会话设置", response["card"]["elements"][0]["content"])
        handler._runtime_call(lambda: None)

        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"], "attached")
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["goal_status"], "active")

    def test_goal_apply_confirm_fast_ack_bypasses_busy_runtime_queue(self) -> None:
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
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="paused",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        state = _runtime_state(handler, "ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        blocker_started = threading.Event()
        blocker_release = threading.Event()
        handler._runtime_submit(
            lambda: (
                blocker_started.set(),
                blocker_release.wait(2),
            )
        )
        self.assertTrue(blocker_started.wait(1))

        response_holder: dict[str, dict] = {}

        def invoke() -> None:
            response_holder["response"] = self._unpack_card_response(
                handler.handle_card_action(
                    "ou_user",
                    "c1",
                    "msg-goal",
                    {
                        "action": "goal_apply_confirm",
                        "thread_id": "thread-1",
                        "status": "active",
                        "attach_binding": "true",
                    },
                )
            )

        worker = threading.Thread(target=invoke)
        worker.start()
        worker.join(timeout=0.2)
        self.assertFalse(worker.is_alive())
        self.assertIn(
            "正在同步 thread、goal 与当前会话设置",
            response_holder["response"]["card"]["elements"][0]["content"],
        )
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"], "detached")

        blocker_release.set()
        worker.join(timeout=1)
        handler._runtime_call(lambda: None)

        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"], "attached")
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["goal_status"], "active")

    def test_goal_set_detached_confirm_can_attach_before_apply(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        state = _runtime_state(handler, "ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        apply_response = self._unpack_card_response(
            handler.handle_card_action(
                "ou_user",
                "c1",
                "msg-goal",
                {
                    "action": "goal_apply_confirm",
                    "thread_id": "thread-1",
                    "objective": "ship goal support",
                    "attach_binding": "true",
                },
            )
        )
        self.assertEqual(apply_response["toast"], "已更新 goal 并恢复当前会话推送。")
        self.assertIn("当前会话已恢复接收该 thread 的飞书推送。", apply_response["card"]["elements"][0]["content"])
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"], "attached")
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["goal_objective"], "ship goal support")
