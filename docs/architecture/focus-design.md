# FOCUS Technical Design

Document role: synchronized English peer. Canonical Chinese: `docs/architecture/focus-design.zh-CN.md`.

This document is only the current architecture map: layers, owners, fact sources,
and dependency direction. Product behavior belongs in `docs/contracts/`, historical
rationale in `docs/decisions/`, and campaign progress in `docs/_work/`. A work ledger
must not become a runtime contract.

See also:

- [main-turn owner contract](../contracts/root-operation-owner.md)
- [`thread/create` local-commit contract](../contracts/thread-create-local-commit.md)
- [`thread/resume` local-commit contract](../contracts/thread-resume-local-commit.md)
- [server-request lifecycle contract](../contracts/server-request-lifecycle.md)
- [Feishu thread lifecycle contract](../contracts/feishu-thread-lifecycle.md)
- [`focus` / `fcodex` shared-backend runtime](./focus-shared-backend-runtime.md)
- [active architecture debt register](./architecture-debt-register.md)

## 1. Design Baseline

FOCUS is a multi-frontend integration layer for Codex. Codex app-server owns
thread, turn, item, goal, pending server-request, and effective-runtime facts.
FOCUS owns only the integration state required by Feishu, Web, wrappers, and the
local service.

The default rules are:

- align with observed upstream Codex behavior first and add only the smallest rule
  required by a real Focus multi-frontend race;
- give each mutable fact one owner; a coordinator orders owners but does not mirror
  their facts;
- keep authority, read models, projections, delivery, and cleanup separate;
- confine an unknown outcome to the exact request/effect that cannot safely be
  repeated; do not turn it into vague thread-, surface-, or service-wide outage;
- require a named safety object and evidence for fail-closed behavior; do not invent
  tree, incarnation, cursor, or exactly-once capabilities absent upstream;
- never infer Codex persisted/effective facts from local intent, caches, or UI state.

FOCUS adds only one general turn rule beyond a single upstream TUI: on one root
thread, a submission/activity that still declares Feishu next-turn/FIFO or
exclusive/autonomous semantics has at most one holder. Ordinary Web/`fcodex`
input is an upstream-routed contributor and acquires no writer. Matching
`turn/completed` releases the exact active lease immediately. Children,
interactions, cards, and delivery do not extend it.

## 2. Layers and Dependency Direction

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

### 2.1 Surfaces and transports

- The Feishu adapter owns events, messages, cards, and outbound-effect
  classification.
- Focus Web Gateway owns loopback HTTP/WebSocket transport, browser sessions, and
  DTO transport.
- `focusctl` is the local management surface. The `focus` / `fcodex` wrapper reaches
  the selected instance backend through a local proxy.

A surface authenticates, translates input, and presents outcomes. It may not mutate
another owner's state directly or infer writer authority from a live connection, a
remaining card, or a trusted user.

Within the Feishu surface, `FeishuProcessCache` is the sole owner of transient
process-local message deduplication, message context, chat metadata, reserved-card,
sender-name, and warning-throttle facts. `FeishuMessageCodec` owns message/card
schema decoding, mention normalization, and terminal-card text projection; it
obtains raw card content and sender names through named ports and never owns the
Lark SDK client. `FeishuIngressController` owns group activation/admin/trigger
policy, local history boundaries, forward aggregation, history recovery, and the
complete inbound dispatch and destination-loss cleanup order. `FeishuBot` only
translates SDK callbacks into a neutral `FeishuInboundMessage`, retains SDK
chat/message/raw-card/sender/outbound effects, and exposes the required surface
façades; it does not mirror ingress, store, recovery, or aggregation facts.

### 2.2 Application transaction owners

Named services and coordinators own fixed cross-owner orderings, including thread
create/resume, Feishu binding transitions, Web open/turn/mutation, backend reset,
server-request projection, and execution-page rollover. They may consume typed
receipts but may not retain mirrors of participating facts.

`RuntimeAdminControlRouter` is the sole local control-plane method and wire-parameter
catalog. It normalizes one request and dispatches through named service, binding, and
thread ports; domain facts and transactions remain behind those ports.

