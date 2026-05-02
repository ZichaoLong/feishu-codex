# 飞书命令矩阵

英文原文：`docs/contracts/feishu-command-matrix.md`

另见：

- `docs/contracts/feishu-help-navigation.zh-CN.md`
- `docs/contracts/session-profile-semantics.zh-CN.md`
- `docs/contracts/runtime-control-surface.zh-CN.md`
- `docs/contracts/group-chat-contract.zh-CN.md`

本文定义当前仓库维护的飞书侧一等命令面。

它回答五件事：

- 当前到底有哪些飞书 slash 命令
- 哪些命令可从 `/help` 导航到达
- 哪些命令刻意只保留为纯文字命令
- 私聊、群聊里分别谁可执行
- 各命令有哪些用户可见按钮，以及本地 `feishu-codexctl` / `feishu-codex` 是否有对应入口

如果代码行为与本文不一致，应把它视为合同缺口，并收紧代码、文档，或两者一起修正。

## 1. 范围

本文覆盖两类表面：

- 注册在飞书入口上的 slash 命令
- 与这些命令直接配套、用户可见的卡片按钮 / 表单动作

本文不重新定义：

- 线程生命周期
- `/status`、`/preflight`、`/unsubscribe` 的底层状态语义
- `/session`、`/resume`、`/profile` 的线程语义
- 进入 `fcodex` TUI 后的 upstream Codex 命令

这些内容分别以相关专题文档为准。

## 2. 阅读约定

### 2.1 “从 `/help` 可达”

“从 `/help` 可达”指的是：

- 可以从 `/help` 根卡进入某个主题页
- 再通过按钮、表单，或该命令返回的后续状态卡到达对应能力

它不要求命令直接出现在 `/help` 根卡片。

### 2.2 权限列的含义

表中的“私聊 / 群聊可执行者”按当前产品合同读取：

- `任何人`
  - 指当前上下文下，不要求管理员身份
- `仅管理员`
  - 指必须通过当前飞书机器人的管理员判定
- `不支持`
  - 指 scope 不允许，或该上下文下命令面本身不开放

特别注意：

- 群里“可直接提问”的普通成员，不等于“可执行群里的 slash 命令”
- 当前合同下，群里的 slash 命令和共享状态设置仍默认属于管理员面
- 普通群成员主要通过“直接提问”以及“处理自己发起 turn 的审批 / 补充输入卡片”参与协作

### 2.3 “本地对应”

“本地对应”只回答两件事：

- `feishu-codexctl` 是否提供相近的查看 / 管理入口
- `feishu-codex` 是否提供相近的安装 / service / 配置入口

如果真正的本地对应入口其实是 `fcodex`，也会显式注明，避免误以为它属于这两个管理 CLI。

## 3. Slash 命令矩阵

### 3.1 导航、当前会话与线程类

