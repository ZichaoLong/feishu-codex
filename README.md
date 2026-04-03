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

实用规则只记这几条：

- 飞书 `/session` 只看当前目录，跨 provider 汇总
- 飞书 `/resume` 按后端全局精确匹配
- `fcodex /session`、`fcodex /resume <name>` 复用与飞书一致的共享发现逻辑
- `fcodex resume <id>` 以及进入 TUI 后的 `/resume` 保持 upstream 原样
- `/profile` 只改 feishu-codex / 默认 `fcodex` 的本地默认 profile，不改裸 `codex` 全局配置
- 想和飞书安全共用同一线程时，优先用 `fcodex`，不要让裸 `codex` 同时写同一线程

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

推荐把语义理解为三层：

- 飞书命令
  - `/session`：当前目录，跨 provider
  - `/resume`：后端全局精确匹配，跨 provider
- `fcodex` shell wrapper 命令
  - `fcodex /session`、`fcodex /resume <name>`：复用飞书同一套共享发现逻辑
- 进入 TUI 后的 upstream 命令
  - TUI 内 `/help`、`/resume` 仍按 upstream 原样工作

完整语义见：

- `docs/session-profile-semantics.md`

补充设计文档：

- `docs/session-profile-semantics.md`
- `docs/codex-permissions-model.md`
- `docs/fcodex-shared-backend-runtime.md`
- `docs/feishu-codex-design.md`
- `docs/shared-backend-resume-safety.md`

对应中文副本：

- `docs/session-profile-semantics.zh-CN.md`
- `docs/codex-permissions-model.zh-CN.md`
- `docs/fcodex-shared-backend-runtime.zh-CN.md`
- `docs/feishu-codex-design.zh-CN.md`
- `docs/shared-backend-resume-safety.zh-CN.md`

## 当前功能

- 直接发送普通文本给当前线程；若未绑定线程，会在当前目录自动新建
- `/new`、`/session`、`/resume <thread_id|thread_name>`、`/rename <title>`、`/star`
- `/profile` 查看或切换 feishu-codex 默认 profile
- `/rm [thread_id|thread_name]` 归档线程；省略参数时归档当前线程
- `/cd`、`/pwd`、`/status`、`/cancel`
- `/mode` 查看或切换当前飞书会话后续 turn 的协作模式（`default` / `plan`）
- `/approval` 查看或切换原生 Codex 审批策略
- `/sandbox` 查看或切换当前飞书会话后续 turn 的沙箱策略（`read-only` / `workspace-write` / `danger-full-access`）
- `/permissions` 以预设方式同时切换审批策略和沙箱（`read-only` / `default` / `full-access`）
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
- `/mode` 改的是“当前飞书会话后续 turn”的协作模式；只有下一条由飞书发起的普通消息真正触发 `turn/start` 时才会写入 backend
- `fcodex` TUI 与飞书共享同一 live thread，但不共享一个即时同步的 mode 控制面；TUI `/collab` 看到的是 TUI 自己这侧的当前状态，不保证与飞书刚执行的 `/mode` 立即一致
- 如果飞书和 TUI 同时都在操作同一线程，谁发起下一轮 turn，哪一轮就按谁当前携带的 mode 执行

### 权限模型速记

- `sandbox` 管“技术边界”，也就是命令最终在什么权限下执行。
  - `read-only`：默认只读、默认无网络；命令仍可执行，但文件系统写入会被限制。
  - `workspace-write`：默认可读全盘、可写当前工作区；当前实现里工作区下仍会默认保护 `.git`、`.agents`、`.codex` 等路径。
  - `danger-full-access`：基本不做内层隔离。
- `approval_policy` 管“审批边界”，也就是什么时候要先经过审批才能继续。
  - `untrusted`：只有“已知安全且只读”的命令会自动执行，其它大多先审批。
  - `on-request`：由模型决定何时请求审批；这是当前默认值。
  - `never`：不发起审批，失败直接返回给模型。
  - `on-failure`：上游已标记 deprecated；交互式场景建议改用 `on-request`。
- 这两项是独立旋钮，不互相替代。
  - `workspace-write + on-request` 近似 upstream 默认 Agent 模式。
  - `read-only + on-request` 更适合“只看代码/只分析”。
  - `danger-full-access + never` 接近 Full Access。
- 在当前 `feishu-codex` 里，审批默认由飞书用户处理；上游 Codex 还支持把审批路由到 `guardian_subagent`，但这里默认未启用。
- 更具体的上游实现、平台后端与排障说明见 `docs/codex-permissions-model.md`。

## 与 feishu-cc 的现状差距

当前还没有这些能力：

- `feishu-cc` 的 workspace 系列命令和 `/run`
- `feishu-cc` 的 `/model` 和更多会话控制能力
- 更完整的降级、重试、异常恢复和可观测性
- 更完整的 MCP 交互支持
