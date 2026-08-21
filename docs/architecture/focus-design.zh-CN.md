# FOCUS 技术设计

文档角色：中文规范源。英文同步副本：`docs/architecture/focus-design.md`。

本文只描述当前架构地图：层、owner、事实源与依赖方向。具体产品行为属于
`docs/contracts/`；历史取舍属于 `docs/decisions/`；阶段进度属于 `docs/_work/`，不能反向成为
运行时合同。

另见：

- [main turn owner 合同](../contracts/root-operation-owner.zh-CN.md)
- [`thread/create` 本地提交合同](../contracts/thread-create-local-commit.zh-CN.md)
- [`thread/resume` 本地提交合同](../contracts/thread-resume-local-commit.zh-CN.md)
- [server request 生命周期合同](../contracts/server-request-lifecycle.zh-CN.md)
- [Feishu thread 生命周期合同](../contracts/feishu-thread-lifecycle.zh-CN.md)
- [`focus` / `fcodex` shared backend 运行时](./focus-shared-backend-runtime.zh-CN.md)
- [活跃架构债务台帐](./architecture-debt-register.zh-CN.md)

## 1. 设计基线

FOCUS 是 Codex 的多前端集成层。Codex app-server 持有 thread、turn、item、goal、pending
server request 与 effective runtime 事实；FOCUS 持有 Feishu、Web、wrapper 和本机服务所需的
集成状态。

默认规则是：

- 先对标上游 Codex 的实际行为，只为 Focus 的真实多前端竞争增加最小规则；
- 一份 mutable fact 只能有一个 owner，coordinator 只编排，不复制事实；
- authority、read model、projection、delivery 与 cleanup 必须分开；
- unknown 只限制无法安全重复的 exact request/effect，不能扩散成 thread、surface 或 service
  的笼统不可用；
- fail-closed 必须有明确安全对象和证据，不能用来补造上游没有的 tree、incarnation、cursor 或
  exactly-once 能力；
- Codex persisted/effective facts 不从本地 intent、缓存或 UI 状态反推。

FOCUS 相比单一上游 TUI 只增加一条通用的多前端 turn 规则：同一 root thread 中仍声明
Feishu next-turn/FIFO 或 exclusive/autonomous 语义的 submission/activity 至多有一个 holder。
普通 Web/`fcodex` input 是 upstream-routed contributor，不取得 writer。matching
`turn/completed` 立即释放 exact active lease；child、interaction、卡片和投递都不延长它。

## 2. 分层与依赖方向

```text
Feishu / Focus Web / focusctl / focus-fcodex
                    |
                    v
surface ingress and presentation
                    |
                    v
application transaction owners
                    |
          +---------+---------+
          v                   v
 process/durable state     Codex adapter
 owners and stores             |
                               v
                         codex app-server
```

### 2.1 Surface 与传输

- Feishu adapter 负责事件、消息、卡片和 outbound effect 分类。
- Focus Web Gateway 负责 loopback HTTP/WebSocket、browser session 与 DTO 传输。
- `focusctl` 是本地管理面；`focus` / `fcodex` wrapper 经本地代理连接所选实例 backend。

Surface 只做鉴权、输入翻译和结果投影。它不能直接修改另一个 owner 的状态，也不能从“连接仍在”、
“卡片仍在”或“用户可信”推导 writer authority。

在 Feishu surface 内，`FeishuProcessCache` 是 transient 进程内 message dedup、message context、chat metadata、
reserved card、sender name 与 warning throttle 事实的唯一 owner。`FeishuMessageCodec` 持有 message/card schema
解码、mention 归一化与 terminal-card text projection；它只经具名 port 获取原始卡片内容与 sender name，绝不持有
Lark SDK client。`FeishuIngressController` 统一持有 group activation/admin/trigger policy、local history boundary、
forward aggregation、history recovery，以及完整的 inbound dispatch 与 destination-loss cleanup 顺序。`FeishuBot`
只把 SDK callback 翻译成中立的 `FeishuInboundMessage`，保留 SDK chat/message/raw-card/sender/outbound effect，并暴露
所需 surface façade；它不再镜像 ingress、store、recovery 或 aggregation 事实。

