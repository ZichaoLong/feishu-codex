# README 草案

此文件与 `README.md` 同步，作为 README 的工作草案版本保留。

`README.md` 当前正文如下。

---

# feishu-codex

`feishu-codex` 通过飞书机器人，把消息、卡片、审批、群聊管理接到同一个 `codex app-server`。

它不是把 Codex TUI 直接搬进飞书；它提供的是：

- 飞书里的 thread 使用入口
- 本地共享 backend 上的 `fcodex` 入口
- 本地管理面 `feishu-codexctl`

## 先建立心智模型

先记住 5 个入口：

| 入口 | 作用 | 什么时候用 |
| --- | --- | --- |
| `feishu-codex` | 安装、配置、启动、停止、日志 | 管理本地服务 |
| 飞书聊天命令 | 当前 chat binding 的使用入口 | 在飞书里提问、切线程、改当前会话设置 |
| `fcodex` | 接到同一实例 shared backend 的本地 Codex 入口 | 想在本地继续飞书正在操作的同一 live thread |
| `feishu-codexctl` | 本地查看 / 管理面 | 看 binding / thread 状态，做 thread-scoped 管理 |
| 裸 `codex` | 独立本地 Codex 会话 | 不需要和飞书共用 live thread 时 |

最重要的一条：如果你想让飞书和本地安全地继续同一个 live thread，就用 `fcodex`，不要混用裸 `codex`。

## 前置条件

- Python 3.11+
- 本机已安装 `codex` CLI，且 `codex --help` 可正常执行
- 已在飞书开放平台创建应用，拿到 `app_id` 与 `app_secret`

## 快速开始

1. 安装。

   macOS / Linux：

   ```bash
   cd /path/to/feishu-codex
   bash install.sh
   ```

   Windows PowerShell：

   ```powershell
   cd \path\to\feishu-codex
   .\install.ps1
   ```

2. 打开系统配置并填写飞书应用信息。

   ```bash
   feishu-codex config system --open
   ```

3. 如果 provider key 走环境变量，写到：

   ```bash
   feishu-codex config env --open
   ```

4. 启动服务。

   ```bash
   feishu-codex start
   ```

5. 获取初始化口令。

   ```bash
   feishu-codex config init-token
   ```

6. 在飞书里私聊机器人执行：

   ```text
   /init <token>
   ```

7. 然后就可以发送 `/help`、普通文本，或开始配置群聊。

## 安装与配置

### 安装

- `install.py` 是唯一安装实现
- `install.sh` 和 `install.ps1` 只是平台包装器
- 安装后会生成 `feishu-codex`、`feishu-codexd`、`feishu-codexctl`、`fcodex`

### 飞书配置

推荐一次性把机器人、权限、事件与回调配好。

至少建议开通：

| 权限标识 | 用途 |
| --- | --- |
| `im:message.p2p_msg:readonly` | 接收单聊消息 |
| `im:message.group_at_msg:readonly` | 接收群里 `@机器人` 的消息 |
| `im:message.group_msg` | 支持群 `assistant` / `all`，以及 `trigger_open_ids` |
| `im:message` | 读取消息内容，并发送 / 引用回复 |
| `im:message:readonly` | 读取消息详情 |
| `im:message:send_as_bot` | 以应用身份发送文本和卡片 |
| `im:message:update` | 更新执行中的卡片 |
| `application:application:self_manage` | `/init`、`/whoareyou` 自动探测机器人身份 |
| `contact:contact.base:readonly` | 解析用户名 |
| `contact:user.base:readonly` | `/whoami`、群授权卡片、群上下文显示名字 |
| `contact:user.employee_id:readonly` | `/whoami` 返回 `user_id` 供排障 |

在「事件与回调」中启用：

- WebSocket 长连接模式
- 事件：`im.message.receive_v1`
- 回调：`card.action.trigger`

本项目默认走长连接，不需要公网 webhook URL。

### 本地配置

最常改的只有 4 处：

- `feishu-codex config system` → `system.yaml`
- `feishu-codex config codex` → `codex.yaml`
- `feishu-codex config env` → `feishu-codex.env`
- `feishu-codex config init-token` → `init.token`

最小 `system.yaml` 大致长这样：

```yaml
app_id: "..."
app_secret: "..."
# admin_open_ids:
#   - "ou_admin_1"
# bot_open_id: "ou_bot_xxx"
# trigger_open_ids:
#   - "ou_user_alias_xxx"
```

建议先在私聊里执行一次 `/init <token>`。它会：

- 把当前发送者写入 `admin_open_ids`
- 尝试自动探测并写入 `bot_open_id`
- 立即更新当前运行中的服务进程

如果 provider key 走环境变量，统一放到：

```ini
provider_api_key=...
```

`feishu-codexd` 与 `fcodex` 启动时都会主动加载这个文件。

