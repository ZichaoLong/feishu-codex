# Session, Resume, and Profile Semantics

Chinese original: `docs/contracts/session-profile-semantics.zh-CN.md`

See also:

- `docs/contracts/local-command-and-thread-profile-contract.md`
- `docs/contracts/runtime-control-surface.md`
- `docs/decisions/shared-backend-resume-safety.md`

This document describes the active semantics across three layers:

1. Feishu commands
2. local `fcodex` / `feishu-codexctl`
3. upstream Codex commands after entering the TUI

If older docs still describe `fcodex` shell slash self-commands, this document wins.

## 1. Feishu Semantics

### `/session`

- scope: current directory
- provider behavior: cross-provider aggregation
- all instances: current backend's current-directory threads

### `/resume <thread_id|thread_name>`

- supports exact `thread_id`
- also supports exact `thread_name`
- provider behavior: cross-provider
- all instances: backend-global
- zero matches error; multiple exact-name matches also error

### `/new`

- immediately creates a new thread and switches the chat binding to it
- uses the current instance's effective new-thread default profile as a one-time
  seed for that new thread
- once the thread is really created, the seed is persisted by `thread_id` as thread-wise resume profile state

### `/profile [name]`

- target: the currently bound thread
- if no thread is bound, reject directly
- writes are allowed only when the target thread is verifiably globally
  unloaded
- loaded threads still under the current instance's control are not hot-switched;
  instead, Feishu offers an “apply and reset the current instance backend” path
- force-reset-only cases must show explicit blocking diagnostics and require
  admin/operator confirmation
- if the live runtime owner belongs to another instance, or the current
  instance does not support backend reset, the request is hard-blocked

### `/reset-backend`

- target: the current instance backend, not the current thread
- admin-only
- preview first; execution must require explicit confirmation
- uses the same instance-scoped backend-reset semantics that `/profile` may
  rely on for re-profile recovery
- exists so operators can clear stale loaded / pending runtime state even when
  they are not currently changing a thread profile

### `/unsubscribe`

- target: the thread currently bound by the chat
- releases Feishu-side runtime residency on that thread
- does not clear the binding, delete the thread, or archive it
- exact state vocabulary is defined in `docs/contracts/runtime-control-surface.md`

## 2. Local Command Surface

### `fcodex`

`fcodex` is now a thin wrapper and no longer exposes shell slash self-commands.

The repository-specific surface it still owns is limited to:

1. enhanced `resume` routing and name resolution
2. thread-wise `-p/--profile` integration

That means shell-level support is removed for:

- `fcodex /help`
- `fcodex /session`
- `fcodex /profile`
- `fcodex /rm`
- `fcodex /resume`
- `fcodex --dry-run ...`

### `fcodex resume <thread_id|thread_name>`

- `thread_id`: resume directly on the selected instance shared backend
- `thread_name`: do cross-provider exact-name resolution first, then resume by thread id
- multi-instance routing still follows runtime-lease safety rules
- local resolution is operator-local; live-attach safety is enforced later by runtime-lease acquisition

### `fcodex -p <profile>`

- when this launch is opening a new session rather than resuming:
  - `-p` is passed through to upstream Codex
  - it also becomes a one-time seed for the first new thread created by this launch
- that seed is written only after the first successful `thread/start`
- if no thread is ever created, no thread-wise record is persisted
- ownership is explicit:
  - wrapper chooses whether this launch carries a seed
  - proxy persists that seed only after a real `thread_id` is returned

### `fcodex -p <profile> resume <thread>`

- if the target thread is verifiably globally unloaded:
  - write the thread-wise resume profile for that thread
  - then resume it
- otherwise:
  - reject directly
  - tell the user to `unsubscribe` and close any other open `fcodex` TUIs on that thread

### `fcodex resume <thread>` without explicit `-p`

- if the thread already has saved thread-wise profile state, inject it automatically
- if it does not, do not fall back to the instance's current new-thread default
  profile
- the instance's new-thread default profile now seeds new threads only; it does
  not override old-thread resume

### `feishu-codexctl`

`feishu-codexctl` is the local discovery / inspection / admin surface.

It owns:

- `service status`
- `service reset-backend`
- `thread list --scope cwd|global`
- `thread status`
- `thread bindings`
- `thread unsubscribe`
- `binding list/status/clear`

It is not a second Codex frontend and does not enter the TUI.

## 3. TUI-Inside Semantics

Once inside a running `fcodex` TUI:

- `/help` is upstream Codex `/help`
- `/resume` is upstream Codex `/resume`
- `/new` is upstream Codex `/new`
- all other commands are upstream semantics too

Therefore:

- TUI `/resume` is not Feishu `/resume`
- TUI `/resume` is not `fcodex resume <thread_name>`
- shared backend means shared live thread state, not one globally synchronized settings surface across clients

## 4. Profile Summary

The system no longer treats the instance's new-thread default profile as the
primary resume model.

The active model is:

- Feishu `/profile` changes the next-resume config of the currently bound thread
- `fcodex -p <profile>` on a new session only seeds the first new thread created by that launch
- `fcodex -p <profile> resume <thread>` changes that thread's persisted resume config
- future resume reads the thread's own thread-wise config, not the instance's
  current new-thread default profile
- wrapper and proxy do not co-own the same write path:
  - wrapper owns existing-thread read/write behavior
  - proxy owns one-time persistence of the first new-thread seed

## 5. Multi-Instance Visibility

- all instances share one persisted thread namespace
- Feishu `/session` and `feishu-codexctl thread list --scope cwd` are current-directory views over that namespace
- Feishu `/resume`, `fcodex resume <thread_name>`, and thread-targeted local admin commands resolve against the same global persisted thread set
- runtime-lease routing and transfer safety are defined in `docs/decisions/shared-backend-resume-safety.md`