### 2.2 应用事务 owner

具名 service/coordinator 持有跨 owner 的固定调用顺序，例如 thread create/resume、Feishu binding
transition、Web open/turn/mutation、backend reset、server-request projection 与 execution-page
rollover。事务 owner 可以消费 typed receipt，但不得保存参与方事实的镜像。

`RuntimeAdminControlRouter` 是本地 control-plane method 与 wire parameter catalog 的唯一位置。它只归一化一笔
request，并经具名 service、binding、thread port 分派；domain fact 与事务仍留在这些 port 后的 owner。

`RuntimeAdminBindingApplication` 统一持有与 surface 无关的 Runtime Admin binding 用例：inventory/read model、
prompt admission、attach/detach、clear 与 stale cleanup。它只协调既有 binding fact、clear transaction 和 thread
lifecycle owner，不镜像其状态；`RuntimeAdminController` 只保留飞书命令/卡片展示与公开 binding façade 委托。

`RuntimeAdminOfflineLifecycle` 统一持有与 surface 无关的 `focusctl` 离线 lifecycle 事务：archive target 解析、
本地 binding preflight、lifecycle result 校验、archive/unarchive/delete mutation，以及跨实例 binding settlement。
它只使用具名基础设施 port 并返回不可变 receipt，不保存 runtime fact，也不读取终端输入或展示输出。
`bot/runtime_admin/cli_inputs.py` 持有 argparse grammar，以及 argv、prompt 文件和 `CODEX_THREAD_ID` 输入的归一化。
CLI 继续持有 delete 确认、dispatch、结果渲染、batch 展示、exit-code policy 与 effect 调用。这些边界把输入 admission 和
filesystem/control-plane mutation 顺序移出 presentation surface，同时不引入新的 durable 状态机。

`bot/focus_runtime/thread_targets.py` 中的 `CodexThreadTargetService` 是无状态的应用边界，集中执行权威 thread target
读取、direct-root 校验与 target 选择；它还协调既有 Web resume target 路径，并集中既有错误分类与 exact interrupt
转发。它不持有 thread、binding 或 runtime fact；Codex app-server 与参与事务的 runtime owner 仍是 authority。

`WebThreadOpenCoordinator` 持有 Web thread directory、open 与 bounded-history 的固定 staged 顺序：
RuntimeLoop prepare/settle/final check 只读写 owner fact，app-server/store I/O 与 detached DTO materialization
在 external transaction worker 上执行。`WebThreadInspectionService` 对 tool detail 与 conversation search 持有
同样的 prepare/effect/settle 边界。`bot/web_runtime/thread_read_projection.py` 只拥有从冻结 typed inputs 构造
list/open/history DTO 的纯投影，不持有 runtime fact，也不决定 response 最终是否仍可安装；该 authority 仍归
coordinator 的 exact final check。`WebDirectThreadTargetCoordinator` 集中已权威证明的 direct-target snapshot 与
`ThreadSpawn` 拒绝后的 Web-local convergence；只有 current document/backend settlement 可调用该 cleanup。

`bot/focus_runtime/binding_coordinator.py` 中的 `BindingRuntimeCoordinator` 是无状态的跨 owner 边界，集中 17 个
需要保留在 binding/runtime coordination 边界的事务。事务同时涉及本地状态与 effect 时，它保持既有 shared-lock 边界：
需要 binding lock 的读取、复核与提交仍在锁内，timer cancellation、unsubscribe 或 runtime-lease release 则在离开
临界区后执行。Web 只经具名、typed 的 `Callable[[str], bool]` runtime-interest port 参与；coordinator 既不导入
`WebRuntimeController`，也不保存 Web
状态。`BindingRuntimeManager` 继续持有 binding/session 事实，`FeishuBindingTransitionOwner` 与
`RuntimeBindingBatchDeactivationOwner` 继续持有各自 commit，`InteractionLeaseStore` 继续持有 main-turn lease，既有
thread 与 service-runtime authority 继续持有各自较窄的 authority。coordinator 只保存注入的 capability，不新增任何
mutable fact。

