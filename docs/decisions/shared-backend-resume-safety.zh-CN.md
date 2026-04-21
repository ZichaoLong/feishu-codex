# Shared Backend 与 Resume 安全性

英文原文：`docs/decisions/shared-backend-resume-safety.md`

另见：

- `docs/architecture/fcodex-shared-backend-runtime.zh-CN.md`：当前 shared backend 与 wrapper 的运行时模型
- `docs/contracts/runtime-control-surface.zh-CN.md`：`/status`、`/release-feishu-runtime` 与本地管理面的共享状态词汇
- `docs/contracts/session-profile-semantics.zh-CN.md`：精确的命令与 wrapper 语义
- `docs/architecture/feishu-codex-design.zh-CN.md`：架构与仓库边界

## 1. 上游基线

- 上游项目：[`openai/codex`](https://github.com/openai/codex.git)
- 当前本地验证基线：`codex-cli 0.118.0`（2026-04-03）
- 本文只聚焦安全边界与 `/resume` 语义；wrapper 运行时细节不再在这里重复展开，而是以 `fcodex-shared-backend-runtime` 为准。

## 2. 问题陈述

只有当两个前端通过同一个 app-server backend 写入同一个线程时，它们才是安全的。

如果它们通过不同的 app-server 进程去恢复同一个持久化线程，就可能各自物化出自己的 live 内存线程，随后再追加彼此冲突的状态。

多实例支持落地后，这条规则需要更明确地读成：

- 多个实例可以共享 `CODEX_HOME` 和 persisted thread namespace
- 但同一时刻，一个 thread 只能被**一个实例 backend** 持有 live runtime
- 裸 `codex` 自己开的 isolated backend 仍然完全不在这条协调路径内

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

在当前仓库里，这条规则又拆成两层：

- **实例内**：Feishu 与 `fcodex` 可以安全共享同一个实例 backend
- **实例间**：通过机器级 `ThreadRuntimeLease` 保证同一 thread 不会被两个实例 backend 同时 live attach

## 5. Backend 安全边界

### 5.1 实例内 shared backend

这是推荐的安全路径。

特性：

- 飞书与本地 TUI 通过同一个 app-server backend 写入
- 已加载线程状态在这个 backend 内共享
- 多个本地 TUI 窗口附着到同一个 backend，也不会引入跨进程分叉

shared backend 与 `fcodex` wrapper 具体如何实现，见 `docs/architecture/fcodex-shared-backend-runtime.zh-CN.md`。

### 5.2 另一个 `feishu-codex` 实例 backend

这是多实例模式新增的一条边界。

特性：

- 多个实例共享 persisted thread namespace
- 但每个实例有自己独立的 live backend
- 同一 thread 的 live residency 由机器级 `ThreadRuntimeLease` 协调
- 若 owner 实例当前 idle，且其 `release_feishu_runtime_available` 为真，则允许自动转移
- 若 owner 实例当前仍在执行，或仍有待处理审批 / 输入，则必须明确拒绝

因此，这不是“共享 backend”，也不是“可以并发双写的两个 backend”。
它是一条**共享持久化 namespace、但 live runtime 严格单 owner** 的协调路径。

### 5.3 裸 `codex` 的 isolated backend

当用户脱离 shared backend 直接运行 stock TUI 时，就是这一路径。

特性：

- `feishu-codex` 无法知道这个本地 TUI 是空闲、关闭，还是即将写入
- `feishu-codex` 不能安全地假设自己对该线程拥有独占所有权
- 如果要在本地继续同一个 live thread，应改用 `fcodex` 走同一个实例的 shared backend
- 如果仍用裸 `codex` 在另一个 backend 写同一线程，就超出了当前支持的安全路径

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
- `resume` 不会借机改写这个 live runtime 的 profile 或 provider

这是安全的，因为该线程已经活在同一个 backend 里。
如果后续需要按另一套 profile 重新解析这个线程，前提是它先回到 `not-loaded-in-current-backend`。

### 6.3 未加载于当前 backend

如果目标线程当前没有加载在本 backend 中，本仓库的安全取舍是直接调用 `thread/resume`。

行为：

- 直接恢复目标线程
- 将当前飞书会话绑定到该线程
- 如果用户随后通过 `fcodex` 接入同一个实例 shared backend，则飞书与 `fcodex` 可以继续安全地共同读写这个 live thread
- 若当前 thread 已被另一实例 backend live attach，则真正的恢复仍要服从机器级 `ThreadRuntimeLease`：
  - owner 可立即 release 时，允许自动转移
  - owner 仍 busy / pending 时，必须明确拒绝
- 如果这次恢复没有显式指定 profile，则飞书与 `fcodex` 的有效行为一致：都使用当前实例 / 所选实例的本地默认 profile

这条路径的前提是：

- 本地继续同一线程时，使用 `fcodex`
- 不要再用裸 `codex` 通过另一个 backend 写这个线程

这里需要刻意记住一件事：两端的执行路径并不相同。

- 飞书侧会在请求 `thread/resume` 前解析并显式传入 profile / model / model_provider
- `fcodex` 则是在 wrapper 启动阶段注入默认 profile，再进入 upstream `codex resume`

这个差异不应被理解为两端语义不一致；它们的目标语义是同一个：对 unloaded 线程，在没有显式 profile 时，都以当前实例 / 所选实例的本地默认 profile 恢复。

本仓库的取舍是**不再**通过预览/确认卡片拦截这类 resume。
因此，对“可能同时被另一个 isolated backend 写入”的线程，避免双 backend 写入的责任在操作侧，而不是由 UI 强制保护。

对命名实例还要再补一条可见性边界：

- 飞书 `/session`、飞书 `/resume` 受当前实例的 `admission + binding` 可见面约束
- `fcodex /session`、`fcodex /resume <name>` 则是本地操作者视角，不读取该 admission 过滤
- 但一旦真的要 live attach，所有路径仍统一服从 `ThreadRuntimeLease`

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

`feishu-codex` 不能消除这种风险。本仓库选择了更直接的 `/resume` 路径，因此安全边界依赖一条操作约束：需要多端继续同一 live thread 时，统一走 shared backend / `fcodex`，不要混用裸 `codex`。

## 8. 飞书多会话边界

安全性和 UX 是两个不同问题。

### 8.1 安全性

同一个实例下的所有飞书会话本来就共享同一个 backend 进程，因此不会像飞书和裸 TUI 之间那样，为每个会话创建不同的 app-server 进程。

所以它们不会遭遇那种跨进程双 live thread 分叉问题。

### 8.2 当前 UX / ownership 取舍

当前模型已经不是“每个 `thread_id` 只维护一个主要通知绑定”的旧模型。

现在更准确的描述是：

- 同一个 `thread_id` 可以同时存在多个 Feishu subscriber / binding
- 这些 subscriber 共享同一个 backend thread，因此对 backend 安全
- 但真正驱动执行与交互路由的仍是 owner / lease，而不是“最后一个绑定者”

具体来说：

- Feishu 内部写入准入由 `Feishu 写入 owner` 控制
- 跨 Feishu / `fcodex` 的审批、补充输入、中断等交互准入由 `interaction owner` 控制
- 当某线程当前没有显式 owner，但只有一个 Feishu subscriber 时，运行时可以按“唯一 subscriber”补位路由；一旦出现多个 subscriber，就必须依赖明确 owner，而不再靠“最后一个绑定”猜测

这带来的用户侧结论是：

- 非 owner 的 Feishu 会话仍可以保留 binding，并继续观察线程的共享事实状态
- 非 owner 不能继续写入，也不能处理当前 turn 的审批 / 输入请求
- 当前仍不承诺“多个飞书会话都看到完全镜像的可交互 live UI”；执行卡片、审批卡和 request 驱动事件仍按 owner 路径路由，而不是向所有 subscriber 广播

因此，这一层的决策结论是：

- 飞书内部允许多 subscriber，共享同一 backend thread
- 可写性与可交互性由 owner lease 决定
- “只有一个主要通知绑定”已经不是当前模型

### 8.3 跨实例边界

多实例并不改变上面这套“实例内多 subscriber”结论，但还要再补两条边界：

- 不同实例之间不共享 live backend
- 同一 thread 若需要跨实例继续，必须先经过：
  - admission（是否对目标实例 Feishu 可见）
  - thread runtime lease（当前是否允许目标实例接管 live runtime）

因此：

- 多实例共享的是 persisted thread namespace
- 不共享的是 live thread 内存态、binding、ACL、owner 和 control plane

精确状态词汇与状态迁移，以
`docs/contracts/runtime-control-surface.zh-CN.md`
和 `docs/contracts/feishu-thread-lifecycle.zh-CN.md` 为准。

## 9. 相关文档

- `docs/contracts/session-profile-semantics.zh-CN.md`：`/session`、`/resume`、`fcodex` 与 profile 的精确命令语义
- `docs/architecture/fcodex-shared-backend-runtime.zh-CN.md`：shared backend、动态端口发现、cwd 代理与 wrapper 运行时行为
- `docs/architecture/feishu-codex-design.zh-CN.md`：架构、设计约束与当前仓库结构
