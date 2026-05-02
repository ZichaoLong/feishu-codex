# 群聊功能合同

英文原文：`docs/contracts/group-chat-contract.md`

本文定义 `feishu-codex` 当前群聊能力的正式行为合同。

它回答的问题是：

- 新群默认值是什么
- 群激活、群聊工作态、管理员命令分别控制什么
- `assistant` 模式的上下文、历史回捞、话题边界如何工作
- 审批卡片与补充输入卡片在群里由谁处理
- 哪些行为应视为明确保证，哪些只是当前限制

另见：

- `docs/architecture/feishu-codex-design.zh-CN.md`
- `docs/contracts/feishu-thread-lifecycle.zh-CN.md`
- `docs/contracts/feishu-help-navigation.zh-CN.md`
- `docs/verification/group-chat-manual-test-checklist.zh-CN.md`

## 1. 范围

本文只定义群聊能力合同。

它不重新定义：

- 单聊线程生命周期
- `/status`、`/release-runtime` 与本地管理面词汇
- `fcodex` wrapper 语义

这些分别以各自专题文档为准。

## 2. 默认值

- 新群默认工作态是 `assistant`
- 新群默认是**未激活**
- 群聊管理员来自 `system.yaml.admin_open_ids`
- `system.yaml.admin_open_ids` 是权威源；运行时管理员集合只是缓存
- 管理员即使在群未激活时，仍可先使用机器人完成初始化与管理
- 运行时身份判定统一使用 `open_id`
- `user_id` 仅保留在日志与 `/whoami` 里做排障展示
- 若希望 `/whoami` 和日志稳定返回 `user_id`，需要额外开 `contact:user.employee_id:readonly`

## 3. 群激活边界

- 群激活是 **chat 级** 开关，不是成员级 ACL
- 群未激活时，非管理员不能在该群里使用机器人
- 群已激活后，当前群成员与后续新加入成员都可日常使用机器人
- 群激活后，即使管理员之后退群，群仍继续可用；直到管理员重新进群停用，或运维显式清理该群状态
- 群激活状态应跨服务重启保留
- 群激活 / 停用 / 再激活只改变授权状态与元数据，不会自动清空群日志、`assistant` 上下文边界、当前 thread 绑定或群工作态
- 激活与停用只允许管理员执行
- 当前群激活管理入口是：
  - `/group`
  - `/group activate`
  - `/group deactivate`

## 4. 群聊工作态

- 严格群聊显式 mention 判定依赖 `system.yaml.bot_open_id`
- `/bot-status` 与 `/init` 中的实时探测只用于诊断和初始化，不会替代运行时读取的 `system.yaml.bot_open_id`
- 如配置 `system.yaml.trigger_open_ids`，命中这些 `open_id` 的 mentions 也视为有效触发
- `trigger_open_ids` 只扩展“哪些 mentions 算触发”，不绕过群激活，也不替代 `bot_open_id`
- 私聊底层会话按用户隔离；群聊底层会话按 `chat_id` 共享

### 4.1 `assistant`

- 只记录“当前允许使用机器人”的人类成员消息
- 只有被有效 mention 时才回复
- 回复时附带自上次触发边界以来的群上下文
- 主聊天流与每个群话题分别维护上下文边界；主聊天流不会自动读入话题回复，话题也不会自动读入主聊天流
- 虽然上下文边界按主聊天流 / 话题分开，但底层仍是同一个群共享会话；模型可以记住本群其他讨论里已经明确的结论

### 4.2 `mention-only`

- 不缓存群上下文
- 只有被有效 mention 时才触发
- 发给 backend 的内容只包含当前这条群消息，不附带历史上下文
- 当前群消息会以轻量 `group_chat_current_turn` 包装发送，并优先使用 `sender_name`

### 4.3 `all`

- 当前允许使用机器人的人类群消息可直接触发
- 风险最高，容易刷屏
- 发给 backend 时默认等价于单聊直转，不附带群历史上下文，也不额外包一层 `group turn`
- 如果当前绑定 thread 已被其他飞书会话共享，`/group-mode all` 必须拒绝
- 一旦群聊处于 `all` 模式，该 thread 就进入 `all` 模式线程独占规则，不能再被其他飞书会话共享；更精确的 runtime 词汇与拒绝规则见 `docs/contracts/runtime-control-surface.zh-CN.md`

## 5. 群命令与共享状态规则

- 群里的所有 `/` 命令都只给管理员
- 群聊 `assistant` 和 `mention-only` 工作态下，管理员群命令本身也必须先显式 mention 触发对象
- 群聊 `all` 工作态下，管理员可直接发送群命令
- 这里的“群命令”既包括群聊专属命令（如 `/group`、`/group-mode`），也包括在群上下文里触发的通用 Feishu 命令（如 `/status`、`/release-runtime`、`/reset-backend`）
- 群命令不会写入 `assistant` 上下文日志，也不会推进上下文边界
- 会改变共享状态的命令和设置继续严格 admin-only，包括：
  - `/new`
  - `/threads`
  - `/resume`
  - `/release-runtime`
  - `/reset-backend`
  - `/profile`
  - `/approval`
  - `/sandbox`
  - `/permissions`
  - 群激活与群工作态管理命令

