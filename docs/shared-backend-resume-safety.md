# Shared Backend and Resume Safety Design

## 1. Problem Statement

`feishu-codex` and stock Codex TUI are safe only when they write the same thread through the same app-server backend.

If they resume the same persisted thread through different app-server processes, they can each materialize their own live in-memory thread and append conflicting state later.

This document defines the safety model, user-facing semantics, and implementation boundaries for:

- shared backend mode
- `/resume` for threads not loaded in the current backend
- Feishu multi-chat behavior for the same thread

## 2. Verified Constraints

### 2.1 Hard facts we can rely on

- Within one app-server process, resuming an already-loaded thread reuses the loaded thread and subscriber model rather than creating a second live copy.
- `thread/loaded/list`, `thread/list.status`, and `thread/read.status` only describe the current app-server process.
- `thread/read` is a stored-history read and does not create a live thread.
- `thread/resume` loads the thread into the current app-server as a live thread.

### 2.2 Facts we cannot rely on

- We cannot reliably detect whether another stock TUI process is currently writing the same thread.
- `source` and `service_name` are provenance hints, not live ownership or lock signals.
- We cannot force a stock TUI process to stop writing.
- We cannot auto-attach to a stock TUI embedded app-server with current public mechanisms.

## 3. Design Goals

- Make the safe path explicit and easy to adopt.
- Avoid fake certainty such as "no other client is writing".
- Avoid silent dual-write on external threads.
- Keep the user mental model simple enough to use daily.

## 4. Non-goals

- Do not attempt global cross-process thread locking.
- Do not present "takeover" as a real technical handoff.
- Do not default to fork-on-resume.
- Do not make safety decisions from heuristics such as `source=cli` alone.

## 5. Core Model

Use one rule everywhere:

- One thread should be written through one backend.

If the user wants both Feishu and local TUI to operate on the same live thread safely, both must connect to the same app-server backend.

## 6. Backend Modes

### 6.1 Shared backend mode

This is the recommended steady-state mode.

Behavior:

- `feishu-codex` owns a stable local websocket endpoint.
- Local TUI connects with `codex --remote ...` rather than starting its own embedded backend.
- Feishu and local TUI share the same loaded thread state.

Recommended local wrapper:

```bash
fcodex "$@"
```

Equivalent launch shape:

```bash
codex --remote ws://127.0.0.1:PORT "$@"
```

Properties:

- safe against dual-live-thread divergence for the same thread
- safe for multiple local TUI windows connected to the same shared backend
- lower cognitive load once adopted
- shared backend does not imply one globally synchronized per-client control plane such as pending collaboration-mode selections

Clarification:

- the live thread is shared through one backend
- each client still decides what it will send on its own next `turn/start`
- collaboration-mode choices made in Feishu do not immediately rewrite what an already-open TUI shows, and vice versa

### 6.2 Isolated backend mode

This is the compatibility mode when the user runs stock TUI without `--remote`.

Properties:

- `feishu-codex` cannot know whether the local TUI is idle, closed, or about to write
- `feishu-codex` cannot safely assume exclusive ownership of an external thread
- external thread resume must be guarded by explicit user choice

## 7. `/resume` Semantics

### 7.1 Classification

After matching the target thread, classify it using only hard facts:

1. `loaded-in-current-backend`
2. `not-loaded-in-current-backend`

Do not add a third "probably safe" class based on cached ownership heuristics.

### 7.2 Loaded in current backend

If the target thread is already loaded in the current `feishu-codex` backend:

- resume directly
- bind the current Feishu chat to that thread
- do not show a risk card

This is safe because the thread already lives in the same backend.

### 7.3 Not loaded in current backend

If the target thread is not loaded in the current backend, `/resume` must not immediately call `thread/resume`.

Instead, show a three-action card:

- `查看快照`
- `恢复并继续写入`
- `取消`

#### `查看快照`

Behavior:

- call `thread/read`
- show title, cwd, updated time, source, optional `service_name`, and recent turns
- do not bind the current Feishu chat
- do not create a live thread in the current backend

Use this as the safe inspection path.

#### `恢复并继续写入`

Behavior:

- call `thread/resume`
- bind the current Feishu chat to that thread
- reply with an explicit warning that this creates a live thread in the `feishu-codex` backend

Required warning meaning:

- if another non-shared backend client also writes this thread, history may diverge or become confusing
- to avoid this, use `fcodex` for local continuation

This is an explicit user-confirmed risk path, not a technical handoff.

#### `取消`

Behavior:

- do nothing

### 7.4 Why fork is not part of the default flow

`thread/fork` is technically safe, but it creates extra branch threads that make later session recovery harder in both TUI and Feishu.

For now:

- do not offer fork in the default `/resume` risk card
- keep fork as a possible future explicit command if needed

## 8. Provenance Display

Expose provenance metadata as informational UI only:

- `source`
- `service_name` when available

Use cases:

- help users understand where a thread came from
- make shared vs external threads easier to reason about

Do not use provenance alone as an automatic safety decision.

## 9. User Guidance

The product should teach one operational rule instead of many exceptions:

- If you want to continue the same thread locally and in Feishu, use `fcodex`, not bare `codex`.

Recommended high-value surfaces:

1. `/help`
2. the external-thread `/resume` risk card
3. a one-time hint after Feishu first materializes a thread

Avoid repeating the warning on every execution card or every message card.

## 10. Symmetric Risk

The risk is symmetric.

If a thread is active in `feishu-codex` and the user later resumes the same thread in local TUI through bare `codex`, the same dual-backend divergence risk exists.

`feishu-codex` cannot prevent that. It can only:

- recommend `fcodex`
- avoid silently resuming unknown external threads into a writable state

## 11. Feishu Multi-chat Semantics

Current safety and UX are different concerns.

### 11.1 Safety

All Feishu chats in one `feishu-codex` service already share one backend process, so they do not create separate app-server processes per chat.

Therefore, they do not suffer from the same cross-process dual-live-thread divergence that exists between Feishu and bare TUI.

### 11.2 Current UX limitation

Current implementation keeps a single binding from `thread_id` to one `(user_id, chat_id)`.

Implication:

- the last Feishu chat bound to a thread receives streaming updates and approvals
- this is not a mirrored multi-chat live view

So the supported semantics today are:

- backend-safe shared thread state inside Feishu
- single-chat notification ownership per thread

Real multi-chat mirrored viewing is out of scope for this design.

## 12. Minimal Implementation Plan

### Phase A: Shared backend mode

- replace random listen address with configurable stable endpoint
- support optional local auth token
- provide `fcodex` wrapper or equivalent helper command
- expose current backend mode in `/status`

### Phase B: Guarded external-thread resume

- add thread classification: loaded vs not loaded in current backend
- add the three-action external-thread card
- add snapshot preview rendering from `thread/read`

### Phase C: UX guidance

- add one clear rule to `/help`
- add explicit shared-mode recommendation in risk-card copy
- expose provenance display where useful

## 13. Acceptance Criteria

- Shared mode lets Feishu and local TUI resume and write the same thread without divergence caused by separate live copies.
- External threads are never silently resumed into writable state from Feishu.
- Users always have a safe inspection path that does not materialize a live thread.
- The UI never claims to know whether another external TUI is actively writing.
