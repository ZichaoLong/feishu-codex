# Runtime Control Surface

Chinese original: `docs/runtime-control-surface.zh-CN.md`

This document defines the shared state vocabulary and control contract across:

- Feishu commands
- the local `feishu-codexctl` admin CLI
- the shared app-server backend

It answers three questions:

- what `/status` is actually describing
- what `/release-feishu-runtime` releases and does not release
- why local runtime-release actions must go through the running `feishu-codex` service rather than directly calling app-server from a separate CLI connection

See also:

- `docs/feishu-thread-lifecycle.md`
- `docs/session-profile-semantics.md`
- `docs/shared-backend-resume-safety.md`

## 1. Upstream Baseline

- Upstream project: [`openai/codex`](https://github.com/openai/codex.git)
- Current local verification baseline: `codex-cli 0.118.0` (2026-04-03)

## 2. Shared State Vocabulary

These terms are the shared factual vocabulary used by Feishu `/status` and the local
`feishu-codexctl` admin surface.

### 2.1 `binding`

Which `thread_id` a Feishu chat is logically bound to.

- `unbound`
- `bound`

`binding` is the source of truth for “which thread this chat continues on next”.
It is not the same as runtime attachment, and not the same as whether the backend
still has the thread loaded.

### 2.2 `feishu runtime`

Whether the `feishu-codex` service connection is still attached to that thread.

- `attached`
- `released`
- `not-applicable`

This is Feishu-side attachment state, not backend-global loaded state.

### 2.3 `backend thread status`

The thread state reported by the current shared backend.

Typical values:

- `notLoaded`
- `idle`
- `active`
- `systemError`

### 2.4 `backend running turn`

A derived judgment:

- `yes` when `backend thread status == active`
- otherwise `no`

This answers whether the backend is currently executing a turn on that thread.
It does not mean the current Feishu chat owns that execution.

### 2.5 `Feishu write owner`

The Feishu-internal single-writer lease for a shared thread.

- it only exists inside `feishu-codex`
- it does not describe `fcodex` or other frontends
- it answers which Feishu binding may currently write

### 2.6 `interaction owner`

The cross-frontend interaction lease shared between Feishu and `fcodex`.

It answers who may currently handle:

- interrupts
- approvals
- user-input requests

Typical holders:

- a Feishu binding
- a local `fcodex` terminal
- `none`

### 2.7 `re-profile possible`

This is a derived judgment, not stored state.

Current contract:

- `yes` when `backend thread status == notLoaded`
- otherwise `no`

It means the thread is currently in a state where the next resume / auto-reattach
may re-resolve profile / provider.

Avoid the term “original thread profile” in this repo’s contract.
The precise wording is:

- resume with the current local default profile
- resume with an explicit profile
- a live loaded runtime cannot be re-profiled via resume

## 3. Important State Combinations

### 3.1 `bound + attached + active`

The chat is still bound, Feishu is still attached, and the backend is currently
executing a turn.

### 3.2 `bound + released + notLoaded`

The binding remains, Feishu has released runtime residency, and the backend has
also unloaded the thread.

This is the clearest “re-profile is possible” state.

### 3.3 `bound + released + idle/active`

Feishu has already released its own runtime residency, but some external subscriber
still keeps the thread loaded in the backend.

The most common case is local `fcodex`.

So `released` does not imply `notLoaded`.

## 4. `/status` Contract

Feishu `/status` is chat-scoped.

It answers, for the current chat binding:

- `binding`
- `feishu runtime`
- `backend thread status`
- `backend running turn`
- `Feishu write owner`
- `interaction owner`
- `re-profile possible`
- whether `/release-feishu-runtime` is currently allowed

It is not a global thread-management command.
Global binding/thread inspection belongs to `feishu-codexctl`.

## 5. Exact Contract of `/release-feishu-runtime`

### 5.1 Scope

Feishu `/release-feishu-runtime`:

- takes no arguments
- targets the current chat’s bound thread
- but semantically releases Feishu runtime residency for that thread across the whole running `feishu-codex` service

It is not a per-chat “soft local flag”.

### 5.2 What it does

On success it:

- keeps all Feishu bindings that point to that thread
- clears the Feishu write owner for that thread
- clears the Feishu interaction owner for that thread when Feishu currently owns it
- flips all still-`attached` Feishu bindings on that thread to `released`
- makes the running `feishu-codex` service unsubscribe its own app-server connection from that thread

### 5.3 What it does not do

It does not:

- delete the thread
- archive the thread
- clear the Feishu chat-to-thread binding
- force local `fcodex` to close
- guarantee that the backend unloads the thread

If the backend still reports `idle` or `active` afterward, some external subscriber
is still attached.

### 5.4 When it is rejected

Current implementation rejects release when:

- a Feishu-side turn on that thread is still in flight
- a Feishu-side approval or user-input request on that thread is still pending

This avoids releasing runtime ownership while Feishu is still responsible for
closing out an execution flow.

### 5.5 How to interpret success

If the command succeeds and:

- `backend thread status == notLoaded`
  - the backend is no longer holding the thread live
  - the next resume path may re-profile
- `backend thread status in {idle, active, systemError}`
  - the backend is still loaded
  - the usual reason is an external subscriber such as local `fcodex`

### 5.6 What happens on the next normal prompt

If a Feishu binding remains `bound` but its `feishu runtime == released`, then the
next ordinary prompt in that chat will:

1. reattach / resume using the bound `thread_id`
2. then start the new turn

If the thread is `notLoaded` at that moment, that reattach path follows the
unloaded-thread profile contract defined in `docs/session-profile-semantics.md`.

## 6. Local Admin Surface: `feishu-codexctl`

### 6.1 What it is

`feishu-codexctl` is the local admin CLI for the running `feishu-codex` service.

It is not:

- an alias of `fcodex`
- a local shell wrapper for Feishu chat commands
- another app-server frontend

Its role is to inspect service / binding / thread state and issue explicit
management actions to the running Feishu service.

### 6.2 Why it must go through the running service

In the public upstream protocol, `thread/unsubscribe` is connection-scoped.

So if a local CLI opens its own app-server connection and sends `thread/unsubscribe`,
it only unsubscribes its own connection, not the Feishu service connection.

Therefore any action that truly changes whether Feishu is still attached to a
thread must be executed by the running `feishu-codex` service itself.

### 6.3 First command set

Current implementation provides:

- `feishu-codexctl service status`
- `feishu-codexctl binding list`
- `feishu-codexctl binding status <binding_id>`
- `feishu-codexctl thread status <thread_id|thread_name>`
- `feishu-codexctl thread bindings <thread_id|thread_name>`
- `feishu-codexctl thread release-feishu-runtime <thread_id|thread_name>`

### 6.4 `binding_id` shape

The local admin CLI uses stable admin-facing binding ids:

- group binding: `group:<chat_id>`
- p2p binding: `p2p:<sender_id>:<chat_id>`

These are local admin identifiers. They do not need to mirror Feishu command names.

## 7. Shared Vocabulary, Not Forced Command Symmetry

This repo intentionally chooses:

- shared state vocabulary across Feishu and local admin
- without forcing identical command names or identical interaction shape

That is because:

- Feishu is naturally chat-scoped
- the local admin CLI is naturally service / binding / thread scoped
- `fcodex` should remain focused on Codex usage over the shared backend, not on Feishu service administration

So the current architecture has three distinct entry points:

- Feishu chat commands: current chat binding control
- `fcodex`: Codex usage on the shared backend
- `feishu-codexctl`: local administration of the running Feishu service