`RuntimeAdminBindingApplication` owns the surface-neutral Runtime Admin binding use
cases: inventory/read models, prompt admission, attach/detach, clear, and stale
cleanup. It coordinates the existing binding fact, clear-transaction, and thread
lifecycle owners without mirroring their state; `RuntimeAdminController` only
presents Feishu commands/cards and delegates its public binding façade.

`RuntimeAdminOfflineLifecycle` owns the surface-neutral `focusctl` offline lifecycle
transaction: archive target resolution, local binding preflight, lifecycle-result
validation, archive/unarchive/delete mutation, and cross-instance binding settlement.
It uses named infrastructure ports and returns immutable receipts; it neither keeps
runtime facts nor reads terminal input or presents output.
`bot/runtime_admin/cli_inputs.py` owns the argparse grammar and normalization of
argv, prompt-file, and `CODEX_THREAD_ID` inputs. The CLI retains delete confirmation,
dispatch, rendering, batch presentation, exit-code policy, and effect invocation.
These boundaries keep input admission and filesystem/control-plane mutation order
out of the presentation surface without adding another durable state machine.

`CodexThreadTargetService` in `bot/focus_runtime/thread_targets.py` is the stateless
application boundary for authoritative thread-target reads, direct-root validation,
and target selection. It also coordinates the existing Web resume-target path and
keeps the existing error classifiers and exact interrupt forwarding together. It
owns no thread, binding, or runtime fact: Codex app-server and the participating
runtime owners remain authoritative.

`WebThreadOpenCoordinator` owns the fixed staged order for the Web thread
directory, open, and bounded-history transactions. RuntimeLoop prepare,
settlement, and final checks alone read or write owner facts; app-server/store
I/O and detached DTO materialization run on the external-transaction worker.
`WebThreadInspectionService` applies the same prepare/effect/settle boundary to
tool detail and conversation search. `bot/web_runtime/thread_read_projection.py`
owns only the pure list/open/history DTO projection from frozen typed inputs. It
owns no runtime fact and cannot decide whether the response is still
installable; the coordinator's exact final check retains that authority.
`WebDirectThreadTargetCoordinator` centralizes authoritative direct-target
snapshots and Web-local convergence after a proven `ThreadSpawn` rejection;
only a current document/backend settlement may invoke that cleanup.

`BindingRuntimeCoordinator` in `bot/focus_runtime/binding_coordinator.py` is the
stateless cross-owner boundary for the 17 transactions kept at the binding/runtime
coordination boundary. Where a transaction combines local state with effects, it
preserves the existing shared-lock boundary: reads, revalidation, and commit that
require the binding lock remain inside it, while timer cancellation, unsubscribe,
or runtime-lease release follows outside that critical section. Web contributes
only a typed runtime-interest port with the `Callable[[str], bool]` shape; the
coordinator imports neither `WebRuntimeController` nor Web state.
`BindingRuntimeManager` retains binding/session facts,
`FeishuBindingTransitionOwner` and `RuntimeBindingBatchDeactivationOwner` retain
their commits, `InteractionLeaseStore` retains main-turn leases, and the existing
thread and service-runtime authorities retain their narrower authority. The
coordinator stores only injected capabilities and no mutable fact of its own.

`FocusRuntime` exposes none of the former 29 private binding helpers: 17 are now
coordinator operations, while consumers of the other 12 thin pass-throughs call the
existing owner directly. Frontend timer cancellation is one of the coordinator
operations. The presentation factory is outside this boundary and now belongs to
the `FeishuPlatform` capability described below.

`FeishuPlatform` in `bot/focus_runtime/feishu_platform.py` is the sole owner of
the runtime-attached Feishu bot reference and the platform-specific chat, actor,
reply, and card-publisher routing that consumes it. It stores only that attached
bot fact; it owns neither inbound route catalogs nor persisted presentation facts.

`FeishuSurface` in `bot/focus_runtime/feishu_surface.py` is the Feishu ingress,
group-policy, command, and card-action application boundary and the sole installer
of the runtime's inbound route catalogs. It composes the existing domain and
capability owners; queue admission and drain still enter through
`FeishuExecutionQueueService`, and their mutable facts remain with those owners.

`TerminalResults` in `bot/focus_runtime/terminal_results.py` coordinates the five
terminal-result lookup, record, resolution, duplicate-check, and publication
operations through `FeishuPlatform`, `TerminalResultStore`, and typed binding and
publication ports. `TerminalResultStore` remains the persisted terminal-result fact
owner; this boundary neither mirrors its records nor owns execution-output state.

