# feishu-codex

`feishu-codex` 通过 Feishu 机器人把消息、审批和会话管理接到 `codex app-server`，不依赖 Claude 风格 hook，也不扫描私有会话文件。

当前状态是可安装、可启动、核心链路可用的 MVP，架构已经切到 Codex 原生协议，但功能成熟度仍低于 `feishu-cc`。

## 前置条件

- Python 3.10+
- 本机已安装 `codex` CLI，且 `codex --help` 可正常执行
- 飞书开放平台已创建应用，获取 `app_id` 和 `app_secret`

## 安装

```bash
cd /path/to/feishu-codex
bash install.sh
```

`install.sh` 会自动完成：

- 创建 Python 虚拟环境到 `~/.local/share/feishu-codex/.venv/`
- 安装代码包与依赖
- 初始化配置文件到 `~/.config/feishu-codex/`
- 注册 systemd 用户服务并安装 `feishu-codex` 管理命令

安装后填写飞书凭证：

```bash
nano ~/.config/feishu-codex/system.yaml
```

可选地调整飞书 API 请求超时：

```yaml
# ~/.config/feishu-codex/system.yaml
app_id: "..."
app_secret: "..."
# request_timeout_seconds: 10
```

按需调整 Codex 参数：

```bash
nano ~/.config/feishu-codex/codex.yaml
```

如果你的 Codex provider 通过环境变量取 key，推荐统一写到：

```bash
nano ~/.config/environment.d/90-codex.conf
```

例如：

```ini
provider1_api_key=...
provider2_api_key=...
```

`install.sh` 会把这个文件接入 `feishu-codex.service`，并且 `feishu-codex run`、`fcodex` wrapper 也会一并加载它。

如果你希望本地 TUI 与飞书安全共用同一线程，推荐使用安装脚本生成的 `fcodex` wrapper。
它会自动把本地 TUI 接到 `feishu-codex` 使用的 shared app-server endpoint，而不是再起一个独立 backend。
默认情况下，`fcodex` 还会继承 `feishu-codex` 自己维护的本地默认 profile；显式 `fcodex -p <profile>` 仍以显式参数为准。
`fcodex` 自己解析的特殊命令只有 `fcodex /help`、`fcodex /profile`、`fcodex /rm`、`fcodex /session`、`fcodex /resume` 这几类，并且必须单独使用；其余参数和子命令都会继续原样传给裸 `codex`。
如果你想在本地先查看线程，再决定恢复哪个，可执行 `fcodex /session`（当前目录）或 `fcodex /session global`（全局）。

如果你希望启用 Codex 原生 `requestUserInput` 卡片，而不是让模型退化成普通文本追问，需要在 `codex.yaml` 中显式开启：

```yaml
collaboration_mode: plan
```

## 配置

运行时环境变量：

- `FC_CONFIG_DIR`: 配置目录
- `FC_DATA_DIR`: 数据目录

未设置时，开发态默认读取项目内 `config/`，数据默认写到 `data/feishu_codex/`。

与 shared backend 相关的常用配置项：

```yaml
# app_server_mode: managed
# app_server_url: ws://127.0.0.1:8765
```

## 使用

```bash
feishu-codex start
feishu-codex stop
feishu-codex restart
feishu-codex status
feishu-codex log
feishu-codex run
feishu-codex config
feishu-codex uninstall
feishu-codex purge
```

本地若要安全继续飞书侧同一线程，使用：

```bash
fcodex
fcodex /help
fcodex /profile
fcodex /profile <profile_name>
fcodex /rm <thread_id>
fcodex /session
fcodex /session global
fcodex /resume <thread_id>
fcodex /resume <thread_name>
fcodex resume <thread_id>
```

说明：

- `fcodex /profile`、`fcodex /rm`、`fcodex /session`、`fcodex /resume ...` 是 `fcodex` wrapper 自己处理的特殊命令
- 目前 wrapper 只接管 `fcodex /help`、`fcodex /profile`、`fcodex /rm`、`fcodex /session`、`fcodex /resume ...`
- 这些 wrapper 自命令必须单独使用，不能和裸 `codex` 的 flags/子命令混用
- 非 `/` 开头的参数和子命令，会继续原样传给裸 `codex`
- 因此 `fcodex session` 不再被当作 wrapper 命令，而会像 `codex session` 一样继续透传

如果你只是临时调试，也可以直接：

```bash
python -m bot
```

## 设计要点

