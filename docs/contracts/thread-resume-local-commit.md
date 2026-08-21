# `thread/resume` Local-Commit Contract

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/thread-resume-local-commit.zh-CN.md`.

> Status: accepted cross-entry contract. This document defines the immediate
> boundary between one `thread/resume` call and its local Focus commit. It does
> not define a durable recovery state machine or a thread-wide mutation gate.

## 1. Scope and Upstream Baseline

Codex app-server can change connection-local subscription state before a
successful `thread/resume` response is returned. Focus must therefore order two
facts without pretending that they form a durable distributed transaction:

1. upstream returned a typed successful resume response;
2. the requesting Focus surface committed its immediate local consequence.

The reviewed public upstream baseline is Codex CLI `0.147.0`:
[`openai/codex@be6e8eac029b183056b7e4402879f15d2c85f61b`](https://github.com/openai/codex/commit/be6e8eac029b183056b7e4402879f15d2c85f61b).
Its [running resume owner](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/thread_processor.rs#L3480-L3734)
captures the active snapshot, adds the connection subscriber, and returns the
response in one listener order. The [atomic subscriber/response ordering](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/thread_lifecycle.rs#L528-L755)
precedes later live notifications, the goal snapshot, and pending
server-request replay. Upstream does not persist a frontend resume journal or
quarantine later thread mutations after a response is lost. Focus follows that
baseline.

This contract applies to snapshot and paged resumes issued through
`ThreadRuntimeAuthority`, including Feishu attach/goal flows, Web cold open,
and RuntimeAdmin attach. Main-turn admission remains governed by
`root-operation-owner`; a resume that may autonomously start a turn must hold
the ordinary exact blank main-turn lease. The sole method-specific exception is
an active-observer attach for a detached Feishu binding after both authoritative
preflight and the immediate pre-send guard confirm an active direct root. That
request carries no model, approval, or permissions overrides and neither
acquires nor transfers the writer.

## 2. The Only Resume Transaction

```text
acquire or observe the machine runtime lease
  -> invalidate explicitly carried settings intent and prepare the exact pre-send guard
  -> send one typed thread/resume request
  -> receive a valid success response
  -> record the response-side effective settings
  -> PendingThreadResume.commit_local_state(...)