`FocusRuntime` 不再暴露原先 29 个 private binding helper：其中 17 项成为 coordinator operation，其余 12 个薄转发
由 consumer 直接调用既有 owner。frontend timer cancellation 属于 coordinator；presentation factory 不属于
该边界，现由下述 `FeishuPlatform` capability 持有。

`bot/focus_runtime/feishu_platform.py` 中的 `FeishuPlatform` 是 runtime 所连接 Feishu bot reference 的唯一
owner，并集中消费该 reference 的平台特定 chat、actor、reply 与 card-publisher routing。它只保存这一份 attached
bot fact，不持有 inbound route catalog 或持久化 presentation fact。

`bot/focus_runtime/feishu_surface.py` 中的 `FeishuSurface` 是飞书 ingress、group policy、command 与 card action
的应用边界，也是 runtime inbound route catalog 的唯一安装位置。它只组合既有 domain/capability owner；queue admission
与 drain 仍进入 `FeishuExecutionQueueService`，相关 mutable fact 仍留在这些既有 owner。

`bot/focus_runtime/terminal_results.py` 中的 `TerminalResults` 经 `FeishuPlatform`、`TerminalResultStore` 与 typed
binding/publication port 协调五项 terminal-result lookup、record、resolution、duplicate check 与 publication operation。
`TerminalResultStore` 仍是持久化 terminal-result fact owner；该边界既不镜像 store record，也不持有 execution-output
state。

### 2.3 协议边界

`bot/adapters/` 与 `bot/codex_protocol/` 隔离 app-server wire、连接 generation、RPC outcome 和
schema drift。业务层只消费 typed response/notification，不依赖 Codex 私有磁盘布局。

`CodexRpcConnection` 独占 websocket、identity lock、handshake state/generation、pending response map、
reader/callback producer 与 inbound JSON-RPC dispatch。`CodexRpcClient` 只是持有一份 connection capability 的
typed façade，不保存 mutable transport fact。

`ManagedAppServerProcess` 独占本地 guardian process generation、选定的 listen endpoint、startup lock、
cleanup token、stream thread 与 runtime publication。`CodexRpcConnection` 只通过这一份 capability 协调
backend 连接与停止，不复制 process handle 或 cleanup state。

`CodexRpcStopBarrier` 独占 stop-request fence、单次 single-flight drain attempt、从 live connection 原子转移
出的 exact websocket/producer/process capability，以及失败后可重试的 cleanup outcome。它只为完成原子转移而
共享 connection identity lock，不镜像 connection generation、handshake 或 pending RPC fact。

### 2.4 状态 owner

进程内状态通常由 `RuntimeLoop` 串行化；跨进程协调或产品要求跨重启保留的事实才进入
`bot/stores/`。文件被持久化不自动使它成为业务 authority；discovery、projection 与 delivery
ledger 仍保持各自较窄的角色。

一个显式 staged external transaction 在 external caller thread 上持有同一
`ServiceRuntimeLifecycle` ingress receipt。对这笔 transaction，RuntimeLoop 只串行化 mutable fact
的短 prepare/settle transition：先开出不可变、绑定 exact target 与 generation 的 receipt，随后在
loop 外执行可能阻塞的 I/O，最后只凭原 receipt 回到 RuntimeLoop 结算。迟到、replacement 或已退休
receipt 的结果没有新 effect；这项 process-local staging 能力本身不迁移任何 caller，不建立
durable operation ledger，也不自动 replay outcome unknown。

Web staged read 在 authoritative settlement 后还可把 CPU/attachment-heavy DTO projection 留在 loop 外；返回前
再用原 document、connection generation、projection revision 与 read observation 做一次 O(1) final check。
这一步只决定 DTO 是否可交付，不能回滚更早已经提交的 resume/runtime interest。Gateway 在 per-client lock
内 prepare 后释放 lock，但 prepared service-ingress receipt 继续覆盖 external worker 与 final settlement。
Gateway handoff cancellation 或 executor failure 只 abandon 尚未 claim 的 receipt；claim 后由原 transaction
完成 settlement，shutdown 等待它退出。

