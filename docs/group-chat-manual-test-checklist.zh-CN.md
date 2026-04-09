# 群聊功能手测清单

本文档用于后续手工验证 `feishu-codex` 当前已实现的群聊相关能力。

## 1. 测试目标

- 验证三种群聊工作态：`assistant`、`mention-only`、`all`
- 验证群 ACL：`admin-only`、`allowlist`、`all-members`
- 验证群命令触发规则
- 验证群共享会话与话题软隔离
- 验证 `assistant` 模式的上下文日志、boundary 与按次历史回捞
- 验证其他机器人消息可否通过历史回捞进入上下文
- 验证外部卡片消息的降级处理边界
- 验证重启后的持久化状态

## 2. 测试角色

- `Admin`：已写入 `system.yaml.admin_open_ids` 的管理员
- `MemberA`：普通成员，初始未授权
- `MemberB`：普通成员，后续用于 `allowlist`
- `OtherBot`：可选；用于验证“其他机器人消息只能通过历史回捞进入上下文”

## 3. 测试前准备

1. 确认服务已启动，且日志可跟踪：
   `journalctl --user -u feishu-codex -f`
2. 确认应用权限至少包含：
   `im:message.group_at_msg:readonly`、`im:message.group_msg`、`im:message`、`im:message:readonly`、`im:message:send_as_bot`、`im:message:update`
   如需用 `/whoareyou` 实时探测机器人 `open_id`，再补 `application:application:self_manage`
3. 确认事件与回调已启用：
   `im.message.receive_v1`、`card.action.trigger`
4. 让 `Admin` 私聊机器人执行 `/whoami`，确认已把正确的 `open_id` 写入 `system.yaml.admin_open_ids`
5. 让 `Admin` 私聊机器人执行 `/whoareyou`，把返回的机器人 `open_id` 写入 `system.yaml.bot_open_id`
6. 如需验证“别人 @我本人时由机器人代答”，再把对应成员的 `open_id` 写入 `system.yaml.trigger_open_ids`
7. 准备一个新群，拉入 `Admin`、`MemberA`、`MemberB`、`feishu-codex` 机器人
8. 如需验证其他机器人历史消息路径，再把 `OtherBot` 拉入群
9. 如需验证历史回捞，请确认飞书侧已开启“群消息历史可见”或等价配置

## 4. 私聊基础检查

1. `Admin` 私聊发送 `/whoami`。预期：返回 `name`、`user_id`、`open_id`，并提示管理员配置使用 `open_id`。
2. `Admin` 私聊发送 `/help group`。预期：帮助文本提到 `assistant`、`mention-only`、`all`、`/groupmode`、`/acl`，且不再提已废弃的旧群聊命令。
3. `MemberA` 私聊发送普通文本。预期：仍可正常使用私聊，不受群 ACL 影响。

## 5. 新群默认值

1. 把机器人拉入一个全新群，不做任何额外配置。
2. `Admin` 在群里发送普通文本，不 `@机器人`。预期：不响应。
3. `Admin` 在群里发送 `@机器人 你好`。预期：正常响应。
4. `MemberA` 在群里发送 `@机器人 你好`。预期：收到 ACL 拒绝提示。
5. `Admin` 在群里发送 `@机器人 /groupmode`。预期：显示当前工作态卡片，默认值为 `assistant`。
6. `Admin` 在群里发送 `@机器人 /acl`。预期：显示当前 ACL 卡片，默认值为 `admin-only`。
7. 如已配置 `trigger_open_ids`，让 `MemberA` 发送 `@Alias 你好`。预期：若 `MemberA` 未获授权，则仍收到 ACL 拒绝提示；说明 alias mention 已进入同一触发链路。

## 6. 群命令触发规则

