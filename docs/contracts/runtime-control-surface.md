# Runtime Control Surface Contract

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/runtime-control-surface.zh-CN.md`.

This document defines the formal semantics of the Feishu-side control surface
and its boundary with Web settings. It deliberately does not define a shared
cross-frontend settings system.

## 1. Feishu and Web writable setting families remain separate

### 1.1 Binding-wise next-turn settings

Entry points:

- `/model`
- `/effort`
- `/approval`
- `/permissions`

Semantics:

- manage overrides for future turns of the current Feishu binding
- are primarily consumed at `turn/start`
- on unloaded-thread recovery, cold `thread/resume` may also carry a narrow
  one-shot subset for the first post-resume autonomous turn
- do not write any project-owned thread-level persisted state

The preference store is not a cross-frontend write bypass. Before a Feishu
binding-wise setting is applied through continuation-capable `thread/resume`
or Feishu `turn/start`, that path must acquire the exact blank submission lease
from `root-operation-owner`. A refusal must not be converted into an implicit
resume, queued setting application, or takeover. Ordinary Web/`fcodex` input
does not consume this binding setting; it keeps its own upstream-routed settings
semantics and acquires no lease for that reason.

The cold-resume wording above does not make a setting-carrying `thread/resume`
an observer-safe delivery path. A persisted active goal can continue after resume, and
an empty, unreadable, future, or otherwise unrecognized goal status must fail
closed too. Such a continuation-capable resume first needs the exact blank lease
under `root-operation-owner.md`. Only an authoritative preflight proving a
reviewed non-continuing status, no goal, or disabled Goals may use the passive
subscription path without a main-turn lease. An admitted native fcodex attach
beside an exact active Focus turn is the narrow exception: it preserves that
writer while following upstream goal-continuation semantics. Its TUI-owned
start/resume settings are forwarded semantically unchanged, but neither the
request nor the response becomes a Focus effective-settings fact; upstream may
ignore an override for an already-loaded thread. See the canonical
[fcodex operation owner](./fcodex-operation-owner.md#direct-thread-targets).

### 1.2 Web has separate instance-wide next-turn settings

Focus Web's model, reasoning effort, approval policy, and permissions profile
belong to one durable `WebNextTurnSettings`. Every browser, post-F5 document,
and thread in the same instance shares it. It belongs to no `client_id`,
selected thread, or main-turn writer and does not automatically merge with a
Feishu binding or local `focus` / `fcodex` TUI state.

The canonical [runtime-settings fact-source
contract](./runtime-settings-fact-sources.md) alone defines seed, persistence,
merge/generation, consumers, and bounded backend fallback. This control-surface
contract adds only the UI boundary: settings controls remain editable during
an active turn and explicitly apply to the next eligible Web turn. Main-turn
admission decides when a snapshot may be consumed; it neither grants nor blocks
the instance-wide settings mutation.

In the separate Web navigation profile, `selected_thread_id` is the sole
semantic selection fact.
It determines meta, `/cd`'s previous scope, attachment admission, and scope
generation. `WebDocumentRegistry.materialized_thread_id` is only current-
process evidence that Focus successfully installed a selection. Older-history
admission therefore requires both facts to name the requested target; the
materialized value cannot authorize a target which the durable profile does
not select. `WebRuntimeInterestRegistry` alone owns desired-client edges and
subscription outcome; neither selection fact is a substitute for it.

When upstream authority makes a selected target unusable, Focus compares and
clears only an exact durable match. That store transaction changes selection
to the draft and increments `scope_generation` atomically; replaying the same
event is a no-op. Process cleanup forgets a materialized value only when it
still names that target, preserves a replacement materialization, and removes
all desired runtime edges for every document whose durable selection was
cleared. It never automatically rebinds pending attachments. Archive,
not-found, and loaded-elsewhere outcomes leave old thread-scope records
isolated; confirmed delete and an invalid direct `ThreadSpawn` target delete
that thread scope. Only after the durable commit does Focus publish a
`profile_changed` invalidation. The event carries no profile copy; each browser
re-reads its own meta before using the new navigation generation. That
generation neither orders nor settles settings generation.

## 2. Removed Feishu setting surfaces

The following entry points are no longer part of the formal project contract:

- legacy project-owned profile commands
- `/memory`
- any thread-wise memory control surface

If an operator wants to change process-level upstream capabilities such as
profile/provider or memory behavior, they must do it through upstream Codex
itself rather than a project-owned Feishu setting surface.

## 3. Other core state axes

Independent from settings, the control surface still separates three state
axes:

1. `binding`
   - which thread the current chat logically points to
2. `attach / detach`
   - whether the current chat receives Feishu push for that thread
3. `backend / live runtime`
   - whether the thread is loaded in the backend, and who currently owns live
     runtime

Those axes are parallel to settings and must not be conflated with them.

## 4. Formal semantics of Feishu turn-time settings

`/model`, `/effort`, `/approval`, `/permissions`:

- belong to the current binding's next-turn settings
- read back the current binding's persisted configuration facts by default
- are not the instance baseline
- are not thread-level persisted truth

Within that family:

- for `/model` and `/effort`, `auto` means "do not explicitly override"
- it no longer maps to any project-owned thread-level fallback state
- Focus treats model and effort as one constrained pair:
  - `validated`: effort is `auto`, or metadata for the explicit model advertises that effort
  - `deferred`: model is `auto`, or the explicit model has no usable metadata; Focus passes the explicit effort through and lets app-server decide
  - `rejected`: metadata exists for the explicit model and does not advertise the explicit effort
- `/model`, `/effort`, and card actions refuse newly requested `rejected` pairs; existing binding values are not migrated, and turn dispatch does not rewrite old values
- known canonical effort input is normalized to lowercase; unknown/custom effort only has surrounding whitespace trimmed and otherwise preserves case
- `ultra` is sent to Codex unchanged; Focus does not translate it to `max` or construct `collaborationMode`
- after explicit model / effort values are sent with a turn, they can affect the shared upstream thread's current and subsequent turns; local `focus` / `fcodex` may observe or overwrite that upstream state
- `auto` only means Focus omits the field; it does not restore `.codex/config.toml`, the model default, or an older value from another frontend
- model / effort are optional overrides: Focus reapplies only non-`auto` fields on each turn, while `auto` continues with the shared upstream thread's current state
- for `/approval` and `/permissions`, the persisted binding value is the
  safety baseline; a new binding is seeded from instance config, and once it is
  persisted it does not drift with later instance-default changes
- approval / permissions have no `auto` state: Focus explicitly reapplies the binding's safety baseline on every turn; another frontend may change the upstream thread, but the next Feishu turn applies this binding's value again

## 5. Side-effect boundary of reset-backend

`reset-backend` is a recovery/admin tool, not a routine settings-apply path.
Typical uses are:

- discard this instance's stale loaded runtime before cold continuation
  elsewhere
- rebuild this instance's backend view after the same persisted thread was
  changed outside this project, for example by bare upstream `codex`

When an instance resets its backend:

- every reset ingress uses the same Focus-owned exact `force` selector:
  an absent field means `false`, while a present field must be a JSON boolean;
  strings, numbers, `null`, arrays, and objects are rejected before any reset
  runtime access or effect, and the service repeats the exact-bool assertion at
  its entry boundary
- `BackendResetService` owns the product order, while
  `BackendResetInteractionCoordinator` owns the bounded four-surface
  preparation and
  `BackendResetCoordinator` owns the narrower backend-epoch replacement
  transaction; neither duplicates binding, interaction, runtime-lease, or
  adapter state
- the backend process restarts
- the old websocket generation and ordinary outbound RPC admission are fenced
  first; Focus keeps the existing pre-detach cancellation of every binding's
  in-process prompt/compact FIFO, while active-turn interruption itself waits
  for confirmed stop
- the process-local server-request pending count is captured for diagnostics;
  reset does not attempt a pre-stop response/fail-close transaction
- structural inventory or diagnostic capture failure aborts before stop and
  leaves ingress fenced; final card/result presentation is best-effort
- after ingress is fenced and before stopping the owned backend,
  `InteractionLeaseStore` read-only captures every full `InteractionLease`
  generation owned by the current service PID and exact process identity; if
  that capture cannot be obtained, the backend is not stopped
- only after owned-child OS exit/wait is confirmed does Focus retire that
  capture by full-record CAS while preserving other PIDs, PID zero, and any
  successor generation installed after capture. The same stage then retires
  the process-local registry, fcodex facts, and active Web mutations; runs
  binding detach plus execution interruption/finalization; and only then
  retires Feishu root admission/continuation/candidate facts and Feishu request
  capabilities. Every owner-local call is idempotent. A structural projection
  failure keeps ingress fenced and starts no replacement. Focus finally rotates
  transport response authority before any replacement starts
- an unknown or failed stop retires none of those facts. If any authoritative
  retirement cannot be confirmed, ingress remains fenced, no replacement is
  started, success is not returned, and the same retirement may be retried
  idempotently from the beginning
- connection-local effective-settings facts, transient event projections, and
  auto-resolution timers are invalidated, and delayed old-generation
  callbacks are rejected
- binding records stay
- binding-wise next-turn settings stay
- exact process-local request/effect records tied to the old backend generation
  are retired with that generation; there is no durable retained-operation,
  server-request settlement, or interaction-lineage fence to carry forward
- Web retirement removes the bounded process-local ordinary-prompt result
  receipts with the old backend generation. A browser locator may survive, but
  its later GET miss reports only unavailable/unknown outcome and establishes no
  durable explanation, retry, replay, or payload restoration. The exact-generation
  fence prevents an old staged worker from acting on or settling the replacement
  backend
- any unproved binding/FIFO cleanup or backend-epoch replacement step keeps
  all ordinary ingress and outbound RPC closed and forbids replacement; only
  successful validation and publication of a strictly newer generation
  reopens them

The result reports `retired_request_count`, the process-local pending count
captured before machine stop. It is diagnostic inventory, not proof that each
request was answered or resolved upstream.

A reset may be projected as successful only after its complete method result
has been admitted. The result is an exact object containing only `force`,
`detached_binding_ids`, `interrupted_binding_ids`, `retired_request_count`,
`purged_thread_ids`, `projection_warnings`, and `app_server_url`. `force` is an
exact boolean matching the request, `retired_request_count` is a non-negative
integer that excludes booleans, the four list fields are arrays of non-empty
strings, and the trimmed backend address is non-empty. Focus does not infer URL
grammar, ordering, uniqueness, or relationships between those lists, and it
does not report response-write counts that the reset transaction does not
produce.

The trusted-local Web surface exposes only the Gateway's current instance. An
authenticated current browser document may read `GET /api/backend-reset`; a
same-origin, CSRF-authorized `POST` to the same path accepts exactly `force`
and `expected_connection_generation`, never an instance, backend address, or
token. The preview is only a snapshot. An executable preview requires the
physical websocket and adapter-ingress generations to be the same positive
safe integer, with reset, cleanup, and queued-disconnect fences all open.

Web execution rechecks policy before effects and linearizes the stale check in
the fixed `CodexRpcConnection identity lock -> AdapterIngressGate lock` order.
Both generations must still equal the submitted value before the gate advances
or drains. A mismatch or already-closed/reconciling gate is a typed known
no-effect conflict. The CLI and Feishu path supplies no expected generation and
retains its existing ability to recover generation-zero or sticky-cleanup
states. Once the Web fence starts, any exception is outcome-unknown; it must not
be automatically retried. A Web success is projected only after the complete
seven-field result above decodes, and the browser projection contains counts
and warnings but never `app_server_url`.

If a control response reports `ok: true` but carries a missing, extra,
malformed, or request-mismatched result, the reset may already have happened.
The CLI emits no success projection, classifies the outcome as unknown, and
exits `3`; the Feishu action replaces the confirmation with a warning card that
has no reset or attach action. Neither surface retries automatically. A later
status or preview for the same target instance can inspect current state but
cannot prove the outcome of the earlier request.

Reset-backend does not:

- rewrite thread history
- automatically re-attach every chat
- preserve or transfer an old approval/question response path
- send automatic pre-stop answers on the user's behalf
- manufacture `serverRequest/resolved` for a discarded interaction epoch or
  otherwise claim that each retired request resolved upstream
- upgrade binding settings into thread-level settings
- act as a profile-switch surface

## 6. What Feishu `/status` should show

`/status` and related diagnostics should show separately:

- the current binding's next-turn overrides
- attach/detach state
- live-runtime / loaded state

They should no longer present:

- a project-owned profile setting
- a thread-wise memory setting
- "extra config that this project will inject on the next resume"
