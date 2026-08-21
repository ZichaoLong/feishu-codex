# Runtime 设置事实源与生效边界

文档角色：中文规范源。英文同步副本：`docs/contracts/runtime-settings-fact-sources.md`。

本文定义回答飞书与 Web settings 问题时的统一规则：一个设置写入之后，哪一层才是它的权威事实源？
它不把两个 frontend 的持久化事实合并，也不表示 Focus Web 不存在独立保存的 navigation/document state。

## 1. 两个互不合并的正式可写设置族

### 1.1 binding-wise next-turn settings

当前正式成员：

- model
- effort
- approval
- permissions

它们的属性：

- 作用域是当前 Feishu binding
- 持久化在 binding runtime settings 中
- 主要在 `turn/start` 被消费
- 在恢复未 loaded thread 时，cold `thread/resume` 也可能携带其中一小段
  one-shot override，避免恢复后的第一轮 autonomous turn 回退到旧的
  loaded-thread 默认值

这只说明设置可在何时携带，不把 resume 变成被动 observer 操作。若 resume 可能自主启动 main turn，
发送前必须取得 `root-operation-owner.zh-CN.md` 定义的 exact blank submission lease；只有权威预检已证明
Goals 禁用、没有 goal 或 goal 处于已审阅的不可继续状态时，才可不取得 main-turn lease 做被动 observation
resume。这里没有 retained record、delivery fence 或跨调用 writer。

### 1.2 instance-wide Web next-turn settings

Web 有一份独立的可写 `WebNextTurnSettings`，成员同样是 model、reasoning effort、approval policy 与
permissions profile。它不属于 browser `client_id`、document、selected thread 或 main-turn writer；同一 Focus
instance 的所有浏览器、F5 后的新 document 与所有 thread 共享同一份设置。它不会自动与 Feishu binding 或本地
`focus` / `fcodex` TUI 状态合并。

`WebNextTurnSettingsStore` 拥有唯一 durable record `web_next_turn_settings.json`。只有在 service start 时尚无
durable record，已校验的 `codex.yaml` 四项值才被捕获为不落盘的 startup seed；后续读取或 turn dispatch 都不重读
配置，也不建立 config mirror。第一次 Web 显式修改创建 record，之后每次重启都以 persisted record 为准。

owner lock 内的修改是 atomic partial merge。每次实际修改产生一个严格递增的 positive `generation`；不同字段可以由
并发 browser 合并，同一字段按 server commit 顺序 last-write-wins。这里没有 expected-generation CAS、冲突 UI、历史
或回滚。`generation` 只在同一 `runtime_epoch` 内可比较：同 epoch 的浏览器只安装完整且
`generation >= current` 的 snapshot：较小值忽略，较大值安装，相等值只有在完整内容一致时才是 no-op；同 generation
却内容不同必须 fail closed 并触发 authoritative refresh。epoch 变化时必须丢弃旧 generation 比较基线，经 authoritative
reload 无条件安装新 epoch 的完整 snapshot，再恢复同 epoch 比较。这样，无 durable record 的新 service start 即使仍从
generation 1 开始，也能用新的 startup seed 替换旧 runtime 的 snapshot。

下列 eligible Web consumer 各自恰好捕获一次 immutable snapshot：

- 新 thread create 与它的 first turn 共用同一份 snapshot；
- existing-thread ordinary prompt；
- 需要 writer admission 的 continuation-capable cold active-goal resume。

regular active-turn steer、observer resume、review、manual compact、F5 与 auth refresh 都不消费新设置。active turn
期间控件仍须可编辑；修改明确只影响下一次 eligible Web turn，不能声称会改写当前 steer 或 active turn。

Web 的有限保序不升级成 backend-wide 保证。普通 cold reattach、loaded `/goal resume`、`/goal set` 与 automatic
continuation 可能先使用 backend 进程已经持有的 runtime config；safe cold resume 也只能在明确 pause boundary 后携带
override。Focus 不增加 universal barrier、config reload watcher、polling、replay、quarantine 或 durable
synchronization 来消除这些窄窗口。用户若要改变 backend fallback，应按 upstream 方式修改其配置并重启相应 backend。