### 2.3 Protocol boundary

`bot/adapters/` and `bot/codex_protocol/` isolate app-server wire shape, connection
generations, RPC outcomes, and schema drift. Application code consumes typed
responses and notifications and does not depend on Codex private disk layout.

`CodexRpcConnection` owns the websocket, identity lock, handshake state and
generation, pending response map, reader/callback producers, and inbound JSON-RPC
dispatch. `CodexRpcClient` is a typed façade with one connection capability and no
mutable transport facts.

`ManagedAppServerProcess` owns the exact local guardian process generation, selected
listen endpoint, startup lock, cleanup token, stream threads, and runtime
publication. `CodexRpcConnection` coordinates backend connection and shutdown
through that single capability; it does not retain copies of process handles or
cleanup state.

`CodexRpcStopBarrier` owns the stop-request fence, one single-flight drain attempt,
the exact websocket/producer/process capabilities transferred out of the live
connection, and any retryable cleanup outcome. It shares the connection identity
lock only to make that transfer atomic; it does not mirror connection generation,
handshake, or pending RPC facts.

### 2.4 State owners

`RuntimeLoop` normally serializes process-local state. Only cross-process
coordination or product facts that must survive restart belong in `bot/stores/`.
Persistence alone does not promote a record into business authority; discovery,
projection, and delivery ledgers retain their narrower roles.

An explicitly staged external transaction keeps one `ServiceRuntimeLifecycle`
ingress receipt on its external caller thread. For that transaction, RuntimeLoop
serializes only short prepare/settle transitions over mutable facts: it first
issues an immutable receipt pinned to the exact target and generation, the
potentially blocking I/O runs outside the loop, and only that original receipt
may settle back on the loop. A late, replaced, or retired receipt has no new
effect. This process-local staging capability does not itself migrate a caller,
create a durable operation ledger, or automatically replay an unknown outcome.

After authoritative settlement, a Web staged read may also leave CPU- or
attachment-heavy DTO projection outside the loop. Before return it performs one
O(1) final check using the original document, connection generation, projection
revision, and read observation. This decides only whether the DTO is deliverable
and cannot roll back an earlier committed resume/runtime interest. Gateway
releases its per-client lock after prepare, while the prepared service-ingress
receipt continues across the external worker and final settlement. Gateway
handoff cancellation or executor failure abandons only an unclaimed receipt; a
claimed transaction performs its own settlement, and shutdown waits for it to
exit.

Web live notifications that require turn/task or attachment materialization use
the same boundary. Inside RuntimeLoop, `WebRuntimeEventCoordinator` only applies
the notification/cache mutation and freezes an immutable receipt pinned to the
exact read observation and runtime epoch. On a service-ingress background
worker, `bot/web_runtime/notification_projection.py` performs turn/task DTO
projection, image hashing/copying, and attachment-URL materialization. Each
thread has at most one projection in flight and one latest-wins successor.
Settlement publishes only while the original observation/epoch still matches;
it drops stale results. A successor does not reuse its predecessor's ingress
receipt: settlement admits it as a new external transaction. If service ingress
is already stopping, that admission fails, the flight retires, and only a
lightweight `thread_invalidated` is emitted, so presentation arriving after the
shutdown fence cannot extend the old barrier indefinitely. Initial worker-
admission failure, or projection failure with no successor, uses the same
lightweight invalidation and never blocks the notification. Physical attachment-scope cleanup after authoritative
`thread/deleted` or a known successful Web delete runs under the same shutdown
barrier. These flights are not
durable, are not replayed, and gain no thread-lifecycle authority.

Web runtime cleanup uses the same staged external-transaction boundary even
though a RuntimeLoop transition discovers the work.
`WebRuntimeLifecycleCoordinator` owns one cleanup flight per thread and
coalesces arrivals during that flight
into at most one successor. RuntimeLoop freezes and rechecks exact local facts;
the external worker probes backend/holder state, sends at most one claimed
canonical unsubscribe, and compare-and-sets the complete service-holder record
captured by the probe. The coordinator rechecks immediately before unsubscribe
send and again before holder release, then finalizes local interest/cache only
under the original generation and facts. A new desire, canonical Feishu
subscriber, pending interaction, backend generation, or holder successor
retains the runtime at the corresponding stage; mismatch never replays the
unsubscribe.