## 6. 运行时审批与补充输入

- 群已激活后，普通成员可以日常对话，也可以处理**自己发起 turn** 产生的审批卡片或补充输入卡片
- 管理员始终可以作为兜底处理者操作这些卡片
- 非管理员普通成员不能处理别人发起 turn 的审批或补充输入
- “允许本次” 与 “允许本会话” 这两种运行时审批动作，都属于当前请求发起者可处理的范围
- 这些卡片处理权只作用于当前 turn 的运行时交互，不等于获得共享状态管理权限

## 7. `assistant` 模式上下文合同

- `assistant` 会把“当前允许使用机器人”的群消息写入本地日志
- 群未激活时，非管理员普通消息不会写入 `assistant` 上下文日志
- 只有人类成员的有效触发 mention 会真正触发回复
- 由于飞书不会把其他机器人发言实时推给机器人，`assistant` 会在每次有效触发时按配置回捞最近历史消息
- 历史回捞与实时日志会合并成同一份上下文，而不是两套独立逻辑
- 下一次有效触发时，上下文由两部分组成：
  - 本地实时日志中，上次边界之后到本次触发之前的消息
  - 飞书历史接口返回、但本地日志里尚未出现的缺失消息
- 当前这次真正触发 backend 的群消息，不应和历史上下文混写成一段普通聊天记录；它应作为单独的当前 turn 块发送，并优先使用 `sender_name`
- 若当前 turn 的发送者名字无法解析，才允许在当前 turn 块里回退到 `sender_id` / `open_id` 的短形式
- 本合同中的“上下文”只指文本讨论上下文，不包含附件是否已下载、是否仍可用、是否已被消费这类附件生命周期状态
- 当前附件入口的下载/可用/消费状态由独立 attachment lifecycle 维护，而不是由历史回捞或 `assistant` 上下文日志恢复
- 主聊天流（`chat` 容器）的历史回捞受 `group_history_fetch_limit` 和 `group_history_fetch_lookback_seconds` 限制
- 主聊天流在边界时间附近会向前留一个很小的冗余秒级窗口，再用边界 `message_id` 去重，避免时间窗卡边时漏消息
- `group_history_fetch_limit` 和 `group_history_fetch_lookback_seconds` 同时也是“是否启用任何历史回捞”的总开关；任一项为 `0` 都会关闭主聊天流和话题回捞
- 话题内（`thread` 容器）的历史回捞不承诺严格受 `group_history_fetch_lookback_seconds` 限制；因为飞书公开接口对 `thread` 容器不支持 `start_time/end_time`，本文合同只保证受上下文边界和 `group_history_fetch_limit` 约束
- 话题内优先按 `ByCreateTimeDesc` 倒序回捞，并在到达边界后尽早停止；只有在该排序方式不可用时才回退到升序扫描
- 当时间窗内缺失消息数量超过 `group_history_fetch_limit` 时，本文合同保留“最近的缺失消息”，而不是最早的一批
- 上下文边界同时记录：
  - 本地日志序号 `seq`
  - 边界时间戳 `created_at`
  - 边界时间戳下已消费的 `message_id` 集合
- 记录边界 `message_id` 集合的目的，是避免下一次有效触发时把“与上次边界同毫秒但尚未消费”的缺失消息误判为旧消息而漏掉
- 本文合同保证“不漏掉同毫秒未消费消息”和“不重复同毫秒已消费消息”，但不承诺把不同来源、同毫秒消息恢复成绝对全序
- 如果本次有效触发发生在群话题内，执行卡片、群激活拒绝和过长文本 follow-up 会尽量留在原话题，而不是回到主聊天流

## 8. 拒绝反馈

- 群未激活时，非管理员在 `assistant` / `mention-only` 中显式 mention 触发对象，会收到“请管理员先 `/group activate`”的拒绝提示
- 群未激活时，非管理员在 `all` 中直接发普通消息会静默忽略，以避免刷屏
- 群未激活时，非管理员在 `all` 中显式 mention 触发对象或发群命令时，仍会收到拒绝提示

## 9. 其他机器人与历史消息

- 其他机器人不会直接触发 `feishu-codex`
- 如果群消息历史对机器人可见，其他机器人消息可以通过每次有效触发时的历史回捞进入上下文
- 如果关闭历史回捞，其他机器人消息不会自动进入 `assistant` 上下文

## 10. 明确限制

- 话题内历史回捞不能像主聊天流那样严格按时间窗裁剪；这是飞书公开接口能力限制，不是产品层有意放宽
- `all` 模式天然更容易刷屏；这不是实现 bug，而是该工作态的产品风险
- 群命令与普通群消息虽然共享底层群会话，但群命令不会进入 `assistant` 上下文日志；这是刻意保持的行为边界
- 即使群上下文中出现文件名或文件类占位文本，也不应把它理解成“对应附件当前仍可用”；附件可用性不属于 history recovery 合同
- 本版本刻意不支持细粒度多用户权限系统；群激活是 chat 级共享授权，不做成员级共享状态隔离
