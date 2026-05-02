# Local Command Surface And Thread-Wise Profile Contract

Chinese original: `docs/contracts/local-command-and-thread-profile-contract.zh-CN.md`

See also:

- `docs/contracts/thread-profile-semantics.md`
- `docs/contracts/runtime-control-surface.md`
- `docs/architecture/fcodex-shared-backend-runtime.md`
- `docs/decisions/shared-backend-resume-safety.md`

This document captures the active command-surface and profile/provider contract
that has already been discussed and accepted.

It answers five questions:

- why Feishu uses `/release-runtime` while local surfaces still keep `thread unsubscribe`
- what shape `fcodex` now has as a thin wrapper
- how `feishu-codexctl` and `fcodex` are split
- what the formal thread-wise `profile/provider` contract is
- how Feishu and `fcodex` should divide `sandbox/approval` settings

If the current implementation diverges from this document, treat that as a
contract gap and tighten the code, the docs, or both.

## 1. Scope And Priority

This document only covers:

- the local `fcodex` / `feishu-codexctl` command-surface split
- the Feishu-side `/release-runtime` plus local `thread unsubscribe` naming and semantics
- the active contract for thread-wise `profile/provider`
- the Feishu-vs-`fcodex` boundary for `sandbox/approval`

For these topics, if this document conflicts with older wording in:

- `docs/contracts/thread-profile-semantics.md`
- `docs/contracts/runtime-control-surface.md`

this document wins.

Those older docs should later be merged forward so the repository does not keep
conflicting active contracts indefinitely.

## 2. Feishu `/release-runtime` And Local `thread unsubscribe`

### 2.1 Naming

The Feishu-side surface uses:

- Feishu command: `/release-runtime`
- local admin CLI: `feishu-codexctl thread unsubscribe`

The previous name was `/release-feishu-runtime`. The active contract is now:

- Feishu public command name: `/release-runtime`
- local CLI and low-level protocol: `thread unsubscribe` / `thread/unsubscribe`

### 2.2 Semantics

`/release-runtime` means:

- target: the thread currently bound by the chat
- actual action: release `feishu-codex`'s Feishu-side runtime residency on that
  thread and call `thread/unsubscribe`
- keep the binding
- if Feishu currently owns the interaction lease, clear that owner
- move every still-`attached` related Feishu binding to `released`

### 2.3 What It Does Not Do

`/release-runtime` does not:

- delete the thread
- archive the thread
- clear the current chat binding
- force-close any `fcodex` TUI
- force the backend to unload immediately

Therefore:

- a successful `/release-runtime` may still leave the thread loaded
- the most common reason is that local `fcodex` is still subscribed

## 3. Active Shape Of `fcodex`

### 3.1 Overall Positioning

`fcodex` stays as close to bare `codex` as practical:

- it is fundamentally a thin wrapper in front of stock `codex`
- it is responsible for shared-backend routing, instance selection, cwd-fixing
  proxying, and a small amount of startup-time thread-wise settings logic
- it no longer carries a broad local admin command surface

In other words:

- `fcodex` should feel as close to `codex` as practical
- repository-specific mental overhead should stay limited to the minimum set
  that the wrapper must own

### 3.2 Command Surface

`fcodex` no longer keeps slash self-commands such as `/help`, `/threads`, `/archive`,
or `/profile`.

It retains only two repository-specific capabilities:

1. wrapper-level enhancement around `resume`
2. thread-wise integration for `-p/--profile`

Everything else is passed through to upstream `codex` whenever possible.

### 3.3 `resume`

`fcodex resume` remains a command surface that this repository owns
explicitly.

Reason:

- it must reuse shared backend / instance routing
- it must support cross-provider thread discovery and exact resume
- it must integrate with the thread-wise `profile/provider` contract

But once the user is inside the running TUI:

- TUI `/resume`
- TUI `/new`
- all other upstream commands

belong to upstream behavior and should not be redefined into a parallel local
product contract.

## 4. Active Shape Of `feishu-codexctl`

### 4.1 Overall Positioning

`feishu-codexctl` is the local discovery / inspection / admin surface.

