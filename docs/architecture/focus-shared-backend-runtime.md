# `focus` / `fcodex` Shared-Backend Runtime Model

Document role: synchronized English peer. Canonical Chinese: `docs/architecture/focus-shared-backend-runtime.zh-CN.md`.

This document describes only the current shared-backend topology, wrapper/proxy
boundary, and runtime owners. Formal contracts define mutation, main-turn,
create/resume, and server-request semantics.

See also:

- [FOCUS technical design](./focus-design.md)
- [main-turn owner contract](../contracts/root-operation-owner.md)
- [fcodex main-turn and proxy contract](../contracts/fcodex-operation-owner.md)
- [`thread/create` local-commit contract](../contracts/thread-create-local-commit.md)
- [`thread/resume` local-commit contract](../contracts/thread-resume-local-commit.md)
- [server-request lifecycle contract](../contracts/server-request-lifecycle.md)
- [thread-profile semantics](../contracts/thread-profile-semantics.md)

## 1. Meaning of “Shared Backend”

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

“Shared backend” means that Feishu, Web, management surfaces, and wrappers within
one Focus instance share that instance's owned app-server. It does not mean every
instance shares one app-server. Instances share `CODEX_HOME` and use the
machine-level registry, loaded gate, and runtime lease to avoid materializing one
thread in multiple managed backends.

An explicitly staged external-ingress transaction may prepare and settle mutable
facts briefly on RuntimeLoop while running its blocking effect outside the loop
under one service-ingress receipt. Internal scheduled workers instead remain under
their unique worker-lifecycle barrier. Adopting either boundary is a property of
each caller, not an implication of the topology diagram. The `focus` / `fcodex`
proxy retains its separate protocol boundary and forwards directly to the published
backend. A RuntimeLoop-discovered Web cleanup launched by the service dispatcher's
background external-transaction entry is in the first category: it acquires the
service-ingress receipt before its daemon worker starts and needs no second worker
registry.

The Focus service supports only an owned, same-host app-server. `focusctl` and
wrappers are internal attached clients of a published endpoint. They are not an
external deployment mode and gain no start/stop/reset authority. Bare `codex`, an
IDE, another machine, and unregistered app-servers are outside this coordination
boundary.

## 2. Instance Backend Lifecycle

Each instance independently owns:

- `FOCUS_CONFIG_DIR` and `FOCUS_DATA_DIR`;
- a service lease and control plane;
- a loopback Web Gateway when enabled;
- a `service -> dormant guardian -> codex app-server` lifecycle;
- publication of the current backend endpoint, process identity, generation, and
  capability token.

Startup acquires the service lease, checks any old runtime record, reserves a
cleanup token, starts a dormant guardian, atomically publishes the runtime record,
and activates the guardian last. Parent loss before publication cannot launch
app-server.

`ServiceRuntimeLifecycle` remains the sole owner of service phase and of startup,
rollback, and shutdown ordering. `ServiceRuntimeAuthority` only coordinates the
existing machine-visible instance registry, global loaded gate, and
service/thread-runtime lease owners when that lifecycle invokes it; it introduces
neither a second lifecycle nor a second copy of their facts.

The guardian owns process lifecycle. On orderly stop, natural child exit, or Focus
service loss, it first converges the process set the platform can prove, then writes
a cleanup receipt bound to that exact generation. A later service waits briefly for
a still-running guardian and automatically retires a gone or PID-reused guardian
only when that receipt matches.

A missing or mismatched receipt is deliberately different from an ordinary crash.
Durable state cannot distinguish a receipt-write failure after successful cleanup
from a guardian that was killed before cleanup completed. Startup therefore remains
fail-closed. The error identifies the exact instance runtime record; after
independently inspecting and cleaning processes within the platform's documented
boundary, an operator may remove only that record and retry. Focus provides no
recovery or force command because such a command would add no cleanup proof. Legacy
direct-child records use the same manual boundary because they never had guardian
tree proof.

Platform proof boundaries are:

