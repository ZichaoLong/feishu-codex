import unittest

from bot.web_runtime.projection import project_turns
from bot.web_runtime.thread_read_model import WebThreadReadModel
from bot.web_runtime.tool_output_presentation import (
    CachedToolOutputPresentation,
    INTERNAL_PRESENTATION_METADATA_KEY,
    MAX_TOOL_OUTPUT_WINDOW_CHARS,
    MAX_TOOL_OUTPUT_WINDOW_OUTPUTS,
    MAX_VISIBLE,
)


class WebThreadReadModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.read_model = WebThreadReadModel()

    def test_replace_and_merge_turns_keep_one_owned_snapshot(self) -> None:
        self.read_model.replace_turns(
            "thread-1",
            [
                {
                    "id": "turn-1",
                    "status": "inProgress",
                    "items": [{"id": "message-1", "type": "agentMessage", "text": "old"}],
                }
            ],
        )

        self.read_model.merge_turns(
            "thread-1",
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [{"id": "message-1", "type": "agentMessage", "text": "new"}],
                },
                {"id": "turn-2", "status": "inProgress", "items": []},
            ],
        )

        snapshot = self.read_model.snapshot("thread-1")
        self.assertEqual([turn["id"] for turn in snapshot.turns], ["turn-1", "turn-2"])
        self.assertEqual(snapshot.turns[0]["status"], "completed")
        self.assertEqual(snapshot.turns[0]["items"][0]["text"], "new")

    def test_replace_and_merge_keep_only_the_recent_ordered_window(self) -> None:
        self.read_model.replace_turns(
            "thread-1",
            [
                {"id": f"turn-{index}", "status": "completed", "items": []}
                for index in range(12)
            ],
        )

        self.assertEqual(
            self.read_model.turn_ids("thread-1"),
            tuple(f"turn-{index}" for index in range(2, 12)),
        )

    def test_configured_recent_window_cannot_exceed_the_product_hard_cap(self) -> None:
        read_model = WebThreadReadModel(recent_turn_limit=200)
        read_model.replace_turns(
            "thread-1",
            [
                {"id": f"turn-{index}", "status": "completed", "items": []}
                for index in range(25)
            ],
        )

        self.assertEqual(
            read_model.turn_ids("thread-1"),
            tuple(f"turn-{index}" for index in range(5, 25)),
        )

    def test_merge_advances_the_default_recent_window(self) -> None:
        self.read_model.replace_turns(
            "thread-1",
            [
                {"id": f"turn-{index}", "status": "completed", "items": []}
                for index in range(12)
            ],
        )
        self.read_model.merge_turns(
            "thread-1",
            [
                {"id": "turn-12", "status": "completed", "items": []},
                {"id": "turn-13", "status": "completed", "items": []},
            ],
        )

        self.assertEqual(
            self.read_model.turn_ids("thread-1"),
            tuple(f"turn-{index}" for index in range(4, 14)),
        )

    def test_live_turn_notifications_keep_the_recent_window_and_active_turn(self) -> None:
        for index in range(12):
            turn_id = f"turn-{index}"
            self.read_model.apply_notification(
                "turn/started",
                {
                    "threadId": "thread-1",
                    "turn": {"id": turn_id, "status": "inProgress", "items": []},
                },
            )
            if index < 11:
                self.read_model.apply_notification(
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": turn_id, "status": "completed", "items": []},
                    },
                )

        self.assertEqual(
            self.read_model.turn_ids("thread-1"),
            tuple(f"turn-{index}" for index in range(2, 12)),
        )
        self.assertEqual(self.read_model.latest_turn("thread-1")["id"], "turn-11")  # type: ignore[index]
        self.assertTrue(self.read_model.latest_turn_is_active("thread-1"))

    def test_existing_turn_notification_updates_without_reordering(self) -> None:
        read_model = WebThreadReadModel(recent_turn_limit=3)
        read_model.replace_turns(
            "thread-1",
            [
                {"id": "turn-1", "status": "completed", "items": []},
                {"id": "turn-2", "status": "inProgress", "items": []},
                {"id": "turn-3", "status": "completed", "items": []},
            ],
        )

        read_model.apply_notification(
            "turn/completed",
            {
                "threadId": "thread-1",
                "turn": {
                    "id": "turn-2",
                    "status": "completed",
                    "items": [{"id": "message-2", "type": "agentMessage"}],
                },
            },
        )

        self.assertEqual(read_model.turn_ids("thread-1"), ("turn-1", "turn-2", "turn-3"))
        self.assertEqual(read_model.snapshot("thread-1").turns[1]["status"], "completed")

    def test_bounding_preserves_an_active_turn_outside_the_recent_tail(self) -> None:
        read_model = WebThreadReadModel(recent_turn_limit=3)
        read_model.replace_turns(
            "thread-1",
            [
                {"id": "turn-active", "status": "inProgress", "items": []},
                {"id": "turn-1", "status": "completed", "items": []},
                {"id": "turn-2", "status": "completed", "items": []},
                {"id": "turn-3", "status": "completed", "items": []},
            ],
        )

        self.assertEqual(
            read_model.turn_ids("thread-1"),
            ("turn-active", "turn-2", "turn-3"),
        )
        self.assertEqual(read_model.active_turn_id_from_turns(read_model.turns("thread-1")), "turn-active")

    def test_snapshot_does_not_expose_mutable_owner_state(self) -> None:
        self.read_model.replace_turns(
            "thread-1",
            [
                {
                    "id": "turn-1",
                    "status": "inProgress",
                    "items": [{"id": "message-1", "type": "agentMessage", "text": "safe"}],
                }
            ],
        )
        self.read_model.remember_cwd("thread-1", "/work/project")
        self.read_model.apply_notification(
            "thread/tokenUsage/updated",
            {"threadId": "thread-1", "tokenUsage": {"totalTokens": 12}},
        )

        snapshot = self.read_model.snapshot("thread-1")
        snapshot.turns[0]["items"][0]["text"] = "mutated"
        assert snapshot.token_usage is not None
        snapshot.token_usage["totalTokens"] = 99

        fresh = self.read_model.snapshot("thread-1")
        self.assertEqual(fresh.turns[0]["items"][0]["text"], "safe")
        self.assertEqual(fresh.token_usage, {"totalTokens": 12})

    def test_narrow_queries_copy_only_the_requested_facts(self) -> None:
        self.read_model.replace_turns(
            "thread-1",
            [
                {
                    "id": "turn-1",
                    "status": "inProgress",
                    "items": [
                        {"id": "message-1", "type": "agentMessage", "text": "large"},
                        {
                            "id": "collab-1",
                            "type": "collabAgentToolCall",
                            "receiverThreadIds": ["child-1"],
                        },
                    ],
                }
            ],
        )
        self.read_model.apply_notification(
            "thread/tokenUsage/updated",
            {"threadId": "thread-1", "tokenUsage": {"totalTokens": 12}},
        )

        self.assertEqual(self.read_model.turn_thread_ids(), ("thread-1",))
        self.assertTrue(self.read_model.latest_turn_is_active("thread-1"))
        latest = self.read_model.latest_turn("thread-1")
        self.assertIsNotNone(latest)
        latest["status"] = "completed"  # type: ignore[index]
        self.assertTrue(self.read_model.latest_turn_is_active("thread-1"))
        collaboration = self.read_model.collaboration_turns("thread-1")
        self.assertEqual(len(collaboration[0]["items"]), 1)
        self.assertEqual(collaboration[0]["items"][0]["type"], "collabAgentToolCall")
        usage, available = self.read_model.token_usage("thread-1")
        self.assertTrue(available)
        self.assertEqual(usage, {"totalTokens": 12})

    def test_live_agent_delta_is_folded_into_the_turn_cache(self) -> None:
        first = self.read_model.apply_notification(
            "item/agentMessage/delta",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "message-1",
                "delta": "hello ",
            },
        )
        second = self.read_model.apply_notification(
            "item/agentMessage/delta",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "message-1",
                "delta": "world",
            },
        )

        self.assertIsNotNone(first)
        self.assertEqual(first.detail["stream_delta"]["kind"], "text")  # type: ignore[union-attr]
        self.assertIsNotNone(second)
        snapshot = self.read_model.snapshot("thread-1")
        self.assertEqual(snapshot.turns[0]["items"][0]["text"], "hello world")

    def test_delta_only_turns_still_keep_the_recent_window(self) -> None:
        read_model = WebThreadReadModel(recent_turn_limit=3)

        for index in range(8):
            read_model.apply_notification(
                "item/agentMessage/delta",
                {
                    "threadId": "thread-1",
                    "turnId": f"turn-{index}",
                    "itemId": f"message-{index}",
                    "delta": str(index),
                },
            )

        self.assertEqual(
            read_model.turn_ids("thread-1"),
            ("turn-5", "turn-6", "turn-7"),
        )

    def test_completed_item_replaces_its_live_delta_projection(self) -> None:
        self.read_model.apply_notification(
            "item/commandExecution/outputDelta",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "command-1",
                "delta": "partial",
            },
        )

        update = self.read_model.apply_notification(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "command-1",
                    "type": "commandExecution",
                    "status": "completed",
                    "aggregatedOutput": "authoritative",
                },
            },
        )

        self.assertIsNotNone(update)
        self.assertEqual(update.raw_turn["items"][0]["aggregatedOutput"], "authoritative")  # type: ignore[index,union-attr]

    def test_initial_tool_outputs_are_bounded_with_exact_internal_metadata(self) -> None:
        omitted = 12_345
        giant_output = "H" * MAX_VISIBLE + "M" * omitted
        self.read_model.replace_turns(
            "thread-1",
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "command-1",
                            "type": "commandExecution",
                            "aggregatedOutput": giant_output,
                        },
                        {
                            "id": "change-1",
                            "type": "fileChange",
                            "changes": [{"path": "app.py", "diff": giant_output}],
                        },
                        {
                            "id": "turn-diff-1",
                            "type": "turnDiff",
                            "diff": giant_output,
                        },
                    ],
                }
            ],
        )

        command, change, turn_diff = self.read_model.snapshot("thread-1").turns[0][
            "items"
        ]
        self.assertLess(len(command["aggregatedOutput"]), len(giant_output))
        command_metadata = command[INTERNAL_PRESENTATION_METADATA_KEY]
        self.assertIsInstance(command_metadata, CachedToolOutputPresentation)
        self.assertEqual(
            command_metadata.aggregated_output_omitted_chars,
            omitted,
        )
        self.assertEqual(command_metadata.aggregated_output_head_line_count, 1)
        self.assertLess(len(change["changes"][0]["diff"]), len(giant_output))
        change_metadata = change[INTERNAL_PRESENTATION_METADATA_KEY]
        self.assertIsInstance(change_metadata, CachedToolOutputPresentation)
        self.assertEqual(
            change_metadata.change_diff_omitted_chars,
            (omitted,),
        )
        self.assertEqual(change_metadata.change_diff_head_line_counts, (1,))
        self.assertLess(len(turn_diff["diff"]), len(giant_output))
        turn_diff_metadata = turn_diff[INTERNAL_PRESENTATION_METADATA_KEY]
        self.assertIsInstance(
            turn_diff_metadata,
            CachedToolOutputPresentation,
        )
        self.assertEqual(
            turn_diff_metadata.turn_diff_omitted_chars,
            omitted,
        )
        self.assertEqual(turn_diff_metadata.turn_diff_head_line_count, 1)

    def test_live_turn_diff_is_bounded_before_entering_the_cache(self) -> None:
        omitted = 789
        diff = "x" * (MAX_VISIBLE + omitted)

        update = self.read_model.apply_notification(
            "turn/diff/updated",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "diff": diff,
            },
        )

        self.assertIsNotNone(update)
        item = self.read_model.snapshot("thread-1").turns[0]["items"][0]
        self.assertLess(len(item["diff"]), len(diff))
        metadata = item[INTERNAL_PRESENTATION_METADATA_KEY]
        self.assertIsInstance(metadata, CachedToolOutputPresentation)
        self.assertEqual(metadata.turn_diff_omitted_chars, omitted)
        self.assertEqual(metadata.turn_diff_head_line_count, 1)

    def test_completed_giant_tool_output_replaces_cache_with_bounded_projection(
        self,
    ) -> None:
        omitted = 321
        output = "x" * (MAX_VISIBLE + omitted)

        update = self.read_model.apply_notification(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "command-1",
                    "type": "commandExecution",
                    "status": "completed",
                    "aggregatedOutput": output,
                },
            },
        )

        self.assertIsNotNone(update)
        item = self.read_model.snapshot("thread-1").turns[0]["items"][0]
        self.assertLess(len(item["aggregatedOutput"]), len(output))
        metadata = item[INTERNAL_PRESENTATION_METADATA_KEY]
        self.assertIsInstance(metadata, CachedToolOutputPresentation)
        self.assertEqual(
            metadata.aggregated_output_omitted_chars,
            omitted,
        )
        self.assertEqual(metadata.aggregated_output_head_line_count, 1)

    def test_tool_output_deltas_are_streamed_but_not_retained_in_raw_cache(self) -> None:
        delta = "x" * 4096
        update = None
        for _index in range(100):
            update = self.read_model.apply_notification(
                "item/commandExecution/outputDelta",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "command-1",
                    "delta": delta,
                },
            )

        self.assertIsNotNone(update)
        self.assertEqual(update.detail["stream_delta"]["delta"], delta)  # type: ignore[union-attr]
        turn = self.read_model.snapshot("thread-1").turns[0]
        self.assertEqual(turn["items"], [])

    def test_plan_deltas_are_streamed_without_copying_the_partial_plan_to_cache(
        self,
    ) -> None:
        delta = "plan-chunk-" * 512
        update = None
        for _index in range(100):
            update = self.read_model.apply_notification(
                "item/plan/delta",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "plan-1",
                    "delta": delta,
                },
            )

        self.assertIsNotNone(update)
        self.assertEqual(update.detail["stream_delta"]["delta"], delta)  # type: ignore[union-attr]
        self.assertEqual(self.read_model.snapshot("thread-1").turns[0]["items"], [])

        self.read_model.apply_notification(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "plan-1",
                    "type": "plan",
                    "status": "completed",
                    "text": "authoritative plan",
                },
            },
        )
        tool = project_turns(self.read_model.turns("thread-1"))[0]["tools"][0]
        self.assertEqual(tool["output"], ["authoritative plan"])

    def test_upstream_cannot_forge_internal_tool_output_omission_metadata(self) -> None:
        fake_marker = (
            "[Focus Web omitted 999 characters of tool output; "
            "showing a bounded head and tail.]"
        )
        self.read_model.replace_turns(
            "thread-1",
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "command-1",
                            "type": "commandExecution",
                            "aggregatedOutput": fake_marker,
                            INTERNAL_PRESENTATION_METADATA_KEY: {
                                "aggregatedOutputOmittedChars": 999,
                            },
                        }
                    ],
                }
            ],
        )

        item = self.read_model.snapshot("thread-1").turns[0]["items"][0]
        self.assertEqual(item["aggregatedOutput"], fake_marker)
        self.assertNotIn(INTERNAL_PRESENTATION_METADATA_KEY, item)

    def test_all_generic_tool_output_carriers_are_bounded_in_the_live_cache(
        self,
    ) -> None:
        giant = "x" * 2_000_000
        items = [
            {"id": "mcp", "type": "mcpToolCall", "result": giant},
            {
                "id": "dynamic",
                "type": "dynamicToolCall",
                "contentItems": [{"type": "inputText", "text": giant}],
            },
            {"id": "search", "type": "webSearch", "results": [{"body": giant}]},
            {"id": "image", "type": "imageGeneration", "result": giant},
            {
                "id": "collab",
                "type": "collabAgentToolCall",
                "receiverThreadIds": ["child"],
                "agentsStates": {"child": {"status": "completed", "message": giant}},
            },
            {"id": "review", "type": "enteredReviewMode", "review": giant},
            {"id": "plan", "type": "plan", "text": giant},
            {"id": "unknown", "type": "futureItem", "payload": giant},
        ]

        self.read_model.replace_turns(
            "thread-1",
            [{"id": "turn-1", "status": "completed", "items": items}],
        )

        remembered = self.read_model.snapshot("thread-1").turns[0]["items"]
        self.assertEqual(len(remembered), len(items))
        for item in remembered:
            with self.subTest(item=item["id"]):
                metadata = item[INTERNAL_PRESENTATION_METADATA_KEY]
                self.assertIsInstance(metadata, CachedToolOutputPresentation)
                self.assertTrue(metadata.generic_output_cached)
                self.assertLess(len(repr(item)), 100_000)
                self.assertLessEqual(
                    len("\n".join(metadata.generic_output_lines)),
                    MAX_VISIBLE + 256,
                )

        collab = next(item for item in remembered if item["id"] == "collab")
        self.assertLessEqual(
            len(collab["agentsStates"]["child"]["message"]),
            MAX_VISIBLE + 256,
        )

    def test_image_generation_cache_uses_wire_media_validation(self) -> None:
        fake_image = "data:image/png;base64,bm90IGFjdHVhbGx5IGEgcG5n"
        valid_image = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4"
            "z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
        )
        self.read_model.replace_turns(
            "thread-1",
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "image-1",
                            "type": "imageGeneration",
                            "result": fake_image,
                        },
                        {
                            "id": "image-2",
                            "type": "imageGeneration",
                            "result": valid_image,
                        }
                    ],
                }
            ],
        )

        fake_item, valid_item = self.read_model.turns("thread-1")[0]["items"]
        self.assertNotIn("result", fake_item)
        self.assertEqual(valid_item["result"], valid_image)
        fake_tool, valid_tool = project_turns(
            self.read_model.turns("thread-1")
        )[0]["tools"]
        self.assertNotIn("media", fake_tool)
        self.assertEqual(fake_tool["output"], [fake_image])
        self.assertEqual(valid_tool["media"]["url"], valid_image)
        self.assertEqual(valid_tool["output"], [])

    def test_reasoning_deltas_preserve_their_upstream_indexes(self) -> None:
        for method, index_name, index, delta in (
            ("item/reasoning/summaryPartAdded", "summaryIndex", 1, ""),
            ("item/reasoning/summaryTextDelta", "summaryIndex", 1, "summary"),
            ("item/reasoning/textDelta", "contentIndex", 2, "detail"),
        ):
            self.read_model.apply_notification(
                method,
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "reasoning-1",
                    index_name: index,
                    "delta": delta,
                },
            )

        item = self.read_model.snapshot("thread-1").turns[0]["items"][0]
        self.assertEqual(item["summary"], ["", "summary"])
        self.assertEqual(item["content"], ["", "", "detail"])

    def test_plan_notification_returns_projection_hint_and_caches_live_item(self) -> None:
        update = self.read_model.apply_notification(
            "turn/plan/updated",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "explanation": "why",
                "plan": [
                    {"step": "one", "status": "completed"},
                    {"step": "two", "status": "in_progress"},
                ],
            },
        )

        self.assertIsNotNone(update)
        self.assertEqual(update.detail["plan_replay"], "live_only")  # type: ignore[union-attr]
        item = self.read_model.snapshot("thread-1").turns[0]["items"][0]
        self.assertEqual(item["text"], "")
        metadata = item[INTERNAL_PRESENTATION_METADATA_KEY]
        self.assertIsInstance(metadata, CachedToolOutputPresentation)
        self.assertTrue(metadata.generic_output_cached)
        tool = project_turns(self.read_model.turns("thread-1"))[0]["tools"][0]
        self.assertEqual(
            tool["output"],
            ["why\n- [completed] one\n- [in_progress] two"],
        )

    def test_turn_cache_has_one_aggregate_tool_output_budget(self) -> None:
        original_chars = 100_000
        self.read_model.replace_turns(
            "thread-1",
            [
                {
                    "id": "turn-1",
                    "items": [
                        {
                            "id": f"command-{index}",
                            "type": "commandExecution",
                            "aggregatedOutput": "x" * original_chars,
                        }
                        for index in range(40)
                    ],
                }
            ],
        )

        items = self.read_model.turns("thread-1")[0]["items"]
        visible = [str(item.get("aggregatedOutput", "") or "") for item in items]
        self.assertLessEqual(
            sum(len(output) for output in visible),
            MAX_TOOL_OUTPUT_WINDOW_CHARS,
        )
        self.assertLessEqual(
            sum(bool(output) for output in visible),
            MAX_TOOL_OUTPUT_WINDOW_OUTPUTS,
        )
        fully_omitted = [
            item for item in items
            if not item.get("aggregatedOutput")
        ]
        self.assertGreater(len(fully_omitted), 0)
        for item in fully_omitted:
            metadata = item[INTERNAL_PRESENTATION_METADATA_KEY]
            self.assertEqual(
                metadata.aggregated_output_omitted_chars,
                original_chars,
            )
            self.assertEqual(metadata.aggregated_output_head_line_count, 0)
            self.assertEqual(
                metadata.aggregated_output_original_chars,
                original_chars,
            )

    def test_output_count_budget_and_turn_budgets_are_independent(self) -> None:
        self.read_model.replace_turns(
            "thread-1",
            [
                {
                    "id": f"turn-{turn_index}",
                    "items": [
                        {
                            "id": f"command-{turn_index}-{output_index}",
                            "type": "commandExecution",
                            "aggregatedOutput": "x",
                        }
                        for output_index in range(MAX_TOOL_OUTPUT_WINDOW_OUTPUTS + 1)
                    ],
                }
                for turn_index in range(2)
            ],
        )

        for turn in self.read_model.turns("thread-1"):
            outputs = [
                str(item.get("aggregatedOutput", "") or "")
                for item in turn["items"]
            ]
            self.assertEqual(sum(bool(output) for output in outputs), 16)
            self.assertEqual(outputs[-1], "")
            metadata = turn["items"][-1][INTERNAL_PRESENTATION_METADATA_KEY]
            self.assertEqual(metadata.aggregated_output_omitted_chars, 1)
            self.assertEqual(metadata.aggregated_output_original_chars, 1)

    def test_item_completion_reapplies_the_existing_turn_budget(self) -> None:
        self.read_model.replace_turns(
            "thread-1",
            [
                {
                    "id": "turn-1",
                    "items": [
                        {
                            "id": f"command-{index}",
                            "type": "commandExecution",
                            "aggregatedOutput": "x" * 100_000,
                        }
                        for index in range(3)
                    ],
                }
            ],
        )

        self.read_model.apply_notification(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "command-new",
                    "type": "commandExecution",
                    "aggregatedOutput": "y" * 100_000,
                },
            },
        )

        items = self.read_model.turns("thread-1")[0]["items"]
        self.assertEqual(items[-1]["aggregatedOutput"], "")
        metadata = items[-1][INTERNAL_PRESENTATION_METADATA_KEY]
        self.assertEqual(metadata.aggregated_output_omitted_chars, 100_000)

    def test_sequential_item_completions_share_one_turn_budget(self) -> None:
        for index in range(MAX_TOOL_OUTPUT_WINDOW_OUTPUTS + 1):
            self.read_model.apply_notification(
                "item/completed",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "id": f"command-{index}",
                        "type": "commandExecution",
                        "aggregatedOutput": "x",
                    },
                },
            )

        items = self.read_model.turns("thread-1")[0]["items"]
        self.assertEqual(sum(bool(item["aggregatedOutput"]) for item in items), 16)
        self.assertEqual(items[-1]["aggregatedOutput"], "")
        metadata = items[-1][INTERNAL_PRESENTATION_METADATA_KEY]
        self.assertEqual(metadata.aggregated_output_omitted_chars, 1)

    def test_active_turn_uses_cache_before_loading_and_remembers_fallback(self) -> None:
        self.read_model.replace_turns(
            "thread-1",
            [{"id": "turn-cached", "status": "inProgress", "items": []}],
        )
        load_calls = 0

        def load_turns() -> list[dict]:
            nonlocal load_calls
            load_calls += 1
            return [{"id": "turn-loaded", "status": "inProgress", "items": []}]

        self.assertEqual(
            self.read_model.active_turn_id("thread-1", load_turns=load_turns),
            "turn-cached",
        )
        self.assertEqual(load_calls, 0)

        self.read_model.forget_runtime("thread-1")
        self.assertEqual(
            self.read_model.active_turn_id("thread-1", load_turns=load_turns),
            "turn-loaded",
        )
        self.assertEqual(load_calls, 1)
        self.assertEqual(self.read_model.turn_ids("thread-1"), ("turn-loaded",))

    def test_backend_disconnect_drops_backend_epoch_facts_but_keeps_cwd(self) -> None:
        self.read_model.replace_turns(
            "thread-1",
            [{"id": "turn-1", "status": "completed", "items": []}],
        )
        self.read_model.remember_cwd("thread-1", "/work/project")
        self.read_model.apply_notification(
            "thread/tokenUsage/updated",
            {"threadId": "thread-1", "tokenUsage": {"totalTokens": 12}},
        )

        self.read_model.backend_disconnected()

        snapshot = self.read_model.snapshot("thread-1")
        self.assertEqual(snapshot.turns, ())
        self.assertEqual(snapshot.cwd, "/work/project")
        self.assertFalse(snapshot.token_usage_available)

    def test_forget_scopes_preserve_the_existing_lifecycle_contract(self) -> None:
        self.read_model.replace_turns(
            "thread-1",
            [{"id": "turn-1", "status": "completed", "items": []}],
        )
        self.read_model.remember_cwd("thread-1", "/work/project")
        self.read_model.apply_notification(
            "thread/tokenUsage/updated",
            {"threadId": "thread-1", "tokenUsage": {"totalTokens": 12}},
        )

        self.read_model.forget_closed_thread("thread-1")
        closed = self.read_model.snapshot("thread-1")
        self.assertEqual(closed.turns, ())
        self.assertEqual(closed.cwd, "")
        self.assertTrue(closed.token_usage_available)

        self.read_model.forget_thread("thread-1")
        forgotten = self.read_model.snapshot("thread-1")
        self.assertFalse(forgotten.token_usage_available)


if __name__ == "__main__":
    unittest.main()
