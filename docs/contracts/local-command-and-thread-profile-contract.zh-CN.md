# 本地命令与运行时设置合同

文档角色：中文规范源。英文同步副本：`docs/contracts/local-command-and-thread-profile-contract.md`。

本文件保留历史文件名，但已不再定义任何项目自管 `profile` 面。它现在定义本地
入口与剩余设置模型之间的边界。

## 1. 四个本地入口

### 1.1 `focus`

负责：

- 进入本地 Codex TUI
- 恢复或接入 live thread
- 作为实例 shared backend 的本地前端

它不是：

- service-management CLI
- 项目自管设置面

### 1.2 `fcodex`

`fcodex` 是 `focus` 的等价别名，保留“Codex TUI thin wrapper”的直观语义。

负责：

- 与 `focus` 完全相同的本地 Codex TUI wrapper 行为
- 给习惯 Codex 专用入口的操作者保留稳定入口

它不是：

- 另一套 agent CLI
- 与 `focus` 分离的 runtime 或状态面

### 1.3 `focusctl`

负责：

- 安装与升级后的本地修复
- service 生命周期
- 实例管理
- 查看 instance / binding / thread / service 状态
- 执行有限的本地管理动作
- 诊断 attach / detach / backend 问题

它不是：

- turn settings 的第二前端
- 飞书设置卡片的本地镜像
- Codex TUI

### 1.4 `focusd`

负责：

- 作为后台 daemon 入口被 service manager 调用

它不是：

- 人日常手敲的管理命令
- 本地 Codex TUI wrapper

## 2. 飞书与 Web 的可写设置边界

### 2.1 binding-wise next-turn settings

- scope：Feishu binding
- 飞书入口：`/model`、`/effort`、`/approval`、`/permissions`
- 本地 `focus` / `fcodex` / 上游 TUI 仍保持各自本地状态，不会自动与飞书侧持久化 binding 设置合并

### 2.2 Web 有独立的 instance-wide next-turn settings

Focus Web 的 model、effort、approval 与 permissions 属于一份 durable `WebNextTurnSettings`，由同一 Focus
instance 的所有 browser、F5 后 document 与 thread 共享。它不是 Feishu binding 或 local-TUI profile，也不会自动与
Feishu、`focus` 或 `fcodex` 合并。main-turn lease 只授权具体 submission/active turn；它不拥有这份设置，也不阻止
connected browser 为下一次 eligible Web turn 修改设置。

具体 seed、持久化、mutation 与消费边界只由
[`runtime-settings-fact-sources.zh-CN.md`](./runtime-settings-fact-sources.zh-CN.md) 定义。本地命令合同只明确：
`focus` / `fcodex` / `focusctl` 都不是这份 Web 设置的写入口，main-turn lease 也不拥有它。

独立的 Web navigation state 中，`WebWriterProfileStore.selected_thread_id` 是唯一 durable semantic selection。
Web `/cd`、attachment scope、meta 与 scope generation 只读取该值，绝不读取进程缓存作为替代。
独立的 `WebDocumentRegistry.materialized_thread_id` 只证明 bounded-history readiness；加载 older history
要求两个值都等于请求 target。desired subscription edge 只归 `WebRuntimeInterestRegistry` 所有，不是
另一份 selection。

当上游使已选 target 变得不可用时，Focus 只把 exact durable match 原子清为 draft，并让 generation 恰好
加一；重复清理是 no-op。它会保留 replacement materialization，并收敛每个被清 document 的全部
runtime-interest edge。这种自动 clear 不等同于用户请求的 same-cwd `/cd` rebind：archive、not-found、
loaded-elsewhere records 继续 isolated 在旧 thread scope；确认 delete 与非法 direct `ThreadSpawn` 则删除
该 scope。commit 后的 `profile_changed` invalidation 不携带 profile 副本，浏览器必须重读自己的 meta。navigation
generation 与 settings generation 互不排序或结算。

## 3. 已移除的项目自管设置

本项目已不再支持：

- 历史上的项目自管 profile 命令
- `/memory`
- `focusctl thread memory`
- 任何项目自管的 thread-memory / provider restore 语义

如果操作者想使用上游 profile/provider 行为，应直接使用上游 Codex 配置、
上游 profile-v2 文件，或上游启动参数。

## 4. `focus` / `fcodex -p/--profile` 的当前含义

本项目不再把 `focus -p/--profile` 或 `fcodex -p/--profile` 视为持久化写入口。

它现在只是：

- 上游 / 本地 TUI 的启动参数
- 不是任何飞书命令的本地镜像
- 不是本项目持久化成 thread truth 的东西

## 5. `focus resume` / `fcodex resume` 仍承诺什么

`focus resume <thread_id|thread_name>` 与 `fcodex resume <thread_id|thread_name>` 现在仍承诺：

- thread identity 解析
- live-runtime-owner / loaded-gate fail-close 行为
- 接到正确的实例 backend

它们是 continuation entry point，不是 ownership credential。live `fcodex` endpoint 的普通
`turn/start` 只要求 exact direct root 与 exact request tracking，随后保持上游 start-or-steer 语义，
不读取或取得 main-turn lease。review、compact、autonomous continuation，以及可能 autostart 的 resume
仍分别服从 [main-turn owner 合同](root-operation-owner.zh-CN.md)与
[`fcodex` owner 合同](fcodex-operation-owner.zh-CN.md)定义的 method-specific admission。live `fcodex`
endpoint attach exact direct root 后，可以按 effect-specific 边界 steer 或 interrupt 该 root 的 exact
current turn，但不会取得或转移 writer；回答 server request 则须通过 `server-request-lifecycle.zh-CN.md`
定义的 exact callback admission。自己原本是 observer、看不到明显 live frontend，或观察到
Web/Feishu/`fcodex` surface 已断线，都不授予更宽的 active-turn takeover authority；`resume` 本身既不是
takeover，也不是 writer credential。

它不再承诺：

- 恢复项目自管的 profile slice
- 恢复项目自管的 memory/provider slice

## 6. 一条维护规则

如果以后本项目要引入一个新设置，必须先明确它的 owner、frontend scope 与生效边界，并且只归类为：

1. binding-wise next-turn settings
2. instance-wide `WebNextTurnSettings`
3. 另一份有正式合同的明确 owner/scope
4. 只读 upstream/diagnostic 视图

在这个归类存在之前，本项目不得为它新增本地命令面或隐式跨前端 setting。
