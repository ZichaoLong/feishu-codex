# Cross-Instance Live Runtime Admission

Document role: synchronized English peer. Canonical Chinese: `docs/decisions/cross-instance-live-runtime-admission.zh-CN.md`.

See also:

- `docs/contracts/root-operation-owner.md`
- `docs/contracts/runtime-control-surface.md`
- `docs/contracts/local-command-and-thread-profile-contract.md`
- `docs/architecture/focus-shared-backend-runtime.md`

## 1. Status

This is an accepted implementation contract for registered local Focus and
`fcodex` runtimes. The current runtime applies its loaded-gate-plus-lease
admission model to the covered attach, resume, and lifecycle paths; the exact
entry-point matrix remains in the linked contracts.

It does not claim to detect, lock, or coordinate a bare Codex client, an IDE,
another machine, or any other unregistered app-server. Those remain outside
the local coordination boundary.

## 2. Problem

The current machine-level `ThreadRuntimeLease` is not strong enough to be the
only cross-instance safety gate.

Reason:

- upstream app-server keeps a thread loaded for about 30 minutes after the last
  subscriber unsubscribes
- a later `thread/resume` may reuse that already-loaded in-memory thread
- therefore `lease == none` does not imply `backend == notLoaded`

That creates a real stale-loaded risk across instances: one instance can keep a
thread loaded in memory after another instance has already advanced persisted
history.

## 3. Decision

### 3.1 Product contract

- thread visibility remains global
- live continuation is instance-exclusive
- cross-instance migration is `cold migration only`
- no cross-instance live takeover or automatic transfer is supported

### 3.2 Admission model

This section answers which instance backend may hold a live runtime; it does
not admit a concrete effect. Prompt, resume, goal, interrupt, server request,
and other effects remain governed by their respective formal contracts. The
two runtime checks below cannot replace any method-specific authority:

1. `global loaded gate`
   - before attach / resume across instances, the system must verify whether
     another running instance still reports the target thread as loaded
   - if another running instance still reports it as loaded, reject
   - if the system cannot verify that fact, reject
2. atomic `ThreadRuntimeLease` claim
   - after the loaded gate passes, the instance must still acquire the machine
     level runtime lease before continuing
   - this is kept to prevent concurrent resume races between two instances that
     both observe a not-loaded state at nearly the same time

### 3.3 Meaning of `ThreadRuntimeLease`

`ThreadRuntimeLease` stays as an internal coordination primitive, but its role
is narrowed:

- it is not the sole source of truth for cross-instance safety admission
- it is the atomic machine-level claim that prevents racing cold-resume /
  backend-materialization attempts
- it carries holder metadata such as `service` vs `fcodex`

User-facing mental model should prefer:

- "another running instance still has this thread loaded"
- not "another instance owns the lease"

### 3.4 Durable coordination-state availability

The instance registry and thread-runtime lease ledger are admission authority,
not disposable caches. Their persistence contract is:

- a missing state file means that no records have been created
- a malformed, unreadable, structurally invalid, or unsupported future schema
  means `unavailable`, never an empty registry or lease set
- registry discovery must fail the global loaded gate when that state is
  unavailable, and an unavailable runtime-lease ledger must reject a claim
- ordinary reads, registration, claims, releases, and pruning must not repair
  or overwrite unavailable state as a side effect; recovery requires an
  explicit operator repair or cleanup decision
- the valid historical unversioned shape is accepted only as a one-way upgrade
  input; every new write carries the current schema version

PID existence alone is not process identity because a PID can be reused. New
coordination records bind a positive owner PID to an OS process-incarnation
identity. Automatic stale pruning is allowed when the PID is gone or that
identity conclusively differs. If the platform cannot inspect an identity, the
record is retained conservatively; uncertainty must not manufacture admission.

### 3.5 Web directory projection and exact open

Loaded labels in the Web global directory use process-local loaded inventories
from the same set of managed running local instances; a runtime lease is not a
complete inventory. Each per-instance query distinguishes a verified result
from an error, and an error is never interpreted as an empty set. The directory
may aggregate those verified snapshots per request, but this is advisory
presentation only: it is neither persisted nor polled and does not replace the
exact loaded gate.

Accordingly, a root thread that remains idle-loaded elsewhere is shown as such
even without a lease. An instance whose state cannot be verified is shown as
unknown/unverified and cannot be used to materialize that thread from the
directory. Because the ordinary persisted list is bounded, verified remote
loaded ids absent from that page may receive metadata-only reads capped by the
same directory owner limit; the existing root-only filter still applies, and an
explicit search is not widened by those reads.

State may change between listing and clicking, so final admission reruns the
Section 3.2 loaded gate for the exact thread. A proven loaded-elsewhere denial
uses HTTP 409 `thread_loaded_elsewhere`. A registry, control-plane, or other
loaded-state verification failure—including an atomic runtime-lease claim that
loses a race after the exact loaded check—instead uses HTTP 503
`thread_runtime_unverified`, retains the current Web selection, and performs no
resume or lease mutation. Internal typing or presentation must not turn either
case into a 500 or claim a proven owner when none was observed. This projection
still covers only managed Focus instances in the local registry.

The strict `thread/loaded/list` control read does not re-enter the target
instance's `RuntimeLoop`: the requesting Web list already owns its own loop
while waiting for peer responses, so two simultaneous global lists would
otherwise wait on each other. The control request reads through the target's
thread-safe RPC owner using only its existing connection and a timeout shorter
than the fan-out's single total deadline. Like an ordinary outbound call, this
read obtains the current outbound epoch permit, carries its actual-send guard,
and only accepts a success or decoded JSON-RPC error while that exact epoch is
still current. It cannot start or reconnect a backend, creates no snapshot
owner, and remains drained by control-plane shutdown before adapter shutdown.

## 4. Attach Contract

### 4.1 Binding / thread / service attach

All attach-style operations must obey the same loaded gate. A resume, goal, or
other effect performed afterward must still pass its method-specific authority;
this gate decides backend residency only.

- `binding attach` is admitted only if the target thread passes the gate
- `thread attach` is admitted only if the target thread passes the gate
- `service attach` is an instance-level batch restore, but failure is decided at
  thread granularity

### 4.2 Service attach result shape

`service attach` should behave as:

- batch restore all detached bindings in the current instance
- group work by thread
- each thread is either fully restored for this instance or fully blocked
- partial success across different threads is allowed
- blocked threads must be listed explicitly with reasons

This means:

- instance-level batch restore
- thread-level fail-close
- result-level partial success

## 5. Operational Implications

- no automatic cross-instance continuation when another running instance may
  still hold a loaded in-memory copy
- source-instance reset, idle unload, or explicit cold migration workflow is
  acceptable
- convenience must lose to fail-close when the loaded state cannot be proven

## 6. Covered Paths and Maintenance Rule

The current implementation applies this decision to:

- Feishu attach paths
- detached binding auto-attach / re-attach paths
- local `focus resume` / `fcodex resume` routing where cross-instance loaded conflicts matter
- status / rejection text so users see "loaded elsewhere" rather than lease-only
  language

Any new path that can load, resume, attach, or otherwise materialize a live
thread runtime must join the same loaded gate, atomic lease claim, and the
formal admission contract for that effect before it reaches app-server.
