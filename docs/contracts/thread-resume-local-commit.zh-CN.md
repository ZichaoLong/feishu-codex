# `thread/resume` 本地提交合同

文档角色：中文规范源。英文同步副本：`docs/contracts/thread-resume-local-commit.md`。

> 状态：已采纳的跨入口合同。本文只定义一次 `thread/resume` 调用与其本地 Focus
> 提交之间的即时边界；它不定义 durable recovery 状态机或 thread-wide mutation gate。

## 1. 范围与上游基线

Codex app-server 可能在返回成功 `thread/resume` response 前，已经改变当前 connection
的 subscription 状态。Focus 因而需要排好两个事实的顺序，但不能把它们伪装成 durable
distributed transaction：

1. 上游返回 typed resume success；
2. 发起请求的 Focus surface 提交紧随其后的本地后果。

当前复核的公开上游基线是 Codex CLI `0.147.0`：
[`openai/codex@be6e8eac029b183056b7e4402879f15d2c85f61b`](https://github.com/openai/codex/commit/be6e8eac029b183056b7e4402879f15d2c85f61b)。
其 [running resume owner](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/thread_processor.rs#L3480-L3734)
在同一 listener 顺序内取得 active snapshot、增加 connection subscriber 并返回 response；
[subscriber 与 response 的原子排序](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/thread_lifecycle.rs#L528-L755)
发生在后续 live notification、goal snapshot 与 pending server-request replay 之前。上游不持久化
frontend resume journal，也不会因 resume response 丢失而隔离后续 thread mutation。Focus 对齐该基线。

本文适用于所有经 `ThreadRuntimeAuthority` 发出的 snapshot 与 paged resume，包括飞书
attach/goal、Web cold open 与 RuntimeAdmin attach。main-turn 准入仍由
`root-operation-owner` 管理；若 resume 可能自主启动 turn，调用点必须持有普通的 exact blank
main-turn lease。唯一的 method-specific 例外是 detached 飞书 binding 在权威 preflight 与
immediate pre-send guard 都确认 direct-root 已 active 时执行 active-observer attach：该调用不携带
model、approval、permissions 等 override，也不取得或转移 writer。

## 2. 唯一 resume 事务

```text
取得或确认 machine runtime lease
  -> 失效 request 显式携带的 settings intent，并准备 exact pre-send guard
  -> 发送一次 typed thread/resume request
  -> 收到有效 success response
  -> 记录 response-side effective settings
  -> PendingThreadResume.commit_local_state(...)
```

`PendingThreadResume` 携带一份 opaque、仅当前进程有效的 receipt。receipt 只属于一个
authority、thread 与 generation。authority 在执行 local callback 前先消费 receipt，因此旧句柄
或重复句柄不能再次运行 callback。connection invalidation 或已确认 backend reset 会让延迟
receipt 失效。

该 receipt 只是一份即时 call-stack capability：不持久化、不投影到 operator status、不在重启后
恢复，也不在结算后继续阻止后续 resume 或其他 thread mutation。

exact call 尚未结算期间只存在一条刻意收窄的进程内 effect fence：同 thread 已 prepared 的 resume，或任一
active canonical direct `turn/start` token，会阻止 unsubscribe claim；unsubscribe claim 一旦 prepared，则在该
claim 退休前，新的 resume 与 canonical `turn/start` 都在 transport 前失败；known-success claim 会保持 slot 到
exact local cleanup commit 或 abandon。start 还必须在 effective-settings mutation 前失败。这条互斥只围绕
canonical unsubscribe：resume 与 start 彼此不串行化，多个同 thread start 可以重叠，其他 thread、steer、
interrupt 与 `fcodex` proxy 的独立 connection 均不受影响。该 fence 只防止 canonical Focus connection 明知 subscription removal 正在过渡时交叉
这些 effect，不建立 durable mutation gate 或 replay authority。

local callback 只包含该 surface 首个必须提交的权威本地写入，例如：

- 提交 exact 飞书 binding 或已准入的 blank main-turn owner；active-observer attach 必须在同一本地
  callback 中同时提交 attached binding 与 response 中唯一非空 `inProgress` turn 的 execution anchor；
- 为请求 Web document 提交 confirmed runtime interest；
- 提交 RuntimeAdmin attach 结果。

cache、history、title、goal、card 与 projection refresh 都是后续结果；它们失败不能撤销已经
提交的 resume。active observer 的开卡同样是 post-commit presentation；但 exact anchor 不是
presentation，不能在 anchor 缺失或 prime 失败时仍把 binding 宣告为 attached。

Web cold open 可以在这笔 resume 事务外再套一层 staged read，但不能改变上述提交点。外层先在
RuntimeLoop 冻结 exact document、thread、read observation 与 backend generation，在 loop 外读取
metadata/goal；随后回到 RuntimeLoop 选择 passive read 或准备一笔 exact resume，再在 loop 外执行
有界 page read/resume 及 detached DTO 输入准备。若 resume 返回 known success，下一次 RuntimeLoop
settlement 必须先经 `PendingThreadResume` 提交 runtime interest，才可 claim read-model/projection；
DTO materialization 可再次移到 loop 外，最终仍须回到 RuntimeLoop 复核原 document、backend
generation、projection revision 与 read observation。

因此 final DTO check 因 F5/document reissue、较新 notification、projection revision 或 backend
replacement 拒绝结果时，只说明该 response 已不适合安装。已经 known-success 且完成 local commit 的
resume 仍然有效；Focus 不补发 compensation、不把它重分类为 transport unknown，也不因 stale DTO
恢复旧 document 的 selection/materialization。browser 可以按新的权威坐标重新读取。

running resume response 可能因 turn 恰好完成而已经是 idle；此时 active-observer attach 可以提交
binding，但不伪造 execution anchor。若 response 仍声称 active，却没有唯一非空 active turn id，
local callback 必须 fail closed，并让该 binding 保持 detached。已建立 anchor 的 observer 只消费
attach 后的 live events；上游 snapshot 不承诺回放 attach 前逐条 command/tool delta，因此执行页必须
明确提示此前过程可能不完整。

## 3. 失败边界

### adapter 调用之前

runtime lease 取得失败、本地 model preparation 失败或 exact guard 拒绝，都属于已知 pre-send。
Focus 只释放本次新取得的 runtime lease，并返回原始 typed failure；不会调用 transport outcome
classifier。

### adapter 已知没有产生 effect

Focus 只释放本次新取得的 runtime lease，并返回原始 adapter error。后续调用是一笔新的显式
用户操作。

### adapter 结果未知

Focus 抛出 `ThreadResumeOutcomeUnknown`，保留 machine runtime lease，且不自动重试。exception
只标识本次 request 与 receipt；系统不建立 recovery marker、retry generation、mutation
quarantine 或 operator action。

Web 有一条 method-specific 的更窄结算：若当前 Web operation 在这次 resume 前 fresh 取得
exact blank main-turn lease，且后续 `turn/start`、review 或 compact effect 尚未调用，
outcome unknown 时通过完整
generation compare-and-set 释放该 blank，同时保留 `unknown` runtime interest。pre-existing、
borrowed、已由 lifecycle 激活或已被替换的 lease 绝不能被这次清理。已收到 resume success、
但 local commit 失败不是 transport unknown：它继续按既有 `RETAIN` / `COMPENSATE`
settlement 与 surface 规则处理。本例外不改变 acknowledged settlement；只有
`recovery_required` 或 `STALE_OR_INVARIANT_VIOLATION` 的 incomplete settlement 保留对应
Web blank，其他已结算 known failure 仍可按既有 surface 规则清理 exact fresh main-turn blank。

用户可以显式检查或再次 resume thread；这笔新操作不受旧 unknown call 阻塞。
Focus 不自动发起该重试。这里诚实接受一个极窄窗口：第一次 resume 可能已经触发 autonomous
goal work，但丢失的 response 与尚未到达的 lifecycle 还不能证明它；blank 释放后到权威
`turn/started` 可见之前，显式重试可能遇到该上游 work。Focus 不把这条不可观测竞态伪装成
durable writer 或 recovery state。

### success response 后本地失败

authority 消费 exact receipt 并抛出 `ThreadResumeLocalCommitFailed`。错误携带原始 exception、
policy、exact generation、settlement outcome，以及调用 surface 是否需要保留自己的 exact local
effect；authority 不为此建立 recovery registry。

response-side effective-settings 写入也遵守同一规则：它失败属于已经 ACK 的本地失败，不是上游
response unknown。

active-observer snapshot/anchor local commit 失败也属于 acknowledged local failure。调用点在 shared lock
内先为仍 detached 的 resident 暂存 transient anchor，核验成功后才用一次 durable write 提交 attached
binding；该 write 失败时只回滚进程内 staged anchor，不执行第二次 durable rollback。随后按
`COMPENSATE` 处理本次 runtime receipt；不能自动重试 running resume，也不能把缺失 anchor 的 attached
binding 留给普通 prompt 使用。

Web 外层 staged read 在 resume local commit 之后发生的 cache、projection 或 final DTO stale failure
不是 `ThreadResumeLocalCommitFailed`。它保留已提交的 runtime interest，并只拒绝这份过时 read response。

## 4. 本地失败策略

每个调用点必须显式选择一种 policy：

- `RETAIN` 保留 subscription/runtime lease。resume 可能继续 autonomous work，或调用点无法证明
  cleanup 安全时使用。
- `COMPENSATE` 只有在 receipt 证明 lease 是本次新取得时，才能 unsubscribe、清 effective-settings facts 并
  释放 runtime lease。pre-existing lease 绝不能被本次调用清理。

失败 outcome 保持最小：

| Outcome | 含义 |
| --- | --- |
| `COMPENSATED` | 本次新取得 lease 的所有安全 cleanup 均完成。 |
| `RETAINED` | runtime effect 保留；调用者收到本次 exact failure。 |
| `CLEANUP_PENDING` | 某一步 cleanup effect 未确认；本次 exact call 报告该不确定性。 |
| `STALE_OR_INVARIANT_VIOLATION` | receipt 来自其他 authority、已消费或已失效；callback 未运行。 |

成功时直接返回 callback 结果。任何 outcome 都不授予 thread-wide lock、durable recovery
authority 或自动 replay 权。

## 5. Ownership 与 reset

`ThreadRuntimeAuthority` 只拥有 adapter 调用顺序、response-side effective-settings facts 与即时 receipt
消费。各 surface owner 决定 local callback 提交什么，以及如何展示一笔 exact local unknown。

connection invalidation 与 confirmed backend reset 都会清 connection-local effective-settings facts 并使延迟
receipt 失效。两者都没有旧 durable resume journal 可清理或 replay。
machine runtime lease 继续由自己的 runtime 合同管理。

## 6. 回归要求

测试必须证明：

- local preparation failure 按 pre-send 分类，且只释放新 lease；
- known-no-effect 与 unknown adapter failure 明确区分；
- unknown 保留 machine runtime lease，但不阻止后续显式调用或无关 mutation；
- Web resume unknown 只释放当前 operation 的 fresh exact blank；borrowed、activated、
  replaced，以及仍 `recovery_required` / stale 的 acknowledged-incomplete lease 保持不变；
- success receipt 最多运行一次 exact local callback；
- same-thread prepared resume 或 active canonical start 会阻止 unsubscribe；prepared unsubscribe 则在
  transport/settings mutation 前拒绝 resume 与 canonical start，且 known-success slot 保持到 exact local commit
  或 abandon；
- unsubscribe fence 不串行化多个 same-thread start，也不影响其他 thread、steer、interrupt 或独立 `fcodex`
  connection；
- `RETAIN` 报告 exact failure，但不安装 recovery registry；
- `COMPENSATE` 只清理新 lease，并报告 partial cleanup；
- connection/reset 会使延迟 receipt 失效；
- snapshot 与 paged resume 使用同一合同；
- active observer 只在 exact active pre-send guard 后发送且不取得 writer；response idle race 不伪造
  anchor，active response 缺少唯一 exact turn 时恢复 detached；
- active observer 的 binding 与 anchor 同一 local callback 提交，开卡失败只降级 presentation；
- active observer durable attach 写入失败时，binding/store 保持 detached，transient anchor 被清除，且不依赖
  第二次 store write 回滚；
- observer 页明确披露 attach 前 process history 可能不完整，并且 pending request replay 不授予它
  取消或审批 authority；
- post-commit projection failure 不能回滚已提交的本地 ownership；
- Web cold open 在 known resume 后遇到 document reissue、较新 notification、projection revision 或 backend
  replacement 时拒绝 stale DTO，但保留已确认的 runtime interest，且显式重读仍是一笔新的 operation。

相关合同：

- `docs/contracts/root-operation-owner.zh-CN.md`
- `docs/contracts/thread-create-local-commit.zh-CN.md`
- `docs/contracts/feishu-thread-lifecycle.zh-CN.md`
- `docs/contracts/fcodex-operation-owner.zh-CN.md`
- `docs/architecture/focus-shared-backend-runtime.zh-CN.md`
