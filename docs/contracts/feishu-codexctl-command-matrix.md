# `feishu-codexctl` Command Matrix

Chinese version: `docs/contracts/feishu-codexctl-command-matrix.zh-CN.md`

See also:

- `docs/contracts/feishu-command-matrix.md`
- `docs/contracts/local-command-and-thread-profile-contract.md`
- `docs/contracts/runtime-control-surface.md`
- `docs/contracts/thread-profile-semantics.md`

This document defines the formal local command matrix for `feishu-codexctl`.

It answers five questions:

- which resources `feishu-codexctl` actually manages
- which state layer each subcommand operates on
- which commands are read-only vs mutating
- what the parameter constraints and instance-selection rules are
- which Feishu surfaces each command corresponds to, and which ones it
  intentionally does not

If code and this document disagree, treat that as a contract gap and tighten
the code, the docs, or both.

## 1. Scope

This document only describes the local `feishu-codexctl` surface.

It does not redefine:

- the Feishu slash-command matrix
- `fcodex` wrapper semantics
- thread lifecycle and runtime vocabulary
- the low-level behavior of `reset-backend`, `thread unsubscribe`, `/status`, or
  `/preflight`

Those remain defined by their dedicated docs.

## 2. Positioning

`feishu-codexctl` is the local inspection / management surface.

Its formal role is:

- inspect running instances
- inspect the target instance's service / binding / thread state
- perform a limited set of binding / thread management actions

It is not:

- a second Codex frontend
- the entrypoint for entering the TUI
- a one-to-one local mirror of every Feishu chat-scoped command

Therefore:

- use `fcodex` to continue a live thread
- use `feishu-codex` to manage local service lifecycle, autostart, install,
  and instances
- use `feishu-codexctl` to inspect binding / thread / service state or perform
  thread-scoped management

## 3. Global Rules

### 3.1 Instance selection

- every command except `instance list` accepts `--instance <name>`
- if `--instance` is omitted, the target instance defaults to `default`
- `instance list` is a cross-instance inspection surface and does not use
  `--instance`
- current `feishu-codexctl` accepts only one `--instance`; unlike
  `feishu-codex`, it does not support batch multi-instance operations

### 3.2 Resource layering

The command surface is split into four resource classes:

- `instance`
  - the running-instance registry
- `service`
  - a target instance's service / control-plane / backend overview
- `binding`
  - the local Feishu chat-binding facts inside an instance
- `thread`
  - persisted thread discovery plus thread-scoped management for a target
    thread

### 3.3 Thread target constraints

For the following thread subcommands:

- `thread status`
- `thread bindings`
- `thread unsubscribe`

the caller must provide exactly one of:

- `--thread-id <id>`
- `--thread-name <name>`

This is a hard surface constraint, not a recommendation.

### 3.4 `binding clear` is not `thread unsubscribe`

`binding clear` / `clear-all`:

- clears Feishu-side local bookmarks
- does not delete the thread
- is not `thread unsubscribe`

`thread unsubscribe`:

- releases Feishu runtime residency for the target thread
- keeps the thread and binding relationships intact

These two actions operate on different state layers and must not be conflated
in code, docs, or product wording.

## 4. Command Matrix

### 4.1 `instance` resource

| Command | Purpose | State layer | Type | Key parameters | Feishu counterpart |
| --- | --- | --- | --- | --- | --- |
| `feishu-codexctl instance list` | List currently running local instances, owner pid, control endpoint, and app-server address | running-instance registry | read-only | none; does not use `--instance` | no direct Feishu counterpart |

### 4.2 `service` resource

| Command | Purpose | State layer | Type | Key parameters | Feishu counterpart |
| --- | --- | --- | --- | --- | --- |
| `feishu-codexctl [--instance <name>] service status` | Show the target instance's current service state, control endpoint, app-server address, and binding / thread counts | instance-level service / control-plane overview | read-only | optional `--instance` | no single exact Feishu equivalent; this is closer to an instance-admin view |
| `feishu-codexctl [--instance <name>] service reset-backend [--force]` | Reset the current instance backend without restarting the `feishu-codex` service | instance-level backend lifecycle | mutating | optional `--instance`; optional `--force` | corresponds to Feishu `/reset-backend`, but this is the local instance-admin surface |

### 4.3 `binding` resource