需要 turn/task 与附件物化的 Web live notification 也使用该边界。`WebRuntimeEventCoordinator` 在
RuntimeLoop 内只应用 notification/cache mutation，并冻结带 exact read observation 与 runtime epoch 的
不可变 receipt；`bot/web_runtime/notification_projection.py` 在 service-ingress background worker 上执行
turn/task DTO 投影、图片 hash/copy 与附件 URL 物化。每个 thread 至多一笔在途 projection 和一个
latest-wins successor；settlement 只有在原 observation/epoch 仍精确匹配时才发布，旧结果直接丢弃。
successor 不复用 predecessor 的 ingress receipt，而在前一笔 settlement 时独立重新准入；service 已进入
STOPPING 时该准入失败、flight 退休并只发布轻量 `thread_invalidated`，因此 shutdown 后到达的 presentation
工作不能无限延长旧 barrier。worker 调度或无 successor 的投影失败同样只发布轻量 invalidation，不阻塞 notification；权威
`thread/deleted` 或 known Web delete success 的 attachment-scope 物理清理也在同一 shutdown barrier 下执行。这些 flight 不持久化，
不 replay，也不取得 thread lifecycle authority。

Web runtime cleanup 虽由 RuntimeLoop transition 发现，也使用同一 staged external-transaction 边界。
`WebRuntimeLifecycleCoordinator` 对每个 thread 只持有一笔 cleanup flight，并把期间新到候选合并为至多一个
successor。RuntimeLoop 冻结并重验 exact local fact；external worker 探测 backend/holder、至多发送一次已
claim 的 canonical unsubscribe，并只对 probe 捕获的完整 service-holder record 做 CAS。coordinator 在
unsubscribe send 前与 holder release 前分别重验，最后也只在原 generation/facts 下清理 local interest/cache。
新 Web desire、canonical 飞书 subscriber、pending interaction、backend generation 或 holder successor 会在
对应阶段保留 runtime；mismatch 绝不 replay unsubscribe。

## 3. 运行时拓扑

- 所有本机实例共享 `CODEX_HOME` 与 persisted thread namespace。
- 每个 Focus instance 独立持有配置、数据、service lease、control plane、Web Gateway，以及一条
  `service -> guardian -> codex app-server` 的同机 backend 生命周期。
- 机器级 instance registry 与 thread runtime lease 协调多个 Focus instance；它们不授予实例内
  main-turn writer。
- 浏览器只连接 Focus Gateway，不直连 app-server。
- `focus` / `fcodex` 选择一个运行实例，经 per-launch 本地代理把上游 TUI 接到该实例发布的 backend。
- Focus 不支持外部 app-server deployment；内部 attached client 不取得 backend lifecycle authority。

完整拓扑、wrapper、cwd proxy、credential 与平台 containment 边界见
[shared backend 运行时](./focus-shared-backend-runtime.zh-CN.md)。

## 4. 当前事实源与 owner

