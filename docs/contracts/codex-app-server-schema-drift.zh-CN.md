# Codex app-server schema 漂移守卫

文档角色：中文规范源。英文同步副本：`docs/contracts/codex-app-server-schema-drift.md`。

状态：已接受的维护合同。审阅后的基线见
[`codex-app-server-schema-baseline.json`](codex-app-server-schema-baseline.json)。

## 目的

Focus 只把一部分 Codex app-server RPC 适配为 Focus 自己的飞书、fcodex 和
Web 合同；上游的 request union 会独立增长。因此，不能因为某个前端恰好能
收发某个 JSON method，就把它默认为 Focus 已支持的能力。

[`scripts/check_codex_app_server_drift.py`](../../scripts/check_codex_app_server_drift.py)
是升级期的 fail-closed 守卫。Focus 当前依赖 `thread/turns/list` 等实验性
历史接口，所以它要求输入来自带 `--experimental` 的生成结果。

它不是浏览器 schema、公开 API，也不是运行时 JSON Schema validator。

## 基线锁定的范围

基线记录上游 commit 与生成命令，并锁定：

- 完整的 `ClientRequest`、`ServerRequest`、`ServerNotification` method
  清单；
- 完整的 `ThreadItem` type 清单；
- `CodexAppServerAdapter` 实际 outbound client request method 必须可由静态
  literal 或审阅过的 helper 参数转发证明，并且属于固定的官方 `ClientRequest`
  inventory；
- Focus 已引用的每个 method，以及上游所有顶层 params 含 `threadId` 的
  client method 的显式分类；
- 对具体 payload 不带非空 `threadId` 的 fcodex client request，已审阅的
  literal allowlist 与不可变的 default-deny 动作；
- Focus 实际消费的请求/通知参数、选定 response root 与 `ThreadItem` 的
  完整可达 definition closure 的语义指纹。

每个指纹旁保留直接的归一化 schema，便于看到有意义的 diff；标题、描述、
默认值、示例和 format 等非 wire-semantic 字段不会制造噪音。

client method 至少明确区分：

- `shared_operation_mutation`
- `observer_read`
- `connection_local_request`
- `explicit_admin_control_plane`
- 明确拒绝或暂不支持的类别

例如 `thread/unsubscribe` 是 connection-local，不是全局 operation mutation。
`thread/resume` 则归入 shared-operation mutation：在 goal 已确定不继续时，它可以只是 idle subscription
bootstrap；但它也可能 load thread，或在 response 之后触发 persisted active goal 的 autonomous
continuation，因而必须经过 main-turn lease/goal-preflight gate。`thread/goal/set` 同样是 shared-operation mutation：
最终得到 `active` 时可继续一个 idle loaded thread，绝不能被当作原始 persisted-state control。
当前 `explicit_admin_control_plane` 有意为空：operation-owner 合同没有通过
“管理员式 owner 路由”开放额外 raw app-server control。这也不是 global-control
逃生门：`fcodex_unscoped_client_request_policy` 是 default-deny，guard 会静态核对
proxy 的 literal allowlist 与已审阅策略完全一致。允许的极小 discovery/connection
集合与 optional `threadId` 规则见 fcodex owner 合同。

server request 还必须区分需要 owner 路由的交互与无状态 protocol utility。当前
baseline 唯一自动处理的 utility 是 `currentTime/read`：Focus 严格验证其
`{ threadId }` 参数，在收到请求的同一 websocket generation 上返回宿主机整数 Unix
时间戳；它不会投影给任何前端，也不会建立或清除 main-turn lease / server-request projection。未知 method、
畸形请求或未来扩展的 utility shape 都不能继承这个例外。`error` 与 `warning` 的完整 params closure
被显式指纹并作为 Web typed runtime notice 输入；Focus 只投影已审阅字段，不解析自然语言文本。
`model/rerouted` 与 `thread/settings/updated` 也被显式纳入 notification 指纹。它们仍不直接投影为用户可见的 transcript
事件，但除 operator 诊断日志外，Focus 还会把它们作为 connection-local effective-settings registry
的 provenance 使用；native media 只读取其中的 model。

## 日常升级检查

先用候选 Codex 二进制生成 schema，再在改 Focus 代码或基线前检查：

```bash
schema_dir="$(mktemp -d)"
codex app-server generate-json-schema --out "$schema_dir" --experimental
python scripts/check_codex_app_server_drift.py --schema-dir "$schema_dir"
```

若有开发者提供的只读 upstream checkout，并且 Rust 构建可用，可用其 exporter
生成同样的输入。用户未提供 checkout 路径时先询问；路径只保留在当前 shell，
Cargo 输出必须放在 checkout 外：

```bash
codex_checkout="<developer-provided-read-only-checkout>"
schema_dir="$(mktemp -d)"
codex_target_dir="$(mktemp -d)"
upstream_head="$(git -C "$codex_checkout" rev-parse HEAD)"
upstream_status="$(git -C "$codex_checkout" status --porcelain=v1)"
CARGO_TARGET_DIR="$codex_target_dir" \
  cargo run --locked --manifest-path "$codex_checkout/codex-rs/Cargo.toml" \
    -p codex-app-server-protocol --bin export -- \
    --out "$schema_dir" --experimental
test "$(git -C "$codex_checkout" rev-parse HEAD)" = "$upstream_head"
test "$(git -C "$codex_checkout" status --porcelain=v1)" = "$upstream_status"
python scripts/check_codex_app_server_drift.py --schema-dir "$schema_dir"
```

guard 本身和它的 unit test 不依赖上游 checkout 或可执行 Codex；只有刷新基线
时才需要此前生成的 schema 目录。普通仓库测试只使用已提交的基线。不得把
`codex_checkout` 或其他本机路径写入 baseline 或任何长期证据。