Web navigation 是独立事实族。`WebWriterProfileStore` 只持久化 selected thread、draft working directory 与
attachment `scope_generation`；`selected_thread_id` 是唯一 durable semantic Web selection。
`WebDocumentRegistry.materialized_thread_id` 只是进程内 bounded-history readiness，因此 older history 准入要求两者都
等于 target。desired subscription edge 及 outcome 只归 `WebRuntimeInterestRegistry`，不能从 selection 或
materialization 推导。navigation/selection generation、blank-submission admission、active-main-turn admission 与浏览器
身份都不能排序、结算或推导 `WebNextTurnSettings`。

## 2. 本项目不再拥有任何 thread-wise next-load setting

下列表面已被移出本项目合同：

- 历史上的项目自管 profile 命令
- `/memory`
- `focusctl thread memory`
- `new_thread_memory_mode_seed`
- 任何项目自管的 thread-level memory/provider/profile restore state

因此，本项目不再维护“某个 thread 下次 resume 时还会额外注入什么配置”这类
持久化事实源。

## 3. 只读事实族：live runtime / upstream snapshot

有些值仍会被读取，但它们不是项目自管的持久化设置：

- live loaded-backend state
- 上游 thread snapshot
- 上游 `config/read` 返回的 runtime view

这些值可以展示在：

- `/status`
- diagnostics
- admin cards

但不得把它们当成：

- 一个可写的项目设置层
- 某个已移除 legacy profile 命令背后的持久化事实源

### 3.1 connection-local effective settings 事实

Focus 在 Web 与飞书共享的 `ThreadEffectiveSettingsRegistry` 中保留一份可丢弃、不持久化的上游设置事实。
它只接受成功的 `thread/start` / `thread/resume` response 与完整的
`thread/settings/updated.threadSettings`；request、`turn/start` response 的 turn identity 本身，以及仅表示排队成功的
`thread/settings/update` ACK 都不是设置事实。`WebNextTurnSettings` 与飞书 binding override 都只是 future-turn intent。

registry 按三个层次保存事实：

1. `thread_base` 是最近一份权威 response 或完整 settings notification；
2. `turn/started` 把当时每个字段的 base 冻结成 matching active-turn snapshot，随后到达的 response 或 settings
   notification 只替换 base，不回填或改写已经 active 的 turn；
3. `model/rerouted` 只有在 `threadId` 与 `turnId` 都精确匹配当前 active turn 时，才覆盖该 turn 的 model。

四个字段各自区分 unknown、known-null 与具体值。固定上游实现始终序列化 `model` 和没有 skip 标记的
`reasoningEffort` option，因此 Focus 严格读取两者；`reasoningEffort=null` 是已知的 auto；未协商 experimental 字段时，缺失的 `approvalPolicy`
或缺失/null 的 `activePermissionProfile` 只让对应字段保持 unknown，不让整次 thread lifecycle 失败。若
continuation-capable cold resume 显式请求 approval 或 permissions，既有 response postcondition 仍要求返回值
精确匹配。完整 settings notification 使用 `model`、`effort`、`approvalPolicy` 与
`activePermissionProfile`；matching notification
若 malformed 或不完整，会原子地把整个新 base 置为 unknown，而不会继续把旧 base 冒充当前事实。
matching current-turn reroute 若缺少合法 `toModel`，只把 active model 降为 unknown，并删除旧 reroute；
其他三个 frozen 字段不受影响。reroute 若缺少可用 turn identity，也会退休当前 active model，因为无法证明它
属于 stale turn；携带可用、已证明 stale turn id 的 malformed reroute 仍是 no-op。

发送 continuation-capable `thread/resume` 前，对 request 显式携带的 model/effort/approval/permissions
执行同样的 base + active 逐字段失效；response unknown 时不得让后到 `turn/started` 冻结旧值。发送
`turn/start` 前，只失效实际显式发送且与现有事实不同的字段，并同时作用于 base 与 active snapshot；发送
`thread/settings/update` 前，同样按字段比较，但只失效 future-turn base。相同值保留，因为上游可能对完整 no-op
不发 notification。失效发生在请求前，ACK 永不登记请求值，因此较早到达的权威 notification 也不会被较晚
返回的 ACK 反向清除。

