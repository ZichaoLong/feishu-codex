from __future__ import annotations

import unittest

from scripts import focus_capabilities


class DevelopFocusSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill_root = (
            focus_capabilities.REPO_ROOT / ".agents/skills/develop-focus"
        )

    def test_skill_is_a_small_explicit_router(self) -> None:
        files = tuple(
            sorted(
                path.relative_to(self.skill_root).as_posix()
                for path in self.skill_root.rglob("*")
                if path.is_file()
            )
        )
        self.assertEqual(files, ("SKILL.md", "agents/openai.yaml"))

        text = (self.skill_root / "SKILL.md").read_text(encoding="utf-8")
        self.assertGreaterEqual(len(text.splitlines()), 30)
        self.assertLessEqual(len(text.splitlines()), 50)
        for forbidden in ("TODO", "```", "docs/_work", "git diff", "git status"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)
        self.assertIn("AGENTS.md", text)
        self.assertIn("docs/architecture/development-navigation.zh-CN.md", text)
        self.assertIn("$navigate-focus-development", text)
        self.assertIn("docs/contracts/install-artifact-delivery.zh-CN.md", text)
        self.assertIn("validation run is not publication", text)
        self.assertIn("feature implementation", text)
        self.assertIn("behavior change", text)

        discipline = (
            focus_capabilities.REPO_ROOT
            / "docs/architecture/development-navigation.zh-CN.md"
        ).read_text(encoding="utf-8")
        self.assertIn("功能实现、行为变更与重构", discipline)
        self.assertIn("合同、代码、测试、guard 与导航影响", discipline)

    def test_agents_template_keeps_upstream_paths_task_local(self) -> None:
        text = (focus_capabilities.REPO_ROOT / "AGENTS.example.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "strictly read-only by default",
            "task-local parameter",
            "never persist it",
            "full 40-character commit",
            "pre-existing differences untouched",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_agents_template_requires_upstream_parity_before_local_machinery(
        self,
    ) -> None:
        text = (focus_capabilities.REPO_ROOT / "AGENTS.example.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "A bounded non-guarantee can be the correct contract",
            "Stable fallback",
            "Forward upstream-owned fields semantically unchanged",
            "Do not whitelist, strip, normalize",
            "upstream-parity and subtraction option first",
            "do not add polling, persistence, replay, quarantine",
            "measurable reduction in user risk",
            "net complexity",
            "before any production edit",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_metadata_requires_explicit_invocation(self) -> None:
        text = (self.skill_root / "agents/openai.yaml").read_text(encoding="utf-8")
        self.assertIn('default_prompt: "使用 $develop-focus ', text)
        self.assertIn("allow_implicit_invocation: false", text)


if __name__ == "__main__":
    unittest.main()