| 命令 | 作用 | `/help` 可达 | 私聊可执行者 | 群聊可执行者 | 用户可见按钮 / 表单 | 本地对应 |
| --- | --- | --- | --- | --- | --- | --- |
| `/help [chat\|group\|thread\|runtime\|identity]` | 显示帮助导航卡片或直接打开指定主题 | 是；根入口 | 仅管理员 | 仅管理员 | 根入口：`当前会话(chat)`、`群聊(group)`、`线程(thread)`、`运行时(runtime)`、`身份(identity)` | `feishu-codex --help`、`feishu-codexctl --help` 仅是本地帮助，不等同于飞书 `/help` |
| `/h` | `/help` 的文字别名 | 否 | 仅管理员 | 仅管理员 | 无 | 无 |
| `/pwd` | 查看当前工作目录 | 否 | 仅管理员 | 仅管理员 | 无 | 无 |
| `/status` | 查看当前 chat binding 的简明状态摘要 | 是；`/help -> chat` | 仅管理员 | 仅管理员 | 无 | `feishu-codexctl binding status <binding_id>` 可看更细状态；`feishu-codex` 无对应 |
| `/preflight` | dry-run 下一条普通消息与 `unsubscribe` 可用性 | 是；`/help -> chat` | 仅管理员 | 仅管理员 | 无 | 无一条完全等价命令；`feishu-codexctl binding status <binding_id>` 可看更细诊断 |
| `/cd [path]` | 无参数时显示当前目录；有参数时切目录并清空当前线程绑定 | 是；`/help -> chat` 表单 | 仅管理员 | 仅管理员 | help 表单提交 | 无 |
| `/new` | 立即创建新 thread，并切到当前 chat binding | 是；`/help -> thread` | 仅管理员 | 仅管理员 | 无 | 无 |
| `/session` | 查看当前目录线程列表 | 是；`/help -> thread` | 仅管理员 | 仅管理员 | 列表卡按钮：`恢复/当前`、`归档`、`更多`、`收起`、`展开会话列表` | `feishu-codexctl thread list --scope cwd` 最接近；`feishu-codex` 无对应 |
| `/resume <thread_id\|thread_name>` | 精确恢复指定 thread 到当前 chat | 是；`/help -> thread` 表单 | 仅管理员 | 仅管理员 | help 表单提交；`/session` 卡片中的 `恢复` 也走同一语义 | `feishu-codexctl` / `feishu-codex` 无对应；本地继续同一线程应使用 `fcodex resume <thread_id\|thread_name>` |
| `/profile [name]` | 查看或切换当前绑定 thread 的 thread-wise resume profile | 是；`/help -> thread -> 当前线程` | 仅管理员 | 仅管理员 | profile 名按钮；必要时附带 `应用并重置 backend` / `强制应用并重置 backend` | `feishu-codexctl` / `feishu-codex` 无直接对应；本地相关入口是 `fcodex -p <profile>` |
| `/rename <title>` | 重命名当前绑定 thread | 是；`/help -> thread -> 当前线程` 表单 | 仅管理员 | 仅管理员 | help 表单提交 | `feishu-codexctl` / `feishu-codex` 无对应 |
| `/rm [thread_id\|thread_name]` | 归档当前 thread，或归档指定 thread | 是；`/help -> thread -> 当前线程`，也可经 `/session` 列表卡 | 仅管理员 | 仅管理员 | `/session` 列表卡里的 `归档`；当前线程页也可直接执行 `/rm` | `feishu-codexctl` / `feishu-codex` 无对应 |
| `/unsubscribe` | 释放当前绑定 thread 的 Feishu runtime residency，但保留 binding | 否 | 仅管理员 | 仅管理员 | 无 | `feishu-codexctl thread unsubscribe --thread-id/--thread-name`；`feishu-codex` 无对应 |
| `/cancel` | 停止当前执行中的 turn | 否 | 仅管理员 | 仅管理员 | 执行卡片内有主入口按钮 `取消执行` | 无 |

### 3.2 运行时与身份类

| 命令 | 作用 | `/help` 可达 | 私聊可执行者 | 群聊可执行者 | 用户可见按钮 / 表单 | 本地对应 |
| --- | --- | --- | --- | --- | --- | --- |
| `/reset-backend` | 预览并重置当前实例 backend | 是；`/help -> runtime` | 仅管理员 | 仅管理员 | `重置 backend` 或 `强制重置 backend` | `feishu-codexctl service reset-backend`；`feishu-codex` 无对应 |
| `/permissions [read-only\|default\|full-access]` | 同时设置审批策略与沙箱策略 | 是；`/help -> runtime` | 仅管理员 | 仅管理员 | `read-only`、`default`、`full-access` | 无 |
| `/approval [untrusted\|on-request\|never]` | 单独设置审批策略 | 是；`/help -> runtime` | 仅管理员 | 仅管理员 | `untrusted`、`on-request`、`never` | 无 |
| `/sandbox [read-only\|workspace-write\|danger-full-access]` | 单独设置沙箱策略 | 是；`/help -> runtime` | 仅管理员 | 仅管理员 | `read-only`、`workspace-write`、`danger-full-access` | 无 |
| `/mode [default\|plan]` | 设置当前飞书会话后续 turn 的 Codex 协作模式 | 是；`/help -> runtime` | 仅管理员 | 仅管理员 | `default`、`plan` | 无 |
| `/whoami` | 查看自己的身份信息 | 是；`/help -> identity` | 任何人 | 不支持 | 无 | 无 |
| `/whoareyou` | 查看机器人身份、配置值与实时探测结果 | 是；`/help -> identity` | 任何人 | 仅管理员 | 无 | 无 |
| `/init <token>` | 初始化管理员与 `bot_open_id` | 是；`/help -> identity` 表单 | 任何人 | 不支持 | help 表单提交 | `feishu-codex config init-token` 只能查看 token，不等价于执行 `/init` |
| `/debug-contact <open_id>` | 排查通讯录名字解析、缓存命中与 fallback 原因 | 否 | 仅管理员 | 不支持 | 无 | 无 |

### 3.3 群聊专属类