| 问题 | 唯一事实源 / owner | 明确不是 authority 的事实 |
| --- | --- | --- |
| thread、turn、item、goal、title、cwd、status 与 effective runtime 是什么？ | Codex app-server；经 adapter 的 typed read/notification 投影 | 本地 requested settings、缓存、卡片、browser snapshot |
| service 是否允许新 ingress、何时可释放资源？ | `ServiceRuntimeLifecycle` 及其 exact external-ingress receipt | Handler、Gateway 或 adapter 自己的局部 ready flag |
| 一代本地 app-server process 及其 durable runtime publication 由谁持有？ | `ManagedAppServerProcess` | `CodexRpcConnection` 的 websocket、pending RPC 或 stop-barrier state |
| live websocket generation、handshake、pending response 与 reader producer 由谁持有？ | `CodexRpcConnection` | `CodexRpcClient` façade、adapter read model 或 surface callback |
| 一次已请求或尚未完成的 Codex RPC shutdown 由谁持有？ | `CodexRpcStopBarrier` 及其 exact transferred resource capability | live connection 字段、卡片、service phase 或复制的 process handle |
| 当前 websocket/backend generation 是否可发送普通 RPC？ | `AdapterIngressGate` 的 outbound permit、actual-send guard 与 response confirmation，加 adapter transport generation authority | callback 到达顺序、缓存 endpoint |
| 哪个实例可 materialize 一个 live thread？ | machine instance registry、global loaded gate 与 `ThreadRuntimeLeaseStore` | 实例内 main-turn lease、thread list 缓存 |
| 谁拥有 lease-bearing Feishu/exclusive/autonomous main-turn holder？ | `InteractionLeaseStore` | 普通 Web/`fcodex` input、child、socket/document liveness、delivery、goal、runtime lease |
| 谁可对 exact current turn 执行 steer、interrupt 或 interactive server-request response？ | app-server exact turn/request fact 加各 surface/domain owner；Web prompt 由 `WebPromptSubmissionCoordinator` 冻结 exact turn/backend generation，并由 `WebPromptResultRegistry` 在有界 receipt 留存期内限定同一 mutation 的唯一 effect slot | main-turn writer relation本身、presentation、缓存或通用本机身份 |
| 一次 create/resume 的本地后果如何提交？ | `ThreadCreateTransaction` / `ThreadRuntimeAuthority` 的即时进程内 receipt | durable journal、跨 thread quarantine、自动 replay |
| app-server callback 是否 pending？ | Codex app-server；`ServerRequestRegistry` 只投影当前 connection epoch | 卡片、浏览器 action lock、main-turn lease |
| Feishu chat 默认绑定哪个 thread？ | `ChatBindingStore`；resident transition 由 `BindingRuntimeManager`、`BindingOwnerAuthority` 与 typed commands 持有 | execution card、subscriber 或 sender cache |
| 同一 Feishu binding 的输入顺序是什么？ | `FeishuExecutionQueueController` | main-turn writer 或 backend residency |
| execution 内容与页面范围是什么？ | `ExecutionTranscript` 与 `ExecutionPageLedger` 各持自己的展示事实 | turn completion、FIFO 或 thread lifecycle authority |
| Feishu destination 是否已可靠丢失？ | `FeishuDestinationLossStore` 与 `FeishuDestinationLivenessCoordinator` | 一般 timeout、未知发送错误、main-turn 状态 |
| Web 的 durable navigation workspace/selection 与 attachment 是什么？ | `WebWriterProfileStore` 与 `WebAttachmentStore`；后者另持有在途 submission 的 process-local exact file pin | browser component、read model、event socket、next-turn settings |
| instance-wide Web next-turn settings 是什么？ | `WebNextTurnSettingsStore`；mutation/projection transaction 由 `WebNextTurnSettingsCoordinator` 收口 | `WebWriterProfileStore`、selection/navigation generation、main-turn lease、thread effective settings、browser overlay/event |
| Web document、runtime interest、read model 与 prompt result evidence 是什么？ | `WebDocumentRegistry`、`WebRuntimeInterestRegistry`、`WebThreadReadModel`、`WebPromptResultRegistry` 各自独占 | 彼此之间的 projection；任何一项都不是 durable writer 或 replay authority |
| canonical Web-triggered subscription/runtime cleanup 由谁持有？ | `WebRuntimeLifecycleCoordinator` 持有 per-thread cleanup flight 与 exact local recheck；`ThreadRuntimeAuthority` 持有 same-thread resume/start/unsubscribe effect fence；thread-runtime lease owner 独占 full-record holder CAS | `fcodex` connection、read cache 或 background worker thread |
| Web thread list/open/history DTO 由谁构造并决定仍可交付？ | `thread_read_projection.py` 从冻结 inputs 做无状态投影；`WebThreadOpenCoordinator` 凭 exact document/generation/revision/observation 做 settlement 与 final admission | external worker、browser component、已经 stale 的 DTO |
| fcodex endpoint/request/interaction 当前是否有效？ | `FcodexParticipantRuntimeRegistry`、`FcodexOperationService`、`FcodexInteractionInbox` 各自持有独立进程内事实 | participant 的 `connected/grace/orphaned` endpoint 状态不是 main-turn writer |
| component config seed 来自哪里？ | `system_config.py`、`codex_config.py`；何时只作 seed、何时由 durable setting owner 优先，以 runtime-settings 合同为准 | UI 回显、settings ACK 或每 read/turn 重读配置 |