## 3. Runtime Topology

- Local instances share `CODEX_HOME` and the persisted thread namespace.
- Each Focus instance independently owns configuration, data, a service lease,
  control plane, Web Gateway, and a local
  `service -> guardian -> codex app-server` backend lifecycle.
- A machine-level instance registry and thread-runtime lease coordinate Focus
  instances. They do not grant an in-instance main-turn writer.
- Browsers connect only to Focus Gateway, never directly to app-server.
- `focus` / `fcodex` selects a running instance and connects the upstream TUI to its
  published backend through a per-launch local proxy.
- Focus does not support external app-server deployment. An internal attached client
  does not gain backend lifecycle authority.

See [the shared-backend runtime](./focus-shared-backend-runtime.md) for the complete
topology, wrapper, cwd proxy, credential, and platform-containment boundaries.

## 4. Current Fact Sources and Owners

| Question | Sole fact source / owner | Explicitly not authority |
| --- | --- | --- |
| What are the thread, turn, item, goal, title, cwd, status, and effective runtime? | Codex app-server through typed adapter reads/notifications | requested settings, caches, cards, browser snapshots |
| Does the service admit ingress, and when may resources be released? | `ServiceRuntimeLifecycle` and its exact external-ingress receipts | Handler, Gateway, or adapter-local ready flags |
| Who owns one local app-server process generation and its durable runtime publication? | `ManagedAppServerProcess` | `CodexRpcConnection` websocket, pending RPC, or stop-barrier state |
| Who owns the live websocket generation, handshake, pending responses, and reader producers? | `CodexRpcConnection` | the `CodexRpcClient` façade, adapter read models, or surface callbacks |
| Who owns a requested or incomplete Codex RPC shutdown? | `CodexRpcStopBarrier` and its exact transferred resource capabilities | live connection fields, cards, service phase, or copied process handles |
| May an ordinary RPC use the current websocket/backend generation? | the `AdapterIngressGate` outbound permit, actual-send guard, and response confirmation together with adapter transport-generation authority | callback arrival order or cached endpoints |
| Which instance may materialize a live thread? | machine instance registry, global loaded gate, and `ThreadRuntimeLeaseStore` | in-instance main-turn lease or cached thread lists |
| Who owns a lease-bearing Feishu/exclusive/autonomous main-turn holder? | `InteractionLeaseStore` | ordinary Web/`fcodex` input, child, socket/document liveness, delivery, goal, runtime lease |
| Who may steer, interrupt, or answer an interactive server request on the exact current turn/request? | the app-server exact turn/request fact plus the relevant surface/domain owner; `WebPromptSubmissionCoordinator` freezes the exact turn/backend generation for a Web prompt and `WebPromptResultRegistry` bounds its mutation to one effect slot while its receipt remains retained | the main-turn writer relation alone, presentation, caches, or generic local identity |
| How are local consequences of one create/resume committed? | immediate process-local receipts from `ThreadCreateTransaction` / `ThreadRuntimeAuthority` | durable journals, cross-thread quarantine, automatic replay |
| Is an app-server callback pending? | Codex app-server; `ServerRequestRegistry` only projects the current connection epoch | cards, browser action locks, main-turn leases |
| Which thread is a Feishu chat bound to by default? | `ChatBindingStore`; resident transitions belong to `BindingRuntimeManager`, `BindingOwnerAuthority`, and typed commands | execution card, subscriber, or sender cache |
| What is the input order for one Feishu binding? | `FeishuExecutionQueueController` | main-turn writer or backend residency |
| What content and page ranges are displayed for an execution? | `ExecutionTranscript` and `ExecutionPageLedger`, each for its presentation facts | turn completion, FIFO, or thread lifecycle authority |
| Is a Feishu destination authoritatively lost? | `FeishuDestinationLossStore` and `FeishuDestinationLivenessCoordinator` | generic timeout, unknown send error, main-turn state |
| What are Web's durable navigation workspace/selection and attachments? | `WebWriterProfileStore` and `WebAttachmentStore`; the latter also owns process-local exact file pins for in-flight submissions | browser components, read models, event sockets, next-turn settings |
| What are the instance-wide Web next-turn settings? | `WebNextTurnSettingsStore`; `WebNextTurnSettingsCoordinator` closes the mutation/projection transaction | `WebWriterProfileStore`, selection/navigation generation, main-turn leases, thread effective settings, browser overlays/events |
| What are a Web document, runtime interest, read model, and prompt-result evidence? | `WebDocumentRegistry`, `WebRuntimeInterestRegistry`, `WebThreadReadModel`, and `WebPromptResultRegistry`, independently | each other's projection; none is a durable writer or replay authority |
| Who owns canonical Web-triggered subscription and runtime cleanup? | `WebRuntimeLifecycleCoordinator` owns the per-thread cleanup flight and exact local rechecks; `ThreadRuntimeAuthority` owns the same-thread resume/start/unsubscribe effect fence; the thread-runtime lease owner alone performs full-record holder CAS | an `fcodex` connection, read cache, or the background worker thread |
| Who constructs Web thread list/open/history DTOs and decides whether they remain deliverable? | `thread_read_projection.py` projects frozen inputs without state; `WebThreadOpenCoordinator` performs settlement and final admission against exact document/generation/revision/observation | the external worker, browser components, or an already-stale DTO |
| Is an fcodex endpoint/request/interaction current? | `FcodexParticipantRuntimeRegistry`, `FcodexOperationService`, and `FcodexInteractionInbox`, independently | participant `connected/grace/orphaned` endpoint state is not a main-turn writer |
| Where do component-config seeds come from? | `system_config.py` and `codex_config.py`; the runtime-settings contract decides when a value is only a seed and when a durable settings owner wins | UI echoes, settings ACKs, or rereading config on every read/turn |

