# feishu-codex Technical Design

See also:

- `docs/session-profile-semantics.md`
- `docs/fcodex-shared-backend-runtime.md`
- `docs/shared-backend-resume-safety.md`

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
- Feishu-specific state stays local: favorites, local default profile, and UI
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
- `bot/stores/*.py`: favorites, local default profile, runtime backend
  discovery state, and group-chat state

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

- favorites / starred state
- local default profile used by Feishu and default `fcodex` launches
- runtime shared-backend discovery state
- p2p thread bindings and group-shared thread bindings keyed by `chat_id`
- group-chat mode, group ACL, group context logs, and boundary state
- transient approval, rename, and card state

### 6.3 Session and Directory Semantics

Exact command semantics are documented outside this design document:

- `docs/session-profile-semantics.md` covers `/session`, `/resume`, `/profile`,
  `/rm`, and wrapper semantics
- `docs/shared-backend-resume-safety.md` covers guarded `/resume` and backend
  safety rules

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
  - thread (`thread` container) history recovery does not currently promise a
    strict `group_history_fetch_lookback_seconds` cutoff, because the public
    Feishu API does not support `start_time` / `end_time` for thread containers
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
      favorites_store.py
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
