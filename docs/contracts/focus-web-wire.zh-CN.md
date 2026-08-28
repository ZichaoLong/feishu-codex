# Focus Web wire 合同

文档角色：中文规范源。英文同步副本：`docs/contracts/focus-web-wire.md`。

本文定义 Focus Python 服务与仓库内浏览器前端之间的自有 wire 边界。它回答 endpoint、projection event 与
DTO vocabulary 由谁持有，以及异常输入何时可以进入 browser state；它不重新定义 Web writer、thread
lifecycle、pending request 或 attachment 的业务语义。

## 1. 场景与边界

浏览器不直接消费 Codex app-server DTO。Python owner 先把上游结果投影成 Focus-owned HTTP response 或
projection event，浏览器在安装状态前对该投影做完整 runtime decode。

本合同覆盖：

- Gateway 的具名 `/api/` endpoint method、path 与 production handler；
- browser projection event type 及其是否 thread-scoped；
- Focus-owned DTO 的顶层 required field 与封闭 string enum；
- Python producer、生成的 TypeScript vocabulary、HTTP/event decoder 之间的一致性。

本合同不覆盖 Codex app-server schema。上游 method/DTO 漂移继续由
[`codex-app-server-schema-drift.zh-CN.md`](./codex-app-server-schema-drift.zh-CN.md) 管理。工具卡、turn block 等
嵌套展示内容的类型检查与跨字段 invariant 继续由 browser decoder 持有，不扩展为通用 schema runtime。

## 2. 单一事实源与 owner

| 事实 | 唯一 owner |
| --- | --- |
| wire version、endpoint、event、required field、封闭 enum | `bot/focus_web_wire_catalog.py` |
| TypeScript 只读投影 | `web/src/focus/focusWire.generated.ts`，只能由 `scripts/generate_focus_web_wire.py` 生成 |
| HTTP DTO 生产 | 对应 Gateway/application owner；通用 projection helper 在 `bot/web_runtime/projection.py` |
| ordinary existing-thread prompt result receipt | `WebPromptResultRegistry`；行为链接 [Focus Web prompt mutation 恢复合同](./focus-web-prompt-mutation-recovery.zh-CN.md) |
| next-turn settings DTO 与 mutation transaction | `WebNextTurnSettingsCoordinator`，durable fact 由 `WebNextTurnSettingsStore` 持有；行为链接 [runtime settings 事实源合同](./runtime-settings-fact-sources.zh-CN.md) |
| event revision 与 fan-out | `FocusWebProjection` |
| 不可信 HTTP response admission | `web/src/focus/httpResponseDecoder.ts` |
| 不可信 event admission 与 nested decode | `web/src/focus/projectionEventDecoder.ts` |
| 完整 snapshot 的 staging 与原子安装 | `web/src/focus/focusProjectionSync.ts` |
| browser next-turn settings snapshot 安装 | `web/src/focus/client-state/web-next-turn-settings.ts` 的 `WebNextTurnSettingsOwner` |
| browser-local full-turn window preference | `web/src/focus/client-state/browser-turn-window.ts` 的 `BrowserTurnWindowOwner` |
| browser document title presentation | `web/src/focus/documentTitle.ts` 的 `syncFocusDocumentTitle` |
| browser document activity favicon presentation | `web/src/focus/documentActivityFavicon.ts` 的 `syncFocusDocumentActivityFavicon` |
| app-server typed runtime notice 投影 | `bot/web_runtime/runtime_notice.py` 的 `project_runtime_notice`；有序发布由 `WebRuntimeEventCoordinator` 持有 |
| browser 当前 document 的有界 runtime notice presentation | `web/src/focus/client-state/runtime-notices.ts` 的 `RuntimeNoticeOwner` |

generated 文件不是第二份事实源，禁止手改。TypeScript interface 仍描述字段类型，但 CI 必须逐 interface 证明其
required field 与 catalog 一致；decoder 必须消费 generated guard，不能再维护平行 key/enum inventory。

## 3. Version 与兼容

- 当前 wire version 是 catalog 中的整数版本。改变既有字段、enum 或 event 的含义，或做不兼容的 vocabulary
  修改时，必须显式审阅是否提升版本。
- v2 把 Web steer 改为不兼容的 strict `reserve → execute`，并新增 canonical `steer_attempt_result`；v1 不保留
  compatibility decoder或alias。
- v3 新增 thread snapshot 必填的 `active_turn_context` 及其封闭 initiator/provenance 词汇；
  不保留 v2 compatibility decoder 或 optional-field 路径。
- v4 新增 document registration 必填的 `intent_generation_floor`；不保留 v3 compatibility decoder 或
  optional-field 路径。
- v5 把 `writer_profile` 收窄为 navigation-only 的 selected thread、draft working directory 与
  `scope_generation`，删除其中四项 setting 字段、`profile_applies_to` 与 `settings_scope`。同一版本新增独立的
  `FocusNextTurnSettings` / result、meta 的 `next_turn_settings`、`GET/POST /api/settings/next-turn` 与
  `settings_changed` invalidation。v4 per-client setting 不保留 compatibility decoder、projection 或 fallback。
- v6 为既有 thread-turn page 增加封闭的 `items_view=summary|full`、稳定的 `page_cursor` 与最小
  `FocusSummaryPrompt`。同版后续增加 optional exact `turn_limit=5|10|20`；省略仍保持原有 10-turn 行为，
  因此不改变 required DTO 或封闭 response/event vocabulary。不保留由完整历史页反向推导目录、或把历史页并入
  live read model 的旧路径。
