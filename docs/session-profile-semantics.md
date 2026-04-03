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

## 2. Feishu Semantics

### `/session`

- Scope: current directory only
- Provider behavior: cross-provider aggregation
- Purpose: browse threads relevant to the current directory
- Sorting: favorites first, then most recently updated

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

## 4. TUI-Inside Semantics

Once the user is inside a running `fcodex` TUI session:

- `/help` is upstream Codex help
- `/resume` is upstream Codex resume behavior
- `/profile` is upstream Codex behavior

Important consequence:

- TUI `/resume` is not equivalent to Feishu `/resume`
- TUI `/resume` is not equivalent to `fcodex /resume <name>`
- TUI `/resume` does not reuse `feishu-codex` cross-provider name matching

Treat the TUI as upstream behavior running on a shared backend, not as an
extension of the wrapper command surface.

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

## 6. Safety Rule

Use one rule everywhere:

- if you want local TUI and Feishu to keep operating on the same live thread,
  use `fcodex`, not bare `codex`

`fcodex` is the shared-backend path. Bare `codex` is intentionally outside the
safe shared-thread contract unless it is manually pointed at the same remote
backend.
