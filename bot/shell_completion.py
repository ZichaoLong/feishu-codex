"""Shell completion helpers for FOCUS command wrappers."""

from __future__ import annotations

import json
import os
import pathlib
import shlex
import sys
import textwrap
from dataclasses import dataclass

from bot.instance_layout import DEFAULT_INSTANCE_NAME, list_known_instance_names
from bot.platform_paths import (
    default_config_root,
    default_user_bash_completion_dir,
    default_user_powershell_completion_path,
    default_user_powershell_profile_path,
    default_user_zsh_completion_path,
    default_user_zsh_rc_path,
)

COMPLETION_COMMAND_NAMES = (
    "focus",
    "focusctl",
    "focusd",
    "fcodex",
)

_FOCUSCTL_OPTIONS_WITH_VALUE = {
    "--instance",
    "--binding-id",
    "--text",
    "--text-file",
    "--synthetic-source",
    "--display-mode",
    "--actor-open-id",
    "--scope",
    "--cwd",
    "--thread-id",
    "--thread-name",
    "--mode",
    "--path",
    "--lines",
}
_FOCUS_TUI_OPTIONS_WITH_VALUE = {
    "-C",
    "--add-dir",
    "-a",
    "--ask-for-approval",
    "-c",
    "--config",
    "--cd",
    "--disable",
    "--enable",
    "-i",
    "--image",
    "--instance",
    "--local-provider",
    "-m",
    "--model",
    "-p",
    "--profile",
    "--remote",
    "--remote-auth-token-env",
    "-s",
    "--sandbox",
}

_ZSH_PROFILE_BLOCK_START = "# >>> focus zsh completion >>>"
_ZSH_PROFILE_BLOCK_END = "# <<< focus zsh completion <<<"
_POWERSHELL_PROFILE_BLOCK_START = "# >>> focus PowerShell completion >>>"
_POWERSHELL_PROFILE_BLOCK_END = "# <<< focus PowerShell completion <<<"
_POWERSHELL_COMPLETION_METADATA_FILE = "powershell-install-paths.json"


@dataclass(frozen=True, slots=True)
class CompletionContext:
    words: tuple[str, ...]
    cword: int

    @property
    def current(self) -> str:
        if 0 <= self.cword < len(self.words):
            return self.words[self.cword]
        return ""

    @property
    def previous(self) -> str:
        if self.cword <= 0:
            return ""
        return self.words[self.cword - 1]

    @property
    def args_before_cursor(self) -> tuple[str, ...]:
        if self.cword <= 1:
            return ()
        return self.words[1 : min(self.cword, len(self.words))]


@dataclass(frozen=True, slots=True)
class CompletionInstallResult:
    bash_dir: pathlib.Path | None = None
    zsh_script_path: pathlib.Path | None = None
    zsh_rc_path: pathlib.Path | None = None
    powershell_script_path: pathlib.Path | None = None
    powershell_profile_path: pathlib.Path | None = None


def bash_completion_dir() -> pathlib.Path | None:
    return default_user_bash_completion_dir()


def bash_completion_file_paths() -> list[pathlib.Path]:
    directory = bash_completion_dir()
    if directory is None:
        return []
    return [directory / command_name for command_name in COMPLETION_COMMAND_NAMES]


def zsh_completion_path() -> pathlib.Path | None:
    return default_user_zsh_completion_path()


def zsh_rc_path() -> pathlib.Path | None:
    return default_user_zsh_rc_path()


def powershell_completion_path() -> pathlib.Path | None:
    return default_user_powershell_completion_path()


def powershell_profile_path() -> pathlib.Path | None:
    return default_user_powershell_profile_path()


def _powershell_completion_metadata_path() -> pathlib.Path:
    return default_config_root() / "shell-completion" / _POWERSHELL_COMPLETION_METADATA_FILE


