# Server Request 生命周期合同

文档角色：中文规范源。英文同步副本：`docs/contracts/server-request-lifecycle.md`。

## 1. 目的与上游基线

本文定义 Focus 如何把 Codex app-server request 投影到 Web、飞书与 `fcodex`。
合同刻意对标上游 Codex，不另造一套更严格的 durable callback 生命周期。

本次审阅的上游基线是 Codex commit
`be6e8eac029b183056b7e4402879f15d2c85f61b`：

- app-server 在发送前，把每个 pending callback、thread id 和完整 request 存入进程内 map；
- server request 会广播给该 thread 的所有 subscribed connection；
- running `thread/resume` 先把 connection attach 到 thread，返回 resume response，
  再按 request id 顺序 replay matching pending request；
- 第一份 client response 原子消费 callback，随后 app-server 向当前 subscribers 广播
  `serverRequest/resolved`；
- turn start/completion/abort 会清除该 thread 的 pending callback。

对应上游实现固定为公开且不可变的源码证据：

- [pending callback 保存、广播、replay 排序与 first-response 消费](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/outgoing_message.rs#L287-L466)；
- [resume response 后 replay pending request](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/thread_lifecycle.rs#L721-L755)；
- [turn lifecycle cleanup](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/bespoke_event_handling.rs#L157-L190)。

## 2. 状态归属

callback 是否真的 pending，归 Codex app-server 所有。Focus 不持久化第二份 callback ledger。

`ServerRequestRegistry` 只持有当前 Focus 进程、当前 app-server connection epoch 的投影：

- key 是规范化的 typed JSON-RPC request id；
- value 是一份 immutable `ServerRequestIdentity`，包含 receiving connection
  generation、method 与 params 深拷贝；
- 小型 resolved set 只用于压制同 epoch 内本地结算后的 replay；
- dispatch outcome unknown 只标记该 exact request。

Web、飞书与 `fcodex` 分别只持有自己的 delivery/card/action 状态，引用 canonical
identity，并增加一次性 response capability 或 token；它们不持有上游 callback 生命周期。

server request 不再有 durable request/root/global settlement fence。pending request、unknown
response、parent relation 缺失或局部 projection 失败，都不能延长 main-turn lease，也不能隔离
另一 request、thread、surface 或 backend generation。

## 3. Reader 顺序与路由

普通 stateful server request 到达时，`CodexRpcClient` 在 websocket reader 顺序中同步调用
轻量 request callback。callback 只向 `RuntimeLoop` 排队，不执行 surface I/O。因此后到的
lifecycle notification 必然排在前一 request frame 之后，不需要 reader write-ahead store 或
detached-callback barrier。

进入 `RuntimeLoop` 后，`ServerRequestCoordinator` 注册 identity，并调用
`ServerRequestSurfaceDispatcher`：

- 新 identity 被交给选中的 surface；
- exact replay 复用同一对象，可以重建或刷新局部 projection；
- 同 key、不同 envelope 的冲突只拒绝该 request；
- known-not-committed 可由之后的明确 replay 再试；
- outcome-unknown 不自动重试，但不阻止无关 request。

dispatcher 只是选择边界，不是另一份状态 owner。surface 只有在保存局部 request record 后才返回
committed claim；decline 必须表示没有产生局部或外部 effect。

当前 backend epoch 中，带非空 `turnId` 且属于 exact direct root 的 canonical command-execution、
file-change 与 permission approval 使用 `shared_approval` 路由：同一 canonical identity 分别投影给
合格的 fcodex、Web 与飞书入口。`InteractionLeaseStore` 只拥有 Focus writer，不是审批 callback 的
准入事实；因此正常 goal continuation 形成的无 writer autonomous turn 也使用同一审批路由。

具有同样 direct-root 与 non-empty-turn 证据的普通 interactive callback 使用 `shared_interaction`
路由。用户输入、MCP elicitation 与 dynamic tool call 分别交给 fcodex、Web 尝试；Web 可以拒绝自身
无法展示的方法或 shape，包括 dynamic tool call。任一 desktop surface 保留 callback 后，不再向飞书
制造重复投影；只有两个 desktop 都权威 decline，才 fallback 到原有飞书 single-surface 路径。desktop
outcome unknown 时禁止该 fallback，但仍会尝试另一个 desktop。auth、unsupported method、empty-turn 与
child-thread request 保持原 single-surface 路径，不能从 root 继承 shared authority。

`shared_approval`、`shared_interaction` 与 Web inbox 的 delivery scope 都是 server-local route fact。
`project_pending_request` 明确不把 scope 投影进 Focus Web DTO；browser 只收到 exact request projection
与 response capability。因此本次变更不提升 Focus Web wire v9。

## 4. Response Authority

adapter 把 response 固定到收到 request 的 websocket generation；app-server 只接受 callback 的
第一份 response。

Focus 只增加多前端所需的最小本地 ABA 保护：

- Web 与飞书使用 exact、一次性的 `response_capability`；
- 同一 canonical shared interaction 的每个合格 `fcodex` endpoint 都获得自己 exact、一次性的
  `response_token`；
- 旧 tab、旧卡、旧 proxy connection 或 value-equal replacement object 不能回答新 request；
- 已证明 pre-send 失败时，该 exact action 可以保持可重试；automatic fail-close 只在明确的
  exact upstream replay 上重试；
- transport outcome unknown 时，只禁止该 exact action 重试，直到 matching resolution/lifecycle
  cleanup 或 connection epoch retirement；exact replay 不会重新开放它。

presentation 与 delivery 不是 lifecycle authority。浏览器断线只隐藏该 document 的 shared-interaction
projection，不替用户回答、也不删除 canonical request；重连后仍合格的 document 可以再次收到它。
writer-owned single-surface interaction 仍按原合同清理，本合同不借重连把它转移给另一 document。若
automatic fail-close write 被证明未发送，之后明确的 upstream replay 仍可按原 route 生成 projection。

shared interaction 允许所有已认证、live 且已 materialize exact direct root 的 Web document 与
fcodex endpoint 回答各自能展示的方法。第一份有效 response 赢得 canonical app-server callback；其他 action
收到 typed superseded/unknown receipt，或被 `serverRequest/resolved` 退休，绝不产生第二份 adapter
response。无效的 fcodex response 只退休该 endpoint token，不替其他 endpoint 回答或取消。回答 interaction
不转移 turn initiator，不改变 active settings、输出目的地或下一 turn admission。
可信本机端展示 upstream 协议允许的全部 response。command approval 优先遵守该 request 的
`availableDecisions`；file 与 permission approval 使用各自 schema 定义的 response set，包括 session
approval 与 permission `strictAutoReview`。Focus 不制造 upstream 协议没有定义的 response。

shared response authority 来自 `ServerRequestRegistry` 当前 connection generation 中的 exact canonical
identity，以及 surface 自己核对的 direct root、非空 turn、live endpoint 与 materialized subscription；
它不来自 main-turn writer lease。proxy-first projection 可以先展示，但只有 canonical identity 绑定后才能
向 adapter 提交 response。非审批 desktop surface 只有证明至少一个 live recipient 后才 claim；否则必须
decline，让 dispatcher 保留飞书 fallback。shared Web user-input auto-resolution 是绑定 canonical request、
backend epoch 与 exact timer generation 的一次 system-owned transaction；它不需要 document writer，也不会仅因
某个 browser 断线而取消。

群停用能够取得 exact Feishu binding 与非管理员 turn actor 证据时，Focus 先尝试一次 canonical
fail-close，并单独撤销该 current-epoch exact identity 的用户 response authority。撤权不等于声称
cancel 已提交：response effect phase 仍独立保留为 `pending`、`submitted` 或 `unknown`。因此明确
pre-send 失败的请求仍是未解决 blocker，但全部旧 Web/fcodex/飞书 capability 都会被中央拒绝，exact
replay 也不能重新授予。只有 matching resolution/lifecycle cleanup 或 epoch retirement 同时清除这两项
事实；无关 request 不受影响。撤权会立即隐藏 exact Web projection 并发布 pending-request change；之后
接入的 fcodex endpoint 在签发 fresh token 前就会被拒绝。已渲染的 fcodex overlay 仍可能保留到该 action
收到 typed superseded receipt，或真实 upstream cleanup 到来，因为 Focus 没有 service→proxy 展示推送。

飞书仍是 exact-binding projection，不向其他 chat 广播；合格可信本机端仍可回答同一 canonical approval。
普通 shared interaction 仅在两个 desktop surface 都权威 decline 后进入飞书原 single-surface 路径；其 queue
与 card lifecycle 其余行为不变。交互卡 send/reply 首次结果 unknown 时不立即重试。飞书官方 create/reply 合同只保证同一 UUID
在一小时内至多成功一次，因此 Focus 只在进程内 projection 保存 exact UUID、不可变 publish intent，以及
首次调用前的 wall/monotonic 时间。canonical resolution 到达后，仅当两套时钟都仍处于保守的50分钟窗口内，
才使用同一 UUID 对账一次；过期 intent 绝不重放。confirmed 后取得 exact message id 并立即更新。若该次
结果 rejected、仍 unknown、effect identity 漂移或已过期，Focus 不再拥有后续 delivery authority，不替
用户回答，也不发起第三次请求。官方依据：
<https://open.feishu.cn/document/server-docs/im-v1/message/create.md>、
<https://open.feishu.cn/document/server-docs/im-v1/message/reply.md>。

## 5. Resolution、Lifecycle 与断线

`serverRequest/resolved` 只结算 request id 与 thread id 都 matching 的 request。registry 结算后，
coordinator 取消 timer，删除 exact Web/飞书/fcodex projection，并只 reconcile 受影响的本地 root。
从未见过的 notification 是局部 `missing`，不能凭空制造 tombstone。

局部 cleanup 只是 best-effort presentation convergence，不是 settlement authority。一个 surface
remover、root lookup 或 root reconciliation 失败时必须记录日志并继续其他 surface cleanup；已经完成的
canonical settlement 不能被逆转。

matching turn/thread lifecycle fact 按 reader 顺序退休该 thread 当前进程内的 request 与 projection。
这是对 app-server callback retirement 的投影，不是第二套 durable settlement transaction。

app-server connection 丢失时，Focus 清除 registry epoch、timer 与旧 surface capability。之后的新
connection 配合 `thread/resume` replay，创建新的 generation-pinned identity 与 projection。Focus
不从磁盘恢复 request，也不跨进程重启保留隐藏 blocker。

frontend transport disconnect 的边界更窄：它只退休对应 Web document 或 fcodex endpoint 的
capability，不回答、也不删除 canonical shared interaction。只要同一 Focus/app-server 进程与 backend
epoch 仍在，Focus 进程内的 Web inbox 保留 canonical shared interaction。Web document 从 disconnected 变为
connected 时，只有当它当前对某个已 materialize exact root 的 shared interaction 重新满足准入，Focus 才发布一次
`pending_request_changed`；同一client的第二个socket不会重复发布。Gateway在该连接事务完成后发送的hello携带
新revision，browser随后从HTTP current pending集合原子重建projection。F5后的新document不需要伪装成旧Tab；
它以自己的identity重新materialize并满足准入即可。

这一同epoch重投影不调用也不伪造`thread/resume`。只有Focus到app-server的connection epoch真正丢失后，
registry与旧capability才被清除，并由后续真实`thread/resume` replay创建fresh canonical identity。重建时
`pending`保持可回答，`processing/submitted/unknown`保持不可重复回答；resolved、revoked、hidden或inactive
generation request不复活。durable queue、物理Tab identity与跨重启恢复明确不在本合同内。

## 6. Subagent 与 Main Turn

Focus 不观察或重建 child lineage，也不把 child callback 重绑到猜测的 root。只有当前 connection
epoch 中由权威 `ThreadSummary` 证明为 direct root 的 exact request thread，才可进入 shared interaction domain；
无法证明的 callback 只按 surface 合同 decline 或 fail-close 该 exact request，不取得 root lease、
不扩大 request fence。完整边界见
[`subagent-observation-and-recovery.zh-CN.md`](subagent-observation-and-recovery.zh-CN.md)。

共享 main-turn lease 独立结算。matching `turn/completed` 必须立即释放 main-turn writer，不等待
request card、child thread、delivery projection 或 cleanup。

## 7. Backend Reset

backend-reset preview 只记录当前 pending 数量用于诊断。replacement transaction 随后停止 owned
backend，退休 registry 以及所有旧 generation 的 surface/transport capability，再启动新 backend。
它不执行 pre-stop per-request response transaction，也不替换 durable interaction epoch。

## 8. 必须保留的回归边界

测试至少覆盖：

- request callback 早于后续 lifecycle notification 入队；
- exact replay 复用对象，connection replacement 后创建 fresh identity；
- exact resolution 与 matching lifecycle 删除三个 surface projection；
- Web↔Web、Web↔fcodex、fcodex↔fcodex shared first-response convergence；
- late attach、endpoint disconnect、resolved-first/response-late 顺序与 bounded same-epoch late receipt；
- 飞书 exact binding 与 same-UUID unknown-card reconciliation；
- 群停用的 submitted/unknown/not-sent effect phase 与独立 exact authority revocation，包括 proxy-first
  stale token 拒绝；
- command/file/permission 的完整 approval option mapping；
- shared interaction 不改变 initiator、settings、destination、下一 turn admission；unsupported、child 与
  empty-turn request 继续 single-surface；
- active-goal autonomous turn 即使没有 Focus writer lease，当前 canonical approval 仍可由合格本机端回答；
- 普通 interaction 的 two-desktop fanout、first-response convergence、unknown 不 fallback 飞书，以及两个
  authoritative decline 后才 fallback 飞书；
- shared user-input timer 跨 document disconnect 保留，同时仍 pin exact callback、backend/timer generation；
- stale surface capability/token 被拒绝；
- pre-send 与 outcome-unknown response 分类；
- 同epoch Web断线、F5、新document与第二socket按current pending重投影或保持清空，以及connection replacement后的
  resume replay重建；
- identity conflict 与 dispatch unknown 只影响一份 request；
- server request 不存在 durable store、root fence 或 global unavailability 路径。
