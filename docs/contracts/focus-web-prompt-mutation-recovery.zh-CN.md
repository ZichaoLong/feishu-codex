# Focus Web prompt mutation 恢复合同

文档角色：中文规范源。英文同步副本：`docs/contracts/focus-web-prompt-mutation-recovery.md`。

本文定义 Focus Web 向 existing thread 提交普通 prompt 时的单 POST、单次 upstream effect authority、
进程内 result receipt 与 Composer 结算边界。它不把上游 `turn/start` / `turn/steer` 升级为跨进程
exactly-once，也不定义 fcodex、飞书普通 prompt/FIFO、显式飞书 `/steer`、review、compact、interrupt
或 thread-create identity。

thread-create 已知 created thread、但首 prompt typed unknown 的 browser-local text-only record 仍由
`thread-create-local-commit` 合同拥有；本文只要求 existing-thread prompt 不复用或覆盖那项记录。

## 1. 用户结果与 upstream 边界

当前复核的公开 upstream 基线是 Codex CLI `0.147.0`：
[`openai/codex@be6e8eac029b183056b7e4402879f15d2c85f61b`](https://github.com/openai/codex/commit/be6e8eac029b183056b7e4402879f15d2c85f61b)。
官方 `turn/start` 保留 start-or-steer 语义；`turn/steer` 接受 non-empty exact
`expectedTurnId`，并在当前 active turn 缺失或不匹配时明确拒绝。公开证据见
[`turn_processor.rs#L474-L607`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L474-L607)、
[`turn_processor.rs#L908-L976`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L908-L976)
与 [`session/mod.rs#L3957-L3993`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/session/mod.rs#L3957-L3993)。
上游 TUI 会从版本特定错误文本中取得 successor 并最多自动 retry 一次；这是 TUI 产品行为，
不是 app-server 的 delivery guarantee，见
[`app.rs#L678-L737`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/tui/src/app.rs#L678-L737)。

Focus 的最小有意差异只覆盖多前端共享 backend 的已证明场景：server 已冻结 active turn A 后，
若上游明确报告 successor B 或已经没有 active turn，Focus 把本次分类为 `known_no_effect`，保留 live
Composer 的原输入，且不自动改投 B 或 fallback `turn/start`。B 可能由 Web、fcodex、飞书或 autonomous goal
为另一语境开启；用户查看新上下文后再明确点击一次。只有 prepare 时根本没有 exact active id 的 attempt
才选择 `mode=start` 并调用一次官方 `turn/start`。一个 browser attempt 因而只发一次 turn-producing RPC，
并至多产生一个 upstream input effect。

Web 只有一个 submit gesture。Enter 与发送按钮进入同一个同步入口，Shift+Enter 插入换行；普通
prompt 不建立 browser FIFO。每次 gesture 只产生一个 canonical mutation identity 和一个 POST。
Focus 不自动重发 outcome unknown，不从 transcript 中缺少 input 推断 no-effect，也不让一个 unknown
prompt 阻塞同 thread 的新 mutation、其他 thread 或其他 surface。

## 2. Identity、receipt 与唯一 owner

`web/src/components/chat/composerSubmission.ts` 的 `ComposerSubmission` 是 press-time text/chips 是否仍
pending、是否可以清空当前 Composer 的唯一 browser-local owner。它不拥有 upstream effect、turn identity
或 result receipt。

浏览器在 yield 给 HTTP 前冻结 canonical UUID `mutation_id` 和 exact payload。POST body 必须恰好包含：

- `text`；
- `attachment_ids`；
- `mutation_id`；
- press-time `source_scope_generation`、`source_attachment_scope` 与
  `source_composer_scope_id`。

当前 `client_id` 与 exact document receipt 来自 Gateway 已认证的 request/header，不由 body 重报；runtime epoch、
backend connection generation、active turn 与 route 由 server 在 prepare 时捕获。server 必须从 mutation id 派生
exact `client_user_message_id = "focus-web:<mutation_id>"`。browser 不得发送或覆盖该值。

browser 对 existing-thread prompt 只可在当前 Tab 的有界 `sessionStorage` 中保存
`(thread_id, mutation_id)` locator，以便 F5 后查询。它不得保存这笔 existing-thread prompt 的 payload、attachment
receipt、recovery bearer、server generation 或 execute continuation。locator 不是 effect authority，不参与
`canSubmit`，也不能用来构造另一笔 POST。
若写入 `sessionStorage` 失败，locator 只降级为当前 document 的内存记录，绝不能阻止唯一 POST；明确的可用性成本是：
若随后丢失原 POST result，F5 后无法继续这项 GET 查询。

进程内 `WebPromptResultRegistry` 是 exact mutation result receipt 的唯一 owner。registry 按 exact
`(thread_id, mutation_id)` 索引一条有界 receipt，只保存 identity、route/result coordinates 与状态，不保存 prompt
payload、attachment ids、document token 或可重放请求。它不写盘、不跨 Focus service restart，也不成为 upstream
lifecycle 或 main-turn owner。terminal receipt 淘汰或 service restart 后的 miss 只表示 Focus 已没有本地解释，绝不
证明 effect 未发生。

一条仍留在 registry 中的 mutation identity 只有一个 effect slot。receipt 留存期间，重复 POST 只能读取同一
`pending`/terminal receipt，或在 identity/scope 不一致时 fail closed，不能取得第二个 slot；mutation id 用于另一
thread、document incarnation 或不同 source scope 时，也必须在任何 upstream effect 前拒绝。

这是显式的 process-local、retention-bounded 保证，不是永久 idempotency key。terminal eviction、confirmed backend
epoch retirement 或 Focus service restart 会删除唯一的 seen-identity evidence；之后 server 无法区分“新的 canonical UUID”
与客户端刻意重用的 expired UUID，后者可能重新取得一个 slot。官方 browser 从不对 locator 重新 POST：F5/poll 只 GET，
新的 submit gesture 总是生成新的 mutation id，因此不会自动触发这项有界非保证。若要在这些边界之后仍拒绝同 UUID，
必须新增 durable/unbounded spent-identity authority；本文明确不作此保证。

## 3. 单 POST prepare / effect / settle

Gateway 先验证 exact closed body、connected document 与 materialized direct-root target，再经 service ingress barrier
进入一笔 staged transaction：

1. **prepare（RuntimeLoop 内）**：只校验并冻结 Composer receipt 的 shape/identity、exact document/target、mutation
   identity、server-derived client message id、backend connection generation、read observation，以及当时的 exact
   active turn A；这里不读取 `WebWriterProfileStore`。存在 A 就冻结 `mode=steer` 与 A；不存在 exact id 就冻结
   `mode=start`。prepare 安装或复用同一 exact `pending` receipt，随后立即释放 RuntimeLoop。
2. **effect（external worker）**：fresh worker 先从 `WebWriterProfileStore` coherent external load 一份 snapshot，exact
   比较其中的 `selected_thread_id`、`scope_generation` 与冻结的 Composer receipt；只有通过这项检查，原 gesture
   才取得继续执行的授权。该检查必须在 attachment claim 与 turn-producing RPC 之前；失败保持 known no-effect。
   随后在原 service-ingress receipt 与 exact backend generation pin 下完成所需 direct metadata proof、attachment
   claim 和 upstream RPC。RuntimeLoop 不等待 app-server/store I/O，也不持有 browser document lock。
3. **settle（RuntimeLoop 内）**：只凭原 immutable receipt 对 exact registry generation/observation 做 CAS，安装一项
   terminal result。replacement document、backend generation、runtime epoch 或 newer observation 不能被迟到 result
   覆盖。settlement 之后的 list/read 只做后台 projection convergence。

`mode=steer` 必须把冻结的 A 原样作为 `expectedTurnId` 发出一次；发送前不再用 paginated/stale turns projection
替换或否决 A。successor B mismatch、no-active 与其他 steer rejection 都是 authoritative no-effect，结算为
`known_no_effect` 且绝不 retarget B 或 fallback `turn/start`。只有 prepare 时根本没有 exact active id 才选择
`mode=start` 并唯一调用官方 `turn/start`；该路径保留既有 goal/next-turn safety，但不显式调用
`thread/resume`，也不增加本地 compare-and-set start、writer lease 或额外 replay guarantee。

attachment store 使用既有 exact submission claim/pin：known-no-effect 在返回该结果前尝试 exact rollback；known success
或 outcome unknown 保持 submitted。若 upstream text effect 已 authoritative known-no-effect、但 rollback 返回 false 或
抛错，receipt 仍为 `known_no_effect`，同时必须携 `reason_code=attachment_rollback_failed`。这个 code 只说明旧附件
无法证明可复用，不把已知未发送的 text 改写成 effect unknown。attachment claim 不成为 result registry 的 payload
store，也不允许 GET 重新发送附件。

effect 进入 transport 后若无法取得权威 response，结算为 `outcome_unknown`。正常 RPC success，或后续 authoritative
transcript 中出现 matching server-derived `clientId`，都可以把 exact receipt 结算为 `succeeded`。一次 read 未看到
该 id 不是 no-effect evidence；history reconstruction 也可能缺失 `clientId`。terminal result 发布失败或 post-effect
projection refresh 失败不能把已知 success 改写成未发送。

external worker 从 prepare 到 settle 全程计入 service shutdown barrier。executor admission failure、handler cancellation
或 shutdown 只有在已知未越过 effect boundary 时才能结算 known-no-effect；一旦 transport outcome 不明就只能结算
unknown。旧 worker 无权 settle replacement backend/epoch，也不能自动 replay。

## 4. Result receipt、F5 与 HTTP wire

POST `/api/threads/{thread_id}/prompt` 的 admitted result 与
GET `/api/threads/{thread_id}/prompt-result/{mutation_id}` 的查询结果使用同一 exact
`FocusPromptResultReceipt`，required fields 为：

- `thread_id`、canonical `mutation_id` 与 server-derived `client_user_message_id`；
- `status = pending | succeeded | known_no_effect | outcome_unknown`；
- `mode = start | steer`；
- `turn_id`：steer 时始终是冻结的 exact A；validated `mode=start + status=succeeded` 必须保留 upstream
  `turn/start` response 的 authoritative `turn.id`。`pending`、pre-effect 或 unknown 没有该证据时为 `""`；
  request/submission tracking id 不得伪装成 actual turn identity；
- `reason_code`：没有附加分类时为 `""`。`attachment_rollback_failed` 精确表示 text effect 已知未发生、但旧 attachment
  chips 不能安全复用；其他值只解释本 request，不取得 lifecycle 或 retry authority。

原始 POST 在 admitted transaction 完成 safety settlement 后以 HTTP 200 返回 terminal receipt；同 identity duplicate
可以读到已有 `pending` 或 terminal receipt。strict validation、document/target/source-scope admission failure 继续使用
effect-free HTTP error，不伪装成 result status。transport loss、malformed response 或 HTTP 5xx 对 browser 而言都是
possibly-sent，不能授权自动重试。
browser 只有在严格解码的 Focus error envelope 与本 prompt endpoint 的闭合 HTTP 合同共同建立显式 pre-effect
evidence 时，才因 HTTP refusal 保留原输入。只有 status code、proxy/non-envelope response，以及任何 HTTP 408
都仍是 possibly-sent；browser code 不得从 4xx 范围或 error 文本猜测 no-effect。

POST endpoint 的闭合 pre-effect code set 是 `unauthorized`、`csrf_failed`、`invalid_client`、
`document_unregistered`、`document_replaced`、`invalid_json`、`invalid_prompt`、
`invalid_mutation_id`、`invalid_attachment`、`invalid_submission_scope`、`empty_prompt`、
`invalid_thread`、`web_writer_disconnected`、`thread_not_materialized` 与
`prompt_result_capacity`。尤其是，POST 上的 `prompt_result_unavailable`、`prompt_result_pending`、
`prompt_mutation_conflict` 与任何未识别 Focus 4xx code 都仍是 possibly-sent：它们可能描述一项 effect slot
已经执行的 identity 或 receipt。lookup-only GET 可独立把严格的 `prompt_result_unavailable` 视为有界本地
receipt 已无法再查询的权威证据；这不是 POST no-effect evidence。

GET endpoint 无 body、无 bearer、无 mutation action。它仍要求当前 authenticated document 已 connected 且 materialize
exact thread；只读取有界 registry receipt。F5 或短暂轮询只能发送这个 GET，绝不能发送 POST、reserve、execute、
resolve、discard、retry、attachment restore 或任何会取得 effect slot 的请求。`pending` 只说明原 worker 尚未 terminal；
receipt miss/eviction/restart 只产生 unavailable/unknown presentation，不开放 retry，也不阻塞新 mutation。
browser 必须明确展示每一种非 success receipt，以及 unavailable 或 transient GET failure，并同时说明没有重放 Prompt。
只有 `succeeded`、`known_no_effect` 或权威 `prompt_result_unavailable` 才删除 locator；`pending`、
`outcome_unknown` 与 transient lookup failure 都保留 locator，供稍后的 GET 再次核对。

matching transcript `clientId` 是把 exact `outcome_unknown` 提升为 `succeeded` 的正面证据。查询和 transcript
reconciliation 都不重放 effect；`known_no_effect` 是唯一允许 live original Composer 把本次 payload 当作未发送保留的
terminal status。

## 5. Composer settlement 与 browser 恢复边界

Composer 在 POST 前不清空 text/chips。terminal safety result 到达时：

- `succeeded`：commit exact `ComposerSubmission` 并清空仍匹配的 press-time payload；
- `outcome_unknown`，以及 transport/decoder ambiguity：按 possibly-sent commit，提示结果未知，但不自动重发；
- 普通 `known_no_effect` 或 authoritative pre-effect HTTP refusal：retain exact payload；只有 exact attachment rollback
  已完成时才保留对应 chips；
- `known_no_effect + attachment_rollback_failed`：若 exact `ComposerSubmission` 仍是 current owner，原子 retain text-only
  并移除旧 chips；若 owner 已被新输入替换，不触碰 newer content，只明确提示用户手工移除并重新添加旧附件。若窄清理
  本身失败，则对该 exact Composer payload 做保守 UI commit，绝不留下可一键复用的旧 chips。这里的 commit 只结算
  attachment/UI safety，不声称 text 已发送；
- 迟到 settlement 永远不能清除用户已经输入的 newer Composer 内容。

上述 commit/retain 完成后 HTTP mutation owner 立即返回；thread list/read、turn projection、presentation event 与
telemetry 都在后台收敛，不能继续让输入框或发送按钮 pending。projection 失败只影响显示，不反转 mutation settlement。

existing-thread locator 不保存 payload，因此 F5 后的 GET 只解释结果，不能恢复 reload 前的 text/chips，也不能创建
retry draft。`known_no_effect` 的“保留原输入”保证只适用于仍持有 exact `ComposerSubmission` 的 live document；这是删除
server payload replay 与 bearer recovery 后的明确可用性成本。用户仍可用新 mutation id 自行重新输入，但 Focus 不替用户
复制或发送旧 payload。

thread-create 首 prompt typed-unknown 的 text-only `sessionStorage` record 与显式 handoff/discard 完整保留。它没有
existing-thread mutation id、client-message id 或 server receipt，且任何 reload path 都不得把它升级为 POST replay。

## 6. 删除项与不受影响的能力

以下旧机制不再属于 Web prompt 合同，production 与 wire 必须原子删除：

- browser/server `dispatch → reserve → execute` 与两阶段 steer continuation；
- raw `recovery_capability`、server digest、base/reservation generation、per-thread high-water；
- `reserved` / `executing` continuation、server-retained prompt payload 与 replay；
- steer-specific resolve/retry/discard、attachment restoration、backend-replaced explanation；
- thread snapshot `mutation_generation`、`steer_attempt_result` 与 browser steer-attempt decoder/storage。

generic `/mutation-unknown` 继续只服务 archive/unarchive/delete 与既有 lifecycle/control mutation；其 process-local unknown
record、disposition 与 UI vocabulary 不因本文删除。thread-create local record也不合并进 prompt result registry。

普通 Web prompt 不读取或取得 main-turn lease，不改变飞书 FIFO、显式 `/steer`、fcodex realtime input、shared approval、
interrupt、settings、goal、binding 或 backend-reset authority。一个旧 `pending` / `outcome_unknown` receipt 只解释 exact
mutation，不能阻止同 thread 新 mutation 或任意其他 surface。

## 7. 验证与停止条件

回归至少覆盖：single POST；slow RPC pending 时 RuntimeLoop sentinel/realtime admission 继续完成；start/steer route；
exact A success；successor B mismatch no-retarget；no-active known-no-effect 且 no-fallback；known-no-effect attachment rollback；success/unknown
附件保持 submitted；RPC success、typed unknown 与 transcript positive reconciliation；receipt 留存期间 duplicate mutation
无第二 effect，eviction/backend retirement/restart 后 expired UUID reuse 是显式有界非保证；document/backend/runtime ABA；
cancellation、executor failure、shutdown barrier；POST response loss；GET-only F5/poll；malformed response；Composer
immediate commit/retain 且不等待 projection refresh；newer Composer
不被迟到 result 清除；同 thread 新 mutation、其他 thread 与其他 surface 不受 unknown 影响；thread-create local recovery
仍为零 server replay。

若 correctness 需要 durable effect ledger、跨进程 idempotency/replay、上游不存在的 negative effect evidence，或把 exact
prompt unknown 扩散成 thread/service 不可用，必须停止并重新对齐。