1. 在默认 `assistant` 模式下，`Admin` 直接发送 `/groupmode`。预期：不生效，因未 `@机器人`。
2. 在默认 `assistant` 模式下，`Admin` 发送 `@机器人 /groupmode`。预期：正常显示工作态卡片。
3. 切到 `mention-only` 后，重复上一步。预期：仍然必须 `@机器人` 才生效。
4. 切到 `all` 后，`Admin` 直接发送 `/groupmode`。预期：可直接生效。
5. 切到 `all` 后，让 `MemberA` 直接发送 `/groupmode` 或 `/new`。预期：收到拒绝提示，因为群里的所有 `/` 命令都只给管理员。

## 7. ACL 行为

1. 保持 `assistant + admin-only`，让 `MemberA` 发送 `@机器人 你好`。预期：收到拒绝提示。
2. `Admin` 发送 `@机器人 /acl policy allowlist`。预期：策略切换成功。
3. `Admin` 发送 `@机器人 /acl grant @MemberB`。预期：授权成功。
4. `MemberB` 发送 `@机器人 你好`。预期：正常响应。
5. `MemberA` 再次发送 `@机器人 你好`。预期：仍被拒绝。
6. `Admin` 发送 `@机器人 /acl policy all-members`。预期：策略切换成功。
7. `MemberA` 发送 `@机器人 你好`。预期：正常响应。
8. 切到 `all + admin-only`，让 `MemberA` 直接发送普通文本。预期：静默忽略，不回复。
9. 仍在 `all + admin-only`，让 `MemberA` 直接发送 `/status` 或 `@机器人 你好`。预期：收到拒绝提示。

## 8. 三种工作态

1. `mention-only`：让 `MemberB` 连发两条普通消息，再发 `@机器人 请总结`。预期：前两条不会进入上下文，回复仅基于当前提问。
2. `assistant`：让 `MemberB` 连发两条普通消息，再发 `@机器人 请总结`。预期：回复会基于这两条上下文。
3. `all`：让 `MemberB` 直接发送普通文本。预期：机器人直接响应，无需 `@`。
4. `all`：让 `OtherBot` 直接发送普通文本或 `@机器人`。预期：不会直接触发。
5. 如已配置 `trigger_open_ids`：让 `MemberB` 发送 `@Alias 请总结`。预期：在 `assistant` / `mention-only` 下可等价触发；在 `all` 下仍按当前 ACL 判断。

## 9. assistant 上下文、boundary 与历史回捞

1. 切到 `assistant + all-members`。
2. 在第一次 `@机器人` 前，先发送若干条普通群消息。
3. `MemberA` 第一次有效发送 `@机器人 请总结之前讨论`。预期：
   - 先出现一张“准备群聊上下文”的执行卡片
   - 最终回复包含前面的普通群消息
   - 这次触发后 boundary 前移
4. 再依次发送：
   `MemberB: 第三条讨论`
   `MemberA: @机器人 再总结`
   预期：本轮只基于上次 boundary 之后的新消息，至少包含第三条讨论。
5. 在两次 `@` 之间插入 `@机器人 /status` 或 `@机器人 /groupmode`。
   预期：这些群命令不进入上下文，也不切断 boundary；下一次真正对话触发仍能看到命令前后的普通群消息。
6. 若群里存在 `OtherBot`，让它在两次人类 `@` 之间发一条普通消息，再由人类 `@机器人`。
   预期：`OtherBot` 的消息不会实时触发，但在下一次有效人类 `@` 时会通过历史回捞进入上下文。
7. 将 `group_history_fetch_limit: 0` 或 `group_history_fetch_lookback_seconds: 0` 后重启服务。
8. 重新让 `OtherBot` 在两次人类 `@` 之间发言。预期：机器人仍不会被 `OtherBot` 直接触发，且这条消息不再自动进入上下文。
9. 在两次有效 `@` 之间制造超过 `group_history_fetch_limit` 的缺失消息。预期：下一次回复中优先保留最近缺失消息，而不是最早的一批。
10. 如有脚本化测试条件，制造“与上次 boundary 同毫秒、但上次未消费”的缺失消息。预期：下一次回复不会漏掉这条消息，也不会重复带入上次已经消费过的同毫秒消息。
11. 主聊天流先发一条普通消息；再在某个话题里发一条普通消息；随后在主聊天流 `@机器人`。预期：回复只看主聊天流消息，不把该话题内容自动带进本轮上下文。
12. 在同一个话题里继续发消息并 `@机器人`。预期：回复只看该话题上下文；执行卡片、ACL 拒绝和长回复 follow-up 都尽量留在这个话题里，而不是跳回主聊天流。
13. 让 `MemberA` 与 `MemberB` 在同一个群里先后各触发一轮对话。预期：不会因为换了提问人而切成两个隔离的群后端会话；机器人仍表现为同一个群共享助手。

