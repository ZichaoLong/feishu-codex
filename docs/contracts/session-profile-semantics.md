# Session, Resume, and Profile Semantics

This document defines the intended user-facing semantics for:

- Feishu commands
- `fcodex` shell-wrapper commands
- upstream Codex commands inside a running TUI

If current implementation diverges from this document, treat the gap as a bug,
an implementation limitation, or explicitly documented future work. This file
describes the target contract, not every current quirk.

## 1. Three Semantic Layers

There are three distinct command surfaces:

1. Feishu commands such as `/session`, `/resume`, `/profile`
2. `fcodex` shell-wrapper commands such as `fcodex /session`
3. Upstream Codex commands inside a running TUI, such as TUI `/resume`

They may share backend state, but they are not interchangeable.
More precisely: the underlying backend thread/session data may be the same, but
"how candidates are discovered, how they are matched, whether a thread is
resumed, and how the current client binds to it afterward" belong to different
semantic layers.

## 2. Feishu Semantics

### `/session`

- Scope: current directory only
- Visibility scope:
  - `default` instance: equivalent to the current backend's full current-directory thread list
  - named instance: only threads visible to the current instance; the visible set is `admission + current instance bindings`
- Provider behavior: cross-provider aggregation
- Purpose: browse threads relevant to the current directory
- Sorting: most recently updated first

### `/resume <thread_id|thread_name>`

- Scope:
  - `default` instance: backend-global
  - named instance: the current instance-visible thread set, meaning admitted threads or threads already bound in this instance
- Provider behavior: cross-provider
- Matching:
  - exact thread id, or
  - exact thread name
  - exact-name matching uses the same shared cross-provider global listing
    algorithm as the session surface, but still obeys the current instance's
    visibility policy; it keeps scanning later pages until it can prove a
    unique match or ambiguity
- Error behavior:
  - zero matches: error
  - multiple exact-name matches: error
- Success behavior:
  - immediately enters a background resume flow and shows a pending hint
  - resume the target thread
  - switch the Feishu chat to the thread's own working directory

### `/profile [name]`

- Reads or changes the local default profile of the current instance
- Affects:
  - the current instance's Feishu-side default profile
  - new `fcodex` launches routed to the same instance when they do not pass `-p/--profile`
- Does not affect:
  - other instances' local default profiles
  - bare `codex` global config
  - already-running TUI instances

### `/rm [thread_id|thread_name]`

- Uses Codex public archive semantics
- This is not hard deletion
- Upstream Codex moves the persisted rollout JSONL from `sessions/` to `archived_sessions/`
- The thread is also marked archived in persisted metadata
- Archived threads are hidden from default `thread/list` results unless an archived filter is explicitly requested
- Upstream Codex supports `thread/unarchive`, but `feishu-codex` does not expose a dedicated `/unarchive` command today

### `/release-feishu-runtime`

- This is Feishu-only and is not part of the shared surface between Feishu and the `fcodex` wrapper
- It targets the current chat's bound thread, but what it releases is Feishu service runtime residency on that thread
- It does not clear the chat binding and does not delete or archive the thread
- It shares the same runtime vocabulary as `feishu-codexctl thread release-feishu-runtime`, but it is not the same entry surface
- Its exact state transitions, blockers, and pure-reject rules are defined in `docs/contracts/runtime-control-surface.md`

## 3. `fcodex` Shell-Wrapper Semantics

### Instance Routing

In multi-instance mode, `fcodex` always selects one target instance backend
before it enters either the wrapper flow or the upstream Codex flow.

The default routing rule is:

- if `--instance <name>` is given, use that instance
- otherwise, if the target `thread_id` already has a global live-runtime lease,
  prefer that owner instance
- otherwise, if exactly one instance is running, use it
- otherwise, if the `default` instance is running, use `default`
- otherwise, if multiple running instances remain and the target is still
  ambiguous, fail and require explicit `--instance`
- if no instance is currently running, fall back to the locally inferred
  current/default instance directory

