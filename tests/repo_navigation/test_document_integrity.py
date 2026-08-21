from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import check_document_integrity as document_integrity


class DocumentIntegrityTests(unittest.TestCase):
    def test_current_repository_documents_are_integral(self) -> None:
        self.assertEqual(document_integrity.repository_violations(), ())

        docs_gate = (document_integrity.REPO_ROOT / "scripts/check-docs.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("python scripts/check_document_integrity.py", docs_gate)

    def test_local_links_and_heading_anchors_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "README.md").write_text(
                "[ok](docs/target.md#现有章节)\n[bad](docs/missing.md)\n",
                encoding="utf-8",
            )
            (root / "docs" / "target.md").write_text(
                "# 现有章节\n[bad](#不存在)\n", encoding="utf-8"
            )

            violations = document_integrity.repository_violations(root)

        self.assertEqual(
            tuple(violation.kind for violation in violations),
            ("missing local link target", "missing local heading anchor"),
        )

    def test_numbered_bilingual_heading_shape_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = root / "docs" / "decisions"
            decisions.mkdir(parents=True)
            (decisions / "example.zh-CN.md").write_text(
                "# 示例\n\n## 1. 当前\n\n### 1.1 细节\n", encoding="utf-8"
            )
            (decisions / "example.md").write_text(
                "# Example\n\n## 1. Current\n", encoding="utf-8"
            )

            violations = document_integrity.repository_violations(root)

        self.assertIn(
            "bilingual numbered-heading mismatch",
            tuple(violation.kind for violation in violations),
        )

    def test_work_directory_allows_only_one_active_markdown_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "docs" / "_work"
            work.mkdir(parents=True)
            ledger = work / "campaign.md"
            ledger.write_text("# Campaign\n\n状态：active；临时。\n", encoding="utf-8")
            (work / "evidence.tsv").write_text("key\tvalue\n", encoding="utf-8")
            self.assertEqual(document_integrity.repository_violations(root), ())

            (work / "closed.md").write_text("# Closed\n\n状态：complete\n", encoding="utf-8")
            violations = document_integrity.repository_violations(root)

        self.assertEqual(
            tuple(violation.kind for violation in violations),
            ("invalid work lifecycle",),
        )

    def test_active_decisions_and_durable_docs_reject_historical_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = root / "docs" / "decisions"
            decisions.mkdir(parents=True)
            (decisions / "example.zh-CN.md").write_text(
                "# 示例\n\n> **部分内容已于今日被取代。**\n\n"
                "证据在 `docs/_work/old.md`。\n",
                encoding="utf-8",
            )

            violations = document_integrity.repository_violations(root)

        self.assertEqual(
            tuple(violation.kind for violation in violations),
            (
                "active decision declares superseded content",
                "durable document references a work file",
            ),
        )


if __name__ == "__main__":
    unittest.main()
