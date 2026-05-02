# 飞书 Help 导航合同

英文原文：`docs/contracts/feishu-help-navigation.md`

本文定义飞书侧 `/help` 的导航面合同。

它回答三件事：

- 哪些命令应可从 `/help` 导航到达
- 哪些命令刻意不放进 `/help`
- 按钮 / 表单 与 slash 命令之间必须保持什么关系

如果实现与本文不一致，应把它视为合同缺口，并收紧实现、文档，或两者一起修正。

## 1. 范围

本文只描述飞书侧 help 与导航面。

它不重新定义：

- 线程生命周期
- runtime 控制面语义
- session / profile 语义
- 本地 `fcodex` 的 help

这些内容分别以各自专题文档为准。

## 2. 根结构

飞书 `/help` 是导航入口，不是平铺所有命令的总清单。

`/help` 根卡片必须按如下顺序暴露五个一级入口：

- `当前会话`，对应文字主题 `chat`
- `群聊`，对应文字主题 `group`
- `线程`，对应文字主题 `thread`
- `运行时`，对应文字主题 `runtime`
- `身份`，对应文字主题 `identity`

根卡片可以为这五个入口提供简短说明，但不应在根卡片上平铺全部命令。

本地 `fcodex` 用法不属于飞书 `/help` 的独立页面；如有必要，只能在概览页或线程页里作为文字提示出现。

## 3. 导航可达性的定义

“从 `/help` 可达”指的是：进入 `/help` 后，可以经过一级或多级按钮到达某个能力。

它不要求每个命令都直接出现在 `/help` 根卡片。

当多级导航能显著减少拥挤、澄清职责时，应优先采用多级导航。

## 4. 语义等价规则

Help 按钮和表单的交互形态可以不同，但行为语义不能另起一套。

因此：

- 由按钮触发的命令，必须复用与 slash 命令相同的命令语义
- 表单只能负责补齐参数，提交后仍必须回到同一条命令路径
- `/help` 导航不能再写一份平行的业务实现

允许不同的返回形态：

- slash 命令可以发送新消息
- 卡片动作可以更新当前卡片或弹 toast

但底层操作、校验、scope guard、状态迁移必须等价。

同时，help / 导航卡片的 payload 也必须保持最小且显式：

- 路由键是 `action`
- payload 里只放目标 action 实际会消费的参数
- `plugin`、bot keyword 或其他部署标识字段不属于回调合同，路由时不得依赖它们

## 5. 当前会话面

`/help` 下的 `chat` 分支负责**当前 chat binding** 的状态与目录控制。

它必须让下列能力可达：

- `/status`
- `/preflight`
- `/cd <path>`，通过表单

这个分支可以跳转到“线程”页，但不承担 thread 管理职责。

这里的 `/status` 与 `/preflight` 仍然是 chat-scoped 命令：

- 即使在群里触发，也仍按当前 chat binding 解释
- 它们不等于全局 thread 管理入口

## 6. 群聊面

`/help` 下的 `group` 分支负责群聊专属规则与控制项。

它必须让下列能力可达：

- `/group`
- `/group-mode`

`group` 页的文字说明应覆盖：

- 群默认是“未激活”
- `/group activate` 与 `/group deactivate` 的用途
- `assistant`、`mention-only`、`all` 三种群聊工作态
- 群成员日常使用、共享状态管理、审批卡片处理三者的权限边界

如果实现保留 `/group` 状态卡和 `/group-mode` 状态卡上的后续按钮，
那么 `/group activate`、`/group deactivate`、`/group-mode <mode>` 也属于
“从 `/help` 可达”的能力，只是它们不要求直接铺在 help 页面上。

## 7. 线程面

`/help` 下的 `thread` 分支负责 thread 浏览、创建、恢复与当前线程管理。

它必须让下列能力可达：

- `/threads`
- `/new`
- `/resume <thread_id|thread_name>`，通过表单
- 一个“当前线程”页面，用于当前绑定 thread 的操作

“当前线程”页面应覆盖：

- `/profile [name]`
- 当前线程的 `/rename <title>`，通过表单
- 当前线程的 `/archive`

这里的“当前线程”页，仍然是**当前绑定 thread** 的操作入口，不是全局 thread 管理页。

现有 `/threads` 卡片继续作为“当前目录线程浏览 + 已列线程的 resume / archive 入口”。

`/release-runtime` 当前明确不要求从 `/help` 作为一等导航能力暴露：

- re-profile 的主路径应由 `/profile [name]` 承担
- 如需排障或本地管理，可以在文字说明中提示 `feishu-codexctl`，但不要求独立 help 按钮

## 8. 运行时面

`/help` 下的 `runtime` 分支负责当前飞书会话的运行时设置，以及当前实例 backend 的实例级控制。

它必须让下列能力可达：

- `/permissions`
- `/approval`
- `/sandbox`
- `/collab-mode`
- `/reset-backend`

`/profile` 不属于这一层。它是当前 thread 的属性，必须留在“线程 -> 当前线程”路径下。

## 9. 身份面

`/help` 下的 `identity` 分支负责身份与 bootstrap。

它必须让下列能力可达：

- `/whoami`
- `/bot-status`
- `/init <token>`，通过表单

`/debug-contact <open_id>` 不属于常规 help 导航面，不要求从 `/help` 可达。

## 10. 明确不纳入 `/help` 导航的命令

下列能力当前明确不要求从飞书 `/help` 导航到达：

- `/h`
- `/cancel`
- `/pwd`
- `/release-runtime`
- `/debug-contact <open_id>`
- 本地 `fcodex` wrapper 命令

对应原因：

- `/h` 只是 `/help` 别名
- `/cancel` 已经有执行卡片上的主入口
- `/pwd` 的信息基本已被“无参数 `/cd`”覆盖
- `/release-runtime` 已被刻意弱化；面向用户的主路径应优先走 `/profile`
- `/debug-contact` 是排障命令，不属于常用导航面
- 本地 wrapper 用法应留在本地 help，不属于飞书 help

## 11. 权限与作用域语义

从 `/help` 触发命令时，必须保留与 slash 命令完全一致的访问规则。

包括：

- 仅私聊命令
- 仅群聊命令
- 群管理员限制
- 非管理员普通私聊默认拒绝
- 但 `/whoami`、`/bot-status`、`/init <token>` 作为身份 / bootstrap 命令，必须仍可在私聊直接触发，不能先被通用“仅管理员私聊”守卫吞掉

如果某个 slash 命令在当前上下文下会被拒绝，那么通过 `/help` 触发同一操作时，也必须被拒绝。

## 12. 关联文档

相关合同见：

- `docs/contracts/thread-profile-semantics.zh-CN.md`
- `docs/contracts/runtime-control-surface.zh-CN.md`
- `docs/contracts/feishu-thread-lifecycle.zh-CN.md`
