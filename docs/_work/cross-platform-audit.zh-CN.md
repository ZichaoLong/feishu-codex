---
title: 跨平台适配审视（Linux / macOS / Windows）
date: 2026-04-22
status: 部分已修复
---

# 跨平台适配审视

本文记录 `feishu-codex` 项目对三大平台（Linux、macOS、Windows）的适配现状，作为后续修复的工单底稿。

## 一、整体结论

主体安装、目录、服务、文件锁、包装脚本已基本完成三平台分流，README 也已明确声明 Linux / macOS / Windows 支持。

但这次复核后，之前的结论需要收紧：

- 之前最高优先级的 Windows 进程探活残留已修复，`fcodex_proxy` 与 `AppServerRuntimeStore` 已统一复用 `bot.process_utils.process_exists`
- Windows 敏感文件权限 contract 已补充：Linux / macOS 收敛到 `0600`，Windows 明确降级为“用户目录 + NTFS ACL + 一次性 warning”
- 平台测试覆盖已从“文件生成/命令拼装”扩展到 service manager 生命周期、wrapper 分支，以及 Windows 下 `fcodex` 的显式子进程路径

因此，这份文档应作为“剩余跨平台缺口清单”，而不是“跨平台适配尚未成形”的结论。

## 二、已完整覆盖的部分

| 组件 | 关键文件 | 说明 |
|------|----------|------|
| 安装脚本 | `install.sh` / `install.ps1` / `install.py` | sh/ps1 都转调 `install.py`，主实现跨平台（venv 目录用 `Scripts\python.exe` vs `bin/python` 区分） |
| 目录解析 | `bot/platform_paths.py` | XDG（Linux）/ `Library/Application Support`（macOS）/ `%APPDATA%`、`%LOCALAPPDATA%`（Windows） |
| 服务管理 | `bot/service_manager.py` | `SystemdUserServiceManager` / `LaunchdUserServiceManager` / `WindowsTaskSchedulerServiceManager` 三实现 + `current_service_manager()` 工厂 |
| 包装脚本 | `bot/manage_cli.py:95-118` | Windows 生成 `.cmd`、Unix 生成 shell 脚本 |
| 文件锁 | `bot/file_lock.py` | Windows 用 `msvcrt.locking()`，Unix 用 `fcntl.flock()` |
| 进程存活检测基础工具 | `bot/process_utils.py` | 已提供统一的 `process_exists()`：Unix 用 `os.kill(pid, 0)`，Windows 用 ctypes 调 `OpenProcess()` |
| 子进程参数 | `bot/manage_cli.py` 等 | 全部 list 形式，无 `shell=True`，避免注入与平台 quoting 差异 |
| README | `README.md:9, 27-39, 88-90, 227` | 明确声明三平台支持，命令统一 |

## 三、待修复问题（Punch List）

### 已修复 — Windows 敏感路径里的 Unix-only 进程探活逻辑

- **位置：**
  - `bot/fcodex_proxy.py::run_proxy`
  - `bot/stores/app_server_runtime_store.py::AppServerRuntimeStore.load_managed_runtime`
- **现象：**
  - 两处 `_process_exists()` 仍直接使用 `os.kill(pid, 0)`
  - 这绕过了已经做过 Windows 适配的 `bot/process_utils.py`
  - 结果是：
    - `fcodex` 本地 cwd proxy 的 `--parent-pid` 退出检测在 Windows 上不可靠
    - app-server runtime 持久化状态的 stale owner / stale pid 清理在 Windows 上不可靠
- **建议修复：**
  1. 删除这两处重复实现，统一复用 `bot.process_utils.process_exists`
  2. 为 proxy 的 parent-exit 自关路径补单测
  3. 为 `AppServerRuntimeStore.load_managed_runtime()` 的 stale pid 清理补单测
- **影响面：**
  - Windows 下 `fcodex` 本地 proxy 生命周期
  - Windows 下 shared app-server 运行时状态清理与发现

### 已修复 — Windows 下敏感文件权限 contract 不清晰

- **位置：**
  - `bot/config.py::_atomic_write_text / save_system_config / ensure_init_token`
  - `bot/manage_cli.py::_ensure_text_file / _ensure_init_token`
  - `bot/stores/service_instance_lease.py::ServiceInstanceLease._write_metadata_unlocked`
  - `bot/env_file.py::ensure_env_template`
