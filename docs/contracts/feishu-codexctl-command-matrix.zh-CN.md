# `feishu-codexctl` 命令矩阵

英文原文：`docs/contracts/feishu-codexctl-command-matrix.md`

另见：

- `docs/contracts/feishu-command-matrix.zh-CN.md`
- `docs/contracts/local-command-and-thread-profile-contract.zh-CN.md`
- `docs/contracts/runtime-control-surface.zh-CN.md`
- `docs/contracts/session-profile-semantics.zh-CN.md`

本文定义本地 `feishu-codexctl` 管理面的正式命令矩阵。

它回答五件事：

- `feishu-codexctl` 到底管哪些资源
- 每个子命令作用于哪个状态层
- 哪些命令只读，哪些命令会改状态
- 参数约束与实例选择规则是什么
- 它与飞书命令面分别对应哪些能力，不对应哪些能力

如果代码行为与本文不一致，应把它视为合同缺口，并收紧代码、文档，或两者一起修正。

## 1. 范围

本文只描述本地 `feishu-codexctl` 命令面。

它不重新定义：

- 飞书 slash 命令矩阵
- `fcodex` wrapper 语义
- thread 生命周期与 runtime 词汇
- `reset-backend`、`unsubscribe`、`/status`、`/preflight` 的底层行为

这些内容分别以相关专题文档为准。

## 2. 定位

`feishu-codexctl` 是本地查看 / 管理面。

它的正式定位是：

- 查看运行中的实例
- 查看目标实例的 service / binding / thread 状态
- 对 binding / thread 做有限的管理动作

它不是：

- 第二个 Codex 前端
- 进入 TUI 的入口
- 飞书 chat-scoped 命令在本地的逐一镜像

因此：

- 想继续 live thread，用 `fcodex`
- 想管理本地 service、自启动、安装与实例，用 `feishu-codex`
- 想看 binding / thread / service 当前状态，或做 thread-scoped 管理，用 `feishu-codexctl`

## 3. 全局规则

### 3.1 实例选择

- 除 `instance list` 外，其余命令都可接受 `--instance <name>`
- 省略 `--instance` 时，默认目标实例是 `default`
- `instance list` 是跨实例查看面，不使用 `--instance`
- 当前 `feishu-codexctl` 只接受一个 `--instance`，不像 `feishu-codex` 那样支持批量多实例

### 3.2 资源分层

命令面按四类资源分层：

- `instance`
  - 运行中的实例注册表
- `service`
  - 某个实例的后台 service / control plane / backend 概况
- `binding`
  - 某个实例里 Feishu chat binding 的本地事实
- `thread`
  - persisted thread 发现面，以及某个 thread 的 thread-scoped 管理

### 3.3 Thread 目标约束

对于以下 thread 子命令：

- `thread status`
- `thread bindings`
- `thread unsubscribe`

必须且只能提供其中一种：

- `--thread-id <id>`
- `--thread-name <name>`

这不是可选建议，而是命令面硬约束。

### 3.4 `binding clear` 与 `thread unsubscribe` 不是一回事

`binding clear` / `clear-all`：

- 清的是 Feishu 本地 bookmark
- 不删除 thread
- 不等于 `unsubscribe`

`thread unsubscribe`：

- 释放的是 Feishu 对该 thread 的 runtime residency
- 保留 thread 与 binding 关系

这两个动作作用在不同状态层，文档和产品文案不得混用。

## 4. 命令矩阵

### 4.1 `instance` 资源

| 命令 | 作用 | 状态层 | 类型 | 关键参数 | 飞书对应 |
| --- | --- | --- | --- | --- | --- |
| `feishu-codexctl instance list` | 列出本机当前运行中的实例、owner pid、control endpoint、app-server 地址 | 运行中实例注册表 | 只读 | 无；不使用 `--instance` | 无直接飞书对应 |

### 4.2 `service` 资源

| 命令 | 作用 | 状态层 | 类型 | 关键参数 | 飞书对应 |
| --- | --- | --- | --- | --- | --- |
| `feishu-codexctl [--instance <name>] service status` | 查看目标实例当前 service 运行态、control endpoint、app-server 地址，以及 binding / thread 统计 | 实例级 service / control plane 概况 | 只读 | 可选 `--instance` | 无一条完全等价的飞书命令；它更接近实例管理员视角 |
| `feishu-codexctl [--instance <name>] service reset-backend [--force]` | 重置当前实例 backend，但不重启 `feishu-codex` service | 实例级 backend 生命周期 | 变更 | 可选 `--instance`；可选 `--force` | 对应飞书 `/reset-backend`，但这是本地实例管理面 |

### 4.3 `binding` 资源

