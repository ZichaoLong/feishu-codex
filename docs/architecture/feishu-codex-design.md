# feishu-codex Technical Design

See also:

- `docs/contracts/session-profile-semantics.md`
- `docs/architecture/fcodex-shared-backend-runtime.md`
- `docs/decisions/shared-backend-resume-safety.md`
- `docs/archive/codex-handler-decomposition-plan.md`

## 1. Background

`feishu-codex` is an independent Codex-oriented project, not a thin rename of an
older Claude integration.

Historical context still matters:

- [`feishu-cc`](https://github.com/ZichaoLong/feishu-cc) proved the Feishu-side
  interaction model
- but that project depended on Claude-specific local file formats and hook
  behavior
- `feishu-codex` keeps the Feishu-side transport and interaction lessons while
  switching the agent/runtime integration to Codex-native surfaces

Upstream baseline:

- Codex source repository: [`openai/codex`](https://github.com/openai/codex.git)
- Current local validation baseline: `codex-cli 0.118.0` (checked on
  2026-04-03)

The design is based on current Codex capabilities that are useful to a Feishu
bridge:

- `codex app-server` as the primary application-facing runtime surface
- `codex exec --json` as a structured probe / debugging aid
- `codex exec resume` and thread-oriented CLI / app-server flows for session
  continuity

## 2. Goals

- Provide a Feishu bridge for Codex prompts, streaming output, approvals, and
  long-lived thread management
- Keep Codex thread metadata under Codex as the source of truth
- Minimize coupling to private on-disk formats or shell-hook behavior
- Keep the Feishu layer, local wrapper layer, and Codex protocol layer cleanly
  separated
- Preserve a low-friction path for users who need to continue the same live
  thread from Feishu and local TUI
- Allow one local operator to run multiple Feishu instances on one machine
  while still sharing one `CODEX_HOME`

## 3. Non-goals

- Recreate the Codex TUI screen inside Feishu
- Depend on undocumented Codex disk layouts for thread discovery or metadata
- Support every experimental Codex feature in the first iteration
- Reuse `feishu-cc` code as a hard architectural dependency
- Treat bare `codex` and shared-backend `fcodex` as the same operational path

## 4. Current Design Principles

- Native protocol first: prefer `codex app-server` behavior and APIs over local
  scraping or reconstructed state
- Single source of truth: thread id, cwd, title, preview, source, and runtime
  config come from Codex
- Feishu-specific state stays local: per-instance local default profile and
  thread/UI binding state remain in `feishu-codex`
- Shared-backend behavior is explicit: continuing the same live thread with
  Feishu should go through the same instance backend
- `CODEX_HOME` and Feishu runtime boundaries stay separate: the former is
  shared, the latter is isolated per instance
- Runtime assumptions are documented: wrapper and shared-backend behavior should
  live in docs, not only in code

## 5. Current Architecture

### 5.1 Layers

`feishu-codex` is organized into four layers:

1. Feishu transport layer
   - receives user messages and card actions
   - sends text, cards, and message patches
2. Application layer
   - command routing
   - user-isolated p2p runtime state and group-shared runtime state keyed by `chat_id`
   - card rendering
   - session and resume coordination
3. Codex adapter and protocol layer
   - owns the Codex runtime connection
   - translates handler actions into Codex requests
   - normalizes notifications and responses
4. Local state layer
   - stores Feishu-only metadata and runtime discovery state
   - deliberately does not replace Codex thread metadata

### 5.2 Runtime Topology

Current runtime behavior:

- all instances share one `CODEX_HOME`
- each instance owns its own:
  - `FC_CONFIG_DIR`
  - `FC_DATA_DIR`
  - service owner
  - control plane
  - managed `codex app-server` backend
- in this repository, `shared backend` means an instance-local shared backend,
  not one global backend for the whole machine
- one instance backend prefers `ws://127.0.0.1:8765`
- if that default port is unavailable, that instance service falls back to
  another free local port and publishes the active endpoint through its own
  local runtime state
- `fcodex` first chooses the target instance, then discovers that instance's
  active backend endpoint and attaches to that same instance backend
- `fcodex` adds a thin local websocket proxy only when it needs shared-backend
  cwd correction for upstream remote-mode behavior
- the machine also maintains two global coordination facts:
  - the running-instance registry
  - the thread live-runtime lease

The exact wrapper/runtime mechanics are documented in
`docs/architecture/fcodex-shared-backend-runtime.md`.

### 5.3 Key Application Modules

Current module split:

- `bot/codex_handler.py`: Feishu-facing command handling and session binding
- `bot/cards.py`: user-facing card rendering
- `bot/card_text_projection.py`: card text projection boundary; owns the
  terminal `final_reply_text` carrier contract and inbound `interactive`
  strong-contract / best-effort text extraction
- `bot/adapters/codex_app_server.py`: Codex adapter boundary
- `bot/codex_protocol/client.py`: websocket JSON-RPC client for `codex app-server`
- `bot/fcodex.py` and `bot/fcodex_proxy.py`: local wrapper and thin proxy
- `bot/feishu_codexctl.py` and `bot/service_control_plane.py`: local service-admin
  CLI and the in-process control plane for the running service
- `bot/instance_layout.py` and `bot/instance_resolution.py`: multi-instance
  filesystem layout and current/target instance resolution
- `bot/binding_identity.py`: stable admin-facing binding identifiers
- `bot/binding_runtime_manager.py`: owner of `binding` / `subscribe` /
  `attach` / `released` runtime state and local runtime snapshots
- `bot/thread_access_policy.py`: policy boundary for thread sharing, Feishu
  write-owner, and interaction-owner admission
- `bot/thread_runtime_coordination.py`: cross-instance live-runtime lease
  acquisition, automatic transfer, and reject flow
- `bot/turn_execution_coordinator.py`,
  `bot/execution_output_controller.py`, and
  `bot/execution_recovery_controller.py`: execution lifecycle state transitions,
  execution-card publishing, terminal-result delivery, and watchdog /
  reconcile / degraded-channel handling
- `bot/runtime_admin_controller.py`: `/status`,
  `/release-feishu-runtime`, and control-plane status/admin management
- `bot/inbound_surface_controller.py`: inbound command surface, card-action
  routing, and help-card command reuse
- `bot/forward_aggregator.py`: merged-forward buffering, timeout dispatch, and
  forwarded-message tree rendering; it owns this transport-local state machine
  instead of leaving it scattered across `FeishuBot`
- `bot/group_history_recovery.py`: assistant-mode group-history recovery,
  local-log merging, context formatting, and boundary `message_id` derivation;
  it does not depend on the Feishu SDK directly, so request construction and
  API calls stay in the `FeishuBot` transport boundary and enter through
  explicit paginated-result ports
- `bot/prompt_turn_entry_controller.py`: prompt entry orchestration,
  lease-acquisition, and released -> attached recovery flow
- `bot/adapter_notification_controller.py`: adapter-notification routing,
  interpretation, and downstream dispatch
- `bot/interaction_request_controller.py`: owns pending approval / user-input
  request state and fail-closed handling for interactive requests
- `bot/codex_session_ui_domain.py`: owns session-card UI flows, including
  transient rename-form state and RuntimeLoop-submitted resume target resolution
- `bot/codex_settings_domain.py`: owns user-facing settings and identity
  commands such as `/profile`, `/approval`, `/sandbox`, `/permissions`,
  `/mode`, `/whoami`, and `/init`; it crosses bot/runtime/profile boundaries
  through explicit `SettingsDomainPorts` rather than retaining a handler owner
- `bot/execution_transcript.py`: an internal transcript assembler for execution-card
  presentation; it builds display-only `reply_segments` / `process_log`
  fragments, and can support hiding the terminal final-answer segment from the
  execution card once that answer has been delivered through a separate
  authoritative carrier; it does not own thread, owner, or binding-level state
- `bot/stores/thread_admission_store.py`: per-instance Feishu visible-thread
  admissions
- `bot/stores/instance_registry_store.py`: machine-global running-instance registry
- `bot/stores/thread_runtime_lease_store.py`: machine-global thread
  live-runtime lease
- `bot/stores/*.py`: per-instance local default profile, runtime backend
  discovery state, and group-chat state

One maintenance rule should also stay explicit for the Feishu transport layer:

- transport-boundary modules such as `FeishuBot` should keep their SDK
  dependency surface visible
- wildcard imports should not be the long-term way to hide which IM API types
  the module actually depends on

One adapter-boundary contract also needs to stay explicit:

- `resume` request inputs should not be abstracted as only `profile`
- for an unloaded thread, Feishu already passes `profile / model /
  model_provider` as resume-time recovery hints
- for a loaded thread, carrying those inputs does not mean the live runtime can
  be rewritten in place

So the adapter boundary should describe which resume inputs are accepted by the
request contract, rather than exposing an older abstract signature that is
narrower than the real call surface.

This first ownership-tightening pass has already landed. The boundaries that
still need to stay explicit as the code evolves are:

- thread sharing, Feishu write-owner, and interaction-owner admission rules
  should stay behind one policy boundary; that boundary is now
  `ThreadAccessPolicy`, not scattered handler/prompt/group entry logic
- `BindingRuntimeManager` should expose snapshot / inventory / iteration style
  read APIs to the rest of the system, rather than leaking the whole mutable
  runtime-state map
- orchestration components such as `PromptTurnEntryController` should be wired
  through explicit ports, rather than growing anonymous callback lists
- session-UI initiated resume flow should also cross the runtime boundary
  through explicit runtime ports, rather than reaching into handler-private
  loop helpers from inside the domain object
- bot-facing domains such as settings, group, and attachment ingress should
  depend on named ports for the specific bot/runtime capabilities they need,
  rather than retaining broad owner protocols with implicit `bot: Any`
- settings-domain commands should depend on named settings ports for bot
  identity/context, runtime view/update, and profile state, rather than on a
  broad handler-owner protocol

Thread-summary access should also keep two contracts separate:

- authoritative read: direct backend read by `thread_id`, used by paths that are
  about to perform a real operation
- bounded-list best-effort lookup: only supplements context or error wording
  from the current global list view, and must not be treated as proof that a
  thread does not exist

Concurrency ownership should also remain explicit:

- `RuntimeLoop` is already the primary serialization mechanism for handler-side
  runtime state mutations
- session-UI initiated resume resolution and resume handoff should also go
  through `RuntimeLoop`, rather than opening ad-hoc background threads that
  touch the shared adapter/runtime boundary from the side
- binding resolution and runtime-state hydrate/create should go through a
  single resolver path, rather than open-coding "pick a binding key, then
  maybe create state" in multiple call sites
- objects such as `ThreadLeaseRegistry` should currently be treated as
  runtime-owned internal state, not as general-purpose thread-safe components
- `CodexHandler._lock` still acts as a broad shared-state fallback lock, but the
  long-term goal should be reducing the amount of state that must be shared at
  all, rather than first splitting that lock into smaller locks

This split is no longer only a "move help/settings/group/session/file out of one
large flow" exercise. The ownership-decomposition direction described in the
historical plan has now largely landed:

- `BindingRuntimeManager` now owns Feishu runtime management for `binding` /
  `subscribe` / `attach` / `released`
- `ThreadAccessPolicy` and the lease stores now own the admission rules for
  Feishu write owner and interaction owner
- `TurnExecutionCoordinator`, `ExecutionOutputController`,
  `ExecutionRecoveryController`, `InteractionRequestController`, and
  `AdapterNotificationController` now own the turn / execution / request-bridge
  lifecycle slices
- `RuntimeAdminController` now owns runtime-admin and control-plane management
- `InboundSurfaceController` and `PromptTurnEntryController` now own the inbound
  surface and prompt-entry orchestration layers

So the earlier line "the next step should not be more file-level slicing of
`CodexHandler`, but state-ownership decomposition" should now be read as an
architectural direction that has already been executed, not as a still-pending
roadmap item.

The main ownership that still remains at the top-level `CodexHandler` is now:

- runtime lifecycle bootstrap / shutdown and service-instance ownership
- assembly of controllers / domains / adapter and cross-domain orchestration
- a small set of helpers and fallback synchronization that still belongs in the
  top-level orchestrator

That means the next cleanup step is no longer "decompose the planned ownership
slices once more". It is to keep shrinking the amount of shared state and
cross-domain coordination that the top-level orchestrator must hold directly,
and to avoid reintroducing new implicit ordering rules into `CodexHandler`.

The rollout order and phase boundaries remain documented in
`docs/archive/codex-handler-decomposition-plan.md`, but that document should now
be treated as historical rollout material rather than a statement of unfinished
current work.

## 6. Data and Behavioral Boundaries

### 6.1 Codex-Owned Data

Codex remains the authority for:

- thread id
- cwd
- thread name
- preview text
- source kind and status
- thread timestamps
- runtime config and model/provider state

### 6.2 Feishu-Local Data

`feishu-codex` keeps only data that is Feishu- or integration-specific:

- per-instance local default profile used by Feishu and `fcodex` launches
  routed to that same instance
- per-instance runtime shared-backend discovery state
- p2p thread bindings and group-shared thread bindings keyed by `chat_id`
- group-chat mode, group ACL, group context logs, and boundary state
- transient approval, rename, and card state
- the per-instance thread-admission set

There are also two machine-global coordination states:

- the running-instance registry
- the thread live-runtime lease

They live under shared `FC_GLOBAL_DATA_DIR`.
They are neither Feishu-chat state nor Codex-owned thread metadata; they exist
only for local CLI discovery and multi-instance runtime coordination.

Within that set, `binding` is intentionally a restart-persistent local bookmark:

- it answers which thread a Feishu chat should continue by default next time
- it is not the same thing as whether Feishu is still attached to the thread
- it is not the same thing as whether the backend is still loaded

So:

- persistent `binding` is a formal product requirement
- explicit clearing of one or all bindings is also a legitimate local admin need
- those reset actions belong to the `feishu-codexctl` binding-management surface
- they should no longer be treated as a separate architectural concept of
  directly deleting `chat_bindings.json`
- the persisted binding schema should also fail closed rather than carrying
  legacy half-states forward
- whenever `current_thread_id` is non-empty, `feishu_runtime_state`
  must be explicitly present
- `feishu_runtime_state` may only be `attached` or `released`
- a `released` binding must not carry a residual `write_owner`
- violations should be treated as storage corruption and fail fast instead of
  being silently normalized during load

`system.yaml.admin_open_ids` follows the same single-source-of-truth rule:

- it is the only authoritative source for the admin set
- the in-memory admin set in a running service is only a cache, not a second
  source of truth
- `/init <token>` is only a controlled convenience write path, and it still
  writes `system.yaml`
- manual edits to `system.yaml` do not require hot reload; the authoritative
  value takes effect after service restart or an explicit reload path
- the cache must never write back into the authority, and a later
  "config + runtime merge" must not silently restore admins that were removed
  from config

### 6.3 Session and Directory Semantics

Exact command semantics are documented outside this design document:

- `docs/contracts/session-profile-semantics.md` covers `/session`, `/resume`, `/profile`,
  `/rm`, and wrapper semantics
- `docs/decisions/shared-backend-resume-safety.md` covers current `/resume` semantics and
  backend safety rules

This document only fixes the boundary:

- thread metadata comes from Codex
- Feishu chat state decides the current working context
- shared-backend continuation is explicit rather than implicit

### 6.4 Approval Model

The current project uses Codex-native approval and sandbox concepts:

- app-server approval requests and responses
- Codex approval policy and sandbox policy fields
- Feishu-facing presets layered on top of those primitives

The integration does not depend on Claude-style shell hook interception.

### 6.5 Group Chat Contract

The detailed group-chat behavior contract no longer lives inline in this design
document.

At the design level, the important boundaries are:

- group backend state is shared by `chat_id`, not split by human member
- `assistant` keeps separate context boundaries for the main chat flow and each
  group thread, while still sharing one backend session
- ACL answers whether a human member is eligible; whether a mention is still
  required is decided by the group mode
- other bots do not directly trigger `feishu-codex`; their messages enter
  context only through history recovery

The formal behavior contract is now:

- `docs/contracts/group-chat-contract.md`
- manual regression checklist:
  `docs/verification/group-chat-manual-test-checklist.zh-CN.md`

## 7. Current Repository Structure

The repository is easier to understand by responsibility than by a frozen
full-tree dump.

- repository root
  - operator-facing material and packaging live in `README.md`, `install.py`,
    `install.sh`, `install.ps1`, and `pyproject.toml`
  - the tracked agent-preference template lives in `AGENTS.example.md`
  - real local override files such as `AGENTS.md` and `AGENTS.zh-CN.md`
    remain intentionally gitignored
- `bot/`
  - entrypoints and transport boundaries: `__main__.py`, `standalone.py`,
    `handler.py`, `feishu_bot.py`
  - top-level orchestration and user-facing domains:
    `codex_handler.py`, `codex_group_domain.py`, `codex_help_domain.py`,
    `codex_session_ui_domain.py`, `codex_settings_domain.py`,
    `file_message_domain.py`, `inbound_surface_controller.py`
  - runtime state, execution flow, and coordination:
    `runtime_loop.py`, `runtime_state.py`, `runtime_view.py`,
    `binding_runtime_manager.py`, `thread_access_policy.py`,
    `thread_lease_registry.py`, `thread_runtime_coordination.py`,
    `turn_execution_coordinator.py`, `execution_output_controller.py`,
    `execution_recovery_controller.py`, `execution_transcript.py`,
    `interaction_request_controller.py`, `adapter_notification_controller.py`,
    `runtime_admin_controller.py`, `runtime_card_publisher.py`,
    `prompt_turn_entry_controller.py`
  - within that runtime slice, `runtime_state.py` is the code-level single
    source of truth for the mutable runtime-state schema, reducer messages, and
    canonical Feishu/backend runtime status vocabulary; other modules should
    import those symbols rather than redefining partial local variants
  - shared UI / helper boundaries: `cards.py`, `card_text_projection.py`,
    `shared_command_surface.py`, `feishu_types.py`, `codex_config_reader.py`
  - wrapper and service-admin path: `fcodex.py`, `fcodex_proxy.py`,
    `feishu_codexctl.py`, `service_control_plane.py`, `instance_layout.py`,
    `instance_resolution.py`, `profile_resolution.py`, `session_resolution.py`,
    `binding_identity.py`
  - Codex adapter / protocol boundary:
    `adapters/base.py`, `adapters/codex_app_server.py`,
    `codex_protocol/client.py`
  - persisted local state: `stores/app_server_runtime_store.py`,
    `stores/chat_binding_store.py`, `stores/group_chat_store.py`,
    `stores/instance_registry_store.py`, `stores/interaction_lease_store.py`,
    `stores/pending_attachment_store.py`, `stores/profile_state_store.py`,
    `stores/service_instance_lease.py`, `stores/thread_admission_store.py`,
    `stores/thread_runtime_lease_store.py`
- `config/`
  - example local config files: `system.yaml.example`, `codex.yaml.example`
- `docs/`
  - formal contracts: `docs/contracts/`
  - current architecture/runtime shape: `docs/architecture/`
  - design decisions and safety boundaries: `docs/decisions/`
  - manual verification material: `docs/verification/`
  - archived rollout/history material: `docs/archive/`
  - local working notes that are not repository truth: `docs/_work/`
- `tests/`
  - unit coverage for adapter/wrapper behavior, handler/controller flows,
    runtime state transitions, stores, cards, and Feishu transport helpers

This grouped view should stay aligned with the ownership split in §5.3.
When a new module materially changes ownership boundaries, update both sections
in the same change.

## 8. Evolution Boundaries

- Upstream Codex app-server and remote behavior may evolve; keep the adapter and
  wrapper boundaries isolated
- Shared-backend wrapper behavior depends on current upstream remote semantics,
  especially around `thread/start`, `cwd`, and reconnect timing
- `codex exec --json` remains useful for probes, smoke checks, and debugging,
  but it is not the current primary runtime path
- Future feature work should preserve the current document split:
  semantics, runtime model, safety model, and design constraints are separate
  concerns