This auto-routing only decides which instance backend `fcodex` will connect to.
It does not rewrite Feishu-side bindings, admissions, or owner contracts.

### Plain `fcodex`

These remain upstream Codex CLI entrypoints:

- `fcodex`
- `fcodex <prompt>`
- `fcodex resume <thread_id>`

Wrapper-specific behavior:

- they connect to the selected instance's shared backend by default
- they inherit the selected instance's local default profile unless
  `-p/--profile` is given
- their working directory is the explicit `--cd` value, or the current shell cwd
- if `--cd` / `-C` is passed explicitly but its value is missing, the wrapper
  should fail fast instead of silently falling back to the current cwd

## Wrapper Commands

These are handled by the `fcodex` wrapper itself:

- `fcodex /help`
- `fcodex /profile [name]`
- `fcodex /rm <thread_id|thread_name>`
- `fcodex /session [cwd|global]`
- `fcodex /resume <thread_id|thread_name>`

These five commands are the entire shared surface between Feishu and the
`fcodex` wrapper. Upstream `/help`, `/resume`, `/profile`, and other commands
inside a running TUI session are intentionally outside this shared surface.

Whenever Feishu help cards, session cards, or wrapper help text reference one
of these commands, they should reuse this shared surface contract rather than
carrying a forked hardcoded usage string.

They must be used as standalone wrapper commands. They are intentionally not
mixed with bare `codex` flags or subcommands.

There is one extra multi-instance difference to remember:

- `fcodex` reuses the same paging and exact-match algorithm as Feishu
- but it is operator-local by default and does not read named-instance
  `thread admission` filtering
- therefore `fcodex /session` and `fcodex /resume <name>` can see a broader
  surface than a named Feishu instance

### `fcodex /session [cwd|global]`

- Target: the selected instance backend
- `fcodex /session`
  - current directory only
  - cross-provider aggregation
- `fcodex /session global`
  - backend-global
  - cross-provider aggregation

This command reuses the same shared discovery algorithm as Feishu, not the
upstream TUI picker. But it does not read the named-instance `thread admission`
store; it is an operator-local discovery surface.

### `fcodex /resume <thread_id|thread_name>`

- `thread_id`
  - pass through to upstream `codex resume <id>` on the selected instance backend
- `thread_name`
  - resolve through the shared `feishu-codex` discovery layer
  - exact-name match
  - global within the selected instance backend
  - cross-provider
  - uses the same shared global listing filters as `fcodex /session global`,
    but keeps scanning later pages until uniqueness or ambiguity is proven
  - zero matches: error
  - multiple exact-name matches: error

After a unique match, the wrapper resumes by thread id through the selected
instance's shared backend. If another instance currently owns the live runtime,
actual attachment still follows the global `thread runtime lease` transfer /
reject rules.

### `fcodex --dry-run /session` and `fcodex --dry-run /resume`

`--dry-run` is a local read-only diagnostic prefix owned by the `fcodex`
wrapper. It is not a command inside the running TUI.

- `fcodex --dry-run /session [cwd|global]`
  - reuses `fcodex /session [cwd|global]` discovery rules
  - explicitly marks the operation as read-only
  - does not start the TUI
- `fcodex --dry-run /resume <thread_id|thread_name>`
  - reuses `fcodex /resume <thread_id|thread_name>` target resolution
  - reports the selected instance, resolved thread, default profile, and thread
    runtime lease check
  - does not start the TUI and does not call `thread/resume`
  - does not clear stale profiles or write any local state

Like ordinary `fcodex /session` and `fcodex /resume`, it does not read
named-instance `thread admission` filtering; it is an operator-local preflight.

### `fcodex /profile [name]`

- Reads or changes the same local default-profile state used by Feishu
- Does not rewrite bare `codex` global config
- `fcodex -p <profile>` always wins over the saved local default

### `fcodex /rm <thread_id|thread_name>`

- Uses Codex archive semantics
- Not hard deletion
- Uses the same underlying archive behavior as Feishu `/rm`
- The persisted rollout JSONL is preserved under `archived_sessions/`, rather than deleted

