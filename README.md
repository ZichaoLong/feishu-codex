# FOCUS

> 说明：本项目最开始来源于 [shenman9/feishu_bot](https://github.com/shenman9/feishu_bot)。更准确地说，它是从 `feishu_bot` 中用于“飞书 + Claude Code”的那部分子集能力演进而来，并在此基础上改造成面向 Codex 的实现，最终形成当前的 FOCUS。

**FOCUS - Feishu, Online Codex for Users and Sharing** 把飞书机器人、本地 Codex TUI 和同一个 `codex app-server`
接到一起。

本项目提供：

- 飞书里的 codex thread 使用入口
- 本地继续同一 codex live thread 的 `focus`
    - `fcodex` 是 `focus` 的等价别名，强调 Codex TUI thin wrapper 语义
- 本地查看 / 管理面 `focusctl`

你可以把它理解成一层桥接：

- 飞书会话先绑定到某个 `thread`
- 这个 `thread` 跑在某个 FOCUS 实例自己的 shared backend 即 `codex app-server` 上
- 多订阅观察+单交互轮转租约：飞书和 `focus` / `fcodex` 可连到同一个实例 backend，此时可安全继续操作同一个 live thread，并同时收到回复消息推送
- 裸 `codex` 仍然可单独使用，裸 `codex` 将使用自己的独立 backend，不在共享线程合同内

## 使用入口

| 入口 | 作用 | 什么时候用 |
| --- | --- | --- |
| 飞书聊天命令 | 当前 chat binding 的使用入口 | 在飞书里提问、切线程、改当前会话设置 |
| `focus` | 本地工作入口，接到同一实例 shared backend 的本地 Codex TUI | 想在本地继续飞书正在操作的同一 live thread |
| `focusctl` | 本地管理面 | 配置、启停、实例、binding、thread、prompt、image、清理 |
| `focusd` | daemon 入口 | 由 service manager 调用，通常不手敲 |

## 快速开始

### 前置条件

- Python 3.11+
- 本机已安装 `codex` CLI，且 `codex --help` 可正常执行
- 已在飞书开放平台创建应用，拿到 `app_id` 与 `app_secret`

### 1. 安装

macOS / Linux：

```bash
cd /path/to/focus
bash install.sh
```

Windows PowerShell：

```powershell
cd \path\to\focus
.\install.ps1
```

Windows 安装会把 `%LOCALAPPDATA%\focus\bin` 写入当前用户的 `PATH`；
通常新开一个 PowerShell / cmd 后即可直接发现 `focus`、`focusd`、
`focusctl`、`fcodex`。`focusctl uninstall` / `purge` 会对称移除
安装器自己加入的这一路径项。
若检测到稳定的 Codex 启动命令（如 `fnm` / `nvm`，或 Windows 上 `npm -g install @openai/codex`），
安装器也会把对应的 `codex_command` 自动写入真实 `codex.yaml`。
Windows 当前不安装 shell completion。

不要使用 `pip install .` 或 `pip install -e .`，这将安装无法被卸载命令 `focusctl uninstall/purge` 覆盖的残留命令入口。

从旧 `feishu-codex` 本地安装迁移时，只执行一条命令：

```bash
bash install.sh --migrate-from-feishu-codex
```

已安装 FOCUS 后也可执行 `focusctl migrate from-feishu-codex`。迁移是一次性 transfer；成功后主路径只使用 `focus` 目录、命令和 service。

### 2. 配置飞书应用

推荐先一次性配好权限、事件与回调。

#### 权限

权限用途概览

  - 初始化与机器人自识别: `/init`, `/bot-status`
      - `application:application:self_manage`
  - 用户与群成员身份识别: 群成员称呼, `/whoami` 身份信息获取
      - `contact:contact.base:readonly`
      - `contact:user.base:readonly`
      - `contact:user.employee_id:readonly`
  - 群聊名称读取: `focusctl binding list --refresh-names` 刷新 `CHAT` 列群名缓存
      - `im:chat:readonly`
  - 接收单聊与群聊消息
      - `im:message.p2p_msg:readonly`
      - `im:message.group_at_msg:readonly`
      - `im:message.group_msg`
  - 读取消息、发送回复、更新卡片
      - `im:message`
      - `im:message:readonly`
      - `im:message:send_as_bot`
      - `im:message:update`
  - 发送图片到飞书
      - `im:resource`

<details>
<summary>一键导入权限 JSON（点击展开）</summary>

在飞书开放平台「权限管理」页面点击「批量开通」，粘贴以下 JSON 即可导入当前建议权限集：

```json
{
  "scopes": {
    "tenant": [
      "application:application:self_manage",
      "contact:contact.base:readonly",
      "contact:user.base:readonly",
      "contact:user.employee_id:readonly",
      "im:chat:readonly",
      "im:message",
      "im:message.group_at_msg:readonly",
      "im:message.group_msg",
      "im:message.p2p_msg:readonly",
      "im:message:readonly",
      "im:message:send_as_bot",
      "im:message:update",
      "im:resource"
    ]
  }
}
```

</details>

#### 事件与回调

在「事件与回调」中启用：

- WebSocket 长连接模式
- 事件：`im.message.receive_v1`
- 事件：`im.message.recalled_v1`（用于撤回仍在队列中的消息）
- 回调：`card.action.trigger`

本项目默认走长连接，不需要公网 webhook URL。

### 3. 本地启动、配置、初始化

打开系统配置：

```bash
focusctl config system --open
```

按需写入 provider 环境变量：

```bash
focusctl config env --open
```

最小需要填的通常是：

- `system.yaml` 里的 `app_id`、`app_secret`
- `focus.env` 里的 provider key 或其他环境变量

启动服务：

```bash
focusctl service start
```

如需登录后自动启动：

```bash
focusctl service autostart enable
```

查看初始化口令：

```bash
focusctl config init-token
```

然后在飞书里私聊机器人：

```text
/init <token>
```

这一步会把当前发送者登记为管理员，并尝试写入当前机器人的 `bot_open_id`。
非管理员普通私聊默认不能直接使用机器人；但 `/whoami`、`/bot-status`、`/init <token>` 这类身份与初始化命令仍可在私聊使用。

### 4. 开始使用

在飞书里：

- 发送 `/help` 或 `/h` 看可用命令导航
- 发送 `/commands` 看可用命令列表
- 直接发送普通文本开始对话
- 手动发送命令 `/new`、`/resume`、`/cd` 管理当前会话绑定的 thread
- 如果想让同一个机器人同时服务多个项目，建议为每个项目单独建一个群聊；每个群聊固定在自己的目录和 thread 上，避免在单聊里反复 `/cd`、`/resume`
- 群聊里管理员先用 `/group activate` 激活，再按群模式使用

在本地继续同一个 live thread：

```bash
focus
focus resume <thread_id|thread_name>
focus --instance corp-a
```

或等价的

```bash
fcodex
fcodex resume <thread_id|thread_name>
fcodex --instance corp-a
```

说明：`--instance <name>` 只接受已创建的命名实例；如未创建，先执行 `focusctl instance create <name>`。

本地查看 / 管理：

```bash
focusctl service status
focusctl binding list
focusctl thread list
focusctl thread status --thread-name <name>
focusctl thread goal --thread-name <name>
focusctl image send --thread-id <thread_id> --path ./diagram.png
```

说明：`focusctl --instance <name> ...` 同样不会隐式创建命名实例。

#### 可选进阶

如果你已经完成基本安装与初始化，再看这部分：

- `focusctl prompt send --binding-id ...` 可向某个既有 Feishu 会话合成发起一轮 prompt；若不同会话各自绑定到不同 thread，它也可作为多个 thread 之间显式协作的控制面入口。
- `/goal` 用于查看或管理当前 thread 的 goal；常用形态包括 `/goal`、`/goal text`、`/goal set <objective>`、`/goal pause`、`/goal resume`、`/goal clear`。本地查看或排障时，可配合 `focusctl thread goal --thread-name <name>` 一起用。如需让机器人帮助生成 goal 主句，可先用 `/last text` 获取当前会话最近的权威终态文本，方便在手机侧复制后再整理成主句。
- `focusctl image send` 是 thread-scoped 动作：在 Codex turn 内可依赖自动注入的 `CODEX_THREAD_ID` 把图片发回当前 thread；若目标不是当前 thread，则必须显式提供 `--thread-id` 或 `--thread-name`。
- 若在 Codex turn 中已经有本地图片文件，可用 `feishu-send-image` skill 调 `focusctl image send`，把图片发回当前 thread 当前 attached 的飞书会话；这个 skill 不负责跨 thread / 手动选目标。

### 5. 多机器人多实例

如果你希望配置多个飞书应用及机器人，每个机器人对应不同的 FOCUS 实例，可按下面方式创建命名实例：

```bash
focusctl instance create corp-a
focusctl --instance corp-a config system --open
focusctl --instance corp-a service start
focus --instance corp-a
fcodex --instance corp-a
```

每个实例有自己的：

- 配置目录
- 数据目录
- service
- shared backend

额外说明：

- 命名实例只读取自己实例目录下的 `system.yaml` / `codex.yaml`
- 它不会继承 `default` 实例的 `codex.yaml`
- 真实 `codex.yaml` 是实例级 override 文件；通常只保留显式设置过的键
- 如需查看完整可配项，应看同目录 `codex.yaml.example`

所有实例共享：

- `CODEX_HOME`
- 持久化 thread 命名空间
- 机器级 `ThreadRuntimeLease`

## 更多帮助

- 飞书里发送 `/help` 或 `/h`
- 本地查看 `focus --help` 或 `fcodex --help`
- 本地查看 `focusctl --help`
- 深入文档看 `docs/doc-index.zh-CN.md`

## 一图看懂架构

```mermaid
flowchart LR
  subgraph Feishu["Feishu"]
    ChatA["单聊 / 群聊 A"]
    ChatB["单聊 / 群聊 B"]
  end

  Work["focus / fcodex<br/>本地继续同一 live thread<br/>local permissions"]
  CTL["focusctl<br/>本地管理"]
  Raw["裸 codex<br/>独立本地会话"]

  subgraph Instance["实例 explorer"]
    BindA["binding A<br/>binding-wise permissions"]
    BindB["binding B<br/>binding-wise permissions"]
    Service["FOCUS service"]
    Backend["shared codex app-server"]
    Thread["thread"]
  end

  Global["machine-global coordination<br/>ThreadRuntimeLease / instance registry"]

  ChatA --> BindA --> Service
  ChatB --> BindB --> Service
  Service --> Backend --> Thread
  Work --> Backend
  CTL -.查看/管理.-> Service
  CTL -.安装/配置/启停.-> Service
  Global -.协调.-> Backend
  Raw -.不在共享线程合同内.-> Thread
```

这张图只表达 3 件事：

- 飞书会话先绑定 `thread`
- `focus` / `fcodex` 连的是同一个实例 backend
- 裸 `codex` 不在共享线程合同内

## 一图看懂共享与冲突控制

```mermaid
flowchart LR
  subgraph A["实例 A：同一 live thread"]
    F1["Feishu binding 1<br/>(attached, own permissions)"]
    F2["Feishu binding 2<br/>(attached, own permissions)"]
    TUI["focus / fcodex subscriber<br/>(local permissions)"]
    Thread["thread"]
    Owner["current-instance<br/>interaction owner"]
  end

  subgraph B["实例 B"]
    Other["尝试 resume / attach<br/>同一 thread"]
  end

  Gate["cross-instance loaded gate"]
  Lease["ThreadRuntimeLease"]

  Thread -->|"普通输出广播"| F1
  Thread -->|"普通输出广播"| F2
  Thread -->|"普通输出广播"| TUI

  Owner -. "独占：下一轮写入 / 审批 / 补充输入 / 中断" .-> Thread

  Gate -. "先问其他运行中实例：<br/>这个 thread 是否仍 loaded" .-> Thread
  Lease -. "loaded gate 通过后，原子 claim：<br/>同一时刻只允许一个实例 live continuation" .-> Thread
  Other -. "跨实例 attach / resume" .-> Gate
  Gate -. "通过后再拿" .-> Lease
```

这张图表达的是当前运行时合同：

- 多个 `attached` 订阅者可以同时收到同一 thread 的 backend 普通消息
- 多订阅不等于多方都能写；真正的写入与交互控制由当前实例内的 `interaction owner` 独占
- 跨实例 attach / resume 会先过 `loaded gate`；若别的运行中实例仍把该 thread 保持为 `loaded`，就直接拒绝
- 只有 `loaded gate` 通过后，才会继续争抢机器级 `ThreadRuntimeLease`

**补充说明**
- `permissions`、`model`、`effort` 都属于 frontend-owned runtime settings：主要由发起该轮 `thread/start` / `turn/start` 的前端注入；仅在恢复未 loaded thread 时，cold `thread/resume` 会携带其中一小段 one-shot override 来保护恢复后的第一轮 autonomous turn。这不会把它们变成 thread-wise next-load 设置，也不会在飞书与本地 `focus` / `fcodex` 间自动同步持久化值；但显式值进入同一个 upstream thread 后，其他前端仍可能观察或覆盖
