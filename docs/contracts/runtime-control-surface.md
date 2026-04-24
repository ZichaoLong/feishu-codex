# Runtime Control Surface

Chinese original: `docs/contracts/runtime-control-surface.zh-CN.md`

This document defines the shared state vocabulary and control contract across:

- Feishu commands
- the local `feishu-codexctl` admin CLI
- the shared app-server backend

It answers four questions:

- what `/status` is actually describing
- what `/preflight` may dry-run and must not mutate
- what `/unsubscribe` releases and does not release
- why local runtime-release actions must go through the running `feishu-codex` service rather than directly calling app-server from a separate CLI connection

See also:

- `docs/contracts/feishu-thread-lifecycle.md`
- `docs/contracts/session-profile-semantics.md`
- `docs/decisions/shared-backend-resume-safety.md`

## 1. Upstream Baseline

- Upstream project: [`openai/codex`](https://github.com/openai/codex.git)
- Current local verification baseline: `codex-cli 0.118.0` (2026-04-03)

## 2. Shared State Vocabulary

These terms are the shared factual vocabulary used by Feishu `/status`,
`/preflight`, and the local `feishu-codexctl` admin surface.

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

Fallback value in Feishu status/admin surfaces when the backend could not be
read at all:

- `unknown`
- `missing`
- `error`

These fallback values are not backend-native thread states:

- `unknown`: the current surface could not determine the backend status field
- `missing`: the current surface confirmed the thread does not exist
- `error`: one backend read attempt failed

### 2.4 `backend running turn`

A derived judgment:

- `yes` when `backend thread status == active`
- otherwise `no`

This answers whether the backend is currently executing a turn on that thread.
It does not mean the current Feishu chat owns that execution.

### 2.5 `interaction owner`

This is the same-instance, cross-frontend turn / interaction lease. In the current contract, it is the only same-instance frontend owner shared by Feishu and `fcodex`.

It answers:

- which frontend may start the next turn on the thread
- who may handle interrupts, approvals, and user-input requests

Typical holders:

- one Feishu binding
- one `fcodex` TUI proxy holder

Ordinary reply streams are not interaction requests: replies from the same backend are broadcast to same-instance Feishu subscribers and `fcodex` subscribers. Approval, user-input, and interrupt requests are routed only to the current `interaction owner`.

The old `Feishu write owner` is no longer a separate product concept; Feishu prompt admission no longer maintains an additional Feishu-only write lease.

## 3. State Combinations And Transitions

### 3.1 Lease comparison

| Fact | Scope | Question it answers | Can exist while `feishu runtime == released`? |
| --- | --- | --- | --- |
| `feishu runtime` = `attached/released` | Feishu service connection | Is the running Feishu service still attached to the thread at all? | This is the state itself |
| `interaction owner` | Cross-frontend (`feishu-codex` + `fcodex`) | Who may start turns and handle interrupts, approvals, and user-input requests? | Yes. An external owner such as `fcodex` may still exist |

Practical consequence:

- `attached + no owner` is a valid idle state
- `released + external interaction owner` is also valid when another frontend
  still keeps the thread live

### 3.2 Important valid combinations

#### `bound + attached + idle + no owner`

The chat still points to the thread, Feishu is still attached, and there is no
current turn owner. This is the normal idle steady state after a turn finishes.

#### `bound + attached + active + current binding is owner`

The binding is attached and currently owns the cross-frontend interaction lease for the running turn.

#### `bound + released + notLoaded`

The binding remains, Feishu has released runtime residency, and the backend has
also unloaded the thread.

This is the clearest state in which thread-wise profile writes are allowed.

#### `bound + released + idle/active`

Feishu has already released its own runtime residency, but some external subscriber
still keeps the thread loaded in the backend.

The most common case is local `fcodex`.

So `released` does not imply `notLoaded`.

### 3.3 Formal transition table

The table below is authoritative for Feishu-facing state transitions.

| Current binding | Current `feishu runtime` | Current backend | Event | Guard | Next binding | Next `feishu runtime` | Next backend | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `unbound` | `not-applicable` | `not-applicable` | ordinary prompt or `/new` | accepted | `bound` | `attached` | `idle` or `active` | Creates a new thread, then starts or prepares the turn |
| `unbound` | `not-applicable` | any | `/resume <thread>` | target resolved and allowed | `bound` | `attached` | usually `idle` | Binds the chat to the resumed thread |
| `bound` | `attached` | `idle` | ordinary prompt | prompt preflight passes | `bound` | `attached` | `active` | Acquires the interaction owner for the turn |
| `bound` | `attached` | `active` | turn terminal event | none | `bound` | `attached` | usually `idle` | Clears the interaction owner; binding and attachment remain |
| `bound` | `attached` | `idle` or `active` | `/unsubscribe` | no Feishu in-flight turn and no pending Feishu approval / input | `bound` | `released` | `notLoaded`, `idle`, or `active` | Unsubscribe releases Feishu residency across the whole running service |
| `bound` | `released` | `notLoaded` or `idle` | ordinary prompt | prompt preflight passes | `bound` | `attached` | `active` | Feishu reattaches / resumes first, then starts the turn |
| `bound` | `released` | any | ordinary prompt | prompt preflight denied | unchanged | unchanged | unchanged | Pure reject: no resume, no subscriber add, no `released -> attached` flip |
| `bound` | `attached` or `released` | any | `/new` or `/resume <other>` | accepted | `bound` to another thread | `attached` | usually `idle` | Replaces the current binding with the new target |
| `bound` | `attached` or `released` | any | explicit clear / archive current binding / chat unavailable cleanup | accepted | `unbound` | `not-applicable` | `not-applicable` for Feishu binding | Clears the Feishu binding and any Feishu-local execution anchor |

### 3.4 Non-ambiguous rules

- `all`-mode exclusivity is evaluated against current Feishu runtime occupancy
  on the thread, not against a merely remembered `bound + released` bookmark.
- A denied prompt is a pure reject.
  It must not call `thread/resume`, add a Feishu subscriber, or mutate
  `feishu runtime` from `released` to `attached`.
- Releasing Feishu runtime drops Feishu residency and clears the interaction owner when Feishu currently owns it, but
  it does not erase the chat's binding bookmark.

## 4. `/status` Contract

Feishu `/status` is chat-scoped.

Even when it is triggered from inside a group chat, it still targets the
**current group binding**, not an arbitrary thread. Whether it may be triggered
in that group is governed by the group-command rules in
`docs/contracts/group-chat-contract.md`.

It answers, for the current chat binding:

- `binding`
- `feishu runtime`
- `backend thread status`
- `backend running turn`
- `interaction owner`
- `re-profile possible`
- whether `/unsubscribe` is currently allowed

Here, `re-profile possible` means thread-wise profile writes are currently
allowed because the thread is verifiably globally unloaded. Backend-unavailable,
`unknown`, or otherwise unreadable states must resolve to `no`, not a permissive
maybe.

When `/status` or local admin surfaces need to explain a deny / blocked result,
they may expose both:

- a stable `reason_code`
- a human-facing explanation text

The code is the automation-stable key; the text is for operators.

It is not a global thread-management command.
Global binding/thread inspection belongs to `feishu-codexctl`.

### 4.1 `/preflight` Contract

Feishu `/preflight` is also chat-scoped and targets the current chat binding.

It is a read-only dry-run surface. It may explain what would happen next, but
it must not:

- start a turn
- call `thread/resume`
- add a subscriber
- mutate binding / runtime / owner state
- clear or write local profile state

It reuses the same prompt preflight checks as ordinary prompts and the same
availability checks as `/unsubscribe`. When it renders a deny /
blocked result, it may expose the same stable `reason_code` plus human-facing
text.

If the current binding is `bound + released`, `/preflight` may only report
whether the next ordinary prompt would be accepted. It must not flip
`released` back to `attached`.

## 5. Exact Contract of `/unsubscribe`

### 5.1 Scope

Feishu `/unsubscribe`:

- takes no arguments
- targets the current chat’s bound thread
- but semantically releases Feishu runtime residency for that thread across the whole running `feishu-codex` service

Even when it is triggered from a group chat, this remains a chat-scoped entry
for the **current group binding**, not a global arbitrary-thread admin command.
Whether it may be triggered in that group is still governed by the
group-command rules in `docs/contracts/group-chat-contract.md`.

It is not a per-chat “soft local flag”.

### 5.2 What it does

On success it:

- keeps all Feishu bindings that point to that thread
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

### 5.4 When it must be rejected

This command must reject release when:

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

If a Feishu binding remains `bound` but its `feishu runtime == released`, then
the next ordinary prompt in that chat:

- runs the normal prompt preflight first
- must be a pure reject if preflight denies it, with the binding staying
  `released`
- may only reattach / resume and start a new turn after preflight accepts it

This document owns the runtime-admission and pure-reject rule only.
If the accepted path hits an unloaded thread, profile / provider resolution is
owned by `docs/contracts/session-profile-semantics.md`.

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

### 6.3 Current formal command set

The current formal command set is:

- `feishu-codexctl instance list`
- `feishu-codexctl service status`
- `feishu-codexctl binding list`
- `feishu-codexctl binding status <binding_id>`
- `feishu-codexctl binding clear <binding_id>`
- `feishu-codexctl binding clear-all`
- `feishu-codexctl thread status (--thread-id <id> | --thread-name <name>)`
- `feishu-codexctl thread bindings (--thread-id <id> | --thread-name <name>)`
- `feishu-codexctl thread unsubscribe (--thread-id <id> | --thread-name <name>)`
- `feishu-codexctl thread admissions`
- `feishu-codexctl thread import (--thread-id <id> | --thread-name <name>)`
- `feishu-codexctl thread revoke (--thread-id <id> | --thread-name <name>)`

### 6.4 Contract for binding persistence and reset

`binding` is a local fact that should persist across Feishu service restarts.

That persistence exists so a Feishu chat can still remember which thread it
should continue with by default after restart. This is a Feishu-side bookmark,
not Codex-owned thread metadata.

At the same time, "clear one or all bindings" is a legitimate local admin need,
especially for:

- development-time bulk reset
- operational recovery
- forcing Feishu back to a fresh "pick a thread again" state

The formal contract is:

- this is a `binding`-layer action, not a `thread runtime` action
- it is not the same thing as `/unsubscribe`
- its formal surface belongs to `feishu-codexctl`

So the formal admin surface is:

- `feishu-codexctl binding clear <binding_id>`
- `feishu-codexctl binding clear-all`

Those actions mean:

- clear Feishu-side remembered binding facts
- consistently clear the running Feishu in-memory state and persisted state
- release any Feishu-local lease, execution-anchor, or subscription state that
  must disappear with that binding

They do not mean:

- delete a Codex thread
- archive a thread
- replace `/unsubscribe`
- replace thread-level admin commands

The distinction is intentional:

- `/unsubscribe`
  - releases Feishu runtime residency for a thread
  - does not clear the binding bookmark
- `binding clear/clear-all`
  - clears Feishu-local binding bookmarks
  - should no longer exist as a separate architectural concept of "just delete
    `chat_bindings.json`"

### 6.5 `binding_id` shape

The local admin CLI uses stable admin-facing binding ids:

- group binding: `group:<chat_id>`
- p2p binding: `p2p:<sender_id>:<chat_id>`

These are local admin identifiers. They do not need to mirror Feishu command names.
In this project, `binding_id` is a restricted admin-facing syntax, not a
generic reversible serializer for arbitrary strings:

- `:` is a reserved separator
- `sender_id` and `chat_id` components must not contain `:`
- if a real upstream id format ever requires `:`, the syntax should be replaced
  explicitly rather than relying on the current concatenation format to round-trip silently

### 6.6 Explicit thread target contract

For the local admin surface, thread targeting is intentionally explicit.

- `--thread-id <id>`
  - means exact thread-id addressing
  - does not fall back to name lookup
- `--thread-name <name>`
  - means exact thread-name matching
  - uses the same shared cross-provider global listing filters as the session
    discovery surface
  - keeps scanning later pages until uniqueness or ambiguity is proven
  - rejects zero matches
  - rejects multiple exact-name matches

The control plane follows the same rule.
It no longer accepts an untyped union `target` that guesses whether the input
was an id or a name.

### 6.7 Single service owner per `FC_DATA_DIR`

For one `FC_DATA_DIR`, there must be exactly one running `feishu-codex`
service owner.

The contract is:

- ownership is established before adapter/control-plane startup
- a second instance must fail fast
- the control endpoint is not the ownership primitive
- the owner writes metadata including `owner_pid`, `owner_token`, and
  `control_endpoint`
- the on-disk owner metadata contains a local control token and must be treated
  as sensitive local state; on Windows this relies on the current user's
  profile path and NTFS ACLs rather than POSIX `0600` semantics
- if startup fails after ownership is acquired, partially started runtime
  components must be fully rolled back before the lease is released
- shutdown may only clean up ownership metadata that still belong to the same
  owner token

Therefore `feishu-codex run` and a background service started via
`feishu-codex start` must not coexist on the same `FC_DATA_DIR`.
If both point at the same directory, the later starter must exit instead of
trying to replace the published control endpoint.

### 6.8 Instance Scope, Admission, and Global Coordination

In multi-instance mode, `feishu-codexctl` deliberately splits into two scopes:

- `instance list`
  - machine-scoped
  - reads the global running-instance registry
  - does not target one specific instance
- all other subcommands
  - instance-scoped
  - operate on one running `feishu-codex` service
  - may select it explicitly via `--instance <name>`; otherwise they resolve
    by current / unique-running / default-running rules and must fail when the
    target remains ambiguous

The formal contract for instance visibility is:

- the `default` instance preserves the original single-instance globally visible
  Feishu surface
- named instances are `admission-scoped` by default
- `thread admissions`
  - lists the thread set currently admitted into this instance
- `thread import`
  - only makes a persisted thread visible on this instance's Feishu surface
  - does not bind the thread
  - does not load the thread into this instance backend
  - does not immediately acquire a live runtime lease
- `thread revoke`
  - only removes this instance's admission for the thread
  - must reject while a binding still points at that thread

There are also two machine-level coordination facts:

- `InstanceRegistry`
  - records which instances are currently running and how local CLIs can reach
    their control endpoint / backend endpoints
  - used by `fcodex` and `feishu-codexctl instance list`
- `ThreadRuntimeLease`
  - records which instance currently owns live backend runtime residency for a
    thread
  - allows multiple holders for the same thread only when they come from the
    same instance
  - rejects concurrent live attachment from different instances

The formal contract for cross-instance live-runtime transfer is:

- if the current owner instance can release Feishu runtime immediately,
  automatic transfer is allowed
- if the current owner instance is still executing, or still has pending
  approval / input, the write attempt must reject clearly
- no queueing, no implicit stealing, and no "last binder wins" guesswork

This `ThreadRuntimeLease` is a machine-level live-runtime fact.
It is not the same thing as Feishu chat binding or interaction owner.

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