This table defines ownership only. State transitions, failure classification, and
user-visible outcomes belong to the linked contracts; this architecture document
must not recreate parallel state machines.

## 5. Key Lifecycle Boundaries

### 5.1 Service and backend

`ServiceRuntimeLifecycle` activates RuntimeLoop, adapter, control plane, Gateway,
and workers only after acquiring the service lease. Shutdown closes new ingress,
waits for admitted callbacks, stops producers, drains RuntimeLoop, then stops the
adapter and releases authority. Components retain narrower transport barriers but
do not mirror the top-level phase.

A transaction admitted through external ingress and crossing loop-external I/O retains the same external-ingress
receipt from admission through final settlement. Shutdown can therefore close new
admission and wait for those transactions while RuntimeLoop continues to process
notifications, interrupts, and short state transitions during the pending effect.
The receipt proves only in-process admission and liveness; it does not prove that
an upstream effect succeeded. The relevant adapter/domain contract still owns the
pre-send, known-response, and outcome-unknown classification.

When Gateway separates prepare and execute across its per-client lifecycle
lock, it still retains this receipt. If a request is cancelled before worker
claim, or the executor cannot admit it, Gateway abandons the exact receipt so
shutdown cannot wait forever. Cancellation after claim stops only the HTTP
waiter: it cannot cancel the running worker or settle twice, and the service
continues waiting for the original transaction to exit.

A RuntimeLoop-discovered Web runtime cleanup launched through
`start_background_external_transaction` is still an external transaction: it
acquires its service-ingress receipt before starting the daemon thread, and that
receipt is its shutdown barrier. It is not one of the long-lived internal
scheduled workers below and needs no second worker registry. Internal scheduled
workers do not acquire an external-ingress receipt; their unique worker registry
remains their producer-stop and join barrier before RuntimeLoop drains.

`ServiceRuntimeLifecycle` remains the sole owner of the service phase and of this
startup, rollback, and shutdown order. `ServiceRuntimeAuthority` is a lower
coordination capability: at lifecycle-defined boundaries it invokes the existing
machine instance registry, global loaded gate, and service/thread-runtime lease
owners. It neither owns nor mirrors those facts and does not depend on presentation or the
`FocusRuntime` composition root.