| 命令 | 作用 | `/help` 可达 | 私聊可执行者 | 群聊可执行者 | 用户可见按钮 / 表单 | 本地对应 |
| --- | --- | --- | --- | --- | --- | --- |
| `/group` | 查看当前群是否已激活 | 是；`/help -> group` | 不支持 | 仅管理员 | 状态卡中可出现 `激活当前群`、`停用当前群` | `feishu-codexctl` / `feishu-codex` 无对应 |
| `/group activate` | 激活当前群聊 | 是；`/help -> group -> /group` 状态卡 | 不支持 | 仅管理员 | `/group` 卡片按钮 `激活当前群` | 无 |
| `/group deactivate` | 停用当前群聊 | 是；`/help -> group -> /group` 状态卡 | 不支持 | 仅管理员 | `/group` 卡片按钮 `停用当前群` | 无 |
| `/groupmode` | 查看当前群聊工作态 | 是；`/help -> group` | 不支持 | 仅管理员 | 状态卡中可出现工作态切换按钮 | 无 |
| `/groupmode assistant` | 切到 `assistant` | 是；`/help -> group -> /groupmode` 状态卡 | 不支持 | 仅管理员 | `/groupmode` 卡片按钮 `assistant` | 无 |
| `/groupmode all` | 切到 `all` | 是；`/help -> group -> /groupmode` 状态卡 | 不支持 | 仅管理员 | `/groupmode` 卡片按钮 `all` | 无 |
| `/groupmode mention-only` | 切到 `mention-only` | 是；`/help -> group -> /groupmode` 状态卡 | 不支持 | 仅管理员 | `/groupmode` 卡片按钮 `mention-only` | 无 |

## 4. 刻意保留为纯文字命令的项目

下列命令当前明确不要求从 `/help` 导航纯按钮直达：

- `/h`
- `/pwd`
- `/cancel`
- `/unsubscribe`
- `/debug-contact <open_id>`

原因分别是：

- `/h` 只是 `/help` 别名
- `/pwd` 已基本被“无参数 `/cd`”覆盖
- `/cancel` 的主入口是执行卡片里的 `取消执行`
- `/unsubscribe` 已被刻意弱化；面向用户的主路径应优先走 `/profile`
- `/debug-contact` 是排障命令，不属于常用导航面

## 5. 非 slash、但属于一等用户表面的卡片动作

这些动作不是 slash 命令，但它们是飞书侧正式支持的用户入口，必须与 slash 语义一起维护。

| 卡片表面 | 作用 | 谁可点击 | `/help` 可达 | 本地对应 |
| --- | --- | --- | --- | --- |
| 执行卡 `取消执行` | 停止当前 turn | 私聊：当前会话操作者；群聊：管理员或当前提问者 | 否 | 无 |
| 命令审批卡 `允许本次/允许本会话/拒绝/中止本轮` | 响应 command approval 请求 | 私聊：当前会话操作者；群聊：管理员或该请求的当前发起者 | 否 | 无 |
| 文件修改审批卡 `允许本次/允许本会话/拒绝/中止本轮` | 响应 file change approval 请求 | 私聊：当前会话操作者；群聊：管理员或该请求的当前发起者 | 否 | 无 |
| 额外权限审批卡 `允许本次/允许本会话/拒绝` | 响应 permissions approval 请求 | 私聊：当前会话操作者；群聊：管理员或该请求的当前发起者 | 否 | 无 |
| 补充输入卡 `选项按钮/自定义提交` | 回答 requestUserInput 问题 | 私聊：当前会话操作者；群聊：管理员或该请求的当前发起者 | 否 | 无 |

## 6. 本地命令面的职责边界

为了避免把飞书命令面和本地管理面混淆，当前合同明确如下：

- `feishu-codex`
  - 负责安装、service 生命周期、自启动、实例管理、配置入口
  - 它不是飞书会话 / 线程操作面
- `feishu-codexctl`
  - 负责本地查看 / 管理 service、binding、thread
  - 它不是第二个 Codex 前端
- `fcodex`
  - 才是本地继续 live thread、进入 Codex TUI 的入口

因此，下列期待当前不成立：

- 不能把 `feishu-codex status` 理解成飞书 `/status`
- 不能把 `feishu-codexctl` 理解成飞书 `/session` 的等价 UI
- 不能期待 `feishu-codex` 或 `feishu-codexctl` 提供飞书 `/new`、`/rename`、`/rm` 这类 chat-scoped 交互命令

## 7. 关联事实源

当前文档对应的主要实现事实源包括：

- `bot/codex_handler.py`
- `bot/inbound_surface_controller.py`
- `bot/codex_help_domain.py`
- `bot/codex_session_ui_domain.py`
- `bot/codex_settings_domain.py`
- `bot/codex_group_domain.py`
- `bot/cards.py`
- `bot/feishu_codexctl.py`
- `bot/manage_cli.py`

如果未来新增、删除、改名任何飞书命令，或改变 `/help` 导航可达性、群管理员边界、按钮权限、以及本地 CLI 对应关系，都应同步更新本文。