生产环境只有一个 notification writer：`FocusRuntime` 在 RuntimeLoop adapter-ingress 边界先更新 registry，
再把事件分发给其他 controller。matching completion 或 non-active status 清 active snapshot/reroute；unload、
archive、close、delete、unsubscribe、backend disconnect 与 confirmed reset 清对应 thread 的 positive
base/active/reroute facts 或全部 connection facts。per-thread external-unknown marker 不属于 lifecycle cleanup，
只在 backend disconnect/reset 时清除。stale reroute 与 stale completion 对当前 turn 都是 no-op。
已识别的 start、completion 或 status notification 若缺少可用 lifecycle identity，会退休旧 active snapshot，
而不是保留 stale active evidence。

`fcodex` proxy websocket 不是这个 registry 的 canonical fact ingress：它的 request、ACK、成功 response 与
connection-local notification 都不能安装设置事实。exact service admission 成功后、已审阅的 turn、settings、
resume、continuation-risk goal 或 thread-lifecycle effect 能被发送前，registry 把该 thread 标成
external-unknown，并退休四个字段。canonical
notification 与随后到达的 start/resume response 都不能清除 marker，因为两条 app-server connection 没有共同
revision 或 causal ordering token；任一消息与 external send 的先后都可能无法观察。因此 exact thread 一直保持
unknown，直到 backend connection invalidation 或 confirmed reset 清除这轮 disposable epoch。这是有意限制在该
thread 的 disclosure/native-media 降级，不是第二 writer 或 service quarantine。targetless `thread/start` 没有旧
thread 可退休。

native media admission 只读取这个 owner 的 model：active 时按 exact reroute、frozen snapshot、thread base 的顺序
解析；unknown 一律 fail closed。得到 model 后，新鲜 `model/list` 项还必须在 `inputModalities` 明确包含 `image`；
catalog 项缺失、`inputModalities=null` 和 catalog 读取失败都保持 `unknown`，明确不含 `image` 的列表则是
text-only。只有显式 image 支持与图片字节签名同时通过时才允许 `localImage`；其他情况都保留受控 same-host
路径文本并省略 native media。

即使已经授权，`localImage` 仍是路径交付，不是不可变字节快照。Focus 会在消费前做最后一次 no-follow
文件身份与签名复验，但从复验结束到 app-server 打开该路径之间仍有本地 TOCTOU 边界。彻底关闭它需要
immutable/content-addressed handoff 或 descriptor/upload 协议，详见
`docs/decisions/feishu-attachment-ingress.zh-CN.md`。

