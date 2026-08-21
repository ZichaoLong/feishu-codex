# Upstream Subagent Lifecycle and Presentation Boundary

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/subagent-observation-and-recovery.zh-CN.md`.

> Upstream baseline:
> [`openai/codex@be6e8eac029b183056b7e4402879f15d2c85f61b`](https://github.com/openai/codex/commit/be6e8eac029b183056b7e4402879f15d2c85f61b)
> (`rust-v0.147.0`).

This contract defines Focus's minimal boundary with Codex spawned subagents.
Focus does not own child lifecycle, parent mailboxes, or agent-tree completeness.
It does not define the root writer or tree-stop behavior.

## 1. Upstream ownership

Codex owns thread identity, source/parent relations, loaded status, child runtime,
history, pending callbacks, and direct-input policy. When a live child finishes,
Codex sends its result with `trigger_turn=false` to its direct parent if that
parent remains loaded. This does not start another parent turn.

A matching root `turn/completed` immediately ends the root main turn and releases
the writer. Child activity, pending callbacks, late completion, or presentation
state must not extend the root turn, block Feishu FIFO, delay the terminal card,
or prevent the next root input.

Focus does not scan, reconstruct, resume, redeliver, or persist child lifecycle.
It has no child-to-root registry, reconcile worker, or subagent shutdown barrier.

## 2. Direct-target boundary

A MultiAgent V2 `ThreadSpawn` child is parent-owned. Before any frontend mutation,
resume, interrupt, archive, delete, or bind, Focus reads an authoritative
`ThreadSummary`. A target with `subagent_kind=threadSpawn` must reject direct
writes. The user continues the root and lets the root agent use upstream
collaboration tools to coordinate the child.

`Thread.source` is usable evidence only when its upstream enum/object shape is
validated. An unknown, missing, or malformed source may remain limited display
data, but it must not be guessed into an ordinary root or gain mutation,
resume, interrupt, lease, binding, or shared-approval authority. The `fcodex`
metadata-only child-read exception also requires a structured `ThreadSpawn`
with a non-empty parent and valid depth; otherwise it is rejected as well.

`fcodex` retains only the upstream-style strict read-only exception: an
authoritatively proven ThreadSpawn may receive
`thread/read(includeTurns=false)` for metadata. This creates no child owner,
runtime interest, writer, subscription, or mutation path. Answering an already
pending exact callback is not the same as starting a child turn.

## 3. Surface projection

Focus Web's generic Tasks UI projects only collaboration items already recorded
in parent history, such as `collabAgentToolCall` and `subAgentActivity`. These
history facts can expose known tasks and results, but do not prove a complete,
atomic agent tree and never participate in lifecycle admission.

Focus emits no live child delta, global inventory/health banner, or Feishu
"still checking" notice. Feishu settles only the root execution; `fcodex`
continues to use the upstream TUI's native presentation.

## 4. Unknown child callbacks

Focus keeps a minimal current-process set of ids proven by authoritative
`ThreadSummary` values to be direct roots. It exists only to decide whether a
shared approval may be offered to multiple surfaces. It stores no parent
relation, child state, result, runtime lifecycle, or recovery intent, and is
cleared when the connection epoch becomes invalid.

A callback that cannot be proven to target a direct root must not be promoted
to shared approval or cause Focus to guess a root, acquire a root lease, rebind
state, start an observer/retry, or send a notice. Web and `fcodex` decline it;
when Feishu is the final surface, it fail-closes only the canonical exact
callback. Any unknown outcome fences only that callback.

## 5. Cold-resume limitation

A bare Codex cold resume restores only the selected root. Children that remain
loaded can be shown again through upstream inventory/read, but Codex does not
recursively reopen the child tree. If the direct parent has left the current
`ThreadManager`, or app-server exited, a late child completion's in-memory
mailbox delivery can be lost. Focus accepts this narrow limitation honestly: it
does not silently inject child history into the root or promise complete delayed
result recovery.

Changes in this area must at least cover: root completion does not wait for a
child; ThreadSpawn direct writes remain rejected; metadata-only reads create no
owner; an unknown child callback enters no shared approval and creates no root
lease, notice, or worker; parent-history Tasks remain visible; and service
shutdown has no child-lifecycle worker or barrier.
