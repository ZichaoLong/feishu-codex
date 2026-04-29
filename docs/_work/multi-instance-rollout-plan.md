# Draft: Multi-Instance Support Rollout Plan

> Status: superseded
>
> The current formal contracts no longer use the old named-instance admission
> design. All instances now share one persisted thread namespace; instance
> boundaries primarily apply to binding, local runtime state, and
> `ThreadRuntimeLease` coordination.
> See `docs/contracts/runtime-control-surface.md` §6.8 and
> `docs/contracts/session-profile-semantics.md` §5.

> Status: a first implementation pass has been completed along the direction of
> this document, but the resulting contracts still need to be tightened in the
> formal docs.
>
> Note: this document remains in `docs/_work/` as a pre-formal implementation
> plan. After design confirmation and landing, the relevant conclusions should
> be pushed down into `docs/architecture/`, `docs/contracts/`,
> `docs/decisions/`, and `README.md`.

## 1. Background and Goals

`feishu-codex` currently defaults to one Feishu app / one instance.
The new practical requirement is:

- the same local operator on one machine
- manages multiple Feishu enterprises / multiple bot apps at the same time
- without being forced to split them into multiple fully separate local Codex
  user spaces
- while still wanting:
  - clear architecture
  - easy maintenance
  - unambiguous behavior
  - a default path that remains convenient

The core conclusion of this design pass is:

- **`CODEX_HOME` is shared by default**: it represents the local operator's own
  Codex user space
- **Feishu instance runtime is isolated per instance**: every instance owns its
  own config, data, service owner, control plane, and app-server backend
- **one thread must never be written by multiple backends at the same time**:
  the persisted thread namespace may be shared, but the live backend namespace is not

## 2. Design Conclusions

### 2.1 What Is Shared

The following are shared at the "local operator" layer:

- `CODEX_HOME`
- upstream `config.toml`
- upstream auth / history / sessions / skills / model cache
- persisted thread metadata and rollout artifacts produced by bare `codex`

Why these are shared:

- the real local operator is usually one person
- thread discovery should interoperate between bare `codex` and `fcodex`
- multi-enterprise support should not force duplicate profile / history /
  skills / auth spaces
- these states describe how the local user uses Codex, not which Feishu
  enterprise is currently running

### 2.2 What Is Isolated

The following are isolated per Feishu instance:

- `FC_CONFIG_DIR`
- `FC_DATA_DIR`
- `system.yaml`
- `init.token`
- `codex.yaml`
- chat binding store
- group chat store
- profile state store
  (instance-scoped new-thread seed profile state, not the machine-global
  thread-wise resume profile)
- service instance lease
- control plane socket
- managed app-server runtime discovery
- Feishu runtime attached / released snapshot
- the app-server backend process itself

Why these are isolated:

- they describe how a specific Feishu bot instance runs
- they directly affect owner state, bindings, ACLs, group context, and admin control
- if they remain shared, the system collapses into "multiple bots in one state
  space", which is too risky and too hard to reason about

### 2.3 Instance Visibility Scope (Admission)

Sharing `CODEX_HOME` means only that:

- multiple instances can see the same persisted thread namespace at the machine level

It does **not** mean that:

- a thread is automatically exposed to every Feishu instance
- ordinary users in any enterprise can `/resume` any shared thread by default

So this design introduces **instance admission**:

- each instance maintains its own admitted-thread set
- a thread becomes visible to that instance's Feishu surface only after an
  admin explicitly imports it
- Feishu `/session`, Feishu `/resume`, and instance-local chat continuation
  only target:
  - threads admitted into the current instance
  - threads already bound inside the current instance
- `fcodex`, as the local Codex entrypoint, may still keep stronger global discovery

Current implementation choice:

- **the `default` instance keeps the old single-instance globally visible behavior**
- **only named instances (for example `corp-a`, `corp-b`) require explicit admission**

Why:

- the default single-instance path stays convenient
- only newly added enterprise instances are tightened by default
- the admin mental model becomes:
  "the default instance works like before; extra enterprise instances are tightened by admission"

The reason for this split:

- shared `CODEX_HOME` solves "one local operator uses Codex as one user space"
- admission solves "which Feishu instance is allowed to expose / use which thread"
- those are different boundaries

Recommendation:

- add a per-instance store for admitted threads
- it should live under that instance's `FC_DATA_DIR`, not under shared `CODEX_HOME`

### 2.4 Backend Conclusion

In this multi-instance design:

- **different instances do not share one live app-server backend**
- **each instance manages its own backend**
- **`fcodex` connects to one instance backend, not to a system-global backend**

Two meanings of "shared" must stay separate:

1. **shared `CODEX_HOME`**
   - shared persisted thread namespace and local user space
