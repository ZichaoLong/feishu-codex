# 群聊功能手测清单

本文档用于后续手工验证 `feishu-codex` 当前已实现的群聊相关能力。

## 1. 测试目标

- 验证三种群聊工作态：`assistant`、`mention-only`、`all`
- 验证群激活边界：未激活、激活、停用
- 验证群命令触发规则
- 验证群共享会话与话题软隔离
- 验证 `assistant` 模式的上下文日志、boundary 与按次历史回捞
- 验证审批卡片 / 补充输入卡片的“发起者本人或管理员”处理边界
- 验证其他机器人消息可否通过历史回捞进入上下文
- 验证外部卡片消息的降级处理边界
- 验证重启后的持久化状态

## 2. 测试角色

- `Admin`：已写入 `system.yaml.admin_open_ids` 的管理员
- `MemberA`：普通成员
- `MemberB`：普通成员
- `MemberC`：可选；用于验证“激活后新加入成员也可用”
- `OtherBot`：可选；用于验证“其他机器人消息只能通过历史回捞进入上下文”

## 3. 测试前准备

1. 确认服务已启动，且日志可跟踪：
   `journalctl --user -u feishu-codex -f`
2. 确认应用权限至少包含：
   `im:message.p2p_msg:readonly`、`im:message.group_at_msg:readonly`、`im:message.group_msg`、`im:message`、`im:message:readonly`、`im:message:send_as_bot`、`im:message:update`
   如需让 `/whoami`、群授权卡片、群上下文里显示可读名字，再补 `contact:contact.base:readonly`、`contact:user.base:readonly`
   如需让 `/whoami` 和日志里稳定看到 `user_id`，再补 `contact:user.employee_id:readonly`；缺少时 `user_id` 允许为空
   如需用 `/whoareyou` 实时探测机器人 `open_id`，再补 `application:application:self_manage`
3. 确认事件与回调已启用：
   `im.message.receive_v1`、`card.action.trigger`
4. 让 `Admin` 私聊机器人执行 `/whoami`，确认已把正确的 `open_id` 写入 `system.yaml.admin_open_ids`
5. 让 `Admin` 私聊机器人执行 `/whoareyou`，确认返回里包含 `configured bot_open_id`、`discovered open_id`，并把需要启用的值写入 `system.yaml.bot_open_id`
6. 如需验证“别人 @我本人时由机器人代答”，再把对应成员的 `open_id` 写入 `system.yaml.trigger_open_ids`
7. 准备一个新群，拉入 `Admin`、`MemberA`、`MemberB`、`feishu-codex` 机器人
8. 如需验证其他机器人历史消息路径，再把 `OtherBot` 拉入群
9. 如需验证历史回捞，请确认飞书侧已开启“群消息历史可见”或等价配置

## 4. 私聊基础检查

1. `Admin` 私聊发送 `/whoami`。预期：返回 `name`、`open_id`，以及仅用于排障的 `user_id`；若未开 `contact:user.employee_id:readonly`，`user_id` 允许为空。若未开通讯录权限，`name` 允许退化成 open_id 前缀。
2. `Admin` 私聊发送 `/debug-contact <open_id>`。预期：能看到 cache 命中情况、live resolved name，以及 fallback 原因 / API 错误；若未开通讯录权限，应能明确看到 fallback。
3. `Admin` 私聊发送 `/help group`。预期：帮助文本提到 `assistant`、`mention-only`、`all`、`/group`、`/groupmode`，且不再提旧群授权命令。
4. `MemberA` 私聊发送普通文本。预期：被拒绝，并提示“如需协作使用，请让管理员在群里先执行 `/group activate`”。

## 5. 新群默认值

1. 把机器人拉入一个全新群，不做任何额外配置。
2. `Admin` 在群里发送普通文本，不 `@机器人`。预期：不响应。
3. `Admin` 在群里发送 `@机器人 你好`。预期：正常响应；管理员可在未激活群里先完成初始化和管理。
4. `MemberA` 在群里发送 `@机器人 你好`。预期：收到“请管理员先 `/group activate`”的拒绝提示。
5. `Admin` 在群里发送 `@机器人 /groupmode`。预期：显示当前工作态卡片，默认值为 `assistant`。
6. `Admin` 在群里发送 `@机器人 /group`。预期：显示当前群授权卡片，默认值为“未激活”。
7. 如已配置 `trigger_open_ids`，让 `MemberA` 发送 `@Alias 你好`。预期：若群未激活，则仍收到拒绝提示；说明 alias mention 仍受群激活边界约束。

## 6. 群命令触发规则

