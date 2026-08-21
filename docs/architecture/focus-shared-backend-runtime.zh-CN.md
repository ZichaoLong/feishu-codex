# `focus` / `fcodex` Shared Backend 运行时模型

文档角色：中文规范源。英文同步副本：`docs/architecture/focus-shared-backend-runtime.md`。

本文只说明当前 shared-backend 拓扑、wrapper/proxy 边界与 runtime owner。具体 mutation、main turn、
create/resume 和 server request 语义以正式合同为准。

另见：

- [FOCUS 技术设计](./focus-design.zh-CN.md)
- [main turn owner 合同](../contracts/root-operation-owner.zh-CN.md)
- [fcodex main-turn 与 proxy 合同](../contracts/fcodex-operation-owner.zh-CN.md)
- [`thread/create` 本地提交合同](../contracts/thread-create-local-commit.zh-CN.md)
- [`thread/resume` 本地提交合同](../contracts/thread-resume-local-commit.zh-CN.md)
- [server request 生命周期合同](../contracts/server-request-lifecycle.zh-CN.md)
- [thread profile 语义](../contracts/thread-profile-semantics.zh-CN.md)

## 1. “Shared backend”的含义

```text
shared CODEX_HOME and persisted thread namespace

machine-global coordination
  - running-instance registry
  - thread runtime leases

instance A
  Feishu -----------+
  Browser -> Gateway+-> Focus service -> RuntimeLoop -> owned codex app-server
  focusctl ---------+                         ^
                                              |
  focus/fcodex -> per-launch local proxy -----+

instance B
  independent Focus service -> independent owned codex app-server
```

`shared backend` 指同一个 Focus instance 内，Feishu、Web、管理面与 wrapper 共享该实例拥有的
app-server；它不表示所有 instance 共用一个 app-server。多个 instance 共享 `CODEX_HOME`，并通过机器级
registry、loaded gate 和 runtime lease 避免同一 thread 被多个受管 backend 同时 materialize。

一个显式 staged external-ingress transaction 可在 RuntimeLoop 中短暂 prepare/settle mutable fact，同时凭同一
service ingress receipt 在 loop 外执行阻塞 effect；内部 scheduled worker 则继续由唯一 worker lifecycle barrier
管理。每个 caller 必须明确采用适合自己的边界，不能从拓扑图本身推断；`focus` / `fcodex` proxy 仍按其独立协议
边界直接透传到已发布 backend。由 RuntimeLoop 发现、经 service dispatcher 的 background external-transaction
入口启动的 Web cleanup 属于前一类：daemon worker 启动前即取得 service-ingress receipt，不需要第二份 worker registry。

Focus service 只支持 owned、same-host app-server。`focusctl` 与 wrapper 是连接已发布 endpoint 的内部
attached client，不是外部 deployment mode，也不取得 start/stop/reset authority。裸 `codex`、IDE、另一台
机器和未注册 app-server 不在这套协调边界内。

## 2. Instance backend 生命周期

每个 instance 独立持有：

- `FOCUS_CONFIG_DIR` 与 `FOCUS_DATA_DIR`；
- service lease 与 control plane；
- loopback Web Gateway（启用时）；
- `service -> dormant guardian -> codex app-server` 生命周期链；
- 当前 backend endpoint、process identity、generation 与 capability token 的发布状态。

启动顺序是：取得 service lease，检查旧 runtime record，预留 cleanup token，启动 dormant guardian，
原子发布 runtime record，最后激活 guardian。publication 前 parent 消失不会启动 app-server。

`ServiceRuntimeLifecycle` 仍是 service phase 以及 startup、rollback、shutdown 顺序的唯一 owner。
`ServiceRuntimeAuthority` 只在该 lifecycle 调用时协调既有 machine-visible instance registry、global
loaded gate 与 service/thread-runtime lease owner；它不建立第二套 lifecycle，也不复制这些 owner 的事实。

Guardian 是进程生命周期 owner。正常停止、child 自然退出或 Focus service 失联时，它先收敛平台可证明的 owned
process set，再写一份绑定 exact generation 的 cleanup receipt。下一代 service 会短暂等待仍存活的 guardian；只有
guardian 已退出或 PID 已复用，且 receipt matching 时，才自动退休旧 generation。

