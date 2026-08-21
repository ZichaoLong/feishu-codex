# fcodex main-turn 与 proxy 边界合同

文档角色：中文规范源。英文同步副本：`docs/contracts/fcodex-operation-owner.md`。

> 本文是 `fcodex` transport/proxy 对
> [main-turn owner 合同](./root-operation-owner.zh-CN.md) 的专项扩展。
> participant、connection 与进程内 recovery fact 都不是 main-turn writer state。

## 范围

`fcodex` 把一个 remote TUI 接到 Focus 的 shared Codex app-server。它需要解决三类
transport 问题：

- 哪些没有 thread target 的 RPC 可以进入共享 backend；
- 一个带 `threadId` 的 RPC 是否指向可直接操作的 root；
- 同一 root main turn 在 `fcodex`、Web 与飞书之间如何保持单一 initiator/writer，
  同时允许合格可信本机端贡献 same-turn input。

本文不把 socket 生命周期、server request、goal continuation、thread create 或 backend reset
合并成 main-turn ownership；它们各有独立 fact。

## 没有 root target 的 client request

没有非空 `params.threadId` 的 request 无法绑定到某个 thread writer，因此默认不转发。
proxy 只允许已审阅的初始化/发现读取：

- `initialize`
- `account/read`
- `config/read`
- `configRequirements/read`
- `model/list`
- `hooks/list`
- `skills/list`
- `account/rateLimits/read`
- `thread/list`
- `thread/loaded/list`
- `app/list`
- `app/installed`
- `experimentalFeature/list`
- `mcpServerStatus/list`

`initialized` 是唯一允许的 connection-local client notification。未知 notification
必须 suppress；畸形 frame、scalar 与 JSON-RPC batch 也不能绕过逐 method 分类。
精确 allowlist 由 `bot/fcodex/proxy.py` 与 app-server schema baseline 共同守卫。

`thread/start` 是受控的 targetless create 例外。它创建 thread，但不创建 main-turn writer；
返回的新 root 只有后续 lease-bearing exclusive/autonomous effect 才取得 submission lease；普通
`turn/start` 仍是无 writer 的 upstream-routed input。create request 的
unknown/retry 与本地 commit 由
[thread-create-local-commit](./thread-create-local-commit.zh-CN.md) 定义。

## Response envelope