## 刷新基线

上游升级不能只因为生成命令成功就视为完成。审阅者必须对每个变动的
method/item 明确决定：支持、拒绝，还是保持不在 Focus 范围内。

```bash
reviewed_upstream_commit="$(git -C "$codex_checkout" rev-parse HEAD)"
python scripts/check_codex_app_server_drift.py \
  --schema-dir "$schema_dir" \
  --write-baseline \
  --upstream-commit "$reviewed_upstream_commit" \
  --generator "<可移植的准确二进制版本与命令；不含本地路径>"
git diff -- docs/contracts/codex-app-server-schema-baseline.json
python -m pytest -q tests/test_codex_app_server_schema_drift.py
```

`--write-baseline` 必须显式给出 commit。它只会在 policy 已通过校验后刷新
自动生成的 inventory/fingerprint；它绝不会自动发明 method 分类。提交前必须
审阅 diff。

## fail-closed 的准确含义

以下任一情况都会失败：

- 输入不是 `--experimental` schema；
- 任一上游 method 或 `ThreadItem` inventory 变化；
- adapter 发出不在固定官方 `ClientRequest` inventory 中的 method，或 method
  由未审阅的动态表达式构造；
- Focus 源码开始引用没有审阅分类的上游 method/item；
- 当前或新增的、顶层带 `threadId` 的 client method 没有分类；
- fcodex 的 no-thread-target 策略不是 default-deny、列出了不存在或必须携带
  thread target 的 method，或与运行中 proxy allowlist 不一致；
- 已分类 method 消失，或 Focus 依赖的 request/response/item shape 改变。

这只是 fail-closed 的**升级期**一半。运行时 owner router 才是 live message 的
权威：fcodex 只能交付已被 service-owned interaction router 分类的 server
request；未知 server request 必须向上游拒绝，不能交给已有 writer。未知的、带
`threadId` 的 client request 同样必须先分类，才可进入 thread-owner 准入路径。
schema guard 负责让这种上游清单变化无法悄悄通过审阅；它本身不发送 JSON-RPC
response，也不授予 writer lease。

## Runtime connection 准入

生成 schema guard 之外还有一层 connection-local 协议 gate。每条 websocket
显式经历 `DISCONNECTED`、`HANDSHAKING`、`READY`；普通请求在 `READY` 前不能使用它。
唯一的 handshake owner 必须按顺序完成：

1. `initialize` request；
2. 必需的 `initialized` notification；
3. `configRequirements/read` response envelope 与 optional
   `allowedApprovalsReviewers=user` 准入。

第三步不是完整 managed-availability validator。Focus 不把
`allowedApprovalPolicies`、`allowedSandboxModes` 或
`allowedPermissionProfiles` 与自己的静态目录做全集比较；上述三个非 reviewer 字段中的部分或
空限制不能让整个 connection 失效。具体值由 upstream 在对应 lifecycle/settings effect 上处理。
response 没有可供后续 effect 使用的 config revision、invalidation 或 atomic receipt，因此这次
握手 snapshot 也不能成为跨时间、跨cwd或跨frontend的硬预过滤 authority。

只有这些精确的 request/notification method 对 handshake owner 窄放行。requirements
准入完成前的小窗口里，notification 按序缓存，硬上限为 128；此时收到 server request、
畸形 ingress 或缓存溢出都会 fail-closed 结束握手。缓存 notification 只有在同一 websocket
generation 进入 `READY` 后才会释放。

普通 request 与普通 server-request response 在进入 transport 前取得 opaque outbound permit，成功返回后
必须确认仍是同一 permit。disconnect、connection supersession、cleanup / activation failure 与显式
backend reset 都会推进 outbound epoch；在此之后才返回的 response 是明确的 transport-unknown，不能作为
可写入本地状态的成功。reset 或 cleanup fence 未解除时，permit 必须在 RPC client 重连或发送前失败。

对 server-request response 而言，该 permit 本身不是 identity authority。canonical identity 必须携带收到
request 的正整数 generation，transport 必须 exact claim typed id / generation，不能做 generation-less
fallback。Web/飞书还必须匹配 exact `response_capability`，fcodex 必须匹配 exact `response_token`；这些
surface nonce 可阻止 Focus restart 复用 generation 后的 stale action。

handshake initialization 使用更窄的 connection capability。固定 generation 的 reset/cleanup read，
以及 fail-close response 使用有界、不得重连的 existing-backend capability；它们不取得
普通 request authority，也不重开 epoch。旧 generation 的延迟 callback 同样不能复活 connection-local
事实。这层 runtime gate 是 connection lifecycle 的权威，和上面的升级期 schema inventory 相关，但二者
不能互相替代。

## 有意保留的边界

此 guard 不声称证明所有未来 app-server 行为。它不能推断 lifecycle/root 语义，
也不能把未知上游 feature 变成 Focus 支持的 feature。adapter 的 outbound method
必须是可静态证明的 literal 或已审阅 helper 转发；无法证明的动态构造会直接失败。
仓库其他位置的源码 literal 扫描仍只是分类信号，不是完整调用图证明。guard 同样
不能替代 adapter/projection 的针对性测试。

尤其是，fcodex owner coordinator 仍是 thread-scoped 边界：不带具体 `threadId`
的 client RPC 没有 thread target 可供分类。运行中的 proxy 用窄化 default-deny
策略补上该边界，而不是假装一个全局请求属于某个既有 main-turn holder。guard 不会凭空推断未来
global method 是否语义安全；它让新增 method 显性化，并保证在审阅者同时修改显式策略和
proxy 实现前，该 method 仍会在本地被拒绝。
