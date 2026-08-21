# Scheduled Resume and Synthetic Prompt Contract

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/scheduled-prompts.zh-CN.md`.

This document defines the current minimal contract for "continue the same
Feishu-bound thread at a future time".

It covers three layers:

- service control plane: `binding/submit-prompt`
- local CLI: `focusctl prompt send`
- Linux `systemd --user` managed skill: `feishu-scheduled-prompts`

## 1. Goal

The supported product shape is not a built-in scheduler subsystem. It is:

- safely synthesize one new prompt turn for an existing Feishu binding at a
  future time
- keep using the same FOCUS instance backend
- preserve the existing running-turn / attach / interaction / live-runtime
  safety boundaries

Explicitly out of scope today:

- a persistent scheduler / job queue
- cross-binding prompt fan-out
- starting a second bare Codex backend to recover the same thread

## 2. `binding/submit-prompt`

The control plane exposes:

- `binding/submit-prompt`

Its contract:

- the scope is **binding**, not thread
- the minimum input is:
  - `binding_id`
  - `text` or `input_items`
- optional inputs:
  - `actor_open_id`
  - `synthetic_source`
  - `display_mode`
- the target binding must already exist; when it does not exist, the control
  plane must fail closed rather than creating a new binding implicitly
- the target binding may currently have no thread; this means “an existing
  binding with no current thread”, and it follows the normal prompt-entry
  semantics of "create thread first, then start turn"
- the target binding may currently be `detached`; when attach / resume
  preflight succeeds, it follows the normal binding recovery path
- all write admission checks must reuse the existing safety boundary rather than
  bypass it

Return contract:

- `started=true`
  - upstream accepted the prompt's `turn/start` submission; it normally starts
    a new turn while upstream is idle, but may join an already active regular
    turn in the narrow race
  - the response preserves the authoritative upstream `turn.id`; Focus still
    waits for matching `turn/started` to activate its lease/execution lifecycle
- `queued=true`
  - the target binding is currently executing, and the synthetic prompt was
    admitted into that same binding's local FIFO
  - `queue_position` should be returned
  - when dequeued, the prompt must read the binding's latest next-turn settings
    such as `/model`, `/effort`, `/approval`, and `/permissions`
- `started=false, queued=false`
  - the action failed closed or startup failed; an accepted deferred prompt is
    instead `started=false, queued=true`
  - `reason` must be returned; `reason_code` should be returned when available

## 3. `focusctl prompt send`

The local CLI exposes:

- `focusctl [--instance <name>] prompt send --binding-id <binding_id> (--text <text> | --text-file <file>)`

Its contract:

- this is the formal local entry for `binding/submit-prompt`
- the default is `display_mode=silent`
- it may additionally accept:
  - `--synthetic-source`
  - `--display-mode silent|announce`
  - `--actor-open-id`
- when the target binding is not writable:
  - the exit code must be non-zero
  - the refusal reason must be printed
- when the target binding obtains the exact FIFO admission defined below, the
  prompt is not considered unwritable; it enters that binding's local FIFO and
  starts after the matching active execution settles. Proof comes only from the
  binding's exact execution anchor, existing same-binding/root/epoch
  continuity, or a preprojection exact local turn re-read under the shared
  binding lock. Writer denial, a start response, or its returned `turn.id` alone is
  insufficient

## 4. `display_mode`

Only two modes exist today:

- `silent`
  - do not emit an extra "this was system-triggered" chat message
  - normal execution / terminal cards still follow the existing runtime behavior
- `announce`
  - send one short trigger notice to the target chat **only after** the
    synthetic prompt's `turn/start` submission is accepted (`started=true`)
  - do not announce a refused, failed, unknown, or merely queued submission;
    a queued item may announce later only when its own dequeue submission is
    accepted

There is no more complex message choreography contract yet.

## 5. `feishu-scheduled-prompts` skill

The repository now ships one Linux-only managed skill:

- `feishu-scheduled-prompts`

Its contract:

- it manages `systemd --user` timer/service units
- when the timer fires, it still routes back through `focusctl prompt send`
- it does not call a standalone Codex SDK helper directly
- it does not depend on a Feishu message loopback trick

The helper currently exposes:

- `create`
- `list`
- `show`
- `remove`
- `run-now`

These helpers are not Feishu slash commands and not a formal cross-platform
public product surface. They are the Linux short-term scheduling shell.

Local tool resolution is part of the helper contract:

- ordinary users in a login shell may rely on `PATH` when it contains
  `focusctl`
- `create --ctl-path <path>` is the explicit override and is stored in the
  generated service unit
- when `--ctl-path` is omitted, the helper discovers `focusctl` in this
  order:
  1. `PATH`
  2. `FOCUS_BIN_DIR/focusctl`, or `~/.local/bin/focusctl`
  3. `FOCUS_DATA_ROOT/.venv/bin/focusctl`, or
     `~/.local/share/focus/.venv/bin/focusctl`
- managed skill instructions should invoke the helper with the managed venv
  Python, normally `FOCUS_DATA_ROOT/.venv/bin/python`, instead of assuming the
  system `python3` satisfies the project runtime

Recurring timers must have an explicit termination strategy. `systemd --user`
only evaluates `OnCalendar`; it does not know whether the business task is
complete. Acceptable patterns are:

- one-shot tasks with a concrete future timestamp
- recurring tasks whose prompt includes an exact self-removal condition and
  removal command
- recurring tasks with a deterministic one-shot cleanup prompt at a known
  deadline

## 6. Safety Boundary

The following are normative:

1. a scheduled task is only "start one new prompt at a future time"
2. scheduled work may not bypass interaction / attach / running-turn admission
3. only the target binding's exact process-local execution anchor, existing
   same-binding/root/epoch continuity, or a preprojection exact local turn
   re-read under the shared binding lock may enter the local in-memory FIFO.
   Preprojection accepts only a current-process, non-Feishu, non-empty exact
   active lease from `InteractionLeaseStore`. For Web/`fcodex`, such evidence
   can come only from a still-lease-bearing exclusive/autonomous path; ordinary
   prompt/start creates none. Cross-binding conflicts, foreign/stale leases,
   turn/root mismatches, and attach/preflight failures still fail closed;
   writer denial, a start response, or its returned `turn.id` does not establish
   continuity
4. there is no automatic cross-instance live-runtime takeover
5. the Linux skill is only a scheduling shell; the real execution surface
   remains `binding/submit-prompt`
6. `display_mode=announce` follows upstream acceptance of the `turn/start`
   submission. It is never advance admission evidence or a reason to announce
   before that acceptance
7. persisted prompts, task metadata, and generated user units can expose input
   or binding details and must be private to the current user; task directories
   use `0700` and files use `0600`

## 6.1 Binding FIFO

Normal Feishu prompts, `focusctl prompt send` / `binding/submit-prompt`,
and `/compact` share the same binding admission semantics:

- if the current binding is idle, execute immediately
- if the current binding has an active execution, only that same binding may
  enqueue follow-up work; admission does not additionally require
  `actor_open_id` to match the actor of the currently running turn
- if that execution mirrors a Web/`fcodex`-initiated turn, only a normal prompt
  may enqueue after the exact active/attached/inflight binding/root/turn mirror
  is rechecked under one lock. Once the mirror has a non-empty exact turn, no
  origin lease is required; a preceding writer denial is neither proof nor a
  prerequisite. `/compact`, another Feishu binding, and a submission without
  an exact turn do not use this exception
- an exact non-empty autonomous turn already mirrored into the active/attached
  binding likewise needs no invented lease. Before projection, only a
  current-process, non-Feishu, non-empty exact active lease in
  `InteractionLeaseStore` may substitute, and it must be re-read unchanged
  under the same lock immediately before append. Ordinary Web/`fcodex`
  prompt/start has no lease and cannot provide that preprojection proof
- when there is no execution anchor, existing FIFO continuity, or preprojection
  exact local turn, the normal prompt does not establish a queue. It acquires a
  blank submission lease and calls official `turn/start` directly
- `actor_open_id` remains part of identity, audit, runtime-interaction
  ownership, and reply context, but it is not an extra partition key for
  same-binding queueing
- the queue is process-local memory only; it does not promise restart recovery,
  listing, cancellation, or cross-binding scheduling
- each queue item is scoped to the exact binding, target thread, and binding
  epoch at admission. It is not a durable continuation token, a writer
  credential, or permission to follow a later binding/target transition
- `FeishuExecutionQueueController` is the sole process-local owner of that
  item order and a monotonic binding epoch. Every successful authority removal
  invalidates the epoch, so A -> B -> A cannot revive A's old item even when
  the binding key or root id is reused
- a drain only claims the exact head with an issuer- and object-identity-checked
  receipt; it does not remove the item before execution settles. Recall or
  binding invalidation cancels that receipt. Consecutive dropped/known-failure
  heads are settled iteratively, not through recursive callbacks
- when dequeued, `/compact` establishes a local execution anchor before calling
  upstream `thread/compact/start`; until that anchor receives its own
  `turn/started`, the `contextCompaction item/started` fallback binding, or an
  explicit start failure, a following prompt cannot pass through it
- `/model`, `/effort`, `/approval`, `/permissions` do not
  queue; they update binding-wise next-turn settings immediately, and later
  dequeued prompts read the latest settings

Every ordinary Feishu prompt which actually crosses the app-server boundary,
including immediate, dequeued, and synthetic scheduled input, calls official
`turn/start` with the complete input/settings payload. It normally starts a new
turn while upstream is idle. In the narrow race where Web, `fcodex`, or
autonomous goal continuation after resume becomes active first, upstream
start-or-steer adds the Feishu input and turn settings to that active regular
turn. This is an accepted submission with an upstream effect, not a FIFO
refusal, and it must not be resent. The response preserves the authoritative
upstream `turn.id`; Focus waits for matching `turn/started` to activate its local
lease/execution lifecycle. `/compact` keeps its separate start contract.

Only one Feishu binding may own pending/draining FIFO continuity for a root at
one time. Once established, later input from that same binding/root/epoch keeps
FIFO order; another binding fails closed until the old continuity drains or is
invalidated. Real dequeue is another `turn/start` submission. If the race makes
it join an active regular turn, that head is consumed and waits for the actual
turn's matching lifecycle terminal rather than being requeued or resent. There
is no polling timer, scheduler, spin, persisted wake-up, or automatic resend.
Unknown/malformed start outcomes, root-owner settlement failure, and
execution-anchor retirement failure remain blocked and never create or advance
new FIFO continuity.

### 6.2 FIFO invalidation and real dequeue

A successful lifecycle transition that removes a binding's old authority must
discard that binding/target FIFO. This includes successful binding deactivation
or clear, runtime detach, moving an existing binding to another root (including
the `/cd` clear-and-rebuild path), archive/delete cleanup, and known-gone-thread
recovery. Group deactivation also discards the group binding's old FIFO even
though an administrator may later re-activate the group.

The first bind created by the head that is itself being drained is not a
target-rebind of this kind. The rule is about an already-bound old thread: no
item submitted for that thread may become input for a different thread merely
because the binding name is reused.

Discarding a FIFO must also cancel a head already held by a re-entrant drainer.
The queue records a cancellation marker, and the drainer checks it before it
can start the old item. Thus an old item cannot escape the deque, survive a
successful invalidation, and start under a recreated binding or new root.

Actual dequeue is a new writer action, not a replay of past admission. It must
acquire the exact blank submission lease immediately before start/compact execution.
If that admission fails, the item is refused or discarded under the normal
failure path; it must not wait as a hidden takeover or be delivered to another
frontend. See [`root-operation-owner.md`](root-operation-owner.md).

If an admitted upstream start has an unknown outcome, the exact queue head is
not replayed. Its PID-bound blank lease and local execution anchor, not the
queue receipt, block later heads in this process until matching lifecycle or
epoch invalidation. A known no-send or preparation drop may consume the exact
head and continue with the next item in the same epoch.

## 7. Platform Boundary

The only formal short-term scheduling implementation today is
`systemd --user`.

Therefore:

- the `feishu-scheduled-prompts` helper is only promised on Linux
- there is no equivalent managed timer helper contract yet for macOS or Windows

If a future cross-platform scheduler product surface is added, this document
must change with the code.