这张表只回答 ownership。状态转换、失败分类和用户可见结果必须查看对应合同，不能在架构文档里再建一份
平行状态机。

## 5. 关键生命周期边界

### 5.1 Service 与 backend

`ServiceRuntimeLifecycle` 在取得 service lease 后才激活 RuntimeLoop、adapter、control plane、Gateway
与 worker。停止时先关闭新 ingress，等待已准入 callback，停止 producer，drain RuntimeLoop，最后停止
adapter 并释放 authority。组件仍保留自己的更窄 transport barrier，但不复制顶层 phase。

经 external ingress 准入且会跨越 loop 外 I/O 的 transaction 从开始到最终 settle 一直占有同一个
external-ingress receipt；因此
shutdown 可以先关闭新准入并等待这些 transaction，而 RuntimeLoop 在 effect pending 时仍可处理 notification、
interrupt 与短状态 transition。receipt 只证明本进程已准入并尚未退出，不证明上游 effect 成功；effect 的
pre-send、known response 与 outcome-unknown 分类仍由对应 adapter/domain 合同决定。

Gateway 若把 prepare 与 execute 分隔在 per-client lifecycle lock 两侧，仍必须保留这份 receipt。request 在
worker claim 前取消或 executor 无法准入时，Gateway exact-abandon 它以免 shutdown 永久等待；claim 后的取消
只终止 HTTP waiter，不能取消已经运行的 worker 或重复 settlement，service 继续等待原 transaction 退出。

经 `start_background_external_transaction` 启动、由 RuntimeLoop 发现的 Web runtime cleanup 仍是一笔 external
transaction：daemon thread 启动前就取得 service-ingress receipt，并以该 receipt 作为 shutdown barrier；它不属于
下述长期 internal scheduled worker，也不需要第二份 worker registry。internal scheduled worker 不取得
external-ingress receipt；其唯一 worker registry 仍负责在 RuntimeLoop drain 前停止 producer 并 join worker。

`ServiceRuntimeLifecycle` 仍是 service phase 以及上述 startup、rollback、shutdown 顺序的唯一 owner。
`ServiceRuntimeAuthority` 是更低层的 coordination capability：它只在 lifecycle 定义的边界调用既有
machine instance registry、global loaded gate 与 service/thread-runtime lease owner；它不持有或复制这些
事实，也不依赖 presentation 或 `FocusRuntime` composition root。

Backend reset 的最小顺序是：fence 普通 ingress，只读捕获当前进程的 exact main-turn leases，等待旧 owned child
完成 OS exit/wait，以 full-record CAS 集中退休 capture，依次退休 registry/fcodex/Web fact、执行 binding detach 与
execution interrupt/finalize，再退休 Feishu root/request fact。每个既有 owner 的调用均为幂等，mutable fact 仍归原
owner。Focus 随后清理本实例 runtime holder、启动并验证 replacement，最后发布并重新准入。stop 未确认时不退休任何旧 backend fact；
任一后续 authoritative retirement 或 structural projection 失败时，已完成的前序退休保持
有效但 replacement 不启动，重试从头幂等执行且 ingress 继续 fenced。其他 PID 与 capture 后的 successor lease
不受影响。它不迁移旧 callback、writer 或 unknown evidence。

### 5.2 Main turn

