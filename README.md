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

5. 在飞书里给机器人发送一条普通文本开始。

如果希望在本地继续写入飞书里的同一线程（同一段连续对话及其上下文），请使用 `fcodex`，不要直接用裸 `codex`。更完整的线程语义见 `docs/session-profile-semantics.zh-CN.md`。

## 安装后会发生什么

`install.sh` 会自动完成：

- 创建 Python 虚拟环境到 `~/.local/share/feishu-codex/.venv/`
- 安装代码包与依赖
- 初始化配置文件到 `~/.config/feishu-codex/`
- 注册 systemd 用户服务
- 安装 `feishu-codex` 管理命令和 `fcodex` wrapper

## 必要配置

最少需要填写飞书凭证：

```yaml
# ~/.config/feishu-codex/system.yaml
app_id: "..."
app_secret: "..."
# request_timeout_seconds: 10
```

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
| `im:message.group_msg` | 支持群聊“全部唤醒”这类需要读取非 @ 消息的场景 |
| `im:message` | 读取消息内容，并发送/引用回复消息 |
| `im:message:readonly` | 读取消息详情，例如展开合并转发消息 |
| `im:message:send_as_bot` | 以应用身份发送文本和卡片消息 |
| `im:message:update` | 更新执行中的卡片内容 |
| `application:application:self_manage` | 读取机器人自身信息，用于更准确识别群聊里是否真的 @到机器人 |
| `contact:user.basic_profile:readonly` | 在合并转发消息里尽量展示用户名；缺少时会回退成 open_id 前缀 |

可在「权限管理」页面点击「批量开通」，粘贴以下 JSON：

```json
{
  "scopes": {
    "tenant": [
      "application:application:self_manage",
      "contact:user.basic_profile:readonly",
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
- `im:message.group_msg` 主要服务群聊“全部唤醒”场景；如果你明确只打算使用私聊和群聊 @机器人，可按需评估是否保留

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
- `/rename <title>`、`/star`、`/rm [thread_id|thread_name]`
- `/profile`：查看或切换 feishu-codex 默认 profile
- `/permissions`：查看或设置权限预设
- `/approval`、`/sandbox`：单独调整审批策略和沙箱策略
- `/mode`：查看或切换当前飞书会话后续 turn 的协作模式
- `/help`、`/help session`、`/help settings`、`/help local`



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
- 想理解为什么 `/resume` 需要保护、什么情况下会有双 backend 风险：`docs/shared-backend-resume-safety.zh-CN.md`
- 想理解整体架构、模块边界、仓库结构，以及 `feishu-cc` 与 Codex 的关系：`docs/feishu-codex-design.zh-CN.md`
- 想理解 `approval`、`sandbox`、`permissions` 这些概念背后的模型：`docs/codex-permissions-model.zh-CN.md`

补充说明：

- `docs/fcodex-shared-backend-runtime*` 与 `docs/feishu-codex-design*` 里会引用上游 Codex 源码仓库：<https://github.com/openai/codex.git>
- 这些实现向文档也会标明当前本地验证所依据的 Codex CLI 版本基线
- 对应英文副本就在同名 `.md` 文件中
