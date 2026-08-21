# Subagent 上游生命周期与展示边界合同

文档角色：中文规范源。英文同步副本：`docs/contracts/subagent-observation-and-recovery.md`。

> 上游基线：
> [`openai/codex@be6e8eac029b183056b7e4402879f15d2c85f61b`](https://github.com/openai/codex/commit/be6e8eac029b183056b7e4402879f15d2c85f61b)
> （`rust-v0.147.0`）。

本文定义 Focus 与 Codex spawned subagent 的最小边界。Focus 不拥有 child
lifecycle、parent mailbox 或 agent tree 完整性，也不定义 root writer 与 tree-stop。

## 1. 上游 ownership

Codex 拥有 thread identity、source/parent relation、loaded status、child runtime、
history、pending callback 与 direct-input policy。live child 结束时，Codex 会以
`trigger_turn=false` 把结果送入仍加载的 direct parent mailbox；它不会自动启动
parent 的新 turn。

matching root `turn/completed` 立即结束 root main turn 并释放 writer。child active、
pending callback、迟到 completion 或展示状态都不能延长 root turn、阻塞飞书 FIFO、
延迟终态卡或阻止下一次 root input。

Focus 不扫描、重建、续跑、补投或持久化 child lifecycle，也没有 child→root
registry、reconcile worker 或 subagent shutdown barrier。

## 2. Direct target 边界

MultiAgent V2 `ThreadSpawn` child 由 parent 拥有。Focus 在任何 frontend mutation、
resume、interrupt、archive、delete 或 bind 前读取权威 `ThreadSummary`；若
`subagent_kind=threadSpawn`，直接写入必须拒绝。用户应继续 root，再由 root agent
使用上游 collaboration tools 调度 child。

`Thread.source` 只有在上游 enum/object 形状可验证时才是可用证据；unknown、缺失或
malformed source 仍可作为受限展示数据，但不得被猜成普通 root，也不得取得 mutation、
resume、interrupt、lease、binding 或 shared approval authority。`fcodex` 的 metadata-only
child read 还要求结构化 `ThreadSpawn` 携带非空 parent 与合法 depth；否则同样拒绝。

`fcodex` 只保留上游式的严格只读例外：对已权威证明的 ThreadSpawn 允许
`thread/read(includeTurns=false)` metadata read。该例外不创建 child owner、runtime
interest、writer、subscription 或 mutation path。回答一个仍 pending 的 exact
callback 也不等于启动 child turn。

## 3. Surface projection

Focus Web 的通用 Tasks UI 只投影 parent history 已经记录的 collaboration items，
例如 `collabAgentToolCall` 与 `subAgentActivity`。这些 history facts 可以帮助查看上游
已经暴露的 task/result，但不证明完整、原子的 agent tree，也不参与生命周期准入。

Focus 不发布 live child delta、全局 inventory/health banner 或 child 核对状态的飞书
notice。飞书只结算 root execution；`fcodex` 继续使用上游 TUI 的原生呈现。

## 4. Unknown child callback

Focus 只把经权威 `ThreadSummary` 证明的 direct-root id 记入当前进程的最小集合，
用于判断 shared approval 是否可面向多个 surface。该集合不保存 parent relation、
child 状态、结果、runtime lifecycle 或恢复意图，并在 connection epoch 失效时清空。

无法证明为 direct root 的 callback 不得升级成 shared approval，也不得猜测 root、
取得 root lease、重绑 root、启动 observer/retry 或发送 notice。Web 与 `fcodex`
decline；飞书作为最后 surface 时，只对 canonical exact callback 提交 fail-close。
任何 unknown outcome 只围栏该 exact callback。

## 5. 冷恢复限制

裸 Codex cold resume 只恢复所选 root；仍 loaded 的 child 可以由上游 inventory/read
再次呈现，但不会递归重开整棵 child tree。若 direct parent 已从当前
`ThreadManager` 卸载，或 app-server 已退出，迟到 child completion 的内存 mailbox
投递可能丢失。Focus 诚实接受这一窄边界，不把 child history 静默注入 root，也不
宣称可恢复所有延后结果。

修改本领域至少应覆盖：root completion 不等待 child；ThreadSpawn direct write
继续拒绝；metadata-only read 不创建 owner；unknown child callback 不进入 shared
approval 且不创建 root lease/notice/worker；parent-history Tasks 仍可展示；service
shutdown 没有 child-lifecycle worker 或 barrier。
