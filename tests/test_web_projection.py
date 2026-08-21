import base64
import json
import unittest

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.focus_web_wire_catalog import FOCUS_WEB_RECORD_BY_NAME
from bot.web_runtime.projection import (
    FocusWebProjection,
    project_goal_payload,
    project_thread_inspection_tool,
    project_thread_snapshot,
    project_thread_summary,
    project_turn_page,
    project_turns,
)


_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cfc0f01f00050001ff89993d1d0000000049454e44ae426082"
)


class FocusWebProjectionTests(unittest.TestCase):
    def test_thread_summary_projects_exact_or_unknown_history_mode(self):
        summary = ThreadSummary(
            thread_id="thread-1",
            cwd="/workspace",
            name="Demo",
            preview="",
            created_at=1,
            updated_at=2,
            source="appServer",
            status="idle",
        )

        self.assertEqual(project_thread_summary(summary)["history_mode"], "unknown")
        summary.history_mode = "paginated"
        self.assertEqual(project_thread_summary(summary)["history_mode"], "paginated")

    def test_thread_inspection_command_reuses_bounded_semantic_projection(self):
        tool = project_thread_inspection_tool(
            {
                "id": "cmd-1",
                "type": "commandExecution",
                "command": "pytest -q",
                "status": "failed",
                "aggregatedOutput": "x" * 70_000,
                "exitCode": 1,
            },
            "turn-1",
            None,
        )

        self.assertEqual(tool["id"], "cmd-1")
        self.assertEqual(tool["status"], "error")
        self.assertTrue(tool["outputTruncated"])
        self.assertEqual(tool["commandExecution"]["exitCode"], 1)
        self.assertEqual(
            tool["inspectionLocator"],
            {
                "turn_id": "turn-1",
                "item_id": "cmd-1",
                "kind": "commandExecution",
                "change_index": None,
            },
        )

    def test_thread_inspection_file_change_selects_exact_change_without_losing_source(self):
        tool = project_thread_inspection_tool(
            {
                "id": "patch-1",
                "type": "fileChange",
                "status": "completed",
                "changes": [
                    {"path": "a.py", "kind": {"type": "update"}, "diff": "-a\n+A"},
                    {"path": "b.py", "kind": {"type": "add"}, "diff": "+B"},
                ],
            },
            "turn-1",
            1,
        )

        self.assertEqual(tool["id"], "patch-1:2")
        self.assertEqual(tool["name"], "Write")
        self.assertEqual(tool["diff"]["path"], "b.py")
        self.assertEqual(
            tool["inspectionLocator"],
            {
                "turn_id": "turn-1",
                "item_id": "patch-1",
                "kind": "fileChange",
                "change_index": 1,
            },
        )

    def test_thread_inspection_projection_rejects_ambiguous_locator_shapes(self):
        cases = (
            ({"id": "cmd", "type": "commandExecution"}, 0),
            ({"id": "patch", "type": "fileChange", "changes": []}, None),
            ({"id": "reason", "type": "reasoning"}, None),
        )
        for item, change_index in cases:
            with self.subTest(item_type=item["type"], change_index=change_index):
                with self.assertRaises(ValueError):
                    project_thread_inspection_tool(
                        item,
                        "turn-1",
                        change_index,
                    )

    def test_live_terminal_tools_carry_exact_inspection_locators_only(self):
        projected = project_turns(
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "cmd-1",
                            "type": "commandExecution",
                            "command": "pytest",
                            "status": "completed",
                            "aggregatedOutput": "ok",
                        },
                        {
                            "id": "cmd-live",
                            "type": "commandExecution",
                            "command": "sleep 1",
                            "status": "inProgress",
                        },
                        {
                            "id": "patch-1",
                            "type": "fileChange",
                            "status": "failed",
                            "changes": [
                                {"path": "a.py", "diff": "+A"},
                                {"path": "b.py", "diff": "+B"},
                            ],
                        },
                    ],
                }
            ]
        )

        tools = projected[0]["tools"]
        self.assertEqual(
            tools[0]["inspectionLocator"],
            {
                "turn_id": "turn-1",
                "item_id": "cmd-1",
                "kind": "commandExecution",
                "change_index": None,
            },
        )
        self.assertNotIn("inspectionLocator", tools[1])
        self.assertEqual(tools[2]["id"], "patch-1:1")
        self.assertEqual(tools[3]["id"], "patch-1:2")
        self.assertEqual(
            [tool["inspectionLocator"] for tool in tools[2:]],
            [
                {
                    "turn_id": "turn-1",
                    "item_id": "patch-1",
                    "kind": "fileChange",
                    "change_index": 0,
                },
                {
                    "turn_id": "turn-1",
                    "item_id": "patch-1",
                    "kind": "fileChange",
                    "change_index": 1,
                },
            ],
        )

    def test_summary_prompt_title_is_bounded_and_marks_only_real_truncation(self):
        cases = (
            ("x" * 160, "x" * 160),
            ("x" * 161, ("x" * 159) + "…"),
            (("x" * 159) + " \n\t y", ("x" * 159) + "…"),
            (("x" * 160) + " \n\t ", "x" * 160),
        )
        for raw_text, expected in cases:
            with self.subTest(raw_text=raw_text[-8:]):
                projected = project_turn_page(
                    [
                        {
                            "id": "turn-1",
                            "items": [
                                {
                                    "type": "userMessage",
                                    "content": [{"type": "text", "text": raw_text}],
                                },
                                {"type": "agentMessage", "text": "must not escape"},
                            ],
                        }
                    ],
                    items_view="summary",
                    page_cursor="page-1",
                    next_cursor=None,
                    coordinates={"runtime_epoch": "epoch", "revision": 1},
                )

                self.assertEqual(projected["page_cursor"], "page-1")
                self.assertEqual(projected["turns"][0]["text"], expected)
                self.assertLessEqual(len(projected["turns"][0]["text"]), 160)
                self.assertEqual(
                    projected["turns"][0]["title_truncated"],
                    expected.endswith("…"),
                )
                self.assertNotIn("must not escape", str(projected))

    def test_summary_keeps_attachments_only_user_prompt_locator(self):
        projected = project_turn_page(
            [
                {
                    "id": "turn-attachment",
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "image", "url": "https://invalid"}],
                        }
                    ],
                }
            ],
            items_view="summary",
            page_cursor=None,
            next_cursor=None,
            coordinates={"runtime_epoch": "epoch", "revision": 1},
        )

        self.assertEqual(
            projected["turns"],
            [
                {
                    "id": "turn-attachment:user",
                    "role": "user",
                    "no": 1,
                    "text": "",
                    "title_truncated": False,
                }
            ],
        )

    def test_summary_projects_only_the_first_user_message_in_each_raw_turn(self):
        projected = project_turn_page(
            [
                {
                    "id": "turn-steered",
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "initial prompt"}],
                        },
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "later steer"}],
                        },
                    ],
                }
            ],
            items_view="summary",
            page_cursor="page-steered",
            next_cursor=None,
            coordinates={"runtime_epoch": "epoch", "revision": 1},
        )

        self.assertEqual(len(projected["turns"]), 1)
        self.assertEqual(projected["turns"][0]["id"], "turn-steered:user")
        self.assertEqual(projected["turns"][0]["text"], "initial prompt")
        self.assertNotIn("later steer", str(projected))

    def test_active_turn_context_requires_the_projected_exact_turn(self):
        snapshot = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-1",
                cwd="/work/project",
                name="",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="active",
            ),
            turns=[{"id": "turn-1", "status": "inProgress", "items": []}],
        )
        context = {
            "turn_id": "turn-other",
            "initiator": {"kind": "web", "binding_id": ""},
        }

        projected = project_thread_snapshot(
            snapshot,
            owner={
                "kind": "none",
                "holder_id": "",
                "relation": "none",
                "label": "None",
            },
            pending_requests=[],
            coordinates={"runtime_epoch": "epoch", "revision": 1},
            active_turn_context=context,
        )

        self.assertIsNone(projected["active_turn_context"])
        context["turn_id"] = "turn-1"
        projected = project_thread_snapshot(
            snapshot,
            owner={
                "kind": "none",
                "holder_id": "",
                "relation": "none",
                "label": "None",
            },
            pending_requests=[],
            coordinates={"runtime_epoch": "epoch", "revision": 1},
            active_turn_context=context,
        )
        self.assertEqual(projected["active_turn_context"], context)
        self.assertIsNot(projected["active_turn_context"], context)

    def test_snapshot_preserves_pending_response_capability(self):
        snapshot = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-1",
                cwd="/work/project",
                name="",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            turns=[],
        )

        projected = project_thread_snapshot(
            snapshot,
            owner={
                "kind": "none",
                "holder_id": "",
                "relation": "none",
                "label": "None",
            },
            pending_requests=[
                {
                    "request_key": "request-1",
                    "connection_generation": 7,
                    "response_capability": "capability-7",
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "pytest"},
                    "thread_id": "thread-1",
                    "owner_thread_id": "thread-1",
                    "turn_id": "turn-1",
                    "status": "pending",
                }
            ],
            coordinates={"runtime_epoch": "epoch", "revision": 1},
        )

        pending = projected["pending_requests"][0]
        self.assertEqual(pending["connection_generation"], 7)
        self.assertEqual(pending["response_capability"], "capability-7")
        self.assertLessEqual(
            set(FOCUS_WEB_RECORD_BY_NAME["pending_request"].required_fields),
            set(pending),
        )

    def test_revision_events_are_monotonic_and_process_scoped(self):
        projection = FocusWebProjection()
        seen: list[dict] = []
        unsubscribe = projection.subscribe(seen.append)

        first = projection.publish("thread_invalidated", thread_id="thread-1", reason="turn/started")
        second = projection.publish("owner_changed", thread_id="thread-1")
        unsubscribe()
        projection.publish("thread_invalidated", thread_id="thread-2")

        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(first["runtime_epoch"], second["runtime_epoch"])
        self.assertEqual([event["revision"] for event in seen], [1, 2])

    def test_faulty_listener_does_not_block_other_listeners_or_publish(self):
        projection = FocusWebProjection()
        seen: list[dict] = []

        def fail(_event: dict) -> None:
            raise RuntimeError("closed subscriber")

        projection.subscribe(fail)
        projection.subscribe(seen.append)

        with self.assertLogs("bot.web_runtime.projection", level="ERROR"):
            event = projection.publish(
                "thread_invalidated",
                thread_id="thread-1",
                reason="turn/started",
            )

        self.assertEqual(event["revision"], 1)
        self.assertEqual([item["revision"] for item in seen], [1])

    def test_snapshot_projects_markdown_reasoning_and_structured_tools(self):
        summary = ThreadSummary(
            thread_id="thread-1",
            cwd="/work/project",
            name="Math notes",
            preview="derive formula",
            created_at=10,
            updated_at=20,
            source="appServer",
            status="idle",
        )
        snapshot = ThreadSnapshot(
            summary=summary,
            turns=[
                {
                    "id": "turn-1",
                    "status": "completed",
                    "startedAt": 1_700_000_000,
                    "durationMs": 1250,
                    "items": [
                        {
                            "id": "user-1",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "Render $$x^2$$"}],
                        },
                        {
                            "id": "reason-1",
                            "type": "reasoning",
                            "summary": ["Check the identity"],
                            "content": [],
                        },
                        {
                            "id": "cmd-1",
                            "type": "commandExecution",
                            "command": "pytest -q",
                            "cwd": "/work/project",
                            "processId": "process-1",
                            "source": "agent",
                            "status": "completed",
                            "aggregatedOutput": "2 passed",
                            "exitCode": 0,
                            "commandActions": [
                                {
                                    "type": "search",
                                    "command": "pytest -q",
                                    "query": "test",
                                    "path": "/work/project/tests",
                                }
                            ],
                            "durationMs": 15,
                        },
                        {
                            "id": "agent-1",
                            "type": "agentMessage",
                            "text": "Result: **done**",
                        },
                    ],
                }
            ],
        )

        projected = project_thread_snapshot(
            snapshot,
            owner={"kind": "none", "holder_id": "", "relation": "none", "label": "No active writer"},
            pending_requests=[],
            coordinates={"runtime_epoch": "epoch", "revision": 3},
        )

        self.assertEqual(projected["thread"]["title"], "Math notes")
        self.assertEqual(projected["turns"][0]["text"], "Render $$x^2$$")
        assistant = projected["turns"][1]
        self.assertEqual([block["kind"] for block in assistant["blocks"]], ["thinking", "tool", "text"])
        self.assertEqual(assistant["tools"][0]["output"], ["2 passed"])
        self.assertEqual(
            assistant["tools"][0]["commandExecution"],
            {
                "cwd": "/work/project",
                "processId": "process-1",
                "source": "agent",
                "exitCode": 0,
                "commandActions": [
                    {
                        "type": "search",
                        "command": "pytest -q",
                        "query": "test",
                        "path": "/work/project/tests",
                    }
                ],
            },
        )
        self.assertEqual(assistant["durationMs"], 1250)

    def test_command_execution_keeps_nullable_exit_and_ignores_non_schema_facts(self):
        projected = project_turns(
            [
                {
                    "id": "turn-1",
                    "status": "inProgress",
                    "items": [
                        {
                            "id": "cmd-1",
                            "type": "commandExecution",
                            "command": "sleep 1",
                            "cwd": "/work/project",
                            "processId": None,
                            "source": "userShell",
                            "status": "inProgress",
                            "exitCode": None,
                            "commandActions": [
                                {
                                    "type": "unknown",
                                    "command": "sleep 1",
                                    "unrecognizedFutureField": "must not cross DTO boundary",
                                },
                                "not-an-action",
                            ],
                        }
                    ],
                }
            ]
        )

        tool = projected[0]["tools"][0]
        self.assertEqual(
            tool["commandExecution"],
            {
                "cwd": "/work/project",
                "processId": None,
                "source": "userShell",
                "exitCode": None,
                "commandActions": [{"type": "unknown", "command": "sleep 1"}],
            },
        )

    def test_goal_notification_payload_is_normalized_for_web(self):
        projected = project_goal_payload(
            {
                "threadId": "thread-1",
                "objective": "Ship Web UI",
                "status": "active",
                "tokenBudget": 100,
                "tokensUsed": 25,
                "timeUsedSeconds": 2,
            }
        )

        self.assertEqual(projected["goal_id"], "thread-1")
        self.assertEqual(projected["tokens_used"], 25)
        self.assertEqual(projected["wall_clock_ms"], 2000)
        self.assertEqual(projected["budget"]["remaining_tokens"], 75)

    def test_attachment_envelope_projects_clean_user_text_and_attachment_chips(self):
        manifest = [
            {
                "id": "attachment-1",
                "kind": "file",
                "name": "notes.txt",
                "media_type": "text/plain",
                "size": 12,
                "path": ".focus-attachments/attachment-1-notes.txt",
            }
        ]
        envelope = "\n".join(
            [
                "[[focus.attachments.v1]]",
                json.dumps(manifest),
                "[[/focus.attachments.v1]]",
                "The files above are staged relative to the current workspace.",
                "[[focus.user_request]]",
                "Summarize the notes.",
            ]
        )
        snapshot = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-1",
                cwd="/work/project",
                name="",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "user-1",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": envelope}],
                        }
                    ],
                }
            ],
        )

        projected = project_thread_snapshot(
            snapshot,
            owner={"kind": "none", "holder_id": "", "relation": "none", "label": "None"},
            pending_requests=[],
            coordinates={"runtime_epoch": "epoch", "revision": 1},
            attachment_url_for_id=lambda attachment_id: f"/api/attachments/{attachment_id}",
        )

        user_turn = projected["turns"][0]
        self.assertEqual(user_turn["text"], "Summarize the notes.")
        self.assertEqual(
            user_turn["attachments"],
            [
                {
                    "kind": "file",
                    "url": "",
                    "fileId": "attachment-1",
                    "name": "notes.txt",
                    "mediaType": "text/plain",
                    "size": 12,
                }
            ],
        )

    def test_attachment_envelope_projects_video_as_inert_file_metadata(self):
        manifest = [
            {
                "id": "attachment-video",
                "kind": "video",
                "name": "clip.mp4",
                "media_type": "video/mp4",
                "size": 12,
                "path": ".focus-attachments/attachment-video-clip.mp4",
            }
        ]
        envelope = "\n".join(
            [
                "[[focus.attachments.v1]]",
                json.dumps(manifest),
                "[[/focus.attachments.v1]]",
                "[[focus.user_request]]",
                "Inspect the clip.",
            ]
        )
        snapshot = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-1",
                cwd="/work/project",
                name="",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "user-1",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": envelope}],
                        }
                    ],
                }
            ],
        )

        projected = project_thread_snapshot(
            snapshot,
            owner={"kind": "none", "holder_id": "", "relation": "none", "label": "None"},
            pending_requests=[],
            coordinates={"runtime_epoch": "epoch", "revision": 1},
            attachment_url_for_id=lambda attachment_id: f"/api/attachments/{attachment_id}",
        )

        self.assertEqual(
            projected["turns"][0]["attachments"],
            [
                {
                    "kind": "file",
                    "url": "",
                    "fileId": "attachment-video",
                    "name": "clip.mp4",
                    "mediaType": "video/mp4",
                    "size": 12,
                }
            ],
        )

    def test_native_image_input_does_not_duplicate_envelope_attachment(self):
        attachment_id = "attachment-image"
        envelope = "\n".join(
            [
                "[[focus.attachments.v1]]",
                json.dumps(
                    [
                        {
                            "id": attachment_id,
                            "kind": "image",
                            "name": "diagram.png",
                            "media_type": "image/png",
                            "path": ".focus-attachments/attachment-image-diagram.png",
                        }
                    ]
                ),
                "[[/focus.attachments.v1]]",
                "The files above are staged relative to the current workspace.",
                "[[focus.user_request]]",
                "Inspect the diagram.",
            ]
        )
        projected = project_turns(
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "user-1",
                            "type": "userMessage",
                            "content": [
                                {"type": "text", "text": envelope},
                                {
                                    "type": "localImage",
                                    "path": "/work/.focus-attachments/attachment-image-diagram.png",
                                },
                            ],
                        }
                    ],
                }
            ],
            attachment_url_for_id=lambda value: f"/api/attachments/{value}",
            attachment_url_for_path=lambda _path: f"/api/attachments/{attachment_id}",
        )

        self.assertEqual(len(projected[0]["attachments"]), 1)
        self.assertEqual(projected[0]["attachments"][0]["fileId"], attachment_id)

    def test_turn_projection_keeps_steer_between_assistant_segments(self):
        projected = project_turns(
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "user-a",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "A"}],
                        },
                        {
                            "id": "agent-before",
                            "type": "agentMessage",
                            "text": "partial answer",
                        },
                        {
                            "id": "user-steer",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "B / steer"}],
                        },
                        {
                            "id": "agent-after",
                            "type": "agentMessage",
                            "text": "continued answer",
                        },
                        {
                            "id": "command-after",
                            "type": "commandExecution",
                            "command": "git status --short",
                            "status": "completed",
                            "aggregatedOutput": " M app.py",
                        },
                    ],
                }
            ]
        )

        self.assertEqual(
            [turn["id"] for turn in projected],
            [
                "turn-1:user",
                "turn-1:assistant",
                "turn-1:user:2",
                "turn-1:assistant:2",
            ],
        )
        self.assertEqual(projected[1]["text"], "partial answer")
        self.assertEqual(projected[2]["text"], "B / steer")
        self.assertEqual(projected[3]["text"], "continued answer")
        self.assertEqual(projected[3]["tools"][0]["id"], "command-after")

    def test_live_turn_ends_with_a_fresh_assistant_segment_after_empty_steer(self):
        projected = project_turns(
            [
                {
                    "id": "turn-live",
                    "status": "inProgress",
                    "items": [
                        {
                            "id": "user-a",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "A"}],
                        },
                        {
                            "id": "agent-before",
                            "type": "agentMessage",
                            "text": "partial answer",
                        },
                        {
                            "id": "user-steer",
                            "type": "userMessage",
                            "content": [],
                        },
                    ],
                }
            ]
        )

        self.assertEqual(
            [turn["id"] for turn in projected],
            ["turn-live:user", "turn-live:assistant", "turn-live:assistant:2"],
        )
        self.assertEqual(projected[-1]["blocks"], [])
        self.assertEqual(projected[-1]["status"], "inProgress")

    def test_hook_prompt_is_a_user_boundary_inside_the_same_turn(self):
        projected = project_turns(
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "user-a",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "A"}],
                        },
                        {
                            "id": "agent-before",
                            "type": "agentMessage",
                            "text": "partial answer",
                        },
                        {
                            "id": "hook-1",
                            "type": "hookPrompt",
                            "fragments": [{"text": "Hook context"}],
                        },
                        {
                            "id": "agent-after",
                            "type": "agentMessage",
                            "text": "continued answer",
                        },
                    ],
                }
            ]
        )

        self.assertEqual(
            [(turn["id"], turn["text"]) for turn in projected],
            [
                ("turn-1:user", "A"),
                ("turn-1:assistant", "partial answer"),
                ("turn-1:user:2", "Hook context"),
                ("turn-1:assistant:2", "continued answer"),
            ],
        )

    def test_codex_items_keep_compaction_diff_review_agents_media_and_unknown_fallback(self):
        snapshot = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-1",
                cwd="/work/project",
                name="",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {"id": "compact-1", "type": "contextCompaction"},
                        {
                            "id": "review-1",
                            "type": "enteredReviewMode",
                            "review": "Review the working tree",
                        },
                        {
                            "id": "patch-1",
                            "type": "fileChange",
                            "status": "completed",
                            "changes": [
                                {
                                    "path": "app.py",
                                    "kind": {"type": "update"},
                                    "diff": "@@ -1,2 +1,2 @@\n-old\n+new\n same",
                                }
                            ],
                        },
                        {
                            "id": "agent-1",
                            "type": "collabAgentToolCall",
                            "tool": "spawnAgent",
                            "status": "completed",
                            "receiverThreadIds": ["child-1"],
                            "prompt": "Inspect tests",
                            "model": "gpt-test",
                            "agentsStates": {
                                "child-1": {"status": "completed", "message": "Done"}
                            },
                        },
                        {"id": "image-1", "type": "imageView", "path": "/work/project/out.png"},
                        {"id": "future-1", "type": "futureCodexItem", "value": 42},
                    ],
                }
            ],
        )

        projected = project_thread_snapshot(
            snapshot,
            owner={"kind": "none", "holder_id": "", "relation": "none", "label": "None"},
            pending_requests=[],
            coordinates={"runtime_epoch": "epoch", "revision": 1},
            attachment_url_for_path=lambda path: f"/media/{path.rsplit('/', 1)[-1]}",
        )

        self.assertEqual([turn["role"] for turn in projected["turns"]], ["compaction", "assistant"])
        self.assertEqual(projected["turns"][0]["id"], "turn-1:compaction")
        tools = projected["turns"][1]["tools"]
        self.assertEqual(
            [tool["name"] for tool in tools],
            ["Review started", "Edit", "Agent", "View image", "Codex item · futureCodexItem"],
        )
        self.assertEqual(tools[1]["diff"]["path"], "app.py")
        self.assertEqual(
            [line["type"] for line in tools[1]["diff"]["lines"]],
            ["hunk", "del", "add", "context"],
        )
        self.assertEqual(tools[2]["output"], ["thread: child-1 · completed", "Done"])
        self.assertEqual(tools[3]["media"]["url"], "/media/out.png")
        self.assertIn('"value": 42', "\n".join(tools[4]["output"]))
        self.assertEqual(
            projected["tasks"],
            [
                {
                    "id": "child-1",
                    "name": "gpt-test",
                    "kind": "subagent",
                    "state": "done",
                    "timing": "",
                    "meta": "",
                    "output": ["Done"],
                    "progress": ["Done"],
                    "result": [],
                    "metadata": [],
                    "runInBackground": False,
                    "parentToolCallId": "agent-1",
                    "prompt": "Inspect tests",
                    "executionState": "completed",
                }
            ],
        )

    def test_empty_user_message_keeps_the_summary_full_page_anchor(self):
        raw_turn = {
            "id": "turn-empty",
            "status": "completed",
            "items": [
                {"id": "user-empty", "type": "userMessage", "content": []},
                {"id": "agent-after", "type": "agentMessage", "text": "answer"},
            ],
        }

        summary = project_turn_page(
            [raw_turn],
            items_view="summary",
            page_cursor="page-empty",
            next_cursor=None,
            coordinates={"runtime_epoch": "epoch", "revision": 1},
        )
        full = project_turn_page(
            [raw_turn],
            items_view="full",
            page_cursor="page-empty",
            next_cursor=None,
            coordinates={"runtime_epoch": "epoch", "revision": 1},
        )

        self.assertEqual(summary["turns"][0]["id"], "turn-empty:user")
        self.assertEqual(full["turns"][0]["id"], "turn-empty:user")
        self.assertEqual(full["turns"][0]["text"], "")

    def test_parent_history_activity_projects_without_child_inventory(self):
        snapshot = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-1",
                cwd="/work/project",
                name="",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "activity-1",
                            "type": "subAgentActivity",
                            "agentThreadId": "child-activity",
                            "agentPath": "/agents/reviewer",
                            "kind": "interrupted",
                        }
                    ],
                }
            ],
        )

        projected = project_thread_snapshot(
            snapshot,
            owner={
                "kind": "none",
                "holder_id": "",
                "relation": "none",
                "label": "None",
            },
            pending_requests=[],
            coordinates={"runtime_epoch": "epoch", "revision": 1},
        )

        self.assertEqual(len(projected["tasks"]), 1)
        self.assertEqual(
            {
                key: projected["tasks"][0][key]
                for key in ("id", "name", "state", "executionState")
            },
            {
                "id": "child-activity",
                "name": "reviewer",
                "state": "fail",
                "executionState": "interrupted",
            },
        )

    def test_dynamic_audio_is_not_projected_as_browser_media(self):
        projected = project_turns(
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "tool-1",
                            "type": "dynamicToolCall",
                            "tool": "inspect_media",
                            "status": "completed",
                            "contentItems": [
                                {
                                    "type": "inputAudio",
                                    "audioUrl": "data:audio/wav;base64,AAAA",
                                }
                            ],
                        }
                    ],
                }
            ]
        )

        tools = projected[0]["tools"]
        self.assertEqual(len(tools), 1)
        self.assertNotIn("media", tools[0])

    def test_inline_tool_images_require_signature_checked_image_bytes(self):
        image_url = "data:image/png;base64," + base64.b64encode(_PNG_1X1).decode("ascii")
        projected = project_turns(
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "tool-1",
                            "type": "dynamicToolCall",
                            "tool": "inspect_image",
                            "status": "completed",
                            "contentItems": [{"type": "inputImage", "imageUrl": image_url}],
                        },
                        {
                            "id": "generated-1",
                            "type": "imageGeneration",
                            "status": "completed",
                            "result": image_url,
                        },
                    ],
                }
            ]
        )

        tools = projected[0]["tools"]
        self.assertEqual(tools[1]["media"]["url"], image_url)
        self.assertEqual(tools[2]["media"]["url"], image_url)

    def test_inline_tool_images_reject_mime_spoofing_and_non_image_data_urls(self):
        invalid_urls = [
            "data:image/png;base64," + base64.b64encode(b"not actually a png").decode("ascii"),
            "data:image/svg+xml;base64," + base64.b64encode(b"<svg></svg>").decode("ascii"),
            "data:image/png;base64,%%%%",
            "data:image/png;base64,",
        ]
        for index, image_url in enumerate(invalid_urls):
            with self.subTest(image_url=image_url):
                projected = project_turns(
                    [
                        {
                            "id": f"turn-{index}",
                            "status": "completed",
                            "items": [
                                {
                                    "id": "tool-1",
                                    "type": "dynamicToolCall",
                                    "tool": "inspect_image",
                                    "status": "completed",
                                    "contentItems": [
                                        {"type": "inputImage", "imageUrl": image_url}
                                    ],
                                },
                                {
                                    "id": "generated-1",
                                    "type": "imageGeneration",
                                    "status": "completed",
                                    "result": image_url,
                                },
                            ],
                        }
                    ]
                )
                tools = projected[0]["tools"]
                self.assertEqual(len(tools), 2)
                self.assertNotIn("media", tools[0])
                self.assertNotIn("media", tools[1])

    def test_user_image_urls_follow_the_same_verified_inline_boundary(self):
        image_url = "data:image/png;base64," + base64.b64encode(_PNG_1X1).decode("ascii")
        projected = project_turns(
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "user-1",
                            "type": "userMessage",
                            "content": [
                                {"type": "image", "url": image_url},
                                {"type": "image", "url": "https://example.test/not-allowed.png"},
                                {
                                    "type": "image",
                                    "url": "data:image/png;base64,"
                                    + base64.b64encode(b"not actually a png").decode("ascii"),
                                },
                            ],
                        }
                    ],
                }
            ]
        )

        attachments = projected[0]["attachments"]
        self.assertEqual(attachments[0]["url"], image_url)
        self.assertEqual(attachments[1]["url"], "")
        self.assertEqual(attachments[2]["url"], "")


if __name__ == "__main__":
    unittest.main()
