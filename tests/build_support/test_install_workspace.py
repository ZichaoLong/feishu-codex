from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest


@unittest.skipIf(
    os.name == "nt" or shutil.which("bash") is None,
    "install_workspace.sh requires Unix bash",
)
class InstallWorkspaceScriptTests(unittest.TestCase):
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "install_workspace.sh"

    @staticmethod
    def _write_executable(path: pathlib.Path, content: str) -> None:
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        path.chmod(0o755)

    def test_help_does_not_require_build_tools(self) -> None:
        result = subprocess.run(
            ["bash", str(self.script), "--help"],
            cwd=self.repo_root,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scripts/install_workspace.sh", result.stdout)
        self.assertIn("npm --prefix web ci", result.stdout)

    def test_rejects_artifact_source_override(self) -> None:
        environment = {
            **os.environ,
            "FOCUS_INSTALL_PYTHON": "/missing/focus-test-python",
        }
        result = subprocess.run(
            ["bash", str(self.script), "--artifact", "another.zip"],
            cwd=self.repo_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--migrate-from-feishu-codex", result.stderr)

    def test_builds_installs_exact_bundle_and_cleans_temporary_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            fake_bin = root / "fake bin"
            fake_bin.mkdir()
            invocation_dir = root / "invocation dir"
            invocation_dir.mkdir()
            temporary_root = root / "temporary artifacts"
            temporary_root.mkdir()
            npm_args = root / "npm-args.txt"
            built_bundle = root / "built-bundle.txt"
            install_args = root / "install-args.txt"
            installed_artifact = root / "installed-artifact.txt"

            self._write_executable(
                fake_bin / "npm",
                """
                #!/usr/bin/env bash
                set -euo pipefail
                printf '%s\n' "$@" > "$FOCUS_TEST_NPM_ARGS"
                """,
            )
            fake_python = fake_bin / "custom python"
            self._write_executable(
                fake_python,
                """
                #!/usr/bin/env bash
                set -euo pipefail

                if [[ "${1:-}" == "-c" ]]; then
                  exit 0
                fi

                entry="${1:-}"
                shift
                case "$entry" in
                  */scripts/build_install_bundle.py)
                    output_dir=""
                    while [[ "$#" -gt 0 ]]; do
                      case "$1" in
                        --output-dir)
                          output_dir="$2"
                          shift 2
                          ;;
                        *)
                          shift
                          ;;
                      esac
                    done
                    [[ -n "$output_dir" ]]
                    mkdir -p "$output_dir"
                    bundle="$output_dir/focus-install-test.zip"
                    printf 'bundle\n' > "$bundle"
                    printf '%s\n' "$bundle" > "$FOCUS_TEST_BUILT_BUNDLE"
                    printf 'bundle=%s\n' "$bundle"
                    ;;
                  */install.py)
                    printf '%s\n' "$entry" "$@" > "$FOCUS_TEST_INSTALL_ARGS"
                    artifact=""
                    while [[ "$#" -gt 0 ]]; do
                      case "$1" in
                        --artifact)
                          artifact="$2"
                          shift 2
                          ;;
                        *)
                          shift
                          ;;
                      esac
                    done
                    [[ -f "$artifact" ]]
                    printf '%s\n' "$artifact" > "$FOCUS_TEST_INSTALLED_ARTIFACT"
                    ;;
                  *)
                    echo "unexpected Python entry: $entry" >&2
                    exit 97
                    ;;
                esac
                """,
            )
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "TMPDIR": str(temporary_root),
                "FOCUS_INSTALL_PYTHON": str(fake_python),
                "FOCUS_TEST_NPM_ARGS": str(npm_args),
                "FOCUS_TEST_BUILT_BUNDLE": str(built_bundle),
                "FOCUS_TEST_INSTALL_ARGS": str(install_args),
                "FOCUS_TEST_INSTALLED_ARTIFACT": str(installed_artifact),
            }

            result = subprocess.run(
                ["bash", str(self.script), "--migrate-from-feishu-codex"],
                cwd=invocation_dir,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                npm_args.read_text(encoding="utf-8").splitlines(),
                ["--prefix", str(self.repo_root / "web"), "run", "build"],
            )
            bundle_path = pathlib.Path(
                built_bundle.read_text(encoding="utf-8").strip()
            )
            self.assertEqual(
                pathlib.Path(installed_artifact.read_text(encoding="utf-8").strip()),
                bundle_path,
            )
            self.assertEqual(
                install_args.read_text(encoding="utf-8").splitlines(),
                [
                    str(self.repo_root / "install.py"),
                    "--artifact",
                    str(bundle_path),
                    "--migrate-from-feishu-codex",
                ],
            )
            self.assertFalse(bundle_path.parent.exists())


if __name__ == "__main__":
    unittest.main()
