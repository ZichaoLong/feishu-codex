# Server-Request Lifecycle Contract

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/server-request-lifecycle.zh-CN.md`.

## 1. Purpose and Upstream Baseline

This contract defines how Focus projects Codex app-server requests to Web,
Feishu, and `fcodex`. It deliberately follows upstream Codex instead of
inventing a stronger durable callback lifecycle.

The reviewed upstream baseline is Codex commit
`be6e8eac029b183056b7e4402879f15d2c85f61b`:

- app-server stores each pending callback, thread id, and request in an
  in-process map before sending it;
- a server request is broadcast to every connection subscribed to its thread;
- running `thread/resume` first attaches the connection, returns the resume
  response, and then replays matching pending requests in request-id order;
- the first client response atomically consumes the callback, and app-server
  then broadcasts `serverRequest/resolved` to current subscribers;
- turn start/completion/abort clears that thread's pending callbacks.

The relevant upstream implementation is pinned to public, immutable source:

- [pending callback storage, broadcast, replay ordering, and first-response consumption](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/outgoing_message.rs#L287-L466);
- [resume response followed by pending-request replay](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/thread_lifecycle.rs#L721-L755);
- [turn lifecycle cleanup](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/bespoke_event_handling.rs#L157-L190).

## 2. State Ownership

Codex app-server owns whether a callback is actually pending. Focus does not
persist a second callback ledger.

`ServerRequestRegistry` owns only the current Focus process and app-server
connection epoch projection:

- key: canonical typed JSON-RPC request id;
- value: one immutable `ServerRequestIdentity`, including receiving connection
  generation, method, and a deep snapshot of params;
- a small resolved set suppresses same-epoch replay after local settlement;
- dispatch-outcome unknown marks only that exact request.

Web, Feishu, and `fcodex` each own only their local delivery/card/action state.
They reference the canonical identity and add one-time response capabilities or
tokens. They do not own upstream callback lifetime.

There is no durable request/root/global settlement fence. A pending request,
unknown response, missing parent relation, or local projection failure does not
extend the main-turn lease and does not quarantine another request, thread,
surface, or backend generation.

## 3. Reader Order and Routing

For ordinary stateful server requests, `CodexRpcClient` invokes the lightweight
request callback synchronously in websocket-reader order. That callback only
queues work on `RuntimeLoop`; it performs no surface I/O. A following lifecycle
notification is therefore queued after the preceding request frame without a
reader write-ahead store or detached-callback barrier.

On `RuntimeLoop`, `ServerRequestCoordinator` registers the identity and invokes
`ServerRequestSurfaceDispatcher`:

- a new identity is offered to the selected surface;
- an exact replay reuses the same object and may rebuild or refresh its local
  projection;
- a same-key, different-envelope conflict rejects only that request;
- known-not-committed may be retried by an explicit later replay;
- outcome-unknown is not automatically retried, but it does not block an
  unrelated request.

The dispatcher is a selection boundary, not another state owner. A surface
claim is committed only after that surface has retained its local request
record. Decline means no local or external effect was produced.

In the current backend epoch, a canonical command-execution, file-change, or
permission approval with a non-empty `turnId` for an exact direct root uses
`shared_approval` routing. The same canonical identity is offered independently
to eligible fcodex, Web, and Feishu projections. `InteractionLeaseStore` owns
only the Focus writer; it is not callback-admission evidence. An autonomous
turn created by ordinary goal continuation therefore uses the same approval
route even when it has no Focus writer.

An ordinary interactive callback with the same direct-root and non-empty-turn
proof uses `shared_interaction` routing. User input, MCP elicitation, and
dynamic tool calls are offered to fcodex and Web independently; Web may decline
a method or shape it cannot present, including dynamic tool calls. If either
desktop surface retains the callback, Feishu is not offered a duplicate. Only
two authoritative desktop declines fall back to the original Feishu
single-surface route. An unknown desktop outcome blocks that fallback but does
not prevent the other desktop offer. Authentication, unsupported methods,
empty-turn requests, and child-thread requests retain their original
single-surface route and do not inherit shared authority from a root.

`shared_approval`, `shared_interaction`, and the Web inbox's delivery scope are
server-local routing facts. `project_pending_request` deliberately omits that
scope from the Focus Web DTO; the browser receives only the exact projected
request and its response capability. This change therefore leaves Focus Web
wire version 9 unchanged.

## 4. Response Authority

The adapter pins a response to the websocket generation that received the
request. App-server accepts only the first response for its callback.

Focus adds the minimum local ABA protection needed by multiple frontends:

- Web and Feishu use an exact one-time `response_capability`;
- every eligible `fcodex` endpoint gets its own exact one-time
  `response_token` for the same canonical shared interaction;
- a stale tab, card, proxy connection, or value-equal replacement object cannot
  answer a newer request;
- a proven pre-send failure may leave that exact action retryable; an
  automatic fail-close retries only on an explicit exact upstream replay;
- a transport outcome of unknown makes only that exact action non-retryable
  until matching resolution/lifecycle cleanup or connection-epoch retirement;
  an exact replay does not reopen it.

Presentation and delivery are not lifecycle authority. A browser disconnect
hides that document's shared-interaction projection without answering the user
or deleting the canonical request; a still-eligible document may receive it
again on reconnect. Writer-owned single-surface interactions keep their
existing cleanup contract, and reconnect does not transfer them to another
document. If an automatic fail-close write is proven not sent, a later explicit
upstream replay may still create a projection through its original route.

For a shared interaction, all authenticated live Web documents and fcodex
endpoints which have materialized the exact direct root may answer the methods
they can present. The first valid response wins the canonical app-server
callback. Other actions receive a typed superseded/unknown receipt or are
retired by `serverRequest/resolved`; they never create a second adapter
response. An invalid fcodex response retires only that endpoint token and does
not answer or cancel for another endpoint. Answering does not transfer the turn
initiator, change active settings or destination, or grant next-turn admission.
Trusted local surfaces expose every response allowed by the upstream protocol.
Command approval honors that request's `availableDecisions`; file and
permission approvals use their schema-defined response sets, including session
approval and permission `strictAutoReview`. Focus does not invent responses
which the upstream protocol does not define.

Shared response authority comes from the exact canonical identity in the
current `ServerRequestRegistry` connection generation plus each surface's
direct-root, non-empty-turn, live-endpoint, and materialized-subscription
checks. It does not come from the main-turn writer lease. A proxy-first
projection may be displayed early, but it cannot submit an adapter response
until the canonical identity binds. A non-approval desktop offer must prove at
least one live recipient before that surface claims it; otherwise it declines
so the dispatcher can preserve the Feishu fallback. A shared Web user-input
auto-resolution is one system-owned transaction tied to the canonical request,
backend epoch, and exact timer generation. It does not require a document
writer and is not cancelled merely because one browser disconnects.

When group deactivation has exact Feishu binding and non-admin turn-actor
evidence, Focus attempts one canonical fail-close and separately revokes user
response authority for that exact current-epoch identity. Revocation is not a
claim that the cancel was submitted: response effect phase remains independently
`pending`, `submitted`, or `unknown`. A proven pre-send failure therefore stays
an unresolved blocker, but every old Web/fcodex/Feishu capability is centrally
denied and exact replay cannot regrant it. The registry clears both facts only
on matching resolution/lifecycle cleanup or epoch retirement; unrelated
requests remain usable. Revocation immediately hides the exact Web projection
and publishes its pending-request change, and a later fcodex endpoint is denied
before a fresh token is issued. An already-rendered fcodex overlay may remain
until its action receives a typed superseded receipt or real upstream cleanup,
because Focus has no service-to-proxy presentation push.

Feishu remains an exact-binding projection rather than a broadcast audience.
A local trusted endpoint may nevertheless answer the same canonical approval.
For ordinary shared interactions, Feishu retains its existing single-surface
behavior and is called only after both desktop surfaces authoritatively decline;
its queue and card lifecycle are otherwise unchanged.
An unknown interactive-card send/reply is not immediately retried. Feishu's
official create/reply contract guarantees same-UUID at-most-once behavior only
within one hour, so Focus keeps the exact UUID, immutable publish intent, and
pre-attempt wall/monotonic timestamps only in the process-local projection.
After canonical resolution it may reconcile once with that UUID only while
both clocks remain inside a conservative 50-minute window; an expired intent is
never replayed. A confirmed reconciliation yields the exact message id and is
patched immediately. If that result is rejected, still unknown, identity-
drifted, or expired, Focus retains no further delivery authority, does not
answer for the user, and makes no third attempt. The official sources are
<https://open.feishu.cn/document/server-docs/im-v1/message/create.md> and
<https://open.feishu.cn/document/server-docs/im-v1/message/reply.md>.

## 5. Resolution, Lifecycle, and Disconnect

`serverRequest/resolved` settles only a matching request id and thread id.
After registry settlement, the coordinator cancels its timer, removes the exact
Web/Feishu/fcodex projections, and reconciles only affected local roots.
Unknown notifications are local `missing` results and do not manufacture a
tombstone.

Local cleanup is best-effort presentation convergence, not settlement
authority. Failure of one surface remover, root lookup, or root reconciliation
is logged and does not skip the other surface removers or reverse an already
canonical settlement.

Matching turn/thread lifecycle facts retire the current thread's process-local
requests and projections in reader order. This mirrors app-server callback
retirement; it does not create a second durable settlement transaction.

App-server connection loss clears the registry epoch, timers, and old surface
capabilities. A later connection plus `thread/resume` replay creates fresh
generation-pinned identities and projections. Focus does not restore request
state from disk or keep hidden blockers across process restart.

A frontend transport disconnect is narrower: it retires only that Web
document or fcodex endpoint capability. It neither answers nor deletes a
canonical shared interaction. While the same Focus/app-server processes and
backend epoch remain alive, the Focus Web inbox retains that canonical shared
interaction. When a Web document changes from disconnected to connected,
Focus publishes one `pending_request_changed` only if the document has again
become eligible for a shared interaction on a currently materialized exact
root. A second socket for an already-connected client does not repeat the
event. The Gateway
sends hello after that connection transaction, so its newer revision makes the
browser atomically rebuild from the HTTP current-pending set. A new document
after F5 need not impersonate the old physical Tab; it materializes and proves
eligibility under its own identity.

This same-epoch reprojection neither invokes nor fabricates `thread/resume`.
Only a real Focus-to-app-server connection-epoch loss clears the registry and
old capabilities, after which a real `thread/resume` replay creates fresh
canonical identities. Reprojection keeps `pending` answerable and keeps
`processing`, `submitted`, and `unknown` non-repeatable; resolved, revoked,
hidden, or inactive-generation requests do not reappear. Durable queues,
physical Tab identity, and cross-restart recovery are explicitly outside this
contract.

## 6. Subagents and Main Turns

Focus neither observes nor reconstructs child lineage and never rebinds a child
callback to a guessed root. An exact request thread enters a shared interaction
domain only when an authoritative `ThreadSummary` in the current connection
epoch proves it is a direct root. An unproved callback is declined or
fail-closed only at that exact request according to the surface contract; it
acquires no root lease and does not widen the request fence. See
[`subagent-observation-and-recovery.md`](subagent-observation-and-recovery.md).

The shared main-turn lease is independent. Matching `turn/completed` releases
the main-turn writer immediately even if a request card, child thread, delivery
projection, or cleanup is still present.

## 7. Backend Reset

Backend-reset preview records the current pending count for diagnostics. The
replacement transaction then stops the owned backend, retires the registry and
all old-generation surface/transport capabilities, and starts a fresh backend.
It does not run a pre-stop per-request response transaction and does not replace
a durable interaction epoch.

## 8. Required Regression Boundaries

Tests must lock down at least:

- request callback enqueue before a following lifecycle notification;
- exact replay object reuse and fresh identity after connection replacement;
- exact resolution and matching lifecycle removal across all three surfaces;
- shared Web↔Web, Web↔fcodex, and fcodex↔fcodex first-response convergence;
- late attachment, endpoint disconnect, resolved-first/response-late ordering,
  and bounded same-epoch late-response receipts;
- exact Feishu binding plus same-UUID unknown-card reconciliation;
- group-deactivation submitted/unknown/not-sent effect phases plus independent
  exact authority revocation, including proxy-first stale-token denial;
- complete command/file/permission approval option mapping;
- shared interaction leaving initiator, settings, destination, and next-turn
  admission unchanged while unsupported, child, and empty-turn requests remain
  single-surface;
- an eligible local endpoint answering a current canonical approval from an
  active-goal autonomous turn even when no Focus writer lease exists;
- two-desktop fanout, first-response convergence, unknown-without-Feishu
  fallback, and two-authoritative-declines Feishu fallback for ordinary
  interactions;
- a shared user-input timer surviving document disconnect while remaining
  pinned to its exact callback and backend/timer generations;
- stale surface capability/token rejection;
- pre-send versus outcome-unknown response classification;
- same-epoch Web disconnect, F5, new-document, and second-socket reprojection
  or clearing from current pending, plus resume-replay reconstruction after a
  connection replacement;
- identity conflict and dispatch unknown remaining scoped to one request;
- no durable store, root fence, or global unavailability path for server
  requests.