It is not a second Codex frontend and should not be responsible for entering a
TUI or continuing a live thread interactively.

### 4.2 Responsibility

Capabilities that primarily live in `feishu-codexctl` include:

- thread and binding inspection
- local discovery and diagnosis
- `thread unsubscribe`
- other thread-scoped or binding-scoped admin actions

This means:

- `fcodex` owns attach / resume / entering Codex
- `feishu-codexctl` owns inspection / diagnosis / management

These are different responsibilities and should no longer be blurred into one
entrypoint that both inspects, enters TUI sessions, and manages runtime state.

## 5. Thread-Wise `profile/provider`

### 5.1 Goal

`profile/provider` no longer uses “instance-local default profile” as the
primary product model.

The active model is:

- each thread carries its own thread-wise resume settings
- those settings persist across future resumes
- they are readable by both Feishu and `fcodex`
- they are shared across instances rather than scoped to one instance-local
  default

### 5.2 What The Setting Means

The thread-wise setting represents:

- the expected configuration that should be used the next time this thread is
  resumed from an unloaded state

It is not:

- the authoritative fact about the currently loaded live runtime
- a guarantee that the provider/model is already in effect on a loaded thread

So the correct mental model here is:

- desired resume config

not:

- current runtime config

### 5.3 Storage Scope

Thread-wise `profile/provider` is keyed by `thread_id` and stored in the
machine-global shared layer.

It should not belong to:

- an instance-local default-profile store
- a Feishu binding store
- an `fcodex` process-local state bucket

Reason:

- thread ids already live in a cross-instance shared namespace
- if settings remain instance-local or binding-local, operators will have to
  remember which instance or chat “really” owns a thread's intended resume
  config

### 5.4 Stored Fields

The thread-wise store should at least persist:

- `profile`
- `model`
- `model_provider`
- `updated_at`

Where:

- `profile` is the main user-facing control
- `model` and `model_provider` are the resolved resume arguments derived from
  that profile

The primary write surface is `profile`.

`provider` should not become an independent parallel user-level write knob. It
mainly exists as resolved data and resume inputs.

### 5.5 When Writes Are Allowed

Thread-wise `profile/provider` may only be changed when the thread is
**verifiably globally unloaded**.

More precisely:

- it is not enough for the current instance backend to report `notLoaded`
- the machine-global runtime layer must also show no live runtime owner for the
  thread
- if loaded/unloaded state cannot be verified, the write must be rejected

So the real condition is:

- verifiably globally unloaded

not merely:

- current backend notLoaded

### 5.6 Behavior While Loaded

If the target thread is still loaded, or is otherwise not yet verifiably
globally unloaded, the system still must **not hot-switch** the live runtime.

There are only two allowed paths:

- verifiably globally unloaded:
  - write the thread-wise desired resume config directly
- not yet globally unloaded, but the current instance can safely or forcibly
  reset its backend:
  - offer an explicit “apply this profile, then reset the current instance
    backend” path

That backend reset means:

- reset only the current instance backend / app-server
- do not restart the whole `feishu-codex` service process
- keep binding bookmarks, thread-wise profile data, and other persisted state

In the following cases, direct write is not allowed and the reason must be made
explicit:

- the current instance still has pending approval / input requests
- the current instance still has running Feishu bindings
- the current backend still has active loaded threads
- backend loaded/unloaded facts cannot currently be fully verified
- the thread's live runtime owner belongs to another instance
- the current instance is in remote app-server mode and does not own a backend
  process

The system must not:

- hot-switch the provider on a loaded thread
- silently record a future change to take effect later
- perform a best-effort live rewrite of the current runtime

So Feishu is no longer limited to a pure “reject and tell the user to run
`/release-runtime`” path.
For loaded state that is still under the current instance's control, the formal
path is “explicit backend reset, then write”.
Only when the current instance does not have enough control, or backend reset is
unsupported, should the request be hard-blocked.

### 5.7 Feishu Write Surface

When a Feishu chat is currently bound to a thread:

- `/profile <name>` should target that currently bound thread
- if the thread is verifiably globally unloaded, it writes the thread-wise desired resume
  config
- if the thread is not yet globally unloaded but the current instance backend
  can be reset, it should offer an “apply and reset backend” path