The minimal backend-reset order is: fence ordinary ingress, read-only capture the
current process's exact main-turn leases, wait for owned-child OS exit/wait,
retire the capture centrally by full-record CAS, retire registry/fcodex/Web facts,
run binding detach and execution interruption/finalization, and then retire
Feishu root/request facts. Every participating owner is idempotent and retains
its own mutable facts. Focus then clears
this instance's runtime holders,
starts and verifies the replacement, and finally publishes and admits it. An
unconfirmed stop retires no old-backend facts. If a later authoritative
retirement or structural projection fails, already completed earlier retirements
remain in effect, the
replacement is not started, and an idempotent retry begins the ordered stage
again while ingress stays fenced. Other PIDs and successor leases installed
after capture are unaffected. Old callbacks, writers, and unknown evidence are
not migrated.

### 5.2 Main turn

A blank submission lease is acquired only for a lease-bearing
Feishu/exclusive/autonomous effect. Ordinary Web/`fcodex` `turn/start` is
upstream-routed input and does not read or write a lease. A validated
`turn/start` success preserves the authoritative upstream `turn.id`, but the
response alone neither activates nor transfers a Focus lease nor establishes
lifecycle/completion authority across notifications or reset. Matching
`turn/started` still binds the actual `turn_id`, and matching completion releases
only the exact active lease. If started was missed, completion cannot bind a
blank from the start response alone. Feishu may settle its exact ordinary-prompt
blank after an authoritative inactive-root reread without attributing a
completion. Its process-local admission token is a transaction receipt, not a
second writer fact. When ordinary fcodex start races an existing fcodex
exclusive/autonomous blank, later lifecycle may still activate that blank. This
accepted narrow race adds no correlation state machine. Inline-review response
identity and compact's empty response keep their method-specific paths; only the
main-turn owner contract defines the details. A live `fcodex`
endpoint attached to the exact direct root, or a connected Web document that has
materialized that root, may steer or interrupt the exact current/startup turn under the
canonical effect-specific boundaries without becoming or changing the writer.
An existing-thread Web prompt uses one POST. Inside RuntimeLoop,
`WebPromptSubmissionCoordinator` freezes the exact document/target/backend
generation and the active turn then visible. If A exists, that attempt may call
`turn/steer` once for A only; only an attempt with no exact active id may call
`turn/start` once. A successor-B mismatch or no-active result is
`known_no_effect`, never a retarget to B or a fallback start.
`WebPromptResultRegistry` retains only bounded, process-local
`pending / succeeded / known_no_effect / outcome_unknown` receipts. It stores no
payload, is not persisted, never replays, and blocks neither a new mutation on
the same thread nor passive reads, shared server-request responses, or exact
interrupt. F5 may only read the bounded result using `(thread_id, mutation_id)`.
A duplicate POST cannot acquire a second effect slot only while that receipt
remains retained. Terminal eviction, confirmed backend retirement, or service
restart removes the seen-identity evidence; the same UUID may then acquire a new
slot, but the official browser only GETs or creates a new UUID for a new gesture.
Such a miss proves no negative effect and grants no replay authority. See the [Focus Web prompt mutation recovery
contract](../contracts/focus-web-prompt-mutation-recovery.md).
Disconnect does not convert a writer into durable grace/orphan state,
and service restart does not recover an old writer. See the [main-turn owner
contract](../contracts/root-operation-owner.md).

### 5.3 Create, resume, and server request

Create and resume use process-local receipts only between one call and its immediate
local commit. Unknown outcomes are not automatically retried and do not create a
durable recovery state machine. The upstream app-server owns callback lifetime;
Focus adds current-generation identity and one-shot surface tokens only to reject
stale actions.

The outer read and projection for a Web cold open may continue after this resume
local commit. A known resume first commits runtime interest. A later document
reissue, notification, projection revision, or backend replacement only makes
the read DTO stale and cannot compensate, roll back, or reclassify that
confirmed resume as unknown. See the
[`thread/resume` local-commit contract](../contracts/thread-resume-local-commit.md).

### 5.4 Presentation and delivery

Feishu execution pages, terminal results, generated images, Web projections, and
destination liveness are independent product capabilities. They may reconcile their
own exact effects asynchronously, but cannot delay matching main-turn completion,
extend a writer, or spread a local delivery unknown into a later turn.

## 6. Repository Responsibility Map

- `bot/`: surfaces, application owners, runtime owners, adapters, CLIs, and the
  composition root. Its physical layout remains broad, so paths alone do not define
  every owner. The dependency-direction guard does enforce the stable boundaries:
  stores and Codex protocol/adapter packages cannot depend upward on surfaces or
  composition; Web, Feishu, and fcodex cannot import one another's presentation
  domains; surface-neutral Runtime Admin transactions cannot import a surface.
