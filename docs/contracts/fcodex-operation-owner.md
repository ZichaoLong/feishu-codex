# fcodex Main-Turn and Proxy Boundary Contract

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/fcodex-operation-owner.zh-CN.md`.

> This document is the fcodex transport/proxy extension of the
> [main-turn owner contract](./root-operation-owner.md). Participant,
> connection, and process-local recovery facts are not main-turn writer
> states.

## Scope

`fcodex` connects a remote TUI to Focus's shared Codex app-server. It has
three transport responsibilities:

- classify RPCs that have no thread target;
- prove whether an RPC carrying `threadId` targets a directly operable root;
- preserve one main-turn initiator/writer across fcodex, Web, and Feishu while
  qualified trusted-local endpoints contribute same-turn input.

Socket lifetime, server requests, goal continuation, thread create, and
backend reset remain separate facts. This contract does not combine them into
main-turn ownership.

## Client Requests Without a Root Target

A request without a non-empty `params.threadId` cannot be tied to a thread
writer and is denied by default. The proxy permits only these reviewed
initialization/discovery reads:

- `initialize`
- `account/read`
- `config/read`
- `configRequirements/read`
- `model/list`
- `hooks/list`
- `skills/list`
- `account/rateLimits/read`
- `thread/list`
- `thread/loaded/list`
- `app/list`
- `app/installed`
- `experimentalFeature/list`
- `mcpServerStatus/list`

`initialized` is the only allowed connection-local client notification.
Unknown notifications are suppressed. Malformed frames, scalars, and JSON-RPC
batches cannot bypass per-method classification. `bot/fcodex/proxy.py` and
the app-server schema baseline jointly guard the exact allowlist.

`thread/start` is the controlled targetless create exception. It creates a
thread, not a main-turn writer. The returned root acquires a submission lease
only on a later lease-bearing exclusive/autonomous effect; ordinary
`turn/start` remains upstream-routed input without a writer. Unknown/retry and local commit behavior
for create is defined by
[thread-create-local-commit](./thread-create-local-commit.md).

## Response Envelope

Codex app-server `0.147.0` deliberately neither sends nor requires the
standard JSON-RPC `"jsonrpc": "2.0"` member. A successful response is
`{id, result}` and an error response is `{id, error}`; see the pinned upstream
definitions in
[`rpc.rs#L1-L2`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server-protocol/src/rpc.rs#L1-L2)
and
[`rpc.rs#L67-L79`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server-protocol/src/rpc.rs#L67-L79).

The proxy therefore applies one minimum envelope rule to backend client
responses and TUI server-request responses: `id` must retain exact type and
value correlation with the pending request; `method` must be absent; and
exactly one of `result` or a structured object `error` must be present. A
coordinated mutation result must additionally satisfy that method's
Focus-owned target, status, or other postcondition before an unknown effect can
settle as known success. Reviewer and settings fields on fcodex `thread/start`
and `thread/resume` are upstream-owned, not Focus-owned postconditions here. A
missing `jsonrpc` member is never by itself evidence for
quarantine, unknown settlement, or transport failure; if a peer includes it,
the field grants no authority. Malformed, hybrid, unmatched, or
postcondition-unproven responses continue to fail closed at the exact request
boundary.

## Direct Thread Targets

A thread-scoped RPC with a non-empty `threadId` enters service admission
before the proxy forwards it. Focus must prove that the exact target is a
direct root. A spawned subagent cannot become an independently writable root,
including when lineage cache evidence is missing.

The sole child-target transport exception is the strict metadata read needed
for upstream TUI navigation: `thread/read`, exact `threadId`,
`includeTurns=false`, and no other unreviewed parameter. It reads metadata
only and creates no owner, lease, subscription, interaction route, or mutation
authority.

Thread reads may be observer operations. Main-turn start/writer identity and
effect-specific active-turn control follow the sections below. Goal, resume,
settings, and lifecycle mutations keep their own
effect-specific admission and process-local unknown evidence; they cannot
extend or replace an existing Focus main-turn lease. This ownership statement
does not promise that upstream resume cannot start later autonomous goal work.

`thread/resume` must carry a non-empty, whitespace-exact direct `threadId`.
Upstream cold resume gives a non-null `history` and a non-empty `path`
precedence over `threadId`, so Focus rejects those two known alternate targets
locally. After that target guard, the proxy preserves the native TUI payload
semantically unchanged: model/provider/tier, cwd/workspace roots,
approval/reviewer, sandbox/permissions, config, instructions, personality, and
future upstream fields are not whitelisted, stripped, normalized, or
overridden by Focus. `thread/start` likewise preserves native TUI params; its
only payload supplement is the wrapper's resolved final cwd when `cwd` is
missing. These upstream-owned fields grant no Focus writer, settings owner, or
other effect authority.

Lifecycle RPCs initiated through Focus's canonical adapter retain that
adapter's own `approvalsReviewer=user` contract. It must not be projected back
onto native fcodex transport. For an already-loaded thread, upstream may ignore
a resume reviewer override and report the reviewer's existing backend value.
fcodex does not own that fact, so a reviewer mismatch is not evidence of a
malformed response, transport failure, or quarantine; the response's exact
thread identity must still match the admitted target.

When an exact active-turn lease already exists, a live fcodex endpoint may use
this admitted native resume as an observer attachment even if a persisted-goal
check says a cold resume could continue work. This does not acquire or transfer
the Focus writer, but it intentionally follows upstream running-resume
semantics rather than promising a pure subscription. Upstream attaches the
connection, returns the current thread response, replays pending requests, and
then invokes idle lifecycle when goal state is present; if the turn became idle
before that final check, the goal extension may continue it. Without an active
Focus turn, a resume which may autostart still requires the existing
blank-submission admission.

The fcodex websocket is also not a fact writer for the connection-local
effective-settings registry. After owner admission succeeds and before the
proxy can send a reviewed turn, settings, resume, continuation-risk goal, or
thread-lifecycle effect,
Focus marks that exact thread external-unknown and retires all four setting
fields. Canonical adapter notifications cannot replace that negative fact:
the two app-server connections expose no common revision or causal ordering
token, so a later-arriving notification may have been emitted before the
external effect. Requests, ACKs, responses, and notifications on the fcodex
socket never install values. The thread stays unknown until canonical backend
epoch replacement/reset clears all disposable facts. This is a per-thread
disclosure/native-media degradation, not a writer or service quarantine. A
targetless `thread/start` has no existing thread to retire.

## Main-Turn Owner

`FcodexMainTurnOwner` owns only the fcodex exclusive/autonomous submission and
active-turn lease projection. Ordinary `turn/start` is upstream-routed realtime
input, not writer admission:

- all three start methods require an exact direct root;
- ordinary `turn/start` requires only a live endpoint and exact root, then
  records the exact participant, connection, JSON-RPC request id, and request
  token. It neither reads, acquires, nor releases `InteractionLeaseStore` and
  does not pass through cross-surface writer denial. Success, error, unknown,
  and connection loss settle only that request;
- `review/start` and `thread/compact/start` still acquire a PID-bound exclusive
  blank tied to the participant and exact connection before transport;
- the inline `review/start` response id is the actual inline-review turn id and
  may activate directly; detached review stays rejected. The empty
  `thread/compact/start` response does not activate the lease and waits for
  lifecycle;
- when no fcodex blank already exists, neither an ordinary-start response nor
  lifecycle notification creates a lease or writer from that request;
- matching `turn/started` may still bind an existing fcodex
  exclusive/autonomous blank to the actual `turn_id`. Matching
  `turn/completed` releases only the exact active lease, while exact terminal
  notifications such as `thread/closed`, archive, or delete may clear it;
- `turn/steer` and ordinary `turn/interrupt` use the independent effect
  authorities below. Neither derives from nor transfers the writer relation.

Admission and request settlement for ordinary start leave the full record of
any existing Web, Feishu, or fcodex blank/active lease unchanged. Upstream
`turn/started`, however, carries no effect identity correlating the notification
to one concurrent RPC. If ordinary start races an existing fcodex
review/compact/goal/resume blank, later lifecycle may still activate that blank,
and Focus cannot prove which effect produced the actual turn. This is an
explicitly accepted narrow upstream race; Focus adds no recipient/turn-owner
state machine to eliminate it. A known exclusive rejection CAS-releases only
its captured generation, so a stale response cannot release an ABA replacement.
An unknown exclusive submission keeps waiting for method-specific or
lifecycle/terminal evidence and creates no cross-restart writer.

A goal receipt, participant identity, or same connection cannot authorize an
exclusive start or create a writer; ordinary `turn/start` has only the one-shot
upstream input authority above. Those facts also do not authorize steer or
ordinary interrupt by themselves. Each effect must satisfy the complete
live-endpoint, exact-direct-root, attached-source, and raw-target boundary
below. Neither effect creates a writer, and there is no fallback tree-stop
route.

## Participant and Connection Lifetime

`FcodexParticipantRuntimeRegistry` records proxy endpoints, request sources,
subscription sources, and backend generation. These are transport
liveness/routing facts, not main-turn writer states.

- an observer socket does not take over when the writer socket disconnects;
- an active-turn lease does not become `grace` or `orphaned` on socket
  disconnect;
- while the service remains alive, matching lifecycle settles the active turn;
- after service restart, PID-bound main-turn leases are pruned and the old
  connection writer is not reconstructed.

Any connected/grace/orphaned vocabulary used for Registry endpoint cleanup
describes only endpoints. It cannot be projected into cross-frontend
main-turn ownership.

## Interactions and Server Requests

In the current backend epoch, a canonical interactive callback with a non-empty
`turnId` for an exact direct root forms a trusted-local shared interaction
domain. This includes command-execution, file-change, and permission approvals,
user input, MCP elicitation, and dynamic tool calls. Every live fcodex endpoint
which has successfully materialized that exact root may receive the same
canonical request, including an observer attached after the turn started. Each
endpoint gets a different one-time response token. The first valid endpoint or
Web response wins; every other endpoint is retired by a typed local receipt or
upstream `serverRequest/resolved`. Approvals are also offered independently to
Feishu. Ordinary interactions are offered first to fcodex and Web; only two
authoritative desktop declines preserve the existing single-surface Feishu
fallback, and an unknown desktop outcome never triggers that fallback. Web may
decline a shape it cannot present, including dynamic tool calls. Authentication,
unsupported methods, empty-turn requests, and child-thread requests do not join
this domain and retain their existing writer/surface route.

Shared-interaction eligibility does not read `InteractionLeaseStore`. That store decides
who may start and preserves initiator/writer identity. When an initiating
`fcodex` endpoint has no connection source yet, its matching active-turn lease
may provide one exact steer or interrupt attachment proof. The lease is not the
shared effect authority and cannot disqualify a canonical callback still
pending in the current app-server epoch. An attached endpoint
may therefore answer a callback from ordinary active-goal continuation even
when that autonomous turn has no Focus writer. A proxy-first projection may be
shown early, but a user action before canonical identity binding receives
`not_sent` and is shown again rather than being retained for automatic submit.
For a non-approval canonical offer, fcodex claims only while at least one live
endpoint has a current connection source for the exact root; this prevents an
invisible fcodex projection from consuming the Feishu fallback.

Shared interaction is response authority for one exact request, not writer
handoff. It does not change the turn initiator, active model/effort/sandbox or
approval settings, output destination, goal, binding, backend generation, or
next-turn admission. The TUI's valid raw response object is forwarded unchanged.
For approvals this includes session approval, strict auto-review, and declared
exec/network-policy amendments. An invalid response retires only that exact
endpoint token; it does not fail-close or answer for another shared endpoint.

Upstream app-server keeps pending requests in process, replays them to a new
connection on resume, consumes the callback on the first response, and clears
thread requests on matching TurnComplete. Focus needs only backend generation,
request identity, and a one-shot action token so a stale UI action cannot
answer a replacement request.

Current request identity is the process-local
`ServerRequestRegistry` described by `server-request-lifecycle.md`; matching
turn/lifecycle completion removes only that local projection and cannot hold
the main-turn lease open. A proxy-first local record/capability only bridges
the real proxy/service arrival race and is not lifecycle or writer authority.
Only automatic fail-close may retain an exact intent after a proven pre-send
failure while waiting for an explicit upstream replay.

After fcodex has retained a canonical shared interaction, its inbox remains
present even when no endpoint is currently attached. Endpoint disconnect drops
only that endpoint token; while the central response authority remains open, a
later admitted native resume and upstream pending replay can issue a fresh token
in the same Focus/app-server epoch. Exact revocation is checked before token
issuance. There is no disk queue or cross-restart
recovery. A response arriving after local service resolution or after the
proxy received `serverRequest/resolved` is absorbed by a bounded, typed-id,
current-epoch receipt rather than quarantining the whole proxy socket.

If a user response is proven not sent, the proxy re-presents the exact request
and requires another explicit action. An automatic fail-close retains its
intent only across an explicit exact upstream replay. An unknown outcome
fences only that request; unrelated requests and the connection remain usable.

An exact group-deactivation revocation is checked centrally at response time.
If its automatic cancel was proven not sent, fcodex has no server-to-proxy push
which can immediately dismiss an already-rendered overlay. That overlay may
remain until the attempted action receives a typed superseded receipt or real
upstream resolution/lifecycle cleanup arrives; it has no response authority and
must not be presented as a resolved callback.

## Backend Reset

Backend generation is the necessary transport fact for reset: a response,
notification, timer, or action token from an old generation must not affect
the replacement backend. The minimal reset order is stop old backend,
invalidate its generation/local pending state, then start and verify the
replacement.

Backend reset is not writer handoff. It retires old process-local request and
effect facts only after the old backend is proved stopped; it does not migrate
them to the replacement generation. The fcodex owner retires only its client
request, direct-root routing, and interaction-inbox facts; it no longer scans or
releases an fcodex-only lease subset. `InteractionLeaseStore` centrally captures
all three surfaces' current-process full leases before stop and exact-CAS retires
them after confirmed stop, avoiding a surface-specific duplicate path. Any failed
retirement forbids starting the replacement.

## Shared Same-Turn Input

An fcodex `turn/steer` is an exact-turn contribution, not writer handoff. Any
live endpoint attached to the exact direct root may submit it, including a
late-attached endpoint, another fcodex participant, or an endpoint attached to
a Web-, Feishu-, or autonomous-goal-origin turn.

- Raw params must contain non-empty, whitespace-exact `threadId` and
  `expectedTurnId`, plus `input`. The stable optional
  `clientUserMessageId` and upstream `input` payload are preserved unchanged
  for upstream schema validation. No unknown key is admitted, and experimental
  `additionalContext` or `responsesapiClientMetadata` must be absent or null.
- The endpoint must be live in the current backend generation and have a
  successful connection runtime source for that exact root. For the initiating
  fcodex endpoint before such a source exists, only a lease matching its
  participant, connection, root, and exact expected turn may serve as attach
  proof.
- Focus forwards the pinned expected turn unchanged. Upstream atomically
  rejects a missing, terminal, stale, mismatched, review, or compact turn;
  Focus neither guesses a successor nor claims a global order among concurrent
  valid steers.
- Success, typed rejection, and native-TUI unknown outcome use ordinary
  tracked request settlement. Focus adds no durable steer ledger, exact
  deduplication, or automatic resend for fcodex.

Shared steer does not acquire, replace, or prolong the writer lease. It does
not change active model, effort, permissions, approval policy, output
destination, goal, binding, or next-turn admission, and it grants no settings,
lifecycle, server-request response, or other thread-mutation authority.

## Ordinary Stop

fcodex Ctrl+C has one product route: send ordinary `turn/interrupt` for the
exact direct-root active turn presented to that TUI. This is explicitly
separate from the shared steer effect:

- raw params contain exactly the `threadId + turnId` keys. `threadId` is a
  non-empty whitespace-exact string; `turnId` is a whitespace-exact string and
  may be explicitly empty. The raw payload is forwarded unchanged;
- the endpoint belongs to the current backend generation and remains live. A
  non-empty `turnId` accepts a current connection source. Before the initiating
  endpoint has that source, an active-turn lease matching its participant,
  connection, and exact turn may also serve only as attachment proof;
- an empty `turnId` accepts only that current connection's exact-root runtime
  source and uses the pinned upstream current/startup interrupt semantics. A
  blank or active lease cannot substitute for that connection attachment proof;
- a qualified late-attached observer, another `fcodex` participant, or an
  endpoint attached to a Web/Feishu-origin turn may interrupt without acquiring
  or changing the writer;
- a stale, terminal, or mismatched non-empty `turnId` from an attached endpoint
  reaches upstream's check unchanged and receives a typed rejection. Focus
  neither guesses the current id, fills an empty value from projection, nor
  retargets a successor turn;
- an unattached or disconnected endpoint, pending resume, backend replacement,
  ambiguous target, or child target is rejected explicitly.

Focus does not scan descendants, create a stop journal, or claim atomic tree
settlement. Interrupt grants no start/steer, settings, goal, binding, lifecycle,
or server-request response authority. A child requires its own independent
control contract.

## Relevant Code and Upstream Evidence

Primary implementation:

- `bot/fcodex/proxy.py`
- `bot/fcodex/operation_contract.py`
- `bot/fcodex/main_turn_owner.py`
- `bot/fcodex/operation_service.py`
- `bot/fcodex/participant_runtime_registry.py`
- `bot/stores/interaction_lease_store.py`

Main-turn lifecycle and running-resume/server-request behavior are pinned to
public upstream
[`openai/codex@be6e8eac029b183056b7e4402879f15d2c85f61b`](https://github.com/openai/codex/commit/be6e8eac029b183056b7e4402879f15d2c85f61b)
(release `rust-v0.147.0`), not a machine-local checkout:

- ordinary-start response/submission and actual start-or-steer evidence is in
  [`turn_processor.rs#L474-L607`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L474-L607),
  [`session/mod.rs#L789-L830`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/session/mod.rs#L789-L830),
  and [`handlers.rs#L189-L270`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/session/handlers.rs#L189-L270);
- inline-review response identity is established by
  [`turn_processor.rs#L1249-L1268`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L1249-L1268)
  and [`review.rs#L116-L179`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/tests/suite/v2/review.rs#L116-L179);
- the empty compact response is established by
  [`thread_processor.rs#L1876-L1887`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/thread_processor.rs#L1876-L1887);
- the TUI sends an empty `turnId` before an actual id is visible, and app-server
  interprets it as current/startup interrupt, as established by
  [`app_server_session.rs#L1132-L1155`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/tui/src/app_server_session.rs#L1132-L1155)
  and
  [`turn_processor.rs#L1409-L1453`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L1409-L1453);
- server-request pending/replay evidence is in the pinned revision's
  [`outgoing_message.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/outgoing_message.rs),
  [`thread_lifecycle.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/thread_lifecycle.rs),
  [`bespoke_event_handling.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/bespoke_event_handling.rs),
  [`app_server_requests.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/tui/src/app/app_server_requests.rs),
  and [`approval_overlay.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/tui/src/bottom_pane/approval_overlay.rs).

If an older document says that fcodex participant/socket state grants or
extends a main-turn writer, this document and the shared main-turn contract
govern.