该 registry 不是新的可写 setting family，也不声称能重建上游全部 runtime config；它给 native-media admission
和 Web active-turn disclosure 提供同一份只读上游证据。固定上游依据为
[`ThreadStartResponse` / `ThreadSettingsUpdatedNotification`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L170-L305)
、
[`ThreadResumeResponse`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L401-L430)
与
[`ThreadSettingsApplied` notification emission](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/bespoke_event_handling.rs#L1190-L1210)。

## 4. 设置与只读事实表

| 设置族 | 持久化源 | 正式生效边界 | 主要读侧 |
| --- | --- | --- | --- |
| binding-wise next-turn | 当前 binding 的 persisted runtime settings | 飞书普通 prompt 通过官方 `turn/start` 携带这些设置。它们通常应用于新开始的 turn；在极窄的上游 start-or-steer 竞态中，也可能应用于已经 active 的 regular turn。恢复未 loaded thread 时，cold `thread/resume` 也可能携带一小段 one-shot override | `/status`、setting cards、preflight |
| instance-wide Web next-turn | 无 durable record 时为 service-start 捕获的已校验 `codex.yaml` seed；首次显式修改后为单一 `web_next_turn_settings.json` record | 每个 eligible Web new-thread create/first turn、existing-thread ordinary prompt 与 continuation-capable cold active-goal resume 各消费一次 immutable snapshot；steer、observer resume、review、manual compact、F5/auth 不消费 | 所有浏览器共享的 next-turn controls、`GET/POST /api/settings/next-turn` 与 meta |
| thread effective settings | 不持久化；只读 connection-local `ThreadEffectiveSettingsRegistry` | Web next-turn intent 不回填这份事实；只展示上游 response/notification 可证明的 base，缺证据为 unknown | thread/active-turn disclosure |
| Web active-turn disclosure | exact active turn id、matching initiator lease、当前 subscriber 集合与 provenance-bearing effective-settings registry 的只读组合 | 不写入、不应用任何 setting；仅说明当前可证明事实 | Web“运行详情” disclosure；matching reroute model 标 `active_reroute`，turn-start frozen field 标 `inherited`，缺失证据标 `unknown` |

Web active-turn disclosure 不是第三个可写 setting family。`turn/started` 冻结当时已证明的 thread base；具体值
与 known-null 都标为 `inherited`，unknown 保持空 value + `unknown`。后续 response/settings event 不回填 active
snapshot；只有 matching `model/rerouted.turnId` 把 model 标成 `active_reroute`。`WebNextTurnSettings` 或飞书
binding-wise next-turn setting 不能冒充这些只读事实，provenance 标签也不能删除。

`turn/start` response 返回 authoritative `turn.id`，但该 response identity 本身不是 matching current active
lifecycle 或其 effective settings 的证明。Focus 等待 `turn/started` 把该 identity 绑定进 active-turn snapshot。
settings 仍已随 request 发出：若 Web、`fcodex`
或 resume 后的 autonomous goal continuation 赢得极窄竞态，上游可能已在把飞书 input steer 进该 regular turn
时应用这些设置。

## 5. binding-wise next-turn 的判定规则

如果问题是：

- “这个 Feishu chat 的下一轮 turn 会使用什么 model / effort / permissions？”

首先看：

- 当前 binding 的 persisted runtime settings

在这个设置族里：

- `auto` 仍表示“不显式 override”
- 它不再映射到任何项目自管 thread-level persisted state
- adapter 不得把 `auto` materialize 成完整的上游 settings 对象并发送旧
  snapshot 值；普通 auto turn 应让上游当前 thread state 自己延续。
- `model` / `reasoning_effort` 与 `approval_policy` / `permissions_profile_id`
  的空值语义不同：
  - `model` / `reasoning_effort` 可以保持空值，表示 `auto`
  - `approval_policy` / `permissions_profile_id` 是 binding-local 安全
    baseline；新 binding 用 `codex.yaml` seed 解析出初始值，一旦 binding
    落盘，就冻结这份 resolved 安全基线，后续不随实例默认漂移
- 两组设置的 turn dispatch 也不同：
  - approval / permissions 每个 Feishu turn 都显式发送，用于重新声明该 binding 的安全基线
  - model / effort 只在非 `auto` 时发送；`auto` 不重新声明，让 upstream thread 当前状态继续生效
- `codex.yaml` 中的 `model` 与 `reasoning_effort` 只 seed 新 binding 的
  初始 runtime state；进入 binding 后，`thread/start` 与普通
  `turn/start` 都只看 binding runtime settings，不再从 adapter config
  fallback。
- `model_provider` 不是 binding runtime setting；它不会在 `/new`、首条
  prompt 创建 thread 或普通 turn 中从 adapter config 自动注入。`codex.yaml`
  不再接受 `model_provider`；provider 应交给上游 Codex 配置，或仅在调用方
  显式传入 provider hint 时发送。
- collaboration mode 不再是 Feishu runtime setting。如需使用，交给上游
  Codex 配置/行为；本项目不再构造或发送上游 `collaborationMode`
  payload。

### 5.1 model / effort 组合校验

对 model/effort 组合校验，Focus 只使用 app-server `model/list` 返回的
`supportedReasoningEfforts` 作为显式 model 的 metadata；不得拿第 3.1 节用于 native media 的窄 registry
反推 effective effort 或校验这组可写设置。

- effort 为 `auto`：`validated`
- model 为 `auto` 且 effort 显式：`deferred`
- 显式 model 没有可用 metadata 且 effort 显式：`deferred`
- 显式 model metadata 声明支持该 effort：`validated`
- 显式 model metadata 未声明支持该 effort：`rejected`

`model/list` 是一份整体的权威 catalog，不是可 best-effort 拼接的提示列表。
如果 `data` 中存在非对象、缺少合法 model selector，或某个已返回的 capability
字段类型不符合协议，Focus 必须拒绝整次 catalog 读取；不得静默跳过坏条目后把
“catalog 损坏”解释成“模型不存在”或“不支持某项能力”。协议未返回的可选
capability 仍保持 `unknown`。

控制面不允许用户新建 `rejected` 组合，但不会迁移或修复已有 binding
数据，也不会在 prompt dispatch 前增加第二套 admission。已有值仍按原字符串传给
app-server，最终执行结果由上游决定。

显式 model / effort 在 `turn/start` 上的上游语义是作用于当前及后续
共享 thread turn。因此，Feishu binding 与本地 TUI 不共享一份项目持久化设置，
但它们仍可能通过同一个 upstream thread 观察或覆盖彼此最近发送的显式值。

## 6. binding store 的空值规则

`chat_bindings.json` 是持久化投影，不是运行语义事实源。runtime-setting
的值、安全基线和显式配置意图是几类不同事实。store 层只负责：

- 保存和读取字符串字段，以及 `configured_settings` 列表
- 校验结构和非空枚举值
- 兼容旧字段名（例如 legacy `sandbox` -> `permissions_profile_id` 字段）

store 层不得引入实例默认 fallback。空字符串必须原样保留，直到
`BindingRuntimeManager` hydrate 时，才按当前实例配置解释：

- `approval_policy` / `permissions_profile_id` 空值只表示旧记录或尚未
  materialize 的 store 形态；hydrate 时解析为当前实例默认，之后一旦
  binding 再次落盘，就写出 resolved 安全基线
- 旧 `collaboration_mode` 字段读取时忽略，新保存不再写出
- `model` / `reasoning_effort` 空值 -> `auto`，不显式 override

`configured_settings` 是 binding-local 的显式用户操作事实源，但不是
`approval_policy` / `permissions_profile_id` 是否存在安全基线的事实源。
它只由 `/model`、`/effort`、`/approval`、`/permissions` 或对应卡片交互写入；
`codex.yaml` seed 不产生 intent。即使某个 value 等于实例默认值，只要对应
setting 名字出现在这个列表里，它仍表示用户显式操作过。

因此：

- 对 `model` / `reasoning_effort`，`configured_settings` 区分“用户显式选择
  auto”与“从未配置”
- 对 `approval_policy` / `permissions_profile_id`，binding 持久化值本身就是
  当前 binding 的安全基线；`configured_settings` 只说明用户是否显式改过它
- 旧记录没有 `configured_settings` 时，store 会按规范化后的非空 setting value
  保守推断 intent；历史上的空值 `auto` intent 无法恢复，这个歧义可以接受

未绑定但已保存过 setting 的 binding 是合法状态：没有 `thread_id`，但承载了
用户的下一轮配置决策或 binding-local 安全基线。具体来说，
`configured/unbound` 表示没有 thread bookmark，但持久化 binding 仍有
`configured_settings`、安全基线或其他必须保留的
binding-local fact。管理面可显示为 `configured/unbound`；它不是 stale thread
binding，不应被 `binding clear-stale` 清理。

## 7. 一条维护规则

如果将来要新增设置，必须先明确它的 owner、frontend scope 与生效边界，并且只归类为：

1. binding-wise next-turn settings
2. instance-wide `WebNextTurnSettings`
3. 另一份有正式合同的明确 owner/scope；不能从 browser、writer、selection 或 thread identity 推导
4. 只读 upstream/diagnostic 视图

在这个归类存在之前，该设置不得成为新的命令面、新的项目持久化状态层，或隐式跨前端 setting。
