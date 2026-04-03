# Shared Backend 与 Resume 安全性设计

英文原文：`docs/shared-backend-resume-safety.md`

## 1. 问题陈述

只有当 `feishu-codex` 与原生 Codex TUI 通过同一个 app-server backend 写入同一个线程时，它们才是安全的。

如果它们通过不同的 app-server 进程去恢复同一个持久化线程，就可能各自物化出自己的 live 内存线程，随后再追加彼此冲突的状态。

本文定义以下内容的安全模型、面向用户的语义，以及实现边界：

- shared backend 模式
- 对当前 backend 中未加载线程执行 `/resume`
- 同一线程在多个飞书会话中的行为

## 2. 已验证约束

### 2.1 我们可以依赖的硬事实

- 在同一个 app-server 进程内，恢复一个已经加载的线程时，会复用已加载线程和订阅者模型，而不是创建第二份 live 副本。
- `thread/loaded/list`、`thread/list.status` 和 `thread/read.status` 只描述当前 app-server 进程。
- `thread/read` 读取的是已存储历史，不会创建 live thread。
- `thread/resume` 会把线程加载进当前 app-server，成为 live thread。

### 2.2 我们不能依赖的事实

- 我们无法可靠检测另一个原生 TUI 进程当前是否正在写同一个线程。
- `source` 和 `service_name` 只是来源提示，不是 live ownership 或 lock 信号。
- 我们无法强制一个原生 TUI 进程停止写入。
- 以当前公开机制，我们无法自动附着到原生 TUI 自带的 app-server。

## 3. 设计目标

- 让安全路径清晰且容易采用。
- 避免伪确定性，例如“没有其它客户端正在写”。
- 避免对外部线程静默双写。
- 让用户心智模型保持足够简单，便于日常使用。

## 4. 非目标

- 不尝试做全局跨进程线程锁。
- 不把“takeover”表述成真实的技术交接。
- 不默认在 resume 时 fork。
- 不根据 `source=cli` 之类启发式信息做安全决策。

## 5. 核心模型

所有地方统一使用一条规则：

- 一个线程应只通过一个 backend 写入。

如果用户希望飞书和本地 TUI 安全地同时操作同一个 live thread，它们就必须连接到同一个 app-server backend。

## 6. Backend 模式

### 6.1 Shared backend 模式

这是推荐的稳定工作模式。

行为：

- `feishu-codex` 维护一个稳定的本地 websocket endpoint。
- 本地 TUI 通过 `codex --remote ...` 连接，而不是启动自己的内嵌 backend。
- 飞书与本地 TUI 共享同一份已加载线程状态。

推荐本地 wrapper：

```bash
fcodex "$@"
```

等价启动形态：

```bash
codex --remote ws://127.0.0.1:PORT "$@"
```

特性：

- 对同一线程，不会因双 live thread 而产生分叉
- 对多个本地 TUI 窗口连接到同一个 shared backend 也是安全的
- 一旦用户接受这条路径，心智负担更低
- shared backend 不意味着存在一个跨客户端即时同步的统一控制面，例如“待发下一轮的协作模式选择”

补充说明：

- live thread 通过同一个 backend 共享
- 但每个客户端仍各自决定自己下一次 `turn/start` 要发送什么
- 因此，飞书里改的协作模式不会立刻改写已打开 TUI 的当前显示，反过来也是一样

### 6.2 Isolated backend 模式

当用户不带 `--remote` 直接运行原生 TUI 时，这是兼容模式。

特性：

- `feishu-codex` 无法知道本地 TUI 是空闲、关闭，还是即将写入
- `feishu-codex` 不能安全地假设自己对某个外部线程拥有独占所有权
- 对外部线程执行 resume 时，必须要求用户显式选择

## 7. `/resume` 语义

### 7.1 分类

在匹配到目标线程后，只使用硬事实进行分类：

1. `loaded-in-current-backend`
2. `not-loaded-in-current-backend`

不要基于缓存的 ownership 启发式，再额外发明一个“可能安全”的第三类。

### 7.2 已加载于当前 backend

如果目标线程已经加载在当前 `feishu-codex` backend 中：

- 直接恢复
- 将当前飞书会话绑定到该线程
- 不展示风险卡片

这是安全的，因为该线程已经活在同一个 backend 里。

### 7.3 未加载于当前 backend

