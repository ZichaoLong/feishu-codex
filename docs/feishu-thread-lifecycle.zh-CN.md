# 飞书侧线程生命周期

英文原文：`docs/feishu-thread-lifecycle.md`

本文定义飞书侧当前的线程生命周期合同。它解释了：为什么 Feishu 侧必须遵守和 `fcodex`
相同的 backend 协议合同，但运行时恢复策略不能照搬 `fcodex`。

另见：

- `docs/fcodex-shared-backend-runtime.zh-CN.md`
- `docs/shared-backend-resume-safety.zh-CN.md`
- `docs/session-profile-semantics.zh-CN.md`

## 1. 上游基线

- 上游项目：[`openai/codex`](https://github.com/openai/codex.git)
- 当前本地验证基线：`codex-cli 0.118.0`（2026-04-03）

## 2. 必须严格区分的四个状态

对一个飞书会话而言，下列事实不是一回事：

1. `binding`
   - 这个飞书会话逻辑上当前绑定到哪个 `thread_id`
2. `subscription`
   - 当前 live 连接是否仍在订阅这个 thread
3. `loaded runtime`
   - 这个 thread 当前是否仍加载在 app-server 内存里
4. `running turn`
   - 当前是否有 turn 正在执行

飞书侧以 `binding` 作为“这个会话当前接着哪个线程继续聊”的事实来源。
`loaded runtime` 只是一个可恢复的运行态事实，不是绑定事实。

## 3. 为什么飞书侧不能照搬 `fcodex`

`fcodex` 在正常使用时，通常会维持一个持续存在的 remote TUI 会话。因此：

- websocket 连接通常持续存在
- 当前 thread 往往持续处于订阅状态
- thread 往往也持续保持 loaded

飞书侧不是这样：

- 飞书用户并不持有一个长寿命 TUI 进程
- service 侧 remote 连接可能独立于聊天窗口而中断
- 某个飞书会话明明还应继续同一个 thread，但这个 thread 的 runtime 可能已经被 unload

所以，飞书侧必须在 runtime 丢失后继续保留线程绑定，并在需要时按绑定去恢复 runtime。

## 4. 飞书侧状态图

```mermaid
flowchart TD
    A[未绑定会话] -->|首次提问 或 /new| B[已绑定 thread, loaded, idle]
    A -->|/resume <thread>| B

    B -->|发送 prompt| C[已绑定 thread, loaded, running]
    C -->|turn completed / idle status / thread closed| B

    B -->|unsubscribe、连接断开、最后订阅者离开| D[已绑定 thread, unloaded]
    C -->|连接断开或漏掉终态通知| D

    D -->|下一条消息 turn/start 直接成功| C
    D -->|下一条消息 turn/start 返回 thread not found -> thread/resume -> retry| C

    B -->|/new 或 /resume 另一个线程| A
    D -->|/new 或 /resume 另一个线程| A
```

## 5. 运行时恢复规则

### 5.1 unload 不等于解绑

如果 app-server 因“最后一个订阅者离开”而 unload 某个 thread，飞书侧仍必须保留：

- `current_thread_id`
- `current_thread_name`
- 这个飞书会话当前目录等本地状态

不能把 `thread/closed` 或 `turn/start -> thread not found` 直接当成“这个会话不再绑定任何线程”的证据。

### 5.2 `thread/closed` 只表示 runtime 结束

上游 `thread/closed` 的语义，是 thread 已从 app-server 内存中卸下。
它不表示持久化 rollout 已消失。只要 rollout 还在，后续仍可 `thread/resume`。

### 5.3 下一条消息负责重新 load runtime

当某个飞书会话已经绑定了 `thread_id` 时：

1. 先尝试 `turn/start`
2. 如果 app-server 返回 `thread not found`
3. 则调用 `thread/resume`
4. 然后重试一次 `turn/start`

这条规则遵守的是上游协议合同；区别只在于飞书侧更经常进入“已绑定但已 unload”的状态。

### 5.4 在线通知是执行中的主真相源

只要飞书侧当前仍订阅着这个 thread，它就会依赖 live notification 获取：

- 流式回复 delta
- 命令/文件修改日志
- 审批请求
- 各类终态事件

`thread/read` 在飞书侧只承担“快照补账”职责，不承担“宣告运行态已失联”的职责。
因此飞书侧的规则是：

- 运行中的执行卡片，优先相信 live notification
- `thread/read` 只用于补齐最终回复、补收口、确认 thread 是否已经不再 active
- 一次 `thread/read` timeout 或 transport error，只能把运行通道标记为临时降级，不能清空当前执行锚点

但终态通知可能因为断连、接管、时序问题而漏掉。因此飞书侧仍需要在这些场景主动做 `thread/read` 对账：

- 收到终态信号时
- 收到 `thread/closed` 时
- 执行卡片长时间没有运行时事件时，由 watchdog 主动对账

### 5.5 执行卡片锚点合同

对同一个飞书会话，任一时刻最多只允许一张“当前执行卡片”：

- 当前执行卡片由 `prompt_message_id`、`card_message_id`、`turn_id` 共同锚定
- live delta、终态通知、watchdog 补账都只能更新这张当前执行卡片
- 当执行结束后，这张卡片会被收口并退出“当前执行锚点”
- 后续新的本地 prompt 或新的外部 turn，才允许创建下一张执行卡片

因此，`thread/read` 软失败不能导致“先把当前卡片判死、清空锚点，再被后续事件新开一张卡片”。

## 6. 与 `fcodex` 的关系

`fcodex` 与飞书侧仍共享同一套 backend 合同：

- 同一个 shared app-server
- 同一套持久化 thread id
- 同样的 `thread/resume` / `turn/start` 语义

不同的只是前端运行时模型：

- `fcodex` 在 TUI 存活期间，通常一直附着在 live backend 上
- 飞书侧更容易进入“绑定还在，但 runtime 已被 unload”的状态

因此最准确的说法是：

- 协议合同相同
- 前端恢复策略不同

## 7. 当前实现合同

当前飞书侧实现应满足：

- 一个飞书会话只维护一个逻辑上的当前 thread 绑定
- 群聊按 `chat_id` 共享绑定
- runtime 丢失不会自动清空绑定
- `thread/read` timeout/transport error 只会标记运行通道降级，不会直接宣告“当前运行态已失联”
- 同一飞书会话同一时刻最多只有一张活动执行卡
- `/new` 与 `/resume` 才是显式改绑操作
- 如果 runtime 已丢失，下一条消息会根据已绑定的 `thread_id` 自动恢复
- `thread/closed` 被视为 runtime 状态迁移，而不是逻辑解绑

## 8. 相关实现文件

- `bot/codex_handler.py`
- `bot/adapters/codex_app_server.py`
- `bot/fcodex.py`
- `bot/fcodex_proxy.py`
- `docs/fcodex-shared-backend-runtime.zh-CN.md`
- `docs/shared-backend-resume-safety.zh-CN.md`
