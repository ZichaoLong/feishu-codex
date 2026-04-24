---
title: Cross-Platform Audit (Linux / macOS / Windows)
date: 2026-04-22
status: Partially fixed
---

# Cross-Platform Audit

This document records the current Linux / macOS / Windows adaptation status of
`feishu-codex`. It serves as a draft punch-list for follow-up fixes.

## 1. Overall Conclusion

The main install flow, path layout, service management, file locking, and
wrapper scripts have already been split across the three major platforms, and
the README now explicitly states Linux / macOS / Windows support.

After this review, however, the earlier conclusion needs to be tightened:

- the earlier highest-priority Windows process-liveness gap has been fixed:
  `fcodex_proxy` and `AppServerRuntimeStore` now both reuse
  `bot.process_utils.process_exists`
- the Windows sensitive-file permission contract is now explicit:
  Linux / macOS converge on `0600`, while Windows is explicitly downgraded to
  "user-directory isolation + NTFS ACL + one-time warning"
- platform test coverage has been extended from "file generation / command
  assembly" to service-manager lifecycle behavior, wrapper branches, and the
  explicit subprocess path used by `fcodex` on Windows

So this document should now be read as a list of remaining cross-platform
gaps, not as evidence that cross-platform support is still missing in general.

## 2. Areas Already Fully Covered

| Component | Key Files | Notes |
|------|----------|------|
| Install scripts | `install.sh` / `install.ps1` / `install.py` | both shell entrypoints delegate to `install.py`; the main implementation is cross-platform (`Scripts\\python.exe` vs `bin/python`) |
| Path resolution | `bot/platform_paths.py` | XDG (Linux) / `Library/Application Support` (macOS) / `%APPDATA%`, `%LOCALAPPDATA%` (Windows) |
| Service management | `bot/service_manager.py` | `SystemdUserServiceManager` / `LaunchdUserServiceManager` / `WindowsTaskSchedulerServiceManager` plus `current_service_manager()` |
| Wrapper generation | `bot/manage_cli.py:95-118` | Windows writes `.cmd`; Unix writes shell scripts |
| File locking | `bot/file_lock.py` | Windows uses `msvcrt.locking()`, Unix uses `fcntl.flock()` |
| Process-liveness utility | `bot/process_utils.py` | unified `process_exists()`: Unix uses `os.kill(pid, 0)`, Windows uses `OpenProcess()` through ctypes |
| Subprocess arguments | `bot/manage_cli.py` and others | list-form invocation everywhere, no `shell=True`, avoiding injection and quoting differences |
| README | `README.md:9, 27-39, 88-90, 227` | three-platform support is explicitly declared |

## 3. Remaining Punch List

### Fixed — Unix-only process-liveness checks on Windows-sensitive paths

- **Where:**
  - `bot/fcodex_proxy.py::run_proxy`
  - `bot/stores/app_server_runtime_store.py::AppServerRuntimeStore.load_managed_runtime`
- **Observed issue:**
  - both places previously used `os.kill(pid, 0)` directly
  - that bypassed the already-adapted `bot/process_utils.py`
  - consequences:
    - `fcodex` local cwd-proxy parent-PID exit detection was unreliable on Windows
    - stale owner / stale PID cleanup in persisted app-server runtime state was
      unreliable on Windows
- **Suggested fix:**
  1. delete both duplicate implementations and reuse
     `bot.process_utils.process_exists`
  2. add unit coverage for proxy self-exit on parent termination
  3. add unit coverage for stale PID cleanup in
     `AppServerRuntimeStore.load_managed_runtime()`
- **Impact:**
  - local `fcodex` proxy lifecycle on Windows
  - shared app-server runtime cleanup and discovery on Windows

### Fixed — Sensitive-file permission contract was unclear on Windows

- **Where:**
  - `bot/config.py::_atomic_write_text / save_system_config / ensure_init_token`
  - `bot/manage_cli.py::_ensure_text_file / _ensure_init_token`
  - `bot/stores/service_instance_lease.py::ServiceInstanceLease._write_metadata_unlocked`
  - `bot/env_file.py::ensure_env_template`
