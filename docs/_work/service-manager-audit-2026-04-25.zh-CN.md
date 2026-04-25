# Service Manager 重构审视报告 — 2026-04-25

本文件针对最近一批“安装入口拆分 + 自启动单独管理 + systemd 模板化”改动做复核，并收口上一版审视中过重或不准确的判断。

- 复核基线：`main` 工作区，基于 HEAD `b3a1898`，并已包含本文件记载的修复
- 涉及文件：
  - `README.md`、`bot/manage_cli.py`、`bot/service_manager.py`
  - `tests/test_manage_cli.py`、`tests/test_service_manager.py`
- 复测：
  - `PYTHONPATH=. pytest -q tests/test_manage_cli.py tests/test_service_manager.py`
  - 结果：`31 passed`
- 复核原则：只看当前公开合同是否清晰、代码是否与合同一致；**不把兼容性包袱本身当成问题**

结论先行：

- 这批重构的主方向是对的，公开管理面已经明显收紧为一套更清晰的合同
- 真正需要修的，核心只有两项：
  - 命名实例不应被普通管理命令静默创建
  - macOS LaunchAgent 悬空 symlink 不应被报成 autostart enabled
- 上一版审视里提到的“孤立 service 清理”“macOS plist 热重载”“Windows XML 编码兼容”不应被当成当前合同下的 bug

---

## 一、当前公开合同

| 维度 | 当前合同 |
| --- | --- |
| 公开安装入口 | `bash install.sh` / `./install.ps1`；Python 侧只保留内部 `bootstrap-install` |
| 默认实例初始化 | 安装脚本负责初始化 `default` 实例 |
| 命名实例初始化 | 统一通过 `feishu-codex instance create <name>` |
| 运行态 vs 自启动 | `start|stop|restart|status` 只管运行态；`autostart enable|disable|status` 只管登录后自动启动 |
| Linux 实例 unit | `default` 用 `feishu-codex.service`；命名实例用模板 `feishu-codex@.service` |
| macOS 自启动 | service 定义在实例数据目录的 `service.plist`；LaunchAgent 路径只表达 autostart |
| Windows 自启动 | Task Scheduler XML 中是否存在 `LogonTrigger` 决定 autostart |

这里最关键的一条是：

- **命名实例的创建入口只有 `instance create`**
- 其他管理命令可以消费既有实例，但不应该顺手把一个新命名实例“建出来”

---

## 二、修订后的问题判断

### 2.1 命名实例被普通管理命令静默创建（已修复）

- 旧位置：`bot/manage_cli.py` 中 `start|autostart|config|run` 共用 `_ensure_instance_scaffold()`
- 问题性质：
  - README 已经把“创建命名实例”收口到 `feishu-codex instance create <name>`
  - 但实现上，只要跑 `feishu-codex --instance corp-a start` 或 `config system`，就会在本地生成 `instances/corp-a/`
  - 这会把“查看 / 管理已有实例”和“创建新实例”混成一条模糊路径
- 修复后合同：
  - `default` 实例仍允许按旧方式自动补 scaffold
  - 命名实例必须先显式 `instance create`
  - `config env` 仍然例外可直接用，因为它操作的是机器级共享 `feishu-codex.env`，不是实例级 scaffold

### 2.2 macOS LaunchAgent dangling symlink 被误报为 enabled（已修复）

- 旧位置：`bot/service_manager.py` 中 `LaunchdUserServiceManager.autostart_status()`
- 旧行为：
  - 只要 LaunchAgent 路径“存在或是 symlink”，就返回 enabled
  - 如果用户手动删掉了 `data_dir/service.plist`，LaunchAgent symlink 仍在，状态会误报
- 修复后合同：
  - 悬空 symlink 返回 `enabled=False`
  - `detail` 明确标为 `launch agent symlink is dangling`

### 2.3 `uninstall` 不扫描孤立平台注册残留（不是当前合同 bug）

- README 写的是：`uninstall` 卸载“所有已知实例”的 service 定义 / 自启动注册与 wrapper
- 当前实现也确实按“已知实例”工作，而不是按平台注册表做前缀扫描
- 这最多是一个未来可选增强，不是“代码背离当前 README/help”的问题

### 2.4 macOS `ensure_service()` 后未主动热重载 launchd（不是当前合同 bug）

- 当前合同已经把“重建 service 定义”和“当前运行态 / autostart 状态”拆开了
- `ensure_service()` 的职责是重写定义文件，不是承诺把已加载 job 立即热更新
- 如果用户显式 `start/restart`，launchd 会按最新定义启动；这与当前公开语义一致

### 2.5 Windows `schtasks /XML` 编码问题（不是当前合同 bug）

- 这属于平台真机验证项，不属于当前公开合同的清晰性问题
- 在“不要把兼容性包袱本身当成问题”的复核口径下，不应继续列为 bug

---

## 三、已落地修复

### 3.1 管理 CLI

- `--instance` help 明确写出：命名实例必须先 `instance create`
- 顶层 help 明确写出：其他命令不会隐式创建命名实例
- 新增 `_prepare_cli_instance()`：
  - `default` 走自动 scaffold
  - 命名实例不存在时直接报错，并提示 `feishu-codex instance create <name>`
- `start|autostart|run|config(system/codex/init-token)` 都改用这条校验路径
- `config env` 保持可直接使用，并只确保共享 env 文件存在

### 3.2 macOS service manager

- `LaunchdUserServiceManager.autostart_status()` 现在会识别悬空 symlink
- 避免“登录后不会正常启动，但状态却显示 enabled”的假阳性

### 3.3 文档与测试

- README 补充：除 `instance create` 外，其他 `feishu-codex --instance <name> ...` 命令不会隐式创建命名实例
- 新增回归测试：
  - 命名实例命令不会静默创建实例
  - `config env` 不要求命名实例先存在
  - macOS autostart status 能识别 dangling symlink

---

## 四、当前剩余判断

按这次修订后的口径，当前这组 service manager 改动里：

- 没有 P0 / P1 问题
- 已修复的真实问题是：
  - 命名实例静默创建
  - macOS dangling symlink 状态误报
- 剩余未修项里，没有我认为必须继续追的合同级 bug

---

## 五、复核说明

- 本文件只覆盖 service manager、安装入口、自启动管理、多实例管理面的收口
- 不覆盖飞书业务逻辑、thread/runtime 合同、卡片交互
- 本文件是 `_work` 复核记录，不是长期事实源