- if only force reset is available, it must surface explicit blocking
  diagnostics and require admin/operator confirmation
- if the live runtime owner belongs to another instance, or the current
  instance is in remote app-server mode, it must block with a clear reason

When a Feishu chat is already bound to a thread and runs `/profile` with no
argument:

- it should show the current thread-wise profile / provider
- it should show re-profile diagnostics
- it should not reset anything immediately
- the later `/profile <name>` or profile-card buttons then enter the
  direct-write or reset path

When a Feishu chat has no bound thread:

- `/profile` should reject directly
- it should tell the user to run `/new` first, or send the first normal prompt to
  create a thread
- it must not silently fall back to the instance's current new-thread default
  profile

That rejection rule applies to:

- `/profile`
- `/profile <name>`
- profile card button actions

### 5.7.1 Feishu `/new` And First-Prompt Seeding

Feishu has two entry points that create a new thread:

- `/new`
- the first normal prompt in an unbound chat

Both must follow the same seed contract:

- thread creation may still use the current instance effective new-thread
  default profile as the creation-time argument
- once the real `thread_id` is known, that default profile must immediately be
  resolved and persisted into the thread-wise store
- that write belongs only to the new thread; it must not be staged in some
  binding-level or instance-level temporary state

Therefore:

- `/new` and “send the first prompt directly” must not create two different new
  thread profile semantics
- if the user later wants to switch that thread's profile, they should still go
  through `/release-runtime` / `/profile <name>` / `resume`

### 5.8 `fcodex resume -p <profile>`

When `fcodex resume <thread>` is given an explicit `-p/--profile`:

- if the target thread is verifiably globally unloaded:
  - resolve the profile to `model/model_provider`
  - write the thread-wise desired resume config
  - then resume the thread using that config
- otherwise:
  - reject directly
  - tell the user to release Feishu subscriptions and close every still-open
    `fcodex` TUI attached to the thread

This contract intentionally forbids hidden “record now, maybe apply later”
behavior for loaded threads.

### 5.9 `fcodex -p <profile>` Starting A New Session

When the user runs:

- `fcodex -p <profile>`

and this launch creates a new thread rather than resuming an existing one:

- that `-p/--profile` acts only as a one-time seed
- it affects only the **first new thread created by this launch**

Once that first new thread has been created successfully and the thread-wise
store has been updated:

- the seed is considered consumed
- it must not keep affecting later upstream `/new` actions inside the same TUI

This document intentionally avoids trying to redefine the entire later TUI
lifecycle under repository-local semantics.

### 5.10 When The Seed Is Written

The `fcodex -p <profile>` seed should not be written into some binding-level or
instance-level temporary state before a thread id exists.

The correct write point is:

- the first `thread/start` succeeds
- the new thread id is known
- then the seed is resolved and persisted into the thread-wise store

This contract intentionally avoids ambiguous “pending placeholder” designs that
guess later which thread they were supposed to belong to.

### 5.11 What If The Write Fails

If:

- `thread/start` already succeeded
- but the thread-wise store write failed

then:

- the new thread itself remains valid
- but the system must explicitly surface that the thread was created while the
  thread-wise setting was not successfully persisted
- it must not silently pretend that the seed was stored

This contract does not require rolling back the already-created thread.

## 6. `sandbox/approval` Boundary

This design round confirms:

- Feishu-side per-binding `sandbox/approval` settings should remain
  binding-scoped and persisted across restarts
- `fcodex` should not introduce an owner-wise cross-frontend shared persistent
  settings plane

`fcodex` should continue to behave as:

- defaulting to `CODEX_HOME/config.toml`
- explicit flags overriding defaults
- no shared persistent settings plane with Feishu

Therefore:

- Feishu-side `sandbox/approval` and `fcodex` defaults are intentionally
  separate
- whichever frontend actually starts a turn is the frontend whose settings are
  carried into that turn

## 7. Questions Intentionally Deferred

This document intentionally does **not** finalize:

- whether a dedicated read-only thread-wise profile inspection command is
  needed

These topics should be tightened separately rather than being filled in by
implicit guesswork.
