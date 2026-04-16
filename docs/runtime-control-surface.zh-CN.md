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

## 3. Runtime 持有、Owner 租约与状态迁移

### 3.1 `attached/released` 不是 owner lease

`feishu runtime` 只回答一件事：

- 当前运行中的 `feishu-codex` 服务，是否仍对该 thread 保持 runtime residency

它不回答：

- 当前哪个 Feishu binding 可以发起下一轮 turn
- 当前哪个前端可以处理审批 / 补充输入 / 中断

这两类问题属于独立的 lease 事实。

### 3.2 三种事实的对照

| 事实 | 作用范围 | 它回答的问题 | 在 `feishu runtime == released` 时还能存在吗？ |
| --- | --- | --- | --- |
| `feishu runtime` = `attached/released` | Feishu 服务连接 | 当前运行中的 Feishu 服务是否仍附着该 thread？ | 这是它本身的状态轴 |
| `Feishu 写入 owner` | 仅 Feishu 内部 | 当前哪个 Feishu binding 可以向共享 thread 写入？ | 不应再保留有意义的 Feishu 写入 owner |
| `交互 owner` | 跨前端（`feishu-codex` + `fcodex`） | 当前谁可以处理中断、审批、补充输入？ | 可以。外部 owner（如 `fcodex`）仍可能存在 |

直接后果是：

- `attached + 无 owner` 是完全合法的 idle 稳态
- `released + 外部交互 owner` 也完全合法，表示别的前端仍在持有 live thread

### 3.3 几个必须明确接受的有效组合

### `bound + attached + idle + 无 owner`

表示当前 chat 仍指向该 thread，Feishu 仍附着，但没有正在运行的 turn owner。
这是 turn 结束后的正常 idle 稳态。

### `bound + attached + active + 当前 binding 是 owner`

表示当前 binding 既持有 Feishu 写入租约，也持有跨前端交互租约，正在执行这一轮 turn。

### `bound + released + notLoaded`

表示：

- 飞书侧逻辑绑定仍保留
- Feishu 服务已不再持有该 thread 的 runtime
- 当前 backend 里 thread 也已 unload

这是最典型的“可以重新切 profile 再恢复”的状态。

### `bound + released + idle/active`

表示：

- Feishu 自己已经释放 runtime
- 但当前 backend 中还有其他订阅者仍附着该 thread
- 最常见的是本地 `fcodex`

因此，`released` 不保证 backend 一定 `notLoaded`。

### 3.4 正式状态转移表

下表是 Feishu 侧状态迁移的权威合同。

| 当前 binding | 当前 `feishu runtime` | 当前 backend | 事件 | 守卫条件 | 下一 binding | 下一 `feishu runtime` | 下一 backend | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `unbound` | `not-applicable` | `not-applicable` | 普通 prompt 或 `/new` | 被接受 | `bound` | `attached` | `idle` 或 `active` | 先建新 thread，再启动或准备 turn |
| `unbound` | `not-applicable` | 任意 | `/resume <thread>` | 目标解析成功且允许恢复 | `bound` | `attached` | 通常为 `idle` | 当前 chat 绑定切到目标 thread |
| `bound` | `attached` | `idle` | 普通 prompt | prompt preflight 通过 | `bound` | `attached` | `active` | 获取 Feishu 写入 owner 和交互 owner |
| `bound` | `attached` | `active` | turn 终态事件 | 无 | `bound` | `attached` | 通常为 `idle` | 清理 owner lease；binding 与附着保持不变 |
| `bound` | `attached` | `idle` 或 `active` | `/release-feishu-runtime` | 当前没有 Feishu 侧运行中的 turn，且没有待处理审批 / 输入 | `bound` | `released` | `notLoaded`、`idle` 或 `active` | release 释放的是整个运行中 Feishu 服务对该 thread 的 runtime 持有 |
| `bound` | `released` | `notLoaded` 或 `idle` | 普通 prompt | prompt preflight 通过 | `bound` | `attached` | `active` | 先重新附着 / resume，再启动 turn |
| `bound` | `released` | 任意 | 普通 prompt | prompt preflight 被拒绝 | 不变 | 不变 | 不变 | 纯拒绝：不得 resume，不得新增 subscriber，不得把 `released` 改成 `attached` |
| `bound` | `attached` 或 `released` | 任意 | `/new` 或 `/resume <other>` | 被接受 | 绑定到另一 thread | `attached` | 通常为 `idle` | 当前 binding 切换到新目标 |
| `bound` | `attached` 或 `released` | 任意 | 显式清空 / 归档当前 binding / chat unavailable 清理 | 被接受 | `unbound` | `not-applicable` | 对该 Feishu binding 来说为 `not-applicable` | 清理 Feishu 侧 binding 以及本地执行锚点 |

### 3.5 不允许含糊的规则

- `all` 模式独占是按“当前 thread 上的 Feishu runtime 占用”判断，不按一个仅被记住的 `bound + released` bookmark 判断。
- 被拒绝的 prompt 必须是 pure reject。
  它不能调用 `thread/resume`，不能新增 Feishu subscriber，也不能把
  `feishu runtime` 从 `released` 改成 `attached`。