- **Observed issue:**
  - many write paths still treated `0o600` as the main protection mechanism
  - on Windows, that is neither reliable NTFS ACL protection nor visible to users
  - behavior was effectively "silent downgrade to default user-directory ACLs"
- **Suggested fix:**
  1. explicitly state in README / contract docs that:
     "On Windows, sensitive-file confidentiality depends on user-directory
     isolation and NTFS ACLs; POSIX `0600` semantics are not promised"
  2. print a one-time warning on Windows instead of silently swallowing the downgrade
  3. if the project later decides it wants to maintain it long-term, evaluate
     `icacls` as a best-effort hardening step; otherwise do not add a second
     unstable path

### Fixed — Platform tests extended from static output to lifecycle behavior

- **Where:** `tests/test_service_manager.py`, `tests/test_manage_cli.py`, etc.
- **Observed issue:**
  - existing tests already covered:
    - `SystemdUserServiceManager.ensure_service()`
    - `LaunchdUserServiceManager.ensure_service()`
    - `WindowsTaskSchedulerServiceManager.ensure_service()`
    - basic scaffolding in `manage_cli._handle_install()`
  - but they still lacked:
    - `start/stop/restart/status/uninstall` lifecycle tests
    - factory-branch tests for `current_service_manager()`
    - wrapper-generation tests for Windows `.cmd`
    - Windows-specific tests for `fcodex` / proxy parent-exit and cleanup paths
- **Suggested fix:**
  1. keep the existing mock-based tests
  2. add subprocess-mock lifecycle tests for all three `ServiceManager` implementations
  3. add Windows / Unix branch assertions for `manage_cli._write_wrapper()`
  4. add behavior tests for `fcodex_proxy` parent-PID probing and exit cleanup

### Fixed — `os.getuid()` readability

- **Where:** `bot/service_manager.py::LaunchdUserServiceManager`
- **Observed issue:** `os.getuid()` does not exist on Windows. The current call
  site is only reached on the macOS path, so runtime behavior is safe, but the
  code required reader-side tracing to confirm that Windows could not hit it.
- **Suggested fix:** resolved by adding an explicit macOS-only class docstring.

### Fixed — Windows no longer depends on `os.execvpe()` lifecycle semantics

- **Where:** `bot/fcodex.py::_run_upstream_codex / main`
- **Observed issue:**
  - the implementation has now converged to:
    - Unix: still use `os.execvpe()`
    - Windows: explicitly use `subprocess.Popen(...); wait(); cleanup`
  - so the Windows path no longer depends on `execvpe` platform differences
- **Suggested follow-up:**
  1. keep the current implementation
  2. later do one manual validation pass in a real Windows terminal for Ctrl-C,
     exit codes, and proxy cleanup

## 4. Verification Checklist

- [x] `bot/fcodex_proxy.py` and `bot/stores/app_server_runtime_store.py` now both
      use `bot.process_utils.process_exists`.
- [x] The Windows `fcodex` path now uses explicit subprocess management and has
      parent-exit / cleanup test coverage.
- [ ] End-to-end self-test of Windows Task Scheduler for
      `feishu-codex install/start/stop/status` passes.
- [ ] macOS launchd plist registration works, `launchctl list` sees it, and log
      paths are correct.
- [x] `tests/test_service_manager.py` and adjacent coverage now exercise
      lifecycle actions for all three managers, not just `ensure_service()`.
- [x] `tests/test_manage_cli.py` now covers both Windows `.cmd` and Unix shell
      wrapper generation branches.
- [x] Windows sensitive-file permission downgrade now has explicit docs and a
      user-visible warning.
- [x] README / docs now explain Windows directory-permission behavior.

## 5. Post-Fix Code Review (2026-04-22)

