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
   - per-user / per-chat runtime state
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
- `bot/stores/*.py`: favorites, local default profile, and runtime backend
  discovery state

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
- per-chat thread bindings
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