- v7 新增 paginated-only 的 terminal tool detail 与“搜索对话” endpoint、封闭的
  `thread_tool_kind=commandExecution|fileChange`、`FocusToolInspectionLocator`、
  `FocusThreadToolDetail` 与 `FocusThreadConversationSearchPage` records，并把封闭的
  `history_mode=legacy|paginated|unknown` 加入 thread summary。legacy/unknown thread 不保留完整 history
  fallback，browser 也不保留 raw upstream item 或累积式 search/detail compatibility path。
- v8 新增非 thread-scoped 的 `runtime_notice` event；其 `error` / `warning` detail 是封闭的 typed
  variant。v7 browser 不保留 compatibility decoder；服务与静态资源仍必须同版本部署。
- v9 把 existing-thread prompt 首请求改为 strict server-routed `phase=dispatch`：browser 不再发送
  `submission_intent` 或 `expected_turn_id`；只有 server 返回 `mode=reserved` 后，后续 strict `phase=execute`
  才消费 exact steer reservation。v8 request body、browser-owned route 与 active-but-ID-missing 零请求路径均不保留
  compatibility decoder 或 alias。
- v10 为 `FocusOperatorWarning` 新增必填的封闭 `attention=advisory|correctness`，使 presentation 可以把仅供
  诊断的运行时拥塞证据与需要用户关注的正确性告警分开。v9 browser 不保留 compatibility decoder；服务与静态资源
  仍必须同版本部署。
- v11 把 ordinary existing-thread prompt 改为单 POST staged transaction，并新增只读
  `GET /api/threads/{thread_id}/prompt-result/{mutation_id}`、封闭的 `FocusPromptResultReceipt` 与
  `prompt_result_status` / `prompt_result_mode`。同一 transaction 删除 v9 的 phase、reserve/execute、recovery bearer、
  steer-attempt result 与 snapshot mutation generation；v10 browser 不保留 compatibility decoder 或 alias。
- v12 把 terminal tool-detail 改为每次只读取一个上游分页 scan page，新增 opaque cursor 的 scan status/page DTO，
  删除 Focus 自有的总页数/item 扫描上限。浏览器负责 cursor 继续与取消；扫描进度和完整 `not_found` 与页面展示预算
  耗尽分离。v11 browser 不保留 compatibility decoder 或 alias。
- v13 把 terminal-tool-detail query/response 改为显式封闭的 `tool_detail_view=preview|full`。`preview` 保留有界
  semantic ToolCall；用户显式请求的 `full` 返回已验证 `commandExecution` / `fileChange` source-detail union，且不再
  应用 Focus 详情字符上限或 browser 25+25 行窗口。旧 `FocusThreadToolDetail` / `tool` scan-page vocabulary、无
  `view` query、compatibility decoder 与 alias 一律删除；服务与静态资源必须同版本部署。
- v14 为 `FocusMeta` 新增必填 `web_display_name`，使 browser 使用配置的部署显示名称维护 document title。
  v13 browser 不保留缺字段 compatibility decoder；服务与静态资源仍必须同版本部署。
- Focus 服务与其静态浏览器资源按同一仓库版本部署。内部兼容 shim、第二套旧 decoder 或 legacy alias 不是默认目标；
  改合同时同步更新 producer、catalog、generated projection、decoder、测试与本文。
- 如果未来允许前后端独立部署或滚动版本共存，必须先建立新的 negotiation/deployment 合同；当前 version 字段本身
  不构成版本协商协议。

## 4. Endpoint 与 event admission

- 每个具名 API endpoint 必须在 catalog 中有唯一 name、method、path 与 handler。Gateway 注册与浏览器 request
  builder 都从该记录读取；不得在两端再抄一份 route 清单。
- `thread_prompt` 是 ordinary existing-thread prompt 的唯一 effect-bearing Web request。body 必须恰好携
  `{text, attachment_ids, mutation_id, source_scope_generation, source_attachment_scope,
  source_composer_scope_id}`；client/document 来自 authenticated request，server 派生
  `client_user_message_id=focus-web:<mutation_id>`。请求不得携 phase、browser-owned route/turn、runtime/backend
  generation、recovery bearer 或 continuation。admitted terminal outcome 以 HTTP 200 返回 exact
  `FocusPromptResultReceipt`；validation/admission failure 仍是 effect-free HTTP error。HTTP 5xx、transport loss、
  non-JSON 或 malformed success response 对 browser 都是 possibly-sent，不能自动 replay。
- `thread_prompt_result` 是纯查询
  `GET /api/threads/{thread_id}/prompt-result/{mutation_id}`。它无 body/bearer，只在 current authenticated document
  已 materialize exact thread 时读取 process-local receipt；F5/polling 不得借它 dispatch、reserve、execute、resolve、
  restore attachment 或取得任何 effect authority。receipt 留存期内，同 mutation id 的 duplicate POST 不能取得第二个
  effect slot；terminal eviction、confirmed backend retirement 或 service restart 后，server 已没有 seen-identity
  evidence，expired id 不是永久 idempotency key。官方 browser 在这些边界只 GET 或为新 gesture 生成新 id。
  miss/eviction/retirement/restart 不是 known-no-effect evidence。
- `thread_start` 只有在收到 exact HTTP 503 `turn_submission_unknown`，且 error details 恰好是
  `{thread_id, operation}`、其中 `thread_id` 为 non-empty、无首尾空白的 string 且 `operation="prompt"` 时，才可让
  browser 为已知 created thread 建立 first-prompt local record。其他 status、缺失/额外字段、非 string、空白包裹值或
  其他 operation 都不得驱动持久 browser state 或导航，仍走普通失败/possibly-sent 展示且绝不自动 replay。