## 10. 其他机器人与事件边界

1. 让 `OtherBot` 在群里单独发消息。预期：`feishu-codex` 不会即时回复。
2. 让 `OtherBot` 在群里发 `@feishu-codex`。预期：仍不会即时触发。
3. 让人类随后 `@机器人` 请求总结。预期：若历史回捞开启，`OtherBot` 的消息可被带入上下文。
4. 观察日志。预期：不会出现“其他机器人直接触发本轮回复”的实时事件链路。

## 11. 外部卡片消息

1. 让人类成员或其他机器人发送一张普通 `interactive` 卡片，卡片中包含清晰文本，但不 `@feishu-codex`。
   预期：如果当前模式允许接收，这条消息会被降级成文本进入处理链路；否则仅作为上下文或被忽略。
2. 让人类成员发送一张包含文本且 `@feishu-codex` 的卡片消息。
   预期：若飞书事件里携带正确 `mentions` 元数据，则会按人类成员的 ACL 和当前工作态正常判断是否触发。
3. 让 `OtherBot` 发送一张包含文本且 `@feishu-codex` 的卡片消息。
   预期：不会直接触发；若历史回捞开启，可在后续人类有效 `@` 时进入上下文。
4. 让人类成员发送一张只有 `@feishu-codex`、没有正文文本的卡片。
   预期：不会变成正常 prompt；当前更接近“空文本”路径。
5. 点击别人或别的机器人发来的卡片按钮。
   预期：`feishu-codex` 不会代为点击或操控该卡片；当前只支持自己发出的卡片点击回调。

## 12. 持久化与重启

1. 先在某群设置非默认工作态和 ACL。
2. 重启服务：
   `systemctl --user restart feishu-codex`
3. 重新验证：
   `@机器人 /groupmode`
   `@机器人 /acl`
   预期：群工作态和群 ACL 都保留。
4. 如果此前已经产生 `assistant` 上下文，再次人类 `@机器人`。预期：上下文边界仍可继续工作，不会整段重置。

## 13. 日志与可观测性

1. 群聊发送一条普通文本。预期：日志里可看到 `name/open_id/user_id/chat_type/msg_type/message_id`。
2. 发送一张外部卡片。预期：日志里 `msg_type=interactive`。
3. `assistant` 模式下有效 `@` 时观察日志。预期：能看到历史回捞成功或失败日志。
4. 让 `OtherBot` 发言后再由人类 `@机器人`。预期：日志里能看到这次人类触发前的上下文准备过程，但不会出现“其他机器人直接触发成功”的记录。

## 14. 回归重点

- 默认新群是否仍为 `assistant + admin-only`
- `assistant` 下管理员群命令是否仍必须 `@`
- 群里的所有 `/` 命令是否仍只给管理员
- `all` 下未授权普通消息是否仍静默忽略
- 其他机器人是否仍不能直接触发
- `assistant` 是否会在每次有效人类 `@` 时补历史消息
- 当缺失消息超过 `group_history_fetch_limit` 时，是否优先保留最近缺失消息
- 同毫秒 boundary 场景下，是否仍不漏掉未消费缺失消息
- 其他机器人消息是否只能通过历史回捞进入上下文
- 群命令是否仍不推进上下文 boundary
- 主聊天流与话题上下文是否仍按 scope 隔离
- 话题内触发后的回复是否仍留在原话题
- 重启后群聊状态是否仍保留