1. 在默认 `assistant` 模式下，`Admin` 直接发送 `/group`。预期：不生效，因未 `@机器人`。
2. 在默认 `assistant` 模式下，`Admin` 发送 `@机器人 /group`。预期：正常显示群授权卡片。
3. 切到 `mention-only` 后，重复上一步。预期：仍然必须 `@机器人` 才生效。
4. 切到 `all` 后，`Admin` 直接发送 `/group`。预期：可直接生效。
5. 切到 `all` 后，让 `MemberA` 直接发送 `/group` 或 `/new`。预期：收到拒绝提示，因为群里的所有 `/` 命令都只给管理员。

## 7. 群激活行为

1. 保持 `assistant + 未激活`，让 `MemberA` 发送 `@机器人 你好`。预期：收到拒绝提示。
2. `Admin` 发送 `@机器人 /group activate`。预期：激活成功。
3. `MemberA` 发送 `@机器人 你好`。预期：正常响应。
4. `MemberB` 发送 `@机器人 你好`。预期：正常响应。
5. 如方便，激活后再把 `MemberC` 拉入群。让 `MemberC` 发送 `@机器人 你好`。预期：无需额外授权即可正常响应。
6. `Admin` 发送 `@机器人 /group deactivate`。预期：停用成功。
7. `MemberA` 再次发送 `@机器人 你好` 或 `/status`。预期：再次收到拒绝提示。
8. 可选：重新激活后让 `Admin` 退群。`MemberA` 再次 `@机器人 你好`。预期：群继续可用；但群里的共享状态命令仍无法由普通成员执行。

## 8. 三种工作态

前置：先执行 `/group activate`。

1. `mention-only`：让 `MemberB` 连发两条普通消息，再发 `@机器人 请总结`。预期：前两条不会进入上下文，回复仅基于当前提问。
2. `assistant`：让 `MemberB` 连发两条普通消息，再发 `@机器人 请总结`。预期：回复会基于这两条上下文。
3. `all`：让 `MemberB` 直接发送普通文本。预期：机器人直接响应，无需 `@`。
4. `all`：让 `OtherBot` 直接发送普通文本或 `@机器人`。预期：不会直接触发。
5. 如已配置 `trigger_open_ids`：让 `MemberB` 发送 `@Alias 请总结`。预期：在 `assistant` / `mention-only` 下可等价触发；在 `all` 下群成员仍可直接发消息。

## 9. 审批卡片与补充输入

前置：群已激活，且当前 `permissions` / `approval` 设置会产生运行时审批。

1. 让 `MemberA` 发起一个需要审批的请求。预期：出现审批卡片。
2. 让 `MemberB` 点击 `MemberA` 这张审批卡片上的“允许本次”或“允许本会话”。预期：被拒绝，并提示仅当前提问者或管理员可操作。
3. 让 `MemberA` 自己点击“允许本次”。预期：审批通过，请求继续执行。
4. 再制造一次需要审批的请求。让 `Admin` 点击卡片。预期：管理员也可兜底处理。
5. 如某条 turn 触发了补充输入卡片，让非发起者普通成员填写并提交。预期：被拒绝。
6. 让当前发起者本人填写并提交。预期：成功继续执行。

## 10. assistant 上下文、boundary 与历史回捞

前置：群已激活，工作态为 `assistant`。

1. 在第一次 `@机器人` 前，先发送若干条普通群消息。
2. `MemberA` 第一次有效发送 `@机器人 请总结之前讨论`。预期：
   - 先出现一张“准备群聊上下文”的执行卡片
   - 最终回复包含前面的普通群消息
   - 这次触发后 boundary 前移
3. 再依次发送：
   `MemberB: 第三条讨论`
   `MemberA: @机器人 再总结`
   预期：本轮只基于上次 boundary 之后的新消息，至少包含第三条讨论。
4. 在两次 `@` 之间插入 `@机器人 /status` 或 `@机器人 /groupmode`。
   预期：这些群命令不进入上下文，也不切断 boundary；下一次真正对话触发仍能看到命令前后的普通群消息。
5. 若群里存在 `OtherBot`，让它在两次人类 `@` 之间发一条普通消息，再由人类 `@机器人`。
   预期：`OtherBot` 的消息不会实时触发，但在下一次有效人类 `@` 时会通过历史回捞进入上下文。