- instance-wide Web next-turn settings 使用同一路径的两条独立 catalog 记录：
  `GET /api/settings/next-turn` 读取完整 snapshot，`POST` 只提交要修改的 setting 字段并返回 owner commit 后的完整
  snapshot。请求不携 expected generation，也不经过 writer-profile/navigation mutation。
- backend reset 为同一路径使用两条 catalog 记录：current-document
  `GET /api/backend-reset` 返回不预留 authority 的 impact snapshot；same-origin/CSRF 的同 path `POST` 只接受
  exact `{force, expected_connection_generation}`。body 不能选择 instance，也不能携 backend 地址或 token。
  Web reset preview 的 status 是封闭的 `available / force-only / unavailable`；只有前两者携 positive safe
  generation，`unavailable` 必须携 generation `0` 且不授予 execute authority。
- `GET /api/threads/{thread_id}` 与 `GET /api/threads/{thread_id}/turns` 的可选 `turn_limit` 只接受 exact
  `5 / 10 / 20`；缺失时为 `10`，空值、重复参数、带空白或其他整数一律 fail closed。后者是 summary outline 与 full
  detail 的唯一历史 endpoint；`full` 请求必须携非空 opaque `cursor`，只有 `summary` 可以省略 cursor。一个 browser
  preference generation 内 recent、summary 与 full 必须使用同一个 page width，保证 summary locator 可直接复用于
  同页 full 请求，而不建立 range/offset 语义。width 改变会废弃旧 locator/detail intent，并以新 width 重建。
- document-bound thread directory/open/history/tool-detail/conversation-search 请求必须使用 staged request boundary。
  Gateway 在 per-client lifecycle lock 内核验 exact request token，并经 service-ingress receipt 让 RuntimeLoop
  prepare 不可变 document/target、backend generation、read observation 与 projection coordinates；随后释放该 lock。
  app-server/store read 与 detached DTO projection 在 RuntimeLoop 外执行，只有 exact claim/settlement 与最终 O(1)
  recheck 回到 loop。无 document 的 directory read 也必须由同一 service-ingress barrier 覆盖。较新 document、
  notification、runtime revision/epoch 或 backend generation 使旧 response 以
  `stale_document_read / stale_thread_read / stale_thread_list` 拒绝，不能安装旧 DTO 或覆盖新 cache。
- 上述 cold open 可以在 staged read 中执行已准入的 `thread/resume`。known resume 必须先结算并提交 runtime interest，
  后续 stale read/DTO 409 只表示 response 不可安装，不是 resume known-no-effect 证据。权威 direct-target read 对
  `ThreadSpawn` child 的拒绝也只可在 exact-current document/backend settlement 清理当前 selection；迟到拒绝不能
  清理 replacement target。
- prepared service-ingress receipt 从 RuntimeLoop prepare 跨越 document-lock release、external worker 与最终 settlement
  保持 shutdown barrier。handler cancellation、executor admission failure 或其它 handoff failure 只可 abandon 尚未
  claim 的 exact receipt；claim 后的 transaction 必须自行 settle，且 shutdown 等待其退出。abandon/settle 只结算
  request lifecycle，不能证明 upstream effect outcome 或授权自动 replay。
- 需要 turn/task 或附件物化的 app-server live notification 先在 RuntimeLoop 内应用 cache mutation，并冻结 exact
  read observation/runtime epoch receipt；`project_turns`、图片 hash/copy、附件 URL/JSON 物化在 service-ingress
  background worker 执行。每个 thread 至多一笔在途 projection 和一个 latest-wins successor；settlement 只发布仍
  匹配原 observation/epoch 的结果，旧结果直接丢弃。worker 调度失败或没有 successor 可收敛的投影失败只发布轻量
  `thread_invalidated`，不能阻塞 notification。权威 `thread/deleted` 或 known Web delete success 的 attachment-scope 物理清理使用同一 shutdown
  barrier。上述 flight 不持久化、不自动 replay，也不取得 lifecycle authority。
- `GET /api/threads/{thread_id}/turns/{turn_id}/tool-items/{item_id}` 是 paginated thread 的只读 terminal
  tool-detail endpoint。query 必须恰好携一项 `view=preview|full`，可选零项或一项 canonical ASCII unsigned 32-bit
  `change_index`，以及可选的一项 exact opaque `cursor`；空值、重复项、前导零、符号、空白、未知 key 或超界值一律以
  `invalid_tool_detail_query` fail closed。请求必须通过当前
  registered Web document barrier，并
  point-prove 当前选中的 exact direct thread 与 current-generation existing connection；该只读请求本身不
  start/resume runtime。legacy/unknown history mode 明确 unavailable，且不得 fallback 到完整
  `thread/read` 或 rollout replay。
- `GET /api/threads/{thread_id}/conversation-search` 是同一 paginated/direct-target 边界内的只读“搜索对话”
  endpoint。query 必须恰好包含一项 `query`，可选一项 `cursor`，且禁止其他 key 或重复项；规范化 query 为
  1..256 个 Unicode code points，non-empty opaque cursor 最多 4,096 个字符且不得带首尾空白；违反该
  closed shape 时以 `invalid_conversation_search_query` fail closed。搜索与详情都不预留 writer、turn、
  runtime、lifecycle、goal 或 approval authority。