- `bot/stores/`: durable authority, intent, coordination, delivery ledgers, and
  rebuildable discovery stores.
- `bot/runtime_admin/`: surface-neutral binding/control/offline-lifecycle
  transactions plus the explicit CLI and Feishu presentation modules. The neutral
  modules are protected from importing any surface.
- `bot/focus_runtime/thread_targets.py`: the `CodexThreadTargetService` boundary for
  authoritative thread-target reads, validation and selection, Web resume-target
  coordination, error classification, and interrupt forwarding; it stores no
  mutable authority.
- `bot/focus_runtime/binding_coordinator.py`: the `BindingRuntimeCoordinator`
  boundary for 17 cross-owner binding/runtime transactions; through existing owner
  capabilities, it preserves shared-lock critical sections and commit/effect order
  while storing no mutable authority.
- `bot/focus_runtime/feishu_platform.py`: the `FeishuPlatform` owner for the one
  attached Feishu bot fact and platform-specific chat/actor/presentation routing;
  it stores no route, persistence, or domain facts.
- `bot/focus_runtime/feishu_surface.py`: the `FeishuSurface` boundary for Feishu
  message/recall/card/attachment ingress, group checks, command and action routes,
  and prompt/FIFO entry through existing capability owners.
- `bot/focus_runtime/terminal_results.py`: the `TerminalResults` boundary for
  terminal-result lookup, persistence coordination, and publication; the existing
  `TerminalResultStore` remains the persisted fact owner.
- `bot/web_runtime/thread_open_coordinator.py`: the Web list/open/history staged
  transaction owner; `bot/web_runtime/thread_read_projection.py`: stateless
  external projection from frozen inputs into list/open/history DTOs;
  `bot/web_runtime/direct_thread_target_coordinator.py`: direct-target proof and
  exact invalid-target convergence; and
  `bot/web_runtime/gateway_external_transaction.py`: the thin bridge from
  aiohttp cancellation to lifecycle-owned prepared receipts.
- `bot/fcodex/control_dispatcher.py`: the service-side `operation/*` protocol
  boundary. It runs inside the caller's `RuntimeLoop` turn, authority-reads direct
  targets, and delegates mutable facts to the existing operation owner.
- `bot/feishu_continuation_controller.py`: the serialized Feishu continuation
  boundary. It orders direct-root reads, root-operation admission, explicit thread
  resume, Runtime Admin attach, goal mutation/resume/compensation, settings fencing,
  settlement, and history/card projection while leaving every mutable fact with the
  existing runtime owners.
- `web/src/focus/`: Focus-owned transport, projection, navigation/profile, and
  mutation/action owners. Generic UI components consume view projections only.
- `docs/contracts/`: formal source for current behavior semantics.
- `docs/architecture/`: current owners, layers, dependencies, and active debt.
- `docs/decisions/`: rationale. Superseded decisions must be visibly marked and may
  not pose as current contracts.
- `docs/_work/`: temporary plans and evidence ledgers, not durable fact sources.
- `tests/`: owner/contract tests; only small composition suites may depend on wiring.

`FocusRuntime` is the current composition root at
`bot/focus_runtime/runtime.py`. The root must not become a new domain-fact owner. The
`bot/focus_runtime/__init__.py` package root stays empty and does not re-export the
runtime; capability owners below that package must not import the composition root.
New behavior belongs in an existing owner, or must first explain why no existing
boundary can own the new fact.

## 7. Evolution Rules

- Follow the [repository navigation and change-cone discipline](./development-navigation.md)
  for every diagnosis, review, implementation, and refactor.
- Inspect pinned upstream code or official protocol material before changing Codex
  behavior, then decide whether Focus truly needs a deviation.
- A new coordinator must name the owners it orders and explain why it stores no
  second copy of their facts.
- A new durable record must state its restart requirement, write authority,
  corruption policy, and deletion condition.
- Any stronger-than-upstream rule must identify the real multi-frontend scenario,
  minimal increment, observable evidence, and user cost.
- Track unresolved owner, aggregate, schema, test, and package work only in the
  [active architecture debt register](./architecture-debt-register.md).