receipt 缺失或不匹配刻意不按普通崩溃处理。持久状态无法区分“进程树已清干净、但 receipt 写盘失败”和“guardian
在完成清理前被强杀”这两种情况，因此启动保持 fail-closed。错误会给出当前 instance 的 exact runtime record；操作者
按文档中的平台边界独立检查并清理进程后，只能删除该 record 再重试。Focus 不提供 recovery/force 命令，因为命令本身
不能补出 cleanup proof。旧 direct-child record 从未具备 guardian tree proof，也使用同一人工边界。

平台证明边界为：

- Linux：要求 subreaper，并收敛被收养的 descendants；
- Windows：app-server 在执行前进入 kill-on-close Job Object；
- macOS：只能证明 app-server 与其 process group；主动建立新 session/group 的 tool/MCP descendant 可能在 service
  stop、reset 或 service crash 清理后继续存活，Focus 事后无法可靠发现或终止它。

cleanup receipt 只表示上述平台特定 containment set 已收敛。在 macOS 上，它不能证明主动逃逸的 descendant 已不存在；
diagnostics 或人工删除 record 也不能增强这份证明。

这项已关闭的 recovery 决策与仍受阻的平台缺口见
[架构债务台帐](./architecture-debt-register.zh-CN.md)。

## 3. Instance 选择与 endpoint 发现

`focusctl`、`focus` 与 `fcodex` 先解析 `--instance`，再通过该实例仍存活的 control plane 读取当前 ready
backend endpoint。configured URL、registry 中的旧 URL 或 durable runtime record 都不能单独作为拨号依据；
loopback 端口可能已经被其他进程复用。

默认 endpoint 优先使用 `ws://127.0.0.1:8765`；冲突时 service 为本实例选择空闲 loopback 端口并发布。
如果目标 instance 未运行或 replacement backend 尚未 ready，attached client 快速失败，不会静默启动另一份
隔离 backend。

Web Gateway 同样按 instance 发布。一个 instance 的一个 Gateway 服务多个 browser document 和多个 thread；
浏览器永远不接触 backend capability token。

## 4. Wrapper 与本地 proxy

`focus` / `fcodex` 的启动链为：

1. wrapper 选择运行中的 Focus instance 和最终 cwd；
2. wrapper 启动一个带 per-launch bearer token 的 loopback websocket proxy；
3. proxy 连接所选实例发布的 app-server endpoint；
4. wrapper 以内部 `codex --remote <proxy>` 启动上游 TUI。

这里的 `--remote` 是上游 TUI 到本地 proxy 的协议链路，不是用户可选择的外部 app-server。用户提供的
external `--remote` target 被拒绝。

最终 cwd 取显式 `--cd` / `-C`，否则取调用 shell cwd。wrapper 把它传给上游 TUI，也交给 proxy；proxy 只在
已准入的 `thread/start` 缺少 cwd 时注入该值。这是为修正上游 remote 启动路径的窄兼容层，不是通用 payload
rewrite。

独立的 `--` terminator 会结束 Focus 自己的 option 与 subcommand 解析；该分隔符及其后的每个参数都原样交给
上游。因而 opaque tail 中的上游 `--cd` 不会改变 wrapper cwd，也不会触发 Focus 的保留参数拒绝。

Upstream remote resume 可能先连接、断开、再连接。proxy 因而跟随 wrapper parent process 生命周期，而不是
在第一个 websocket 断开时退出。

## 5. Proxy 的职责

Proxy 不是透明任意 RPC 通道。它负责：

- websocket token 鉴权；
- frame/schema 与 method 分类；
- targetless read allowlist；
- exact direct-root target 校验；
- child metadata-read 的窄例外；
- main-turn submission/control 准入；
- targetless `thread/start` 的一次性 current-generation capability；
- cwd 注入；
- 把 client response 固定到 exact participant、connection、request id 与 action token。

Proxy 不持有 Codex callback lifecycle，也不从 socket 状态推导 writer。`connected/grace/orphaned` 只解释
fcodex endpoint 的重连与清理；main-turn owner 只来自共享 `InteractionLeaseStore` 中 PID-bound 的 exact
submission/active lease。

## 6. 两条不能混淆的协调轴

### 6.1 Backend residency

机器级 loaded gate 加 `ThreadRuntimeLeaseStore` 回答“哪个 Focus instance 可以把 thread 保持 live”。
跨实例只支持 cold migration；无法确认其他实例是否仍 loaded 时拒绝。runtime lease 不是用户 writer。