- Codex 线程元数据以 app-server 为单一事实源
- 本地只持久化 Feishu 特有状态，例如收藏
- `/session` 显示当前目录线程，收藏优先
- `/resume` 先按 thread id 原生恢复，失败后再按 thread name 精确匹配
- `/session` 与名字匹配式 `/resume` 都显式跨 provider 检索
- `/profile` 只维护 feishu-codex 与默认 `fcodex` 的本地默认 profile，不改动裸 `codex` 全局配置
- 对未加载在当前 backend 中的外部线程，`/resume` 会先给出“查看快照 / 恢复并继续写入 / 取消”三选一保护卡片
- 原生 `requestUserInput` 依赖 `collaboration_mode: plan`，并通过 `initialize.capabilities.experimentalApi=true` 启用

## Session / Profile 语义

- 飞书 `/session`
  - 只显示当前目录线程
  - 显式跨 provider 汇总
- 飞书 `/resume <thread_id|thread_name>`
  - 按后端全局精确匹配
  - 可跨 provider
  - 同名多匹配直接报错
- wrapper 级 `fcodex /resume <thread_name>`
  - 先调用 feishu-codex 的共享发现逻辑做跨 provider 精确匹配
  - 匹配到唯一线程后，转成 `thread_id` 调 upstream `codex --remote ... resume <id>`
- plain `fcodex` / `fcodex <prompt>`
  - 直接连接 shared backend
  - 目录语义由 `--cd` 或当前 shell cwd 决定
- wrapper 级 `fcodex /session [cwd|global]`
  - 使用 feishu-codex 的共享发现逻辑列线程
  - `fcodex /session` 默认列当前目录、跨 provider 线程
  - `fcodex /session global` 列后端全局、跨 provider 线程
- wrapper 级 `fcodex /help`
  - 只展示这些 shell wrapper 自命令的边界与语义
  - 不能写成 `fcodex --cd /repo /session` 或 `fcodex /resume demo --model ...` 这种混合形式
- wrapper 级 `fcodex /profile [name]`
  - 查看或切换 feishu-codex / 默认 `fcodex` 的本地默认 profile
  - 不改写裸 `codex` 全局配置
- wrapper 级 `fcodex /rm <thread_id|thread_name>`
  - 调用 Codex 公开的线程归档（archive）
  - 会从常规列表中隐藏，不是硬删除
- `fcodex` TUI 内置 `/resume`
  - 保持 upstream 原样
  - 不复用 feishu-codex 的跨 provider 名字解析逻辑
  - 当前版本通常按 backend 默认 provider 过滤
  - 不受 feishu-codex `/profile` 控制，也不应假定它与飞书 `/session` 的筛选范围一致
- `/profile`
  - 只影响飞书侧默认 profile 与未显式 `-p/--profile` 的 `fcodex`
  - 不影响裸 `codex`
  - `fcodex -p <profile>` 永远优先

补充设计文档：

- `docs/feishu-codex-design.md`
- `docs/shared-backend-resume-safety.md`

## 当前功能

- 直接发送普通文本给当前线程；若未绑定线程，会在当前目录自动新建
- `/new`、`/session`、`/resume <thread_id|thread_name>`、`/rename <title>`、`/star`
- `/profile` 查看或切换 feishu-codex 默认 profile
- `/rm [thread_id|thread_name]` 归档线程；省略参数时归档当前线程
- `/cd`、`/pwd`、`/status`、`/cancel`
- `/mode` 查看或切换当前飞书会话后续 turn 的协作模式（`default` / `plan`）
- `/approval` 查看或切换原生 Codex 审批策略
- 原生 Codex 审批卡片：
  - `item/commandExecution/requestApproval`
  - `item/fileChange/requestApproval`
  - `item/permissions/requestApproval`
  - `item/tool/requestUserInput`
- 第一版计划卡片：
  - `turn/plan/updated` 会展示结构化计划步骤
  - `item=plan` 完成时会展示计划正文

说明：

- `collaboration_mode: default` 下，Codex 仍可能把“先问用户再继续”的需求退化成普通文本回复
- `collaboration_mode: plan` 下，Feishu 才能接到真正的 `item/tool/requestUserInput` 并回传原生回答结果

## 与 feishu-cc 的现状差距

当前还没有这些能力：

- `feishu-cc` 的 workspace 系列命令和 `/run`
- `feishu-cc` 的 `/model` 和更多会话控制能力
- 更完整的降级、重试、异常恢复和可观测性
- 更完整的 MCP 交互支持
