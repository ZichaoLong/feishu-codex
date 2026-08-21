# main turn owner 合同

文档角色：中文规范源。英文同步副本：`docs/contracts/root-operation-owner.md`。

> 文件名保留历史链接兼容；本文不再定义“root 与全部 descendants 共同结算”的
> root-operation writer。本文只定义 Focus 相比上游 Codex 多出的最小规则：
> 同一个 root thread 中仍明确要求串行化的 Feishu/非普通 prompt action 至多有一个 admission holder。
> 普通 Web/`fcodex` prompt 是 upstream-routed realtime input，不读取或取得该 holder；一个 active turn 可以接收多个
> exact contributor，contributor 不成为 writer。

## 1. 目的与范围

上游 Codex 在 matching `turn/completed` 后立即结束 main turn。spawned subagent
可以继续在自己的 thread 上运行，但不会延长 root main turn，也不会阻止用户提交下一轮。

Focus 额外面对 Web、飞书和 `fcodex` 同时向同一 shared app-server thread
提交输入的竞争。共享 lease 会串行化 Focus 已经观察到的本机 submission/active turn，
但不能把上游官方 `turn/start` 从 start-or-steer 变成 atomic compare-and-set start。
在极窄的时序窗口中，Web、`fcodex` 或 resume 后的 autonomous goal continuation
可能先在上游变成 active，而对应 exact active-turn 事实尚未到达飞书准入路径；此时
飞书 `turn/start` 的 input 与本轮 settings 会加入该 active regular turn。本文把它明确
记录为上游基线的残余竞态，不再为消除它引入私有 RPC 或第二套状态机。

因此，Focus 只为仍要求 next-turn/FIFO 或 exclusive action 的路径保留共享 lease；它不再把普通 Web 或
`fcodex` prompt 串行化成 start writer。Web server 在单 POST prepare 时冻结 exact active id：有 id 就在
external effect 中 steer exact turn，缺 id 就使用官方 `turn/start`；`fcodex` 把已准入的原生 `turn/start` params 原样交给同一 upstream
start-or-steer 语义。本文不把 child、interaction、delivery、presentation、goal、socket 或 cleanup 合并成
一个更大的 operation。

本文覆盖：

- 飞书 binding 发起的普通 prompt、review、compact 和其 active-turn control；
- Web document 发起的普通 prompt、review、compact 和 steer/interrupt；
- `fcodex` participant/connection 发起的对应 main-turn RPC；
- 同一 Focus instance 内上述前端之间的竞争。

另一 Focus instance、独立启动的裸 Codex client、thread create、resume、goal mutation、
server request 与 destination liveness 各有自己的合同；它们不能反向扩大本文的
main-turn writer 生命周期。

## 2. 核心规则

一个 root thread 可以有多个 observer和exact realtime contributor；共享 lease 只授权仍声明 exclusive/next-turn
语义的 action，不是 ordinary-input mutex，也不表示它独占 active-turn steer、interrupt或approval：

- idle 时没有 main-turn owner；
- 发出仍声明 Feishu next-turn/FIFO 或 exclusive 语义的 turn-producing RPC 前，前端先取得一份
  exact blank submission lease；
- 普通 `turn/start` known-success response 的 `turn.id` 保留 upstream 返回的 turn identity；该 response
  本身不激活或转移 Focus lease，也不建立跨 notification/reset 的 lifecycle/completion authority；同一
  `FeishuRootOperationController` 只能把它保留为该 exact prompt admission 的一次、进程内 interrupt candidate；
- matching `turn/started` 给出 actual `turn_id` 后，同一 lease 才变成 active-turn lease；
- lease holder 仍是 writer；合格且 live/attached 的 `fcodex` endpoint 或 connected/materialized
  Web document 可以使用 effect-specific exact-turn authority steer，可信本机端普通 interrupt 使用其独立
  exact-turn authority；这些 effect 都不转移 writer；
- matching `turn/completed(thread_id, turn_id)` 立即释放 lease；
- 没有任何 surface 拥有把未绑定 completion 归因到某次普通 `turn/start` submission 的
  权威 receipt，因此 completion 不能绑定或释放任何 blank lease。若 matching
  `turn/started` 遗漏，blank fail closed 保留到权威 terminal evidence 或 service restart；
- 飞书的 `FeishuRootOperationController` 还可以在权威重读 root 已 inactive 后，结算它
  当前 exact 的普通 prompt blank；该结算只证明已经没有 main turn，不把某条 completion
  归因到本次 submission；
