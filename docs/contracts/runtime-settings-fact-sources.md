# Runtime Settings Fact Sources and Effectivity Boundaries

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/runtime-settings-fact-sources.zh-CN.md`.

This document defines the shared rule for answering Feishu and Web settings
questions: after a setting is written, which layer is the authoritative fact
source? It does not merge the two frontends' persisted facts or deny Focus
Web's separate navigation/document state.

## 1. Two formal writable setting families which do not merge

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

This states only when the setting may be carried. It does not make resume a
passive observer operation. A resume that may autonomously start a main turn
must first acquire the exact blank submission lease defined by
`root-operation-owner.md`. Only an authoritative preflight proving Goals are
disabled, no goal exists, or the goal is in a reviewed non-continuing state may
perform passive observation resume without a main-turn lease. There is no
retained record, delivery fence, or cross-call writer here.

### 1.2 Instance-wide Web next-turn settings

Web has a separate writable `WebNextTurnSettings` with the same four members:
model, reasoning effort, approval policy, and permissions profile. It belongs
to no browser `client_id`, document, selected thread, or main-turn writer. Every
browser, post-F5 document, and thread in one Focus instance shares the same
settings. They do not automatically merge with a Feishu binding or local
`focus` / `fcodex` TUI state.

`WebNextTurnSettingsStore` owns the sole durable
`web_next_turn_settings.json` record. Only when no durable record exists at
service start are the four validated `codex.yaml` values captured as a
non-materialized startup seed. Later reads and turn dispatches do not reread
configuration or maintain a config mirror. The first explicit Web mutation
creates the record, and that persisted record wins on every later restart.

Mutations perform an atomic partial merge under the owner lock. Every actual
mutation produces a strictly increasing positive `generation`. Concurrent
browsers may merge different fields; the same field is last-write-wins by
server commit order. There is no expected-generation CAS, conflict UI, history,
or rollback. `generation` is comparable only within one `runtime_epoch`. In the
same epoch, a browser admits only complete snapshots with `generation >=
current`: it ignores a lower value, installs a higher value, and treats an equal
value as unchanged only when the complete content is identical. Different
content at the same generation fails closed and triggers an authoritative
refresh. On an epoch change it discards the old generation comparison base,
unconditionally installs the new epoch's complete snapshot through an
authoritative reload, and only then resumes same-epoch comparison. This allows
a new service start without a durable record to replace the old runtime's
snapshot with a different startup seed even though generation begins at 1
again.

Each eligible Web consumer captures exactly one immutable snapshot:

- new-thread creation and its first turn share one snapshot;
- an existing-thread ordinary prompt;
- a continuation-capable cold active-goal resume which requires writer
  admission.

Regular active-turn steer, observer resume, review, manual compact, F5, and auth
refresh do not consume new settings. Controls remain editable during an active
turn. A mutation explicitly affects only the next eligible Web turn and cannot
claim to rewrite the current steer or active turn.

This bounded Web ordering is not a backend-wide guarantee. Ordinary cold
reattach, loaded `/goal resume`, `/goal set`, and automatic continuation may
first use runtime configuration already held by the backend process. Even a
safe cold resume may carry overrides only after an explicit pause boundary.
Focus adds no universal barrier, config-reload watcher, polling, replay,
quarantine, or durable synchronization to close those narrow windows. To
change backend fallback, users follow upstream configuration and restart the
relevant backend.

Web navigation is a separate fact family. `WebWriterProfileStore` persists only
the selected thread, draft working directory, and attachment
`scope_generation`; `selected_thread_id` is the sole durable semantic Web
selection. `WebDocumentRegistry.materialized_thread_id` is only process-local
bounded-history readiness, so older-history admission requires both values to
equal the target. Desired subscription edges and outcomes belong only to
`WebRuntimeInterestRegistry` and cannot be inferred from selection or
materialization. Navigation/selection generation, blank-submission admission,
active-main-turn admission, and browser identity neither order, settle, nor
derive `WebNextTurnSettings`.

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

### 3.1 Connection-local effective-settings facts

Focus keeps disposable, non-persisted upstream setting facts in one
`ThreadEffectiveSettingsRegistry` shared by Web and Feishu. It accepts only a
successful `thread/start` / `thread/resume` response or a complete
`thread/settings/updated.threadSettings`. Requests, a `turn/start` response turn
identity by itself, and the queued-operation ACK from `thread/settings/update` are not setting
facts. `WebNextTurnSettings` and Feishu binding overrides remain future-turn
intent.

The registry has three layers:

1. `thread_base` is the most recent authoritative response or complete settings
   notification;
2. `turn/started` freezes every then-current base field into a matching
   active-turn snapshot; a later response or settings notification replaces
   only the base and never backfills or mutates that active turn;
3. `model/rerouted` overrides only the model of the current active turn when
   both `threadId` and `turnId` match exactly.

Each of the four fields preserves unknown separately from known null and a
concrete value. The pinned upstream implementation always serializes `model`
and its non-skipped `reasoningEffort` option, so Focus parses both strictly;
`reasoningEffort=null` is known auto. When an
experimental field was not negotiated, an absent `approvalPolicy` or an
absent/null `activePermissionProfile` makes only that field unknown rather
than failing the whole thread lifecycle. The existing response postcondition
still requires exact approval and permissions matches when a
continuation-capable cold resume requests them explicitly. A complete settings
notification supplies `model`, `effort`, `approvalPolicy`, and
`activePermissionProfile`. A matching malformed or
incomplete notification atomically replaces the new base with all-unknown
instead of allowing the stale base to masquerade as current.
A matching current-turn reroute without a valid `toModel` makes only the active
model unknown and removes any older reroute; the other three frozen fields are
unchanged. A reroute without a usable turn identity also retires the current
active model because it cannot be proved stale. A malformed reroute carrying a
usable, proven-stale turn id remains a no-op.

Before a continuation-capable `thread/resume`, Focus applies the same
field-wise base-plus-active invalidation to explicitly carried
model/effort/approval/permissions; a response-unknown path may not let a later
`turn/started` freeze stale values. Before `turn/start`, Focus invalidates only explicitly sent fields that differ
from current evidence, across both base and active snapshot. Before
`thread/settings/update`, the same field-wise comparison invalidates only the
future-turn base. Equal values remain because upstream may suppress a complete
no-op notification. Invalidation precedes the send and an ACK never installs
request values, so a later ACK cannot erase an earlier authoritative event.

There is one production notification writer: `FocusRuntime` updates the
registry at the RuntimeLoop adapter-ingress boundary before fan-out. A matching
completion or non-active status clears active snapshot/reroute. Unload,
archive, close, delete, unsubscribe, backend disconnect, and confirmed reset
clear the corresponding positive base/active/reroute facts or all connection
facts. Per-thread external-unknown markers are excluded from lifecycle cleanup
and survive until backend disconnect/reset. Stale reroutes and stale
completions are no-ops for the current turn. A recognized start, completion,
or status notification without a usable lifecycle identity retires the old
active snapshot instead of preserving stale active evidence.

The fcodex proxy websocket is not a canonical fact ingress for this registry:
its requests, ACKs, successful responses, and connection-local notifications
never install setting facts. After exact service admission and before a
reviewed turn, settings, resume, continuation-risk goal, or thread-lifecycle
effect can be sent, the
registry marks that thread external-unknown and retires all four fields.
Canonical notifications and later start/resume responses do not clear the
marker because the two app-server connections expose no common revision or
causal ordering token; either message can have crossed the external send in an
unobservable order. The exact thread therefore remains unknown until backend
connection invalidation or confirmed reset clears the disposable epoch. This
is intentionally a narrow disclosure/native-media degradation, not a second
writer or service quarantine. A targetless `thread/start` has no prior thread
to retire.

Native-media admission reads only this owner's model, resolving active facts
in exact-reroute, frozen-snapshot, then thread-base order and failing closed on
unknown. For that model, a fresh `model/list` entry must explicitly include
`image` in `inputModalities`; absent catalog entries, `inputModalities=null`,
and catalog-read failures remain `unknown`, while an explicit list without
`image` is text-only. Only explicit image support plus a verified image byte
signature permits `localImage`; every other case keeps the controlled
same-host path text and omits native media.

Even an authorized `localImage` remains a pathname handoff, not an immutable
byte snapshot. Focus performs a final no-follow file-identity and signature
check, but the interval before app-server opens that path is a residual local
TOCTOU boundary. Fully closing it requires an immutable/content-addressed or
descriptor/upload protocol, as detailed in
`docs/decisions/feishu-attachment-ingress.md`.

This registry is not a new writable settings family and does not claim to
reconstruct all upstream runtime configuration. It supplies the same read-only
upstream evidence to native-media admission and Web active-turn disclosure.
The fixed upstream evidence is
[`ThreadStartResponse` / `ThreadSettingsUpdatedNotification`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L170-L305)
,
[`ThreadResumeResponse`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L401-L430),
and
[`ThreadSettingsApplied` notification emission](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/bespoke_event_handling.rs#L1190-L1210).

## 4. Settings and read-only fact table

| Setting family | Persisted source | Official application boundary | Primary read-side |
| --- | --- | --- | --- |
| binding-wise next-turn | persisted runtime settings of the current binding | Feishu ordinary prompts carry them through official `turn/start`. They normally apply to a newly started turn; in the narrow upstream start-or-steer race they can instead apply to an already active regular turn. Cold `thread/resume` may carry a narrow one-shot subset for unloaded-thread recovery | `/status`, setting cards, preflight |
| instance-wide Web next-turn | service-start-captured validated `codex.yaml` seed while no durable record exists; one `web_next_turn_settings.json` record after the first explicit mutation | each eligible Web new-thread create/first turn, existing-thread ordinary prompt, and continuation-capable cold active-goal resume consumes one immutable snapshot; steer, observer resume, review, manual compact, F5/auth do not | shared next-turn controls in every browser, `GET/POST /api/settings/next-turn`, and meta |
| thread effective settings | not persisted; read-only connection-local `ThreadEffectiveSettingsRegistry` | Web next-turn intent never backfills this fact; it reports only the base proved by an upstream response/notification, with missing evidence unknown | thread and active-turn disclosure |
| Web active-turn disclosure | a read-only composition of the exact active turn id, matching initiator lease, current subscriber set, and provenance-bearing effective-settings registry | applies no setting and writes no runtime fact; it only reports what is currently provable | Web Runtime Details disclosure; a matching reroute model is `active_reroute`, a turn-start frozen field is `inherited`, and missing evidence is `unknown` |

Web active-turn disclosure is not a third writable-setting family.
`turn/started` freezes the thread base proved at that moment. Concrete values
and known nulls are labelled `inherited`; unknown remains an empty value plus
`unknown`. A later response/settings event does not backfill the active
snapshot. Only a matching `model/rerouted.turnId` labels model as
`active_reroute`. `WebNextTurnSettings` and Feishu binding-wise next-turn
settings cannot masquerade as these read-only facts, and provenance labels
cannot be omitted.

`turn/start` returns the authoritative `turn.id`, but that response identity by
itself is not proof of the matching current active lifecycle or its effective
settings. Focus waits for `turn/started` to bind that identity into the active-turn
snapshot. The settings were still sent with the request:
if Web, `fcodex`, or autonomous goal continuation after resume won the narrow
race, upstream may already have applied them while steering the Feishu input
into that regular turn.

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

For model/effort pair validation, Focus uses only `supportedReasoningEfforts`
from app-server `model/list` as metadata for an explicit model. It does not use
the narrow native-media registry in Section 3.1 to infer an effective effort or
to validate this writable pair.

- effort is `auto`: `validated`
- model is `auto` and effort is explicit: `deferred`
- the explicit model has no usable metadata and effort is explicit: `deferred`
- explicit-model metadata advertises the effort: `validated`
- explicit-model metadata does not advertise the effort: `rejected`

`model/list` is one authoritative catalog, not a set of hints that Focus may
best-effort splice together. If `data` contains a non-object, lacks a valid
model selector, or returns a capability field with a protocol-invalid type,
Focus rejects the whole catalog read. It must not silently skip a bad entry
and reinterpret catalog corruption as “model absent” or “capability not
supported.” An optional capability omitted by the protocol remains `unknown`.

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

If a new setting is added later, its owner, frontend scope, and application
boundary must first be classified as exactly one of:

1. binding-wise next-turn settings
2. instance-wide `WebNextTurnSettings`
3. another formally contracted explicit owner/scope, never inferred from a
   browser, writer, selection, or thread identity
4. read-only upstream/diagnostic view

Until that classification exists, the setting must not become a new command
surface, persisted project state layer, or implicit cross-frontend setting.