| Command | Purpose | State layer | Type | Key parameters | Feishu counterpart |
| --- | --- | --- | --- | --- | --- |
| `feishu-codexctl [--instance <name>] binding list` | List visible bindings in the target instance, including binding state, Feishu runtime state, associated thread, and cwd | instance-local binding discovery | read-only | optional `--instance` | no direct Feishu counterpart; lower-level than Feishu `/threads` and `/status` |
| `feishu-codexctl [--instance <name>] binding status <binding_id>` | Show a single binding's chat, thread, runtime, next-prompt availability, interaction owner, and current session settings | single-binding detailed state | read-only | `binding_id` | covers and exceeds Feishu `/status` and `/preflight` |
| `feishu-codexctl [--instance <name>] binding clear <binding_id>` | Clear a single binding bookmark | single-binding bookmark | mutating | `binding_id` | no direct Feishu counterpart |
| `feishu-codexctl [--instance <name>] binding clear-all` | Clear all binding bookmarks in the target instance | all binding bookmarks in one instance | mutating | optional `--instance` | no direct Feishu counterpart |

### 4.4 `thread` resource

| Command | Purpose | State layer | Type | Key parameters | Feishu counterpart |
| --- | --- | --- | --- | --- | --- |
| `feishu-codexctl [--instance <name>] thread list [--scope cwd\|global] [--cwd <path>]` | List persisted threads; defaults to current-directory filtering, but also supports a global view | persisted-thread discovery | read-only | optional `--instance`; `--scope cwd/global`; `--cwd` is meaningful only for `cwd` scope | partially corresponds to Feishu `/threads` and `/resume` target discovery |
| `feishu-codexctl [--instance <name>] thread status (--thread-id <id> \| --thread-name <name>)` | Show one thread's backend status, bound / attached / released bindings, interaction owner, and `/release-runtime` availability | single thread's thread-scoped state | read-only | exactly one of `--thread-id` or `--thread-name` | no single exact Feishu equivalent; overlaps the lower-level diagnostics behind Feishu `/status`, `/preflight`, and `/release-runtime` |
| `feishu-codexctl [--instance <name>] thread bindings (--thread-id <id> \| --thread-name <name>)` | Show the binding list currently associated with a target thread | reverse mapping from a thread to bindings | read-only | exactly one of `--thread-id` or `--thread-name` | no direct Feishu counterpart |
| `feishu-codexctl [--instance <name>] thread unsubscribe (--thread-id <id> \| --thread-name <name>)` | Make Feishu release runtime residency for a target thread while keeping thread and binding relationships intact | Feishu runtime residency for one thread | mutating | exactly one of `--thread-id` or `--thread-name` | corresponds to Feishu `/release-runtime`, but is thread-scoped rather than current-chat-scoped |

## 5. Mapping to the Feishu command surface

### 5.1 Surfaces with a reasonably clear counterpart

| `feishu-codexctl` | Closest Feishu surface | Key difference |
| --- | --- | --- |
| `service reset-backend` | `/reset-backend` | both are instance-level backend management; Feishu is an admin card flow, local is a CLI admin flow |
| `binding status <binding_id>` | `/status`, `/preflight` | local output is lower-level and includes binding id, interaction owner, reason codes, and other debugging details |
| `thread unsubscribe --thread-id/--thread-name` | `/release-runtime` | Feishu `/release-runtime` only targets the current chat binding; the local command can target any thread directly |
| `thread list --scope cwd` | `/threads` | Feishu `/threads` is a chat usage surface; the local command is only thread discovery |
| `thread list --scope global` / `thread status` | `/resume` target discovery and diagnosis | Feishu `/resume` is a resume action; the local commands are inspection / management surfaces and do not enter the live thread |

### 5.2 Surfaces intentionally without a Feishu counterpart

The following local commands intentionally have no one-to-one Feishu command:

- `instance list`
- `service status`
- `binding list`
- `binding clear`
- `binding clear-all`
- `thread bindings`

Reasons:

- they belong to a local admin / debugging perspective
- exposing them directly in Feishu would raise cognitive load for ordinary
  users
- some of them, such as `binding clear`, are pure local cleanup surfaces rather
  than part of the day-to-day chat contract

## 6. Output and mental model

When reading command output, the current contract recommends this model:

- `instance`
  - answers “which instances are actually running right now”
- `service`
  - answers “what is the state of this instance's background service and
    control plane”
- `binding`
  - answers “which thread does this Feishu conversation currently point to,
    and can it continue directly”
- `thread`
  - answers “what is this thread's current backend state, which bindings point
    at it, and can Feishu release runtime residency for it”

The most important distinction is:

- `binding` is a chat-scoped view
- `thread` is a thread-scoped view

They must not be read as interchangeable views.

## 7. Related implementation fact sources

The main implementation fact sources for this document include:

- `bot/feishu_codexctl.py`
- `bot/runtime_admin_controller.py`
- `bot/instance_resolution.py`
- `bot/thread_resolution.py`
- `bot/service_control_plane.py`

If any future change adds, removes, renames, or re-scopes a `feishu-codexctl`
subcommand, or changes instance-selection rules, thread-target constraints,
state-layer boundaries, or Feishu-command correspondence, this document must be
updated together with that change.
