from __future__ import annotations

import os
import pathlib
import shlex
import shutil

DEFAULT_CODEX_COMMAND = "codex"
_MACH_O_MAGICS = {
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
}


def _is_windows() -> bool:
    return os.name == "nt"


def _resolve_existing_path(raw: str | None) -> pathlib.Path | None:
    if not raw:
        return None
    path = pathlib.Path(raw).expanduser()
    if not path.exists():
        return None
    return path.resolve()


def _current_command_path(command: str) -> pathlib.Path | None:
    resolved = shutil.which(command)
    if not resolved:
        return None
    return pathlib.Path(resolved).expanduser()


def _is_native_executable(path: pathlib.Path) -> bool:
    candidate = pathlib.Path(path)
    if not candidate.is_file():
        return False
    if not _is_windows() and not os.access(candidate, os.X_OK):
        return False
    try:
        with candidate.open("rb") as handle:
            header = handle.read(4)
            if header == b"\x7fELF" or header in _MACH_O_MAGICS:
                return True
            if not header.startswith(b"MZ"):
                return False
            handle.seek(0x3C)
            pe_offset_raw = handle.read(4)
            if len(pe_offset_raw) != 4:
                return False
            pe_offset = int.from_bytes(pe_offset_raw, byteorder="little")
            handle.seek(pe_offset)
            return handle.read(4) == b"PE\0\0"
    except OSError:
        return False


def _native_executable_command(path: pathlib.Path | str | None) -> str | None:
    if path is None:
        return None
    candidate = pathlib.Path(path).expanduser().absolute()
    if not _is_native_executable(candidate):
        return None
    rendered = _render_windows_command_path(candidate) if _is_windows() else str(candidate)
    return shlex.join([rendered])


def _render_windows_command_path(path: pathlib.Path | str) -> str:
    return str(pathlib.Path(path)).replace("\\", "/")


