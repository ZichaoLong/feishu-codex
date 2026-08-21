"""Install and remove shell completion for FOCUS command wrappers."""

from __future__ import annotations

import json
import os
import pathlib
import shlex
import textwrap
from dataclasses import dataclass

from bot.platform_paths import (
    default_config_root,
    default_user_bash_completion_dir,
    default_user_powershell_completion_path,
    default_user_powershell_profile_path,
    default_user_zsh_completion_path,
    default_user_zsh_rc_path,
)
from bot.public_command_contract import PUBLIC_COMMAND_NAMES

_ZSH_PROFILE_BLOCK_START = "# >>> focus zsh completion >>>"
_ZSH_PROFILE_BLOCK_END = "# <<< focus zsh completion <<<"
_POWERSHELL_PROFILE_BLOCK_START = "# >>> focus PowerShell completion >>>"
_POWERSHELL_PROFILE_BLOCK_END = "# <<< focus PowerShell completion <<<"
_POWERSHELL_COMPLETION_METADATA_FILE = "powershell-install-paths.json"


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
    return [directory / command_name for command_name in PUBLIC_COMMAND_NAMES]


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
    command_names = ", ".join(_powershell_quote(name) for name in PUBLIC_COMMAND_NAMES)
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
