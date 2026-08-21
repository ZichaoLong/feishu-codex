# 跨实例 Live Runtime 准入决策

文档角色：中文规范源。英文同步副本：`docs/decisions/cross-instance-live-runtime-admission.md`。

另见：

- `docs/contracts/root-operation-owner.zh-CN.md`
- `docs/contracts/runtime-control-surface.zh-CN.md`
- `docs/contracts/local-command-and-thread-profile-contract.zh-CN.md`
- `docs/architecture/focus-shared-backend-runtime.zh-CN.md`

## 1. 状态

本文是已接受、适用于已注册本机 Focus 与 `fcodex` runtime 的实现合同。当前 runtime 已在所覆盖的
attach、resume 与 lifecycle 路径应用“loaded gate + lease”准入模型；各具体入口仍以本文链接的合同矩阵为准。

本文不声称能发现、锁定或协调裸 Codex client、IDE、另一台机器或任何未注册 app-server；它们仍在本机
协调边界之外。

## 2. 问题

当前机器级 `ThreadRuntimeLease` 不足以单独承担跨实例安全准入。

原因是：

- 上游 app-server 在最后一个 subscriber `unsubscribe` 后，仍会把 thread 保持
  `loaded` 约 30 分钟
- 后续的 `thread/resume` 可能直接复用这份已经加载的内存态 thread
- 因此 `lease == none` 并不推出 `backend == notLoaded`

这会带来真实的跨实例 stale-loaded 风险：实例 A 可能还留着旧的内存态
thread，而实例 B 已经基于持久化历史继续推进了对话。

## 3. 决策

### 3.1 产品合同

- thread visibility 继续全局共享
- live continuation 必须实例独占
- 跨实例迁移只支持 `cold migration only`
- 不支持跨实例 live takeover / 自动转移

### 3.2 准入模型

本节只解决“哪个 instance backend 可以持有 live runtime”，不决定具体 effect 是否获准。
prompt、resume、goal、interrupt、server request 等 effect 仍分别服从各自正式合同；下面两层
runtime 检查不能替代任何 method-specific authority：

1. `global loaded gate`
   - 跨实例 `attach / resume` 之前，必须先验证是否仍有其他运行中的实例报告该
     thread 为 `loaded`
   - 只要别的运行中实例仍报告它 `loaded`，就拒绝
   - 如果系统无法验证这个事实，也拒绝
2. 原子 `ThreadRuntimeLease` claim
   - 只有 loaded gate 通过后，当前实例才允许继续争抢机器级 runtime lease
   - 这层仍然保留，用来防止两个实例几乎同时观察到全局 `notLoaded` 后并发
     `resume` 的竞态

### 3.3 `ThreadRuntimeLease` 的含义

`ThreadRuntimeLease` 继续保留，但角色收窄为内部协调原语：

- 它不再是跨实例安全准入的唯一事实源
- 它是机器级原子 claim，用来阻止并发 cold-resume / backend materialization 竞态
- 它承载 holder 元数据，例如 `service` / `fcodex`

用户侧心智模型应优先理解成：

- “另一个运行中的实例仍把这个 thread 保持在 loaded”
- 而不是“另一个实例持有了 lease”

### 3.4 持久协调状态的可用性

instance registry 与 thread-runtime lease ledger 是准入权威，不是可随时丢弃的 cache。其持久化合同为：

- 状态文件不存在，才表示尚未创建任何记录
- JSON 损坏、文件不可读、结构不合法或出现当前实现不支持的未来 schema，均表示
  `unavailable`，不得解释为空 registry 或空 lease 集合
- registry unavailable 时，global loaded gate 必须拒绝；runtime-lease ledger unavailable
  时，任何新 claim 也必须拒绝
- 普通读取、注册、claim、release 与自动清理不得以“恢复”为由顺带覆盖 unavailable 状态；
  恢复必须来自操作者明确的 repair / cleanup 决策
- 合法的历史无版本格式只作为单向升级输入；此后的新写入必须携带当前 schema version

PID 存活本身不构成进程身份，因为 PID 会被复用。新协调记录必须把正数 owner PID 与操作系统提供的
process-incarnation identity 绑定。只有 PID 已消失，或能够确定 incarnation 不一致时，才允许自动清理
stale 记录；如果当前平台无法验证 incarnation，则保守保留记录，不得用不确定性制造准入许可。

