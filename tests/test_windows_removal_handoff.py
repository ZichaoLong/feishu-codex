from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from bot.windows_removal_handoff import (
    WindowsRemovalHandoffError,
    WindowsRemovalTarget,
    prepare_windows_removal_handoff,
)


class _HelperProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, *, timeout: float) -> int:
        del timeout
        return 0

    def kill(self) -> None:
        self.killed = True


class WindowsRemovalHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = pathlib.Path(self.tempdir.name)
        self.data_root = self.root / "focus-data"
        self.data_root.mkdir()
        self.machine_lock = self.root / ".focus-data.managed-install.lock"
        self.staging_parent = self.root / "staging"

    def prepare(self, *targets: WindowsRemovalTarget):
        return prepare_windows_removal_handoff(
            operation="uninstall",
            parent_pid=1234,
            machine_lock_path=self.machine_lock,
            targets=tuple(targets),
            powershell_executable="powershell.exe",
            staging_parent=self.staging_parent,
        )

    def test_plan_and_script_pin_exact_targets_and_result_protocol(self) -> None:
        venv = self.data_root / ".venv"
        handoff = self.prepare(WindowsRemovalTarget("managed_venv", venv))
        self.addCleanup(handoff.discard)

        plan = json.loads(handoff.plan_path.read_text(encoding="utf-8"))
        script = handoff.script_path.read_text(encoding="utf-8")

        self.assertEqual(
            set(plan),
            {
                "schema",
                "handoff_id",
                "operation",
                "parent_pid",
                "handoff_lock_path",
                "targets",
            },
        )
        self.assertEqual(plan["schema"], 1)
        self.assertEqual(plan["operation"], "uninstall")
        self.assertEqual(plan["parent_pid"], 1234)
        self.assertEqual(
            plan["handoff_lock_path"],
            str(self.machine_lock.resolve()) + ".handoff",
        )
        self.assertEqual(
            plan["targets"],
            [{"path": str(venv.resolve()), "role": "managed_venv"}],
        )
        self.assertIn("$null = $parent.Handle", script)
        self.assertIn("$parent.WaitForExit()", script)
        self.assertIn("$handoffLock.Lock(0, 1)", script)
        self.assertIn("status = 'armed'", script)
        self.assertIn("Remove-Item -LiteralPath $path -Recurse -Force", script)
        self.assertIn("status = $status", script)
        self.assertIn("target root became a reparse point", script)
        self.assertNotIn(str(venv), script)

    def test_rejects_overlapping_targets_and_lock_inside_target(self) -> None:
        with self.assertRaisesRegex(WindowsRemovalHandoffError, "互相包含"):
            prepare_windows_removal_handoff(
                operation="purge",
                parent_pid=1234,
                machine_lock_path=self.machine_lock,
                targets=(
                    WindowsRemovalTarget("data", self.data_root),
                    WindowsRemovalTarget("venv", self.data_root / ".venv"),
                ),
                powershell_executable="powershell.exe",
                staging_parent=self.staging_parent,
            )

        with self.assertRaisesRegex(WindowsRemovalHandoffError, "machine lock"):
            prepare_windows_removal_handoff(
                operation="purge",
                parent_pid=1234,
                machine_lock_path=self.data_root / "install.lock",
                targets=(WindowsRemovalTarget("data", self.data_root),),
                powershell_executable="powershell.exe",
                staging_parent=self.staging_parent,
            )

    def test_rejects_symlink_target_and_staging_inside_target(self) -> None:
        real_target = self.root / "real-venv"
        real_target.mkdir()
        symlink_target = self.data_root / ".venv"
        try:
            symlink_target.symlink_to(real_target, target_is_directory=True)
        except OSError as exc:  # pragma: no cover - platform capability
            self.skipTest(f"symlink unavailable: {exc}")

        with self.assertRaisesRegex(WindowsRemovalHandoffError, "符号链接"):
            self.prepare(WindowsRemovalTarget("managed_venv", symlink_target))

        with self.assertRaisesRegex(WindowsRemovalHandoffError, "staging"):
            prepare_windows_removal_handoff(
                operation="purge",
                parent_pid=1234,
                machine_lock_path=self.machine_lock,
                targets=(WindowsRemovalTarget("data", self.data_root),),
                powershell_executable="powershell.exe",
                staging_parent=self.data_root / "temporary",
            )

    def test_matching_armed_proof_returns_handoff_only(self) -> None:
        handoff = self.prepare(
            WindowsRemovalTarget("managed_venv", self.data_root / ".venv")
        )
        process = _HelperProcess()

        def launch(command, **kwargs):
            self.assertEqual(command[0], "powershell.exe")
            self.assertIn(str(handoff.plan_path), command)
            self.assertIsNotNone(kwargs["stdin"])
            handoff.armed_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "handoff_id": handoff.handoff_id,
                        "parent_pid": 1234,
                        "helper_pid": 4321,
                        "status": "armed",
                    }
                ),
                encoding="utf-8",
            )
            return process

        with patch("bot.windows_removal_handoff.subprocess.Popen", side_effect=launch):
            receipt = handoff.launch(arm_timeout_seconds=0.1)

        self.assertEqual(receipt.handoff_id, handoff.handoff_id)
        self.assertEqual(receipt.helper_pid, 4321)
        self.assertEqual(receipt.result_path, handoff.result_path)
        self.assertFalse(process.terminated)
        handoff.discard()
        self.assertTrue(handoff.staging_dir.exists())

    def test_missing_armed_proof_stops_helper_before_parent_exit(self) -> None:
        handoff = self.prepare(
            WindowsRemovalTarget("managed_venv", self.data_root / ".venv")
        )
        process = _HelperProcess()

        with patch("bot.windows_removal_handoff.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(WindowsRemovalHandoffError, "handoff barrier"):
                handoff.launch(arm_timeout_seconds=0.01)

        self.assertTrue(process.terminated)
        handoff.discard()
        self.assertFalse(handoff.staging_dir.exists())

    def test_mismatched_armed_proof_stops_helper_before_parent_exit(self) -> None:
        handoff = self.prepare(
            WindowsRemovalTarget("managed_venv", self.data_root / ".venv")
        )
        process = _HelperProcess()

        def launch(*_args, **_kwargs):
            handoff.armed_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "handoff_id": "wrong",
                        "parent_pid": 1234,
                        "helper_pid": 4321,
                        "status": "armed",
                    }
                ),
                encoding="utf-8",
            )
            return process

        with patch("bot.windows_removal_handoff.subprocess.Popen", side_effect=launch):
            with self.assertRaisesRegex(WindowsRemovalHandoffError, "armed proof"):
                handoff.launch(arm_timeout_seconds=0.1)

        self.assertTrue(process.terminated)
        handoff.discard()
        self.assertFalse(handoff.staging_dir.exists())


if __name__ == "__main__":
    unittest.main()