- `release-feishu-runtime` 释放的是 Feishu 的 runtime residency 和 Feishu 本地 owner lease；
  它不会抹掉 chat 仍指向哪个 thread 的 binding bookmark。

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

如果某个 Feishu binding 当前仍 `bound`，但其 `feishu runtime == released`，那么之后在这个 chat 里直接发送普通消息时，会先执行正常的 prompt preflight。

1. 如果 prompt 被拒绝，这次拒绝必须是 pure reject，binding 继续保持 `released`。
2. 只有在 prompt 被接受时，Feishu 才会按当前绑定的 `thread_id` 重新附着 / resume，然后启动 turn。

如果当时该 thread 已 `notLoaded`，这条“已通过 preflight 的重新附着路径”会遵守
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

### 6.3 当前实现提供的第一批命令

当前实现提供：

- `feishu-codexctl service status`
- `feishu-codexctl binding list`
- `feishu-codexctl binding status <binding_id>`
- `feishu-codexctl thread status (--thread-id <id> | --thread-name <name>)`
- `feishu-codexctl thread bindings (--thread-id <id> | --thread-name <name>)`
- `feishu-codexctl thread release-feishu-runtime (--thread-id <id> | --thread-name <name>)`

### 6.4 `binding` 持久化与重置的目标合同

`binding` 默认是跨重启保留的本地事实。

这样设计的原因是：

- 飞书会话重启后，仍应记住“下一次默认接着哪个 thread 继续”
- 这属于 Feishu 集成层自己的 bookmark，不属于 Codex thread 元数据

但“清空一个或全部 binding”也是一个合理的本地管理需求，尤其适用于：

- 开发期批量重置
- 状态救火
- 让飞书侧整体回到“重新选择 thread”的初始状态

这类动作的目标合同是：

- 它属于 `binding` 层，不属于 `thread runtime` 层
- 它不等于 `/release-feishu-runtime`
- 它的正式入口应属于本地管理面 `feishu-codexctl`

因此，目标控制面应收敛到：

- `feishu-codexctl binding clear <binding_id>`
- `feishu-codexctl binding clear-all`

这两个动作的语义是：

- 清除 Feishu 侧记住的 binding 事实
- 同步清理运行中的 Feishu 内存态与持久化态
- 必要时释放该 binding 相关的 Feishu 本地 lease / 执行锚点 / 订阅关系

它们不承诺：

- 删除 Codex thread
- archive thread
- 替代 `/release-feishu-runtime`
- 替代 thread 级管理命令

明确区分：

- `/release-feishu-runtime`
  - 释放的是 Feishu 对某个 thread 的 runtime 持有
  - 不清空 binding
- `binding clear/clear-all`
  - 清掉的是 Feishu 本地 bookmark
  - 不应再作为一个“单独删 `chat_bindings.json` 文件”的独立架构概念存在

### 6.5 `binding_id` 形状

本地管理 CLI 使用稳定的 admin-facing `binding_id`：

- 群聊共享 binding：`group:<chat_id>`
- 私聊 binding：`p2p:<sender_id>:<chat_id>`

它们只服务于本地管理面，不需要和飞书聊天命令名字保持对称。
`binding_id` 在本项目里被定义为受限的管理语法，而不是任意字符串的通用可逆编码：

- `:` 是保留分隔符
- `sender_id` 和 `chat_id` 组件不允许包含 `:`
- 如果未来发现真实上游 ID 可能包含 `:`，应整体替换这套语法，而不是继续依赖当前拼接格式做静默 round-trip

### 6.6 显式 thread 目标合同

对本地管理面而言，thread 目标必须显式表达，不再靠输入内容推断。

- `--thread-id <id>`
  - 表示按 thread id 精确寻址
  - 不会再回退到名字解析
- `--thread-name <name>`
  - 表示按 thread name 精确匹配
  - 过滤规则与 session 发现面使用的共享跨 provider 全局列表一致
  - 会继续扫描后续分页，直到能证明唯一命中或存在歧义
  - 0 个匹配时报错
  - 多个精确同名匹配时报错

控制面 RPC 也遵守同一规则。
它不再接受一个未标注类型的 union `target` 字段，让 service 自己猜是 id 还是 name。

### 6.7 每个 `FC_DATA_DIR` 只允许一个 service owner

对同一个 `FC_DATA_DIR`，只允许一个正在运行的 `feishu-codex` service 实例持有所有权。

合同是：

- 所有权必须先于 adapter / control plane 启动建立
- 第二个实例必须 fail-fast
- control socket 不是所有权原语
- owner 会写入包含 `owner_pid`、`owner_token`、`socket_path` 的元数据
- 如果在拿到 owner 之后启动失败，所有已部分启动的 runtime 组件都必须先完整回滚，再释放 lease
- 停止时只允许清理由同一个 owner token 仍持有的 metadata / socket

因此，`python -m bot` 直接运行和 systemd 管理的 service，不允许在同一个
`FC_DATA_DIR` 上并存。
如果二者指向同一目录，后启动的一方必须直接退出，而不是尝试抢 socket。

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