## 4. TUI-Inside Semantics

Once the user is inside a running `fcodex` TUI session:

- `/help` is upstream Codex help
- `/resume` is upstream Codex resume behavior
- `/profile` is upstream Codex behavior

Important consequence:

- TUI `/resume` is not equivalent to Feishu `/resume`
- TUI `/resume` is not equivalent to `fcodex /resume <name>`
- TUI `/resume` does not reuse `feishu-codex` cross-provider name matching
- Feishu `/mode` changes only the mode that future Feishu-started turns will send
- TUI collaboration-mode changes affect only future TUI-started turns
- shared backend means shared live thread state, not one globally synchronized collaboration-mode control plane

Treat the TUI as upstream behavior running on a shared backend, not as an
extension of the wrapper command surface.

### Collaboration Mode Scope

- Feishu `/mode` does not immediately rewrite what TUI `/collab` shows
- TUI `/collab` is not an authoritative view of the current Feishu chat's pending next-turn mode
- whichever client actually starts the next `turn/start` determines the collaboration mode for that turn
- once a turn starts, that turn's `collaborationMode` can update the backend thread defaults for subsequent turns, until another client explicitly overrides them later

## 5. Profile Contract

`feishu-codex` owns one local default-profile state per instance:

- Feishu `/profile` on that instance
- `fcodex /profile` when routed to that instance

That state is separate from bare Codex global config.

Therefore:

- changing Feishu `/profile` for one instance changes the default used by
  future wrapper launches routed to that instance
- changing `fcodex /profile` for one instance changes the default seen by
  Feishu on that same instance
- one instance's `/profile` does not rewrite any other instance's local
  default profile
- `fcodex -p <profile>` overrides the saved default for that launch only
- bare `codex -p <profile>` is outside this contract

### Profile Resolution During Resume

- When the target thread is currently `not-loaded-in-current-backend`:
  - Feishu `/resume`
  - `fcodex resume <thread_id>`
  - `fcodex /resume <thread_id|thread_name>`
  all ultimately use the current-instance / selected-instance local default
  profile when no explicit profile is provided.
- That is a behavior contract, not a requirement that both paths share the same
  implementation:
  - Feishu resolves the current instance local default profile before
    `thread/resume` and
    explicitly sends the resolved profile / model / model_provider
  - the `fcodex` wrapper injects the selected instance's default profile
    first, then enters the upstream `codex resume` path
- Therefore unloaded-thread resume should be understood as "same behavior,
  different execution path", not as two conflicting profile rules.
- When the target thread is already `loaded-in-current-backend`, `resume`
  reuses the existing live runtime.
  - In that branch, `resume` cannot rewrite the live thread's profile or
    provider
  - explicit `-p/--profile` or other resume-time overrides do not become an
    effective switch there
- If a Feishu binding still points at the thread but its `feishu runtime` is
  already `released`, later ordinary prompts first follow the reattach /
  pure-reject rule defined in `docs/contracts/runtime-control-surface.md`.
  - a denied prompt must remain a pure reject, with the binding staying
    `released`
  - only an accepted prompt may reattach / resume using the bound thread
  - if that accepted path hits a `notLoaded` thread, the unloaded-thread rule
    in this section applies
  - if the thread is still `loaded`, it only reuses the live runtime and
    cannot switch provider through that path
- For that reason, "thread original profile" is not recommended contract
  language in this project.
  Prefer:
  - "resume with the current-instance / selected-instance local default profile"
  - "resume with an explicit profile"

## 6. Safety Rule

Use one rule everywhere:

- if you want local TUI and Feishu to keep operating on the same live thread,
  use `fcodex`, not bare `codex`
- in multi-instance mode, local TUI should connect to the same instance backend
  that actually owns the live thread; `fcodex` auto-routes when it can, and
  requires explicit `--instance` when it cannot disambiguate

`fcodex` is the shared-backend path. Bare `codex` is intentionally outside the
safe shared-thread contract unless it is manually pointed at the same remote
backend.