- Linux requires a subreaper and converges adopted descendants;
- Windows places app-server in a kill-on-close Job Object before execution;
- macOS proves app-server and its process group. A tool or MCP descendant that
  deliberately creates a new session/group can survive service stop, reset, or
  service crash cleanup; Focus cannot discover or terminate it reliably afterward.

A cleanup receipt always means only that the platform-specific containment set above
was converged. On macOS it is not proof that an escaped descendant is absent, and
diagnostics or manual record removal cannot strengthen that claim.

The closed recovery decision and remaining platform gap are indexed in the
[architecture debt register](./architecture-debt-register.md).

## 3. Instance Selection and Endpoint Discovery

`focusctl`, `focus`, and `fcodex` resolve `--instance`, then ask that instance's live
control plane for its current ready backend endpoint. A configured URL, stale
registry URL, or durable runtime record is insufficient dialing evidence because a
loopback port may have been reused by another process.

The preferred default endpoint is `ws://127.0.0.1:8765`. On conflict, the service
selects a free loopback port for that instance and publishes it. If the target
instance is stopped or a replacement backend is not ready, an attached client fails
fast instead of silently starting an isolated backend.

Web Gateway is also published per instance. One Gateway serves multiple browser
documents and threads. Browsers never receive the backend capability token.

## 4. Wrapper and Local Proxy

The `focus` / `fcodex` startup chain is:

1. select a running Focus instance and final cwd;
2. start a loopback websocket proxy with a per-launch bearer token;
3. connect that proxy to the selected instance's published app-server endpoint;
4. launch the upstream TUI with internal `codex --remote <proxy>`.

Here `--remote` is the upstream TUI-to-local-proxy protocol link, not a user-selectable
external app-server. A user-supplied external `--remote` target is rejected.

The final cwd is explicit `--cd` / `-C`, or the calling shell cwd. The wrapper passes
it to both the upstream TUI and proxy. The proxy injects it only when an admitted
`thread/start` omits cwd. This is a narrow correction for upstream remote startup,
not generic payload rewriting.

The standalone `--` terminator ends Focus-owned option and subcommand parsing. The
terminator and every argument after it are forwarded to upstream unchanged; an
upstream `--cd` in that opaque tail therefore cannot change the wrapper cwd or
trigger Focus's reserved-option rejection.

Upstream remote resume may connect, disconnect, and connect again. The proxy
therefore follows the wrapper parent process rather than exiting after the first
websocket disconnect.

## 5. Proxy Responsibilities

The proxy is not an arbitrary transparent RPC channel. It owns:

- websocket token authentication;
- frame/schema and method classification;
- the targetless-read allowlist;
- exact direct-root target validation;
- the narrow child metadata-read exception;
- main-turn submission/control admission;
- a one-shot current-generation capability for targetless `thread/start`;
- cwd injection;
- binding a client response to the exact participant, connection, request id, and
  action token.

The proxy does not own Codex callback lifetime and does not derive writer authority
from socket state. `connected/grace/orphaned` explains only fcodex endpoint reconnect
and cleanup. Main-turn ownership comes only from a PID-bound exact submission/active
lease in the shared `InteractionLeaseStore`.

## 6. Two Independent Coordination Axes

### 6.1 Backend residency

The machine-level loaded gate plus `ThreadRuntimeLeaseStore` answers which Focus
instance may keep a thread live. Cross-instance movement is cold migration only; an
unverifiable loaded state is rejected. A runtime lease is not a user writer.