| 命令 | 作用 | 状态层 | 类型 | 关键参数 | 飞书对应 |
| --- | --- | --- | --- | --- | --- |
| `feishu-codexctl [--instance <name>] binding list` | 列出当前实例可见 binding，以及其 binding state、Feishu runtime、关联 thread 与 cwd | 实例内 binding 发现面 | 只读 | 可选 `--instance` | 无直接飞书对应；比飞书 `/session` 和 `/status` 更底层 |
| `feishu-codexctl [--instance <name>] binding status <binding_id>` | 查看单个 binding 的 chat、thread、runtime、next prompt 可用性、interaction owner、当前会话设置等 | 单个 binding 详细状态 | 只读 | `binding_id` | 覆盖并超出飞书 `/status` 与 `/preflight` |
| `feishu-codexctl [--instance <name>] binding clear <binding_id>` | 清除单个 binding bookmark | 单个 binding bookmark | 变更 | `binding_id` | 无直接飞书对应 |
| `feishu-codexctl [--instance <name>] binding clear-all` | 清除当前实例下全部 binding bookmark | 实例内全部 binding bookmark | 变更 | 可选 `--instance` | 无直接飞书对应 |

### 4.4 `thread` 资源

| 命令 | 作用 | 状态层 | 类型 | 关键参数 | 飞书对应 |
| --- | --- | --- | --- | --- | --- |
| `feishu-codexctl [--instance <name>] thread list [--scope cwd\|global] [--cwd <path>]` | 列 persisted thread；默认按当前目录过滤，也支持全局列出 | persisted thread 发现面 | 只读 | 可选 `--instance`；`--scope cwd/global`；`--cwd` 仅对 `cwd` 作用域有意义 | 部分对应飞书 `/session` 与 `/resume` 的目标发现面 |
| `feishu-codexctl [--instance <name>] thread status (--thread-id <id> \| --thread-name <name>)` | 查看某个 thread 的 backend 状态、bound/attached/released bindings、interaction owner、unsubscribe 可用性 | 单个 thread 的 thread-scoped 状态 | 只读 | 必须二选一：`--thread-id` 或 `--thread-name` | 无一条完全等价的飞书命令；部分覆盖飞书 `/status`、`/preflight`、`/unsubscribe` 的底层诊断 |
| `feishu-codexctl [--instance <name>] thread bindings (--thread-id <id> \| --thread-name <name>)` | 查看某个 thread 当前关联的 binding 列表 | 单个 thread 到 binding 的反向关系 | 只读 | 必须二选一：`--thread-id` 或 `--thread-name` | 无直接飞书对应 |
| `feishu-codexctl [--instance <name>] thread unsubscribe (--thread-id <id> \| --thread-name <name>)` | 让 Feishu 释放某个 thread 的 runtime residency，同时保留 thread 与 binding 关系 | 单个 thread 的 Feishu runtime residency | 变更 | 必须二选一：`--thread-id` 或 `--thread-name` | 对应飞书 `/unsubscribe`，但这是 thread-scoped 而不是当前 chat-scoped |

## 5. 与飞书命令面的对应关系

### 5.1 有较明确对应关系的项目

| `feishu-codexctl` | 飞书侧最接近入口 | 关键区别 |
| --- | --- | --- |
| `service reset-backend` | `/reset-backend` | 都是实例级 backend 管理；飞书面是管理员卡片流，本地面是 CLI 管理流 |
| `binding status <binding_id>` | `/status`、`/preflight` | 本地输出更底层，包含 binding id、interaction owner、reason code 等调试信息 |
| `thread unsubscribe --thread-id/--thread-name` | `/unsubscribe` | 飞书 `/unsubscribe` 只作用于当前 chat binding；本地命令可按任意 thread 定位 |
| `thread list --scope cwd` | `/session` | 飞书 `/session` 是 chat 使用入口；本地命令只是线程发现面 |
| `thread list --scope global` / `thread status` | `/resume` 的目标发现与诊断 | 飞书 `/resume` 是恢复动作；本地命令是查看 / 管理，不会帮你进入 live thread |

### 5.2 刻意没有飞书对应的项目

下列本地命令当前明确没有飞书侧一对一命令：

- `instance list`
- `service status`
- `binding list`
- `binding clear`
- `binding clear-all`
- `thread bindings`

原因是：

- 它们属于本地管理员 / 调试视角
- 直接暴露到飞书会抬高普通用户认知负担
- 其中一些动作，如 `binding clear`，是纯本地清理面，不属于日常 chat 使用合同

## 6. 输出与心智模型

阅读输出时，当前合同建议这样理解：

- `instance`
  - 回答“现在有哪些实例真的在跑”
- `service`
  - 回答“这个实例的后台服务和 control plane 现在怎么样”
- `binding`
  - 回答“某个飞书会话当前默认指向哪个 thread、还能不能直接继续”
- `thread`
  - 回答“这个 thread 现在在 backend 里是什么状态、有哪些 binding 连着它、能不能让 Feishu 释放 runtime”

最重要的一条：

- `binding` 是 chat-scoped 视角
- `thread` 是 thread-scoped 视角

两者不要混读。

## 7. 关联事实源

当前文档对应的主要实现事实源包括：

- `bot/feishu_codexctl.py`
- `bot/runtime_admin_controller.py`
- `bot/instance_resolution.py`
- `bot/session_resolution.py`
- `bot/service_control_plane.py`

如果未来新增、删除、改名任何 `feishu-codexctl` 子命令，或改变实例选择规则、thread 目标约束、状态层边界、以及与飞书命令面的对应关系，都应同步更新本文。
