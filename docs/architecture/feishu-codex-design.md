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
- Feishu-specific state stays local: local default profile and thread/UI
  binding state remain in `feishu-codex`
- Shared-backend behavior is explicit: continuing the same live thread with
  Feishu should go through one backend
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

- the `feishu-codex` service uses a managed app-server path by default
- it starts a local `codex app-server` subprocess and talks to it over websocket
- the shared backend prefers `ws://127.0.0.1:8765`
- if that default port is unavailable, the service falls back to a free local
  port and publishes the active endpoint through local runtime state
- `fcodex` and other remote-style flows discover that active endpoint and attach
  to the same shared backend
- `fcodex` adds a thin local websocket proxy only when it needs shared-backend
  cwd correction for upstream remote-mode behavior

The exact wrapper/runtime mechanics are documented in
`docs/architecture/fcodex-shared-backend-runtime.md`.

### 5.3 Key Application Modules

Current module split:

- `bot/codex_handler.py`: Feishu-facing command handling and session binding
- `bot/cards.py`: user-facing card rendering
- `bot/adapters/codex_app_server.py`: Codex adapter boundary
- `bot/codex_protocol/client.py`: websocket JSON-RPC client for `codex app-server`
- `bot/fcodex.py` and `bot/fcodex_proxy.py`: local wrapper and thin proxy
- `bot/feishu_codexctl.py` and `bot/service_control_plane.py`: local service-admin
  CLI and the in-process control plane for the running service
- `bot/binding_identity.py`: stable admin-facing binding identifiers
- `bot/interaction_request_controller.py`: owns pending approval / user-input
  request state and fail-closed handling for interactive requests
- `bot/codex_session_ui_domain.py`: owns session-card UI flows, including
  transient rename-form state
- `bot/execution_transcript.py`: an internal transcript assembler for execution-card
  presentation; it builds reply/log fragments and does not own thread, owner,
  or binding-level state
- `bot/stores/*.py`: local default profile, runtime backend discovery state,
  and group-chat state

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

Some decomposition constraints should also remain explicit:

- thread sharing, Feishu write-owner, and interaction-owner admission rules
  should stay behind one policy boundary; that boundary is now
  `ThreadAccessPolicy`, not scattered handler/prompt/group entry logic
- `BindingRuntimeManager` should expose snapshot / inventory / iteration style
  read APIs to the rest of the system, rather than leaking the whole mutable
  runtime-state map
- orchestration components such as `PromptTurnEntryController` should be wired
  through explicit ports, rather than growing anonymous callback lists

Thread-summary access should also keep two contracts separate:

- authoritative read: direct backend read by `thread_id`, used by paths that are
  about to perform a real operation
- bounded-list best-effort lookup: only supplements context or error wording
  from the current global list view, and must not be treated as proof that a
  thread does not exist

Concurrency ownership should also remain explicit:

- `RuntimeLoop` is already the primary serialization mechanism for handler-side
  runtime state mutations
- binding resolution and runtime-state hydrate/create should go through a
  single resolver path, rather than open-coding "pick a binding key, then
  maybe create state" in multiple call sites
- objects such as `ThreadLeaseRegistry` should currently be treated as
  runtime-owned internal state, not as general-purpose thread-safe components
- `CodexHandler._lock` still acts as a broad shared-state fallback lock, but the
  long-term goal should be reducing the amount of state that must be shared at
  all, rather than first splitting that lock into smaller locks

This first-layer split already moved help/settings/group/session/file concerns
out of the old monolithic flow, but it is not yet the final form of "real
decoupling".

The next step should not be more file-level slicing of `CodexHandler`. It
should be state-ownership decomposition:

- Feishu runtime management for `binding` / `subscribe` / `attach` /
  `released`
- owner/lease rules for Feishu write owner and interaction owner
- turn / execution lifecycle, including execution anchor, watchdog, and
  follow-up orchestration
- service control-plane management
- adapter notification / request bridge responsibilities

If those state machines continue to live together in `CodexHandler`, the result
is only lighter file navigation, not a clearer long-term architecture. The
maintenance burden still comes from remembering implicit ordering constraints
across unrelated runtime concerns.

For the recommended rollout order and phase boundaries of that ownership
decomposition, see `docs/archive/codex-handler-decomposition-plan.md`.

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

- local default profile used by Feishu and default `fcodex` launches
- runtime shared-backend discovery state
- p2p thread bindings and group-shared thread bindings keyed by `chat_id`
- group-chat mode, group ACL, group context logs, and boundary state
- transient approval, rename, and card state

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
- whenever `current_thread_id` is non-empty, `current_thread_runtime_state`
  must be explicitly present
- `current_thread_runtime_state` may only be `attached` or `released`
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

The current repository layout is:

```text
feishu-codex/
  bot/
    __main__.py
    standalone.py
    feishu_bot.py
    handler.py
    cards.py
    codex_handler.py
    fcodex.py
    fcodex_proxy.py
    feishu_codexctl.py
    service_control_plane.py
    binding_identity.py
    config.py
    constants.py
    profile_resolution.py
    session_resolution.py
    adapters/
      base.py
      codex_app_server.py
    codex_protocol/
      client.py
    stores/
      app_server_runtime_store.py
      profile_state_store.py
  config/
    system.yaml.example
    codex.yaml.example
  docs/
    contracts/
    architecture/
    decisions/
    verification/
    archive/
    doc-index.md
    doc-index.zh-CN.md
  tests/
    test_codex_app_server.py
    test_codex_handler.py
  install.sh
  pyproject.toml
  README.md
```

This structure is already sufficient for the current architecture:

- Feishu transport and handler code stay in `bot/`
- Codex integration boundaries stay in `bot/adapters/` and
  `bot/codex_protocol/`
- local persisted state stays in `bot/stores/`
- formal feature contracts stay in `docs/contracts/`
- current architecture and implementation boundaries stay in
  `docs/architecture/`
- upstream-derived safety decisions stay in `docs/decisions/`
- manual verification material stays in `docs/verification/`
- completed rollout plans and historical material stay in `docs/archive/`

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