- **现象：**
  - 多处敏感文件写入仍把 `0o600` 当作主要保护手段
  - 在 Windows 上，这既不是可靠的 NTFS ACL 保护，也没有用户可见提示
  - 当前实现等价于“静默退化为依赖用户目录默认 ACL”
- **建议修复：**
  1. 在 README / contract docs 中明确声明：
     “Windows 下敏感文件保密性依赖用户目录隔离与 NTFS ACL，不承诺 POSIX `0600` 语义”
  2. 对 Windows 上的权限降级打印一次性 warning，而不是静默吞掉
  3. 若后续确认愿意长期维护，可再评估 `icacls` 作为 best-effort 加固；否则不要引入第二套不稳定路径

### 已修复 — 平台测试覆盖从静态生成扩展到生命周期行为

- **位置：** `tests/test_service_manager.py`、`tests/test_manage_cli.py` 等
- **现象：**
  - 现有测试已覆盖：
    - `SystemdUserServiceManager.ensure_service()`
    - `LaunchdUserServiceManager.ensure_service()`
    - `WindowsTaskSchedulerServiceManager.ensure_service()`
    - `manage_cli._handle_install()` 的基础 scaffold
  - 但仍缺：
    - `start/stop/restart/status/uninstall` 的行为测试
    - `current_service_manager()` 工厂分支测试
    - Windows `.cmd` wrapper 生成测试
    - `fcodex` / proxy 在 Windows 上的 parent-exit 与清理路径测试
- **建议修复：**
  1. 保留现有 mock 测试，不要误删
  2. 为三个 ServiceManager 的生命周期动作各补一组 subprocess mock 测试
  3. 为 `manage_cli._write_wrapper()` 增加 Windows / Unix 双分支断言
  4. 为 `fcodex_proxy` 的 `parent_pid` 探活与退出清理增加行为测试

### 已修复 — `os.getuid()` 可读性

- **位置：** `bot/service_manager.py::LaunchdUserServiceManager`
- **现象：** `os.getuid()` 在 Windows 上不存在。当前调用位于 `LaunchdUserServiceManager` 内部，仅 macOS 路径会触达，运行时安全。但代码读起来需要追踪调用方才能确认没有 Windows 触发风险。
- **建议修复：** 已通过类 docstring 标注为 macOS-only。

### 已修复 — `os.execvpe()` 的 Windows 生命周期语义不再作为运行时依赖

- **位置：** `bot/fcodex.py::_run_upstream_codex / main`
- **现象：**
  - 当前实现已收敛为：
    - Unix: 继续使用 `os.execvpe()`
    - Windows: 显式 `subprocess.Popen(...); wait(); cleanup`
  - 因此 Windows 路径不再依赖 `execvpe` 的平台语义差异
- **建议修复：**
  1. 保留当前实现
  2. 后续在真实 Windows 终端再做一次 Ctrl-C / 退出码 / proxy 清理手工验证

## 四、复核清单（修复后请逐项验证）

- [x] `bot/fcodex_proxy.py` 与 `bot/stores/app_server_runtime_store.py` 已统一改用 `bot.process_utils.process_exists`。
- [x] Windows 下 `fcodex` 代码路径已改为显式子进程管理，并覆盖父进程退出/清理的单测。
- [ ] Windows 下 `feishu-codex install/start/stop/status` 走 Task Scheduler 的全链路自测通过。
- [ ] macOS 下 launchd plist 注册、`launchctl list` 可见、日志路径正确。
- [x] `tests/test_service_manager.py` 与相关测试已覆盖三平台 manager 的生命周期动作，而不仅是 `ensure_service()`。
- [x] `tests/test_manage_cli.py` 已覆盖 Windows `.cmd` 与 Unix shell wrapper 两条分支。
- [x] 已补充 Windows 上敏感文件权限退化的文档说明和用户可见提示。
- [x] README / docs 已增补 Windows 配置目录权限说明。

## 五、修复后的代码审视（2026-04-22）

在修复落地后对 diff 做了一次完整审视，测试 `python -m pytest tests/test_service_manager.py tests/test_manage_cli.py tests/test_codex_app_server.py` 84 passed。整体方向与 punch list 一致，以下记录确认项与遗留小瑕疵。

