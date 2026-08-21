from __future__ import annotations

import unittest
from pathlib import PurePosixPath

from scripts import check_portable_upstream_references as portable_refs


class PortableUpstreamReferenceTests(unittest.TestCase):
    def kinds_for(self, text: str) -> tuple[str, ...]:
        return tuple(
            violation.kind
            for violation in portable_refs.find_violations("example.md", text)
        )

    def test_current_scanned_repository_surfaces_are_portable(self) -> None:
        self.assertEqual(portable_refs.repository_violations(), ())

        docs_gate = (portable_refs.REPO_ROOT / "scripts/check-docs.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "python scripts/check_portable_upstream_references.py", docs_gate
        )

    def test_rejects_machine_local_checkout_paths(self) -> None:
        kinds = self.kinds_for(
            "\n".join(
                (
                    "/home/alice/llm/codex/codex-rs",
                    "/home/alice/src/codex/codex-rs",
                    "/workspace/openai-codex/codex-rs",
                    "/opt/src/openai/codex",
                    "~/llm/codex",
                    "~/src/openai-codex",
                    "$HOME/llm/codex",
                    "${HOME}/src/codex",
                    r"$USERPROFILE\src\codex",
                    "../codex/codex-rs",
                    "../../vendor/openai-codex",
                    r"C:\Users\alice\llm\codex\codex-rs",
                    "D:/src/openai-codex/codex-rs",
                    r"\\server\share\codex\codex-rs",
                )
            )
        )
        self.assertEqual(kinds, ("machine-local upstream checkout",) * 14)

    def test_allows_runtime_paths_and_similarly_named_focus_surfaces(self) -> None:
        allowed = "\n".join(
            (
                "~/.codex/config.toml",
                "/home/alice/.codex/config.toml",
                r"C:\Users\alice\.codex\config.toml",
                "~/.local/share/feishu-codex",
                "/opt/fcodex",
                "/preserved/codex",
                "/stable/codex",
                "/usr/bin/codex",
                "../contracts/fcodex-operation-owner.md",
                "/usr/bin/codex-cli",
            )
        )
        self.assertEqual(self.kinds_for(allowed), ())

    def test_rejects_path_shorthand_without_false_positive_names(self) -> None:
        invalid = self.kinds_for(
            "\n".join(
                (
                    "codex@abcdef1:codex-rs/core/src/lib.rs",
                    "openai/codex@main:codex-rs/app-server/src/lib.rs",
                )
            )
        )
        self.assertEqual(invalid, ("non-portable upstream shorthand",) * 2)

        full_commit = "a" * 40
        allowed = "\n".join(
            (
                "feishu-codex@.service",
                "fcodex@corp-a",
                f"codex@{full_commit}",
                f"[openai/codex@{full_commit}]"
                f"(https://github.com/openai/codex/commit/{full_commit})",
            )
        )
        self.assertEqual(self.kinds_for(allowed), ())

    def test_rejects_unpinned_upstream_labels(self) -> None:
        kinds = self.kinds_for(
            "\n".join(
                (
                    "codex@main",
                    "openai/codex@rust-v0.147.0",
                    "openai/codex@" + "A" * 40,
                )
            )
        )
        self.assertEqual(kinds, ("unpinned upstream label",) * 3)

    def test_requires_full_lowercase_commit_in_source_permalinks(self) -> None:
        invalid = self.kinds_for(
            "\n".join(
                (
                    "https://github.com/openai/codex/blob/abcdef1/file.rs",
                    "https://github.com/openai/codex/tree/main/codex-rs",
                    "https://github.com/openai/codex/commit/"
                    + "A" * 40,
                )
            )
        )
        self.assertEqual(invalid, ("unpinned upstream permalink",) * 3)

        full_commit = "b" * 40
        allowed = "\n".join(
            (
                "https://github.com/openai/codex",
                "https://github.com/openai/codex.git",
                f"https://github.com/openai/codex/blob/{full_commit}/file.rs",
                f"https://github.com/openai/codex/tree/{full_commit}/codex-rs",
                f"https://github.com/openai/codex/commit/{full_commit}",
            )
        )
        self.assertEqual(self.kinds_for(allowed), ())

    def test_only_negative_syntax_fixture_is_excluded(self) -> None:
        self.assertTrue(
            portable_refs.is_scanned_surface(
                PurePosixPath("docs/_work/active-campaign.zh-CN.md")
            )
        )
        self.assertFalse(
            portable_refs.is_scanned_surface(
                PurePosixPath(
                    "tests/repo_navigation/test_portable_upstream_references.py"
                )
            )
        )
        self.assertTrue(
            portable_refs.is_scanned_surface(
                PurePosixPath("tests/fixtures/upstream-reference.txt")
            )
        )
        self.assertTrue(
            portable_refs.is_scanned_surface(PurePosixPath("bot/runtime.py"))
        )


if __name__ == "__main__":
    unittest.main()
