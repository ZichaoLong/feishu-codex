# Draft: Admin and User Workflows in Multi-Instance Mode

> Status: superseded
>
> The current formal contracts no longer use the old named-instance admission
> design. All instances now share one persisted thread namespace; instance
> boundaries primarily apply to binding, local runtime state, and
> `ThreadRuntimeLease` coordination.
> See `docs/contracts/runtime-control-surface.md` §6.8 and
> `docs/contracts/session-profile-semantics.md` §5.

> Status: a first implementation pass has been completed along the direction of
> this document; it remains a draft for administrator / end-user workflows.
>
> Note: this document describes the target usage model. It does not imply that
> current code already supports every detail. After design confirmation and
> implementation, stable parts should be pushed down into formal contracts and
> the README.

## 1. Roles

This document distinguishes only two roles:

- **Administrator / local operator**
  - installs, maintains, and runs `feishu-codex` on the machine
  - creates and manages multiple Feishu instances
  - decides whether a given enterprise instance is allowed to use a given thread
- **Ordinary Feishu user**
  - only interacts with the bot inside their own enterprise / group / p2p chat
  - does not touch local service-management details directly

One important premise:

- the real local operator across multiple enterprises is usually the same person
- so sharing local `CODEX_HOME` is natural
- but Feishu runtime state and permission surfaces still remain isolated per instance

## 2. Admin Mental Model

An admin should not think in terms of "multiple fully separate local Codex
installations", but rather:

- I have one shared local Codex user space
- I operate multiple Feishu instances
- each instance has its own:
  - app credentials
  - service
  - control plane
  - backend
  - binding / group / ACL state
- all of those instances may see the same persisted thread namespace
- but only one instance backend may live-attach a given thread at a time

## 3. Target Admin Workflow

### 3.1 Create Instances

The admin creates one instance per enterprise.

Typical target shape:

- `corp-a`
- `corp-b`

Each instance maintains its own:

- `system.yaml`
- `codex.yaml`
- `init.token`
- instance-local runtime state under `FC_DATA_DIR`

Shared:

- `CODEX_HOME`

### 3.2 Start and Stop Instances

The admin starts services per instance.

Example target command surface:

```bash
feishu-codex --instance corp-a start
feishu-codex --instance corp-b start
feishu-codex --instance corp-a status
feishu-codex --instance corp-b log
```

The admin should understand:

- `corp-a` and `corp-b` are two independent Feishu services
- each owns its own backend
- stopping one instance affects only that instance
- the `default` instance still keeps the legacy single-instance behavior;
  named instances are the ones that tighten visibility through explicit admission

### 3.3 Manage Per-Instance Runtime State

The admin uses `feishu-codexctl` to manage a specific instance.

Example target command surface:

```bash
feishu-codexctl --instance corp-a service status
feishu-codexctl --instance corp-a binding list
feishu-codexctl --instance corp-a thread status --thread-id <id>
```

The admin should understand:

- the object managed by `feishu-codexctl` is a specific running Feishu service
- it is not a global, instance-free omniscient thread console

### 3.4 Make a Shared Thread Visible / Usable to an Instance

The most important point comes first:

- sharing `CODEX_HOME` does **not** mean every instance automatically exposes every thread

Recommended target workflow:

1. the thread exists in shared `CODEX_HOME`
2. the admin explicitly imports it into a given instance
3. only imported instances treat that thread as visible / resumable on the
   Feishu side

Suggested local management commands:

```bash
feishu-codexctl --instance corp-b thread import --thread-id <thread_id>
feishu-codexctl --instance corp-b thread revoke --thread-id <thread_id>
```

Semantics:

- `import`
  - moves a shared thread into that instance's admitted range
  - does not mean immediate live runtime ownership
  - does not mean immediate load into that instance backend
- `revoke`
  - makes the thread no longer visible by default to that instance's Feishu surface
  - should block or demand cleanup first if there is still a binding or running turn

This is the core admin action for explicit cross-enterprise sharing.

Current implementation choice:

- the `default` instance still treats persisted threads in shared `CODEX_HOME`
  as visible by default
- only named instances require explicit admin-side `import`
- that keeps the old single-instance path mostly unchanged, while tightening
  visibility for additional enterprise instances

### 3.5 Principles for Cross-Enterprise Thread Reuse

An admin may decide whether different enterprise instances can use the same
persisted thread.

But these principles should hold:

- this must be an explicit choice, not accidental implicit reuse
- even if multiple instances can see the same thread, they may not write it simultaneously
- if instance A currently live-attaches a thread, instance B may only:
  - observe
  - wait
  - explicitly take over later if such a design is added
  - or be rejected and stop writing

In plainer terms:

- **seeing the same thread is acceptable**
- **materializing it as two live backend runtimes is not**

### 3.6 How Live Runtime Lease Should Flow

The recommended workflow should feel natural, but stay fail-closed.

#### Case A: no instance currently holds live runtime

- once the thread has been imported into instance B
- the next prompt on instance B may obtain the runtime lease normally
- instance B then resumes / attaches the thread in its own backend and starts the turn

User-visible effect:

- "I can just send the message and it continues"

#### Case B: instance A currently holds live runtime, but is idle

- the thread has already been imported into instance B
- a new prompt arrives on instance B
- the system detects:
  - A is the current owner instance
  - but A has no running turn
  - and no pending approval / pending input