### 多实例

可以跑多个实例；每个实例有自己的配置目录、数据目录、service owner 和 backend，但共享一套 `CODEX_HOME`。

常用形式：

```bash
feishu-codex instance create corp-a
feishu-codex instance list
feishu-codex --instance corp-a config system --open
feishu-codex --instance corp-a start
fcodex --instance corp-a
feishu-codexctl instance list
feishu-codexctl --instance corp-a service status
feishu-codex instance remove corp-a
```

多实例下再记住几条：

- `feishu-codex instance create <name>` 只负责创建该实例的 scaffold，不启动 service
- `feishu-codex instance list` 列出本机已知实例，并标注它们当前是否在运行
- `--instance default` 等价于不写 `--instance`；`default` 实例直接使用配置根 / 数据根本身，不会创建 `instances/default/`
- 同一 thread 的 live runtime 不能被两个实例 backend 同时持有
- 飞书侧 `/session`、`/resume` 受当前实例的 admission 可见性约束；本地 `fcodex` / `feishu-codexctl` 更偏操作者视角
- 删除命名实例请用 `feishu-codex instance remove <name>`；它只删除该命名实例的配置、数据与 service 定义，不会删除 `default`、共享 env 或 `_global`

多实例的推荐管理面分工：

- 创建 / 列出 / 删除实例：`feishu-codex instance create|list|remove`
- 配置 / 启停 / 日志：`feishu-codex --instance <name> ...`
- 查看运行中的实例注册表：`feishu-codexctl instance list`
- 本地线程管理：`feishu-codexctl --instance <name> ...`
- 本地继续 live thread：`fcodex --instance <name> ...`

### 安装后会发生什么

安装器会自动：

- 创建虚拟环境并安装依赖
- 初始化 `default` 实例的配置 / 数据目录
- 生成 `system.yaml.example`、`codex.yaml.example`
- 生成 `init.token`
- 生成共享的 `feishu-codex.env`
- 安装平台对应的用户态 service manager 配置
  - Linux：`systemd --user`
  - macOS：`LaunchAgent`
  - Windows：`Task Scheduler`

多实例时，命名实例不会写回根目录；它们固定落在 `instances/<name>` 子目录下。

默认根目录如下：

| 平台 | 配置根 | 数据根 |
| --- | --- | --- |
| Linux | `~/.config/feishu-codex` | `~/.local/share/feishu-codex` |
| macOS | `~/Library/Application Support/feishu-codex/config` | `~/Library/Application Support/feishu-codex/data` |
| Windows | `%APPDATA%\\feishu-codex\\config` | `%LOCALAPPDATA%\\feishu-codex\\data` |
| 源码树直跑 | `./config` | `./data/feishu_codex` |

逻辑布局可以按下面理解：

```text
<config_root>/
  feishu-codex.env              # 机器级共享 env，所有实例共用
  system.yaml                   # default 实例
  codex.yaml
  init.token
  instances/
    corp-a/
      system.yaml
      codex.yaml
      init.token

<data_root>/
  feishu-codex.log              # default 实例日志
  chat_bindings.json            # default 实例 binding 持久化
  profile_state.json            # default 实例“新线程默认 profile”
  app_server_runtime.json       # default 实例当前 backend 发现状态
  service-instance.json         # default 实例 service owner 元数据
  _global/                      # 机器级共享协调区
    instance_registry.json
    thread_resume_profiles.json
    thread_runtime_leases.json
  instances/
    corp-a/
      feishu-codex.log
      chat_bindings.json
      profile_state.json
      app_server_runtime.json
      service-instance.json
```

如果你执行：

```bash
feishu-codex instance create corp-a
```

它会创建 `corp-a` 这套实例目录与模板文件，但不会自动启动该实例 service。

### 服务管理

统一用 `feishu-codex`：

```bash
feishu-codex start|stop|restart|status
feishu-codex log
feishu-codex run
feishu-codex config
feishu-codex instance create <name>
feishu-codex instance list
feishu-codex instance remove <name>
feishu-codex uninstall
feishu-codex purge
```

多实例时，`start|stop|restart|status|log|run|config` 这组命令在最前面加 `--instance <name>` 即可；`instance create|remove` 则直接把实例名写在子命令参数里，`instance list` 不接受顶层 `--instance`。

## 使用

### 单聊

只有管理员可在私聊里直接发送普通文本提问。

- 非管理员私聊会被拒绝；如需协作使用，请把机器人拉进群并由管理员先执行 `/group activate`

- 当前没有绑定线程时，会在当前目录创建新线程
- 当前已经绑定线程时，会继续写入该线程
- 附件会先下载到当前工作目录的 `_feishu_attachments/`
- `folder`、`sticker`、`merge_forward` 的子附件、`interactive` 卡片内资源，当前会直接拒绝为附件输入
- 附件消息本身不会直接启动 turn；通常要再发一条文字说明来消费这些 pending 附件