2. **shared app-server backend**
   - shared live thread memory state, subscriptions, interaction requests, and turn lifecycle

This plan accepts only the first kind of sharing across instances, not the second.

### 2.5 Safety Rule

All instances share one core rule:

- **at any moment, one thread may be written through only one backend**

This is a direct extension of the current shared-backend safety model:

- within one instance: Feishu and `fcodex` may safely share a live thread
  through that instance backend
- across instances: two instance backends may not live-attach the same thread
  at the same time
- bare `codex` writing the same thread through its own isolated backend
  remains outside the supported safety path

## 3. Target Runtime Model

### 3.1 One Runtime Space Per Instance

Target model:

- `instance A`
  - `FC_CONFIG_DIR_A`
  - `FC_DATA_DIR_A`
  - `service owner A`
  - `control plane A`
  - `app-server backend A`
- `instance B`
  - `FC_CONFIG_DIR_B`
  - `FC_DATA_DIR_B`
  - `service owner B`
  - `control plane B`
  - `app-server backend B`
- all instances share the same `CODEX_HOME`

### 3.2 Shared Within One Instance, Isolated Across Instances

In this design, "shared backend" should now mean:

- **shared backend inside one instance**
- not "one backend shared by all instances in the whole system"

Therefore:

- all Feishu chats inside one instance share that instance backend
- `fcodex` connected to that instance shares that instance backend
- different instances do not share live backend state

## 4. New Infrastructure

If `CODEX_HOME` is shared, multiple instances will see the same persisted
threads. So a cross-instance coordination layer becomes mandatory; otherwise
"shared visibility" turns into "live ownership conflict".

### 4.1 Global Instance Registry

Add a machine-global instance registry.

Recommended responsibilities:

- record which instances are currently running
- record, for each instance:
  - `instance_name`
  - `FC_CONFIG_DIR`
  - `FC_DATA_DIR`
  - control endpoint
  - backend URL
  - owner pid / started_at
- provide one discovery surface for `feishu-codexctl` and `fcodex`:
  "find instances first, then decide who to connect to"

Boundary:

- it describes only the instance list and connection entrypoints
- it does not directly own thread-owner logic

### 4.2 Global Thread Runtime Lease

Add a machine-global thread runtime lease layer.

Recommended responsibilities:

- record whether a given `thread_id` is currently live-attached by some
  instance backend
- record which instance owns that attachment
- prevent two instances from materializing the same persisted thread as two live runtimes
- provide a single fact source for the cross-instance workflow:
  "automatic transfer when idle, explicit reject when active"

Suggested fields:

- `thread_id`
- `owner_instance`
- `owner_service_token`
- `backend_url`
- `attached_at`
- `lease_state` such as `attached` / `released` / `stale`

Boundary:

- it only owns "which instance currently holds live backend residency for this thread"
- it does not own Feishu chat bindings, group ACLs, approval owner, or interaction owner
- interaction owner remains an instance-local runtime fact, not a new global concept

Recommended transfer rule:

- if a thread is not currently live-attached by any instance:
  - the first admitted instance that hits it may acquire the runtime lease and start the turn
- if a thread is live-attached by instance A, but A has no running turn and no
  pending approval / supplemental input:
  - the next prompt on instance B may trigger **automatic transfer**
  - concretely: B asks A to release runtime, then B acquires the lease and takes over the backend
- if a thread still has a running turn or pending interaction on instance A:
  - prompts on instance B must pure reject
  - the initial version should not do hidden queueing or silent stealing
  - if truly needed later, explicit admin takeover can be designed separately

## 5. Target Command-Surface Shape

## 5.1 `feishu-codex`

`feishu-codex` is the service-management entrypoint at the instance layer.

Target shape:

- `feishu-codex --instance <name> start|stop|restart|status|log|run|config`
- optionally also:
  - `feishu-codex instance list`
  - `feishu-codex instance create <name>`
  - `feishu-codex instance remove <name>`

Recommendation:

- switch `systemd --user` to a template service such as
  `feishu-codex@<instance>.service`
- outer command auto-resolution may remain, but the service-management surface
  is fundamentally still instance-scoped

## 5.2 `feishu-codexctl`

`feishu-codexctl` should remain an instance-scoped management surface, not
degrade into a global "god console".

Why:

- it manages one running Feishu service
- binding / thread release / runtime status are all attached to one instance backend
- even if auto-inference is allowed, the underlying contract should still point
  to one specific instance

Target shape:

- `feishu-codexctl --instance <name> service status`
- `feishu-codexctl --instance <name> binding list`
- `feishu-codexctl --instance <name> thread status --thread-id ...`

## 5.3 `fcodex`