只有 lease-bearing Feishu/exclusive/autonomous effect 在发送前取得空白 submission lease。普通 Web/`fcodex`
`turn/start` 是 upstream-routed input，不读写 lease。validated `turn/start` success 原样保留 authoritative
upstream `turn.id`，但 response 本身不激活或转移 Focus lease，也不建立跨 notification/reset 的
lifecycle 或 completion authority；matching `turn/started` 仍负责绑定 actual `turn_id`，matching completion
只释放 exact active lease。若 started 遗漏，completion 不能仅凭 start response 绑定 blank。飞书可以在
权威重读 root 已 inactive 后结算 exact 的普通 prompt blank，但不归因 completion；其进程内
admission token 是事务 receipt，不是第二份 writer fact。普通 fcodex start 与已有 fcodex exclusive/autonomous
blank 并发时，后续 lifecycle 仍可能激活该 blank；这是接受的极窄竞态，不增加关联状态机。inline review
response identity 与 compact 空 response 保持 method-specific 路径；细节只由 main-turn owner 合同定义。
live 且已 attach exact direct root 的 `fcodex` endpoint，或已连接并 materialize 该 root 的 Web document，
都可以按 canonical effect-specific 边界 steer 或 interrupt exact current/startup turn，且不会取得或改变 writer。
Web existing-thread prompt 只有一个 POST。`WebPromptSubmissionCoordinator` 在 RuntimeLoop 内冻结
exact document/target/backend generation 与当时 active turn；若存在 A，这个 attempt 只能向 A 发送一次
`turn/steer`，不存在 exact id 时才只发送一次 `turn/start`。successor B mismatch 或 no-active
是 `known_no_effect`，不 retarget B，也不 fallback start。`WebPromptResultRegistry` 只保存有界、进程内
`pending / succeeded / known_no_effect / outcome_unknown` receipt，不保存 payload，不写盘，不 replay，
也不阻塞同 thread 的新 mutation、passive read、shared server-request response 或 exact interrupt。F5 只能用
`(thread_id, mutation_id)` 读取这份 bounded result receipt；只在 receipt 仍留存时，重复 POST 才不能取得第二个
effect slot。terminal eviction、confirmed backend retirement 或 service restart 会删除 seen-identity evidence；此后
同 UUID 可能取得新 slot，但官方 browser 只 GET 或为新 gesture 生成新 UUID。上述 miss 不证明 effect 未发生，
也不开放重放。详见 [Focus Web prompt mutation 恢复合同](../contracts/focus-web-prompt-mutation-recovery.zh-CN.md)。
前端断线不会把 writer 变成 durable grace/orphan state，service restart 也不会
恢复旧 writer。详情以 [main turn owner 合同](../contracts/root-operation-owner.zh-CN.md) 为准。

### 5.3 Create、resume 与 server request

Create/resume 只在一次调用与紧随其后的本地 commit 之间使用进程内 receipt。unknown 不自动 retry，也不建立
持久恢复状态机。Server request 的 callback lifetime 归上游；Focus 只用当前 generation identity 与一次性
surface token 防止 stale action。

Web cold open 的 outer read/projection 可以在这笔 resume local commit 之后继续。known resume 必须先提交
runtime interest；后续 document reissue、notification、projection revision 或 backend replacement 只会使
read DTO stale，不能 compensation、回滚或把已确认 resume 重分类为 unknown。详见
[`thread/resume` 本地提交合同](../contracts/thread-resume-local-commit.zh-CN.md)。

### 5.4 Presentation 与 delivery

Feishu execution page、terminal result、generated image、Web projection 和 destination-liveness 都是独立
产品能力。它们可以异步恢复自己的 exact effect，但不能阻止 matching main-turn completion、延长 writer，
或把局部投递 unknown 扩散到后继 turn。

## 6. 仓库职责地图

- `bot/`：surface、application owner、runtime owner、adapter、CLI 与 composition root。目前物理目录仍较平，
  不能只从路径推断每个 owner；dependency-direction guard 已强制稳定边界：store 与 Codex protocol/adapter package
  不得反向依赖 surface 或 composition，Web、Feishu、fcodex 不得互相导入 presentation domain，surface-neutral
  Runtime Admin transaction 不得导入 surface。
