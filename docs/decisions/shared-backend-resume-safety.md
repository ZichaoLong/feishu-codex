# Shared Backend and Resume Safety

See also:

- `docs/architecture/fcodex-shared-backend-runtime.md` for the current shared-backend and
  wrapper runtime model
- `docs/contracts/runtime-control-surface.md` for the shared state vocabulary used by
  `/status`, `/release-feishu-runtime`, and the local admin surface
- `docs/contracts/session-profile-semantics.md` for exact command and wrapper semantics
- `docs/architecture/feishu-codex-design.md` for architecture and repository boundaries

## 1. Upstream Baseline

- Upstream project: [`openai/codex`](https://github.com/openai/codex.git)
- Current local validation baseline: `codex-cli 0.118.0` (checked on 2026-04-03)
- This document focuses on safety and `/resume` semantics. It intentionally does
  not restate most wrapper/runtime details; those belong in
  `fcodex-shared-backend-runtime`.

## 2. Problem Statement

`feishu-codex` and stock Codex TUI are safe only when they write the same thread
through the same app-server backend.

If they resume the same persisted thread through different app-server processes,
they can each materialize their own live in-memory thread and append
conflicting state later.

This document defines the current safety model for:

- shared backend operation
- `/resume` for threads not loaded in the current backend
- Feishu multi-chat behavior for the same thread

## 3. Verified Constraints

### 3.1 Hard facts we can rely on

- Within one app-server process, resuming an already-loaded thread reuses the
  loaded thread and subscriber model rather than creating a second live copy.
- `thread/loaded/list`, `thread/list.status`, and `thread/read.status` only
  describe the current app-server process.
- `thread/read` is a stored-history read and does not create a live thread.
- `thread/resume` loads the thread into the current app-server as a live thread.

### 3.2 Facts we cannot rely on

- We cannot reliably detect whether another stock TUI process is currently
  writing the same thread.
- `source` and `service_name` are provenance hints, not live ownership or lock
  signals.
- We cannot force another stock TUI process to stop writing.
- We cannot auto-attach to a stock TUI embedded app-server with current public
  mechanisms.

## 4. Core Safety Rule

Use one rule everywhere:

- One thread should be written through one backend.

If the user wants Feishu and local TUI to operate on the same live thread
safely, both must connect to the same app-server backend.

## 5. Backend Safety Boundary

### 5.1 Shared backend

This is the recommended safe path.

Properties:

- Feishu and local TUI write through the same app-server backend
- the same loaded thread state is shared
- multiple local TUI windows can attach to that backend without creating
  cross-process divergence

How the current runtime and `fcodex` wrapper make that work is documented in
`docs/architecture/fcodex-shared-backend-runtime.md`.

### 5.2 Isolated backend

This is what happens when the user runs stock TUI outside the shared backend.

Properties:

- `feishu-codex` cannot know whether that local TUI is idle, closed, or about
  to write
- `feishu-codex` cannot safely assume exclusive ownership of such a thread
- local continuation of the same live thread should use `fcodex` and the shared
  backend instead
- continuing to write the same thread through bare `codex` on another backend
  is outside the supported safe path

## 6. `/resume` Safety Model

### 6.1 Classification

After matching the target thread, classify it using only hard facts:

1. `loaded-in-current-backend`
2. `not-loaded-in-current-backend`

Do not add a third "probably safe" class based on cached ownership heuristics.

### 6.2 Loaded in current backend

If the target thread is already loaded in the current `feishu-codex` backend:

- resume directly
- bind the current Feishu chat to that thread
- do not show a risk card
- `resume` does not use this path to rewrite the live runtime's profile or
  provider

This is safe because the thread already lives in the same backend.
If the thread must later be re-resolved under a different profile, it must
first return to `not-loaded-in-current-backend`.

### 6.3 Not loaded in current backend

If the target thread is not loaded in the current backend, this repository's
safety decision is to call `thread/resume` directly.

Behavior:

- resume the target thread immediately
- bind the current Feishu chat to that thread
- if the user later attaches through `fcodex` on the same shared backend,
  Feishu and `fcodex` can continue operating on the same live thread safely
- if this resume does not specify an explicit profile, Feishu and `fcodex`
  have the same effective behavior: they both use the current `feishu-codex`
  local default profile

This path assumes:

- local continuation of the same thread uses `fcodex`
- bare `codex` is not also writing that thread through another backend

One detail should be recorded explicitly: the two clients do not reach that
behavior through the same execution path.

- Feishu resolves and sends profile / model / model_provider before
  `thread/resume`
- `fcodex` injects the default profile in the wrapper layer before entering the
  upstream `codex resume` path

That difference should not be interpreted as a semantic mismatch. The intended
semantics are the same on both sides: for an unloaded thread, absent an
explicit profile, resume uses the local default profile.

The repository decision is to no longer block this path with a preview/confirm
card. Avoiding dual-backend writes for such threads is an operational rule, not
a UI-enforced guard.

## 7. Provenance and Symmetric Risk

Expose provenance metadata as informational UI only:

- `source`
- `service_name` when available

Use cases:

- help users understand where a thread came from
- make shared vs external threads easier to reason about

Do not use provenance alone as an automatic safety decision.

The risk is symmetric:

- if Feishu resumes an external thread into its own backend, divergence is
  possible
- if a user later resumes a Feishu-active thread through bare `codex` on
  another backend, the same risk exists

`feishu-codex` cannot eliminate that risk. This repository chooses a more direct
`/resume` path, so the safety boundary relies on one operational rule: if
multiple clients should continue the same live thread, keep them on the shared
backend via `fcodex`, and do not mix in bare `codex`.

## 8. Feishu Multi-Chat Boundary

Safety and UX are different concerns.

### 8.1 Safety

All Feishu chats in one `feishu-codex` service already share one backend
process, so they do not create separate app-server processes per chat.

Therefore, they do not suffer from the same cross-process dual-live-thread
divergence that exists between Feishu and bare TUI.

### 8.2 Current UX / ownership decision

The current model is no longer the older "one primary notification binding per
`thread_id`" model.

The more accurate description now is:

- one `thread_id` may have multiple Feishu subscribers / bindings at the same
  time
- those subscribers share one backend thread, so this remains backend-safe
- execution-driving and interaction-driving routing still follows explicit
  owner / lease state, not "who bound last"

Concretely:

- Feishu-internal write admission is controlled by the `Feishu write owner`
- cross-frontend approvals, user-input requests, and interrupts are controlled
  by the `interaction owner`
- when a thread has no explicit owner but exactly one Feishu subscriber, the
  runtime may fall back to that sole subscriber for routing; once multiple
  subscribers exist, routing must depend on explicit owner state rather than
  any "last binding wins" guess

The user-visible consequence is:

- non-owner Feishu chats may still keep their binding and observe shared thread
  facts
- non-owner chats may not write or handle the current turn's approvals / input
  requests
- the system still does not promise a fully mirrored interactive live UI across
  multiple Feishu chats; execution cards, approval cards, and request-driving
  events route by the effective owner path rather than broadcasting every
  interactive surface to every subscriber

So the decision at this layer is:

- Feishu allows multiple subscribers on one backend thread
- writability and interactivity are determined by owner leases
- "one primary notification binding" is no longer the current model

For the exact state vocabulary and state-transition contract, see
`docs/contracts/runtime-control-surface.md` and
`docs/contracts/feishu-thread-lifecycle.md`.

## 9. Related Documents

- `docs/contracts/session-profile-semantics.md`: exact command semantics for `/session`,
  `/resume`, `fcodex`, and profile handling
- `docs/architecture/fcodex-shared-backend-runtime.md`: shared backend, dynamic port
  discovery, cwd proxy, and wrapper runtime behavior
- `docs/architecture/feishu-codex-design.md`: architecture, design constraints, and current
  repository structure