- matching completion 之后，其他前端可以立即发起下一轮，不等待 child、卡片、消息投递、
  interaction 或 retained cleanup。
- 普通 Web/`fcodex` prompt 不取得 blank/active lease。Web server 在 RuntimeLoop prepare 时从 read model 冻结
  exact-ID steer route，缺 ID 就冻结 upstream `turn/start` route；effect 外移并以 exact receipt 回到 loop settle；`fcodex` ordinary start 只登记
  exact request token。现有 lease、blank 或 foreign holder 都不能在这两条路径返回 cross-surface writer denial。

writer denial 本身不构成排队、隐式 steer 或自动 handoff。只有另外获得 exact 证明的飞书 binding FIFO
可以保留下一轮输入，而且 queued item 在真正出队前没有 writer。规则按 root thread 生效，不是全服务单用户锁。

## 3. 唯一事实源与状态

`InteractionLeaseStore` 是 main-turn writer 的唯一事实源。状态不需要独立的四态
writer state machine：

| 状态 | store 事实 | 允许行为 |
| --- | --- | --- |
| idle | 没有该 thread 的 lease | Feishu/exclusive action 可竞争取得新 submission；普通 Web/`fcodex` input 不需要 lease |
| submission | lease 存在且 `turn_id` 为空 | 该 exact exclusive/Feishu generation 正在提交或等待权威 identity/terminal 对账；普通 Web/`fcodex` prompt 仍可请求 upstream-routed input；第 6 节保留同一飞书 exact prompt admission 的一次 candidate interrupt |
| active | 同一 lease 的 `turn_id` 非空 | holder 保留其 exclusive/Feishu activity identity；connected/materialized Web document 或合格的 live/attached `fcodex` endpoint 可以贡献 exact-turn input，也可以 interrupt current turn；这些 effect 都不转移 holder |

lease generation 由 `lease_id` 命名；full-record CAS 比较完整且不可变的
`thread_id + holder + lease_id + updated_at + turn_id` value。激活与恢复必须比较 exact
`lease_id` generation；旧 response、旧 notification 或同名 holder 不能覆盖后继 lease。
matching completion 只按 exact `thread_id + turn_id` 释放，不能 ABA 释放下一轮。

飞书的 opaque admission/continuation token 只是本地事务 receipt，不是第二份 writer fact，
也不能从 projection 或 restart 恢复。它只允许 `FeishuRootOperationController` 保留并结算
仍 full-equal 的 exact blank lease，包括在权威重读 root 已 inactive 之后；它不能把任意
completion 关联到本次 submission。共享 writer 的 current 事实仍只在
`InteractionLeaseStore`。详见[飞书 thread 生命周期合同](feishu-thread-lifecycle.zh-CN.md)。

普通 prompt admission 还可以在该 controller 内暂存 response `turn.id`，作为一次
process-local interrupt candidate。candidate 保留该 RPC known-success 时 upstream 返回的 exact turn coordinate；它不是
Focus lease、matching notification/completion、FIFO continuity、snapshot/log 中的 mutable fact
或持久事实；越过既有 audit point 后仍只记录 exact id 的脱敏短 hash，不记录 candidate/raw id。
matching actual lifecycle、owner loss、admission finish 或 root-terminal cleanup 会清除它；
unbound `turn/completed` 不能借 candidate 获得 correlation authority，service restart 后也不恢复。
每个 admission 最多安装一次；claim 原子清空槽位，consume 后同一 token 不能重新 arm。

main-turn holder 都绑定当前 Focus service PID 与 process identity：

- 飞书：sender/binding chat identity；
- Web：document `client_id`；
- `fcodex` exclusive/autonomous submission：participant incarnation 与 exact connection。

PID 0 或跨重启 retained holder 不是 main-turn writer。service restart 后，旧 PID lease
会被清理；Focus 不尝试凭旧 socket、document id、binding 或 retained record 重建 writer。