App-server subscription is a separate, connection-local fact. In the pinned
Codex `0.147.0` baseline, [`thread/unsubscribe` passes the request connection
into the effect](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/thread_processor.rs#L472-L479),
and [removes only that connection/thread pair from the reciprocal
indexes](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/thread_state.rs#L472-L504).
The canonical Focus adapter connection is therefore governed by its Web desire,
canonical Feishu subscribers, pending interactions, and exact runtime facts.
Each `fcodex` proxy has an independent app-server connection/subscription: a
canonical unsubscribe neither removes nor cancels it, and its presence does not
veto canonical cleanup. It may still retain the machine runtime independently.

### 6.2 Main-turn writer

`InteractionLeaseStore` answers only which Feishu next-turn/FIFO or
exclusive/autonomous action holds submission/activity identity. Idle has no owner;
only those lease-bearing effects acquire a blank before transport. Ordinary Web and
`fcodex` `turn/start` are upstream-routed realtime input: they neither read, acquire,
nor release a lease and cannot return writer denial from an existing foreign holder.
For a blank that does exist, a known-success ordinary `turn/start` response preserves
the authoritative upstream `turn.id` but never activates or transfers a Focus lease
from the response alone. Matching `turn/started` binds the local active lifecycle;
matching completion releases only the exact active lease. No surface
can correlate an unbound completion to one ordinary submission, so if started already
passed or was missed, the blank remains fail-closed until authoritative terminal
evidence or service restart. Feishu may additionally settle its exact ordinary-prompt
blank after an authoritative inactive-root reread, without binding a turn id or
attributing a completion. Its process-local admission token is a transaction receipt,
not a second writer fact. If ordinary fcodex start races an existing fcodex
exclusive/autonomous blank, later lifecycle carries no effect identity to distinguish
their sources and may still activate that blank. This accepted narrow upstream race
adds no correlation state machine. Inline-review response identity and compact's empty
response keep their method-specific paths. Children, pending server requests,
endpoints, delivery, and presentation do not extend the lease. Shared steer,
interrupt, and shared server-request response each use their own exact turn/request plus surface
authority; none derives from or transfers the writer.

Feishu active-observer `thread/resume` is another method-specific non-writer
state. It anchors a detached binding to the exact active turn in the response
only for later live presentation; it neither acquires nor transfers a main-turn
lease and creates no cancel, approval, or pending-request response authority.
Matching terminal settlement clears this observer provenance. The attached
binding remains, and a successor turn does not inherit observer identity.

Sharing one backend is necessary to avoid a live-runtime fork; it does not grant a
writer. See the [main-turn owner contract](../contracts/root-operation-owner.md).
Feishu FIFO continuity is a separate process-local ordering fact. It retains no
writer and cannot be used by another binding or root epoch.

`BindingRuntimeCoordinator` in `focus_runtime/binding_coordinator.py` adds no third
coordination axis. It groups 17 cross-owner binding/runtime transactions, preserves
the existing shared-lock critical sections and local-commit-before-effect order, and
observes Web retention only through a typed `Callable[[str], bool]` interest port.
Binding/session, transition, batch-deactivation, interaction-lease, and thread
runtime facts remain with their existing owners; the coordinator stores no mutable
fact. The other 12 methods removed from the former 29-method composition-root
cluster were direct-owner pass-throughs and now bypass `FocusRuntime`.

The Feishu runtime extraction adds no coordination axis either.
`FeishuPlatform` solely holds the attached Feishu bot fact and routes platform
lookups and presentation effects; `FeishuSurface` installs and dispatches inbound
message, command, and action routes through existing owners; and `TerminalResults`
coordinates terminal-result lookup, storage, and publication through typed ports.
`TerminalResultStore` remains the persisted fact owner, and queue ordering remains
with the existing Feishu execution-queue owners.

## 7. Main Operation Boundaries

| Operation | Current minimal boundary |
| --- | --- |
| inventory / read | read from the target instance's app-server; caches prove neither thread absence nor backend readiness. Web directory/open/history/inspection freezes exact document/target/backend/observation coordinates on RuntimeLoop, performs store/app-server reads and detached projection outside the loop, then settles and final-checks only through the original receipt. One service-ingress receipt spans the whole transaction as a shutdown barrier. A result superseded by notification, F5, or backend replacement cannot overwrite newer facts |
| `thread/start` | Web/Feishu share the canonical adapter: future persistent threads explicitly request and validate `historyMode=paginated`, while existing legacy threads are not migrated; the same-stack local callback follows the typed response. fcodex still forwards the upstream TUI payload unchanged and settles it with a current-generation capability; Focus does not automatically resend an unknown or paginated-unsupported create and does not quarantine other threads |
| `thread/resume` | send once after loaded-gate/runtime-lease admission; a process-local receipt joins success to the immediate local commit; unknown creates no durable marker. A call that may start autonomous work normally acquires an exact blank main-turn lease first. On a Web unknown outcome before any later turn-producing effect, only the exact blank freshly acquired by the current operation before resume is released while runtime interest remains; borrowed, activated, replaced, and still-recovery-required/stale acknowledged-incomplete leases stay intact. A known successful Web cold open commits runtime interest first; a later stale document, notification, projection, or backend-generation result cannot roll back or reclassify that resume. Another narrow exception is an admitted native fcodex attach beside an exact active Focus turn: it preserves that writer while accepting upstream goal-continuation semantics; native TUI settings/reviewer fields remain upstream-owned and do not become Focus effective-settings facts. The separate Feishu running-observer method-specific exception is fixed in the next row |
| canonical Web runtime cleanup | only the loss of the last Web desire with no pending interaction or canonical Feishu subscriber admits cleanup. `WebRuntimeLifecycleCoordinator` owns one per-thread flight and at most one coalesced successor. It freezes exact interest/backend generation, probes backend and the complete service-holder release record outside RuntimeLoop, claims an unsubscribe transition reciprocally fenced against same-thread canonical resume/start, rechecks before at most one unsubscribe send, rechecks before holder release, applies full-record holder CAS, and finalizes local interest/cache only while the original facts still match. New interest or a holder successor retains runtime; unknown/CAS mismatch does not replay or block another thread. Canonical `turn/start` receives typed known-no-effect before transport/settings mutation while unsubscribe is claimed; active canonical starts prevent the claim but remain mutually concurrent. Steer/interrupt and each `fcodex` independent connection are outside this fence. The final recheck-to-unsubscribe-send and recheck-to-holder-CAS windows are accepted bounded non-guarantees |
| Feishu active-observer `thread/resume` | a Feishu command/card origin still passes existing inbound-actor admission and group-`all` thread exclusivity; trusted CLI/control attach retains its existing admission and gains no stronger global group-chat check. Allow only authoritative direct-root active preflight for a detached binding, then exactly recheck active immediately pre-send. It neither acquires nor transfers a main-turn lease and carries no next-turn overrides. One local callback after the resume response commits both the attached binding and sole non-empty `inProgress` turn anchor under the shared lock. An already-idle response degrades to ordinary attached with no observer provenance/card; an active response without one exact turn id fails closed and restores detached. The observer page bootstraps only available assistant text from the response, discloses that pre-attach history may be incomplete, and then receives later live notifications. It neither displays nor automatically rejects upstream pending-request replay and gains no cancel or approval authority. Ordinary messages, `turn/start`, next-turn settings, and Feishu FIFO remain unchanged. Matching terminal settlement clears observer provenance, and a successor turn uses ordinary attached behavior. See the [Feishu thread lifecycle contract](../contracts/feishu-thread-lifecycle.md) and [`thread/resume` local-commit contract](../contracts/thread-resume-local-commit.md) |
| Web/`fcodex` main-turn start, steer, and interrupt | ordinary `turn/start` keeps upstream start-or-steer without reading, acquiring, or releasing the shared lease. An existing-thread Web prompt uses one exact browser mutation and bounded result receipt; fcodex uses an exact request token. If Web prepare freezes active A, the whole attempt calls `turn/steer` once for A only; only prepare with no exact active id calls `turn/start` once. A successor mismatch or no-active result never retargets or falls back. Review/compact and autonomous continuation retain their contract-specific exclusive blank. A connected/materialized Web document or qualified live/attached `fcodex` endpoint may steer or interrupt a direct-root turn without writer transfer. A non-empty fcodex interrupt id accepts a connection source or matching exact active lease as attachment proof; an empty id accepts only the current connection source and preserves upstream current/startup semantics. Neither effect scans descendants or retargets a successor turn |
| Feishu ordinary prompt start and FIFO | immediate, dequeued, and synthetic Feishu prompts send the complete input/settings payload through official `turn/start`. It normally starts a new turn while upstream is idle. In the narrow race where Web, `fcodex`, or autonomous goal continuation after resume becomes active first, upstream start-or-steer adds the Feishu input and turn settings to that active regular turn. The response preserves the authoritative upstream `turn.id`, but Focus waits for matching `turn/started` to activate the local lease/execution lifecycle. If that notification was missed, completion cannot bind the blank; the same `FeishuRootOperationController` may settle only its exact ordinary-prompt blank after an authoritative inactive-root reread, without attributing any completion. FIFO admission remains limited to an exact current execution anchor, existing same-binding/root/epoch continuity, or a current-process preprojection exact turn re-read under the binding lock; a start response never establishes continuity. Unknown/malformed outcomes remain blocked. See the [Feishu thread lifecycle contract](../contracts/feishu-thread-lifecycle.md) and [scheduled prompts](../contracts/scheduled-prompts.md) |
| explicit Feishu `/steer` | only `/steer <non-empty text>` enters an independent exact-effect owner; ordinary messages, attachments, FIFO, and settings paths remain unchanged. The owner freezes the exact thread/turn from the current attached/running binding, applies group-`all` exclusivity, an authoritative direct-root active reread, a connection-generation fence, and final binding/execution CAS, then calls official `turn/steer` once. An active observer is eligible; compact is locally rejected, while other upstream-owned rejections are classified as known no-effect. Transport/timeout/protocol or response-ID anomalies after dispatch report only unknown outcome, with no retry, fallback, or successor retargeting. The effect acquires no writer, lease, page, approval, or lifecycle authority |
| Web active-turn disclosure | read-only composition of the exact active turn, matching initiator lease, current Feishu subscribers, and turn-start-frozen effective-settings provenance; missing fields are unknown and instance-wide `WebNextTurnSettings` intent never masquerades as current facts |
| server-request response | callback lifetime belongs to app-server; Focus retains only current-generation identity and a one-shot surface token |
| backend reset | fence ingress, read-only capture current-process exact main-turn leases, confirm owned-child OS exit/wait, idempotently retire old leases/generation/local capabilities by full-record CAS and their surface owners, run binding detach and execution interruption/finalization only in that post-stop stage, clear this instance's runtime holders, start and verify replacement, then publish and admit it; an unconfirmed stop/retirement/projection stays fenced and starts no replacement |

Backend reset is not writer handoff. It does not migrate old callbacks or recover an
unknown request. The lease capture contains only the current service PID and exact
process identity; reset must not clear another PID, PID zero, or a successor
generation installed after capture. Presentation is a best-effort consequence after
the reset transaction.

An existing-thread Web prompt sends one POST. RuntimeLoop performs only
prepare/settle; metadata, attachment I/O, and the sole upstream RPC run on the
external worker that retains the original service-ingress receipt. The
process-local `WebPromptResultRegistry` stores a bounded result receipt by exact
`(thread_id, mutation_id)`, with no payload, persistence, replay, runtime lease,
or writer authority. F5/poll may only GET that receipt. Eviction, service
restart, or backend replacement makes the local evidence unavailable; it proves
no negative effect and grants no replay. See the [Focus Web prompt mutation
recovery contract](../contracts/focus-web-prompt-mutation-recovery.md).

## 8. Credential Boundaries

These credentials are never interchangeable:

- control-plane/service token: local management and instance discovery;
- backend websocket token: connection to the instance app-server;
- proxy token: one local websocket launched by `focus` / `fcodex`;
- Web bootstrap token: one-time exchange for a loopback browser session.

Local token and cross-process lock files must be regular files. Focus opens them
without following a final symlink and rechecks the opened descriptor against the
directory entry. A symlink, non-regular file, or observed path replacement fails
closed instead of becoming local authority for an external file.

Browsers receive neither backend nor control-plane credentials. Python attached
clients explicitly bypass user external websocket proxies. The wrapper only adds
loopback entries to `NO_PROXY/no_proxy`; it does not remove external proxy settings.

## 9. Known Boundaries

- Bare Codex and unregistered backends are not coordinated by Focus runtime or
  main-turn leases.
- Upstream `--remote` wire shape, connection sequence, and `thread/start` payload may
  change; proxy behavior must be re-audited on Codex upgrades.
- The TUI thread picker belongs upstream and may differ from Feishu/Web/`focusctl`
  inventory.
- Upstream exposes no separate pure-subscribe RPC. An active observer reuses
  running `thread/resume`, so its response may already be idle after the exact
  active pre-send check. The resume snapshot provides only available assistant
  text and does not promise replay of each pre-attach command/tool delta; a
  mid-turn Feishu attach explicitly accepts this bounded history gap.
- Gateway remains loopback-only. External Web access is a separate authentication
  and deployment contract and cannot be implemented by exposing app-server.
- On macOS, a tool/MCP descendant that creates a new session/group is outside the
  supported containment set and may outlive stop, reset, or service crash cleanup.
- A missing/mismatched cleanup receipt and a legacy direct-child record require
  independent, platform-bounded process inspection followed by exact-record manual
  cleanup; there is intentionally no command that converts this unknown state into
  proof.

## 10. Code Entry Points

- composition root: `focus_runtime/runtime.py` (`FocusRuntime`)
- backend lifecycle: `owned_app_server_guard.py`,
  `stores/app_server_runtime_store.py`, `service_runtime_lifecycle.py`
- adapter/generation: `adapters/codex_app_server.py`, `adapter_ingress_gate.py`,
  `codex_protocol/client.py`
- wrapper/proxy: `fcodex/cli.py`, `fcodex/proxy.py`
- fcodex owners: `fcodex/main_turn_owner.py`, `fcodex/operation_service.py`,
  `fcodex/participant_runtime_registry.py`, `fcodex/interaction_inbox.py`
- runtime coordination: `focus_runtime/service_authority.py`,
  `focus_runtime/binding_coordinator.py` (`BindingRuntimeCoordinator`: 17 stateless
  cross-owner binding/runtime transactions preserving shared-lock critical sections
  and commit/effect order),
  `focus_runtime/thread_targets.py` (`CodexThreadTargetService`: authoritative
  thread-target reads, validation and selection, Web resume-target coordination,
  existing error classification, and interrupt forwarding),
  `thread_runtime_coordination.py`,
  `stores/thread_runtime_lease_store.py`, `stores/instance_registry_store.py`
- Feishu runtime surface: `focus_runtime/feishu_platform.py` (the attached bot
  fact and platform routing), `focus_runtime/feishu_surface.py` (inbound route
  installation and dispatch), `focus_runtime/terminal_results.py` (terminal-result
  persistence/publication coordination; persisted facts stay in
  `stores/terminal_result_store.py`)
- create/resume: `thread_create_transaction.py`, `thread_runtime_authority.py`
- Feishu active-observer attach: `feishu_active_observer.py`,
  `feishu_thread_session_coordinator.py`,
  `focus_runtime/feishu_thread_session_composition.py`,
  `binding_execution_runtime.py`, `runtime_admin/binding_application.py`
- Feishu start/FIFO: `prompt_turn_entry_controller.py`,
  `feishu_execution_queue.py`, `feishu_execution_queue_service.py`,
  `thread_access_policy.py`
- explicit Feishu steer: `feishu_turn_steer.py`,
  `focus_runtime/feishu_surface.py`
- Web Gateway/request admission/recovery: `bot/web_runtime/gateway.py`,
  `bot/web_runtime/gateway_external_transaction.py`,
  `bot/web_runtime/thread_open_coordinator.py`,
  `bot/web_runtime/thread_read_projection.py`,
  `bot/web_runtime/thread_inspection.py`,
  `bot/web_runtime/direct_thread_target_coordinator.py`,
  `bot/web_runtime/gateway_request_admission.py`, `bot/web_runtime/auth.py`,
  `bot/web_runtime/controller.py`, `bot/web_runtime/turn_command_coordinator.py`,
  `bot/web_runtime/operation_service.py`, `bot/web_runtime/mutation_recovery.py`