Codex app-server `0.147.0` 明确不发送、也不要求标准 JSON-RPC 的
`"jsonrpc": "2.0"` member。成功 response 是 `{id, result}`，失败 response 是
`{id, error}`；固定上游定义见
[`rpc.rs#L1-L2`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server-protocol/src/rpc.rs#L1-L2)
与
[`rpc.rs#L67-L79`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server-protocol/src/rpc.rs#L67-L79)。

因此 proxy 对 backend client response 及 TUI server-request response 使用同一最小 envelope
规则：`id` 必须与 pending request 保持 exact type/value correlation；不能携带 `method`；
`result` 与结构化 object `error` 必须恰好存在一个。受协调 mutation 的成功 result 还必须满足
该 method 由 Focus 拥有的 target/status 等后置条件，才能把 unknown effect 结算为 known
success。`fcodex` `thread/start` / `thread/resume` 中的 reviewer 与 settings 属于上游，
不是这里的 Focus-owned 后置条件。
缺少 `jsonrpc` 本身绝不是 quarantine、unknown settlement 或 transport failure 的证据；即使来宾
附带该字段，它也不参与 authority。其他畸形、混合、无匹配或无法证明 mutation 后置条件的
response 继续按原 exact request 边界 fail closed。

## 直接 thread target

带非空 `threadId` 的 thread-scoped RPC 在 proxy 转发前进入 service admission。
Focus 必须确认 exact target 是 direct root；spawned subagent 不能被当成独立可写 root，
也不能因 lineage cache 缺失而升级。

唯一 child-target transport 例外是上游 TUI 导航需要的严格 metadata read：
`thread/read`、exact `threadId`、`includeTurns=false` 且没有其他未审阅参数。
它只读 metadata，不创建 owner、lease、subscription、interaction route 或 mutation authority。

thread read 可以是 observer 操作。main-turn start/writer identity 与 effect-specific active-turn
control 按下文处理；goal、resume、settings 与 lifecycle mutation 保留各自 effect-specific admission
和进程内 unknown evidence，不能延长或
替换现有 Focus main-turn lease。该 ownership 结论不承诺 upstream resume 不会启动之后的 autonomous
goal work。

`thread/resume` 必须携带非空、无首尾空白的 exact direct `threadId`。上游 cold-resume
对非 null `history` 与非空 `path` 的优先级高于 `threadId`，因此 Focus 把这两种已知的
alternate target 在本地拒绝。通过这一 target guard 后，proxy 对原生 TUI payload 保持语义原样：
model/provider/tier、cwd/workspace roots、approval/reviewer、sandbox/permissions、config、instructions、
personality 以及未来上游字段都不由 Focus whitelist、删除、归一化或覆盖。`thread/start`
同样保留原生 TUI params，唯一 payload 补丁是当 `cwd` 缺失时注入 wrapper 已解析的最终 cwd。
这些 upstream-owned 字段不授予 Focus writer、settings owner 或其他 effect authority。

Focus 自有 canonical adapter 发起的 lifecycle RPC 仍使用它自己的
`approvalsReviewer=user` 合同；那不得反向应用到 native `fcodex` 运输。对已加载 thread，
upstream 可以忽略 resume 中的 reviewer override 并回报 backend 已有 reviewer。`fcodex` 不拥有这个事实，
所以 reviewer 不同不是 response 畸形、transport failure 或 quarantine 证据；response 中的 exact
thread identity 仍必须与已准入 target 一致。

exact active-turn lease 已存在时，live fcodex endpoint 可以把这条已准入的 native resume 用作
observer attach，即使 persisted-goal 检查认为 cold resume 可能继续工作。它不取得或转移 Focus
writer，但这里明确采用 upstream running-resume 自然语义，而不承诺纯 subscription：upstream
会 attach connection、返回当前 thread response、replay pending request，并在存在 goal state 时调用
idle lifecycle；若 turn 在最后检查前已经 idle，goal extension 可能继续执行。若没有 active
Focus turn，可能 autostart 的 resume 仍按既有合同要求 blank-submission admission。

fcodex websocket 也不是 connection-local effective-settings registry 的事实 writer。owner admission 成功后、
proxy 能发送已审阅的 turn、settings、resume、continuation-risk goal 或 thread-lifecycle effect 前，Focus 把
exact thread 标成 external-unknown，并退休四个 setting 字段。canonical adapter notification 不能覆盖这个 negative fact：两条
app-server connection 没有共同 revision 或 causal ordering token，较晚到达的 notification 仍可能早于 external
effect 发出。fcodex socket 上的 request、ACK、response 与 notification 都不登记值；该 thread 一直保持 unknown，
直到 canonical backend epoch replacement/reset 清除全部 disposable facts。这只是 exact-thread disclosure/native-media
降级，不建立 writer 或 service quarantine。targetless `thread/start` 没有既有 thread 可退休。

## main turn owner

`FcodexMainTurnOwner` 只拥有 `fcodex` exclusive/autonomous submission 与 active-turn lease
投影；普通 `turn/start` 是 upstream-routed realtime input，不是 writer admission：

- 三种 start 都只接受 exact direct root；
- 普通 `turn/start` 只要求 live endpoint 与 exact root，并登记 exact
  participant、connection、JSON-RPC request id 与 request token。它不读取、取得或释放
  `InteractionLeaseStore`，也不经过 cross-surface writer denial；success、error、unknown 与
  connection loss 都只结算该 request；
- `review/start` 与 `thread/compact/start` 仍在发送前取得 PID-bound、
  participant+connection-bound exclusive blank lease；
- inline `review/start` response id 是 actual inline-review turn id，可以直接激活；proxy 继续
  拒绝 detached review。`thread/compact/start` 的空 response 不激活 lease，只等待 lifecycle；
- 若当前没有 fcodex blank，ordinary start 的 response 或 lifecycle notification 都不会凭该
  request 创建 lease 或 writer；
- matching `turn/started` 仍可把已有 fcodex exclusive/autonomous blank 绑定 actual `turn_id`；
  active lease 保持对应 activity identity，matching `turn/completed` 只释放 exact active lease，
  `thread/closed`、archive/delete 等 exact terminal notification 可以清除它；
- `turn/steer` 与普通 `turn/interrupt` 使用下文独立的 effect authority，不从 writer relation
  派生，也不转移 writer。

ordinary start 准入与 request settlement 前后，既有 Web、飞书或 fcodex blank/active lease 的完整
record 保持不变。但 upstream `turn/started` 不携可把 notification 归因到某个并发 RPC 的 effect
identity：若普通 start 与既有 fcodex review/compact/goal/resume blank 并发，后续 lifecycle 仍可能激活
该 blank，Focus 无法证明实际 turn 来自哪一笔 effect。这是明确接受的上游极窄竞态；Focus 不为消除它
增加 recipient/turn owner 状态机。exclusive known rejection 只按 captured generation CAS 释放 exact
blank；stale response 不能释放 ABA replacement。unknown exclusive submission 继续等待
method-specific 或 lifecycle/terminal 证据，且不创建跨重启 writer。

goal receipt、participant identity 或同一 connection 不能授权 exclusive start 或产生 writer；普通
`turn/start` 只有上述单次 upstream input authority。这些事实本身也不能授权 steer 或普通 interrupt；
每项 effect 都必须满足下文 live endpoint、exact direct-root、attached source 与 raw target 的完整边界。
两项 effect 都不产生 writer，也不存在 tree-stop fallback。

## participant 与 connection 生命周期

`FcodexParticipantRuntimeRegistry` 记录 proxy endpoint、request source、subscription source
和 backend generation。这些是 transport liveness/route facts，不是 main-turn writer state。

- observer socket 不因 writer socket 断线而接管 active turn；
- active-turn lease 不因 socket disconnect 转成 `grace` 或 `orphaned`；
- service 仍在运行时，matching lifecycle 负责释放 active turn；
- service restart 后，PID-bound main-turn lease 被清理，不重建旧 connection writer。

Registry 内用于 endpoint cleanup 的 connected/grace/orphaned vocabulary 只能解释 endpoint，
不能投影成跨前端 main-turn ownership。

## interaction 与 server request

当前 backend epoch 中，带非空 `turnId` 且属于 exact direct root 的 canonical interactive callback
构成 trusted-local shared interaction domain，包括 command-execution、file-change 与 permission approval、
用户输入、MCP elicitation 和 dynamic tool call。所有成功 materialize 该 exact root 的 live fcodex endpoint
都可以收到同一 canonical request，包括 turn 开始后才 attach 的 observer；每个 endpoint 获得不同的一次性
response token。第一份有效 endpoint 或 Web response 胜出，其他 endpoint 由 typed local receipt 或 upstream
`serverRequest/resolved` 退休。approval 还分别投影给飞书；普通 interaction 先分别尝试 fcodex 与 Web，只有
两个 desktop 都权威 decline 才保留原 single-surface 飞书 fallback，desktop outcome unknown 不得触发该
fallback。Web 可以拒绝自身无法展示的 shape，包括 dynamic tool call。auth、unsupported method、empty-turn
与 child-thread request 不进入该 domain，继续沿用既有 writer/surface route。

shared-interaction 资格不读取 `InteractionLeaseStore`：该 store 决定谁能 start 并保持 initiator/writer identity；
initiating `fcodex` 尚无 connection source 时，matching active-turn lease 可以为其提供一次 exact steer
或 interrupt attach proof。lease 不是共享 effect authority，也不能否定 app-server 当前 epoch 中仍
pending 的 canonical callback。active goal 的正常自动续跑即使没有 Focus
writer，已 attach endpoint 仍可回答 callback；proxy-first projection 可以先展示，但用户 action 在
canonical identity 绑定前只会得到 `not_sent` 并重新展示，不保留为待自动提交的 intent。
非审批 canonical offer 只有在 exact root 至少存在一个具有 current connection source 的 live endpoint 时，
fcodex 才 claim，避免不可见的 fcodex projection 吃掉飞书 fallback。

shared interaction 只授予一份 exact request 的 response authority，不是 writer handoff。它不改变 turn
initiator、active model/effort/sandbox/approval settings、输出目的地、goal、binding、backend
generation 或下一 turn admission。TUI 的合法 raw response object 原样转发；approval 还包括 session
approval、strict auto-review 与 request 已声明的 exec/network-policy amendment。无效 response 只退休该
exact endpoint token，不替其他 shared endpoint fail-close 或回答。

上游 app-server 使用进程内 pending request map，在 resume 时向新 connection replay，并由第一份
response 消费；matching TurnComplete 会清理 thread 的 pending requests。Focus 只需要补充
backend generation、request identity 与一次性 action token，避免旧 UI action 回答 replacement
request。

当前 request identity 只属于
`server-request-lifecycle.zh-CN.md` 定义的进程内 `ServerRequestRegistry`；matching turn/lifecycle completion
只清除这份 local projection，不能继续占用 main-turn lease。proxy-first local record/capability 只解决
真实 proxy/service 到达竞争，不是 lifecycle 或 writer authority；只有 automatic fail-close 可在已证明
pre-send failure 后保留 exact intent，等待 explicit upstream replay。

fcodex 已保留 canonical shared interaction 后，即使当前没有 endpoint attach，其 inbox 仍保留。endpoint
disconnect 只删除该 endpoint token；只要中央 response authority 仍开放，同一 Focus/app-server epoch 中之后的已准入 native resume
与 upstream pending replay 可以签发 fresh token；exact 撤权会在签 token 前检查。不建立磁盘 queue，也不做跨重启恢复。若 response 晚于 service local resolution，
或 proxy 已先收到 `serverRequest/resolved`，bounded、typed-id、current-epoch receipt 会吸收该迟到
response，不隔离整条 proxy socket。

用户 response 被证明 not sent 时，proxy 重新展示 exact request，并要求用户再次明确操作。automatic
fail-close 只在 explicit exact upstream replay 之间保留 intent。unknown outcome 只封锁该 request；
无关 request 与 connection 保持可用。

群停用形成的 exact revocation 会在中央 response-time 复核。若 automatic cancel 被证明未发送，fcodex
当前没有 service→proxy push 可以立刻关闭已经渲染的 overlay；它可能保留到用户动作收到 typed superseded
receipt，或真实 upstream resolution/lifecycle cleanup 到来。该 overlay 已无 response authority，不能伪装成
callback 已解决。

## backend reset

backend reset 的必要 transport 事实是 backend generation：旧 generation 的 response、
notification、timer 与 action token 不能作用于 replacement backend。reset 的最小顺序是停止
旧 backend、失效旧 generation/local pending、启动并验证 replacement。

backend reset 不是 writer handoff。只有在旧 backend 已证明停止后，才退休旧进程内 request/effect fact；
它们不会迁移到 replacement generation。fcodex owner 只退休自己的 client request、direct-root routing 与
interaction inbox facts；它不再遍历或释放 fcodex lease 子集。三 surface 的 current-process full leases 由
`InteractionLeaseStore` 在 stop 前统一 capture、stop confirmed 后统一 exact CAS，避免 surface-specific 双路径；任一
retirement 失败都禁止启动 replacement。

## 共享 same-turn input

fcodex `turn/steer` 是 exact-turn contribution，不是 writer handoff。任何已 attach exact direct root
的 live endpoint 都可以提交，包括 late-attached endpoint、另一个 fcodex participant，以及 attach 到
Web、飞书或 autonomous-goal origin turn 的 endpoint。

- raw params 必须包含非空且无首尾空白的 `threadId`、`expectedTurnId`，以及 `input`。稳定可选的
  `clientUserMessageId` 和 upstream `input` payload 原样保留，由 upstream schema 校验。未知 key 不准入；
  experimental `additionalContext`、`responsesapiClientMetadata` 必须 absent 或 null；
- endpoint 必须在当前 backend generation 中 live，并拥有该 exact root 的成功 connection runtime source。
  initiating fcodex endpoint 尚无该 source 时，只有 participant、connection、root 与 exact expected turn
  全部 matching 的 lease 可以作为 attach proof；
- Focus 原样转发 pinned expected turn。missing、terminal、stale、mismatched、review 或 compact turn 由
  upstream 原子拒绝；Focus 不猜 successor，也不声称并发合法 steer 存在 global order；
- success、typed rejection 与原生 TUI unknown outcome 使用普通 tracked request settlement。Focus 不为
  fcodex 新建 durable steer ledger、exact deduplication 或自动重发。

shared steer 不取得、替换或延长 writer lease；不改变 active model、effort、permissions、approval policy、
output destination、goal、binding 或 next-turn admission，也不授予 settings、lifecycle、server-request
response 或其他 thread-mutation authority。

## 普通停止

`fcodex` Ctrl+C 只有一条产品路径：向当前 TUI 提供的 exact direct-root active turn 发送普通
`turn/interrupt`。它与 shared steer effect 明确分离：

- raw params 的 keys 必须恰好为 `threadId + turnId`；`threadId` 是非空、无首尾空白的 string，
  `turnId` 是无首尾空白的 string，允许显式空值；raw payload 原样转发；
- endpoint 必须属于 current backend generation 并保持 live。非空 `turnId` 接受 current connection
  source；若 initiating endpoint 尚未形成该 source，matching participant、connection 与 exact turn 的
  active-turn lease 也只可作为 attach proof；
- 空 `turnId` 只接受该 current connection 自己的 exact-root runtime source，并采用固定上游的
  current/startup interrupt 语义；blank 或 active lease 都不能替代这份 connection attach proof；
- 合格的 late-attached observer、不同 `fcodex` participant，以及已 attach 到 Web/飞书发起 turn 的
  endpoint 都可以发送 interrupt，但都不取得或改变 writer；
- attached endpoint 提交的 stale、terminal 或 mismatched 非空 `turnId` 原样到达上游 check 并收到
  typed rejection；Focus 不猜测 current id、不自动改打后继 turn；空值也不由 Focus 补写 projected id；
- 未 attach、断线、pending resume、backend replacement、含混 target 与 child target 都明确拒绝。

Focus 不扫描 descendants、不建立 stop journal，也不承诺原子 tree settlement。interrupt 不授予
start/steer、settings、goal、binding、lifecycle 或 server-request response authority；child 必须在自己的
独立合同下控制。

## 相关代码与上游依据

主要实现：

- `bot/fcodex/proxy.py`
- `bot/fcodex/operation_contract.py`
- `bot/fcodex/main_turn_owner.py`
- `bot/fcodex/operation_service.py`
- `bot/fcodex/participant_runtime_registry.py`
- `bot/stores/interaction_lease_store.py`

main-turn lifecycle 以及 running-resume/server-request 行为固定对标公共上游
[`openai/codex@be6e8eac029b183056b7e4402879f15d2c85f61b`](https://github.com/openai/codex/commit/be6e8eac029b183056b7e4402879f15d2c85f61b)
（release `rust-v0.147.0`），不引用机器本地 checkout：

- 普通 start 的 response/submission 与 actual start-or-steer 证据分别见
  [`turn_processor.rs#L474-L607`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L474-L607)、
  [`session/mod.rs#L789-L830`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/session/mod.rs#L789-L830)
  与 [`handlers.rs#L189-L270`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/session/handlers.rs#L189-L270)；
- inline review response identity 见
  [`turn_processor.rs#L1249-L1268`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L1249-L1268)
  与 [`review.rs#L116-L179`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/tests/suite/v2/review.rs#L116-L179)；
- compact 空 response 见
  [`thread_processor.rs#L1876-L1887`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/thread_processor.rs#L1876-L1887)；
- TUI 在尚无可见 actual id 时发送空 `turnId`，app-server 将空值解释为 current/startup
  interrupt，分别见
  [`app_server_session.rs#L1132-L1155`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/tui/src/app_server_session.rs#L1132-L1155)
  与
  [`turn_processor.rs#L1409-L1453`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L1409-L1453)；
- server-request pending/replay 证据见固定提交中的
  [`outgoing_message.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/outgoing_message.rs)、
  [`thread_lifecycle.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/thread_lifecycle.rs)、
  [`bespoke_event_handling.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/bespoke_event_handling.rs)、
  [`app_server_requests.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/tui/src/app/app_server_requests.rs)
  与 [`approval_overlay.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/tui/src/bottom_pane/approval_overlay.rs)。

若旧文档仍声称 fcodex participant/socket state 会授予或延长 main-turn writer，以本文与共同
main-turn 合同为准。