`fcodex` keeps its role as the Codex usage entrypoint, but should support
multi-instance automatic routing.

Target principles:

- common paths should not force explicit `--instance`
- `--instance` remains available architecturally as the disambiguation parameter
- ambiguity should fail closed, not guess

Suggested target shape:

- `fcodex [--instance <name>]`
- `fcodex [--instance <name>] <prompt>`
- `fcodex [--instance <name>] resume <thread_id>`
- `fcodex [--instance <name>] resume <thread_id|thread_name>`

Suggested automatic routing order:

1. if `--instance` is explicit, use it directly
2. if the target thread is currently live-attached by exactly one instance,
   route to that instance automatically
3. if there is only one running instance, use that instance automatically
4. if local config declares a default instance, use it
5. otherwise, raise an ambiguity error and require explicit `--instance`

## 6. Implementation Phases

### Phase 1: Instance Layout and Service Templates

Goal: establish the instance runtime boundary first.

Work items:

- define instance directory layout
- add instance-name validation and layout resolver
- route `FC_CONFIG_DIR` / `FC_DATA_DIR` / systemd service name through one resolver
- switch install scripts to install template services
- reserve a per-instance thread-admission store
- keep `CODEX_HOME` shared; do not split the home in this phase

Phase goal:

- multiple instances can start and stop independently
- per-instance data does not contaminate other instances
- `fcodex` does not yet need full automatic routing

### Phase 2: Instance Admission + Global Instance Registry

Goal: tighten "which instance may expose which thread" first, then let local
commands discover "which instances are currently running".

Work items:

- add a per-instance thread-admission store
- define visibility rules for Feishu `/session` and Feishu `/resume`
- add local admin entrypoints for import / revoke of per-instance admission
- add the registry store
- register / unregister on service start / stop
- let `feishu-codexctl` enumerate instances or connect to the right control plane
- let `fcodex` discover candidate instances through the registry

Phase goal:

- shared `CODEX_HOME` no longer means "all threads visible to all instances by default"
- local commands no longer depend only on one `FC_DATA_DIR`
- the system can clearly tell which `feishu-codex` instances are running

### Phase 3: Global Thread Runtime Lease

Goal: prevent two instance backends from writing the same thread.

Work items:

- add the thread runtime lease store
- explicitly write lease state on thread attach / resume / release
- clean up or recover lease state on service stop, crash, and instance restart
- make ambiguous paths fail closed

Phase goal:

- at one moment, one thread is live-attached by at most one instance backend
- even when `CODEX_HOME` is shared, live runtime still has one owner

### Phase 4: `fcodex` Auto-Routing and Instance Disambiguation

Goal: make local UX feel natural while keeping the contract explicit.

Work items:

- add `--instance` support to `fcodex`
- implement the automatic routing order
- surface "discoverable but not live-safe by contract" semantics for threads
  produced by bare `codex`
- under ambiguity, error out directly rather than guessing

Phase goal:

- common paths do not require frequent `--instance`
- complex paths still have an explicit, controllable selector

### Phase 5: Docs, README, and Regression Coverage

Goal: convert "implementable" into "maintainable formal contract".

Work items:

- push confirmed conclusions into:
  - `docs/architecture/`
  - `docs/contracts/`
  - `docs/decisions/`
  - `README.md`
- add instance-related tests:
  - same-instance double-start fail-fast
  - different instances running in parallel
  - global registry registration / recovery
  - thread runtime lease takeover / release
  - `fcodex` auto-routing / ambiguity errors

## 7. Explicitly Accepted Limits

This plan should accept these limits explicitly instead of trying to blur them:

- bare `codex` writing the same thread through an isolated backend at the same
  time as Feishu is outside the supported safety path
- `fcodex` may discover threads created by bare `codex`, but cannot absorb the
  live backend of bare `codex` into the current owner model automatically
- one thread may be visible to multiple instances, but may not be live-attached
  by multiple instance backends at the same time
- sharing one thread across enterprises is an explicit admin / operator choice;
  it should not happen silently just because `CODEX_HOME` is shared
- even if multiple enterprise instances can see one thread, whether a given
  chat may keep writing still depends on that instance's ACL / mode / owner rules

## 8. Recommended Review Questions

Before implementation starts, these questions are worth confirming:

1. whether "shared `CODEX_HOME`, isolated backend per instance" is acceptable
   as the formal direction
2. whether `fcodex` may keep `--instance`, while still auto-routing by default
3. whether `feishu-codexctl` should remain fundamentally instance-scoped
4. whether a global `instance registry` and `thread runtime lease` must be added
5. whether "bare `codex` isolated backend concurrently writes the same thread"
   should remain a documentation / user-education boundary instead of a fully
   sealed technical guarantee
