# feishu-codex Technical Design

See also:

- `docs/session-profile-semantics.md`
- `docs/fcodex-shared-backend-runtime.md`
- `docs/shared-backend-resume-safety.md`
- `docs/codex-handler-decomposition-plan.md`

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
`docs/fcodex-shared-backend-runtime.md`.

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
decomposition, see `docs/codex-handler-decomposition-plan.md`.

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

- `docs/session-profile-semantics.md` covers `/session`, `/resume`, `/profile`,
  `/rm`, and wrapper semantics
- `docs/shared-backend-resume-safety.md` covers current `/resume` semantics and
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

The following behaviors are part of the current implementation contract:

#### Defaults

- new groups default to `assistant`
- new groups default to `admin-only`
- group administrators come from `system.yaml.admin_open_ids`
- `system.yaml.admin_open_ids` is authoritative; the runtime admin set is only a cache
- runtime identity decisions use `open_id` only; `user_id` is retained only for
  logs and `/whoami` diagnostics, and requires
  `contact:user.employee_id:readonly` if you want it to be populated reliably

#### Human-member access

- whether a human member is eligible to trigger the bot in a group is decided
  by that group's ACL
- ACL decides "who is eligible"; whether a mention is still required is decided
  by the group mode
- group ACL only manages human members, not other bots
- supported ACL policies are:
  - `admin-only`
  - `allowlist`
  - `all-members`

#### Group modes

- strict explicit-mention matching depends on `system.yaml.bot_open_id`
- realtime discovery from `/whoareyou` and `/init` is only for diagnostics and bootstrap; it does not replace the runtime value read from `system.yaml.bot_open_id`
- if `system.yaml.trigger_open_ids` is configured, mentions that hit those
  `open_id`s are also treated as valid triggers
- `trigger_open_ids` only extends which mentions count as a trigger; it does not
  bypass ACL and it does not replace `bot_open_id`
- p2p backend state stays user-isolated; group backend state is shared by
  `chat_id`
- `assistant`
  - receives and caches group messages
  - replies only when a valid trigger mention is present
  - includes group context since the last trigger boundary
- main-flow (`chat` container) history recovery is constrained by
  `group_history_fetch_limit` and `group_history_fetch_lookback_seconds`
- main-flow recovery keeps a small backward slack window around the boundary
  timestamp, then dedupes with boundary `message_id`s so messages are not
  missed at the edge of the time window
- `group_history_fetch_limit` and `group_history_fetch_lookback_seconds` also
  act as the global recovery switch; setting either to `0` disables both
  main-flow and thread recovery
- thread (`thread` container) history recovery does not currently promise a
  strict `group_history_fetch_lookback_seconds` cutoff, because the public
    Feishu API does not support `start_time` / `end_time` for thread containers
  - thread recovery prefers `ByCreateTimeDesc` and stops as soon as it crosses
    the stored boundary; it only falls back to ascending scan if descending
    ordering is not usable in practice
  - maintains separate context boundaries for the main chat flow and each group
    thread
  - still uses one shared group backend session, so the model may remember
    conclusions established elsewhere in the same group
- `mention-only`
  - does not cache group context
  - triggers only on valid trigger mentions
- `all`
  - human group messages can trigger directly
  - highest spam risk

#### Group-command triggering

- p2p commands can be sent directly
- all group `/` commands are admin-only
- in group `assistant` and `mention-only`, admin commands themselves must also
  explicitly mention a trigger target first
- in group `all`, admins can send group commands directly
- group commands do not enter the `assistant` context log and do not advance the
  assistant boundary

#### Assistant-mode context

- `assistant` writes group messages into a local log
- only effective human mentions can trigger a reply
- because Feishu does not push other bots' messages to bots in real time,
  `assistant` backfills a limited window of recent history on every effective
  mention
- history backfill and live group logs are merged into one context pipeline
- the context boundary tracks both sequence and time so each new effective
  mention can resume from the previous boundary
- when the trigger happens inside a group thread, execution cards, ACL denials,
  and long-text follow-ups should stay in that thread instead of jumping back to
  the main flow

#### ACL denial feedback

- unauthorized members in `assistant` / `mention-only` receive a denial message
  only when they explicitly mention the bot
- unauthorized members in `all` are silently ignored for plain messages to
  avoid noise
- unauthorized members in `all` still receive a denial message when they
  explicitly mention the bot or send a group command

#### Other bots and history

- other bots cannot directly trigger `feishu-codex`
- if group history is visible to the bot, messages from other bots can still
  enter the `assistant` context through the per-mention history backfill
- if history backfill is disabled, other bots' messages do not automatically
  enter the `assistant` context

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
    *.md
    *.zh-CN.md
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
- semantic, runtime, and design explanations stay in `docs/`

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