### 5.1 已确认到位

- **P0 进程探活统一**：`bot/fcodex_proxy.py` 与 `bot/stores/app_server_runtime_store.py` 的 `_process_exists()` 已删除，改走 `bot.process_utils.process_exists`。`tests/test_codex_app_server.py` 新增 `test_proxy_exits_when_parent_process_disappears` 与 `test_load_managed_runtime_clears_file_when_app_server_pid_is_stale` 分别覆盖 proxy parent-exit 与 app-server runtime stale pid 清理两条路径。
- **P1 权限集中化**：新增 `bot/file_permissions.py::ensure_private_file_permissions()`，在 Unix 下 `chmod 0o600`，在 Windows 下一次性打印中文 warning 并 return。`bot/config.py`、`bot/manage_cli.py`、`bot/stores/service_instance_lease.py`、`bot/env_file.py` 四处 `os.chmod(..., 0o600)` 调用点都已迁移。`test_handle_install_creates_scaffold_and_wrappers` 在 Unix 上断言 `system.yaml`、`init.token`、`feishu-codex.env` 三个文件 mode 确为 `0600`。
- **P1 测试覆盖拓展**：`tests/test_service_manager.py` 新增三个 Manager 的 `start / status / uninstall` 生命周期测试、`current_service_manager()` 工厂三分支测试、不支持平台的 `ServiceManagerError` 测试。`tests/test_manage_cli.py` 新增 `test_write_wrapper_creates_windows_cmd_launcher` 与 `test_write_wrapper_creates_unix_shell_launcher` 两条分支。
- **P2 `service_manager.py:175`**：补了 `"""macOS-only launchd user service manager."""` docstring。
- **P2 `os.execvpe` Windows 分支**：`bot/fcodex.py` 新增 `_run_upstream_codex()`，Windows 下走 `subprocess.Popen + wait + SystemExit(returncode)`；`_stop_child_process()` 负责 TERM→超时→KILL 的统一清理。`test_fcodex_uses_subprocess_on_windows_and_cleans_proxy` 与 `test_fcodex_windows_interrupt_cleans_codex_and_proxy` 分别覆盖正常退出与 `KeyboardInterrupt` 路径。
- **文档**：`README.md` 新增“安全说明”小节；`docs/contracts/runtime-control-surface.md(.zh-CN.md)` 在 owner metadata 条目下声明 Windows 不承诺 POSIX `0600` 语义、依赖当前用户目录与 NTFS ACL。

### 5.2 遗留小瑕疵（非阻塞，修复时可顺手处理）

1. **`test_fcodex_uses_subprocess_on_windows_and_cleans_proxy` 里 `child_process.poll.return_value = 7` 的正常退出路径没严格断言“不重复 terminate 已退出的 codex”**。

   当前 mock 里 `poll.return_value = 7` 已被设好（表示子进程已退出），走 `finally` 清理 proxy 的路径正确。此条仅为测试严密性建议，不影响正确性。

2. **`bot/stores/app_server_runtime_store.py` 的 `import os`** 现在只剩 `os.replace()` 一处用到。不构成问题，跳过即可。

3. **`bot/config.py::_atomic_write_text` 的 `else: os.chmod(tmp_path, mode)` 分支当前调用点不会命中**（全部使用 `0o600`）。属无害兜底，保留或让 `ensure_private_file_permissions` 接 `mode` 参数均可，非必要。

4. **`bot/file_permissions.py` 的 warning 走 `print(..., file=sys.stderr)`**。后台 daemon 场景下该提示可能被吞到 log 文件中不够显眼。`_warned_windows_acl_fallback` flag 保证进程级只提示一次，设计合理。若项目统一了 logger，后续可替换为 `logger.warning`。

### 5.3 建议的后续动作

- **可选**：把 `file_permissions.py` 的 warning 改走 logger；将 `ensure_private_file_permissions` 扩展为接受 `mode`，顺手消掉 `config.py` 的 else 死分支。
- **待真实平台验证**：Windows Task Scheduler 全链路自测、macOS launchd plist 注册与日志路径自测（复核清单中仍为未勾选项）。

## 六、参考

- 探查范围：`bot/`、`tests/`、`install.{sh,ps1,py}`、`README.md`、`docs/`
- 探查日期：2026-04-22
- 当前分支：`main`
