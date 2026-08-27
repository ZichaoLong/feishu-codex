# Focus Web Wire Contract

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/focus-web-wire.zh-CN.md`.

This document defines the Focus-owned wire boundary between the Python service and
the in-repository browser frontend. It identifies the owners of endpoints,
projection events, and DTO vocabulary, and defines when untrusted input may enter
browser state. It does not redefine the product semantics of the Web writer, thread
lifecycle, pending requests, or attachments.

## 1. Scenario and Boundary

The browser does not consume Codex app-server DTOs directly. A Python owner first
projects an upstream result into a Focus-owned HTTP response or projection event.
The browser performs a complete runtime decode before installing that projection.

This contract covers:

- the method, path, and production handler of each named Gateway `/api/` endpoint;
- browser projection event types and whether each event is thread-scoped;
- top-level required fields and closed string enums of Focus-owned DTOs;
- consistency among Python producers, generated TypeScript vocabulary, and HTTP/event
  decoders.

This contract does not cover the Codex app-server schema. Upstream method and DTO
drift remains governed by
[`codex-app-server-schema-drift.md`](./codex-app-server-schema-drift.md). Nested
presentation content such as tool cards and turn blocks, plus cross-field invariants,
remain owned by browser decoders rather than a general-purpose schema runtime.

## 2. Single Sources and Owners

| Fact | Sole owner |
| --- | --- |
| Wire version, endpoints, events, required fields, and closed enums | `bot/focus_web_wire_catalog.py` |
| Read-only TypeScript projection | `web/src/focus/focusWire.generated.ts`, generated only by `scripts/generate_focus_web_wire.py` |
| HTTP DTO production | The relevant Gateway/application owner; shared projection helpers live in `bot/web_runtime/projection.py` |
| Ordinary existing-thread prompt result receipt | `WebPromptResultRegistry`; behavior links to the [Focus Web prompt mutation recovery contract](./focus-web-prompt-mutation-recovery.md) |
| Next-turn-settings DTO and mutation transaction | `WebNextTurnSettingsCoordinator`, backed by the durable fact in `WebNextTurnSettingsStore`; behavior links to the [runtime-settings fact-source contract](./runtime-settings-fact-sources.md) |
| Event revision and fan-out | `FocusWebProjection` |
| Untrusted HTTP response admission | `web/src/focus/httpResponseDecoder.ts` |
| Untrusted event admission and nested decoding | `web/src/focus/projectionEventDecoder.ts` |
| Complete-snapshot staging and atomic installation | `web/src/focus/focusProjectionSync.ts` |
| Browser next-turn-settings snapshot installation | `WebNextTurnSettingsOwner` in `web/src/focus/client-state/web-next-turn-settings.ts` |
| Browser-local full-turn window preference | `BrowserTurnWindowOwner` in `web/src/focus/client-state/browser-turn-window.ts` |
| Browser document-title presentation | `syncFocusDocumentTitle` in `web/src/focus/documentTitle.ts` |
| Typed app-server runtime-notice projection | `project_runtime_notice` in `bot/web_runtime/runtime_notice.py`; ordered publication remains with `WebRuntimeEventCoordinator` |
| Bounded runtime-notice presentation for the current browser document | `RuntimeNoticeOwner` in `web/src/focus/client-state/runtime-notices.ts` |

The generated file is not a second source of truth and must not be edited manually.
TypeScript interfaces still describe field types, but CI must prove their required
fields against the catalog interface by interface. Decoders must consume generated
guards and may not retain parallel key or enum inventories.

## 3. Version and Compatibility

- The current wire version is the integer in the catalog. A change to the meaning of
  an existing field, enum, or event, or any incompatible vocabulary change, requires
  an explicit version-bump review.
- Version 2 makes Web steer an incompatible strict `reserve → execute` flow and adds
  the canonical `steer_attempt_result`. No v1 compatibility decoder or alias remains.
- Version 3 adds the required `active_turn_context` thread-snapshot field and its
  closed initiator/provenance vocabulary. No v2 compatibility decoder or optional
  field path remains.
- Version 4 adds the required document-registration `intent_generation_floor`. No
  v3 compatibility decoder or optional-field path remains.
- Version 5 narrows `writer_profile` to navigation-only selected thread, draft
  working directory, and `scope_generation`, removing its four setting fields,
  `profile_applies_to`, and `settings_scope`. The same version adds independent
  `FocusNextTurnSettings` and result records, meta `next_turn_settings`,
  `GET/POST /api/settings/next-turn`, and the `settings_changed` invalidation.
  No compatibility decoder, projection, or fallback preserves v4 per-client
  settings.
- Version 6 adds the closed `items_view=summary|full`, a stable `page_cursor`,
  and the minimal `FocusSummaryPrompt` to the existing thread-turn page. A later
  same-version addition accepts optional exact `turn_limit=5|10|20`; omission
  preserves the former 10-turn behavior, so required DTO and closed
  response/event vocabularies do not change. Version 6 removes the old paths
  that derived an outline from full history or merged history pages into the
  live read model.
- Version 7 adds the paginated-only terminal-tool-detail and “conversation
  search” endpoints, the closed
  `thread_tool_kind=commandExecution|fileChange`, and the
  `FocusToolInspectionLocator`, `FocusThreadToolDetail`, and
  `FocusThreadConversationSearchPage` records. It also adds the closed
  `history_mode=legacy|paginated|unknown` to thread summaries. Legacy/unknown
  threads retain no full-history fallback, and the browser retains no raw
  upstream item or accumulating search/detail compatibility path.
- Version 8 adds the non-thread-scoped `runtime_notice` event with closed typed
  `error` / `warning` detail variants. A v7 browser has no compatibility
  decoder; the service and its static assets must still be deployed at the
  same version.
- Version 9 changes the first existing-thread prompt request to strict,
  server-routed `phase=dispatch`. The browser no longer sends
  `submission_intent` or `expected_turn_id`; only a server
  `mode=reserved` response lets the later strict `phase=execute` consume an
  exact steer reservation. Version 8 request bodies, browser-owned routing,
  and the active-but-id-missing zero-request path retain no compatibility
  decoder or alias.
- Version 10 adds the required closed `attention=advisory|correctness` field to
  `FocusOperatorWarning`, allowing presentation to separate diagnostic runtime
  congestion evidence from correctness warnings that require user attention. A
  version 9 browser retains no compatibility decoder; service and static assets
  still deploy at the same version.
- Version 11 changes an ordinary existing-thread prompt into one staged POST and
  adds read-only `GET /api/threads/{thread_id}/prompt-result/{mutation_id}`, the
  closed `FocusPromptResultReceipt`, and `prompt_result_status` /
  `prompt_result_mode`. The same transaction removes the version 9 phases,
  reserve/execute, recovery bearer, steer-attempt result, and snapshot mutation
  generation. A version 10 browser retains no compatibility decoder or alias.
- Version 12 changes terminal-tool-detail reads into one upstream paginated scan
  page per request, adds the opaque-cursor scan status/page DTO, and removes the
  Focus-owned total page/item scan ceiling. The browser owns cursor continuation
  and cancellation; scan progress and complete `not_found` are distinct from
  presentation-budget exhaustion. A version 11 browser retains no compatibility
  decoder or alias.
- Version 13 changes terminal-tool-detail query/response to the explicit closed
  `tool_detail_view=preview|full`. `preview` retains a bounded semantic ToolCall;
  an explicitly user-requested `full` returns a validated commandExecution /
  fileChange source-detail union and no longer applies the Focus detail character
  ceiling or browser 25+25-line window. The old `FocusThreadToolDetail` / scan-page
  `tool` vocabulary, no-`view` query, compatibility decoder, and aliases are
  removed; service and static assets deploy at the same version.
- Version 14 adds required `web_display_name` to `FocusMeta`, allowing the
  browser to maintain its document title from the configured deployment display
  name. A version 13 browser retains no missing-field compatibility decoder;
  service and static assets still deploy at the same version.
- The Focus service and its static browser assets deploy from the same repository
  version. Internal compatibility shims, a second legacy decoder, and legacy aliases
  are not default goals. A contract change updates the producer, catalog, generated
  projection, decoder, tests, and this document together.
- If independent frontend/backend deployments or rolling version coexistence are
  supported later, a separate negotiation and deployment contract must come first.
  The current version field alone is not a version-negotiation protocol.

## 4. Endpoint and Event Admission

- Every named API endpoint has one catalog record containing a unique name, method,
  path, and handler. Gateway registration and browser request construction both read
  that record; neither side keeps another route catalog.
- `thread_prompt` is the only effect-bearing Web request for an ordinary
  existing-thread prompt. Its body contains exactly `{text, attachment_ids,
  mutation_id, source_scope_generation, source_attachment_scope,
  source_composer_scope_id}`. Client/document identity comes from the
  authenticated request, and the server derives
  `client_user_message_id=focus-web:<mutation_id>`. The request carries no phase,
  browser-owned route/turn, runtime/backend generation, recovery bearer, or
  continuation. An admitted terminal outcome returns the exact
  `FocusPromptResultReceipt` with HTTP 200; validation/admission failure remains
  an effect-free HTTP error. HTTP 5xx, transport loss, non-JSON, or malformed
  success is possibly-sent at the browser and cannot authorize automatic replay.
- `thread_prompt_result` is the pure query
  `GET /api/threads/{thread_id}/prompt-result/{mutation_id}`. It has no body or
  bearer and reads the process-local receipt only when the current authenticated
  document has materialized the exact thread. F5/polling cannot use it to
  dispatch, reserve, execute, resolve, restore an attachment, or acquire any
  effect authority. While a receipt is retained, a duplicate POST with the same
  mutation id cannot acquire a second effect slot. After terminal eviction,
  confirmed backend retirement, or service restart, the server has no
  seen-identity evidence and an expired id is not a permanent idempotency key.
  The official browser only GETs across these boundaries or creates a new id for
  a new gesture. A miss, eviction, retirement, or restart is not known-no-effect
  evidence.
- `thread_start` may create a first-prompt local record for a known created
  thread only from an exact HTTP 503 `turn_submission_unknown` whose error
  details contain exactly `{thread_id, operation}`, where `thread_id` is a
  non-empty string with no surrounding whitespace and `operation="prompt"`.
  Another status, missing or extra field, non-string or padded value, or another
  operation cannot drive durable browser state or navigation; it remains an
  ordinary failed/possibly-sent presentation and is never replayed automatically.
- Instance-wide Web next-turn settings have two independent catalog records on
  one path: `GET /api/settings/next-turn` reads a complete snapshot, while
  `POST` submits only the setting fields to change and returns the complete
  post-commit owner snapshot. The request carries no expected generation and
  does not pass through writer-profile/navigation mutation.
- Backend reset uses two catalog entries for one path: current-document
  `GET /api/backend-reset` returns a non-reserving impact snapshot, while the
  same-origin/CSRF `POST` accepts exactly `{force,
  expected_connection_generation}`. The body cannot select an instance or
  carry a backend address/token. A Web reset preview has the closed
  `available / force-only / unavailable` status; only the first two carry a
  positive safe generation, while `unavailable` carries generation `0` and
  grants no execute authority.
- The optional `turn_limit` on `GET /api/threads/{thread_id}` and
  `GET /api/threads/{thread_id}/turns` accepts exactly `5 / 10 / 20`; omission
  means `10`, while empty, repeated, whitespace-padded, or other integer values
  fail closed. The latter endpoint is the sole summary/full history path. `full`
  requires a non-empty opaque cursor; only `summary` may omit it. Recent,
  summary, and full use one page width within a browser preference generation,
  so its summary locator is reusable for that full page. A width change retires
  old locators/detail intent and rebuilds them at the new width.
- Document-bound thread-directory, open, history, tool-detail, and
  conversation-search requests use the staged request boundary. Under the
  per-client lifecycle lock, Gateway verifies the exact request token and uses a
  service-ingress receipt to have RuntimeLoop prepare immutable
  document/target, backend-generation, read-observation, and projection
  coordinates; it then releases that lock. App-server/store reads and detached
  DTO projection run outside RuntimeLoop. Only exact claim/settlement and the
  final O(1) recheck return to the loop. A documentless directory read remains
  covered by the same service-ingress barrier. A newer document, notification,
  runtime revision/epoch, or backend generation rejects the old response as
  `stale_document_read / stale_thread_read / stale_thread_list`; it cannot
  install the old DTO or overwrite a newer cache.
- The staged cold open above may perform an admitted `thread/resume`. A known
  resume settles and commits runtime interest first; a later stale read/DTO 409
  means only that the response is not installable and is not evidence that the
  resume had no effect. An authoritative direct-target rejection of a
  `ThreadSpawn` child may likewise clean the current selection only under an
  exact-current document/backend settlement. A late rejection cannot clean the
  replacement target.
- The prepared service-ingress receipt remains a shutdown barrier across
  RuntimeLoop prepare, document-lock release, the external worker, and final
  settlement. Handler cancellation, executor-admission failure, or another
  handoff failure may abandon only an exact receipt that has not been claimed.
  A claimed transaction settles itself, and shutdown waits for it to exit.
  Abandon/settle closes request lifecycle only; it proves no upstream effect
  outcome and grants no automatic replay authority.
- An app-server live notification that needs turn/task or attachment
  materialization first applies its cache mutation in RuntimeLoop and freezes an
  exact read-observation/runtime-epoch receipt. A service-ingress background
  worker performs `project_turns`, image hashing/copying, and attachment URL/JSON
  materialization. Each thread has at most one projection in flight and one
  latest-wins successor. Settlement publishes only a result still matching the
  original observation/epoch and drops stale work. Worker-admission failure, or
  projection failure with no successor to converge, emits only a lightweight
  `thread_invalidated` and cannot block the notification. Physical
  attachment-scope cleanup after authoritative `thread/deleted` or a known
  successful Web delete uses the same
  shutdown barrier. These flights are not durable, are not replayed, and gain no
  lifecycle authority.
- `GET /api/threads/{thread_id}/turns/{turn_id}/tool-items/{item_id}` is the
  read-only terminal-tool-detail endpoint for a paginated thread. Its query
  requires exactly one `view=preview|full`, with either no or one canonical ASCII
  unsigned 32-bit `change_index` and at most one exact opaque `cursor`. Empty,
  repeated, leading-zero, signed, whitespace-padded, unknown, or out-of-range
  values fail closed as
  `invalid_tool_detail_query`. The request
  passes through the current registered Web-document barrier and point-proves
  the exact selected direct thread plus a current-generation existing
  connection; this read does not itself start or resume a runtime. Legacy or
  unknown history mode is explicitly unavailable and never falls back to full
  `thread/read` or rollout replay.
- `GET /api/threads/{thread_id}/conversation-search` is the read-only
  “conversation search” endpoint inside the same paginated/direct-target
  boundary. Its query has exactly one `query`, at most one `cursor`, no other
  keys, and no duplicates. The normalized query contains 1..256 Unicode code
  points. A non-empty opaque cursor contains at most 4,096 characters and has
  no surrounding whitespace. A violation of this closed shape fails as
  `invalid_conversation_search_query`. Neither search nor detail reserves
  writer, turn, runtime, lifecycle, goal, or approval authority.
- Python may publish only catalogued event types. It rejects an unknown type before
  incrementing the revision or fanning out the event.
- `settings_changed` says only that durable settings may have changed and is not
  thread-scoped. It carries no settings copy, generation, or mutation
  settlement; the browser can only use it to re-read the authoritative settings
  endpoint. Failure to publish the event after the durable commit cannot turn
  the completed settings mutation into an HTTP failure.
- `runtime_notice` is non-thread-scoped because upstream `warning.threadId` is
  optional. Only an absent/null warning target projects as global; a present
  empty upstream target is rejected. Under the existing event-envelope
  contract, `FocusWebProjection` encodes that global scope as `thread_id=""`.
  An `error` requires concrete non-empty thread and turn targets, and its
  envelope carries that non-empty `thread_id`. Its detail has exactly one of
  two shapes:
  `error={method,message,additional_details,will_retry,turn_id}` or
  `warning={method,message}`. Each string is at most 16 KiB of valid UTF-8 at
  both producer and browser-decoder admission. A malformed, unencodable, or
  oversized field drops the whole notice; Focus neither truncates nor rewrites
  its text. Focus uses only the typed discriminator and fields and does not
  infer retry, severity, or lifecycle from English text such as `Reconnecting`.
- `runtime_notice` enters only a bounded presentation owner in the current
  browser document and is read by the unified Runtime Details presentation. A
  `will_retry=true` retry and an ordinary warning do not enter the primary
  conversation flow. A non-retry error remains prominent in the primary flow
  and is also retained in Runtime Details. Connection/disconnected state is an
  independent fact that merely joins notices in presentation and is not owned by
  the notice owner. A notice is not persisted and owns no turn-completion,
  writer, recovery, or projection-reload fact; a valid notice alone must not
  reload the snapshot. Existing authoritative thread invalidation for an
  `error` notification still happens first, followed by its notice on the same
  ordered event stream.
- The browser accepts only the generated event vocabulary. An unknown or malformed
  event fails closed at transport decode: its payload is not interpreted, the
  installed revision does not advance, and the existing authoritative projection
  reload path runs.
- A thread-scoped event carries a valid `thread_id`; named decoders continue to
  validate event-specific details.
- Generic lifecycle/control `mutation_reconciled` carries an exact `mutation_id`,
  operation, and closed `effect_observed / user_discard / retry_opened`
  disposition. Its control HTTP settlement uses the same vocabulary. An ordinary
  prompt instead uses the separate result-receipt status, and browser recovery
  never infers draft restoration from lifecycle `already_reconciled`.
- `prompt_result_receipt` is the one result for the single-POST prompt and the
  GET-only result query. It contains exact thread/mutation/server-derived
  client-message coordinates, closed status/mode, `turn_id`, and `reason_code`.
  An empty optional coordinate is still represented by a required empty string;
  omitting a field cannot create another shape.

## 5. DTO Admission and Capability Values

- A catalog required field must be present. The corresponding decoder still owns
  nullability, scalar types, nested shapes, and cross-field relationships.
- `FocusMeta.web_display_name` is a nonempty string without surrounding
  whitespace and exactly projects the admitted instance configuration. The
  browser document title starts with that deployment name. When a materialized
  active thread exists, it appends ` · ` and `FocusThreadSummary.title`; that
  thread title already resolves authoritative name, first-prompt preview, and
  untitled fallback in that order. With no active thread, the title is only the
  deployment name. Thread switches, renames, and meta installation update the
  title. Focus applies no character-count truncation to the conversation side;
  browser chrome naturally clips the visible tab. Document-title presentation
  only collapses newlines and consecutive whitespace to one space and does not
  mutate the persisted thread name or preview.
- Backend-reset preview and result are exact top-level records: missing or extra
  fields fail browser admission. The result contains only `force`, five
  count/warning fields, and no backend URL or identifier lists. Its `force`
  must equal the request. Any 5xx, transport loss, non-JSON response, or
  malformed 2xx result is outcome-unknown in the current browser document and
  cannot authorize an automatic retry.
- Catalog enums are closed vocabularies. TypeScript types and runtime decoders consume
  the generated vocabulary. Business code may branch on an admitted value but may not
  create another parallel enum set.
- The current-context meter consumes only admitted
  `FocusTokenUsage.last.totalTokens` and `modelContextWindow`.
  `total.totalTokens` is cumulative across the thread lifetime; it is neither
  current context nor billing and cannot be used as a fallback. The meter follows
  a compaction decrease in `last`. Remaining percent uses the same 12,000-token
  baseline, clamping, and integer rounding as Codex `/status`; a missing `last`,
  window, or availability flag renders unavailable. The pinned upstream semantics
  are the [token-usage update](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/protocol/src/protocol.rs#L2087-L2125)
  and [`/status` baseline](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/protocol/src/protocol.rs#L2226-L2271).
- `FocusOperatorWarning` has two independent closed dimensions:
  `severity=warning|error` and `attention=advisory|correctness`. A new registry
  family defaults to `attention=correctness`. Coalescing may only strengthen
  either dimension: an existing `error` cannot be downgraded to `warning`, and
  an existing `correctness` cannot be downgraded to `advisory`.
  `runtime_queue_delay` and `runtime_task_slow` are explicitly
  `attention=advisory`: they prove observed latency, not a correctness failure
  in a turn, writer, mutation, or external effect, and they acquire no lifecycle
  authority.
- RuntimeLoop captures only a diagnostic snapshot when a task is enqueued; this
  changes no FIFO, threshold, timeout, call/cancel, or stop semantics.
  `queue_depth_at_enqueue` is the number of tasks already waiting before the new
  task enters the queue. `active_task_at_enqueue` and
  `active_task_age_seconds_at_enqueue` identify the then-running task and its
  elapsed runtime. Queue-delay detail uses `waiting_task`, those three enqueue
  context fields, `queue_age_seconds`, and `threshold_seconds`. Slow-task detail
  uses `running_task`, the same enqueue context, `queue_age_seconds`,
  `task_duration_seconds`, and `threshold_seconds`.
- `FocusToolInspectionLocator` is the exact
  `{turn_id, item_id, kind, change_index}` record, with `kind` restricted to
  `commandExecution / fileChange`. A terminal command carries
  `change_index=null`; a FileChange carries a valid non-negative change index.
  A card's synthetic id is not source identity and cannot be used to derive a
  multi-change locator.
- `FocusThreadToolDetailScanPage` carries exactly
  `{runtime_epoch, revision, thread_id, turn_id, item_id, kind, change_index,
  status, cursor, next_cursor, scanned_items, view, detail}`. `view` is only
  `preview / full` and echoes the request; `status` is only
  `scanning / found / not_found`; `cursor` echoes the request cursor.
  `scanning` carries a progressing `next_cursor` and `detail=null`, while
  `found` and `not_found` carry `next_cursor=null`, with `not_found` also
  carrying `detail=null`. `scanned_items` is the upstream item count in this
  page and is independent of the display-character budget.
  - A `found + preview` `detail` is
    `FocusThreadToolDetailPreview={view, tool}`: `view="preview"` and `tool`
    is the exact locator's semantic ToolCall, whose nested inspection locator
    agrees with the top-level locator.
  - A `found + full` `detail` is `FocusThreadToolDetailFull={view, source}`:
    `view="full"` and `source` is the exact terminal item's closed typed union.
    `FocusCommandExecutionSourceDetail` carries exactly
    `{type,id,pluginId,scriptPath,command,cwd,processId,source,status,commandActions,aggregatedOutput,exitCode,durationMs}`.
    `FocusCommandExecutionSourceAction` is a tagged union: `read` carries
    `{type,command,name,path}`, `listFiles` carries `{type,command,path}`,
    `search` carries `{type,command,query,path}`, and `unknown` carries
    `{type,command}`. `FocusFileChangeSourceDetail` carries exactly
    `{type,id,changes,status}`, and each
    `FocusFileChangeSourceChange` carries exactly `{path,kind,diff}`. These are
    semantic projections of fixed upstream-published variants. The browser
    decoder still validates scalar/nullability, nested PatchChangeKind /
    CommandAction shape, terminal status, and the exact top-level locator rather
    than admitting arbitrary raw items or an unknown future variant.
- Every `FocusThreadSummary` carries the closed
  `history_mode=legacy|paginated|unknown`. `legacy` and `paginated` disclose the
  upstream persisted fact unchanged. Only a Focus provisional/temporary
  summary without evidence from an upstream Thread DTO may project `unknown`.
  The `history_search` and `tool_detail` capability flags say only whether the
  current Web build enables the corresponding browser product surface; they do
  not prove that one thread is inspectable. Per-thread admission still uses its
  exact `history_mode`.
- `FocusConversationSearchMatchRange` is exactly `{start, end}`, with a
  non-empty increasing range on UTF-16 character boundaries inside the
  snippet. `FocusConversationSearchOccurrence` carries exactly
  `{turn_id, item_id, snippet, snippet_match_range, turn_cursor}`.
  `FocusThreadConversationSearchPage` carries exactly
  `{runtime_epoch, revision, thread_id, query, cursor, occurrences,
  next_cursor}`. `cursor` and `next_cursor` are either `null` or exact opaque
  strings. The page echoes the normalized query and request cursor and contains
  neither source text nor a raw upstream occurrence.
- A `summary` turn page projects only the first user message from each raw turn,
  with the exact shape `{id, role, no, text, title_truncated}`. It carries no
  assistant/final-agent body, thinking, tools, attachments, or other full-turn
  content. `text` is a display-ready bounded title: after whitespace normalization
  it contains at most 160 characters. `title_truncated=true` only when non-whitespace
  content was actually omitted, in which case the final character is `…`. An
  attachments-only user message or one with no visible text still retains its
  locator with empty `text` and `title_truncated=false`.
- Every summary or full turn page projects the upstream page `backwardsCursor`
  unchanged as `page_cursor`, or an empty string when upstream supplied no anchor.
  A non-empty `page_cursor` is the inclusive opaque anchor for the first turn in
  that page and may be reused as a later full-request cursor; `older_turn_cursor`
  locates only the next older page. Focus does not parse, synthesize, persist, or
  repair either cursor. During a lazy summary scan from head, the browser retains
  only Prompt locators and each page's `page_cursor`; it cannot replay the
  cursorless head request as full because a new turn may arrive between requests.
- On legacy rollouts, each upstream `thread/turns/list` may still replay the whole
  rollout. This is the current upstream cost boundary; Focus does not hide it with
  a durable history index/cache or cursor state machine. If upstream rejects a
  stale anchor, Focus reports the failure and requires an explicit user refresh.
- A tool-detail response admits only a Focus typed detail for a terminal
  `commandExecution` or `fileChange`; a raw upstream item never reaches the
  browser. Each request reads one upstream page (Focus uses page width 100),
  with continuation through the opaque cursor; `scanning` means another page
  remains, and only `next_cursor=null` yields complete `not_found`. Focus does
  not impose another total page or item ceiling; the browser can show its
  scanned-item count and let the user cancel. A missing exact turn/item, an
  item whose status is not `completed / failed / declined`, a type or
  `change_index` mismatch, an invalid cursor, unknown variant, malformed known
  source field, or timeout fails only that request. HTTP cancellation stops the
  browser's current wait and future requests; it does not promise immediate
  termination of a synchronous service RPC already inside `to_thread`.
  `preview` is the existing bounded semantic projection, retaining its
  per-output presentation boundary and 1 MiB serialized-response ceiling.
  The official browser exposes and sends `full` only after a found preview for
  the same exact locator. That is a browser interaction rule: the endpoint
  retains no preview-history state and independently admits a `view=full`
  exact-item read. A full read fresh-re-reads the item rather than
  reconstructing from preview/cache and does not apply
  `ToolOutputPresentationBudget`, a Focus detail-response character ceiling, or
  browser line crop. Command full detail projects app-server-persisted
  `aggregatedOutput` unchanged but cannot recover its roughly 1 MiB head/tail
  persistence-boundary middle; FileChange full detail projects the whole
  `changes[]`, with `change_index` used only for initial focus. One upstream
  FileChange item in transit before Focus receives it, and browser residency
  for explicitly requested full source, are outside the bounded-preview
  guarantee.
- A conversation-search response projects only the current query/cursor page:
  at most 20 user/steer or per-turn final-assistant occurrences. Each snippet
  contains at most 1,024 Unicode code points and carries a UTF-16
  character-boundary match range and an opaque turn cursor. Tools, diffs,
  reasoning, plans, MCP, and subagent content are excluded. Focus caps the
  serialized response at 64 KiB. This endpoint promises neither a complete
  full-text index nor the Prompt outline.
- Tool-output character budgets and omission counts use Unicode code points in
  decoded strings, not UTF-16 code units or encoded bytes. A conceptual LF
  between adjacent line-array entries counts as one code point.
- Each tool-card output in a full snapshot/page first receives a per-output
  presentation bound: at most 65,536 source code points remain, split into a
  16,384-character head and 49,152-character tail. On middle omission,
  `outputTruncated=true`, `outputOmittedChars` is the exact omitted source count,
  and `outputHeadLineCount` is the trusted index of the Focus-owned marker in
  `output`. A decoder validates the three fields together and the exact marker at
  that index; it never discovers the boundary by searching marker-like text. A
  structured diff carries the same fact as `diff.omissionLineIndex`.
- Beyond the per-output bound, all tool-card outputs in one full snapshot/page
  share a request-local aggregate budget of 262,144 presented characters and 16
  non-empty outputs. The process-local live read cache applies the same aggregate
  budget independently to each raw turn. When no aggregate capacity remains, the
  tool/card/status survives in the sole fully-omitted shape:
  `output=[]`, `outputTruncated=true`, exact `outputOmittedChars`, and
  `outputHeadLineCount=0`. A structured diff analogously carries `lines=[]`, the
  same `omittedChars`, and `omissionLineIndex=0`. A zero index is valid only with
  empty output/lines. This presentation budget creates no durable owner and does
  not claim to bound user/assistant text, tool metadata, media bytes, thread count,
  or total process memory.
- Browser presentation consumes the admitted omission coordinates without
  changing or rediscovering the marker protocol. A non-empty per-output shape
  localizes the exact trusted marker row as a middle-omission disclosure and
  truthfully states that a bounded head and tail remain. The empty/zero-index
  aggregate shape instead states that the current 16-output / 262,144-code-point
  budget omitted the entire body; it never claims that a head or tail is shown.
  Marker-like tool-authored text without matching admitted coordinates is
  ordinary output and never becomes omission authority.
- A live tool-output `delta` is a raw string chunk. The browser concatenates chunks in
  admitted order without inserting line breaks or inferring line boundaries, then
  reapplies the same per-output and current-page aggregate budgets. Once an output
  has the fully-omitted shape it remains empty and only advances its exact omission
  count by the length of later raw chunks.
- Segments for one raw turn in `thread_delta.turns` are the producer's current
  canonical causal order. The browser may merge earlier live assistant blocks by
  stable segment/item ID, but installs the incoming segment order for that raw turn;
  an assistant segment created by an early stream must not remain ahead of the later
  projected user prompt merely because of its old browser insertion position. A
  same-raw-turn live segment without a matching incoming ID may survive only as a
  bounded trailing presentation. Other raw turns and the global order of later
  events remain unchanged.
- Python producers emit all catalog-required fields. Representative producer fixtures
  check required fields and enum values directly against the catalog rather than
  copying expected lists into tests.
- A document registration's `intent_generation_floor` is a non-negative safe integer.
  Within the exact-client lifecycle lock and after any document reissue completes, the
  Gateway reads the retained `latest_intent_generation` from RuntimeLoop's
  `WebDocumentRegistry`; a missing document record yields `0`. A same-incarnation retry
  after registration-response loss receives that same retained floor. Before any
  initial restoration or navigation intent, the browser rebases its document-global
  intent clock with `max(local, floor)`. This field grants no writer authority and is
  not a durable fact.
- `FocusNextTurnSettings` is a complete snapshot with a positive `generation`
  plus model, reasoning effort, approval policy, and permissions profile. Meta,
  settings GET, and settings POST results install the same shape; a POST partial
  request is not a snapshot. `generation` is comparable only within one
  `runtime_epoch`. In the same epoch, the browser installs only a complete
  snapshot with `generation >= current`: a lower generation is ignored, a
  higher generation is installed, and an equal generation remains unchanged
  only when the complete content is identical. Same-generation/different-content
  fails closed and triggers an authoritative refresh. On a runtime-epoch
  change, it discards the old settings
  generation baseline and unconditionally replaces it with the complete
  snapshot from the authoritative composite reload. A larger old-epoch
  generation must not reject a new epoch's startup seed. There is no
  expected-generation CAS, conflict UI, history, or rollback. A direct settings
  GET/POST result from another epoch only invalidates the current composite and
  requests a reload; only authoritative meta/composite installation may switch
  the settings epoch.
- `writer_profile` contains only `selected_thread_id`, `working_dir`, and
  `scope_generation`. Navigation intent generation, `scope_generation`, thread
  selection, F5/document incarnation, and projection revision neither order nor
  settle `next_turn_settings`; settings generation in the other direction
  grants no navigation, attachment, or writer effect.
- Any upstream effective-settings or active-turn disclosure represented in a
  thread snapshot remains an independent read-only fact. It does not overwrite
  `next_turn_settings`, and a Web next-turn snapshot, request/ACK, or
  `settings_changed` event cannot backfill unknown current-thread/turn fields.
- One-shot response capabilities, including a pending request's
  `connection_generation` and `response_capability`, preserve the values issued by
  their owner. A projection must not guess, reconstruct, or issue a replacement
  capability.
- An ordinary existing-thread prompt's `FocusPromptResultReceipt` contains exactly
  `{thread_id, mutation_id, client_user_message_id, status, mode, turn_id,
  reason_code}`. `client_user_message_id` is the server-derived
  `focus-web:<mutation_id>`. `status` admits only `pending / succeeded /
  known_no_effect / outcome_unknown`, while `mode` admits only `start / steer`.
  For steer, `turn_id` is always the exact expected turn frozen at prepare. A
  validated `mode=start + status=succeeded` preserves the authoritative `turn.id`
  from the upstream `turn/start` response. It is an empty string when `pending`,
  pre-effect, or unknown lacks that evidence; a request/submission tracking id never
  masquerades as actual turn identity. `reason_code` is likewise empty without an
  additional classification. `attachment_rollback_failed` says only that the text
  effect is known-no-effect while old attachment chips are unsafe to reuse; the
  browser retains text-only or uses a more conservative UI settlement that removes
  those chips and explicitly requests reattachment. Other codes explain only the
  exact request. A matching
  transcript client id may positively reconcile unknown to succeeded; absence of a
  match cannot imply known-no-effect.
- The prompt backend-connection generation is a server-private staged-effect pin and
  never enters a receipt, snapshot, or event. It remains distinct from the
  `connection_generation` projected for a pending server request, which belongs to
  that request's one-shot response authority.
- A thread snapshot's `active_turn_context` is read-only disclosure. It is `null`
  without an active turn; otherwise it carries the exact `turn_id`, an initiator
  kind/Feishu binding proved by a matching exact lease, the currently attached
  Feishu audience, and provenance for each active-setting field. `turn/started`
  freezes the connection-local thread base at that moment: concrete values and
  known nulls are `inherited`, missing evidence is an empty value plus `unknown`,
  and later response/settings events do not backfill the active snapshot.
  `active_reroute` accepts only an upstream model reroute for that matching turn.
  The DTO grants no writer, steer, interrupt, approval, or FIFO authority.
- An actual add or removal in the canonical Feishu subscriber set publishes an
  exact-thread `thread_invalidated` with reason `feishu_audience_changed`; duplicate
  subscribe and no-op unsubscribe do not publish. The browser then reads a fresh
  snapshot.
- The browser maintains a process-local active-turn-disclosure revision floor for
  each thread. Every `owner_changed` or `thread_invalidated` event advances that
  thread's floor. So does a thread delta for turn start/completion or a changed active
  turn id, `model/rerouted`, `thread/settings/updated`, an archived/closed/deleted
  lifecycle, or `thread/status/changed` to a non-active status. The old context is
  hidden until a response from the same runtime epoch covers that thread floor.
  `backend_disconnected` and `projection_invalidated` instead advance a process-local
  global disclosure floor and hide the current context regardless of thread.
- A snapshot response which is otherwise behind a newer same-thread event may
  partially install its non-null `active_turn_context` only if it covers both the
  applicable thread and global disclosure floors and its response thread id,
  response active turn id, context turn id, and the browser's still-current exact
  thread/turn all match. This exact-context merge may replace an older context; it is
  not restricted to filling `null`. It must not install or roll back coordinates,
  turns, status, profile, mutation state, or any other response field. These floors
  and the event-triggered bounded refresh add no polling/retry loop and grant no
  writer, owner, lifecycle, settings, approval, FIFO, or mutation authority.

## 6. Installation and Failure Semantics

- An HTTP response reaches a view or projection owner only after a complete decode.
  A failed result cannot be installed through a TypeScript cast.
- A historical summary or full page is request-local bounded presentation data and
  is never merged into the process-local live thread read model. The browser retains
  only its recent live window, one bounded Prompt outline, and at most one full-detail
  page; replacing a history page must not make the server live cache or browser
  full-turn DOM grow monotonically. The process-local live cache has a hard cap of
  20 raw turns; it is not duplicated per browser document and never accumulates
  history pages or separate page widths.
- Tool detail and a conversation-search page are likewise request-local,
  browser-ephemeral presentation. The browser retains at most one tool detail
  and one page of at most 20 search results. Another detail, a new query, or a
  next page replaces instead of appending. Main-timeline and `preview` tool
  output/diff still mount only a 25-line head and 25-line tail. An explicit
  `full` source defaults to one scrollable, selectable complete source-text
  view. FileChange alone lets the user switch to a complete diff presentation
  derived from the same admitted `changes[].kind` and `changes[].diff`. That
  switch issues no new request, retains no second source/cache, and neither
  presentation may apply the line window or a character crop. Only one
  presentation is mounted at a time; switching back to source text unmounts
  the complete diff DOM, and a new
  full target defaults to source text. Close, active-thread or runtime-epoch
  change, document replacement, and client dispose clear the associated
  request intent, presentation mode, and content. This clears browser
  references, not a physical-GC time. A search result's turn cursor may only
  replace the sole full-detail history window; it creates no second turns cache.
- The browser inspection owner exposes one closed, browser-local unavailable
  reason for each detail/search surface. In precedence order it derives
  `document_unavailable`, `no_active_thread`, `thread_not_materialized`, exact
  `legacy_history`, `build_unsupported`, or `unknown_history` from current
  document access, selected/snapshot identity, build capability, and admitted
  `history_mode`. A classified request-local upstream method-not-found adds
  `runtime_unsupported`; classified selection/history loss maps back to
  `thread_not_materialized` / `unknown_history`. Identity, capability, access,
  or runtime-epoch change clears request-local reasons. Only exact
  `history_mode=legacy` may use the legacy/migration explanation; no version
  guess, probe, polling, start/resume, transcript fallback, or durable state is
  introduced. The search entry remains visible for a selected thread while the
  current document is usable so this reason can be presented. An omitted
  terminal command/FileChange card with an exact locator likewise keeps its
  detail entry; it dispatches the RPC only when the reason is absent and
  otherwise presents the reason beside the existing bounded content.
- A complete reload stages meta, thread lists, and the active snapshot before one
  installation. If any part fails, the projection stays fail-closed and follows the
  existing retry/error path; no partial new projection is installed.
- A typed stale response from a staged read installs no response DTO. If that
  read already completed a known `thread/resume` local commit, the browser only
  rereads a new authoritative snapshot; it cannot restore the old selection,
  replay resume automatically, or reinterpret the stale response as an
  effect-free failure.
- A malformed HTTP response appears to the user as a failed request or an invalidated
  current projection. A malformed or unknown event appears as a projection reload.
  Neither changes domain authority or creates a second recovery state machine.
- The catalog constrains wire admission only. Web writer, main-turn owner, request
  lifecycle, and mutation-unknown facts remain with their domain owners and formal
  contracts.

## 7. Change and Verification Discipline

Change this boundary in one direction:

1. update the Python catalog and the real producer;
2. regenerate the TypeScript projection without editing generated output;
3. update decoder types, nested invariants, or canonicalization;
4. delete the replaced route, event, key, or enum inventory;
5. update both contract languages and representative producer/decoder regressions.

Ordinary CI must at least prove generated freshness, Gateway handler resolution, the
absence of parallel browser API/event inventories, exact required-field agreement
between catalog records and TypeScript interfaces, production decoder consumption of
every record and enum, and catalog conformance of representative Python DTOs. When a
Node toolchain exists, frontend tests, typecheck, style, provenance, notices, and build
also run. A missing toolchain is recorded as unverified by the environment, never as a
pass.
