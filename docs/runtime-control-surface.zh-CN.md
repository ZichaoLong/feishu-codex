# 运行时控制面

英文原文：`docs/runtime-control-surface.md`

本文定义 Feishu 命令面、本地 `feishu-codexctl` 管理面，以及 shared backend
之间共享的一组状态词汇与控制合同。它主要回答三件事：

- `/status` 到底在描述哪些状态
- `/release-feishu-runtime` 具体释放什么，不释放什么
- 为什么本地管理 CLI 必须通过正在运行的 `feishu-codex` 服务，而不能自己直连 app-server 做释放

另见：

- `docs/feishu-thread-lifecycle.zh-CN.md`
- `docs/session-profile-semantics.zh-CN.md`
- `docs/shared-backend-resume-safety.zh-CN.md`

## 1. 上游基线

- 上游项目：[`openai/codex`](https://github.com/openai/codex.git)
- 当前本地验证基线：`codex-cli 0.118.0`（2026-04-03）

## 2. 共享状态词汇

这组词汇是 Feishu `/status` 和本地 `feishu-codexctl` 共同使用的事实词汇。

### 2.1 `binding`

表示某个飞书会话逻辑上当前绑定到哪个 `thread_id`。

- `unbound`
  - 当前没有绑定线程
- `bound`
  - 当前仍绑定某个线程

`binding` 是“这个会话下一次默认接着哪个线程继续”的事实来源。
它不等于 runtime 是否仍加载，也不等于 Feishu 是否仍附着该 thread。

### 2.2 `feishu runtime`

表示 `feishu-codex` 这条 app-server 连接当前是否仍附着在该 thread 上。

- `attached`
  - Feishu 服务当前仍订阅这个 thread
- `released`
  - 绑定还在，但 Feishu 服务已经主动释放了自己对这个 thread 的运行时持有
- `not-applicable`
  - 当前没有绑定线程

这里描述的是 Feishu 这一侧的运行时附着状态，不是 backend 全局 loaded 状态。

### 2.3 `backend thread status`

表示该 thread 在当前 shared backend 中的状态。

典型值：

- `notLoaded`
- `idle`
- `active`
- `systemError`

这是 app-server 的线程状态，不是飞书会话自己的 UI 状态。

### 2.4 `backend running turn`

这是从 `backend thread status` 派生出来的判断：

- 当 status 为 `active` 时，值为 `yes`
- 其他状态为 `no`

它回答的是“当前 backend 上这个 thread 是否正在跑 turn”，并不等于“当前飞书会话是否是这次执行的 owner”。

### 2.5 `Feishu 写入 owner`

这是 `feishu-codex` 服务内部为多飞书订阅者维护的单写租约。

- 它只在 Feishu 服务内部存在
- 它不描述 `fcodex` 或其他外部前端
- 它的作用是：同一个 thread 在多个飞书会话同时绑定时，仍只允许一个飞书会话发起写入

因此，这是“Feishu 内部单写 owner”，不是“全系统全前端唯一写 owner”。

### 2.6 `交互 owner`

这是跨前端共享的交互租约，当前实现会在 Feishu 与 `fcodex` 之间共享。

它回答的问题是：

- 当前谁可以处理中断、审批、用户输入等交互控制

典型持有者：

- 某个 Feishu binding
- 某个 `fcodex` 本地终端
- `none`

### 2.7 `re-profile possible`

这是一个派生判断，不是持久化状态。

当前合同下：

- 当 `backend thread status == notLoaded` 时，值为 `yes`
- 否则为 `no`

它的含义是：

- 这个 thread 现在是否处于“下一次 `resume` / 自动重载有机会重新解析 profile / provider”的状态

不要再用“线程原始 profile”作为合同术语。
更准确的说法是：

- 当前本地默认 profile 恢复
- 显式 profile 恢复
- 当前 live runtime 无法借 `resume` 改 provider

## 3. 几个必须明确区分的组合

### 3.1 `bound + attached + active`

表示：

- 当前飞书会话仍绑定该 thread
- Feishu 服务仍附着该 thread
- backend 上当前正在执行 turn

### 3.2 `bound + attached + notLoaded`

这通常只会短暂出现于状态迁移边缘，最终应转到：

- 重新 resume 后回到 `attached + idle/active`
- 或显式释放后转到 `released + notLoaded`

### 3.3 `bound + released + notLoaded`

表示：

- 飞书侧逻辑绑定仍保留
- Feishu 服务已不再持有该 thread 的 runtime
- 当前 backend 里 thread 也已 unload

这是最典型的“可以重新切 profile 再恢复”的状态。

### 3.4 `bound + released + idle/active`

表示：

- Feishu 自己已经释放 runtime
- 但当前 backend 中还有其他订阅者仍附着该 thread
- 最常见的是本地 `fcodex`

因此，`released` 不保证 backend 一定 `notLoaded`。

## 4. `/status` 合同

飞书 `/status` 是 chat-scoped 命令。

它只回答：

- 当前这个聊天绑定的 `binding`
- 当前这个绑定所指 thread 的 `feishu runtime`
- 当前 shared backend 中该 thread 的 `backend thread status`
- 当前 `Feishu 写入 owner`
- 当前 `交互 owner`
- 当前是否 `re-profile possible`
- 当前是否允许执行 `/release-feishu-runtime`

它不会变成一个“全局线程管理器”。
全局线程和绑定状态应交给本地 `feishu-codexctl`。

## 5. `/release-feishu-runtime` 精确合同

### 5.1 作用对象

飞书 `/release-feishu-runtime`：

- 不带参数
- 作用于“当前 chat 绑定的 thread”
- 但它的实际生效范围是：这个 thread 在整个 `feishu-codex` 服务内的 Feishu runtime 持有

也就是说，它不是“只把当前 chat 自己标成 released”。

### 5.2 它会做什么

当该命令成功时：

- 保留所有指向该 thread 的 Feishu `binding`
- 清除该 thread 的 Feishu 写入 owner
- 清除该 thread 的 Feishu 交互 owner（如果当前 owner 是 Feishu）
- 把所有当前仍 `attached` 的相关 Feishu binding 统一切到 `released`
- 让 `feishu-codex` 服务自己的 app-server 连接对该 thread 执行 `thread/unsubscribe`

### 5.3 它不会做什么

它不会：

- 删除 thread
- archive thread
- 清空 Feishu chat 与 thread 的绑定关系
- 强制关闭本地 `fcodex` TUI
- 保证 backend 一定 unload

如果 backend 在命令后仍 `idle` 或 `active`，说明还有外部订阅者没有释放。

### 5.4 它何时会被拒绝

当前实现会在以下场景拒绝释放：

- 该 thread 当前仍有飞书侧 turn 在执行
- 该 thread 当前仍有飞书侧审批请求或用户输入请求未处理

这是为了避免把“当前仍由 Feishu 负责收口的执行”切成半关闭状态。

### 5.5 成功后的解释

如果命令成功后：

- `backend thread status == notLoaded`
  - 说明当前 backend 中已不再有订阅者
  - 后续重新 resume 时，重新解析 profile / provider 是可能的
- `backend thread status in {idle, active, systemError}`
  - 说明 backend 仍 loaded
  - 最常见原因是本地 `fcodex` 还在订阅这个 thread

### 5.6 之后再发普通消息会怎样

如果某个 Feishu binding 当前仍 `bound`，但其 `feishu runtime == released`，那么之后在这个 chat 里直接发送普通消息时：

1. 先按当前绑定的 `thread_id` 重新附着 / resume
2. 再启动 turn

如果当时该 thread 已 `notLoaded`，这条重新附着路径会遵守
`docs/session-profile-semantics.zh-CN.md` 里关于 unloaded thread 的 profile 恢复合同。

## 6. 本地管理面：`feishu-codexctl`

### 6.1 它是什么

`feishu-codexctl` 是 `feishu-codex` 服务的本地管理 CLI。

它不是：

- `fcodex` 的别名
- 飞书聊天命令的本地壳
- 一个新的 app-server 前端

它的职责是：

- 查看服务状态
- 查看 binding / thread 的共享状态词汇
- 对运行中的 `feishu-codex` 服务发出明确的管理动作

### 6.2 为什么它必须经过运行中的服务

上游公开协议里，`thread/unsubscribe` 是 connection-scoped 的。

这意味着：

- 如果本地 CLI 自己连 app-server，再发 `thread/unsubscribe`
- 它只会取消“CLI 这条连接自己的订阅”
- 不会取消 `feishu-codex` 服务那条连接的订阅

因此，任何要真正改变“Feishu 是否仍附着该 thread”的动作，都必须由运行中的 `feishu-codex` 服务代为执行。

### 6.3 第一批命令

当前实现提供：

- `feishu-codexctl service status`
- `feishu-codexctl binding list`
- `feishu-codexctl binding status <binding_id>`
- `feishu-codexctl thread status <thread_id|thread_name>`
- `feishu-codexctl thread bindings <thread_id|thread_name>`
- `feishu-codexctl thread release-feishu-runtime <thread_id|thread_name>`

### 6.4 `binding_id` 形状

本地管理 CLI 使用稳定的 admin-facing `binding_id`：

- 群聊共享 binding：`group:<chat_id>`
- 私聊 binding：`p2p:<sender_id>:<chat_id>`

它们只服务于本地管理面，不需要和飞书聊天命令名字保持对称。

## 7. 共享词汇，而不是强求命令同名

本项目当前选择的是：

- Feishu 端和本地管理端共享同一组状态词汇
- 但不强求命令名、入口形态、交互方式完全同构

这样做的原因是：

- Feishu 天然是 chat-scoped
- 本地管理 CLI 天然是 service / binding / thread scoped
- `fcodex` wrapper 仍应保持“Codex 使用入口”的边界，不应兼任 Feishu 服务管理 CLI

因此，当前架构里的三种入口分别是：

- 飞书聊天命令：面向当前 chat binding
- `fcodex`：面向 shared backend 的 Codex 使用入口
- `feishu-codexctl`：面向运行中 Feishu 服务的本地管理入口