app-server subscription 是另一项 connection-local fact。固定 Codex `0.147.0` 中，
[`thread/unsubscribe` 把 request connection 传入 effect](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/thread_processor.rs#L472-L479)，
并且[只从互相索引中删除该 connection/thread pair](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/thread_state.rs#L472-L504)。
因此 canonical Focus adapter connection 由其 Web desire、canonical 飞书 subscriber、pending interaction 与 exact
runtime fact 决定。每个 `fcodex` proxy 都有独立 app-server connection/subscription：canonical unsubscribe 不会删除
或取消它，它的存在也不能 veto canonical cleanup；它仍可独立保持 machine runtime resident。

### 6.2 Main-turn writer

`InteractionLeaseStore` 只回答“哪个 Feishu next-turn/FIFO 或 exclusive/autonomous action 持有 submission/activity
identity”。idle 无 owner；只有这些 lease-bearing effect 在发送前取得 blank。普通 Web/`fcodex` `turn/start`
是 upstream-routed realtime input，不读取、取得或释放 lease，也不因现有 foreign holder 返回 writer denial。
对于确实存在的 blank，ordinary `turn/start` known-success response 保留 authoritative upstream `turn.id`，但绝不
从 response 单独激活或转移 Focus lease；matching `turn/started` 才绑定本地 active lifecycle，matching completion
只释放 exact active lease。没有任何 surface
能把未绑定 completion 关联到某次普通 submission；若 started 已过去/遗漏，blank 会 fail closed 保留到权威
terminal evidence 或 service restart。飞书可以在权威重读 root 已 inactive 后额外结算 exact 的普通 prompt blank，
但不能因此绑定 turn id 或归因 completion；其进程内 admission token 是事务 receipt，不是第二份 writer fact。
若 ordinary fcodex start 与已有 fcodex exclusive/autonomous blank 并发，后续 lifecycle 无 effect identity 可区分
来源，仍可能激活该 blank；这是接受的极窄上游竞态，不增加关联状态机。inline review response identity 与 compact
的空 response 保持各自 method-specific 路径。child、pending server request、endpoint、delivery 与 presentation
均不延长 lease。shared steer、interrupt 与 shared server-request response 分别使用自己的 exact turn/request + surface
authority，不从 writer relation 派生，也不转移 writer。

飞书 active-observer `thread/resume` 也是 method-specific 的非 writer 状态：它只把 detached binding 与
response 中的 exact active turn 锚定为后续 live presentation，既不取得或转移 main-turn lease，也不产生
cancel、approval 或 pending-request response authority。matching terminal 会清除这笔 observer provenance；
attached binding 本身继续存在，后继 turn 不继承 observer 身份。

共享同一 backend 是避免 live-runtime fork 的必要条件，不自动授予 writer。详情见
[main turn owner 合同](../contracts/root-operation-owner.zh-CN.md)。
飞书 FIFO continuity 是另一项进程内 ordering fact；它不保留 writer，也不能被另一 binding 或 root epoch 使用。

`focus_runtime/binding_coordinator.py` 中的 `BindingRuntimeCoordinator` 不增加第三条协调轴。它集中 17 个跨 owner
binding/runtime 事务，保持既有 shared-lock 临界区与 local-commit-before-effect 顺序，并且只经 typed 的
`Callable[[str], bool]` interest port 观察 Web retention。binding/session、transition、batch-deactivation、
interaction-lease 与 thread-runtime 事实仍由既有 owner 持有；coordinator 不保存 mutable fact。原 composition root
29 项方法簇中的其余 12 项只是 direct-owner 薄转发，现已绕过 `FocusRuntime`。

飞书 runtime extraction 同样不增加新的协调轴。`FeishuPlatform` 唯一持有 attached Feishu bot fact，并集中平台 lookup
与 presentation effect routing；`FeishuSurface` 经既有 owner 安装和分派 inbound message、command 与 action route；
`TerminalResults` 经 typed port 协调 terminal-result lookup、storage 与 publication。`TerminalResultStore` 仍是持久化
fact owner，queue ordering 仍由既有飞书 execution-queue owner 持有。

## 7. 主要操作边界

| 操作 | 当前最小边界 |
| --- | --- |
| inventory / read | 从目标 instance 的 app-server 读取；缓存不能证明 thread 不存在或 backend ready。Web directory/open/history/inspection 先在 RuntimeLoop 冻结 exact document/target/backend/observation，再在 loop 外执行 store/app-server read 与 detached projection，最后只凭原 receipt exact settle/final-check；同一 service-ingress receipt 跨越 whole transaction 并参与 shutdown barrier。notification、F5 或 backend replacement 后的旧 result 不覆盖新事实 |
| `thread/start` | Web/Feishu 共用 canonical adapter：以后新建的持久 thread 显式请求并验证 `historyMode=paginated`，既有 legacy 不迁移；typed response 后执行同栈 local callback。fcodex 继续原样透传 upstream TUI payload，并用 current-generation capability 结算；unknown 或不支持 paginated 都不由 Focus 自动重发或隔离其他 thread |
| `thread/resume` | loaded gate/runtime lease 后发送一次；成功与 immediate local commit 用进程内 receipt 连接；unknown 不建立 durable marker。可能自主开 turn 时通常须先取得 exact blank main-turn lease；Web outcome unknown 在后续 turn-producing effect 尚未调用时，只释放当前 operation 在 resume 前 fresh 取得的 exact blank 并保留 runtime interest，borrowed/activated/replaced 与仍 recovery-required/stale 的 acknowledged-incomplete lease 均不释放。Web cold open 的 known success 必须先提交 runtime interest；outer read/DTO 后来因 document、notification、projection 或 backend generation stale 而失败时不回滚或重分类该 resume。另一个窄例外是 exact active Focus turn 旁的已准入 native fcodex attach，它保持 writer 不变并接受 upstream goal-continuation 语义；native TUI settings/reviewer 字段保持 upstream-owned，不成为 Focus effective-settings 事实。飞书 running-observer 的独立 method-specific 例外见下一行 |
| canonical Web runtime cleanup | 只有最后一项 Web desire 消失且没有 pending interaction 或 canonical 飞书 subscriber 时才准入 cleanup。`WebRuntimeLifecycleCoordinator` 每 thread 只持有一笔 flight，并至多合并一个 successor；它冻结 exact interest/backend generation，在 RuntimeLoop 外探测 backend 与完整 service-holder release record，claim 与 same-thread canonical resume/start 窄互斥的 unsubscribe transition，发送前重验且至多发送一次 unsubscribe，holder release 前再次重验，再执行 full-record holder CAS，且仅在原 facts 仍匹配时清理 local interest/cache。新 interest 或 holder successor 保留 runtime；unknown/CAS mismatch 不 replay，也不阻塞其他 thread。unsubscribe 已 claim 时，canonical `turn/start` 在 transport/settings mutation 前得到 typed known-no-effect；active canonical starts 阻止 claim，但彼此仍可并发。steer/interrupt 与各 `fcodex` 独立 connection 不在这条 fence 内。final recheck 到 unsubscribe send、release recheck 到 holder CAS 的窗口是明确接受的 bounded non-guarantee |
| 飞书 active-observer `thread/resume` | 飞书 command/card 发起时仍先执行既有 inbound actor admission 与 group `all` thread exclusivity；trusted CLI/control attach 保持既有 admission，不新增更强全局群聊检查。只允许 detached binding 的权威 direct-root active preflight，并在发送前 exact 重验 active；不取得或转移 main-turn lease，不携带 next-turn override。resume response 到达后的同一 local callback 在 shared lock 内提交 attached binding 与唯一非空 `inProgress` turn anchor；response 已 idle 时退化为普通 attached 且不建立 observer provenance/card，response 仍 active 却无唯一 exact turn id 时 fail closed 并恢复 detached。observer page 只 bootstrap response 中可用的 assistant 文本，明确披露 attach 前历史可能不完整，再接收后续 live notification；它不显示/自动 reject upstream pending-request replay，也不取得取消或审批 authority。普通消息、`turn/start`、next-turn settings 与 Feishu FIFO 不变；matching terminal 清除 observer provenance，后继 turn 恢复普通 attached 行为。详见[飞书 thread 生命周期合同](../contracts/feishu-thread-lifecycle.zh-CN.md)与[`thread/resume` local-commit 合同](../contracts/thread-resume-local-commit.zh-CN.md) |
| Web/`fcodex` main-turn start、steer 与 interrupt | 普通 `turn/start` 保持上游 start-or-steer，不读取、取得或释放 shared lease；Web existing-thread prompt 使用一笔 exact browser mutation 与 bounded result receipt，fcodex 使用 exact request token 结算。Web prepare 若冻结 active A，则整笔 attempt 只对 A 调用一次 `turn/steer`；只有 prepare 时没有 exact active id 才调用一次 `turn/start`。successor mismatch 与 no-active 不 retarget/fallback。review/compact 与 autonomous continuation 仍按各自合同取得 exclusive blank。connected/materialized Web document 或合格的 live/attached `fcodex` endpoint 可以 steer 或 interrupt exact direct-root turn 且不转移 writer；fcodex 非空 interrupt id 接受 connection source 或 matching exact active lease 作为 attach proof，空 id 只接受 current connection source 并保留 upstream current/startup 语义。两项 effect 都不扫描 descendants，也不改打 successor turn |
| 飞书普通 prompt start 与 FIFO | 即时、出队与 synthetic 飞书 prompt 都通过官方 `turn/start` 发送完整 input/settings payload。上游 idle 时通常开始新 turn；在极窄竞态中，若 Web、`fcodex` 或 resume 后的 autonomous goal continuation 先变成 active，上游 start-or-steer 会把飞书 input 与本轮 settings 加入该 active regular turn。response 保留 authoritative upstream `turn.id`，但 Focus 等 matching `turn/started` 激活本地 lease/execution lifecycle。若该 notification 遗漏，completion 不能绑定 blank；同一 `FeishuRootOperationController` 只能在权威重读 root 已 inactive 后结算 exact 的普通 prompt blank，且不把 completion 归因到本次 submission。FIFO 准入仍只接受当前 exact execution anchor、既有 same-binding/root/epoch continuity，或在 binding lock 内再次核对的本进程 preprojection exact turn；start response 不能自行建立 continuity。unknown/malformed outcome 继续 blocked。详见[飞书 thread 生命周期合同](../contracts/feishu-thread-lifecycle.zh-CN.md)与[定时 prompt 合同](../contracts/scheduled-prompts.zh-CN.md) |
| 飞书显式 `/steer` | 只允许 `/steer <非空文本>` 进入独立 exact-effect owner；普通消息、附件、FIFO 与 settings 路径不变。owner 冻结当前 attached/running binding 的 exact thread/turn，执行 group `all` exclusivity、权威 direct-root active 重读、connection-generation fence 与最终 binding/execution CAS 后，只调用一次官方 `turn/steer`。active observer 可用；compact 本地拒绝，其他 upstream-owned rejection 归类为 known no-effect。dispatch 后 transport/timeout/protocol 或 response-ID 异常只提示结果 unknown，不 retry、fallback 或 retarget successor；不取得 writer、lease、page、approval 或 lifecycle authority |
| Web active-turn disclosure | 只读组合 exact active turn、matching initiator lease、当前 Feishu subscriber 与 turn-start frozen effective-settings provenance；缺失字段显示 unknown，instance-wide `WebNextTurnSettings` intent 不冒充当前事实 |
| server request response | callback lifetime 归 app-server；Focus 只保留 current-generation identity 与一次性 surface token |
| backend reset | fence ingress，只读捕获 current-process exact main-turn leases，确认旧 owned child OS exit/wait，以 full-record CAS 与各 surface owner 幂等退休旧 lease/generation/local capability，仅在该 post-stop stage 执行 binding detach 与 execution interrupt/finalize，清理本实例 runtime holder，启动并验证 replacement，再发布准入；stop/retirement/projection 未确认时保持 fenced 且不启动 replacement |

Backend reset 不做 writer handoff，不迁移旧 callback，也不恢复 unknown request。lease capture 只包含 current service
PID 与 exact process identity；其他 PID、PID 0 及 capture 后的 successor generation 不得被 reset 清除。presentation 是
reset 事务之后的 best-effort consequence。

Web existing-thread prompt 只发一个 POST。RuntimeLoop 只执行 prepare/settle：metadata、attachment I/O
与唯一 upstream RPC 在持有原 service-ingress receipt 的 external worker 中执行。进程内
`WebPromptResultRegistry` 按 exact `(thread_id, mutation_id)` 保存有界 result receipt，不保存 payload、
不写盘、不 replay，也不成为 runtime lease 或 writer。F5/poll 只能 GET 这份 receipt；淘汰、
service restart 或 backend replacement 后的 miss 只表示本地证据不可用，不证明 no-effect，也不授权重放。
规范行为见 [Focus Web prompt mutation 恢复合同](../contracts/focus-web-prompt-mutation-recovery.zh-CN.md)。

## 8. Credential 边界

以下 credential 不得复用：

- control-plane/service token：本地管理与 instance discovery；
- backend websocket token：连接该 instance 的 app-server；
- proxy token：单次 `focus` / `fcodex` 启动的本地 websocket；
- Web bootstrap token：一次性换取 loopback browser session。

这些本地 token 与跨进程 lock 文件必须是普通文件；Focus 以 no-follow 方式打开，
并复核打开 descriptor 与路径的 file identity。发现符号链接、非普通文件或
路径在检查期间被替换时，操作 fail closed，不把外部文件当作本地 authority。

浏览器不得获得 backend/control-plane credential。Python attached client 显式绕过用户的外网 websocket
proxy；wrapper 只补强 loopback `NO_PROXY/no_proxy`，不删除用户的外网代理配置。

## 9. 已知边界

- 裸 Codex 或未注册 backend 不受 Focus runtime lease 和 main-turn lease 协调。
- upstream `--remote` wire、连接顺序和 `thread/start` payload 可能变化，升级 Codex 时必须重审 proxy。
- TUI 内 thread picker 属于上游，可能不同于 Feishu/Web/`focusctl` inventory。
- upstream 没有独立的 pure-subscribe RPC；active observer 复用 running `thread/resume`，因此 exact active
  pre-send check 后的 response 仍可能已经 idle。resume snapshot 只提供其中可用的 assistant 文本，不承诺
  回放 attach 前逐条 command/tool delta；飞书中途接入明确接受这项有界历史缺口。
- 当前 Gateway 仍是 loopback；外部 Web 访问是独立鉴权与部署合同，不能通过暴露 app-server 实现。
- macOS 上主动建立新 session/group 的 tool/MCP descendant 不在受支持 containment set 内，可能在 stop、reset 或
  service crash 清理后继续存活。
- cleanup receipt 缺失/不匹配或 legacy direct-child record 要求按平台边界独立检查进程，再人工清理 exact record；
  刻意不提供把 unknown 变成 proof 的命令。

## 10. 代码入口

- composition root：`focus_runtime/runtime.py`（`FocusRuntime`）
- backend lifecycle：`owned_app_server_guard.py`、`stores/app_server_runtime_store.py`、
  `service_runtime_lifecycle.py`
- adapter/generation：`adapters/codex_app_server.py`、`adapter_ingress_gate.py`、
  `codex_protocol/client.py`
- wrapper/proxy：`fcodex/cli.py`、`fcodex/proxy.py`
- fcodex owners：`fcodex/main_turn_owner.py`、`fcodex/operation_service.py`、
  `fcodex/participant_runtime_registry.py`、`fcodex/interaction_inbox.py`
- runtime coordination：`focus_runtime/service_authority.py`、
  `focus_runtime/binding_coordinator.py`（`BindingRuntimeCoordinator`：17 个无状态跨 owner binding/runtime 事务，
  保持 shared-lock 临界区与 commit/effect 顺序）、
  `focus_runtime/thread_targets.py`（`CodexThreadTargetService`：权威 thread target 读取、校验与选择、
  Web resume target 协调、既有错误分类与 interrupt 转发）、`thread_runtime_coordination.py`、
  `stores/thread_runtime_lease_store.py`、`stores/instance_registry_store.py`
- 飞书 runtime surface：`focus_runtime/feishu_platform.py`（attached bot fact 与平台 routing）、
  `focus_runtime/feishu_surface.py`（inbound route 安装与分派）、`focus_runtime/terminal_results.py`
  （terminal-result persistence/publication coordination；持久化 fact 仍在 `stores/terminal_result_store.py`）
- create/resume：`thread_create_transaction.py`、`thread_runtime_authority.py`
- 飞书 active-observer attach：`feishu_active_observer.py`、`feishu_thread_session_coordinator.py`、
  `focus_runtime/feishu_thread_session_composition.py`、`binding_execution_runtime.py`、
  `runtime_admin/binding_application.py`
- 飞书 start/FIFO：`prompt_turn_entry_controller.py`、`feishu_execution_queue.py`、
  `feishu_execution_queue_service.py`、`thread_access_policy.py`
- 飞书显式 steer：`feishu_turn_steer.py`、`focus_runtime/feishu_surface.py`
- Web Gateway/request admission/recovery：`bot/web_runtime/gateway.py`、
  `bot/web_runtime/gateway_external_transaction.py`、`bot/web_runtime/thread_open_coordinator.py`、
  `bot/web_runtime/thread_read_projection.py`、`bot/web_runtime/thread_inspection.py`、
  `bot/web_runtime/direct_thread_target_coordinator.py`、
  `bot/web_runtime/gateway_request_admission.py`、`bot/web_runtime/auth.py`、
  `bot/web_runtime/controller.py`、`bot/web_runtime/turn_command_coordinator.py`、
  `bot/web_runtime/operation_service.py`、`bot/web_runtime/mutation_recovery.py`
