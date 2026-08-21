# Runtime 控制面合同

文档角色：中文规范源。英文同步副本：`docs/contracts/runtime-control-surface.md`。

本文定义飞书侧控制面的正式语义及其与 Web settings 的边界，刻意不定义一套跨前端共享的 settings 系统。

## 1. 飞书与 Web 的可写设置族保持分离

### 1.1 binding-wise next-turn settings

入口：

- `/model`
- `/effort`
- `/approval`
- `/permissions`

语义：

- 管理当前 Feishu binding 后续 turn 的 override
- 主要在 `turn/start` 被消费
- 在恢复未 loaded thread 时，cold `thread/resume` 也可能为恢复后的第一轮
  autonomous turn 携带其中一小段 one-shot override
- 不写任何项目自管的 thread-level persisted state

preference store 不是跨前端写入旁路。Feishu binding-wise setting 要通过可能 continuation 的
`thread/resume` 或 Feishu `turn/start` 被应用前，该路径必须取得 `root-operation-owner` 定义的 exact blank
submission lease。被拒绝时不得把它变成隐式 resume、排队的 setting application 或 takeover。普通
Web/`fcodex` input 不消费这份 binding setting，保持各自 upstream-routed settings 语义且不因此取得 lease。

上文 cold-resume 的措辞不把携带 setting 的 `thread/resume` 变成 observer-safe 投递路径。persisted active goal
可在 resume 后继续执行；空、无法读取、未来或未识别 goal status 也必须 fail-close。这种可能 continuation 的
resume 必须先取得 `root-operation-owner.zh-CN.md` 定义的 exact blank lease；只有权威预检已证明 goal
处于已审阅不可继续状态、不存在或 Goals 已禁用，才可走不取得 main-turn lease 的被动 subscription 路径。
exact active Focus turn 旁的已准入 native fcodex attach 是唯一窄例外：它保持 writer 不变，并遵循 upstream
goal-continuation 语义。其 TUI-owned start/resume settings 语义原样转发，但 request 与 response 都不成为
Focus effective-settings 事实；already-loaded thread 是否接受 override 仍归 upstream。见 canonical
[fcodex operation owner](./fcodex-operation-owner.zh-CN.md#直接-thread-target)。

### 1.2 Web 有独立的 instance-wide next-turn settings

Focus Web 的 model、reasoning effort、approval policy 与 permissions profile 由一份 durable
`WebNextTurnSettings` 持有。同一 instance 的所有 browser、F5 后 document 与 thread 共享它；它不属于
`client_id`、selected thread 或 main-turn writer，也不会自动与 Feishu binding 或本地 `focus` / `fcodex`
TUI 状态合并。

完整的 seed、持久化、merge/generation、consumer 与 bounded backend fallback 只由
[`runtime-settings-fact-sources.zh-CN.md`](./runtime-settings-fact-sources.zh-CN.md) 定义。本控制面只追加 UI 边界：active
turn 期间设置控件仍可修改，并明确作用于下一次 eligible Web turn；main-turn admission 只决定何时可消费 snapshot，
不授予或阻止 instance-wide settings mutation。

与设置独立的 Web navigation profile 中，`selected_thread_id` 是唯一的语义 selection 事实；meta、`/cd` 的 previous scope、
attachment 准入与 scope generation 都只以它为准。`WebDocumentRegistry.materialized_thread_id` 只证明
当前进程已成功安装某次 selection；older-history 准入因此要求这两项事实都等于请求 target，materialized
值不能授权 durable profile 并未选择的 target。desired-client edge 与 subscription outcome 只归
`WebRuntimeInterestRegistry` 所有，两个 selection 事实都不能替代它。

当上游权威事实使已选 target 变得不可用时，Focus 只 compare-and-clear 与它 exact match 的 durable
selection。该 store transaction 会原子地把 selection 改为 draft 并令 `scope_generation` 加一；重复处理
同一事件是 no-op。进程内清理只在 materialized 值仍等于旧 target 时遗忘它，保留已经替换的
materialization，并清除每个 durable selection 被清 document 的全部 desired runtime edge。这里绝不自动
rebind pending attachments：archive、not-found、loaded-elsewhere 会把旧 thread-scope records 保留为
isolated；确认 delete 与非法 direct `ThreadSpawn` target 则删除该 thread scope。只有 durable commit
之后，Focus 才发布不携带 profile 副本的 `profile_changed` invalidation；每个浏览器必须重读自己的 meta，
取得新 navigation generation 后才能继续 scope-bound 写入。该 generation 与 settings generation 互不排序或结算。

## 2. 已移除的飞书设置面

下列入口已不再属于本项目的正式合同：

- 历史上的项目自管 profile 命令
- `/memory`
- 任何 thread-wise memory 控制面

如果操作者想修改 process-level 的上游能力，例如 profile/provider 或
memory 行为，应直接通过上游 Codex 处理，而不是走项目自管的飞书设置面。

## 3. 其他核心状态轴

独立于 settings 之外，控制面仍严格区分三条状态轴：

1. `binding`
   - 当前 chat 逻辑上指向哪个 thread
2. `attach / detach`
   - 当前 chat 是否接收该 thread 的飞书推送
3. `backend / live runtime`
   - 该 thread 当前是否 loaded，以及谁拥有 live runtime

这些轴与 settings 正交，不得混淆。

## 4. 飞书 turn-time settings 的正式语义

`/model`、`/effort`、`/approval`、`/permissions`：

- 都属于当前 binding 的 next-turn settings
- 默认回读当前 binding 的持久化配置事实
- 不是 instance baseline
- 不是 thread-level persisted truth

在这个设置族内：

- 对 `/model`、`/effort`，`auto` 表示“不显式 override”
- 它不再映射到任何项目自管 thread-level fallback state
- Focus 把 model 与 effort 作为一个受约束的组合：
  - `validated`：effort 为 `auto`，或显式 model 的 metadata 明确支持该 effort
  - `deferred`：model 为 `auto`，或显式 model 没有可用 metadata；Focus 原样传递显式 effort，由 app-server 决定
  - `rejected`：显式 model 的 metadata 明确存在，但没有声明支持该显式 effort
- `/model`、`/effort` 与卡片动作会拒绝新建 `rejected` 组合；既有 binding 值不迁移，turn dispatch 也不改写旧值
- 已知 canonical effort 输入会规范化为小写；未知/custom effort 只去除首尾空白并保留大小写
- `ultra` 原样发送给 Codex，不转换为 `max`，也不构造 `collaborationMode`
- 显式 model / effort 随 turn 进入共享 upstream thread 后，可能影响该 thread 当前及后续 turn；本地 `focus` / `fcodex` 可以观察或覆盖这些上游状态
- `auto` 只表示 Focus 省略对应字段，不表示恢复 `.codex/config.toml`、model default 或其他 frontend 的旧值
- model / effort 是可选 override：Focus 每轮只重新应用非 `auto` 字段；`auto` 会继续沿用共享 upstream thread 的当前状态
- 对 `/approval`、`/permissions`，binding 持久化值是安全基线；新 binding
  从实例配置 seed 出初始值，一旦落盘就不随实例默认漂移
- approval / permissions 不提供 `auto`：Focus 发起每个 turn 时都会显式重新应用该 binding 的安全基线；其他 frontend 可以改写 upstream thread，但下一次 Feishu turn 会再次应用本 binding 的值

## 5. reset-backend 的副作用边界

`reset-backend` 是恢复/管理工具，不是常规的 settings apply 路径。
典型用途是：

- 在跨实例 cold continue 之前，主动丢弃当前实例里陈旧的 loaded runtime
- 当同一 persisted thread 在项目外被修改后，例如用户用裸上游 `codex`
  改写了线程，再重建本实例 backend 对它的内存态视图

当实例执行 backend reset 时：

- 所有reset入口共用Focus自有的exact `force` selector：字段缺失表示`false`，字段存在时必须是JSON boolean；
  string、number、`null`、array与object在任何reset runtime访问或effect前被拒绝，service入口再重复exact-bool断言
- 产品级顺序只归 `BackendResetService`，更窄的 backend epoch replacement
  transaction 只归 `BackendResetCoordinator`，有界四 surface preparation 归
  `BackendResetInteractionCoordinator`；三者都不复制 binding、interaction、runtime lease 或 adapter 状态
- backend 进程会重启
- 首先同时 fence 旧 websocket generation 与普通 outbound RPC 准入；Focus 在 detach
  前保留既有的 per-binding 进程内 prompt/compact FIFO cancel，但本地 active-turn interrupt 本身必须等待 confirmed stop
- 读取进程内 server-request pending count 仅用于诊断；reset 不执行 stop 前 response/fail-close transaction
- binding inventory 或 diagnostic capture failure 会在 stop 前中止 transaction 并保持 ingress fenced；
  最终 card/result presentation 是 best-effort
- ingress fenced 后、owned backend stop 前，`InteractionLeaseStore` 先只读捕获当前 service PID 与 exact
  process identity 所有的 full `InteractionLease` generation；无法取得这份 capture 时不停止 backend
- 只有 owned child 的 OS exit/wait 已确认后，Focus 才以 full-record CAS 退休上述 capture，且保留其他 PID、
  PID 0 与 capture 后出现的 successor generation；同一阶段依次幂等退休进程内 registry、fcodex fact 与 Web active
  mutation，执行 binding detach 和 execution interrupt/finalize，最后才退休 Feishu root
  admission/continuation/candidate 与 Feishu request capability。structural projection failure 时保持 ingress fenced
  且不启动 replacement。随后再 rotate transport response authority，之后才允许启动 replacement
- stop outcome unknown 或抛错时不退休任何上述 fact；任一 authoritative retirement 无法确认时保持 ingress
  fenced、不启动 replacement、不返回 success，同一 retirement 可从头幂等重试
- connection-local effective-settings 事实、瞬时事件 projection 与 auto-resolution
  timer 会失效，旧 generation 的延迟 callback 会被拒绝
- binding 记录保留
- binding-wise next-turn settings 保留
- 绑定旧 backend generation 的 exact 进程内 request/effect record 随该 generation 一起退休；
  不存在需要带到新 generation 的 durable retained-operation、server-request settlement 或 interaction-lineage fence
- Web retirement 随旧 backend generation 退休 ordinary prompt 的有界 process-local result receipt；browser locator
  可以继续存在，但后续 GET miss 只报告结果 unavailable/unknown，不建立 durable explanation、retry、replay 或 payload
  restoration。旧 staged worker 受 exact generation fence 约束，不能作用于或结算 replacement backend
- 任一 binding/FIFO cleanup 或 backend-epoch replacement 步骤无法证明时，普通 ingress 与
  outbound RPC 必须继续关闭且禁止 replacement；只有严格更新的 generation 完成验证和发布后才能重开

结果中的 `retired_request_count` 是 machine stop 前读取的进程内 pending 数量，只是诊断 inventory，
不证明每个 request 都已回答或已在上游 resolved。

只有完整 method result 通过准入后，reset 才能被投影为成功。result 必须是只含 `force`、
`detached_binding_ids`、`interrupted_binding_ids`、`retired_request_count`、`purged_thread_ids`、
`projection_warnings` 与 `app_server_url` 的 exact object。`force` 必须是与请求一致的 exact boolean；
`retired_request_count` 必须是排除 boolean 的非负 integer；四个 list 字段必须是由非空 string 组成的 array；
backend 地址 trim 后必须非空。Focus 不额外推断 URL grammar、排序、唯一性或 list 之间的关系，也不展示
reset transaction 从未生产的 response-write count。

trusted-local Web surface 只能操作该 Gateway 的当前 instance。authenticated 且仍为 current 的 browser
document 可以读取 `GET /api/backend-reset`；同 path 的 `POST` 还必须通过 same-origin 与 CSRF，只能提交 exact
`force` 和 `expected_connection_generation`，不能提交 instance、backend 地址或 token。preview 只是 snapshot，
不预留影响范围。只有 physical websocket generation 与 adapter-ingress generation 是同一个 positive safe
integer，且 reset、cleanup 与 queued-disconnect fence 全部开放时，preview 才可执行。

Web execute 在任何 effect 前重新计算 policy，并按固定的
`CodexRpcConnection identity lock -> AdapterIngressGate lock` 顺序线性化 stale check。两个 generation 必须仍与
提交值完全一致，gate 才能 advance 或 drain；mismatch 或已经 closed/reconciling 的 gate 是 typed、可证明零 effect
的 conflict。CLI 与飞书不提交 expected generation，保留 generation 0 或 sticky cleanup 下的既有恢复能力。
Web fence 一旦开始，之后的任何异常都是 outcome unknown，不能自动 retry。只有上文七字段 result 完整 decode 后
才能向 Web 投影 success；browser 只收到 counts 与 warnings，绝不收到 `app_server_url`。

control response 若报告 `ok: true`，但携带缺字段、多字段、错型或与请求不一致的 result，reset 可能已经执行。
CLI 不输出 success projection，按 outcome unknown 处理并返回退出码 `3`；飞书 action 会把确认卡替换为不含 reset/attach
动作的警告卡。两个 surface 都不自动重试。之后只能对同一目标实例读取 status 或 preview 来检查当前状态，不能证明上一笔请求的结果。

reset-backend 不会：

- 重写 thread history
- 自动把所有 chat 重新 attach
- 保留或转移旧 approval/question 的响应路径
- 在 stop 前替用户自动提交 answer
- 为已丢弃 interaction epoch 凭空制造 `serverRequest/resolved`，或声称每个 retired request 都已在上游结算
- 把 binding settings 升格成 thread-level settings
- 充当 profile 切换入口

## 6. 飞书 `/status` 应展示什么

`/status` 与相关诊断应分别展示：

- 当前 binding 的 next-turn overrides
- attach/detach 状态
- live-runtime / loaded 状态

它们不应再展示：

- 项目自管 profile 设置
- thread-wise memory 设置
- “本项目会在下次 resume 时再注入的额外配置”