### 3.5 Web 目录投影与 exact open

Web 全局目录的 loaded 标注必须使用同一组本机受管运行实例的 process-local loaded inventory，
而不是把 runtime lease 当作完整 inventory。每实例一次查询必须显式区分 verified result 与
error；失败不能被解释为空集合。目录可以请求时聚合这些 verified snapshots，但它只是 advisory
presentation，不持久化、不轮询，也不替代 exact loaded gate。

因此，另一个实例中无 lease 但仍 idle-loaded 的 root thread 仍显示为 loaded elsewhere；无法
验证的实例状态显示为 unknown/unverified，且不能从该目录发起 materialization。
普通 persisted list 本身有界，因此已验证的 remote loaded id 若不在该页内，可以执行 metadata-only
补读；补读数量受同一 directory owner limit 约束，仍经过既有 root-only 过滤，且显式 search 不因
这些补读扩大结果。

用户点击与目录读取之间可能发生变化，最终准入仍逐 thread 重新执行第 3.2 节的 loaded gate。
明确的 loaded elsewhere 拒绝使用 HTTP 409 `thread_loaded_elsewhere`；registry、control-plane 或
其他 loaded-state 验证失败（包括 exact loaded check 之后的原子 runtime-lease claim 在竞态中失败）
则使用 HTTP 503 `thread_runtime_unverified`，保留当前 Web selection，
且不执行 resume 或 lease mutation。内部类型或 presentation 不能把任一情况降成 500，也不能在没有
观察到 owner 时谎称已证明 owner。这个投影仍只覆盖 registry 中受管的本机 Focus instances。

严格的 `thread/loaded/list` control read 不回入目标实例的 `RuntimeLoop`：发起查询的 Web list 已在
自己的 loop 内等待其他实例，两个实例同时刷新全局目录时若各自回入对方 loop，就会形成相互等待。
control request 直接通过目标实例线程安全的 RPC owner 读取，只使用已有连接，且 RPC timeout 短于
fan-out 的单一总 deadline。与普通 outbound call 一样，该读取先获取当前 outbound epoch permit，
将 actual-send guard 带入 transport，且只在该 exact epoch 仍然有效时接受 success 或已解码的
JSON-RPC error。它不能启动或重连 backend，不新增 snapshot owner；control-plane shutdown 仍会
先 drain 该请求，再停止 adapter。

## 4. Attach 合同

### 4.1 binding / thread / service attach

所有 attach 入口都必须服从同一套 loaded gate。入口随后执行的 resume、goal 或其他 effect
仍必须通过对应的 method-specific authority；本节的 gate 只判断 backend residency。

- `binding attach`：只有目标 thread 通过 gate 才允许
- `thread attach`：只有目标 thread 通过 gate 才允许
- `service attach`：是实例级批量恢复，但失败判断粒度必须是 thread

### 4.2 service attach 结果形状

`service attach` 应满足：

- 批量恢复当前实例内所有 detached bindings
- 实际处理时按 thread 分组
- 每个 thread 要么为本实例完整恢复，要么整条阻塞
- 不同 thread 之间允许部分成功
- 被阻塞的 thread 必须明确列出原因

也就是：

- 实例级批量恢复
- thread 级 fail-close
- 结果层允许部分成功

## 5. 操作面含义

- 只要另一个运行中实例还有可能保留这个 thread 的 loaded 内存态，就不做自动跨实例继续
- 源实例 reset、等待 idle unload、或显式 cold migration 都是可以接受的用户路径
- 只要 loaded 状态无法被证明安全，就必须让位于 fail-close，而不是让位于便利性

## 6. 已覆盖路径与维护规则

当前实现将本决策应用到：

- Feishu attach 相关路径
- detached binding 的自动 attach / re-attach 路径
- 本地 `focus resume` / `fcodex resume` 中与跨实例 loaded 冲突有关的准入逻辑
- 状态展示与拒绝文案，让用户看到的是“loaded elsewhere”，而不是只看到
  lease 术语

任何新增的、会 load、resume、attach 或以其他方式 materialize live thread runtime 的路径，都必须在到达
app-server 前接入同一条 loaded gate、原子 lease claim，以及该 effect 自己的正式准入合同。
