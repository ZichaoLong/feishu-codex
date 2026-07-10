# Runtime Settings Fact Sources and Effectivity Boundaries

Chinese original: `docs/contracts/runtime-settings-fact-sources.zh-CN.md`

This document defines the shared rule for answering: after a setting is
written, which layer is the authoritative fact source?

## 1. Only one writable setting family remains

### 1.1 Binding-wise next-turn settings

Current formal members:

- model
- effort
- approval
- permissions

Their properties:

- scoped to the current Feishu binding
- persisted in binding runtime settings
- primarily consumed at `turn/start`
- on unloaded-thread recovery, cold `thread/resume` may carry a narrow one-shot
  subset so the first post-resume autonomous turn does not fall back to stale
  loaded-thread defaults

## 2. The project no longer owns any thread-wise next-load setting

The following surfaces are removed from the project contract:

- legacy project-owned profile commands
- `/memory`
- `focusctl thread memory`
- `new_thread_memory_mode_seed`
- any project-owned thread-level memory/provider/profile restore state

As a result, the project no longer maintains a persisted fact source for
"extra config that this project injects on the next resume of a thread."

## 3. Read-only fact family: live runtime / upstream snapshot

Some values are still read, but they are not project-owned persisted settings:

- live loaded-backend state
- upstream thread snapshot
- runtime views returned by upstream `config/read`

Those values may be shown in:

- `/status`
- diagnostics
- admin cards

But they must not be treated as:

- a writable project setting layer
- the persisted fact source behind a removed legacy profile command

## 4. Writable-setting table

| Setting family | Persisted source | Official application boundary | Primary read-side |
| --- | --- | --- | --- |
| binding-wise next-turn | persisted runtime settings of the current binding | `turn/start`; cold `thread/resume` may carry a narrow one-shot subset for unloaded-thread recovery | `/status`, setting cards, preflight |

## 5. Decision rule for binding-wise next-turn

If the question is:

- "what model / effort / permissions will the next turn from this Feishu chat
  use?"

look first at:

- the persisted runtime settings of the current binding

Within that family:

- `auto` still means "do not explicitly override"
- it no longer maps to any project-owned thread-level persisted state
- adapters must not materialize `auto` into a complete upstream settings object
  carrying stale snapshot values; ordinary auto turns should let the upstream
  thread state continue on its own.
- `model` / `reasoning_effort` and `approval_policy` /
  `permissions_profile_id` have different empty-value semantics:
  - `model` / `reasoning_effort` may remain empty, meaning `auto`
  - `approval_policy` / `permissions_profile_id` are the binding-local safety
    baseline; a new binding is seeded from `codex.yaml`, and once the binding
    is persisted the resolved safety baseline is frozen and does not drift with
    later instance-default changes
- Their turn-dispatch behavior also differs:
  - approval / permissions are sent explicitly on every Feishu turn to reassert the binding's safety baseline
  - model / effort are sent only when non-`auto`; `auto` does not reassert a value and lets the upstream thread's current state continue
- `model` and `reasoning_effort` in `codex.yaml` only seed a new binding's
  initial runtime state; once a binding exists, ordinary `thread/start` and
  `turn/start` calls read binding runtime settings only and do not fall back to
  adapter config.
- `model_provider` is not a binding runtime setting; `/new`, first-prompt
  thread creation, and ordinary turns do not inject it from adapter config. It
  is not accepted in `codex.yaml`; configure providers in upstream Codex, or
  send one only when a caller explicitly provides a provider hint.
- collaboration mode is not a Feishu runtime setting. If needed, configure it
  in upstream Codex; this project does not construct or send upstream
  `collaborationMode` payloads.

### 5.1 Model / effort pair validation

Focus uses only `supportedReasoningEfforts` from app-server
`model/list` as validation metadata for an explicit model. It does not try
to reconstruct an "effective model / effort" mirror from the current thread.

- effort is `auto`: `validated`
- model is `auto` and effort is explicit: `deferred`
- the explicit model has no usable metadata and effort is explicit: `deferred`
- explicit-model metadata advertises the effort: `validated`
- explicit-model metadata does not advertise the effort: `rejected`

The control surface refuses newly requested `rejected` pairs, but it does
not migrate or repair existing binding data and does not add a second admission
check before prompt dispatch. Existing values are still sent to app-server as
their stored strings, and upstream owns the final execution result.

Upstream `turn/start` semantics apply explicit model / effort values to the
current and subsequent turns of the shared thread. The Feishu binding and local
TUI therefore do not share project-persisted settings, but they can still
observe or overwrite the most recently sent explicit values through the same
upstream thread.

## 6. Empty Values In The Binding Store

`chat_bindings.json` is a persisted projection, not the runtime semantic fact
source. Runtime-setting values, safety baselines, and explicit configuration
intent are separate facts. The store layer is responsible only for:

- saving and reading string fields plus the `configured_settings` list
- validating structure and non-empty enum values
- accepting legacy field names, such as legacy `sandbox` as the
  `permissions_profile_id` field

The store layer must not apply instance-default fallbacks. Empty strings must be
preserved until `BindingRuntimeManager` hydrates the record and interprets them
with the current instance config:

- empty `approval_policy` / `permissions_profile_id` only means an old record
  or a not-yet-materialized store shape; hydrate resolves it to the current
  instance default, and the next persistence of that binding writes the resolved
  safety baseline
- legacy `collaboration_mode` fields are ignored on read and are not written by
  new saves
- empty `model` / `reasoning_effort` -> `auto`, meaning no explicit override

`configured_settings` is the binding-local source of truth for explicit user
actions, but it is not the source of truth for whether `approval_policy` /
`permissions_profile_id` have a safety baseline. It is set only by explicit
`/model`, `/effort`, `/approval`, or `/permissions` interactions, not by
`codex.yaml` seeds. A value that equals the instance default still remains
configured when its setting name appears in this list.

Consequently:

- for `model` / `reasoning_effort`, `configured_settings` distinguishes "the
  user explicitly selected auto" from "never configured"
- for `approval_policy` / `permissions_profile_id`, the persisted binding value
  itself is the current binding's safety baseline; `configured_settings` only
  records whether the user explicitly changed it
- for old records without `configured_settings`, the store conservatively
  infers intent from non-empty normalized setting values; historical empty
  `auto` intent cannot be recovered

An unbound binding with persisted settings is a valid state: it has no
`thread_id`, but it carries the user's next-turn configuration decision or a
binding-local safety baseline. Concretely, `configured/unbound` means there is
no thread bookmark and the persisted binding still has `configured_settings`, a
safety baseline, or another binding-local fact that must be retained. Admin
surfaces may display it as `configured/unbound`; it is not a stale thread
binding and must not be removed by `binding clear-stale`.

## 7. One maintenance rule

If a new setting is added later, it must first be classified as exactly one of:

1. binding-wise next-turn settings
2. read-only diagnostic view

Until that classification exists, the setting must not become a new command
surface or persisted project state layer.