- Python 只能发布 catalog 内的 event type。未知 type 必须在 revision 增加和 fan-out 之前被拒绝。
- `settings_changed` 只表示 durable settings 可能已更新，且不是 thread-scoped。事件不得携带 settings 副本、generation
  或 mutation settlement；browser 只能据此重新读取 authoritative settings endpoint。durable commit 后 event 发布失败
  不能把已完成的设置修改反转成 HTTP failure。
- `runtime_notice` 是非 thread-scoped event，因为上游 `warning.threadId` 可缺失；只有 absent/null warning target
  可以投影为 global，出现但为空的上游 target 必须拒绝；`FocusWebProjection` 按既有 envelope 合同把该 global scope
  编码为 `thread_id=""`。`error` 必须携 non-empty thread/turn target，envelope 也携该 non-empty `thread_id`。detail
  只允许两种 exact shape：
  `error={method,message,additional_details,will_retry,turn_id}` 或
  `warning={method,message}`。每个 string 在 producer 与 browser decoder 两侧都不得超过 16 KiB UTF-8；畸形、
  不可编码或超限字段使整条 notice fail closed，不能截断或改写文本。Focus 只读取 typed discriminator 与字段，
  不从 `Reconnecting` 等英文内容推断 retry、严重性或 lifecycle。
- `runtime_notice` 只进入当前 browser document 的有界 presentation owner，并由统一“运行详情”读取：
  `will_retry=true` 的 retry 与普通 warning 不进入主聊天流；non-retry error 继续在主聊天流显著显示，同时也保留在
  详情中。connection/disconnected 是独立事实，只在 presentation join 时与 notice 同屏，不由 notice owner 持有。
  notice 不持久化，不取得 turn completion、writer、recovery 或 projection-reload authority；合法 notice 自身不得触发
  snapshot reload。既有 `error` notification 的 authoritative thread invalidation 仍先发生，再按同一有序 event stream
  发布 notice。
- 浏览器只接收 generated event vocabulary。未知或 malformed event 在 transport decode 边界 fail closed：不解释
  payload、不推进已安装 revision，并触发现有 authoritative projection reload 路径。
- thread-scoped event 必须携带有效 `thread_id`；event-specific detail 仍由具名 decoder 校验。
- generic lifecycle/control `mutation_reconciled` 必须携带 exact `mutation_id`、operation 与封闭的
  `effect_observed / user_discard / retry_opened` disposition；对应 control HTTP settlement 使用同一词汇。
  ordinary prompt 改用独立 result-receipt status，browser recovery 绝不从 lifecycle `already_reconciled` 推断是否恢复草稿。
- `prompt_result_receipt` 统一承载 single-POST prompt 与 GET-only result query。它必须包含 exact
  thread/mutation/server-derived client-message coordinates，以及封闭的 status/mode、`turn_id` 与 `reason_code`；
  empty optional coordinate 仍以 required empty string 表示，不能通过缺字段制造另一种 shape。

## 5. DTO admission 与能力值

- catalog required field 表示字段必须实际存在；`null` 是否允许、标量类型、nested shape 与跨字段关系由对应 decoder
  继续校验。
- `FocusMeta.web_display_name` 必须是 non-empty、无首尾空白的 string，并精确投影已准入的实例配置值。browser
  document title 以该部署名称开头；materialized active thread 存在时，后接 ` · ` 与
  `FocusThreadSummary.title`。该 thread title 已按 authoritative name、首条 prompt preview、无标题 fallback 的顺序
  解析。没有 active thread 时只显示部署名称。thread switch、rename 与 meta 安装会同步更新 title；Focus 不按字符数
  裁剪会话侧字符串，标签页可见宽度由 browser chrome 自然截断。换行和连续空白只在 document-title presentation
  中折叠为单个空格，不修改持久化 thread name 或 preview。
- browser document favicon 只消费当前选中 thread 的既有 `running` presentation 与 browser connection 状态，不创建
  新的 runtime fact 或 wire vocabulary。连接正常且当前 thread 正在工作时，它使用预生成帧低频旋转；无当前 thread
  或工作结束时恢复初始 Focus favicon；连接未建立或断开时，灰色静态图标覆盖可能过期的 working presentation。
  hidden document 降低帧率；状态切换或 owner teardown 必须清理 timer 并恢复确定的静态图标。该 effect 不发起网络
  请求，也不得驱动 Vue view rerender、thread lifecycle 或 admission 决策。
- backend-reset preview 与 result 是 exact 顶层 record：缺字段或多字段都不能通过 browser admission。result 只含
  `force` 与五项 count/warning 字段，不含 backend URL 或 identifier list；其中 `force` 必须与 request 一致。
  任意 5xx、transport loss、non-JSON response 或 malformed 2xx result 在当前 browser document 内都是 outcome
  unknown，不能授权自动 retry。
- catalog enum 是封闭词汇；TypeScript 类型与 runtime decoder 都必须消费 generated vocabulary。业务分支可以按某个
  已通过 admission 的值分流，但不得另建平行 enum set。
