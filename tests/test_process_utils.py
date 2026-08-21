from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from bot import process_utils


class ProcessIdentityTests(unittest.TestCase):
    def test_non_proc_posix_identity_uses_locale_stable_process_start(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout="Fri Jul 31 10:11:12 2026\n",
            stderr="",
        )
        with patch("bot.process_utils.subprocess.run", return_value=completed) as run:
            identity = process_utils._posix_process_identity(123)

        self.assertEqual(identity, "posix:Fri Jul 31 10:11:12 2026")
        self.assertEqual(run.call_args.args[0], ["ps", "-o", "lstart=", "-p", "123"])
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")

    def test_non_proc_posix_identity_is_unknown_when_ps_fails(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ps"],
            returncode=1,
            stdout="",
            stderr="not found",
        )
        with patch("bot.process_utils.subprocess.run", return_value=completed):
            self.assertEqual(process_utils._posix_process_identity(123), "")


class ProcessUtilsTests(unittest.TestCase):
    def test_process_exists_treats_linux_zombie_as_not_running(self) -> None:
        with patch("bot.process_utils.os.kill", return_value=None):
            with patch("bot.process_utils._linux_process_state", return_value="Z"):
                self.assertFalse(process_utils.process_exists(1234))


if __name__ == "__main__":
    unittest.main()