- then **automatic transfer** is allowed:
  - B asks A to release runtime
  - once A releases successfully
  - B acquires the runtime lease
  - B resumes in its own backend and starts the turn

The user experience can still feel like:

- "if the other side is idle already, I can just send the message and take over"

#### Case C: instance A currently holds live runtime and is still running or waiting for interaction

- the prompt on instance B must be pure reject
- no queueing
- no silent stealing
- no guessing that takeover might be fine

The message to users / admins should be direct:

- which instance is the current owner
- whether it is "running" or "waiting for approval / input"
- that writing is not allowed right now, and they should try again later

#### Recommended initial cut

To keep complexity under control, the first version should support:

- `import`
- `revoke`
- automatic transfer when the owner is idle
- pure reject when the owner is active / pending

It should not rush into:

- forced admin takeover
- cross-instance queueing
- non-owner live follow / mirrored UI

That keeps both admin and end-user mental models simple:

- a thread must first be imported into an instance
- idle threads may flow naturally
- busy threads reject explicitly

### 3.7 What Admins Should Tell Ordinary Users

Admins can give users a very simple explanation:

- just use the bot normally inside your own enterprise / group
- if a thread is currently being executed elsewhere, the system may say it is
  temporarily not writable or that you should wait
- if the same live thread must be continued locally, let the admin / local
  operator use `fcodex`
- do not treat bare `codex` as the default safe entrypoint for live-thread sharing with Feishu

## 4. Target Usage Model for Ordinary Feishu Users

The ordinary user's mental model should stay simple:

- I only interact with the bot inside my current enterprise
- I can only reach threads already imported / exposed to this instance
- whether this bot may write the current thread depends on ACL / mode / owner
  state for the current chat
- if the system says the thread is busy, occupied, or not writable from this
  instance, I should not keep triggering concurrent requests

Ordinary users do **not** need to understand:

- `CODEX_HOME`
- app-server backend
- control plane
- runtime lease

## 5. How the Local Operator Should Use `fcodex`

### 5.1 Default Principle

`fcodex` is the local entrypoint for safely sharing a live thread.

The formal recommendation does not change:

- if local and Feishu need to continue the same live thread, use `fcodex`
- do not treat bare `codex` with its isolated backend as the default path for
  sharing a live thread

### 5.2 Target Experience in Multi-Instance Mode

The target `fcodex` experience in multi-instance mode is:

- auto-pick the correct instance in common cases
- require explicit instance choice in complex or ambiguous cases

Example target command surface:

```bash
fcodex
fcodex resume <thread_id>
fcodex resume <thread_name>
fcodex --instance corp-a
fcodex --instance corp-b resume <thread_id>
```

One boundary to keep explicit:

- `fcodex` may keep stronger global discovery than any single Feishu instance
- but once it actually connects to a backend to write, it must still obey
  instance routing and the global runtime lease

### 5.3 User Mental Model for Automatic Routing

The local operator should be able to think:

- if the system already knows which instance currently live-attaches the thread,
  `fcodex` connects there directly
- if there is only one running instance, `fcodex` uses it directly
- if there are multiple running instances and the correct choice is unclear,
  `fcodex` asks me to specify the instance explicitly

Users should **not** expect:

- `fcodex` to guess the instance under ambiguity
- `fcodex` to automatically absorb bare `codex` isolated backends into the
  shared owner model

## 6. Recommended Role of Bare `codex`

Bare `codex` remains valid, but its role must be stated clearly:

- it is the upstream native command surface
- it can create and resume persisted threads
- because `CODEX_HOME` is shared, those threads can later be discovered by `fcodex`
- but the isolated backend started by bare `codex` is outside the
  `feishu-codex` safe live-thread-sharing contract

So the recommendation is:

- treat bare `codex` as the entrypoint for independent local Codex usage
- treat `fcodex` as the entrypoint when a live thread must be shared safely with Feishu

## 7. Default Habits Recommended for Admins

The recommended default habits are:

1. create one instance per enterprise
2. configure each instance with its own `system.yaml`
3. use `feishu-codex --instance ...` for day-to-day service management
4. use `feishu-codexctl --instance ...` for day-to-day local thread management
5. use `fcodex` whenever a live thread must be continued together with Feishu
6. do not treat concurrent writes through bare `codex` as a supported path
7. if a thread must be reused across enterprises, let the admin decide it
   explicitly rather than letting ordinary users drift into it accidentally

## 8. Short End-User Explanation

The final user-facing explanation can be compressed into a few lines:

- use the bot normally inside the current enterprise / group
- if the system says a thread is running, waiting for approval, or not writable
  right now, wait or contact the admin
- do not assume bots in different groups / enterprises automatically share a
  writable context
- if the same live thread must be continued locally, let the admin / local
  operator use `fcodex`

## 9. Decisions Still Worth Confirming

Before implementation, these usage-side choices are worth confirming explicitly:

1. whether `fcodex` may hide multi-instance details by default and expose
   `--instance` only on ambiguity
2. whether `feishu-codexctl` should remain instance-scoped rather than turning
   into an instance-free global thread console
3. whether reusing one thread across enterprises should remain an explicit
   admin decision rather than a default path for ordinary users
4. whether "bare `codex` isolated backend concurrently writes the same thread"
   remains a documentation / user-education boundary
5. whether ordinary users should see an explicit reject / wait message in
   cross-instance contention cases, instead of hidden queueing or guessed takeover
