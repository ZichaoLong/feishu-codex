# 群聊功能合同

英文原文：`docs/contracts/group-chat-contract.md`

本文定义 `feishu-codex` 当前群聊能力的正式行为合同。

它回答的问题是：

- 新群默认值是什么
- ACL、群聊工作态、管理员命令分别控制什么
- `assistant` 模式的上下文、历史回捞、话题边界如何工作
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
- `/status`、`/release-feishu-runtime` 与本地管理面词汇
- `fcodex` wrapper 语义

这些分别以各自专题文档为准。

## 2. 默认值

- 新群默认工作态是 `assistant`
- 新群默认 ACL 是 `admin-only`
- 群聊管理员来自 `system.yaml.admin_open_ids`
- `system.yaml.admin_open_ids` 是权威源；运行时管理员集合只是缓存
- 运行时身份判定统一使用 `open_id`
- `user_id` 仅保留在日志与 `/whoami` 里做排障展示
- 若希望 `/whoami` 和日志稳定返回 `user_id`，需要额外开 `contact:user.employee_id:readonly`

## 3. 人类成员权限

- 人类成员是否具备某个群里的触发资格，由该群 ACL 决定
- ACL 只决定“谁有资格”，是否还需要显式 mention 由群工作态决定
- 群 ACL 只管理人类成员，不管理其他机器人
- 当前支持的 ACL 策略包括：
  - `admin-only`
  - `allowlist`
  - `all-members`

## 4. 群聊工作态

- 严格群聊显式 mention 判定依赖 `system.yaml.bot_open_id`
- `/whoareyou` 与 `/init` 中的实时探测只用于诊断和初始化，不会替代运行时读取的 `system.yaml.bot_open_id`
- 如配置 `system.yaml.trigger_open_ids`，命中这些 `open_id` 的 mentions 也视为有效触发
- `trigger_open_ids` 只扩展“哪些 mentions 算触发”，不绕过 ACL，也不替代 `bot_open_id`
- 私聊底层会话按用户隔离；群聊底层会话按 `chat_id` 共享

### 4.1 `assistant`

- 接收并缓存群里消息
- 只有被有效 mention 时才回复
- 回复时附带自上次触发边界以来的群上下文
- 主聊天流与每个群话题分别维护上下文边界；主聊天流不会自动读入话题回复，话题也不会自动读入主聊天流
- 虽然上下文边界按主聊天流 / 话题分开，但底层仍是同一个群共享会话；模型可以记住本群其他讨论里已经明确的结论

### 4.2 `mention-only`

- 不缓存群上下文
- 只有被有效 mention 时才触发

### 4.3 `all`

- 人类群消息可直接触发
- 风险最高，容易刷屏

## 5. 群命令触发规则

- 私聊命令可直接发送
- 群里的所有 `/` 命令都只给管理员
- 群聊 `assistant` 和 `mention-only` 工作态下，管理员群命令本身也必须先显式 mention 触发对象
- 群聊 `all` 工作态下，管理员可直接发送群命令
- 群命令不会写入 `assistant` 上下文日志，也不会推进上下文边界

## 6. `assistant` 模式上下文合同

- `assistant` 会把群消息写入本地日志
- 只有人类成员的有效触发 mention 会真正触发回复
- 由于飞书不会把其他机器人发言实时推给机器人，`assistant` 会在每次有效触发时按配置回捞最近历史消息
- 历史回捞与实时日志会合并成同一份上下文，而不是两套独立逻辑
- 下一次有效触发时，上下文由两部分组成：
  - 本地实时日志中，上次边界之后到本次触发之前的消息
  - 飞书历史接口返回、但本地日志里尚未出现的缺失消息
- 主聊天流（`chat` 容器）的历史回捞受 `group_history_fetch_limit` 和 `group_history_fetch_lookback_seconds` 限制
- 主聊天流在边界时间附近会向前留一个很小的冗余秒级窗口，再用边界 `message_id` 去重，避免时间窗卡边时漏消息
- `group_history_fetch_limit` 和 `group_history_fetch_lookback_seconds` 同时也是“是否启用任何历史回捞”的总开关；任一项为 `0` 都会关闭主聊天流和话题回捞
- 话题内（`thread` 容器）的历史回捞当前不承诺严格受 `group_history_fetch_lookback_seconds` 限制；因为飞书公开接口对 `thread` 容器不支持 `start_time/end_time`，当前实现只保证受上下文边界和 `group_history_fetch_limit` 约束
- 话题内优先按 `ByCreateTimeDesc` 倒序回捞，并在到达边界后尽早停止；只有在该排序方式不可用时才回退到升序扫描
- 当时间窗内缺失消息数量超过 `group_history_fetch_limit` 时，当前实现保留“最近的缺失消息”，而不是最早的一批
- 上下文边界同时记录：
  - 本地日志序号 `seq`
  - 边界时间戳 `created_at`
  - 边界时间戳下已消费的 `message_id` 集合
- 记录边界 `message_id` 集合的目的，是避免下一次有效触发时把“与上次边界同毫秒但尚未消费”的缺失消息误判为旧消息而漏掉
- 当前实现保证“不漏掉同毫秒未消费消息”和“不重复同毫秒已消费消息”，但不承诺把不同来源、同毫秒消息恢复成绝对全序
- 如果本次有效触发发生在群话题内，执行卡片、ACL 拒绝和过长文本 follow-up 会尽量留在原话题，而不是回到主聊天流

## 7. ACL 拒绝反馈

- 未获授权成员在 `assistant` / `mention-only` 中显式 mention 触发对象时，会收到拒绝提示
- 未获授权成员在 `all` 中直接发普通消息会静默忽略，以避免刷屏
- 未获授权成员在 `all` 中显式 mention 触发对象或发群命令时，仍会收到拒绝提示

## 8. 其他机器人与历史消息

- 其他机器人不会直接触发 `feishu-codex`
- 如果群消息历史对机器人可见，其他机器人消息可以通过每次有效触发时的历史回捞进入上下文
- 如果关闭历史回捞，其他机器人消息不会自动进入 `assistant` 上下文

## 9. 当前限制

- 话题内历史回捞当前不能像主聊天流那样严格按时间窗裁剪；这是飞书公开接口能力限制，不是当前实现故意放宽
- `all` 模式天然更容易刷屏；这不是实现 bug，而是该工作态的产品风险
- 群命令与普通群消息虽然共享底层群会话，但群命令不会进入 `assistant` 上下文日志；这是刻意保持的行为边界