6. 将 `group_history_fetch_limit: 0` 或 `group_history_fetch_lookback_seconds: 0` 后重启服务。
7. 重新让 `OtherBot` 在两次人类 `@` 之间发言。预期：机器人仍不会被 `OtherBot` 直接触发，且所有历史回捞路径都会关闭，这条消息不再自动进入上下文。
8. 在两次有效 `@` 之间制造超过 `group_history_fetch_limit` 的缺失消息。预期：下一次回复中优先保留最近缺失消息，而不是最早的一批。
9. 如有脚本化测试条件，制造“与上次 boundary 同毫秒、但上次未消费”的缺失消息。预期：下一次回复不会漏掉这条消息，也不会重复带入上次已经消费过的同毫秒消息。
10. 主聊天流先发一条普通消息；再在某个话题里发一条普通消息；随后在主聊天流 `@机器人`。预期：回复只看主聊天流消息，不把该话题内容自动带进本轮上下文。
11. 在同一个话题里继续发消息并 `@机器人`。预期：回复只看该话题上下文；执行卡片、群激活拒绝和长回复 follow-up 都尽量留在这个话题里，而不是跳回主聊天流。
12. 让 `MemberA` 与 `MemberB` 在同一个群里先后各触发一轮对话。预期：不会因为换了提问人而切成两个隔离的群后端会话；机器人仍表现为同一个群共享助手。

## 11. 其他机器人与事件边界

1. 让 `OtherBot` 在群里单独发消息。预期：`feishu-codex` 不会即时回复。
2. 让 `OtherBot` 在群里发 `@feishu-codex`。预期：仍不会即时触发。
3. 让人类随后 `@机器人` 请求总结。预期：若历史回捞开启，`OtherBot` 的消息可被带入上下文。
4. 观察日志。预期：不会出现“其他机器人直接触发本轮回复”的实时事件链路。

## 12. 外部卡片消息

1. 让人类成员或其他机器人发送一张普通 `interactive` 卡片，卡片中包含清晰文本，但不 `@feishu-codex`。
   预期：如果当前模式允许接收，这条消息会被降级成文本进入处理链路；否则仅作为上下文或被忽略。
2. 让人类成员发送一张包含文本且 `@feishu-codex` 的卡片消息。
   预期：若飞书事件里携带正确 `mentions` 元数据，则会按人类成员的群激活状态和当前工作态正常判断是否触发。
3. 让 `OtherBot` 发送一张包含文本且 `@feishu-codex` 的卡片消息。
   预期：不会直接触发；若历史回捞开启，可在后续人类有效 `@` 时进入上下文。
4. 让人类成员发送一张只有 `@feishu-codex`、没有正文文本的卡片。
   预期：不会变成正常 prompt；当前更接近“空文本”路径。
5. 点击别人或别的机器人发来的卡片按钮。
   预期：`feishu-codex` 不会代为点击或操控该卡片；当前只支持自己发出的卡片点击回调。

## 13. 持久化与重启

1. 先在某群设置非默认工作态并激活该群。
2. 重启服务：
   `feishu-codex restart`
3. 重新验证：
   `@机器人 /groupmode`
   `@机器人 /group`
   预期：群工作态和群激活状态都保留。
4. 如果此前已经产生 `assistant` 上下文，再次人类 `@机器人`。预期：上下文边界仍可继续工作，不会整段重置。

## 14. 日志与可观测性

1. 群聊发送一条普通文本。预期：日志里可看到 `name/open_id/user_id/chat_type/msg_type/message_id`；若未开 `contact:user.employee_id:readonly`，`user_id` 允许为 `-`。
2. 发送一张外部卡片。预期：日志里 `msg_type=interactive`。
3. `assistant` 模式下有效 `@` 时观察日志。预期：能看到历史回捞成功或失败日志。
4. 让 `OtherBot` 发言后再由人类 `@机器人`。预期：日志里能看到这次人类触发前的上下文准备过程，但不会出现“其他机器人直接触发成功”的记录。

## 15. 回归重点

- 默认新群是否仍为 `assistant + 未激活`
- 非管理员私聊是否仍被拒绝
- `assistant` 下管理员群命令是否仍必须 `@`
- 群里的所有 `/` 命令是否仍只给管理员
- `/group activate` 后当前成员和新加入成员是否都可用
- 审批卡片与补充输入是否仍由“当前发起者本人或管理员”处理
- `all` 下未激活群的普通消息是否仍静默忽略
- 其他机器人是否仍不能直接触发
- `assistant` 是否会在每次有效人类 `@` 时补历史消息
- 当缺失消息超过 `group_history_fetch_limit` 时，是否优先保留最近缺失消息
- 同毫秒 boundary 场景下，是否仍不漏掉未消费缺失消息
- 其他机器人消息是否只能通过历史回捞进入上下文
- 群命令是否仍不推进上下文 boundary
- 主聊天流与话题上下文是否仍按 scope 隔离
- 话题内触发后的回复是否仍留在原话题
- 重启后群工作态与群激活状态是否仍保留