同一 service 进程内的 backend reset 不会自然改变 PID，因此必须使用更窄的 confirmed-stop transaction：ingress fenced
后、旧 child stop 前只读捕获 `owner_pid == current PID` 且 `owner_process_identity == current process identity` 的 full
lease；owned child OS exit/wait 确认后再对每份 captured 的完整
`thread_id + holder + lease_id + updated_at + turn_id` value 做 CAS retirement。missing 或
不同 full record 只说明 capture 已退休或 successor 已接管，不能再清 successor；其他 PID 与 PID 0 始终不在 capture 中。
capture、stop 或 retirement 任一步无法证明时保持 ingress fenced 且不启动 replacement。该规则不把 reset 变成 writer
handoff，也不恢复或 replay 旧 submission。

## 4. 提交、unknown 与释放

需要 lease 的 Feishu/exclusive submission 按以下最小事务处理：

1. 在发送 start RPC 前取得新的 blank lease；
2. 已知本地拒绝或已知 upstream rejection 只释放这份 exact blank lease；
3. 普通 `turn/start` known-success response 的 `turn.id` 是 upstream 为该 RPC 返回的 authoritative turn identity，
   必须原样保留；但 response 本身不能激活/转移 Focus lease，也不建立 matching notification/completion 或跨 reset
   lifecycle authority。飞书只可按第 6 节把它保留为同一 exact prompt admission 的一次 interrupt candidate；
4. matching `turn/started` 把同一 blank lease 绑定到 actual `turn_id`；
5. `turn/start` 等 turn-producing effect 的 outcome unknown，或已 known-accepted 但 identity
   尚未绑定时，只保留 PID-bound blank lease，等待权威 lifecycle 或 terminal evidence；
6. matching `turn/completed` 只释放已绑定且 exact `thread_id + turn_id` matching 的 active
   lease；这是所有 surface 的普通 active path；
7. 若 matching `turn/started` 已过去或遗漏，没有任何 surface 拥有足够的
   effect-correlation evidence 把 completion 归给 blank generation。blank 会 fail closed
   保留到权威 terminal evidence；Web/`fcodex` exclusive/autonomous blank 保留到 `thread/closed`、archive/delete 等
   exact thread terminal 或 service restart。代价是其间同一 thread 的新 main-turn
   admission 被拒绝，但其他 thread 不受影响；
8. 飞书保留一条更窄的可用性路径：只有同一 `FeishuRootOperationController` 的 exact、
   进程内 awaiting admission token，才允许权威重读 inactive root status 并只释放仍
   full-equal 的 blank。这不会绑定 turn id，也不会声称哪条 completion 属于本次
   submission。token mismatch、replacement/ABA、没有本地 awaiting admission 或来自
   其他 surface 的 blank 均不可使用；
9. `thread/closed`、archive/delete 等确切 thread 终结事实可以清除该 thread 的 lease。

普通 Web prompt 明确不进入上述事务：单 POST 在 RuntimeLoop 内 prepare exact browser mutation/route，在 external worker
执行最多一项 upstream input effect，再凭 immutable receipt 回到 loop exact settle；它不取得或保留 main-turn lease。
RPC 内的 typed unknown 与首 HTTP response loss 只由有界 exact result receipt/browser locator 解释，不得重新包装成
blank writer。旧 unknown 不自动重放，但也不拒绝用户以新 mutation id 发起下一条明确输入。

普通 `fcodex turn/start` 同样不进入上述事务：它保留 native params，只登记 exact participant、connection、
request id/token 并结算 response 或 connection loss，不读取、取得或释放 main-turn lease。其准入与 request
settlement 不改变既有 lease；但若同时存在 fcodex exclusive/autonomous blank，后续 `turn/started` 没有可归因
到某笔 effect 的 identity，仍可能激活该 blank。这是接受的上游极窄竞态，不以新的关联状态机消除。

Web `thread/resume` 有一条不改变上述 turn-producing 规则的 method-specific 例外：如果
resume response unknown，后续 prompt/review/compact effect 尚未调用，而且 exact receipt
证明 blank 是本次调用 fresh 取得的，Web 通过完整 generation CAS 释放它，同时保留只读
runtime interest。borrowed、pre-existing、已激活或已替换的 lease 不释放；resume 已 ACK、
仅 local commit 失败不是 transport unknown，继续按既有 `RETAIN` / `COMPENSATE`
settlement 与 surface 规则处理；本例外不改变 acknowledged settlement，只有
`recovery_required` 或 stale/invariant-violation 的 incomplete settlement 保留对应 Web blank，
其他已结算 known failure 仍按既有 surface 规则处理。这样显式 retry 可以重新准入，但 Focus
不自动 retry 或 takeover。
代价是一个诚实接受的极窄窗口：第一次 resume 可能已自主开工，而
`turn/started` 尚未可见；blank 释放到该 lifecycle 到达之间，显式 retry 可能遇到这份上游
work。Focus 没有可关联证据消除这个窗口，也不为它建立 durable writer 或 recovery state。