如果目标线程当前没有加载在本 backend 中，`/resume` 不能立刻调用 `thread/resume`。

应先展示一张三操作卡片：

- `查看快照`
- `恢复并继续写入`
- `取消`

#### `查看快照`

行为：

- 调用 `thread/read`
- 展示标题、cwd、更新时间、source、可选的 `service_name`，以及最近几轮对话
- 不绑定当前飞书会话
- 不在当前 backend 中创建 live thread

它是安全的只读检查路径。

#### `恢复并继续写入`

行为：

- 调用 `thread/resume`
- 将当前飞书会话绑定到该线程
- 明确回复一条警告：这会在 `feishu-codex` backend 中创建一个 live thread

必须表达清楚的警告含义：

- 如果另一个非 shared-backend 客户端也在写这个线程，历史可能分叉，或者至少让后续状态变得混乱
- 如果想避免这件事，本地继续时请使用 `fcodex`

这是一条经用户确认的风险路径，不是技术上的“接管”。

#### `取消`

行为：

- 不做任何事

### 7.4 为什么 fork 不在默认流程里

`thread/fork` 从技术上是安全的，但它会制造额外分支线程，进而让 TUI 和飞书两边后续的会话恢复都变得更难理解。

因此当前策略是：

- 不在默认 `/resume` 风险卡片中提供 fork
- 如果未来需要，再把 fork 作为显式命令单独暴露

## 8. 来源展示

把来源元数据只作为信息展示：

- `source`
- 如果存在则展示 `service_name`

用途：

- 帮助用户理解线程来自哪里
- 帮助区分 shared thread 与 external thread

不要仅凭 provenance 自动做安全决策。

## 9. 用户指引

产品应反复教一条操作规则，而不是堆很多例外：

- 如果希望在本地与飞书继续同一个线程，请使用 `fcodex`，不要直接用裸 `codex`。

建议放在高价值位置：

1. `/help`
2. 外部线程的 `/resume` 风险卡片
3. 飞书第一次物化线程后给一次提示

不要在每一张执行卡片或每一条消息卡片上重复警告。

## 10. 对称风险

风险是对称的。

如果某个线程已经在 `feishu-codex` 中活跃，用户之后又用裸 `codex` 通过自己的 backend 恢复同一个线程，同样存在双 backend 分叉风险。

`feishu-codex` 无法阻止这件事。它能做的只有：

- 推荐用户使用 `fcodex`
- 避免把未知外部线程静默恢复到可写状态

## 11. 飞书多会话语义

当前的安全性和 UX 是两个不同问题。

### 11.1 安全性

同一个 `feishu-codex` service 下的所有飞书会话本来就共享同一个 backend 进程，因此不会像飞书和裸 TUI 之间那样，为每个会话创建不同的 app-server 进程。

所以它们不会遭遇跨进程双 live thread 分叉问题。

### 11.2 当前 UX 限制

当前实现对每个 `thread_id` 只维护一个 `(user_id, chat_id)` 绑定。

这意味着：

- 最后一个绑定到该线程的飞书会话，会收到流式更新和审批请求
- 当前并不支持多会话镜像式 live view

因此，当前支持的语义是：

- 飞书内部共享线程状态，对 backend 安全
- 每个线程只有一个会话拥有通知归属权

真正的多会话镜像查看不在本文设计范围内。

## 12. 最小实现计划

### Phase A: Shared backend 模式

- 把随机监听地址替换为可配置的稳定 endpoint
- 支持可选的本地 auth token
- 提供 `fcodex` wrapper 或等价辅助命令
- 在 `/status` 中展示当前 backend 模式

### Phase B: 受保护的外部线程 resume

- 增加线程分类：当前 backend 中已加载 / 未加载
- 增加三操作外部线程卡片
- 基于 `thread/read` 增加快照预览渲染

### Phase C: UX 指引

- 在 `/help` 中加入一条清晰规则
- 在风险卡片文案中明确推荐 shared 模式
- 在合适位置展示 provenance 信息

## 13. 验收标准

- 在 shared 模式下，飞书和本地 TUI 可以恢复并写入同一个线程，而不会因为各自物化出独立 live 副本而分叉。
- 对外部线程，飞书绝不会静默将其恢复到可写状态。
- 用户始终有一条不会物化 live thread 的安全查看路径。
- UI 永远不会声称自己知道另一个外部 TUI 是否正在主动写入。