- 当前上下文 meter 只消费已准入 `FocusTokenUsage.last.totalTokens` 与
  `modelContextWindow`。`total.totalTokens` 是 thread 生命周期累计值，既不是当前 context，也不能冒充 billing；
  compact 后 `last` 下降时 meter 必须随之回落。remaining percent 使用 Codex `/status` 相同的 12,000-token
  baseline、clamp 与整数舍入；`last`、window 或 availability 缺失时显示 unavailable，不得 fallback 到 `total`。
  上游语义固定在[token usage 更新](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/protocol/src/protocol.rs#L2087-L2125)与
  [`/status` baseline](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/protocol/src/protocol.rs#L2226-L2271)。
- `FocusOperatorWarning` 的 `severity=warning|error` 与 `attention=advisory|correctness` 是两个独立的封闭维度。
  registry 新 family 默认 `attention=correctness`；同 family coalesce 时两个维度都只能升级，已有 `error` 不得被
  `warning` 降级，已有 `correctness` 不得被 `advisory` 降级。`runtime_queue_delay` 与 `runtime_task_slow` 明确是
  `attention=advisory`：它们证明观察到延迟，不证明 turn、writer、mutation 或外部 effect 已出现正确性故障，也不取得
  lifecycle authority。
- RuntimeLoop 在任务入队时只捕获诊断 snapshot，不改变 FIFO、threshold、timeout、call/cancel 或 stop 语义。
  `queue_depth_at_enqueue` 是新任务入队前已经等待的任务数；`active_task_at_enqueue` 与
  `active_task_age_seconds_at_enqueue` 描述当时正在运行的任务及其已运行时长。queue-delay detail 使用
  `waiting_task`、上述三项 enqueue context、`queue_age_seconds` 与 `threshold_seconds`；slow-task detail 使用
  `running_task`、同一 enqueue context、`queue_age_seconds`、`task_duration_seconds` 与 `threshold_seconds`。
- `FocusToolInspectionLocator` 是 exact
  `{turn_id, item_id, kind, change_index}` record；`kind` 只能是
  `commandExecution / fileChange`。terminal command 必须携 `change_index=null`，FileChange 必须携合法的
  non-negative change index。卡片 synthetic id 不是 source identity，不得用它反推多 change item 的 locator。
- `FocusThreadToolDetailScanPage` 必须恰好携
  `{runtime_epoch, revision, thread_id, turn_id, item_id, kind, change_index, status, cursor, next_cursor,
  scanned_items, view, detail}`。`view` 只能是 `preview / full` 并回显本次请求；`status` 只能是
  `scanning / found / not_found`；`cursor` 回显本次请求 cursor。`scanning` 必须携前进的 `next_cursor` 且
  `detail=null`，`found` 与 `not_found` 的 `next_cursor` 必须为 `null`，后者 `detail=null`。`scanned_items` 是
  本页上游 item 数，不是展示字符预算。
  - `found + preview` 的 `detail` 必须是 `FocusThreadToolDetailPreview={view, tool}`；其中 `view="preview"`，
    `tool` 是 exact locator 的 semantic ToolCall，顶层 locator 与 nested inspection locator 必须一致。
  - `found + full` 的 `detail` 必须是 `FocusThreadToolDetailFull={view, source}`；其中 `view="full"`，`source` 是
    exact terminal item 的封闭 typed union。`FocusCommandExecutionSourceDetail` 必须恰好携
    `{type,id,pluginId,scriptPath,command,cwd,processId,source,status,commandActions,aggregatedOutput,exitCode,durationMs}`。
    `FocusCommandExecutionSourceAction` 是 tagged union：`read` 携 `{type,command,name,path}`，`listFiles` 携
    `{type,command,path}`，`search` 携 `{type,command,query,path}`，`unknown` 只携 `{type,command}`。
    `FocusFileChangeSourceDetail` 必须恰好携 `{type,id,changes,status}`，且每个 `FocusFileChangeSourceChange` 必须恰好携 `{path,kind,diff}`。
    这些是固定 upstream published variants 的语义投影；browser decoder 继续验证 scalar/nullability、PatchChangeKind
    / CommandAction nested shape、terminal status 和顶层 exact locator，而不是接受任意 raw item 或未知 future variant。
- 每个 `FocusThreadSummary` 必须携封闭的 `history_mode=legacy|paginated|unknown`。`legacy` 与
  `paginated` 原样披露 upstream persisted fact；只有没有 upstream Thread DTO 证据的 Focus provisional/
  temporary summary 可投影 `unknown`。`history_search` 与 `tool_detail` capability 只说明当前 Web build
  是否向浏览器启用相应产品 surface，不证明某个 thread 可检查；per-thread admission 必须仍以 exact
  `history_mode` 为准。
- `FocusConversationSearchMatchRange` 是 exact `{start, end}`；两者必须是 snippet 内非空、递增的 UTF-16
  character boundaries。`FocusConversationSearchOccurrence` 必须恰好携
  `{turn_id, item_id, snippet, snippet_match_range, turn_cursor}`；
  `FocusThreadConversationSearchPage` 必须恰好携
  `{runtime_epoch, revision, thread_id, query, cursor, occurrences, next_cursor}`。`cursor` / `next_cursor` 只允许
  `null` 或 exact opaque string；页 response 必须回显规范化 query 与本次 request cursor，不能返回搜索全文或
  raw upstream occurrence。
- `summary` turn page 只投影每个 raw turn 的首个 user message，shape 必须精确为
  `{id, role, no, text, title_truncated}`。它不携 assistant/final-agent 正文、thinking、tool、attachment 或其他完整
  turn 内容。`text` 是展示就绪的有界标题：空白规范化后最多 160 个字符；只有确实省略了非空白内容时
  `title_truncated=true`，此时末字符为 `…`。attachments-only 或空可见文本的 user message 仍保留 locator，使用空
  `text` 与 `title_truncated=false`。
- 每个 summary/full turn page 的 `page_cursor` 原样投影上游该页的 `backwardsCursor`；没有上游 anchor 时使用空字符串。
  非空 `page_cursor` 是该页首 turn 的 inclusive opaque anchor，可作为后续 full 请求的 cursor；`older_turn_cursor` 则只
  定位下一页更旧 turns。Focus 不解析、合成、持久化或修复任一 cursor。浏览器从 head lazy 扫描 summary 时，只保留
  Prompt locator 与该页 `page_cursor`，不能把无 cursor 的 head 请求直接重放为 full，因为两次请求之间可能新增 turn。
- legacy rollout 的每次 `thread/turns/list` 仍可能在上游重放完整 rollout；这是当前上游成本边界，不通过 Focus durable
  history index/cache 或 cursor 状态机掩盖。上游拒绝失效 anchor 时，Focus 向浏览器报告失败，由用户显式刷新。
- tool-detail response 只允许 terminal `commandExecution` 或 `fileChange` 的 Focus typed detail，不能把 raw
  upstream item 透传到浏览器。每次请求只读取上游允许的一个 page（Focus 使用 page width 100），通过 opaque
  cursor 继续请求；`scanning` 表示仍有下一页，`next_cursor=null` 时才表示完整扫描后的 `not_found`。Focus 不对
  总页数或总 item 数设置另一层硬上限；浏览器可显示已扫描 item 数并由用户取消。exact turn/item 不存在、item status
  不属于 `completed / failed / declined`、类型或 `change_index` 不匹配、cursor 异常、未知 variant、known source
  field malformed 与超时都只让当前请求失败。HTTP cancel 只停止浏览器当前等待和后续请求，不承诺已经进入
  `to_thread` 的服务端同步 RPC 立即终止。
  `preview` 是 existing bounded semantic projection，继续应用单项 tool-output presentation boundary 与 1 MiB
  serialized-response ceiling。官方浏览器只会在同一 exact locator 的 preview 已找到后展示并发送 `full`；这是 browser
  interaction rule，而非 endpoint 可持久化证明的前置条件：endpoint 不保留 preview-history，仍独立准入一个
  `view=full` exact-item read。full fresh re-read exact item，绝不从 preview/cache 重建，且不应用
  `ToolOutputPresentationBudget`、Focus detail response character ceiling 或 browser line crop。command full 原样投影
  app-server 已持久化的 `aggregatedOutput`，不能恢复其约 1 MiB head/tail persistence boundary 已丢失的中段；FileChange
  full 原样投影 whole `changes[]`，`change_index` 只用于 initial focus。上游单个 FileChange item 在到达 Focus 前的
  传输体积、以及用户显式请求的 full source browser residency，都不在有界 preview 保证内。
- conversation-search response 只投影当前 query/cursor 对应的一页：最多 20 条 user/steer 或每 turn 最终
  assistant occurrence；每条 snippet 最多 1,024 个 Unicode code points，并携 UTF-16 character-boundary
  match range 与 opaque turn cursor。tool、diff、reasoning、plan、MCP 与 subagent 内容不进入搜索；序列化
  response 的 Focus 硬上限为 64 KiB。该 endpoint 不承诺完整全文索引，也不承担 Prompt outline。
- tool-output 字符预算与省略计数统一使用 JSON 解码后字符串的 Unicode code point，不使用 UTF-16 code unit 或编码
  字节数；line array 相邻元素之间概念上的 LF 计一个 code point。
- full snapshot/page 中每个 tool-card output 先应用单项展示边界：最多保留 65,536 个原始 code point，其中 head 16,384、
  tail 49,152；发生中段省略时，`outputTruncated=true`、`outputOmittedChars` 是精确原始省略字符数，
  `outputHeadLineCount` 是 Focus 自有 marker 在 `output` 中的可信 index。decoder 必须成组校验三字段和该 index 上的
  exact marker，不能搜索 marker-like 文本猜边界。structured diff 使用同一事实，并把可信 index 投影为
  `diff.omissionLineIndex`。
- 单项边界之外，一次 full snapshot/page 的全部 tool-card output 共享 262,144 个展示字符和 16 个非空 output 的
  request-local aggregate budget；进程内 live read cache 则对每个 raw turn 应用同一 aggregate budget。预算不足时保留
  tool/card/status，但使用唯一的全省略 shape：`output=[]`、`outputTruncated=true`、精确
  `outputOmittedChars`、`outputHeadLineCount=0`；structured diff 对应为 `lines=[]`、相同 `omittedChars` 与
  `omissionLineIndex=0`。zero index 只允许与空 output/lines 成对出现。该 presentation budget 不创建 durable owner，
  也不声称限制 user/assistant 正文、tool metadata、媒体字节、thread 数或整个进程内存。
- browser presentation 只消费已准入的 omission 坐标，不改变或重新发现 marker 协议。非空的单项裁剪 shape
  把 exact trusted marker 行本地化为中段省略提示，并如实说明仍保留有界 head/tail；empty/zero-index aggregate
  shape 则说明当前 16 项 output / 262,144 code-point 预算已省略全部正文，绝不再声称显示 head/tail。没有 matching
  admitted 坐标的 marker-like 工具正文仍只是普通 output，不能成为 omission authority。
- live tool-output delta 的 `delta` 是原始字符串 chunk；browser 必须按收到顺序直接拼接，不能自行插入换行或按 chunk
  猜 line boundary，并对结果继续应用同一单项和当前 page aggregate budget。已进入全省略 shape 的 output 继续保持为空，
  只按后续 raw chunk 长度推进精确 omission count。
- `thread_delta.turns` 中同一 raw turn 的 segments 是 producer 当前规范的因果顺序。browser 可按稳定 segment/item ID
  合并更早到达的 live assistant blocks，但必须安装该 raw turn 的 incoming segment 顺序，不能让旧 browser 插入位置
  把早期 stream 创建的 assistant segment 留在随后到达的 user prompt 前面。尚无 matching incoming ID 的同 raw-turn
  live segment 只可作为有界 trailing presentation 保留；其他 raw turns 及后到达 event 的全局顺序不变。
- Python producer 必须输出 catalog 所需字段。代表性 producer fixture 必须直接用 catalog 检查 required field 与
  enum value，不能在测试里再抄预期清单。
- document registration 的 `intent_generation_floor` 是非负安全整数。Gateway 必须在 exact-client lifecycle lock 内、
  完成可能的 document reissue 后，从 RuntimeLoop 的 `WebDocumentRegistry` 读取其保留的
  `latest_intent_generation`；缺失 document record 时返回 `0`。同一 incarnation 在 registration response 丢失后重试，
  必须得到同一保留 floor。browser 必须在任何初始恢复或导航 intent 前以 `max(local, floor)` 重基线其
  document-global intent clock；该字段不授予 writer authority，也不成为持久化事实。
- `FocusNextTurnSettings` 是完整 snapshot，必须携 positive `generation` 与 model、reasoning effort、approval policy、
  permissions profile 四字段。meta、settings GET 与 settings POST result 都安装同一 shape；POST 的 partial request 不是
  snapshot。`generation` 只在同一 `runtime_epoch` 内可比较：同 epoch 时较小 generation 忽略，较大 generation 安装，
  equal generation 只有完整内容一致时才保持 unchanged；same-generation/different-content 必须 fail closed 并触发
  authoritative refresh。runtime epoch 变化时必须丢弃旧 settings generation baseline，并以 authoritative composite reload 的完整
  snapshot 无条件替换，不能用旧 epoch 的较大 generation 拒绝新 epoch 的 startup seed。这里不建立
  expected-generation CAS、冲突 UI、历史或回滚。来自不同 epoch 的 direct settings GET/POST result 只能使当前
  composite projection 失效并请求 reload；只有 authoritative meta/composite 安装可以切换 settings epoch。
- `writer_profile` 只包含 `selected_thread_id`、`working_dir` 与 `scope_generation`。navigation intent generation、
  `scope_generation`、thread selection、F5/document incarnation 与 projection revision 都不能排序或结算
  `next_turn_settings`；反方向的 settings generation 也不能授权 navigation、attachment 或 writer effect。
- thread snapshot 的 upstream effective-settings/active-turn disclosure 若存在，仍是独立只读事实。它不覆盖
  `next_turn_settings`，而 Web next-turn snapshot、request/ACK 或 `settings_changed` 也不能回填当前 thread/turn 的
  unknown 字段。
- 一次性 response capability（例如 pending request 的 `connection_generation` 与 `response_capability`）必须保留
  签发 owner 给出的原值。projection 不得猜测、重建或签发替代 capability。
- ordinary existing-thread prompt 的 `FocusPromptResultReceipt` 必须恰好携
  `{thread_id, mutation_id, client_user_message_id, status, mode, turn_id, reason_code}`。
  `client_user_message_id` 必须是 server-derived `focus-web:<mutation_id>`；`status` 只允许
  `pending / succeeded / known_no_effect / outcome_unknown`，`mode` 只允许 `start / steer`。
  steer 的 `turn_id` 始终是 prepare 时冻结的 exact expected turn；validated `mode=start + status=succeeded` 必须保留
  upstream `turn/start` response 的 authoritative `turn.id`。`pending`、pre-effect 或 unknown 没有该证据时必须是
  空字符串；request/submission tracking id 不得伪装成 actual turn identity。`reason_code` 没有附加分类时同样是
  空字符串。`attachment_rollback_failed` 只表示 text effect 已 known-no-effect、但旧 attachment chips 不能安全
  复用；browser 必须保留 text-only 或以更保守的 UI settlement 移除旧 chips，并明确要求重新添加附件。其他 code
  只解释 exact request。
  matching transcript client id 可把 unknown 正面对账为 succeeded；缺少 matching id 不能推断 known-no-effect。
- prompt backend connection generation 是 server-private staged-effect pin，绝不进入 receipt、snapshot 或 event；它也
  不同于 pending server request 投影中的 `connection_generation`，后者属于该 request 的 one-shot response authority。
- thread snapshot 的 `active_turn_context` 是只读 disclosure：没有 active turn 时为 `null`；否则必须携 exact
  `turn_id`、由 matching exact lease 证明的 initiator kind/Feishu binding、当前 attached Feishu audience，以及
  每个 active-setting 字段自己的 provenance。`turn/started` 冻结当时的 connection-local thread base；具体值与
  known-null 输出 `inherited`，缺失证据输出空 value + `unknown`，后续 response/settings event 不回填 active
  snapshot。`active_reroute` 只接受 matching turn 的上游 model reroute fact。该 DTO 不授予 writer、steer、
  interrupt、approval 或 FIFO authority。
- canonical Feishu subscriber 集合只有在真实增删时才发布 exact-thread `thread_invalidated`，
  reason 为 `feishu_audience_changed`；duplicate subscribe 与 no-op unsubscribe 不发布。browser 随后重读快照。
- browser 为每个 thread 维护一条 process-local active-turn-disclosure revision floor。任何
  `owner_changed` 或 `thread_invalidated` 都推进对应 thread floor；turn start/completion、active turn id
  变化、`model/rerouted`、`thread/settings/updated`、archived/closed/deleted lifecycle，以及
  `thread/status/changed` 变为 non-active 的 thread delta 也同样推进。旧 context 立即隐藏，直到同一
  runtime epoch 的 response 覆盖该 thread floor。`backend_disconnected` 与 `projection_invalidated`
  则推进 process-local global disclosure floor，并跨 thread 隐藏当前 context。
- 若 snapshot response 仅因较新的同 thread event 而不能整体安装，只有它同时覆盖适用的 thread/global
  disclosure floor，且 response thread id、response active turn id、context turn id 与 browser 当前
  exact thread/turn 全部一致时，才可局部安装其非空 `active_turn_context`。该 exact-context merge
  可以替换旧 context，不只用于填充 `null`；不得安装或回滚 coordinates、turns、status、profile、
  mutation state 或任何其他 response 字段。上述 floor 与 event-triggered 有界 refresh 不建立 polling/
  retry loop，也不授予 writer、owner、lifecycle、settings、approval、FIFO 或 mutation authority。

## 6. 安装与失败语义

- HTTP response 只有完整 decode 成功后才可交给 view/projection owner；失败结果不得通过 TypeScript cast 安装。
- 历史 summary/full page 是 request-local 的有界展示数据，不得 merge 到 process-local live thread read model。浏览器
  只保留 live recent window、一个有界 Prompt outline 与至多一个 full-detail page；替换历史页不能让服务端 live cache
  或浏览器 full-turn DOM 单调累积。process-local live cache 的硬上限为 20 个 raw turns；它不按 browser document
  复制 cache，也不因多个 page width 或历史读取而累积。
- tool detail 与 conversation-search page 同样是 request-local、browser-ephemeral presentation。浏览器同时
  只保留一个 tool detail 与一页最多 20 条的搜索结果；另一 detail、新查询或翻页替换当前内容而不是追加。
  main timeline 和 `preview` 的 tool output/diff 继续只挂载 25 行 head + 25 行 tail。显式 `full` source 默认用一个
  scrollable、selectable 的完整 source-text view 呈现；仅 FileChange 可由用户切换为基于同一已准入
  `changes[].kind` 与 `changes[].diff` 的完整 diff presentation。该切换不发起新请求、不另存一份 source/cache，且两种
  presentation 都不得套用该行窗口或字符裁剪；同一时刻只挂载一种，切回 source text 必须卸载完整 diff DOM。新的
  full target 默认回到
  source text。关闭、active thread 或 runtime epoch 改变、document replacement 与 client dispose 必须清理相应
  request intent、presentation mode 和内容；这清除浏览器引用，不承诺物理 GC 时刻。搜索结果的 turn cursor 只可替换
  上述唯一 full-detail history window，不得创建第二份 turns cache。
- browser inspection owner 为 detail/search 各自公开一个封闭的 browser-local unavailable reason。按优先级，它从
  当前 document access、selected/snapshot identity、build capability 与已准入 `history_mode` 推导
  `document_unavailable`、`no_active_thread`、`thread_not_materialized`、exact `legacy_history`、
  `build_unsupported` 或 `unknown_history`。已分类的 request-local upstream method-not-found 增加
  `runtime_unsupported`；已分类的 selection/history 丢失分别映射回 `thread_not_materialized` / `unknown_history`。
  identity、capability、access 或 runtime epoch 改变时清除 request-local reason。只有 exact
  `history_mode=legacy` 才能使用 legacy/迁移说明；不猜版本、不探测轮询、不 start/resume、不 fallback transcript，
  也不新增 durable state。当前 document 可用且已选择 thread 时，搜索入口保持可见，以便展示该 reason。带 exact
  locator 的 terminal command/FileChange omission card 同样保留详情入口；只有 reason 为空时才发 RPC，否则在既有
  有界内容旁展示具体原因。
- 完整 reload 先 staging meta、thread list 与 active snapshot，再统一安装。任一部分失败时保留 fail-closed 状态并走
  现有 retry/error 路径，不安装半份新 projection。
- staged read 的 typed stale response 不安装任何 response DTO。若该 read 内部已经完成 known
  `thread/resume` local commit，browser 只重新读取新权威 snapshot，不能恢复旧 selection、自动 replay resume 或
  把 stale response 改写成 effect-free failure。
- malformed HTTP response 对用户表现为该请求失败或当前 projection 继续失效；malformed/unknown event 对用户表现为
  projection reload。两者都不得修改业务 authority，也不得创建另一套 recovery state machine。
- catalog 只约束 wire admission。Web writer、main-turn owner、request lifecycle 与 mutation unknown 的事实仍归各自
  正式合同和 domain owner。

## 7. 变更与验证纪律

修改本边界时按一个方向执行：

1. 修改 Python catalog 与真实 producer；
2. 重新生成 TypeScript projection，不手改 generated 文件；
3. 更新 decoder 中的类型、nested invariant 或 canonicalization；
4. 删除被替代的 route/event/key/enum 清单；
5. 更新双语合同与 representative producer/decoder regression。

普通 CI 至少必须证明 generated freshness、Gateway handler 可定位、browser API/event inventory 无平行副本、catalog
required field 与 TypeScript interface 一致、每个 record/enum 被 production decoder 消费，以及代表性 Python DTO
满足 catalog。存在 Node 工具链时还必须运行 frontend test、typecheck、style、provenance、notices 与 build；缺少工具链
只能记为环境未验证，不能记为通过。
