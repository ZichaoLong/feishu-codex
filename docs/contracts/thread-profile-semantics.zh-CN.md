# Thread 与 Resume 语义

英文原文：`docs/contracts/thread-profile-semantics.md`

本文件保留历史文件名，但已不再定义任何项目自管 `profile` 功能。它现在记录
`/threads`、`/resume`、`/archive` 与本地 shared-backend continuation 的语义。

## 1. 当前范围

本文定义的是：

- 飞书侧 thread 浏览怎么工作
- `/resume` 现在承诺什么
- `/archive` 会改什么
- 本地 `focus resume` / `fcodex resume` 在 shared-backend 模型里的含义

本文不定义：

- 任何项目自管 profile 设置
- 任何项目自管 thread-wise next-load 设置
- 对上游 `codex --profile` 的本地镜像

## 2. thread identity 与 ownership

本项目始终区分三件事：

1. thread identity
   - 来自上游 Codex 的 thread 元数据
2. Feishu binding
   - 决定当前聊天逻辑上指向哪个 thread
3. live runtime ownership
   - 决定哪个 backend 当前实际承载这个 loaded thread

这三者不得混淆。

## 3. `/threads`

`/threads` 是当前工作目录的 thread 浏览面。

它会：

- 列出当前目录上下文里的候选线程
- 帮助用户选择后续要 resume 或 archive 的线程
- 自身不直接改 runtime settings

## 4. `/resume`

`/resume <thread_id|thread_name>` 现在只承诺：

- 解析目标线程
- 在 live reuse 之前做跨实例安全准入
- 对着正确的 backend 做恢复
- 把当前飞书会话绑定到该线程

它不再承诺：

- 回放项目自管的 profile slice
- 回放项目自管的 memory/provider slice
- 重建任何由本项目拥有的 thread-level setting layer

如果目标线程已经加载在当前 backend 中，resume 直接复用该 live runtime。
如果当前未加载，则在通过仓库定义的安全闸门后，调用上游 `thread/resume` 恢复。

## 5. `/archive`

`/archive [thread_id|thread_name]` 用于归档当前线程或显式指定的目标线程。

它会：

- 改 Codex 里的 thread archive 状态
- 在当前线程被归档时，按需要清理或更新当前 binding

它不会：

- 修改 runtime-setting family
- 隐含任何 profile 或 memory 语义

若上游 archive 请求在发送后发生 timeout 或 transport 断开，Focus 会把结果标记为
`unknown`，保留 binding 且不自动重试。上游明确成功但本地 binding / lease 清理失败时，
Focus 会明确报告“已归档、清理不完整”，而不是把两层结果合并成一个模糊失败。

归档前，Focus 会 fail-closed 检查本机已知其他 Focus 实例是否仍将 root thread 保持为
loaded；若存在 blocking instance，应改在该实例执行或先明确 reset 其 backend。该检查只覆盖
本机已登记的 Focus/fcodex runtime，不检测裸 Codex、IDE 或其他机器，也不是跨客户端原子锁。

## 6. 本地 `focus` / `fcodex` continuation

`focus resume <thread_id|thread_name>` 与 `fcodex resume <thread_id|thread_name>` 是 live shared-backend thread 的本地继续入口。

它承诺：

- 相同的 thread identity 解析模型
- 相同的跨实例 loaded/runtime 安全检查
- 把本地 TUI continuation 接到正确的 backend 上

`focus -p/--profile` 与 `fcodex -p/--profile` 仍只保留为上游 Codex 的启动参数。
本项目不会持久化它、不会把它映射进飞书，也不会把它当成 thread truth。

## 7. 非目标

本项目不再承诺：

- “飞书 `/resume` 会回放旧 thread profile”
- “飞书与 `focus` / `fcodex` 共享一个项目自管的 profile 事实源”
- “thread unloaded 后仍带着一个项目自管的 next-load profile 层”

当前合同是刻意收窄的：

- thread identity 归上游所有
- resume safety 归本仓库所有
- turn-time override 归 binding 所有
