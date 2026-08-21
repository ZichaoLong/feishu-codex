from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import check_source_context


class SourceContextGuardTests(unittest.TestCase):
    @staticmethod
    def _policy(
        reviewed_sources: dict[str, check_source_context.ReviewedSource],
    ) -> check_source_context.SourceContextPolicy:
        return check_source_context.SourceContextPolicy(
            alignment_threshold_bytes=100,
            alignment_threshold_lines=10,
            review_threshold_lines=5,
            reviewed_sources=reviewed_sources,
        )

    def test_scan_contract_covers_product_source_roots_and_extensions(self) -> None:
        self.assertEqual(
            check_source_context._SOURCE_ROOTS,
            (
                ".agents",
                "bot",
                "tests",
                "web/public",
                "web/src",
                "web/test",
                "web/scripts",
                "scripts",
            ),
        )
        self.assertEqual(
            check_source_context._TOP_LEVEL_SOURCE_CONTAINERS,
            ("", "web"),
        )
        self.assertTrue(
            {".py", ".ts", ".tsx", ".vue", ".js", ".mjs", ".css"}
            <= check_source_context._SOURCE_SUFFIXES
        )

    def test_scan_includes_source_roots_and_skips_generated_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            source_files = {
                "install.py": b"installer",
                ".agents/skills/example/scripts/helper.py": b"skill",
                "bot/domain.py": b"python",
                "bot/crlf.py": b"line one\r\nline two\r\n",
                "tests/domain_test.py": b"test",
                "web/vite.config.ts": b"config",
                "web/public/boot.js": b"boot",
                "web/src/component.vue": b"vue",
                "web/src/theme.css": b"css",
                "web/test/component.test.ts": b"typescript",
                "web/scripts/check.mjs": b"javascript",
                "scripts/check.sh": b"shell",
            }
            ignored_files = {
                "bot/web_assets/dist/bundle.js": b"generated",
                "tests/__pycache__/misleading.py": b"generated",
                "web/src/coverage/report.js": b"generated",
                "web/src/icon.svg": b"not-scanned",
            }
            for relative, content in {**source_files, **ignored_files}.items():
                path = repo_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            for root in check_source_context._SOURCE_ROOTS:
                (repo_root / root).mkdir(parents=True, exist_ok=True)

            with patch.object(check_source_context, "_REPO_ROOT", repo_root):
                metrics = check_source_context._source_metrics()

        self.assertEqual(
            metrics,
            {
                path: check_source_context.SourceMetrics(
                    normalized_bytes=len(content.replace(b"\r\n", b"\n")),
                    lines=(
                        content.replace(b"\r\n", b"\n").count(b"\n")
                        + int(
                            bool(content)
                            and not content.replace(b"\r\n", b"\n").endswith(b"\n")
                        )
                    ),
                )
                for path, content in source_files.items()
            },
        )

    def test_missing_configured_source_root_is_a_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "bot").mkdir()
            with (
                patch.object(check_source_context, "_REPO_ROOT", repo_root),
                patch.object(check_source_context, "_SOURCE_ROOTS", ("bot", "web/src")),
                self.assertRaisesRegex(ValueError, "configured source root.*web/src"),
            ):
                check_source_context._source_metrics()

    def test_policy_schema_names_review_and_alignment_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "source-context-policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "alignment_threshold_bytes": 100,
                        "alignment_threshold_lines": 10,
                        "review_threshold_lines": 5,
                        "reviewed_sources": {
                            "bot/large.py": {
                                "category": "focus_owned",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(check_source_context, "_POLICY_PATH", policy_path):
                self.assertEqual(
                    check_source_context._load_policy(),
                    check_source_context.SourceContextPolicy(
                        alignment_threshold_bytes=100,
                        alignment_threshold_lines=10,
                        review_threshold_lines=5,
                        reviewed_sources={
                            "bot/large.py": check_source_context.ReviewedSource(
                                category="focus_owned",
                            )
                        },
                    ),
                )

    def test_old_ambiguous_budget_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "source-context-policy.json"
            policy_path.write_text(
                json.dumps({"max_unlisted_bytes": 100, "legacy_file_budgets": {}}),
                encoding="utf-8",
            )

            with (
                patch.object(check_source_context, "_POLICY_PATH", policy_path),
                self.assertRaisesRegex(
                    ValueError,
                    "alignment_threshold_bytes",
                ),
            ):
                check_source_context._load_policy()

    def test_reviewed_path_must_belong_to_the_scanned_source_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "source-context-policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "alignment_threshold_bytes": 100,
                        "alignment_threshold_lines": 10,
                        "review_threshold_lines": 5,
                        "reviewed_sources": {
                            "bot/web_assets/dist/generated.js": {
                                "category": "focus_owned",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(check_source_context, "_POLICY_PATH", policy_path),
                self.assertRaisesRegex(ValueError, "outside the scanned source set"),
            ):
                check_source_context._load_policy()

    def test_reviewed_source_category_must_be_known(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "source-context-policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "alignment_threshold_bytes": 100,
                        "alignment_threshold_lines": 10,
                        "review_threshold_lines": 5,
                        "reviewed_sources": {
                            "bot/reviewed.py": {
                                "category": "unknown",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(check_source_context, "_POLICY_PATH", policy_path),
                self.assertRaisesRegex(ValueError, "reviewed source category"),
            ):
                check_source_context._load_policy()

    def test_reviewed_source_growth_and_shrink_do_not_form_a_size_ratchet(self) -> None:
        with (
            patch.object(
                check_source_context,
                "_load_policy",
                return_value=self._policy(
                    {
                        "bot/growing.py": check_source_context.ReviewedSource(
                            category="focus_owned",
                        ),
                        "bot/shrinking.py": check_source_context.ReviewedSource(
                            category="focus_owned",
                        ),
                    }
                ),
            ),
            patch.object(
                check_source_context,
                "_source_metrics",
                return_value={
                    "bot/growing.py": check_source_context.SourceMetrics(140, 14),
                    "bot/shrinking.py": check_source_context.SourceMetrics(80, 4),
                },
            ),
        ):
            self.assertEqual(
                check_source_context.check(),
                check_source_context.SourceContextReport(errors=(), warnings=()),
            )

    def test_unreviewed_line_or_byte_alignment_threshold_requires_decision(
        self,
    ) -> None:
        with (
            patch.object(
                check_source_context,
                "_load_policy",
                return_value=self._policy(
                    {
                        "bot/reviewed.py": check_source_context.ReviewedSource(
                            category="focus_owned",
                        )
                    }
                ),
            ),
            patch.object(
                check_source_context,
                "_source_metrics",
                return_value={
                    "bot/reviewed.py": check_source_context.SourceMetrics(140, 14),
                    "bot/new-bytes.py": check_source_context.SourceMetrics(100, 4),
                    "bot/new-lines.py": check_source_context.SourceMetrics(80, 10),
                },
            ),
        ):
            report = check_source_context.check()

        self.assertEqual(len(report.errors), 2)
        self.assertTrue(
            any("bot/new-bytes.py" in error for error in report.errors)
        )
        self.assertTrue(
            any("bot/new-lines.py" in error for error in report.errors)
        )
        self.assertEqual(report.warnings, ())

    def test_deleted_reviewed_source_requires_inventory_cleanup(self) -> None:
        with (
            patch.object(
                check_source_context,
                "_load_policy",
                return_value=self._policy(
                    {
                        "bot/large.py": check_source_context.ReviewedSource(
                            category="focus_owned",
                        )
                    }
                ),
            ),
            patch.object(check_source_context, "_source_metrics", return_value={}),
        ):
            report = check_source_context.check()

        self.assertEqual(
            report.errors,
            (
                "reviewed source inventory points to a missing scanned source file: "
                "bot/large.py",
            ),
        )

    def test_review_threshold_is_inclusive_machine_readable_and_non_failing(
        self,
    ) -> None:
        with (
            patch.object(
                check_source_context,
                "_load_policy",
                return_value=self._policy({}),
            ),
            patch.object(
                check_source_context,
                "_source_metrics",
                return_value={
                    "bot/review.py": check_source_context.SourceMetrics(90, 5),
                    "bot/below.py": check_source_context.SourceMetrics(90, 4),
                },
            ),
        ):
            report = check_source_context.check()

        self.assertEqual(report.errors, ())
        self.assertEqual(len(report.warnings), 1)
        warning = report.warnings[0]
        self.assertEqual(warning.path, "bot/review.py")
        prefix, payload = warning.render().split(" ", 1)
        self.assertEqual(prefix, "SOURCE_CONTEXT_WARNING")
        self.assertEqual(
            json.loads(payload),
            {
                "actual_bytes": 90,
                "actual_lines": 5,
                "alignment_threshold_bytes": 100,
                "alignment_threshold_lines": 10,
                "path": "bot/review.py",
                "review_threshold_lines": 5,
            },
        )

    def test_alignment_threshold_boundaries_require_review(self) -> None:
        with (
            patch.object(
                check_source_context,
                "_load_policy",
                return_value=self._policy({}),
            ),
            patch.object(
                check_source_context,
                "_source_metrics",
                return_value={
                    "bot/boundary.py": check_source_context.SourceMetrics(100, 10)
                },
            ),
        ):
            report = check_source_context.check()

        self.assertEqual(len(report.errors), 1)
        self.assertIn("bot/boundary.py", report.errors[0])
        self.assertEqual(report.warnings, ())

    def test_main_emits_warnings_but_returns_success(self) -> None:
        warning = check_source_context.SourceContextWarning(
            path="bot/review.py",
            actual=check_source_context.SourceMetrics(90, 6),
            review_threshold_lines=5,
            alignment_threshold_lines=10,
            alignment_threshold_bytes=100,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(
                check_source_context,
                "check",
                return_value=check_source_context.SourceContextReport(
                    errors=(),
                    warnings=(warning,),
                ),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = check_source_context.main()

        self.assertEqual(result, 0)
        self.assertIn("1 review warning(s)", stdout.getvalue())
        self.assertIn("SOURCE_CONTEXT_WARNING", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