```

`PendingThreadResume` carries one opaque, process-local receipt. The receipt
belongs to one authority, thread, and generation. The authority consumes it
before invoking the local callback, so a stale or duplicate handle cannot run
the callback again. Connection invalidation or confirmed backend reset makes a
delayed receipt stale.

The receipt is only an immediate call-stack capability. It is not persisted,
projected in operator status, reconstructed after restart, or used as a
post-settlement block on a later resume or another thread mutation.

One deliberately narrow in-process effect fence exists while an exact call is
still unsettled. A same-thread prepared resume or any active canonical direct
`turn/start` token prevents an unsubscribe claim. Once an unsubscribe claim is
prepared, a new resume and canonical `turn/start` fail before transport until
that claim retires; a known-success claim retains the slot through its exact
local cleanup commit or abandonment. Start also fails before effective-settings
mutation. This is reciprocal only around
canonical unsubscribe: resume and start do not serialize each other, multiple
same-thread starts may overlap, and another thread, steer, interrupt, or an
`fcodex` proxy's independent connection is unaffected. The fence prevents the
canonical Focus connection from knowingly crossing subscription removal with
those effects; it creates no durable mutation gate or replay authority.

The local callback contains only the first authoritative local write required
by that surface, for example:

- commit the exact Feishu binding or admitted blank main-turn owner; an
  active-observer attach commits both the attached binding and the sole
  non-empty `inProgress` turn anchor from the response in the same callback;
- mark the Web runtime interest confirmed for the requesting document;
- commit the RuntimeAdmin attach result.

Cache, history, title, goal, card, and projection refreshes are later
consequences. Their failure does not undo an already committed resume. Opening
the observer card is likewise post-commit presentation, but the exact anchor is
not presentation: Focus must not report an attached observer after a missing or
failed anchor commit.

A Web cold open may wrap this resume transaction in a staged read, but it does
not move the commit point above. The outer operation first freezes the exact
document, thread, read observation, and backend generation on RuntimeLoop, then
reads metadata and goal state outside the loop. It returns to RuntimeLoop to
choose a passive read or prepare one exact resume, and performs the bounded page
read/resume plus detached DTO-input preparation outside the loop. When resume
returns known success, the next RuntimeLoop settlement commits runtime interest
through `PendingThreadResume` before it may claim the read model and projection.
DTO materialization may then leave the loop again, followed by a final
RuntimeLoop check of the original document, backend generation, projection
revision, and read observation.

Consequently, a final DTO check rejected by an F5/document reissue, newer
notification, projection revision, or backend replacement means only that the
response is no longer installable. A known-success resume whose local commit
finished remains effective. Focus neither compensates it, reclassifies it as
transport-unknown, nor restores the replaced document's old
selection/materialization because the DTO became stale. The browser may reread
under new authoritative coordinates.

A running resume response may already be idle because the turn completed in the
narrow race. The observer attach may commit the binding without fabricating an
execution anchor. If the response still reports active but has no sole non-empty
active turn id, the local callback fails closed and restores the binding to
detached. An anchored observer consumes only live events after attach. Upstream
does not promise replay of earlier command/tool deltas, so its execution page
explicitly says that prior progress may be incomplete.

## 3. Failure Boundaries

### Before the adapter call

A runtime-lease acquisition failure, local model-preparation failure, or exact
guard rejection is known pre-send. Focus releases only a runtime lease newly
acquired by this attempt and returns the original typed failure. It does not
run the transport outcome classifier.

### Adapter result is known not to have had an effect

Focus releases only a newly acquired runtime lease and returns the original
adapter error. A later call is a new explicit action.

### Adapter result is unknown

Focus raises `ThreadResumeOutcomeUnknown`, retains the machine runtime lease,
and does not automatically retry. The exception identifies only this request
and receipt. No recovery marker, retry generation, mutation quarantine, or
operator action is created.

Web has one narrower, method-specific settlement rule. If the current Web
operation freshly acquired an exact blank main-turn lease before this resume
and no later `turn/start`, review, or compact effect was called, an unknown
outcome releases that blank through a full-generation compare-and-set while
retaining `unknown` runtime interest. A pre-existing, borrowed,
lifecycle-activated, or replaced lease is never removed by this cleanup. An
acknowledged resume whose local commit failed is not transport-unknown: it
continues through the existing `RETAIN` / `COMPENSATE` settlement and surface
rules. This exception does not change acknowledged settlement: only an
incomplete settlement that is `recovery_required` or
`STALE_OR_INVARIANT_VIOLATION` retains the corresponding Web blank; another
settled known failure may still clean its exact fresh main-turn blank under the
existing surface rule.

The user can explicitly inspect or resume the thread again. That new action is
not blocked by the old unknown call.
Focus never initiates that retry automatically. This honestly accepts one
very narrow window: the first resume may already have started autonomous goal
work, while the lost response and not-yet-observed lifecycle cannot prove it.
Between releasing the blank and observing authoritative `turn/started`, an
explicit retry can meet that upstream work. Focus does not disguise this
unobservable race as a durable writer or recovery state.

### Successful response, local failure

The authority consumes the exact receipt and raises
`ThreadResumeLocalCommitFailed`. The error carries the original exception,
the selected policy, the exact generation, a settlement outcome, and whether
the calling surface must retain its own exact local effect. It creates no
authority-owned recovery registry.

The response-side effective-settings write follows the same rule: its failure is
an acknowledged local failure, not an unknown upstream response.

An active-observer snapshot/anchor failure is also an acknowledged local
failure. Under the shared lock, the caller first stages a transient anchor in
the still-detached resident and only then commits the attached binding with one
durable write. If that write fails, it rolls back only the process-local staged
anchor and performs no second durable rollback write. It then applies
`COMPENSATE` to this runtime receipt. It never retries running resume
automatically or leaves an attached binding without an anchor for an ordinary
prompt to use.

A cache, projection, or final-DTO stale failure in the outer Web staged read
after the resume local commit is not `ThreadResumeLocalCommitFailed`. It retains
the committed runtime interest and rejects only that obsolete read response.

## 4. Local-Failure Policies

Every caller explicitly chooses one policy:

- `RETAIN` keeps the subscription/runtime lease. Use it whenever resume may
  continue autonomous work or the caller cannot prove cleanup is safe.
- `COMPENSATE` may unsubscribe, clear the effective-settings facts, and release the runtime
  lease only when this receipt proves that the lease was newly acquired. A
  pre-existing lease is never cleaned up by this attempt.

Failure outcomes are deliberately small:

| Outcome | Meaning |
| --- | --- |
| `COMPENSATED` | All safe cleanup steps completed for this newly acquired lease. |
| `RETAINED` | The runtime effect was left in place; the caller receives the exact failure. |
| `CLEANUP_PENDING` | One cleanup effect was not confirmed; the exact call reports that uncertainty. |
| `STALE_OR_INVARIANT_VIOLATION` | The receipt was foreign, consumed, or invalidated; the callback did not run. |

Success returns the callback result directly. None of these outcomes grants a
thread-wide lock, durable recovery authority, or automatic replay right.

## 5. Ownership and Reset

`ThreadRuntimeAuthority` owns only adapter ordering, response-side effective-settings facts,
and immediate receipt consumption. Surface owners decide what their local
callback commits and how an exact local unknown is presented.

Connection invalidation and confirmed backend reset both clear connection-local
effective-settings facts and invalidate delayed receipts.
Neither operation clears or replays an old durable resume journal because no
such journal exists. Machine runtime leases remain governed by their own
runtime contract.

## 6. Regression Requirements

Tests must prove:

- local preparation failure is classified as pre-send and releases only a new
  lease;
- known-no-effect and unknown adapter failures remain distinct;
- an unknown result retains the machine runtime lease but does not block later
  explicit calls or unrelated mutations;
- Web resume unknown releases only the current operation's fresh exact blank;
  borrowed, activated, replaced, and still-recovery-required/stale
  acknowledged-incomplete leases stay intact;
- a successful receipt runs one exact local callback at most once;
- a same-thread prepared resume or active canonical start prevents unsubscribe,
  while a prepared unsubscribe rejects resume and canonical start before
  transport/settings mutation and retains a known-success slot through exact
  local commit or abandonment;
- the unsubscribe fence does not serialize multiple same-thread starts or
  affect another thread, steer, interrupt, or an independent `fcodex`
  connection;
- `RETAIN` reports the exact failure without installing a recovery registry;
- `COMPENSATE` cleans only a newly acquired lease and reports partial cleanup;
- connection/reset invalidates delayed receipts;
- snapshot and paged resume use the same contract;
- active observer sends only after the exact active pre-send guard and acquires
  no writer; an idle response race fabricates no anchor, while an active response
  without one exact turn restores detached state;
- observer binding and anchor commit in one local callback, while card failure
  degrades presentation only;
- a failed durable observer-attach write leaves both binding and store detached,
  clears the transient anchor, and does not depend on a second store write for
  rollback;
- the observer page discloses incomplete pre-attach process history, and pending
  request replay grants it neither cancel nor approval authority;
- post-commit projection failure cannot roll back committed local ownership;
- after a known Web cold-open resume, document reissue, a newer notification,
  projection revision, or backend replacement rejects a stale DTO while
  retaining confirmed runtime interest, and an explicit reread is a new
  operation.

Related contracts:

- `docs/contracts/root-operation-owner.md`
- `docs/contracts/thread-create-local-commit.md`
- `docs/contracts/feishu-thread-lifecycle.md`
- `docs/contracts/fcodex-operation-owner.md`
- `docs/architecture/focus-shared-backend-runtime.md`
