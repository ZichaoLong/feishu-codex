# feishu-codex

> 说明：本项目最开始来源于 [shenman9/feishu_bot](https://github.com/shenman9/feishu_bot)。更准确地说，它是从 `feishu_bot` 中用于“飞书 + Claude Code”的那部分子集能力演进而来，并在此基础上改造成面向 Codex 的实现，因此形成了当前的 `feishu-codex`。
>
> 也可以把它理解为：保留飞书侧消息、卡片、审批和会话管理这类交互形态，同时将底层接入切换为 Codex 原生 app-server 协议。

`feishu-codex` 通过 Feishu 机器人把消息、审批和会话管理接到 `codex app-server`。

当前状态是可安装（Linux）、可启动、核心链路可用的 MVP，功能仍在持续补齐中。

## 前置条件

- Python 3.10+
- 本机已安装 `codex` CLI，且 `codex --help` 可正常执行
- 飞书开放平台已创建应用，获取 `app_id` 和 `app_secret`

## 先理解这几件事

- `feishu-codex` service 持有一个 shared Codex backend；飞书侧与 `fcodex` 只有接到同一个 backend，才适合继续同一个 live thread
- 大多数独立本地使用场景，直接用裸 `codex` 就可以；只有在“接飞书正在操作的同一线程”或“借助 shared discovery 恢复线程”时，才优先使用 `fcodex`
- README 负责快速开始、常用命令和避坑；如果你想继续深挖而不先读源码，文末“继续深挖看哪里”会把问题映射到对应文档

## 现在怎么开始

1. 安装：

   ```bash
   cd /path/to/feishu-codex
   bash install.sh
   ```

2. 填写飞书凭证：

   ```bash
   nano ~/.config/feishu-codex/system.yaml
   ```

3. 如果你的 Codex provider 通过环境变量取 key，写到：

   ```bash
   nano ~/.config/environment.d/90-codex.conf
   ```

4. 启动服务：

   ```bash
   systemctl --user start feishu-codex.service
   ```

5. 查看初始化口令：

   ```bash
   cat ~/.config/feishu-codex/init.token
   ```

6. 在飞书里私聊机器人执行：

   ```text
   /init <token>
   ```

7. 再继续发送 `/help`、普通文本，或开始配置群聊。

如果希望在本地继续写入飞书里的同一线程（同一段连续对话及其上下文），请使用 `fcodex`，不要直接用裸 `codex`。更完整的线程语义见 `docs/session-profile-semantics.zh-CN.md`。

## 安装后会发生什么

`install.sh` 会自动完成：

- 创建 Python 虚拟环境到 `~/.local/share/feishu-codex/.venv/`
- 安装代码包与依赖
- 初始化配置文件到 `~/.config/feishu-codex/`
- 刷新本地默认模板 `~/.config/feishu-codex/system.yaml.example` 与 `~/.config/feishu-codex/codex.yaml.example`
- 生成初始化口令文件 `~/.config/feishu-codex/init.token`
- 注册 systemd 用户服务
- 安装 `feishu-codex` 管理命令和 `fcodex` wrapper

## 必要配置

最少需要填写飞书凭证：

```yaml
# ~/.config/feishu-codex/system.yaml
app_id: "..."
app_secret: "..."
# request_timeout_seconds: 10
# admin_open_ids:
#   - "ou_admin_1"
# bot_open_id: "ou_bot_xxx"
# trigger_open_ids:
#   - "ou_user_alias_xxx"
# group_history_fetch_limit: 50
# group_history_fetch_lookback_seconds: 86400
```

建议先私聊机器人执行一次 `/init <token>`。它会：

- 把当前发送者的 `open_id` 写入 `admin_open_ids`
- 尝试自动探测并写入 `bot_open_id`
- 立即更新当前服务进程；不需要为了生效而重启

管理员始终属于群里的已授权人类成员，并可通过 `/groupmode`、`/acl` 管理群聊；群里的所有 `/` 命令也都只给管理员。若群工作态是 `assistant` 或 `mention-only`，管理员仍需先显式 mention 触发对象才会触发对话或群命令。管理员配置与群 ACL 统一使用 `open_id`；可先私聊机器人发送 `/whoami` 获取。
运行时身份判定只依赖 `open_id`；`user_id` 仅保留在日志与 `/whoami` 输出里，便于人工排障。

群聊能力要求显式配置 `bot_open_id`。当前群聊链路默认只依赖本地配置做严格判定；未配置时，群里的有效 mention 不会被视为触发。
`/whoareyou` 与 `/init` 里的实时探测只用于诊断和初始化，不会绕过或替代 `system.yaml.bot_open_id` 这个运行时权威值。

如果你希望“别人 `@你本人` 时，由机器人代答”，可额外配置 `trigger_open_ids`。只要群消息 `mentions[].open_id` 命中这些值之一，也会被视为一次有效群聊触发。常见做法：

- 私聊机器人发送 `/whoami`，拿到你自己的 `open_id`
- 把它写进 `system.yaml.trigger_open_ids`
- 保留 `system.yaml.bot_open_id`，用于机器人自身 mention 的严格判定

如果你使用群聊 `assistant` 模式，还可以调整每次有效触发时的历史回捞窗口：

- `group_history_fetch_limit`：每次最多回捞多少条历史消息，默认 `50`
- `group_history_fetch_lookback_seconds`：主聊天流回捞时使用的时间窗口，默认 `86400`（24 小时）；它同时也是历史回捞总开关的一部分
- 说明：飞书公开接口对 `thread` 容器不支持 `start_time/end_time`；因此话题内回捞当前不承诺严格按该时间窗口裁剪，只保证受上下文边界和条数限制
- 任一项设为 `0`，即可禁用所有历史回捞（包括主聊天流和话题）

## 群聊能力速览

- 新群默认：`assistant` + `admin-only`
- 私聊底层会话按人隔离；群聊底层会话按 `chat_id` 共享一个 Codex backend 会话
- 人类成员权限：按群管理，使用 `/acl`
- ACL 只决定谁具备群聊触发资格；是否必须显式 mention 由群工作态决定
- 群里的所有 `/` 命令都只给管理员；在群聊 `assistant` 和 `mention-only` 工作态下，**管理员命令和普通对话都需要先显式 mention 触发对象**
- 有效 mention 默认只认机器人自身 `bot_open_id`；如配置 `trigger_open_ids`，`@这些人` 也会视为触发
- 在群聊 `assistant` 工作态下，每次有效人类 `@` 都会额外回捞最近群历史，用来补齐两次 `@` 之间缺失的上下文，包括其他机器人消息；主聊天流回捞受时间窗和条数限制，话题内回捞当前只保证受边界和条数限制；当缺失消息超过 `group_history_fetch_limit` 时，保留最近缺失消息
- `assistant` 的主聊天流与群话题使用不同上下文边界：主聊天流只看主聊天流，话题只看当前话题；但底层仍是同一个群共享会话
- 在群话题内触发时，执行卡片、ACL 拒绝和长回复会尽量留在原话题
- 在群聊 `all` 工作态下，人类消息和群命令可直接触发；其他机器人不会直接触发

如果你要对照完整行为边界，请看 `docs/feishu-codex-design.zh-CN.md` 中的“群聊功能合同”。

按需调整 Codex 参数：

```bash
nano ~/.config/feishu-codex/codex.yaml
```

如果 provider key 走环境变量，推荐统一放在：

```ini
# ~/.config/environment.d/90-codex.conf
provider1_api_key=...
provider2_api_key=...
```

`install.sh` 会把这个文件接入 `feishu-codex.service`，并且 `feishu-codex run`、`fcodex` 也会一并加载它。

[飞书开放平台](https://open.feishu.cn)里，建议先把应用权限、事件与回调一次性配好。

在「权限管理」中，建议至少开通这些权限：

| 权限标识 | 用途 |
| --- | --- |
| `im:message.p2p_msg:readonly` | 接收单聊消息 |
| `im:message.group_at_msg:readonly` | 接收群聊里 @机器人的消息 |
| `im:message.group_msg` | 支持群聊 `assistant` / `all` 工作态，读取非 `@机器人` 的群消息；若要支持 `trigger_open_ids`（例如别人 `@你本人` 触发），也依赖此项 |
| `im:message` | 读取消息内容，并发送/引用回复消息 |
| `im:message:readonly` | 读取消息详情，例如展开合并转发消息 |
| `im:message:send_as_bot` | 以应用身份发送文本和卡片消息 |
| `im:message:update` | 更新执行中的卡片内容 |
| `application:application:self_manage` | 建议开通；`/init` 与 `/whoareyou` 自动探测机器人自身 `open_id` 依赖它 |
| `contact:contact.base:readonly` | 允许调用通讯录用户接口，用于解析用户名 |
| `contact:user.base:readonly` | 允许返回用户名等基础字段；`/whoami`、群 ACL 可读名字、群上下文用户名都依赖它；缺少时会回退成 open_id 前缀 |
| `contact:user.employee_id:readonly` | 建议默认开通；允许在消息事件与 `/whoami` 中返回 `user_id`。`user_id` 仅用于排障，不参与运行时身份判定 |

可在「权限管理」页面点击「批量开通」，粘贴以下 JSON：

```json
{
  "scopes": {
    "tenant": [
      "application:application:self_manage",
      "contact:contact.base:readonly",
      "contact:user.base:readonly",
      "contact:user.employee_id:readonly",
      "im:message.group_msg",
      "im:message.group_at_msg:readonly",
      "im:message",
      "im:message.p2p_msg:readonly",
      "im:message:readonly",
      "im:message:send_as_bot",
      "im:message:update"
    ]
  }
}
```

说明：

- 上面这组权限覆盖当前 README 所描述的主链路能力
- 当前 `feishu-codex` 不要求你额外开 `docs`、`drive`、`calendar`、`wiki`、`base` 这些 scope
- `im:message.group_msg` 主要服务群聊 `assistant` / `all` 工作态；如果你明确只打算使用私聊和群聊 `mention-only`，可按需评估是否保留
- 群聊显式 mention 判定只依赖 `system.yaml.bot_open_id`
- `/whoareyou` 只用于辅助你探测应填写的机器人 `open_id`；探测结果不会自动参与运行时判定
- `user_id` 仅用于日志与 `/whoami` 排障展示；运行时 ACL、管理员、mention 判定一律只看 `open_id`
- `trigger_open_ids` 只影响“哪些 mentions 视为触发”，不绕过 ACL，也不替代 `bot_open_id`

在「事件与回调」中，启用 **WebSocket 长连接模式**，并配置：

- 事件配置：`im.message.receive_v1`
- 回调配置：`card.action.trigger`

本项目默认走飞书长连接，不需要额外配置公网 webhook URL。

## 启动与使用
使用 `systemctl --user` 管理服务：

```bash
systemctl --user start feishu-codex
systemctl --user stop feishu-codex
systemctl --user restart feishu-codex
systemctl --user status feishu-codex --no-pager
journalctl --user -u feishu-codex -f
```

或使用 `feishu-codex` 命令管理服务：

```bash
feishu-codex start/stop/restart/status # 转调 systemctl --user [xxx] feishu-codex
feishu-codex log # 转调 journalctl --user -u feishu-codex -f
feishu-codex run # 前台调试，不走 systemd
feishu-codex config/uninstall/purge # 其他便捷命令: 配置、卸载、卸载+删除配置
```

## 常用命令

飞书侧：

- 直接发送普通文本：向当前线程提问；如果当前没有绑定线程，会在当前目录自动新建
- `/session`：查看当前目录线程
- `/resume <thread_id|thread_name>`：按后端全局精确匹配恢复线程，并切换到线程自己的目录
- `/new`：立即新建线程
- `/cd <path>`、`/pwd`、`/status`、`/cancel`
- `/rename <title>`、`/rm [thread_id|thread_name]`
- `/profile`：查看或切换 feishu-codex 默认 profile
- `/permissions`：查看或设置权限预设
- `/approval`、`/sandbox`：单独调整审批策略和沙箱策略
- `/mode`：查看或切换当前飞书会话后续 turn 的协作模式
- `/init <token>`：私聊初始化管理员与 `bot_open_id`
- `/whoami`：私聊查看自己的 `open_id`，以及 best-effort 的 `user_id`（仅用于排障；缺少 `contact:user.employee_id:readonly` 时可为空）
- `/whoareyou`：查看机器人的 `app_id`、已配置 `bot_open_id`、实时探测 `open_id`
- `/groupmode`：查看或切换当前群聊工作态
- `/acl`：查看或调整当前群聊授权策略
- `/help`、`/help session`、`/help settings`、`/help group`
- 本地 `fcodex` wrapper 命令说明：在终端执行 `fcodex /help`

## 群聊使用

把机器人拉进群后即可使用，但是否会响应，取决于当前群聊工作态和 ACL。

- 新群默认是 `assistant` + `admin-only`
- 这意味着：默认只有配置里的管理员具备群聊触发资格；其他成员即使显式 mention 触发对象也不会触发
- 群聊建议同时配置 `bot_open_id`
- 如果要让机器人读取群里所有消息，或支持 `trigger_open_ids`（例如别人 `@你本人` 时触发），必须为应用开通 `im:message.group_msg`

群命令触发规则：

- 私聊：直接发送命令即可，例如 `/whoami`、`/whoareyou`
- 群聊里的所有 `/` 命令都只给管理员
- 群聊 `assistant` / `mention-only`：管理员命令也需要先显式 mention 触发对象，例如 `@机器人 /groupmode`、或 `@trigger_open_ids里的某个人 /groupmode`
- 群聊 `all`：管理员可直接发送群命令

群聊工作态：

- `mention-only`：只有有效 mention 的消息会触发；不缓存群上下文
- `assistant`：缓存群里收到的消息，但只有被人类有效 mention 时才回复；每次有效触发都会按配置回捞时间窗内缺失的最近历史消息，并把两次触发之间的消息统一并入同一份上下文。上下文边界会同时记录边界时间戳下已消费的 `message_id`，用于避免同毫秒缺失消息被漏掉。回捞开始前会先出现一张“准备群聊上下文”的执行卡片；若回捞失败，则本次会直接报错停止，避免带着残缺上下文继续回复
- `assistant` 的主聊天流和每个群话题分别维护自己的上下文边界：主聊天流不会自动读入话题回复，话题也不会自动读入主聊天流；但它们共享同一个群底层会话，因此模型可以记住本群其他讨论里已经明确的结论
- 主聊天流历史回捞同时受 `group_history_fetch_limit` 和 `group_history_fetch_lookback_seconds` 限制；话题内历史回捞当前只保证受上下文边界和 `group_history_fetch_limit` 限制，但 `group_history_fetch_lookback_seconds=0` 仍会关闭全部历史回捞
- `all`：群里消息都会直接触发机器人回复；风险最高，容易刷屏
- 有效 mention 默认只认 `bot_open_id`；如配置 `trigger_open_ids`，命中这些 `open_id` 的 mentions 也会视为触发
- 群命令不会写入 `assistant` 上下文日志，也不会推进上下文边界
- 其他机器人不会直接触发当前机器人；它们的群消息如果要进入上下文，依赖 `assistant` 在每次有效触发时做历史回捞
- 在群话题内触发时，执行卡片、ACL 拒绝和过长文本 follow-up 会尽量继续回复到原话题

管理员命令：

- 群里的所有 `/` 命令都只给管理员
- 在 `assistant` / `mention-only` 中，下面这些群命令要先显式 mention 触发对象再发送；在 `all` 中管理员可直接发送
- `/groupmode`：查看当前群聊工作态
- `/groupmode assistant`
- `/groupmode all`
- `/groupmode mention-only`
- `/acl`：查看当前群 ACL
- `/acl policy admin-only`
- `/acl policy allowlist`
- `/acl policy all-members`
- `/acl grant @成员`
- `/acl revoke @成员`

ACL 策略：

- `admin-only`：只有管理员具备群聊触发资格
- `allowlist`：管理员和已授权成员具备群聊触发资格
- `all-members`：群内所有成员都具备群聊触发资格，后续新加入成员也自动可用
- 是否还需要显式 mention，仍取决于当前群聊工作态
- 未获授权成员在 `all` 模式下直接发普通消息会静默忽略；只有显式 mention 触发对象或发群命令时才会收到拒绝提示

其他机器人与历史回捞：

- 飞书不会把其他机器人发言实时推送给机器人，因此它们不能直接触发 `feishu-codex`
- `assistant` 每次有效人类 `@` 都会额外调用消息历史接口，把时间窗口内缺失的消息补进上下文；如果缺失消息多于 `group_history_fetch_limit`，当前实现保留最近缺失消息
- 因此，在“群消息历史可见”配置正确时，其他机器人消息通常能出现在 `assistant` 回复所见的上下文里
- 如果你禁用了历史回捞，其他机器人消息就不会自动进入上下文

典型用法：

1. 在 `system.yaml` 里配置 `admin_open_ids`
2. 私聊机器人执行 `/whoareyou`，把返回的机器人 `open_id` 填进 `bot_open_id`
3. 如需“别人 @你本人时由机器人代答”，再把你的 `open_id` 填进 `trigger_open_ids`
4. 把机器人拉进群
5. 管理员在群里执行 `@机器人 /groupmode assistant`
6. 如需放开人类成员使用范围，再执行 `@机器人 /acl policy allowlist` 或 `@机器人 /acl policy all-members`
7. 如果你希望回复能参考其他机器人发言，确保应用已开通 `im:message.group_msg`，并保留历史回捞配置

如需限制 assistant 模式每次补齐上下文的规模，可再加：

```yaml
group_history_fetch_limit: 30
group_history_fetch_lookback_seconds: 21600
```

上例表示：每次有效触发时，最多补最近 6 小时内的 30 条历史消息。

更严格的群聊行为合同，见 [docs/feishu-codex-design.zh-CN.md](/home/zlong/llm/feishu-codex/docs/feishu-codex-design.zh-CN.md) 的“6.5 群聊功能合同”。



本地 `fcodex`：
`fcodex` 是一个面向 `feishu-codex` shared backend 的本地 wrapper。你可以把它理解为：`fcodex` 默认把 `codex` 接到 shared backend；裸 `codex` 则更适合独立本地会话。

`fcodex` 支持 `/xxx` 自命令，除了 `/xxx` 型自命令，其余参数和子命令仍会继续走 upstream `codex`。

- `fcodex`：启动 `codex` 并接到 feishu-codex shared backend
- `fcodex /help`
- `fcodex /session [global]`：查看 shared backend 可见的线程，不限于飞书创建的线程；默认当前目录，`global` 为 backend 全局，均跨 provider 聚合。当前默认 `sourceKinds` 为 `cli`、`vscode`、`exec`、`appServer`
- `fcodex /resume <thread_id>`：按精确 `thread_id` 恢复线程
- `fcodex /rm <thread_id|thread_name>`：同飞书侧 `/rm`，调用 Codex archive，会从常规列表中隐藏，不是硬删除
- `fcodex /resume <thread_name>`：基于 `fcodex /session` 会话发现逻辑，恢复 thread_name 对应会话
- `fcodex /profile`：同飞书侧 `profile`，持久修改 feishu-codex / 默认 `fcodex` 的本地默认 profile，不会改动裸 `codex` 全局配置

如果你需要在本地继续飞书相关的会话，见下文“什么时候用 `fcodex`”。

## 什么时候用 `fcodex`

- 要在本地继续飞书正在写的同一线程时，用 `fcodex`。
- 要先按与飞书一致的规则查找线程、确认 `thread_id` 时，用 `fcodex /session [global]`。
- 只是开一个独立本地会话时，直接用裸 `codex`。

如果你已经拿到精确 `thread_id`，也可以用 `fcodex /resume <thread_id>` 恢复线程。恢复时优先使用该线程原本在用的 provider；跨 provider 恢复可能因历史加密内容失败。更完整的 `session` / `resume` 语义见 `docs/session-profile-semantics.zh-CN.md`

## 避坑速记

- `/new` 会立即创建一个新线程，不是先绑一个空占位
- `/rm` 调用的是 Codex archive，会从常规列表中隐藏，不是硬删除
- 进入 TUI 后，里面的 `/resume` 是 upstream Codex 行为，不等同于 `fcodex /resume`
- `/profile` 改的是 feishu-codex / 默认 `fcodex` 的本地默认 profile，不改裸 `codex` 全局配置

## 继续深挖看哪里

如果你已经能用起来，但还想进一步理解项目，又不想先去读源码，建议按问题找文档：

- 想理解 `/session`、`/resume`、`/profile`、thread / session 的精确语义：`docs/session-profile-semantics.zh-CN.md`
- 想理解 `fcodex`、shared backend、动态端口、cwd 代理这些运行时机制：`docs/fcodex-shared-backend-runtime.zh-CN.md`
- 想理解为什么飞书侧不能照搬 `fcodex` 的前端实现，以及线程绑定 / unload / resume 的状态机：`docs/feishu-thread-lifecycle.zh-CN.md`
- 想理解 shared backend、安全边界，以及为什么裸 `codex` 不应与飞书 / `fcodex` 同时写同一线程：`docs/shared-backend-resume-safety.zh-CN.md`
- 想理解整体架构、模块边界、仓库结构，以及 `feishu-cc` 与 Codex 的关系：`docs/feishu-codex-design.zh-CN.md`
- 想理解 `approval`、`sandbox`、`permissions` 这些概念背后的模型：`docs/codex-permissions-model.zh-CN.md`

补充说明：

- `docs/fcodex-shared-backend-runtime*` 与 `docs/feishu-codex-design*` 里会引用上游 Codex 源码仓库：<https://github.com/openai/codex.git>
- 这些实现向文档也会标明当前本地验证所依据的 Codex CLI 版本基线
- 对应英文副本就在同名 `.md` 文件中