After the fixes landed, the diff was reviewed end-to-end.
`python -m pytest tests/test_service_manager.py tests/test_manage_cli.py tests/test_codex_app_server.py`
reported 84 passed. The overall direction matches the punch list. The points
below capture confirmed fixes and the remaining minor rough edges.

### 5.1 Confirmed In Place

- **P0 unified process-liveness checks**:
  the duplicate `_process_exists()` implementations in `bot/fcodex_proxy.py`
  and `bot/stores/app_server_runtime_store.py` are gone; both now use
  `bot.process_utils.process_exists`.
  `tests/test_codex_app_server.py` adds
  `test_proxy_exits_when_parent_process_disappears` and
  `test_load_managed_runtime_clears_file_when_app_server_pid_is_stale`.
- **P1 centralized file-permission handling**:
  `bot/file_permissions.py::ensure_private_file_permissions()` now owns the
  policy: Unix uses `chmod 0o600`, Windows prints a one-time warning and returns.
  Call sites in `bot/config.py`, `bot/manage_cli.py`,
  `bot/stores/service_instance_lease.py`, and `bot/env_file.py` have been moved
  over. `test_handle_install_creates_scaffold_and_wrappers` asserts `0600` on
  Unix for `system.yaml`, `init.token`, and `feishu-codex.env`.
- **P1 broader test coverage**:
  `tests/test_service_manager.py` now covers `start / status / uninstall`,
  the three factory branches of `current_service_manager()`, and unsupported
  platform failures. `tests/test_manage_cli.py` adds explicit Windows / Unix
  wrapper-generation tests.
- **P2 `service_manager.py:175`**:
  a `"""macOS-only launchd user service manager."""` docstring was added.
- **P2 Windows `os.execvpe` branch removal**:
  `bot/fcodex.py` adds `_run_upstream_codex()`;
  Windows now uses `subprocess.Popen + wait + SystemExit(returncode)`, while
  `_stop_child_process()` handles TERM → timeout → KILL cleanup.
  Adjacent tests cover both normal exit and `KeyboardInterrupt`.
- **Docs**:
  `README.md` now includes a security note; and
  `docs/contracts/runtime-control-surface.md` /
  `docs/contracts/runtime-control-surface.zh-CN.md`
  explicitly state that Windows does not promise POSIX `0600` semantics and
  instead relies on user-directory isolation plus NTFS ACLs.

### 5.2 Remaining Small Issues (Non-Blocking)

1. **`test_fcodex_uses_subprocess_on_windows_and_cleans_proxy` could assert more
   strictly that an already-exited codex child is not redundantly terminated.**

   The current mock already sets `child_process.poll.return_value = 7`
   (meaning the child has exited), and proxy cleanup is correct.
   This is only a test-tightness suggestion.

2. **`import os` in `bot/stores/app_server_runtime_store.py`** is now only used
   by `os.replace()`. This is harmless and can be ignored.

3. **The `else: os.chmod(tmp_path, mode)` branch in
   `bot/config.py::_atomic_write_text` is not hit by current call sites**
   (all of them use `0o600`). This is a harmless fallback; either keeping it or
   extending `ensure_private_file_permissions` to accept `mode` would be fine.

4. **`bot/file_permissions.py` currently emits its warning through
   `print(..., file=sys.stderr)`**. In daemon contexts that warning may end up
   only in logs. The one-time process-level flag
   `_warned_windows_acl_fallback` is still reasonable.
   If the project later standardizes logging here, it can become
   `logger.warning`.

### 5.3 Suggested Next Actions

- **Optional**: route `file_permissions.py` warnings through the logger and let
  `ensure_private_file_permissions` accept `mode`, which would also eliminate
  the dead-ish `config.py` fallback branch.
- **Still needs real-platform validation**:
  full Windows Task Scheduler self-test, and macOS launchd plist registration /
  log-path verification (both remain unchecked in the verification checklist).

## 6. References

- Audit scope: `bot/`, `tests/`, `install.{sh,ps1,py}`, `README.md`, `docs/`
- Audit date: 2026-04-22
- Current branch: `main`