常用命令：

- `/help`
- `/status`
- `/preflight`
- `/session`
- `/resume <thread_id|thread_name>`
- `/new`
- `/unsubscribe`
- `/cd <path>`、`/pwd`、`/cancel`
- `/rename <title>`、`/rm [thread_id|thread_name]`
- `/profile [name]`
- `/permissions`、`/approval`、`/sandbox`、`/mode`
- `/whoami`、`/whoareyou`、`/init <token>`

### 群聊

新群默认是：

- 工作态：`assistant`
- 群状态：`未激活`（管理员仍可先用来初始化和管理）

群里再记住 5 条：

- 群里的共享状态命令和设置都只给管理员
- `/group` 查看当前群是否已激活；`/group activate` 激活，`/group deactivate` 停用
- 群一旦激活，当前成员和后续新加入成员都可正常使用；管理员之后退群也不影响日常对话
- 在 `assistant` / `mention-only` 下，管理员命令和普通对话都需要先显式 mention 触发对象
- 运行时审批卡片和补充输入卡片默认由当前请求发起者本人处理；管理员仍可兜底处理
- 如需支持 `trigger_open_ids` 或读取非 `@机器人` 群消息，需要开 `im:message.group_msg`

最常用的群命令：

```text
@机器人 /group
@机器人 /group activate
@机器人 /group deactivate
@机器人 /groupmode
@机器人 /groupmode assistant
@机器人 /groupmode mention-only
@机器人 /groupmode all
```

### 本地继续与本地管理

#### `fcodex`

`fcodex` 现在尽量接近裸 `codex`：它负责 shared-backend 路由、实例选择、cwd 代理，以及少量 thread-wise profile 逻辑；它不再保留 shell 层 slash 自命令。

最常用的入口：

```bash
fcodex
fcodex --instance corp-a
fcodex --cd /path/to/project
fcodex resume <thread_id|thread_name>
fcodex -p <profile>
fcodex -p <profile> resume <thread_id|thread_name>
```

请直接记住下面几条：

- `fcodex`、`fcodex <prompt>`、`fcodex resume <thread_id>` 仍是 upstream Codex CLI，只是默认连到 shared backend
- `fcodex resume <thread_name>` 会做跨 provider 的精确名字匹配，再按 thread id 恢复
- `fcodex` shell 层不再支持 `/help`、`/session`、`/profile`、`/rm`、`/resume`
- `fcodex` 也不再提供 `--dry-run` wrapper 入口
- 一旦进入 TUI，里面的 `/help`、`/resume`、`/new` 等都回到 upstream Codex 语义

#### `feishu-codexctl`

`feishu-codexctl` 是本地查看 / 管理面，不是第二个 Codex 前端。

常用命令：

```bash
feishu-codexctl service status
feishu-codexctl instance list
feishu-codexctl binding list
feishu-codexctl binding status <binding_id>
feishu-codexctl binding clear <binding_id>
feishu-codexctl binding clear-all
feishu-codexctl thread list --scope cwd
feishu-codexctl thread list --scope global
feishu-codexctl thread status --thread-id <id>
feishu-codexctl thread status --thread-name <name>
feishu-codexctl thread bindings --thread-id <id>
feishu-codexctl thread bindings --thread-name <name>
feishu-codexctl thread unsubscribe --thread-id <id>
feishu-codexctl thread unsubscribe --thread-name <name>
feishu-codexctl thread admissions
feishu-codexctl thread import --thread-id <id>
feishu-codexctl thread revoke --thread-id <id>
```

说明：

- `thread list` 默认 `--scope cwd`，也支持 `--cwd /path/to/project`
- 线程目标必须显式写成 `--thread-id` 或 `--thread-name`
- `binding clear` / `clear-all` 清的是 Feishu 本地 bookmark，不是删线程，也不等于 `unsubscribe`

## 进阶使用

### 多目录 / 多项目

最稳妥的做法是：按“一个飞书会话绑定一个工作目录 / 线程上下文”来用。

常见形态：

- 私聊机器人时，单聊天然按人隔离
- 群聊时，一个群绑定一个共享线程上下文
- 同时操控多个项目时，开多个会话 / 群，让它们分别绑定不同目录和 thread

### 飞书与本地共同操作同一个 thread

这是本项目最重要的能力之一，但前提是：飞书和本地都连到同一个实例 backend。

当前模型是：

- 同一个 thread 可以有多个 subscriber
- 普通输出和终态结果会广播给所有订阅者
- 审批、补充输入、中断等交互请求只路由给当前 `interaction owner`
- 谁发起当前 turn，谁拿到这一轮的交互 owner
- turn 结束后 owner 被释放，thread 回到 idle 稳态