- `bot/stores/`：durable authority、intent、coordination、delivery ledger 与可重建 discovery store。
- `bot/runtime_admin/`：集中 surface-neutral binding/control/offline-lifecycle transaction，以及明确分开的 CLI
  和 Feishu presentation module；方向门禁禁止 neutral module 反向导入任何 surface。
- `bot/focus_runtime/thread_targets.py`：`CodexThreadTargetService` 边界，集中权威 thread target 读取、校验与选择、
  Web resume target 协调、错误分类和 interrupt 转发；它不保存 mutable authority。
- `bot/focus_runtime/binding_coordinator.py`：`BindingRuntimeCoordinator` 边界，集中 17 个跨 owner binding/runtime
  事务，经既有 owner capability 保持 shared-lock 临界区与 commit/effect 顺序，且不保存 mutable authority。
- `bot/focus_runtime/feishu_platform.py`：`FeishuPlatform` owner，唯一持有 attached Feishu bot fact，并集中平台特定
  chat/actor/presentation routing；不保存 route、persistence 或 domain fact。
- `bot/focus_runtime/feishu_surface.py`：`FeishuSurface` 边界，经既有 capability owner 承接飞书
  message/recall/card/attachment ingress、group check、command/action route 与 prompt/FIFO entry。
- `bot/focus_runtime/terminal_results.py`：`TerminalResults` 边界，集中 terminal-result lookup、persistence coordination
  与 publication；既有 `TerminalResultStore` 仍是持久化 fact owner。
- `bot/web_runtime/thread_open_coordinator.py`：Web list/open/history staged transaction owner；
  `bot/web_runtime/thread_read_projection.py`：冻结 inputs 到 list/open/history DTO 的无状态 external projection；
  `bot/web_runtime/direct_thread_target_coordinator.py`：direct-target proof 与 exact invalid-target convergence；
  `bot/web_runtime/gateway_external_transaction.py`：aiohttp cancellation 到 lifecycle-owned prepared receipt 的薄 handoff。
- `bot/fcodex/control_dispatcher.py`：service侧`operation/*`协议边界；它在调用者的`RuntimeLoop` turn内运行，
  权威读取direct target，并把mutable fact交给既有operation owner。
- `bot/feishu_continuation_controller.py`：串行化的飞书continuation边界；它依次编排direct-root读取、
  root-operation准入、显式thread resume、Runtime Admin attach、goal mutation/resume/compensation、
  settings fence、settlement与history/card projection，所有mutable fact仍由既有runtime owner持有。
- `web/src/focus/`：Focus-owned transport、projection、navigation/profile 与 mutation/action owner；通用 UI
  component 只消费 view projection。
- `docs/contracts/`：当前行为语义的正式来源。
- `docs/architecture/`：当前 owner、层次、依赖与活跃债务。
- `docs/decisions/`：设计理由；被取代的决策必须显著标记，不能冒充当前合同。
- `docs/_work/`：临时计划与证据台帐，不是长期事实源。
- `tests/`：按 owner/合同验证；只有小型 composition suite 可以依赖装配细节。

`FocusRuntime` 是位于 `bot/focus_runtime/runtime.py` 的当前 composition root。Root 不应成为新的
domain-fact owner。
`bot/focus_runtime/__init__.py` package root 保持为空且不 re-export runtime；该 package 下的 capability owner 不得
反向 import composition root。新增行为应进入现有 owner，或先明确新事实为何不能由现有边界承担。

## 7. 演进规则

- 每次诊断、review、实现与重构都遵循
  [仓库导航与变更锥纪律](./development-navigation.zh-CN.md)。
- 修改 Codex 行为前先审阅固定上游代码或官方协议，再决定 Focus 是否真的需要偏离。
- 新 coordinator 必须说明它编排哪些 owner，以及为什么不保存第二份事实。
- 新持久化记录必须说明跨重启需求、写 authority、损坏策略与删除条件。
- 任何 stronger-than-upstream 规则都必须给出真实多前端场景、最小增量、可观测证据和用户成本。
- 当前未结 owner、aggregate、schema、测试与 package 问题只在
  [活跃架构债务台帐](./architecture-debt-register.zh-CN.md) 维护。