第 7 步是明确保留的极窄 lifecycle-observation 残余与可用性成本：Focus 宁可暂时拒绝
同一 thread 的下一次 start，也不凭无法关联 effect 的 completion 猜测释放。飞书只能用
第 8 步的 exact inactive-root 重读缩短该窗口；它不触发自动 retry、replay 或更大范围隔离。

这些规则按 method 区分。当前固定 upstream 中，inline `review/start` response 的 id
就是 actual inline review turn id，继续作为 response-specific activation evidence；
`fcodex` 只允许 inline review。`thread/compact/start` response 不含 turn id，继续使用
既有 lifecycle 路径。known-accepted compact 在 identity wait 超时并转成 unknown outcome
之前，不能使用 inactive-root 重读结算；此前的 stale idle observation 不能提前释放它。
普通 `turn/start` 同样保留 response 的 authoritative `turn.id`，但只由 matching `turn/started` 激活本文的 Focus lease；
response identity、lease activation 与 completion correlation 是三项不同事实。

unknown submission 不创建 durable root fence，也不隔离其他 thread。它只阻止同一 thread
在“这次 start 可能已经生效”尚未对账时重复提交。service restart 后旧 blank lease 不再存活；
Focus 依靠上游 thread/list/read/resume 与后续 lifecycle 恢复可见状态，而不是恢复旧 writer。
confirmed backend reset 还会让各 surface owner 幂等退休同一旧 backend 的 process-local admission/attempt lineage；这项
local retirement 与上述 centralized lease CAS 是由各原有 owner 参与的同一有序 transaction，不允许 fcodex、Web 或飞书各自
再遍历和释放一份 surface-only lease 子集。

前端断线、binding/document 丢失或卡片发送失败本身不把 active lease转换成
`grace/orphaned/stopping`。已经激活的 turn 仍由 matching completion 结算；能证明 RPC
尚未发送的 blank submission 可以由 exact receipt 清理，outcome unknown 的 turn-producing blank
submission 只按上述权威证据对账；Web/`fcodex` exclusive/autonomous blank 保留到 matching started、exact thread terminal 或
进程代际变化，飞书只在其 exact、进程内 admission 合同内增加窄 inactive-root 重读。

lease store 无法可靠读取时，相关 main-turn admission/control 必须拒绝；不能猜测 idle，
也不能借 retained record 代替该事实。

## 5. 不延长 main turn 的事实

下列事实可以有自己的 owner、恢复和投递合同，但都不延长 main-turn lease：

- spawned subagent 是否仍 active、是否迟到、是否已完整发现；
- approval/user-input 等 pending interaction；
- 飞书 queued item 与 same-binding FIFO continuity；
- Web/飞书/fcodex 的 subscriber、document 或 socket liveness；
- execution card/page、终态消息、图片、delivery receipt 或 send unknown；
- goal、resume、create、settings 或 lifecycle mutation 的本地事务；
- thread runtime lease、backend generation 与跨实例运行时保护；
- transcript、projection、telemetry、cleanup 与进程内 recovery receipt。

Focus 不持久化 root writer，也不在重启后恢复旧 writer。goal、resume、create 或 control
mutation 的 unknown 最多保留当前进程中用于解释、对账该 exact request/effect 的证据；
它不是 writer，不 replay，也不能隔离无关 thread 或 surface。

## 6. 普通停止

普通停止只对标上游的 active-turn interrupt，并与 main-turn writer authority 分开：

- 飞书侧普通取消先使用 execution/lifecycle owner 已持有的 exact actual active `turn_id`；只有
  execution 正在运行、matching lifecycle id 仍缺失时，才可从同一 binding/root 的 exact prompt admission
  claim 一次 response `turn.id` candidate。本文不把飞书输入、卡片或管理员身份扩成通用
  stop authority；
- 已连接且 materialize 同一 exact direct root 的 Web document，以及 live 且已 attach 同一
  exact direct root 的 `fcodex` endpoint，可以 interrupt 该 root 的 current turn，
  即使它不是 initiator/writer；
