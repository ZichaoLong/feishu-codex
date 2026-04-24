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

`/help` 根卡片必须只暴露三个一级入口：

- `session`
- `settings`
- `group`

根卡片可以为这三个入口提供简短说明，但不应在根卡片上平铺全部命令。

本地 `fcodex` 用法不属于飞书 `/help` 面。

## 3. 导航可达性的定义

“从 `/help` 可达”指的是：进入 `/help` 后，可以经过一级或多级按钮到达某个能力。

它不要求每个命令都直接出现在根卡片。

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

## 5. Session 面

`/help` 下的 `session` 分支负责线程与工作目录相关能力。

它必须让下列能力可达：

- `/session`
- `/new`
- `/resume <thread_id|thread_name>`，通过表单
- `/cd <path>`，通过表单
- 一个“当前线程”页面，用于当前绑定线程的操作

“当前线程”页面应覆盖：

- `/status`
- `/preflight`
- `/unsubscribe`
- 当前线程的 `/rename <title>`，通过表单
- 当前线程的 `/rm`

这里的“当前线程”页，仍然是**当前 chat binding** 的操作入口，不是全局 thread 管理页。

- `/status`、`/preflight` 与 `/unsubscribe` 即使在群里触发，仍按 chat-scoped 命令解释
- 如果需要按任意 thread 做 thread-scoped 管理，正式入口属于本地 `feishu-codexctl`

Help 面不需要再做一个“全局线程浏览器”或“全局归档表单”。

现有 `/session` 卡片继续作为“当前目录线程浏览 + 已列线程的 resume / archive 入口”。

## 6. Settings 面

`/help` 下的 `settings` 分支负责当前绑定 thread 的 profile 与当前 binding 的运行时设置。

它必须让下列能力可达：

- `/profile`
- `/permissions`
- `/approval`
- `/sandbox`
- `/mode`

同时应提供一个 identity / admin 子页，让下列能力可达：

- `/whoami`
- `/whoareyou`
- `/init <token>`，通过表单

## 7. Group 面

`/help` 下的 `group` 分支负责群聊专属规则与控制项。

它必须让下列能力可达：

- `/groupmode`

`/acl` 应在该页面中以文字说明和 slash 示例出现，但当前合同刻意不要求把它做成按钮或表单导航。

原因是：

- `grant` / `revoke` 通常需要 mention
- 一次操作可能涉及多个用户
- 直接使用 slash 语法比硬塞进不完整表单更清楚

群里触发的 `/status`、`/preflight`、`/unsubscribe`、`/profile` 等通用 Feishu 命令，不属于 `group` 分支。
它们仍分别归属 `session` 或 `settings` 分支；只是当执行上下文在群里时，仍需服从群命令触发规则。

## 8. 明确不纳入 `/help` 导航的命令

下列能力当前明确不要求从飞书 `/help` 纯导航到达：

- `/h`
- `/cancel`
- `/pwd`
- 本地 `fcodex` wrapper 命令

对应原因：

- `/cancel` 已经有执行卡片上的主入口
- `/pwd` 的信息基本已被“无参数 `/cd`”覆盖
- 本地 wrapper 用法应留在本地 help，不属于飞书 help

## 9. 权限与作用域语义

从 `/help` 触发命令时，必须保留与 slash 命令完全一致的访问规则。

包括：

- 仅私聊命令
- 仅群聊命令
- 群管理员限制

如果某个 slash 命令在当前上下文下会被拒绝，那么通过 `/help` 触发同一操作时，也必须被拒绝。

## 10. 关联文档

相关合同见：

- `docs/contracts/session-profile-semantics.zh-CN.md`
- `docs/contracts/runtime-control-surface.zh-CN.md`
- `docs/contracts/feishu-thread-lifecycle.zh-CN.md`
