import unittest
from unittest.mock import patch

from bot.web_runtime.projection import project_turn_page, project_turns
from bot.web_runtime.thread_read_model import WebThreadReadModel
from bot.web_runtime.tool_output_presentation import (
    HEAD,
    INTERNAL_PRESENTATION_METADATA_KEY,
    MAX_TOOL_OUTPUT_WINDOW_CHARS,
    MAX_TOOL_OUTPUT_WINDOW_OUTPUTS,
    MAX_VISIBLE,
    TAIL,
    present_tool_output,
)


class ToolOutputPresentationTests(unittest.TestCase):
    def test_small_text_and_line_inputs_keep_their_existing_shape(self) -> None:
        text = "alpha\r\nbeta\n\nomega\n"
        lines = ["one", "two\nembedded", ""]

        self.assertEqual(
            present_tool_output(text).lines,
            ["alpha\r", "beta", "", "omega", ""],
        )
        self.assertEqual(present_tool_output(lines).lines, lines)
        self.assertEqual(present_tool_output(text).omitted_chars, 0)
        self.assertEqual(present_tool_output(lines).omitted_chars, 0)

    def test_giant_single_line_keeps_exact_head_tail_and_marker(self) -> None:
        omitted = 12_345
        text = "H" * HEAD + "M" * omitted + "T" * TAIL

        presented = present_tool_output(text)

        marker = (
            f"[Focus Web omitted {omitted} characters of tool output; "
            "showing a bounded head and tail.]"
        )
        self.assertEqual(presented.lines, ["H" * HEAD, marker, "T" * TAIL])
        self.assertEqual(presented.omitted_chars, omitted)
        self.assertEqual(presented.head_line_count, 1)
        self.assertEqual(len(presented.lines[0]) + len(presented.lines[-1]), MAX_VISIBLE)

    def test_multiline_input_reports_the_exact_conceptual_omission(self) -> None:
        lines = ["head", "M" * (MAX_VISIBLE + 37), "tail"]
        conceptual_chars = sum(map(len, lines)) + len(lines) - 1
        omitted = conceptual_chars - MAX_VISIBLE

        presented = present_tool_output(lines)

        self.assertEqual(presented.omitted_chars, omitted)
        self.assertEqual(presented.lines[0], "head")
        self.assertEqual(presented.lines[-1], "tail")
        self.assertIn(
            f"[Focus Web omitted {omitted} characters of tool output; "
            "showing a bounded head and tail.]",
            presented.lines,
        )

    def test_unicode_boundaries_count_code_points_without_splitting_emoji(self) -> None:
        exact = "😀" * MAX_VISIBLE
        overflow = exact + "😀"

        self.assertEqual(present_tool_output(exact).omitted_chars, 0)
        presented = present_tool_output(overflow)
        self.assertEqual(presented.omitted_chars, 1)
        self.assertEqual(presented.original_chars, MAX_VISIBLE + 1)
        self.assertEqual(len(presented.lines[0]), HEAD)
        self.assertEqual(len(presented.lines[-1]), TAIL)

    def test_command_projection_passes_raw_output_and_exposes_truncation_facts(
        self,
    ) -> None:
        output = "a" * (MAX_VISIBLE + 19)
        with patch(
            "bot.web_runtime.projection.present_tool_output",
            wraps=present_tool_output,
        ) as presenter:
            projected = project_turns(
                [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [
                            {
                                "id": "command-1",
                                "type": "commandExecution",
                                "command": "produce-output",
                                "aggregatedOutput": output,
                                "status": "completed",
                            }
                        ],
                    }
                ]
            )

        presenter.assert_called_once_with(output)
        tool = projected[0]["tools"][0]
        self.assertTrue(tool["outputTruncated"])
        self.assertEqual(tool["outputOmittedChars"], 19)
        self.assertEqual(tool["outputHeadLineCount"], 1)
        self.assertIn(
            "[Focus Web omitted 19 characters of tool output; "
            "showing a bounded head and tail.]",
            tool["output"],
        )

    def test_small_command_projection_adds_no_truncation_fields(self) -> None:
        projected = project_turns(
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "command-1",
                            "type": "commandExecution",
                            "command": "printf output",
                            "aggregatedOutput": "first\nsecond\n",
                            "status": "completed",
                        }
                    ],
                }
            ]
        )

        tool = projected[0]["tools"][0]
        self.assertEqual(tool["output"], ["first", "second", ""])
        self.assertNotIn("outputTruncated", tool)
        self.assertNotIn("outputOmittedChars", tool)

    def test_cached_command_projection_keeps_the_original_omission_count(self) -> None:
        omitted = 12_345
        output = "H" * MAX_VISIBLE + "M" * omitted
        read_model = WebThreadReadModel()
        read_model.replace_turns(
            "thread-1",
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "command-1",
                            "type": "commandExecution",
                            "aggregatedOutput": output,
                        }
                    ],
                }
            ],
        )

        with patch("bot.web_runtime.projection.present_tool_output") as presenter:
            projected = project_turns(read_model.turns("thread-1"))

        presenter.assert_not_called()
        tool = projected[0]["tools"][0]
        self.assertTrue(tool["outputTruncated"])
        self.assertEqual(tool["outputOmittedChars"], omitted)
        self.assertEqual(tool["outputHeadLineCount"], 1)
        self.assertIn(
            f"[Focus Web omitted {omitted} characters of tool output; "
            "showing a bounded head and tail.]",
            tool["output"],
        )

    def test_cached_file_change_keeps_the_original_diff_omission_count(self) -> None:
        omitted = 456
        diff = "H" * MAX_VISIBLE + "M" * omitted
        read_model = WebThreadReadModel()
        read_model.replace_turns(
            "thread-1",
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "change-1",
                            "type": "fileChange",
                            "status": "completed",
                            "changes": [{"path": "app.py", "diff": diff}],
                        }
                    ],
                }
            ],
        )

        with patch("bot.web_runtime.projection.present_tool_output") as presenter:
            projected = project_turns(read_model.turns("thread-1"))

        presenter.assert_not_called()
        tool = projected[0]["tools"][0]
        self.assertTrue(tool["outputTruncated"])
        self.assertEqual(tool["outputOmittedChars"], omitted)
        self.assertEqual(tool["outputHeadLineCount"], 1)

    def test_full_history_structured_diffs_use_the_bounded_projection(self) -> None:
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,30000 @@\n"
            + "+x\n" * 30_000
        )
        page = project_turn_page(
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "turn-diff-1",
                            "type": "turnDiff",
                            "diff": diff,
                            "status": "completed",
                        },
                        {
                            "id": "change-1",
                            "type": "fileChange",
                            "status": "completed",
                            "changes": [{"path": "app.py", "diff": diff}],
                        },
                    ],
                }
            ],
            items_view="full",
            page_cursor="page-1",
            next_cursor=None,
            coordinates={"runtime_epoch": "epoch-1", "revision": 1},
        )

        tools = [
            tool
            for turn in page["turns"]
            for tool in turn.get("tools", [])
        ]
        self.assertEqual(len(tools), 2)
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertTrue(tool["outputTruncated"])
                self.assertGreater(tool["outputHeadLineCount"], 0)
                self.assertEqual(
                    tool["diff"]["omissionLineIndex"],
                    tool["outputHeadLineCount"],
                )
                self.assertEqual(
                    tool["diff"]["omittedChars"],
                    tool["outputOmittedChars"],
                )
                self.assertLess(len(tool["diff"]["lines"]), 30_000)
                self.assertLessEqual(len(tool["diff"]["lines"]), MAX_VISIBLE)
                self.assertTrue(
                    any(
                        "[Focus Web omitted " in line["text"]
                        for line in tool["diff"]["lines"]
                    )
                )

    def test_direct_projection_does_not_trust_marker_or_dict_metadata(self) -> None:
        marker = (
            "[Focus Web omitted 999 characters of tool output; "
            "showing a bounded head and tail.]"
        )

        projected = project_turns(
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "command-1",
                            "type": "commandExecution",
                            "aggregatedOutput": marker,
                            INTERNAL_PRESENTATION_METADATA_KEY: {
                                "aggregatedOutputOmittedChars": 999,
                            },
                        }
                    ],
                }
            ]
        )

        tool = projected[0]["tools"][0]
        self.assertEqual(tool["output"], [marker])
        self.assertNotIn("outputTruncated", tool)
        self.assertNotIn("outputOmittedChars", tool)
        self.assertNotIn("outputHeadLineCount", tool)

    def test_cached_generic_outputs_project_from_the_bounded_internal_payload(
        self,
    ) -> None:
        giant = "x" * (MAX_VISIBLE + 777)
        read_model = WebThreadReadModel()
        read_model.replace_turns(
            "thread-1",
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "mcp-1",
                            "type": "mcpToolCall",
                            "server": "server",
                            "tool": "tool",
                            "result": giant,
                        },
                        {"id": "plan-1", "type": "plan", "text": giant},
                    ],
                }
            ],
        )

        with patch("bot.web_runtime.projection.present_tool_output") as presenter:
            projected = project_turns(read_model.turns("thread-1"))

        presenter.assert_not_called()
        tools = projected[0]["tools"]
        self.assertEqual([tool["id"] for tool in tools], ["mcp-1", "plan-1"])
        for tool in tools:
            with self.subTest(tool=tool["id"]):
                self.assertTrue(tool["outputTruncated"])
                self.assertEqual(tool["outputOmittedChars"], 777)
                boundary = tool["outputHeadLineCount"]
                self.assertEqual(
                    tool["output"][boundary],
                    "[Focus Web omitted 777 characters of tool output; "
                    "showing a bounded head and tail.]",
                )

    def test_exact_count_spoof_does_not_change_the_cached_boundary(self) -> None:
        spoof = (
            "[Focus Web omitted 1000 characters of tool output; "
            "showing a bounded head and tail.]"
        )
        original = f"{spoof}\n" + "x" * (MAX_VISIBLE + 1000 - len(spoof) - 1)
        read_model = WebThreadReadModel()
        read_model.replace_turns(
            "thread-1",
            [
                {
                    "id": "turn-1",
                    "items": [
                        {
                            "id": "command-1",
                            "type": "commandExecution",
                            "aggregatedOutput": original,
                        }
                    ],
                }
            ],
        )

        tool = project_turns(read_model.turns("thread-1"))[0]["tools"][0]

        self.assertEqual(tool["output"][0], spoof)
        self.assertEqual(tool["outputOmittedChars"], 1000)
        self.assertGreater(tool["outputHeadLineCount"], 0)
        self.assertEqual(
            tool["output"][tool["outputHeadLineCount"]],
            spoof,
        )

    def test_full_page_has_one_aggregate_budget_across_raw_turns(self) -> None:
        original_chars = 100_000
        raw_turns = [
            {
                "id": f"turn-{turn_index}",
                "items": [
                    {
                        "id": f"command-{turn_index}-{tool_index}",
                        "type": "commandExecution",
                        "aggregatedOutput": "x" * original_chars,
                    }
                    for tool_index in range(4)
                ],
            }
            for turn_index in range(10)
        ]

        page = project_turn_page(
            raw_turns,
            items_view="full",
            page_cursor="page-1",
            next_cursor=None,
            coordinates={"runtime_epoch": "epoch-1", "revision": 1},
        )
        tools = [
            tool
            for turn in page["turns"]
            for tool in turn.get("tools", [])
        ]
        visible = ["\n".join(tool["output"]) for tool in tools if tool["output"]]
        self.assertLessEqual(
            sum(len(output) for output in visible),
            MAX_TOOL_OUTPUT_WINDOW_CHARS,
        )
        self.assertLessEqual(len(visible), MAX_TOOL_OUTPUT_WINDOW_OUTPUTS)
        fully_omitted = [tool for tool in tools if not tool["output"]]
        self.assertGreater(len(fully_omitted), 0)
        for tool in fully_omitted:
            self.assertTrue(tool["outputTruncated"])
            self.assertEqual(tool["outputOmittedChars"], original_chars)
            self.assertEqual(tool["outputHeadLineCount"], 0)

    def test_file_change_hidden_aggregate_does_not_consume_card_budget(self) -> None:
        raw_turn = {
            "id": "turn-1",
            "items": [
                {
                    "id": "change-1",
                    "type": "fileChange",
                    "aggregatedOutput": "hidden" * 20_000,
                    "changes": [
                        {
                            "path": f"file-{index}.txt",
                            "diff": "x" * 1_000,
                        }
                        for index in range(MAX_TOOL_OUTPUT_WINDOW_OUTPUTS + 1)
                    ],
                }
            ],
        }
        direct = project_turns([raw_turn])[0]["tools"]
        read_model = WebThreadReadModel()
        read_model.replace_turns("thread-1", [raw_turn])
        cached_item = read_model.turns("thread-1")[0]["items"][0]
        cached = project_turns(read_model.turns("thread-1"))[0]["tools"]

        self.assertNotIn("aggregatedOutput", cached_item)
        self.assertEqual(
            [
                (
                    tool["output"],
                    tool.get("outputOmittedChars"),
                    tool.get("outputHeadLineCount"),
                )
                for tool in cached
            ],
            [
                (
                    tool["output"],
                    tool.get("outputOmittedChars"),
                    tool.get("outputHeadLineCount"),
                )
                for tool in direct
            ],
        )
        self.assertEqual(sum(bool(tool["output"]) for tool in cached), 16)
        self.assertEqual(cached[-1]["output"], [])
        self.assertEqual(cached[-1]["outputOmittedChars"], 1_000)
        self.assertEqual(cached[-1]["diff"]["omissionLineIndex"], 0)

    def test_heterogeneous_cache_projection_matches_direct_aggregate_order(self) -> None:
        payload = "x" * 30_000
        raw_turn = {
            "id": "turn-1",
            "items": [
                {"id": "plan", "type": "plan", "text": payload},
                {
                    "id": "mcp",
                    "type": "mcpToolCall",
                    "server": "server",
                    "tool": "tool",
                    "result": payload,
                },
                {
                    "id": "change",
                    "type": "fileChange",
                    "aggregatedOutput": "hidden" * 20_000,
                    "changes": [{"path": "app.py", "diff": payload}],
                },
                {
                    "id": "collab",
                    "type": "collabAgentToolCall",
                    "tool": "spawnAgent",
                    "receiverThreadIds": ["child"],
                    "agentsStates": {
                        "child": {"status": "completed", "message": payload}
                    },
                },
                {
                    "id": "command",
                    "type": "commandExecution",
                    "aggregatedOutput": payload,
                },
            ],
        }
        direct = project_turns([raw_turn])[0]["tools"]
        read_model = WebThreadReadModel()
        read_model.replace_turns("thread-1", [raw_turn])
        cached = project_turns(read_model.turns("thread-1"))[0]["tools"]

        def signature(tools: list[dict]) -> list[tuple]:
            return [
                (
                    tool["id"],
                    tool["output"],
                    tool.get("outputOmittedChars"),
                    tool.get("outputHeadLineCount"),
                )
                for tool in tools
            ]

        self.assertEqual(signature(cached), signature(direct))
        self.assertEqual(sum(bool(tool["output"]) for tool in cached), 5)

    def test_fully_omitted_structured_diff_has_a_zero_boundary(self) -> None:
        diff = "+x\n" * 30_000
        page = project_turn_page(
            [
                {
                    "id": "turn-1",
                    "items": [
                        {
                            "id": f"diff-{index}",
                            "type": "turnDiff",
                            "diff": diff,
                        }
                        for index in range(8)
                    ],
                }
            ],
            items_view="full",
            page_cursor="page-1",
            next_cursor=None,
            coordinates={"runtime_epoch": "epoch-1", "revision": 1},
        )

        tools = [
            tool
            for turn in page["turns"]
            for tool in turn.get("tools", [])
        ]
        fully_omitted = next(tool for tool in tools if not tool["output"])
        self.assertEqual(fully_omitted["outputOmittedChars"], len(diff))
        self.assertEqual(fully_omitted["outputHeadLineCount"], 0)
        self.assertEqual(fully_omitted["diff"]["lines"], [])
        self.assertEqual(fully_omitted["diff"]["omittedChars"], len(diff))
        self.assertEqual(fully_omitted["diff"]["omissionLineIndex"], 0)


if __name__ == "__main__":
    unittest.main()