- Web 有 exact id 时携无改形的 `thread_id + turn_id`；active/submitting 但 identity 尚不可见时显式携空
  `turn_id`，采用 pinned Codex 0.147.0 current/startup interrupt。Web 用 `thread/read(includeTurns=false)` 证明
  materialized direct root，不从 turns projection 核对或改写 id。`fcodex` 同样语义原样转发 exact-or-empty
  raw ids：非空 id 可由 current connection source 或 matching exact active fcodex lease 证明 attach；空 id 只接受
  current connection 自己的 exact-root runtime source，任何 blank/active lease 都不能替代；
- initiator lease 在这里至多是一份 attach proof，不授予 interrupt effect 的独占权，也不因
  interrupt 而转移、释放或替换 writer；
- 非空 id 只向该 exact turn 发送 `turn/interrupt`；空 id 只使用固定 upstream 的 current/startup path。stale、
  terminal 或 mismatched non-empty id 由上游拥有的 exact-ID rejection boundary 明确拒绝，绝不自动改打后继 turn；固定 upstream
  的 interrupt 只在 app-server projection lock 内核对 id，随后锁外发送无 id 的 core interrupt，
  因而本文不声称 compare 与 effect dispatch 是 core-atomic；
- exact control path 已经选定该 turn 后，不追加 descendant scan，也不把失败扩大成 root/tree
  stop。spawned child 不进入本机共享 interrupt domain，必须在其自身独立合同下控制。

Feishu candidate 在调用前由 admission owner exact claim 并从槽位移除。只有
`turn/interrupt` 尚未跨过 dispatch boundary 的 typed pre-send failure，且同一 claim/admission
仍 current，才恢复 candidate 与 pending cancel，供下一次显式 `/cancel` 或后续 actual lifecycle
使用；known exact-ID rejection、known RPC response 与 dispatch 后 outcome unknown 都消费 candidate。
未取得 actual id 或 candidate 时，只能报告本次未取消并保留本地 cancel intent，不能回复
“已请求停止”。已消费 candidate 不因后续 `turn/started` 自动再 dispatch。

成功的 interrupt RPC response 只证明请求跨过 dispatch boundary 且目标后来 terminal，不证明
terminal status 为 `interrupted`。dispatch 后 transport/protocol outcome unknown 必须单独报告为
“可能已发送，结果未知”；只有 matching `turn/completed.status=interrupted` 才把 execution 标为
cancelled，其他 terminal status 保留其真实语义。

为排查本机多 surface 的中断来源，Web document、飞书 binding 与 `fcodex` endpoint
各自在通过自身准入、选定 exact thread/turn 后、跨越最后一个本地 effect boundary 前，
向既有有界 process log 写一条 best-effort、脱敏的
`turn_interrupt_dispatch_attempt phase=attempt`。`source` 只来自内部封闭词汇，不从 HTTP、JSON-RPC、
飞书 payload 或其他客户端自报字段读取；日志只含 source 与 thread/turn 的短 hash ref，
不含完整 id、用户/聊天身份、prompt、body、token 或 capability。

该日志只证明 Focus 到达了一次本地 dispatch attempt，不证明上游已经收到、接受或结算
interrupt。它不建立 durable journal 或新的 runtime fact，不参与 writer、准入、重试、
lifecycle 或 outcome settlement；日志失败也不能阻止 effect。backend reset 仍是独立控制
合同，不伪装成普通 `turn/interrupt` source；绕过 Focus 直接写 app-server 的客户端也没有
本地 source 证据，Focus 不据此猜测来源。

Focus 不提供泛化 `operator-stop`、descendant scan、durable stop journal 或原子 tree-stop
产品路径，也不宣称拥有上游没有提供的证据。interrupt 结果 unknown 时，最多保留当前
进程中的 exact request/effect 用于对账；它不授予 takeover authority，也不阻塞其他 thread。

## 7. 共享信任不等于 writer

多个前端连接同一个 shared backend 是避免 runtime fork 的必要条件，但不是 writer 权限。
observer、同一用户的另一 tab/socket、同一飞书用户或本机 control plane，都不能仅因连接存在而取得
exclusive writer。普通 Web/`fcodex` prompt 的 realtime dispatch、shared Web/`fcodex` exact-turn steer、第 6 节的 interrupt 与
server-request 合同中的 shared response 都是最小 effect-specific authority；它们都不是
takeover：ordinary prompt 只授权该次 upstream start-or-steer input，三者都不授予 settings、goal、
binding、lifecycle 或 child control。