def _read_powershell_completion_metadata() -> tuple[pathlib.Path | None, pathlib.Path | None]:
    path = _powershell_completion_metadata_path()
    if not path.exists():
        return None, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(raw, dict):
        return None, None
    script_raw = str(raw.get("script_path", "") or "").strip()
    profile_raw = str(raw.get("profile_path", "") or "").strip()
    script_path = pathlib.Path(script_raw).expanduser() if script_raw else None
    profile_path = pathlib.Path(profile_raw).expanduser() if profile_raw else None
    return script_path, profile_path


def _write_powershell_completion_metadata(
    *,
    script_path: pathlib.Path,
    profile_path: pathlib.Path | None,
) -> None:
    path = _powershell_completion_metadata_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "script_path": str(script_path),
                "profile_path": str(profile_path) if profile_path is not None else "",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _powershell_profile_autoload_disabled() -> bool:
    raw = os.environ.get("FOCUS_POWERSHELL_SKIP_PROFILE_AUTOLOAD", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _remove_powershell_completion_metadata() -> None:
    path = _powershell_completion_metadata_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _dedupe_paths(*paths: pathlib.Path | None) -> tuple[pathlib.Path, ...]:
    ordered: list[pathlib.Path] = []
    seen: set[str] = set()
    for candidate in paths:
        if candidate is None:
            continue
        normalized = pathlib.Path(candidate).expanduser()
        key = str(normalized)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(normalized)
    return tuple(ordered)


def render_bash_completion_script(*, venv_python: pathlib.Path) -> str:
    python_command = shlex.quote(str(pathlib.Path(venv_python)))
    return textwrap.dedent(
        f"""\
        # Bash completion for FOCUS wrappers.

        _fc_complete_dispatch() {{
          local command_name="$1"
          local output
          COMPREPLY=()
          output=$({python_command} -m bot.shell_completion complete "$command_name" "$COMP_CWORD" "${{COMP_WORDS[@]}}" 2>/dev/null) || return 0
          [[ -n "$output" ]] || return 0
          while IFS= read -r line; do
            [[ -n "$line" ]] || continue
            COMPREPLY+=("$line")
          done <<< "$output"
        }}

        _focus_complete_focus() {{
          _fc_complete_dispatch focus
        }}

        _focus_complete_focusctl() {{
          _fc_complete_dispatch focusctl
        }}

        _focus_complete_focusd() {{
          _fc_complete_dispatch focusd
        }}

        _focus_complete_fcodex() {{
          _fc_complete_dispatch fcodex
        }}

        complete -o bashdefault -o default -F _focus_complete_focus focus
        complete -o bashdefault -o default -F _focus_complete_focusctl focusctl
        complete -o bashdefault -o default -F _focus_complete_focusd focusd
        complete -o bashdefault -o default -F _focus_complete_fcodex fcodex
        """
    )


def render_zsh_completion_script(*, venv_python: pathlib.Path) -> str:
    python_command = shlex.quote(str(pathlib.Path(venv_python)))
    return textwrap.dedent(
        f"""\
        # zsh completion for FOCUS wrappers.

        if ! whence compdef >/dev/null 2>&1; then
          autoload -Uz compinit
          compinit
        fi

        _fc_complete_dispatch() {{
          local command_name="$1"
          local output
          local -a candidates
          output=$({python_command} -m bot.shell_completion complete "$command_name" "$((CURRENT - 1))" "${{words[@]}}" 2>/dev/null) || return 1
          [[ -n "$output" ]] || return 1
          candidates=("${{(@f)output}}")
          (( ${{#candidates[@]}} == 0 )) && return 1
          compadd -a candidates
        }}

        _focus_complete_focus() {{
          _fc_complete_dispatch focus
        }}

        _focus_complete_focusctl() {{
          _fc_complete_dispatch focusctl
        }}

        _focus_complete_focusd() {{
          _fc_complete_dispatch focusd
        }}

        _focus_complete_fcodex() {{
          _fc_complete_dispatch fcodex
        }}

        compdef _focus_complete_focus focus
        compdef _focus_complete_focusctl focusctl
        compdef _focus_complete_focusd focusd
        compdef _focus_complete_fcodex fcodex
        """
    )


def render_powershell_completion_script(*, venv_python: pathlib.Path) -> str:
    python_command = _powershell_quote(str(pathlib.Path(venv_python)))
    command_names = ", ".join(_powershell_quote(name) for name in COMPLETION_COMMAND_NAMES)
    return textwrap.dedent(
        f"""\
        # PowerShell completion for FOCUS wrappers.

        $script:FocusCompletionPython = {python_command}
        $script:FocusCompletionCommands = @({command_names})

        function global:__FocusGetCompletions {{
          param(
            [string] $wordToComplete,
            [System.Management.Automation.Language.CommandAst] $commandAst,
            [int] $cursorPosition
          )

          $words = @($commandAst.CommandElements | ForEach-Object {{ $_.Extent.Text }})
          if ($words.Count -eq 0) {{
            return
          }}

          $commandName = $words[0]
          if ($script:FocusCompletionCommands -notcontains $commandName) {{
            return
          }}

          $linePrefix = $commandAst.Extent.Text.Substring(0, [Math]::Min($cursorPosition, $commandAst.Extent.Text.Length))
          $endsWithWhitespace = $linePrefix.Length -gt 0 -and [char]::IsWhiteSpace($linePrefix[$linePrefix.Length - 1])
          if ($endsWithWhitespace) {{
            $cword = $words.Count
            $words += ""
          }} else {{
            $cword = [Math]::Max($words.Count - 1, 0)
          }}

          $results = & $script:FocusCompletionPython -m bot.shell_completion complete $commandName $cword @words 2>$null
          foreach ($candidate in $results) {{
            [System.Management.Automation.CompletionResult]::new($candidate, $candidate, 'ParameterValue', $candidate)
          }}
        }}

        foreach ($commandName in $script:FocusCompletionCommands) {{
          Register-ArgumentCompleter -Native -CommandName $commandName -ScriptBlock {{
            param($wordToComplete, $commandAst, $cursorPosition)
            __FocusGetCompletions $wordToComplete $commandAst $cursorPosition
          }}
        }}
        """
    )


def install_shell_completion_files(*, venv_python: pathlib.Path) -> CompletionInstallResult:
    bash_dir = _install_bash_completion_files(venv_python=venv_python)
    zsh_script_path, zsh_profile = _install_zsh_completion_files(venv_python=venv_python)
    powershell_script, powershell_profile = _install_powershell_completion_files(venv_python=venv_python)
    return CompletionInstallResult(
        bash_dir=bash_dir,
        zsh_script_path=zsh_script_path,
        zsh_rc_path=zsh_profile,
        powershell_script_path=powershell_script,
        powershell_profile_path=powershell_profile,
    )


def remove_shell_completion_files() -> None:
    _remove_bash_completion_files()
    _remove_zsh_completion_files()
    _remove_powershell_completion_files()


def _install_bash_completion_files(*, venv_python: pathlib.Path) -> pathlib.Path | None:
    directory = bash_completion_dir()
    if directory is None:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    rendered = render_bash_completion_script(venv_python=venv_python)
    for path in bash_completion_file_paths():
        path.write_text(rendered, encoding="utf-8")
    return directory


def _remove_bash_completion_files() -> None:
    for path in bash_completion_file_paths():
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def _install_zsh_completion_files(*, venv_python: pathlib.Path) -> tuple[pathlib.Path | None, pathlib.Path | None]:
    script_path = zsh_completion_path()
    if script_path is None:
        return None, None
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(render_zsh_completion_script(venv_python=venv_python), encoding="utf-8")
    rc_path = zsh_rc_path()
    if rc_path is not None:
        _upsert_managed_block(
            rc_path,
            start_marker=_ZSH_PROFILE_BLOCK_START,
            end_marker=_ZSH_PROFILE_BLOCK_END,
            body=f'[[ -f "{script_path}" ]] && source "{script_path}"',
        )
    return script_path, rc_path


def _remove_zsh_completion_files() -> None:
    rc_path = zsh_rc_path()
    if rc_path is not None:
        _remove_managed_block(
            rc_path,
            start_marker=_ZSH_PROFILE_BLOCK_START,
            end_marker=_ZSH_PROFILE_BLOCK_END,
        )
    for script_path in _dedupe_paths(zsh_completion_path()):
        try:
            script_path.unlink()
        except FileNotFoundError:
            continue


def _install_powershell_completion_files(
    *,
    venv_python: pathlib.Path,
) -> tuple[pathlib.Path | None, pathlib.Path | None]:
    script_path = powershell_completion_path()
    profile_path = powershell_profile_path()
    if script_path is None or profile_path is None:
        return None, None
    recorded_script_path, recorded_profile_path = _read_powershell_completion_metadata()
    if recorded_script_path != script_path or recorded_profile_path != profile_path:
        _remove_powershell_completion_files()
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(render_powershell_completion_script(venv_python=venv_python), encoding="utf-8")
    if _powershell_profile_autoload_disabled():
        _remove_managed_block(
            profile_path,
            start_marker=_POWERSHELL_PROFILE_BLOCK_START,
            end_marker=_POWERSHELL_PROFILE_BLOCK_END,
        )
        _write_powershell_completion_metadata(script_path=script_path, profile_path=None)
        return script_path, None
    _upsert_managed_block(
        profile_path,
        start_marker=_POWERSHELL_PROFILE_BLOCK_START,
        end_marker=_POWERSHELL_PROFILE_BLOCK_END,
        body=f"if (Test-Path '{_powershell_literal(str(script_path))}') {{ . '{_powershell_literal(str(script_path))}' }}",
    )
    _write_powershell_completion_metadata(script_path=script_path, profile_path=profile_path)
    return script_path, profile_path


def _remove_powershell_completion_files() -> None:
    recorded_script_path, recorded_profile_path = _read_powershell_completion_metadata()
    for profile_path in _dedupe_paths(recorded_profile_path, powershell_profile_path()):
        _remove_managed_block(
            profile_path,
            start_marker=_POWERSHELL_PROFILE_BLOCK_START,
            end_marker=_POWERSHELL_PROFILE_BLOCK_END,
        )
    for script_path in _dedupe_paths(recorded_script_path, powershell_completion_path()):
        try:
            script_path.unlink()
        except FileNotFoundError:
            continue
    _remove_powershell_completion_metadata()


def _upsert_managed_block(
    path: pathlib.Path,
    *,
    start_marker: str,
    end_marker: str,
    body: str,
) -> None:
    path = pathlib.Path(path)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    cleaned = _strip_managed_block(existing, start_marker=start_marker, end_marker=end_marker).rstrip()
    block = f"{start_marker}\n{body.rstrip()}\n{end_marker}\n"
    rendered = f"{cleaned}\n\n{block}" if cleaned else block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def _remove_managed_block(path: pathlib.Path, *, start_marker: str, end_marker: str) -> None:
    path = pathlib.Path(path)
    if not path.exists():
        return
    existing = path.read_text(encoding="utf-8")
    rendered = _strip_managed_block(existing, start_marker=start_marker, end_marker=end_marker).strip()
    if not rendered:
        path.unlink()
        return
    path.write_text(f"{rendered}\n", encoding="utf-8")


def _strip_managed_block(text: str, *, start_marker: str, end_marker: str) -> str:
    rendered = str(text or "")
    while True:
        start = rendered.find(start_marker)
        if start < 0:
            return rendered
        end = rendered.find(end_marker, start)
        if end < 0:
            return rendered
        end += len(end_marker)
        if end < len(rendered) and rendered[end] == "\n":
            end += 1
        rendered = rendered[:start] + rendered[end:]


def _powershell_literal(value: str) -> str:
    return str(value or "").replace("'", "''")


def _powershell_quote(value: str) -> str:
    return f"'{_powershell_literal(value)}'"


def complete_words(command_name: str, words: list[str], cword: int) -> list[str]:
    normalized_command = str(command_name or "").strip()
    if normalized_command not in COMPLETION_COMMAND_NAMES:
        return []
    context = CompletionContext(words=tuple(words), cword=max(cword, 0))
    if normalized_command == "focusctl":
        return _complete_focusctl(context)
    if normalized_command == "focusd":
        return _complete_focusd(context)
    return _complete_fcodex(context)


def _complete_focusctl(context: CompletionContext) -> list[str]:
    for option_name, choices in (
        ("--instance", list_known_instance_names()),
        ("--display-mode", ["silent", "announce"]),
        ("--scope", ["cwd", "global"]),
        ("--status", ["active", "paused"]),
    ):
        matches = _complete_choice_option(context, option_name, choices)
        if matches is not None:
            return matches

    current = context.current
    args_before = context.args_before_cursor
    positionals = _positionals_before_cursor(args_before, _FOCUSCTL_OPTIONS_WITH_VALUE)
    positional_index = len(positionals)

    if positional_index == 0:
        return _complete_candidates(
            current,
            [
                "--instance",
                "--version",
                "--help",
                "-h",
                "instance",
                "service",
                "config",
                "binding",
                "prompt",
                "thread",
                "image",
                "skill",
                "migrate",
                "uninstall",
                "purge",
            ],
        )

    resource = positionals[0]
    if resource == "config":
        if current.startswith("-"):
            return _complete_candidates(current, ["--open", "--help", "-h"])
        if positional_index == 1:
            return _complete_candidates(current, ["system", "codex", "env", "init-token"])
        return []
    if resource == "instance":
        if positional_index == 1:
            return _complete_candidates(current, ["create", "list", "remove"])
        if len(positionals) >= 2 and positionals[1] == "remove" and positional_index == 2:
            named_instances = [name for name in list_known_instance_names() if name != DEFAULT_INSTANCE_NAME]
            return _complete_candidates(current, named_instances)
        return []
    if resource == "service":
        if positional_index == 1:
            return _complete_candidates(
                current,
                ["start", "stop", "restart", "status", "autostart", "log", "reset-backend", "attach"],
            )
        if len(positionals) >= 2 and positionals[1] == "reset-backend" and current.startswith("-"):
            return _complete_candidates(current, ["--force", "--help", "-h"])
        if len(positionals) >= 2 and positionals[1] == "autostart" and positional_index == 2:
            return _complete_candidates(current, ["enable", "disable", "status"])
        if len(positionals) >= 2 and positionals[1] == "log" and current.startswith("-"):
            return _complete_candidates(current, ["--lines", "--help", "-h"])
        return []
    if resource == "skill" and positional_index == 1:
        return _complete_candidates(current, ["install", "uninstall"])
    if resource == "migrate":
        if positional_index == 1:
            return _complete_candidates(current, ["from-feishu-codex"])
        return []
    if resource == "binding":
        if positional_index == 1:
            return _complete_candidates(
                current,
                ["list", "status", "clear", "attach", "detach", "clear-all", "clear-stale"],
            )
        if len(positionals) >= 2 and positionals[1] == "list" and current.startswith("-"):
            return _complete_candidates(current, ["--refresh-names", "--help", "-h"])
        if len(positionals) >= 2 and positionals[1] == "clear-stale" and current.startswith("-"):
            return _complete_candidates(current, ["--dry-run", "--help", "-h"])
        return []
    if resource == "prompt":
        if positional_index == 1:
            return _complete_candidates(current, ["send"])
        if len(positionals) >= 2 and positionals[1] == "send" and current.startswith("-"):
            return _complete_candidates(
                current,
                [
                    "--binding-id",
                    "--text",
                    "--text-file",
                    "--synthetic-source",
                    "--display-mode",
                    "--actor-open-id",
                    "--help",
                    "-h",
                ],
            )
        return []
    if resource == "thread":
        if positional_index == 1:
            return _complete_candidates(
                current,
                ["list", "status", "bindings", "goal", "archive", "clear-archived-bindings", "attach", "detach"],
            )
        if len(positionals) < 2:
            return []
        action = positionals[1]
        goal_subaction = positionals[2] if len(positionals) >= 3 else ""
        if current.startswith("-"):
            if action == "list":
                return _complete_candidates(current, ["--scope", "--cwd", "--help", "-h"])
            if action == "goal":
                if goal_subaction == "set":
                    return _complete_candidates(
                        current,
                        [
                            "--thread-id",
                            "--thread-name",
                            "--objective",
                            "--status",
                            "--help",
                            "-h",
                        ],
                    )
                if goal_subaction in {"show", "clear"}:
                    return _complete_candidates(current, ["--thread-id", "--thread-name", "--help", "-h"])
                return _complete_candidates(current, ["--thread-id", "--thread-name", "--help", "-h"])
            if action in {"status", "bindings", "archive", "attach", "detach"}:
                return _complete_candidates(current, ["--thread-id", "--thread-name", "--help", "-h"])
            if action == "clear-archived-bindings":
                return _complete_candidates(current, ["--thread-id", "--all", "--dry-run", "--help", "-h"])
        if action == "goal" and positional_index == 2:
            return _complete_candidates(current, ["show", "set", "clear"])
        return []
    if resource == "image":
        if positional_index == 1:
            return _complete_candidates(current, ["send"])
        if len(positionals) >= 2 and positionals[1] == "send" and current.startswith("-"):
            return _complete_candidates(current, ["--path", "--thread-id", "--thread-name", "--help", "-h"])
        return []
    return []


def _complete_focusd(context: CompletionContext) -> list[str]:
    matches = _complete_choice_option(context, "--instance", list_known_instance_names())
    if matches is not None:
        return matches
    return _complete_candidates(context.current, ["--instance", "--version", "--help", "-h"])


def _complete_fcodex(context: CompletionContext) -> list[str]:
    matches = _complete_choice_option(context, "--instance", list_known_instance_names())
    if matches is not None:
        return matches

    current = context.current
    args_before = context.args_before_cursor
    positionals = _positionals_before_cursor(args_before, _FOCUS_TUI_OPTIONS_WITH_VALUE)
    positional_index = len(positionals)
    if positional_index == 0:
        if not args_before:
            return _complete_candidates(current, ["--instance", "--version", "resume"])
        return _complete_candidates(current, ["resume"])
    return []


def _complete_choice_option(
    context: CompletionContext,
    option_name: str,
    choices: list[str],
) -> list[str] | None:
    current = context.current
    previous = context.previous
    if previous == option_name:
        return _complete_candidates(current, choices)
    inline_prefix = f"{option_name}="
    if current.startswith(inline_prefix):
        value_prefix = current[len(inline_prefix) :]
        return [f"{inline_prefix}{match}" for match in _complete_candidates(value_prefix, choices)]
    return None


def _positionals_before_cursor(args_before_cursor: tuple[str, ...], options_with_value: set[str]) -> list[str]:
    positionals: list[str] = []
    index = 0
    while index < len(args_before_cursor):
        token = args_before_cursor[index]
        if token == "--":
            positionals.extend(args_before_cursor[index + 1 :])
            break
        if token.startswith("-") and token != "-":
            option_name = token.split("=", 1)[0]
            if option_name in options_with_value and "=" not in token:
                index += 2
                continue
            index += 1
            continue
        positionals.append(token)
        index += 1
    return positionals


def _complete_candidates(prefix: str, candidates: list[str]) -> list[str]:
    normalized_prefix = str(prefix or "")
    seen: set[str] = set()
    matches: list[str] = []
    for candidate in candidates:
        normalized_candidate = str(candidate or "")
        if not normalized_candidate.startswith(normalized_prefix):
            continue
        if normalized_candidate in seen:
            continue
        seen.add(normalized_candidate)
        matches.append(normalized_candidate)
    return matches


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4 or args[0] != "complete":
        print("usage: python -m bot.shell_completion complete <command> <cword> <comp_words...>", file=sys.stderr)
        return 2
    command_name = str(args[1] or "").strip()
    try:
        cword = int(args[2])
    except ValueError:
        return 0
    words = list(args[3:])
    for candidate in complete_words(command_name, words, cword):
        print(candidate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
