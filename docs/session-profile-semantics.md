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
- Provider behavior: cross-provider aggregation
- Purpose: browse threads relevant to the current directory
- Sorting: most recently updated first

### `/resume <thread_id|thread_name>`

- Scope: backend-global
- Provider behavior: cross-provider
- Matching:
  - exact thread id, or
  - exact thread name
- Error behavior:
  - zero matches: error
  - multiple exact-name matches: error
- Success behavior:
  - immediately enters a background resume flow and shows a pending hint
  - resume the target thread
  - switch the Feishu chat to the thread's own working directory

### `/profile [name]`

- Reads or changes the `feishu-codex` local default profile
- Affects:
  - Feishu-side default profile
  - new `fcodex` launches that do not pass `-p/--profile`
- Does not affect:
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
- It only releases Feishu's own runtime residency for the currently bound thread
- It does not clear the chat binding and does not delete or archive the thread
- If the thread still remains `loaded` afterward, some external subscriber is still attached, most commonly local `fcodex`
- If the thread becomes `notLoaded` afterward, whether the next restore may re-profile follows the profile contract in Section 5 below

## 3. `fcodex` Shell-Wrapper Semantics

## Plain `fcodex`

These remain upstream Codex CLI entrypoints:

- `fcodex`
- `fcodex <prompt>`
- `fcodex resume <thread_id>`

Wrapper-specific behavior:

- they connect to the `feishu-codex` shared backend by default
- they inherit the local default profile unless `-p/--profile` is given
- their working directory is the explicit `--cd` value, or the current shell cwd

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

They must be used as standalone wrapper commands. They are intentionally not
mixed with bare `codex` flags or subcommands.

### `fcodex /session [cwd|global]`

- `fcodex /session`
  - current directory only
  - cross-provider aggregation
- `fcodex /session global`
  - backend-global
  - cross-provider aggregation

This command uses the same shared discovery logic as Feishu, not the upstream
TUI picker.

### `fcodex /resume <thread_id|thread_name>`

- `thread_id`
  - pass through to upstream `codex resume <id>`
- `thread_name`
  - resolve through the shared `feishu-codex` discovery layer
  - exact-name match
  - backend-global
  - cross-provider
  - zero matches: error
  - multiple exact-name matches: error

After a unique match, the wrapper resumes by thread id through the shared
backend.

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

There is one local default-profile state owned by `feishu-codex`:

- Feishu `/profile`
- `fcodex /profile`

That state is separate from bare Codex global config.

Therefore:

- changing Feishu `/profile` changes the default used by future wrapper launches
- changing `fcodex /profile` changes the default seen by Feishu
- `fcodex -p <profile>` overrides the saved default for that launch only
- bare `codex -p <profile>` is outside this contract

### Profile Resolution During Resume

- When the target thread is currently `not-loaded-in-current-backend`:
  - Feishu `/resume`
  - `fcodex resume <thread_id>`
  - `fcodex /resume <thread_id|thread_name>`
  all ultimately use the current `feishu-codex` local default profile when no
  explicit profile is provided.
- That is a behavior contract, not a requirement that both paths share the same
  implementation:
  - Feishu resolves the local default profile before `thread/resume` and
    explicitly sends the resolved profile / model / model_provider
  - the `fcodex` wrapper injects the default profile first, then enters the
    upstream `codex resume` path
- Therefore unloaded-thread resume should be understood as "same behavior,
  different execution path", not as two conflicting profile rules.
- When the target thread is already `loaded-in-current-backend`, `resume`
  reuses the existing live runtime.
  - In that branch, `resume` cannot rewrite the live thread's profile or
    provider
  - explicit `-p/--profile` or other resume-time overrides do not become an
    effective switch there
- If a Feishu binding still points at the thread but its `feishu runtime` is
  already `released`, the next ordinary message first runs the normal prompt
  preflight.
  - If that prompt is denied, the denial is pure reject and the binding must
    stay `released`
  - Only if the prompt is accepted does Feishu reattach / resume using the
    bound thread, then start the turn
  - If the thread is `notLoaded` at that moment, the unloaded-thread rule in
    this section applies
  - If the thread is still `loaded`, it only reuses the live runtime and
    cannot switch provider through that path
- For that reason, "thread original profile" is not recommended contract
  language in this project.
  Prefer:
  - "resume with the current local default profile"
  - "resume with an explicit profile"

## 6. Safety Rule

Use one rule everywhere:

- if you want local TUI and Feishu to keep operating on the same live thread,
  use `fcodex`, not bare `codex`

`fcodex` is the shared-backend path. Bare `codex` is intentionally outside the
safe shared-thread contract unless it is manually pointed at the same remote
backend.