server-selected Web prompt 使用单 POST 的进程内 `prepare → external effect → exact settle`，并由有界
result receipt 保证 receipt 留存期间同一 mutation identity 只有一个 effect slot；terminal eviction、confirmed
backend retirement 或 service restart 后 seen-identity evidence 消失，同 UUID 可能取得新 slot。官方 browser 在这些
边界只 GET 或为新 gesture 生成新 UUID；F5 不能 reserve、execute、resolve 或 replay。这个边界不改变 main-turn
lease，也不扩张其他能力。详情见
[Focus Web prompt mutation 恢复合同](focus-web-prompt-mutation-recovery.zh-CN.md)。server 在 prepare 时冻结的
non-empty exact `expectedTurnId=A` 语义不变；Focus 仍证明 connected/materialized direct root、
cwd/attachment/source scope、runtime/backend generation 与 exact mutation settlement，但不再用
`thread/read(includeTurns=true)` 的 projected active id 作发送前否决。upstream core 若报告 successor B 或 no-active，
本次结算 known-no-effect 且不自动改投 B 或 fallback `turn/start`。
effect 后的 unknown 只能由 matching server-derived `clientId` 等正面证据确认，不取得发送前 lifecycle authority。

显式飞书 `/steer <text>` 是另一条 method-specific exact-turn contribution authority，不是普通 prompt
或 writer handoff。它只接受当前 attached/running binding 已镜像的非空 exact turn（包括 active observer），
执行 group `all` exclusivity、权威 direct-root active 重读、backend connection generation fence 与最终
binding/execution CAS 后，调用一次官方 `turn/steer`。它不取得 lease、不入 FIFO、不携带 next-turn settings、
不消费附件，也不 fallback 或自动 retry；known rejection 报告未加入，dispatch 后 unknown 报告可能已发送且
不改打 successor。完整边界见[飞书 thread 生命周期合同第 5.3.2 节](feishu-thread-lifecycle.zh-CN.md#532-显式-steer-exact-turn-contribution)。

共享 exact-turn authority 不会让 Focus 主动把飞书普通 prompt 改发 `turn/steer`。所有真正发往上游的
飞书普通 prompt——即时、出队或 synthetic——都使用官方 `turn/start`。上游 idle 时通常开始新 turn；
若 Web、`fcodex` 或 resume 后的 autonomous goal continuation 在极窄窗口内先 active，官方
start-or-steer 会把飞书 input 与本轮 settings 加入该 active regular turn。这笔 input 已被上游消费，
不是排队拒绝，也不能自动重发；`/compact` 仍使用独立合同。

飞书普通 prompt 可以进入一个 exact binding 的进程内 FIFO，并在 matching terminal 后作为下一 turn 执行。
准入只使用本 binding 的 exact execution anchor、既有 same-binding/root/epoch continuity，或 projection 前在
shared binding lock 内再次核对仍完全一致的本进程、非飞书、非空 exact active lease。普通 Web/`fcodex`
prompt 不创建该 preprojection 证明；但飞书 mirror 已有非空 exact turn 后，其 execution anchor 本身足够，不要求
origin lease。`turn/start` response、其中返回的 `turn.id`、writer denial 或“本地还没看到 lease”都不能自行建立 FIFO
continuity。同一 root 最多一个飞书 binding 保留 continuity；后续项只保持
same-binding/root/epoch 顺序。另一 binding、foreign/stale process evidence、blank 或 mismatched turn evidence
与 `/compact` 仍然拒绝。matching lifecycle-terminal 是唯一 wake-up；不补 timer、scheduler、持久化、自旋或自动重发。

Focus Web“运行详情”中的 active-turn disclosure 只是只读展示，不是 authority。initiator 只有在 main-turn lease 与当前
authoritative active turn id 一致时才分类；Feishu audience 只列当前 attached subscriber。`turn/started`
冻结当时已经由 response/settings event 证明的 connection-local thread base；具体值和 known-null 标为
`inherited`，缺失字段为 `unknown`，后续 base 变化不回填 active snapshot。只有 matching model reroute 标为
`active_reroute`；不能拿 instance-wide `WebNextTurnSettings` 或飞书 next-turn settings 冒充 active setting。

browser 用 process-local revision floor 排序这份 disclosure，而不是建立第二个 runtime owner。任何
`owner_changed` 或 `thread_invalidated` 都推进 exact thread floor；turn start/completion 或 active-turn
identity 变化、`model/rerouted`、`thread/settings/updated`、archived/closed/deleted lifecycle，以及
non-active thread status 也同样推进。`backend_disconnected` 与 `projection_invalidated` 则推进 global
floor。若 response 只落后于无关的较新 stream state，只有在覆盖适用的 thread/global floor 时，才可替换
exact matching 的 `active_turn_context`；它不能回滚其他 snapshot 字段、触发 disclosure polling/retry
loop，或授权 writer、lifecycle、settings、approval、FIFO 与 mutation effect。

相反，idle thread 也不因旧 frontend 曾经拥有它而继续被占用。没有 active/submission lease
就是 main-turn 层的 idle；其他领域的精确 mutation/recovery gate 必须说明自己的 effect，
不能重新包装成 durable root writer。

## 8. 上游依据与实现

本合同固定对标公共上游
[`openai/codex@be6e8eac029b183056b7e4402879f15d2c85f61b`](https://github.com/openai/codex/commit/be6e8eac029b183056b7e4402879f15d2c85f61b)
（release `rust-v0.147.0`），不引用某台机器上的 checkout：

- [`turn_processor.rs#L474-L607`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L474-L607)
  原样返回普通 `turn/start` 的 upstream `turn.id`；
- [`session/mod.rs#L789-L830`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/session/mod.rs#L789-L830)
  创建该 turn identity，而
  [`handlers.rs#L189-L270`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/session/handlers.rs#L189-L270)
  才执行 actual start-or-steer admission；idle-path 测试
  [`turn_start.rs#L227-L253`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/tests/suite/v2/turn_start.rs#L227-L253)
  证明无竞态时 response、started 与 completed id 一致；Focus 仍不据 response 单独激活 lease；
- [`turn_processor.rs#L1249-L1268`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L1249-L1268)
  与 [`review.rs#L116-L179`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/tests/suite/v2/review.rs#L116-L179)
  证明 inline review response id 是 matching review lifecycle 使用的 actual turn id；
- [`thread_processor.rs#L1876-L1887`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/thread_processor.rs#L1876-L1887)
  证明 compact response 为空；
- [`turn.rs#L175-L203`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L175-L203)
  固定 `turn/steer` 的非空 exact `expectedTurnId`、input 与 response `turnId` 协议；
- [`tasks/mod.rs#L783-L824`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/tasks/mod.rs#L783-L824)
  与 [`thread_events.rs#L124-L139`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/tui/src/app/thread_events.rs#L124-L139)
  证明 matching terminal 后 active main turn 立即清除；
- [`turn_processor.rs#L1409-L1471`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L1409-L1471)
  对非空 interrupt 在 app-server lock 内核对 exact active-turn projection，随后锁外向 core
  发送不带 id 的 `Op::Interrupt`；
- [`bespoke_event_handling.rs#L1499-L1522`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/bespoke_event_handling.rs#L1499-L1522)、
  [`#L187-L202`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/bespoke_event_handling.rs#L187-L202) 与
  [`#L1096-L1112`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/bespoke_event_handling.rs#L1096-L1112)
  共同证明 natural completion 与 aborted terminal 都会完成 interrupt RPC，只有 aborted lifecycle
  投影 `turn/completed.status=interrupted`；因此 `{}` 不是 confirmed interrupted；
- [`subagent_notifications.rs#L1611-L1707`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/tests/suite/subagent_notifications.rs#L1611-L1707)
  覆盖 parent 新 turn 与仍运行 child 的后续 mailbox delivery。

主要实现位置：

- `bot/stores/interaction_lease_store.py`
- `bot/feishu_root_operation_controller.py`
- `bot/web_runtime/operation_service.py`
- `bot/web_runtime/mutation_recovery.py`
- `bot/active_turn_disclosure.py`
- `bot/feishu_execution_queue_service.py`
- `bot/feishu_turn_steer.py`
- `bot/thread_access_policy.py`
- `bot/thread_runtime_authority.py`
- `bot/fcodex/main_turn_owner.py`
- `bot/adapter_event_bridge.py`
- `bot/adapter_notification_pipeline.py`

若这些实现或其他 active 文档仍把 retained root/descendant、展示或 endpoint liveness 写成
普通 main-turn writer authority，以本文为准；该差异是待删除的合同漂移，不是兼容要求。
