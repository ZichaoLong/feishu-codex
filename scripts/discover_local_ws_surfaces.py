#!/usr/bin/env python3
"""Discover local app-server and fcodex proxy websocket surfaces for one user."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_APP_SERVER_KIND = "app-server"
_PROXY_KIND = "proxy"
_SECRET_OPTION_NAMES = {"--service-token"}


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    user: str
    raw_args: str
    argv: tuple[str, ...]


def _run_command(args: list[str]) -> str:
    completed = subprocess.run(
        args,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout


def _load_processes(user_name: str | None = None) -> dict[int, ProcessInfo]:
    output = _run_command(["ps", "-eo", "pid=,ppid=,user=,args="])
    processes: dict[int, ProcessInfo] = {}
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_text, ppid_text, user, args_text = parts
        if user_name is not None and user != user_name:
            continue
        try:
            pid = int(pid_text)
            ppid = int(ppid_text)
        except ValueError:
            continue
        try:
            argv = tuple(shlex.split(args_text))
        except ValueError:
            argv = (args_text,)
        processes[pid] = ProcessInfo(
            pid=pid,
            ppid=ppid,
            user=user,
            raw_args=args_text,
            argv=argv,
        )
    return processes


def _listener_ports_by_pid() -> dict[int, list[dict[str, Any]]]:
    try:
        output = _run_command(["ss", "-ltnpH"])
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {}
    listeners: dict[int, list[dict[str, Any]]] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        if ":" not in local:
            continue
        host_text, port_text = local.rsplit(":", 1)
        host = host_text.strip("[]")
        try:
            port = int(port_text)
        except ValueError:
            continue
        pids = {int(item) for item in re.findall(r"pid=(\d+)", line)}
        for pid in pids:
            listeners.setdefault(pid, []).append(
                {
                    "host": host,
                    "port": port,
                    "local": local,
                }
            )
    return listeners


def _extract_option(argv: tuple[str, ...], *names: str) -> str:
    for index, arg in enumerate(argv):
        for name in names:
            if arg == name:
                if index + 1 < len(argv):
                    return argv[index + 1]
                return ""
            prefix = f"{name}="
            if arg.startswith(prefix):
                return arg[len(prefix) :]
    return ""


def _has_option(argv: tuple[str, ...], *names: str) -> bool:
    return bool(_extract_option(argv, *names))


def _sanitize_argv(argv: tuple[str, ...]) -> str:
    sanitized: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        option_name = arg.split("=", 1)[0]
        if option_name in _SECRET_OPTION_NAMES:
            if "=" in arg:
                sanitized.append(f"{option_name}=<redacted>")
                index += 1
                continue
            sanitized.append(option_name)
            if index + 1 < len(argv):
                sanitized.append("<redacted>")
                index += 2
                continue
        sanitized.append(arg)
        index += 1
    return shlex.join(sanitized)


def _looks_like_app_server(proc: ProcessInfo) -> bool:
    return "app-server" in proc.argv and _has_option(proc.argv, "--listen")


def _looks_like_proxy(proc: ProcessInfo) -> bool:
    return "bot.fcodex.proxy" in proc.raw_args


def _instance_from_token_path(path_text: str) -> str:
    normalized = str(path_text or "").strip()
    if not normalized:
        return ""
    marker = "/instances/"
    if marker not in normalized:
        if normalized.endswith("/app_server_websocket.token"):
            return "default"
        return ""
    suffix = normalized.split(marker, 1)[1]
    instance_name, _sep, _rest = suffix.partition("/")
    return instance_name.strip()


def _find_instance_name(proc: ProcessInfo, process_map: dict[int, ProcessInfo]) -> str:
    direct = _extract_option(proc.argv, "--instance")
    if direct:
        return direct
    token_path = _extract_option(proc.argv, "--ws-token-file")
    token_path_instance = _instance_from_token_path(token_path)
    if token_path_instance:
        return token_path_instance
    current_pid = proc.pid
    seen: set[int] = set()
    while current_pid and current_pid not in seen:
        seen.add(current_pid)
        current = process_map.get(current_pid)
        if current is None:
            return ""
        instance_name = _extract_option(current.argv, "--instance")
        if instance_name:
            return instance_name
        current_pid = current.ppid
    return ""


def _build_ws_url(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        return f"ws://[{host}]:{port}"
    return f"ws://{host}:{port}"


def _parse_port_from_ws_url(url: str) -> int:
    normalized = str(url or "").strip()
    if not normalized or ":" not in normalized:
        return 0
    try:
        return int(normalized.rsplit(":", 1)[1])
    except ValueError:
        return 0


def _probe_surface_without_auth(url: str, *, open_timeout: float) -> dict[str, Any]:
    try:
        from websockets.exceptions import InvalidStatus
        from websockets.sync.client import connect
    except Exception as exc:  # pragma: no cover - dependency failure path
        return {
            "status": "probe_unavailable",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }

    def _rpc_call(ws: Any, *, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
                ensure_ascii=False,
            )
        )
        while True:
            raw = ws.recv()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            payload = json.loads(raw)
            if payload.get("id") == request_id:
                return payload

    try:
        with connect(url, open_timeout=open_timeout, max_size=None, proxy=None) as ws:
            initialize = _rpc_call(
                ws,
                request_id=1,
                method="initialize",
                params={
                    "clientInfo": {
                        "name": "focus-local-ws-discovery",
                        "version": "1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            if "error" in initialize:
                return {
                    "status": "rpc_error",
                    "step": "initialize",
                    "error": initialize["error"],
                }
            models = _rpc_call(
                ws,
                request_id=2,
                method="model/list",
                params={},
            )
            if "error" in models:
                return {
                    "status": "rpc_error",
                    "step": "model/list",
                    "error": models["error"],
                }
            data = models.get("result", {}).get("data") or []
            sample_models = [
                str(item.get("model"))
                for item in data[:3]
                if isinstance(item, dict) and item.get("model")
            ]
            return {
                "status": "ok",
                "model_count": len(data),
                "sample_models": sample_models,
                "methods": ["initialize", "model/list"],
            }
    except InvalidStatus as exc:
        response = exc.args[0] if exc.args else None
        status_code = getattr(response, "status_code", None)
        result: dict[str, Any] = {
            "status": "unauthorized" if status_code == 401 else "connect_failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        if status_code is not None:
            result["http_status"] = status_code
        return result
    except Exception as exc:
        return {
            "status": "connect_failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _probe_risk_text(status: str) -> str:
    if status == "ok":
        return "unauthenticated local access appears allowed"
    if status == "unauthorized":
        return "unauthenticated local access was rejected"
    if status == "rpc_error":
        return "websocket accepted the connection, but JSON-RPC returned an error"
    if status == "probe_unavailable":
        return "probe dependency unavailable"
    return "surface was discovered, but unauthenticated probe did not complete"


def _find_related_remote_url(proc: ProcessInfo, process_map: dict[int, ProcessInfo]) -> str:
    parent = process_map.get(proc.ppid)
    if parent is not None:
        remote = _extract_option(parent.argv, "--remote")
        if remote:
            return remote
    for candidate in process_map.values():
        if candidate.ppid != proc.ppid:
            continue
        remote = _extract_option(candidate.argv, "--remote")
        if remote:
            return remote
    return ""


def _discover_surfaces(
    *,
    process_map: dict[int, ProcessInfo],
    listeners_by_pid: dict[int, list[dict[str, Any]]],
    probe: bool,
    open_timeout: float,
) -> list[dict[str, Any]]:
    app_server_groups: dict[str, list[ProcessInfo]] = {}
    for proc in process_map.values():
        if not _looks_like_app_server(proc):
            continue
        listen_url = _extract_option(proc.argv, "--listen")
        if not listen_url:
            continue
        app_server_groups.setdefault(listen_url, []).append(proc)

    surfaces: list[dict[str, Any]] = []
    for listen_url, processes in sorted(app_server_groups.items()):
        canonical = min(processes, key=lambda item: item.pid)
        ws_auth_mode = _extract_option(canonical.argv, "--ws-auth")
        token_file = _extract_option(canonical.argv, "--ws-token-file")
        surface: dict[str, Any] = {
            "kind": _APP_SERVER_KIND,
            "user": canonical.user,
            "instance": _find_instance_name(canonical, process_map),
            "listen_url": listen_url,
            "port": _parse_port_from_ws_url(listen_url),
            "pids": sorted(proc.pid for proc in processes),
            "declared_auth": ws_auth_mode or "none",
            "ws_token_file": token_file or "",
            "redacted_commands": [_sanitize_argv(proc.argv) for proc in sorted(processes, key=lambda item: item.pid)],
            "notes": [],
        }
        if not ws_auth_mode:
            surface["notes"].append("no `--ws-auth` flag was found in argv")
        if probe:
            surface["unauth_probe"] = _probe_surface_without_auth(listen_url, open_timeout=open_timeout)
            surface["risk"] = _probe_risk_text(surface["unauth_probe"]["status"])
        surfaces.append(surface)

    for proc in sorted(process_map.values(), key=lambda item: item.pid):
        if not _looks_like_proxy(proc):
            continue
        listen_entries = listeners_by_pid.get(proc.pid, [])
        listen_url = ""
        port = 0
        if listen_entries:
            chosen = min(listen_entries, key=lambda item: item["port"])
            listen_url = _build_ws_url(chosen["host"], chosen["port"])
            port = chosen["port"]
        if not listen_url:
            remote_url = _find_related_remote_url(proc, process_map)
            if remote_url:
                listen_url = remote_url
                port = _parse_port_from_ws_url(remote_url)
        surface = {
            "kind": _PROXY_KIND,
            "user": proc.user,
            "instance": _find_instance_name(proc, process_map),
            "listen_url": listen_url,
            "port": port,
            "pids": [proc.pid],
            "backend_url": _extract_option(proc.argv, "--backend-url"),
            "data_dir": _extract_option(proc.argv, "--data-dir"),
            "declared_auth": "not visible on argv; verify by probe",
            "argv_secret_exposure": "--service-token" in proc.raw_args,
            "redacted_commands": [_sanitize_argv(proc.argv)],
            "notes": [],
        }
        if surface["argv_secret_exposure"]:
            surface["notes"].append("legacy `--service-token` argv exposure detected")
        if probe and listen_url:
            surface["unauth_probe"] = _probe_surface_without_auth(listen_url, open_timeout=open_timeout)
            surface["risk"] = _probe_risk_text(surface["unauth_probe"]["status"])
        elif probe:
            surface["unauth_probe"] = {
                "status": "not_probed",
                "error": "listen port could not be resolved for this proxy",
            }
            surface["risk"] = _probe_risk_text("connect_failed")
        surfaces.append(surface)

    return surfaces


def _probe_command_text(surface: dict[str, Any], verify_script: Path) -> list[str]:
    listen_url = str(surface.get("listen_url", "") or "").strip()
    if not listen_url:
        return []
    commands = [
        f"{shlex.quote(str(verify_script))} --surface {surface['kind']} --url {shlex.quote(listen_url)}"
    ]
    declared_auth = str(surface.get("declared_auth", "") or "").strip()
    token_file = str(surface.get("ws_token_file", "") or "").strip()
    if surface["kind"] == _APP_SERVER_KIND and declared_auth and declared_auth != "none" and token_file:
        commands.append(
            f"{shlex.quote(str(verify_script))} --surface app-server --url {shlex.quote(listen_url)} "
            f"--token-file {shlex.quote(token_file)}"
        )
    return commands


def _render_text_report(
    *,
    user_name: str,
    user_uid: int,
    scan_scope: str,
    probe: bool,
    surfaces: list[dict[str, Any]],
) -> str:
    lines = [
        f"Current user: {user_name} (uid={user_uid})",
        f"Scan scope: {scan_scope}",
        f"Probe mode: {'enabled' if probe else 'disabled'}",
        "Probe methods: initialize, model/list" if probe else "Probe methods: skipped",
    ]
    verify_script = Path(__file__).with_name("verify_local_ws_surface.py")
    if not surfaces:
        lines.append("No local app-server or fcodex proxy websocket surfaces were discovered for this scan scope.")
        return "\n".join(lines)

    for surface in surfaces:
        lines.append("")
        title = f"{surface['kind']}:{surface.get('user') or 'unknown-user'}:{surface.get('instance') or 'unknown'}"
        lines.append(title)
        lines.append(f"  user: {surface.get('user') or '(unknown)'}")
        lines.append(f"  listen: {surface.get('listen_url') or '(unresolved)'}")
        lines.append(f"  port: {surface.get('port') or '(unresolved)'}")
        lines.append(f"  pids: {', '.join(str(pid) for pid in surface.get('pids', []))}")
        lines.append(f"  declared_auth: {surface.get('declared_auth') or '(unknown)'}")
        if surface["kind"] == _APP_SERVER_KIND and surface.get("ws_token_file"):
            lines.append(f"  ws_token_file: {surface['ws_token_file']}")
        if surface["kind"] == _PROXY_KIND and surface.get("backend_url"):
            lines.append(f"  backend_url: {surface['backend_url']}")
        if surface.get("argv_secret_exposure"):
            lines.append("  argv_secret_exposure: yes")
        if probe:
            unauth_probe = surface.get("unauth_probe") or {}
            lines.append(f"  unauth_probe: {unauth_probe.get('status', '(missing)')}")
            if unauth_probe.get("http_status"):
                lines.append(f"  http_status: {unauth_probe['http_status']}")
            lines.append(f"  risk: {surface.get('risk', '(missing)')}")
        notes = surface.get("notes") or []
        for note in notes:
            lines.append(f"  note: {note}")
        for command in _probe_command_text(surface, verify_script):
            lines.append(f"  verify: {command}")
    return "\n".join(lines)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Discover local Codex websocket surfaces for the current user and "
            "optionally run a minimal unauthenticated read-only probe."
        )
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument(
        "--all-users",
        action="store_true",
        help="Scan matching websocket surfaces across all visible local users, not just the current user",
    )
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="Skip the unauthenticated read-only probe and only show discovered surfaces",
    )
    parser.add_argument(
        "--open-timeout",
        type=float,
        default=3.0,
        help="Websocket open timeout in seconds for probe mode",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    current_uid = os.getuid()
    user_name = pwd.getpwuid(current_uid).pw_name
    scan_scope = "all-users" if args.all_users else "current-user"
    process_map = _load_processes(None if args.all_users else user_name)
    listeners_by_pid = _listener_ports_by_pid()
    probe = not args.no_probe
    surfaces = _discover_surfaces(
        process_map=process_map,
        listeners_by_pid=listeners_by_pid,
        probe=probe,
        open_timeout=args.open_timeout,
    )
    if args.json:
        payload = {
            "current_user": user_name,
            "current_uid": current_uid,
            "scan_scope": scan_scope,
            "probe_enabled": probe,
            "probe_methods": ["initialize", "model/list"] if probe else [],
            "surfaces": surfaces,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            _render_text_report(
                user_name=user_name,
                user_uid=current_uid,
                scan_scope=scan_scope,
                probe=probe,
                surfaces=surfaces,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