def _is_path_within(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _home_dir() -> pathlib.Path:
    return pathlib.Path.home()


def _nvm_installation_root_for_path(path: pathlib.Path) -> pathlib.Path | None:
    current = path.resolve()
    for ancestor in (current, *current.parents):
        parent = ancestor.parent
        grandparent = parent.parent
        if parent.name == "node" and grandparent.name == "versions":
            return ancestor
    return None


def _node_launcher_command(
    installation_root: pathlib.Path,
    *,
    fallback_codex: pathlib.Path | None = None,
) -> str | None:
    node_candidates = [
        installation_root / "bin" / "node",
        installation_root / "bin" / "node.exe",
    ]
    node = next((candidate for candidate in node_candidates if candidate.exists()), None)
    if node is None:
        return None
    codex_js = installation_root / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if codex_js.exists():
        return shlex.join([str(node), str(codex_js)])
    if fallback_codex is not None and fallback_codex.exists():
        resolved_fallback = fallback_codex.resolve()
        if resolved_fallback.exists():
            return shlex.join([str(node), str(resolved_fallback)])
        return shlex.join([str(node), str(fallback_codex)])
    return None


def _fnm_default_launcher_command(
    *,
    fallback_node: pathlib.Path | None = None,
    fallback_codex: pathlib.Path | None = None,
) -> str | None:
    for fnm_root in _candidate_fnm_roots():
        default_installation_root = fnm_root / "aliases" / "default"
        if not default_installation_root.exists():
            continue

        default_node_candidates = [
            default_installation_root / "bin" / "node",
            default_installation_root / "bin" / "node.exe",
        ]
        default_codex_candidates = [
            default_installation_root / "bin" / "codex",
            default_installation_root / "bin" / "codex.cmd",
            default_installation_root / "bin" / "codex.exe",
        ]
        default_codex_js = default_installation_root / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"

        stable_node = next((candidate for candidate in default_node_candidates if candidate.exists()), fallback_node)
        stable_codex = next(
            (candidate for candidate in default_codex_candidates if candidate.exists()),
            default_codex_js if default_codex_js.exists() else fallback_codex,
        )
        if stable_node is None or stable_codex is None:
            continue
        if not stable_node.exists() or not stable_codex.exists():
            continue
        return shlex.join([str(stable_node), str(stable_codex)])
    return None


def _candidate_fnm_roots() -> list[pathlib.Path]:
    roots: list[pathlib.Path] = []
    for raw in (
        os.environ.get("FNM_DIR"),
        str(_home_dir() / ".local" / "share" / "fnm"),
        str(_home_dir() / ".fnm"),
    ):
        path = _resolve_existing_path(raw)
        if path is not None and path not in roots:
            roots.append(path)
    fnm_executable = _resolve_existing_path(shutil.which("fnm"))
    if fnm_executable is not None and fnm_executable.parent not in roots:
        roots.insert(0, fnm_executable.parent)
    return roots


def _candidate_nvm_roots() -> list[pathlib.Path]:
    roots: list[pathlib.Path] = []
    for raw in (
        os.environ.get("NVM_DIR"),
        str(_home_dir() / ".nvm"),
    ):
        path = _resolve_existing_path(raw)
        if path is not None and path not in roots:
            roots.append(path)
    return roots


def _detect_windows_node_executable() -> pathlib.Path | None:
    current_node = _current_command_path("node")
    resolved_node = _resolve_existing_path(str(current_node)) if current_node is not None else None
    if resolved_node is not None:
        return resolved_node
    home = _home_dir()
    candidates = [
        pathlib.Path(os.environ.get("ProgramFiles") or "") / "nodejs" / "node.exe",
        pathlib.Path(os.environ.get("ProgramFiles(x86)") or "") / "nodejs" / "node.exe",
        home / "AppData" / "Local" / "Programs" / "nodejs" / "node.exe",
    ]
    for candidate in candidates:
        resolved = _resolve_existing_path(str(candidate))
        if resolved is not None:
            return resolved
    return None


def _windows_npm_codex_command_for_wrapper(wrapper_path: pathlib.Path) -> str | None:
    resolved_wrapper = _resolve_existing_path(str(wrapper_path))
    if resolved_wrapper is None:
        return None
    npm_dir = resolved_wrapper.parent
    codex_js = npm_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    node = _detect_windows_node_executable()
    if node is not None and codex_js.exists():
        return shlex.join([
            _render_windows_command_path(node),
            _render_windows_command_path(codex_js),
        ])
    return _render_windows_command_path(resolved_wrapper)


def _windows_npm_stable_codex_command_for_wrapper(wrapper_path: pathlib.Path) -> str | None:
    resolved_wrapper = _resolve_existing_path(str(wrapper_path))
    if resolved_wrapper is None:
        return None
    npm_dir = resolved_wrapper.parent
    codex_js = npm_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    node = _detect_windows_node_executable()
    if node is None or not codex_js.exists():
        return None
    return shlex.join([
        _render_windows_command_path(node),
        _render_windows_command_path(codex_js),
    ])


def _detect_windows_npm_global_codex_command() -> str | None:
    appdata = os.environ.get("APPDATA", "").strip()
    home = _home_dir()
    npm_dirs = []
    if appdata:
        npm_dirs.append(pathlib.Path(appdata).expanduser() / "npm")
    npm_dirs.append(home / "AppData" / "Roaming" / "npm")
    seen: set[str] = set()
    for npm_dir in npm_dirs:
        key = str(npm_dir)
        if key in seen:
            continue
        seen.add(key)
        for candidate in (npm_dir / "codex.cmd", npm_dir / "codex"):
            command = _windows_npm_codex_command_for_wrapper(candidate)
            if command is not None:
                return command
    return None


def _detect_windows_npm_global_stable_codex_command() -> str | None:
    appdata = os.environ.get("APPDATA", "").strip()
    home = _home_dir()
    npm_dirs = []
    if appdata:
        npm_dirs.append(pathlib.Path(appdata).expanduser() / "npm")
    npm_dirs.append(home / "AppData" / "Roaming" / "npm")
    seen: set[str] = set()
    for npm_dir in npm_dirs:
        key = str(npm_dir)
        if key in seen:
            continue
        seen.add(key)
        for candidate in (npm_dir / "codex.cmd", npm_dir / "codex"):
            command = _windows_npm_stable_codex_command_for_wrapper(candidate)
            if command is not None:
                return command
    return None


def _detect_fnm_stable_codex_command() -> str | None:
    current_codex = _current_command_path("codex")
    current_node = _current_command_path("node")
    resolved_node = _resolve_existing_path(str(current_node)) if current_node is not None else None
    resolved_codex = _resolve_existing_path(str(current_codex)) if current_codex is not None else None
    return _fnm_default_launcher_command(fallback_node=resolved_node, fallback_codex=resolved_codex)


def _resolve_nvm_alias(root: pathlib.Path, name: str, *, seen: set[str] | None = None) -> str | None:
    normalized = str(name or "").strip()
    if not normalized:
        return None
    if seen is None:
        seen = set()
    if normalized in seen:
        return None
    seen.add(normalized)
    version_dir = root / "versions" / "node" / normalized
    if version_dir.exists():
        return normalized
    alias_path = root / "alias" / normalized
    if alias_path.exists():
        target = alias_path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        return _resolve_nvm_alias(root, target, seen=seen)
    return None


def _detect_nvm_stable_codex_command() -> str | None:
    current_codex = _current_command_path("codex")
    resolved_codex = _resolve_existing_path(str(current_codex)) if current_codex is not None else None
    for nvm_root in _candidate_nvm_roots():
        version_candidates: list[pathlib.Path] = []
        resolved_alias = _resolve_nvm_alias(nvm_root, "default")
        if resolved_alias:
            version_candidates.append(nvm_root / "versions" / "node" / resolved_alias)
        versions_root = nvm_root / "versions" / "node"
        if versions_root.exists():
            for child in sorted(versions_root.iterdir()):
                if child.is_dir() and child not in version_candidates:
                    version_candidates.append(child)
        if (
            resolved_codex is not None
            and _is_path_within(resolved_codex, nvm_root)
            and resolved_codex.name.startswith("codex")
        ):
            installation_root = _nvm_installation_root_for_path(resolved_codex)
            if installation_root is not None:
                command = _node_launcher_command(installation_root, fallback_codex=current_codex)
                if command is not None:
                    return command
        for candidate in version_candidates:
            wrapper = candidate / "bin" / "codex"
            if not wrapper.exists():
                continue
            command = _node_launcher_command(candidate, fallback_codex=wrapper)
            if command is not None:
                return command
    return None


def _normalize_explicit_managed_command(configured_command: str) -> str | None:
    parts = shlex.split(configured_command)
    if len(parts) != 1:
        return None
    explicit_path = _resolve_existing_path(parts[0])
    if explicit_path is None or explicit_path.name.startswith("node"):
        return None
    if "fnm_multishells" in explicit_path.parts:
        command = _fnm_default_launcher_command(fallback_codex=pathlib.Path(parts[0]).expanduser())
        if command is not None:
            return command
    installation_root = _nvm_installation_root_for_path(explicit_path)
    if installation_root is None:
        return None
    return _node_launcher_command(installation_root, fallback_codex=pathlib.Path(parts[0]).expanduser())


def detect_stable_codex_command() -> str | None:
    if _is_windows():
        current_codex = _current_command_path(DEFAULT_CODEX_COMMAND)
        if current_codex is not None:
            rendered_current = _render_windows_command_path(current_codex)
            return (
                _normalize_explicit_managed_command(rendered_current)
                or _windows_npm_stable_codex_command_for_wrapper(current_codex)
                or _native_executable_command(current_codex)
            )
        return (
            _detect_windows_npm_global_stable_codex_command()
            or _detect_fnm_stable_codex_command()
            or _detect_nvm_stable_codex_command()
        )
    current_codex = _current_command_path(DEFAULT_CODEX_COMMAND)
    return (
        _native_executable_command(current_codex)
        or _detect_fnm_stable_codex_command()
        or _detect_nvm_stable_codex_command()
    )


def resolve_managed_codex_command(configured_command: str) -> str:
    normalized = str(configured_command or "").strip() or DEFAULT_CODEX_COMMAND
    if normalized != DEFAULT_CODEX_COMMAND:
        return _normalize_explicit_managed_command(normalized) or normalized
    if _is_windows():
        current_codex = _current_command_path(DEFAULT_CODEX_COMMAND)
        if current_codex is not None:
            rendered_current = _render_windows_command_path(current_codex)
            return (
                _normalize_explicit_managed_command(rendered_current)
                or _windows_npm_codex_command_for_wrapper(current_codex)
                or rendered_current
            )
        return detect_stable_codex_command() or DEFAULT_CODEX_COMMAND
    return detect_stable_codex_command() or DEFAULT_CODEX_COMMAND
