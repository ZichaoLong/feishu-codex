# Shared Backend 与 Resume 安全性

英文原文：`docs/shared-backend-resume-safety.md`

另见：

- `docs/fcodex-shared-backend-runtime.zh-CN.md`：当前 shared backend 与 wrapper 的运行时模型
- `docs/session-profile-semantics.zh-CN.md`：精确的命令与 wrapper 语义
- `docs/feishu-codex-design.zh-CN.md`：架构与仓库边界

## 1. 上游基线

- 上游项目：[`openai/codex`](https://github.com/openai/codex.git)
- 当前本地验证基线：`codex-cli 0.118.0`（2026-04-03）
- 本文只聚焦安全边界与 `/resume` 语义；wrapper 运行时细节不再在这里重复展开，而是以 `fcodex-shared-backend-runtime` 为准。

## 2. 问题陈述

只有当 `feishu-codex` 与 stock Codex TUI 通过同一个 app-server backend 写入同一个线程时，它们才是安全的。

如果它们通过不同的 app-server 进程去恢复同一个持久化线程，就可能各自物化出自己的 live 内存线程，随后再追加彼此冲突的状态。

本文定义当前的安全模型，用于说明：

- shared backend 路径
- 对当前 backend 中未加载线程执行 `/resume`
- 同一线程在多个飞书会话中的行为边界

## 3. 已验证约束

### 3.1 我们可以依赖的硬事实

- 在同一个 app-server 进程内，恢复一个已经加载的线程时，会复用已加载线程和订阅者模型，而不是创建第二份 live 副本。
- `thread/loaded/list`、`thread/list.status` 和 `thread/read.status` 只描述当前 app-server 进程。
- `thread/read` 读取的是已存储历史，不会创建 live thread。
- `thread/resume` 会把线程加载进当前 app-server，成为 live thread。

### 3.2 我们不能依赖的事实

- 我们无法可靠检测另一个 stock TUI 进程当前是否正在写同一个线程。
- `source` 和 `service_name` 只是来源提示，不是 live ownership 或 lock 信号。
- 我们无法强制另一个 stock TUI 进程停止写入。
- 以当前公开机制，我们无法自动附着到原生 TUI 自带的 app-server。

## 4. 核心安全规则

所有地方统一使用一条规则：

- 一个线程应只通过一个 backend 写入。

如果用户希望飞书和本地 TUI 安全地同时操作同一个 live thread，它们就必须连接到同一个 app-server backend。

## 5. Backend 安全边界

### 5.1 Shared backend

这是推荐的安全路径。

特性：

- 飞书与本地 TUI 通过同一个 app-server backend 写入
- 已加载线程状态在这个 backend 内共享
- 多个本地 TUI 窗口附着到同一个 backend，也不会引入跨进程分叉

shared backend 与 `fcodex` wrapper 具体如何实现，见 `docs/fcodex-shared-backend-runtime.zh-CN.md`。

### 5.2 Isolated backend

当用户脱离 shared backend 直接运行 stock TUI 时，就是这一路径。

特性：

- `feishu-codex` 无法知道这个本地 TUI 是空闲、关闭，还是即将写入
- `feishu-codex` 不能安全地假设自己对该线程拥有独占所有权
- 对这种外部线程的 resume，必须要求用户显式选择

## 6. `/resume` 安全模型

### 6.1 分类

在匹配到目标线程后，只使用硬事实进行分类：

1. `loaded-in-current-backend`
2. `not-loaded-in-current-backend`

不要再额外发明一个基于启发式缓存的“可能安全”类别。

### 6.2 已加载于当前 backend

如果目标线程已经加载在当前 `feishu-codex` backend 中：

- 直接恢复
- 将当前飞书会话绑定到该线程
- 不展示风险卡片

这是安全的，因为该线程已经活在同一个 backend 里。

### 6.3 未加载于当前 backend

如果目标线程当前没有加载在本 backend 中，`/resume` 不应立刻调用 `thread/resume`。

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

这是安全的只读检查路径。

#### `恢复并继续写入`

行为：

- 调用 `thread/resume`
- 将当前飞书会话绑定到该线程
- 明确回复一条警告：这会在当前 `feishu-codex` backend 中创建一个 live thread

警告需要表达清楚的含义：

- 如果另一个非 shared-backend 客户端也在写这个线程，历史可能分叉，或者至少让后续状态变得混乱
- 如果目标是本地继续同一个 live thread，应优先走 shared backend 路径

这是一条经用户确认的风险路径，不是技术上的“接管”。

#### `取消`

行为：

- 不做任何事

## 7. 来源展示与对称风险

把来源元数据只作为信息展示：

- `source`
- 如果存在则展示 `service_name`

用途：

- 帮助用户理解线程来自哪里
- 帮助区分 shared thread 与 external thread

不要仅凭 provenance 自动做安全决策。

风险是对称的：

- 如果飞书把外部线程恢复进自己的 backend，可能产生分叉
- 如果用户之后又用裸 `codex` 在另一个 backend 恢复飞书正在使用的线程，同样存在风险

`feishu-codex` 不能消除这种风险。它能做的，是避免把未知外部线程静默恢复到可写状态，并让安全路径保持显式。

## 8. 飞书多会话边界

安全性和 UX 是两个不同问题。

### 8.1 安全性

同一个 `feishu-codex` service 下的所有飞书会话本来就共享同一个 backend 进程，因此不会像飞书和裸 TUI 之间那样，为每个会话创建不同的 app-server 进程。

所以它们不会遭遇那种跨进程双 live thread 分叉问题。

### 8.2 当前 UX 限制

当前实现对每个 `thread_id` 只维护一个主要的通知绑定。
私聊场景下，这个绑定等价于 `(sender_id, chat_id)`；群聊场景下，则是群共享 state key 与 `chat_id`。

这意味着：

- 最后一个绑定到该线程的飞书会话，会收到流式更新和审批请求
- 当前并不支持多会话镜像式 live view

当前支持的语义是：

- 飞书内部共享线程状态，对 backend 安全
- 每个线程只有一个会话拥有通知归属权

## 9. 相关文档

- `docs/session-profile-semantics.zh-CN.md`：`/session`、`/resume`、`fcodex` 与 profile 的精确命令语义
- `docs/fcodex-shared-backend-runtime.zh-CN.md`：shared backend、动态端口发现、cwd 代理与 wrapper 运行时行为
- `docs/feishu-codex-design.zh-CN.md`：架构、设计约束与当前仓库结构
