# Local Commands and Runtime-Settings Contract

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/local-command-and-thread-profile-contract.zh-CN.md`.

This file keeps its historical filename, but it no longer defines any
project-owned profile surface. It defines the boundary between local entry
points and the remaining settings model.

## 1. Four local entry points

### 1.1 `focus`

Responsible for:

- entering the local Codex TUI
- resuming or attaching to a live thread
- acting as a local frontend against the instance shared backend

It is not:

- a service-management CLI
- a project-owned settings surface

### 1.2 `fcodex`

`fcodex` is an equivalent alias for `focus`, kept for the direct "Codex TUI
thin wrapper" meaning.

Responsible for:

- the same local Codex TUI wrapper behavior as `focus`
- a stable entry for operators who prefer a Codex-specific command name

It is not:

- another agent CLI
- a separate runtime or state surface from `focus`

### 1.3 `focusctl`

Responsible for:

- local repair after installation and upgrades
- service lifecycle
- instance management
- inspecting instance / binding / thread / service state
- performing limited local admin actions
- diagnosing attach / detach / backend problems

It is not:

- a second frontend for turn settings
- a local mirror of Feishu setting cards
- a Codex TUI

### 1.4 `focusd`

Responsible for:

- the background daemon entry called by the platform service manager

It is not:

- a daily manual management command
- a local Codex TUI wrapper

## 2. Feishu and Web writable-settings boundary

### 2.1 Binding-wise next-turn settings

- scope: Feishu binding
- Feishu entries: `/model`, `/effort`, `/approval`, `/permissions`
- local `focus` / `fcodex` / upstream TUI keep their own local state; they do not
  auto-merge with persisted Feishu binding settings

### 2.2 Web has separate instance-wide next-turn settings

Focus Web's model, effort, approval, and permissions belong to one durable
`WebNextTurnSettings` shared by every browser, post-F5 document, and thread in
the same Focus instance. It is neither a Feishu binding nor a local-TUI profile
and does not auto-merge with Feishu, `focus`, or `fcodex`. A main-turn lease
authorizes only its concrete submission/active turn; it neither owns these
settings nor prevents a connected browser from changing them for the next
eligible Web turn.

The canonical [runtime-settings fact-source
contract](./runtime-settings-fact-sources.md) alone defines seed, persistence,
mutation, and consumption. This local-command contract states only that
`focus`, `fcodex`, and `focusctl` are not write entries for these Web settings,
and a main-turn lease does not own them.

In the separate Web navigation state,
`WebWriterProfileStore.selected_thread_id` is the sole durable semantic
selection. Web `/cd`, attachment scope, meta, and scope
generation read that value, never a process cache. The separate
`WebDocumentRegistry.materialized_thread_id` proves only bounded-history
readiness; loading older history requires both values to equal the requested
target. Desired subscription edges belong only to
`WebRuntimeInterestRegistry` and are not another selection.

When upstream makes a selected target unusable, Focus atomically clears only
an exact durable match to draft and increments its generation once; repeated
cleanup is a no-op. It preserves a replacement materialization and converges
all runtime-interest edges for each cleared document. This automatic clear
does not behave like a user-requested same-cwd `/cd` rebind: archive,
not-found, and loaded-elsewhere records remain isolated in the old thread
scope, while confirmed delete and invalid direct `ThreadSpawn` delete that
scope. A post-commit `profile_changed` invalidation carries no profile copy;
the browser must re-read its own meta. Navigation generation neither orders nor
settles settings generation.

## 3. Removed project-owned settings

The project no longer supports:

- legacy project-owned profile commands
- `/memory`
- `focusctl thread memory`
- any project-owned thread-memory or provider restore semantics

If an operator wants upstream profile/provider behavior, they must use
upstream Codex config, upstream profile-v2 files, or upstream launch
parameters directly.

## 4. Current meaning of `focus` / `fcodex -p/--profile`

The project no longer treats `focus -p/--profile` or `fcodex -p/--profile` as
a persisted mutation entry.

Its role is now:

- an upstream / local-TUI launch parameter
- not a local mirror of any Feishu command
- not something this project persists as thread truth

## 5. What `focus resume` / `fcodex resume` still promises

`focus resume <thread_id|thread_name>` and
`fcodex resume <thread_id|thread_name>` now promise:

- thread identity resolution
- live-runtime-owner / loaded-gate fail-close behavior
- attaching to the correct instance backend

They are continuation entry points, not ownership credentials. An ordinary
`turn/start` from a live `fcodex` endpoint needs an exact direct root and exact
request tracking, then keeps upstream start-or-steer semantics without reading
or acquiring a main-turn lease. Review, compact, autonomous continuation, and
resume that may autostart instead retain the method-specific admission defined
by [the main-turn owner contract](root-operation-owner.md) and the
[`fcodex` owner contract](fcodex-operation-owner.md). Once a live `fcodex`
endpoint is attached to the exact direct root, it may steer or interrupt that
root's exact current turn under the effect-specific boundary without claiming
or transferring the writer. Answering a server request instead requires the
exact callback admission in `server-request-lifecycle.md`. Being an existing
observer, finding no visibly live frontend, or observing a disconnected
Web/Feishu/`fcodex` surface grants no broader takeover authority. `resume`
itself is neither takeover nor a writer credential.

It no longer promises:

- restoring a project-owned profile slice
- restoring a project-owned memory/provider slice

## 6. One maintenance rule

If a new setting is introduced into this project, its owner, frontend scope,
and application boundary must first be classified as exactly one of:

1. binding-wise next-turn settings
2. instance-wide `WebNextTurnSettings`
3. another formally contracted explicit owner/scope
4. read-only upstream/diagnostic view

Until that classification exists, the project must not add a new local command
surface or implicit cross-frontend setting for it.