推荐路径：

1. 飞书里操作某 thread
2. 需要本地接手时，用 `fcodex` 连到同一实例 backend
3. 需要让 Feishu 释放 runtime residency 时，用 `/unsubscribe` 或 `feishu-codexctl thread unsubscribe`

不推荐路径：

- 用裸 `codex` 在另一个 isolated backend 上继续同一 thread
- 让两个实例 backend 同时 live attach 同一 thread

## 关键行为说明

### Thread-wise `profile/provider`

当前正式合同已经是 thread-wise，而不是实例级 resume 默认值。

请直接记住：

- 飞书 `/profile <name>` 作用于**当前绑定 thread**，不是实例级全局默认值
- 当前 chat 还没绑定 thread 时，`/profile` 会直接拒绝；先 `/new`，或先发第一条普通消息创建 thread
- `/new` 与未绑定 chat 的第一条普通消息，都会把当前实例当前生效的“新线程默认 profile”作为这个新 thread 的一次性 seed
- 新 thread 真正创建成功、拿到真实 `thread_id` 后，这个 seed 才会按 `thread_id` 持久化成 thread-wise resume 设置
- 后续 resume 读的是 thread 自己保存的 profile，而不是实例此刻的“新线程默认 profile”
- 只有目标 thread **可验证地 globally unloaded** 时才允许改 profile；loaded 或状态不可验证时都会直接拒绝，不会热切，也不会偷偷记账

本地侧对应规则：

- `fcodex -p <profile>`：给这次启动将创建的**第一个新 thread**做一次性 seed
- `fcodex -p <profile> resume <thread>`：只有 thread **可验证地 globally unloaded** 时才允许；成功后会写入该 thread 的持久化 resume profile
- 如果目标 thread 仍 loaded，会直接拒绝，并提示先 `unsubscribe`、再关闭其他打开该 thread 的 `fcodex` TUI

### Sandbox / approval / permissions

先记住概念：

- `sandbox`：技术执行边界
- `approval_policy`：什么时候必须先审批
- `permissions`：对前两者的预设打包

当前实现里：

- 飞书侧设置是 binding 级的，并会随 binding 持久化
- `fcodex` 侧仍主要按显式参数和 upstream Codex 配置生效
- 当前没有跨前端即时同步、统一持久化的设置面
- 某一轮 turn 实际采用的是发起该轮的那个前端设置

### 避坑速记

- `/new` 会立即创建新线程并切换当前 binding
- `/rm` 实际调用的是 Codex archive，不是硬删除
- `unsubscribe` 释放的是 Feishu runtime residency，不会清 binding，也不会删线程
- `/profile` 改不了时，通常先 `/unsubscribe`，再关闭其他打开同一 thread 的 `fcodex` TUI
- `folder`、`sticker`、`merge_forward` 子附件、`interactive` 卡片资源，当前不作为附件输入
- `fcodex resume <name>` 与 TUI 内 `/resume` 不是一回事
- 本地查线程请用 `feishu-codexctl thread list`，不要再找 `fcodex /session`
- 本地切换 profile 请用 `-p/--profile`；不要再找 `fcodex /profile`

## 按问题查文档

| 你想确认什么 | 先看哪里 |
| --- | --- |
| 当前总体架构、模块边界、仓库结构 | `docs/architecture/feishu-codex-design.zh-CN.md` |
| 飞书 `/session`、`/resume`、`/profile`，以及 `fcodex` / `feishu-codexctl` 的当前语义 | `docs/contracts/session-profile-semantics.zh-CN.md` |
| `unsubscribe`、`fcodex` / `feishu-codexctl` 分工、thread-wise profile/provider 的正式合同 | `docs/contracts/local-command-and-thread-profile-contract.zh-CN.md` |
| `/status`、`/preflight`、`/unsubscribe`、`feishu-codexctl` 的共享状态词汇 | `docs/contracts/runtime-control-surface.zh-CN.md` |
| 群激活、群聊模式、历史回捞、触发规则 | `docs/contracts/group-chat-contract.zh-CN.md` |
| `approval`、`sandbox`、`permissions` 的语义 | `docs/contracts/codex-permissions-model.zh-CN.md` |
| `fcodex`、shared backend、动态端口、cwd 代理 | `docs/architecture/fcodex-shared-backend-runtime.zh-CN.md` |
| shared backend 与 `/resume` 的安全边界 | `docs/decisions/shared-backend-resume-safety.zh-CN.md` |
| 飞书 `/help` 的导航结构 | `docs/contracts/feishu-help-navigation.zh-CN.md` |
| 飞书线程生命周期 | `docs/contracts/feishu-thread-lifecycle.zh-CN.md` |

更完整的文档入口见 `docs/doc-index.zh-CN.md`。
