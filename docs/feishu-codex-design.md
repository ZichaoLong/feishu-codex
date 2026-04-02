# feishu-codex Technical Design

See also:

- `docs/shared-backend-resume-safety.md` for the backend-sharing and guarded-`/resume` model.

## 1. Background

`feishu-cc` works because it wraps Claude Code CLI and fills several gaps with local conventions:

- session discovery by scanning `~/.claude/projects/*.jsonl`
- title sync by reading/writing Claude session JSONL
- permission interception by `PreToolUse` hook + local HTTP server
- special interactive flows by translating Claude hook decisions back into CLI-visible output

That approach is deeply tied to Claude Code internals. It is workable, but maintenance cost is high because:

- session metadata comes from private on-disk formats
- permission handling depends on shell hook behavior
- some UX flows are reconstructed rather than using a native remote protocol

For Codex, local inspection shows a better path exists.

Verified locally on 2026-03-31:

- `codex exec --json` emits structured JSONL events such as `thread.started`, `turn.started`, `item.completed`, `turn.completed`
- `codex exec resume` supports non-interactive session continuation by id or thread name
- `codex app-server` exposes an application protocol with:
  - `thread/start`
  - `thread/resume`
  - `thread/list`
  - `thread/read`
  - `thread/name/set`
  - `turn/start`
  - `turn/interrupt`
  - permission approval request/response objects

This means `feishu-codex` should not be a string-replaced clone of `feishu-cc`.
It should be a new adapter-driven design that keeps the Feishu layer and replaces the agent integration layer.

## 2. Goals

- Build a `feishu-codex` service with the same core user value as `feishu-cc`:
  - send prompts from Feishu
  - stream progress and final answer back to Feishu cards
  - manage long-lived sessions
  - resume sessions from the current directory
  - rename sessions
  - interrupt active work
  - route approvals to Feishu
- Make session metadata use a single source of truth from Codex itself.
- Avoid parsing private Codex on-disk files.
- Avoid shell-hook-based approval interception when Codex protocol already provides approvals.
- Make the implementation easier to maintain than `feishu-cc`.

## 3. Non-goals

- Do not emulate Codex TUI screen rendering in Feishu.
- Do not depend on undocumented internal file layouts for thread discovery or naming.
- Do not attempt to support every Codex experimental feature in v1.
- Do not build a generic multi-agent bridge in the first version.

## 4. Design Principles

- Native protocol first: prefer `codex app-server` APIs over CLI scraping or disk scanning.
- Single source of truth: thread id, cwd, title, preview come from Codex protocol, not local caches.
- Feishu-specific state stays local: only store metadata that Codex itself does not own, such as user-specific favorites.
- Keep transport and agent runtime separated so the Feishu layer can be reused.
- Make fallback paths explicit: `codex exec --json` is a validation and emergency fallback path, not the primary architecture.

## 5. Recommended Architecture

### 5.1 High-level layout

`feishu-codex` should be split into 4 layers:

1. Feishu transport layer
- receive user messages and card actions
- send text / cards / patch updates

2. Application layer
- command routing
- per-user-per-chat state
- card rendering
- session list sorting and matching

3. Codex adapter layer
- owns the Codex runtime connection
- translates app intents into Codex protocol requests
- translates Codex notifications into normalized events

4. Persistence layer
- local store for Feishu-only metadata
- no local cache for Codex thread title / cwd / preview

### 5.2 Process topology

Use a long-lived local `codex app-server` subprocess over `stdio://`.

Why:

- simpler than remote websocket auth
- keeps deployment similar to `feishu-cc`
- no need to expose an extra network port
- lets one service process own a persistent Codex protocol session

Future extension:

- allow connecting to remote `codex app-server` via websocket
- keep the same adapter interface

### 5.3 Why app-server is the primary path

The app-server protocol already exposes the lifecycle and approval primitives we need.

Compared with `codex exec --json`:

- better session control
- native thread listing and reading
- native rename API
- native interrupt API
- native approval requests and responses
- no need to infer behavior from stdout only

`codex exec --json` should remain as:

- a smoke-test tool
- an integration probe
- a fallback for narrow environments where app-server is unavailable

## 6. Core Abstractions

### 6.1 AgentAdapter

Introduce an adapter interface instead of baking CLI behavior into the handler.

Suggested shape:

```python
class AgentAdapter(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...

    def create_thread(self, *, cwd: str, model: str | None, settings: TurnSettings) -> ThreadRef: ...
    def resume_thread(self, thread_id: str) -> ThreadRef: ...
    def list_threads(self, query: ThreadQuery) -> ThreadPage: ...
    def read_thread(self, thread_id: str, include_turns: bool = False) -> ThreadData: ...
    def rename_thread(self, thread_id: str, name: str) -> None: ...

    def start_turn(self, thread_id: str, input_items: list[InputItem], settings: TurnSettings) -> TurnRef: ...
    def interrupt_turn(self, thread_id: str, turn_id: str | None = None) -> None: ...

    def approve_permissions(self, request_id: str, decision: PermissionDecision) -> None: ...
    def approve_exec(self, request_id: str, decision: ExecDecision) -> None: ...
```

### 6.2 Normalized domain objects

The application layer should not depend on raw protocol JSON.

Normalize these objects:

- `ThreadSummary`
  - `id`
  - `cwd`
  - `name`
  - `preview`
  - `created_at`
  - `updated_at`
  - `source_kind`
  - `status`
- `TurnEvent`
  - `thread_id`
  - `turn_id`
  - `phase`
  - `text_delta`
  - `tool_call`
  - `command_execution`
  - `diff`
  - `completed`
  - `error`
- `PermissionRequest`
  - `request_id`
  - `thread_id`
  - `turn_id`
  - `kind`
  - `filesystem_read`
  - `filesystem_write`
  - `network_enabled`
  - `reason`

## 7. Session and Thread Model

### 7.1 Single source of truth

Codex owns:

- thread id
- cwd
- thread name
- preview text
- source kind
- timestamps

Feishu local store owns only:

- favorites / starred flag
- per-chat current binding
- transient UI state

This avoids the `feishu_sessions.json.title` ambiguity that `feishu-cc` had to clean up.

### 7.2 Per-chat runtime state

For each `(user_id, chat_id)` keep:

- `current_thread_id`
- `current_cwd`
- `current_turn_id`
- `running`
- `session_model`
- `approval_mode`
- `message_queue`
- pending approval / form state

### 7.3 `/new`

Behavior:

- create a new Codex thread with `thread/start`
- bind current Feishu chat to the new thread id
- keep current `cwd`, `model`, and approval settings

### 7.4 `/session`

Behavior:

- call `thread/list`
- filter by `cwd = current_cwd`
- explicitly include source kinds instead of relying on protocol default

Required source kinds:

- `cli`
- `appServer`
- `exec` if we allow fallback-created threads

Rationale:

- protocol default source filtering is not enough
- otherwise local CLI threads and Feishu-created threads may appear in different universes

Display policy:

- favorites first
- then recent non-favorites
- use Codex `name` if set
- otherwise use Codex `preview`
- never use locally cached title fallback

### 7.5 `/resume <arg>`

Matching order:

1. exact thread id
2. unique thread id prefix
3. exact thread name

Candidate scope:

- same allowed source kinds as `/session`
- current cwd first for the card flow
- configurable broader search for explicit `/resume`

Implementation:

- use `thread/list` / `thread/read`
- if thread is not currently loaded by app-server, call `thread/resume`
- then bind the Feishu chat to that thread id

### 7.6 `/rename`

Use native `thread/name/set`.

This is a major improvement over `feishu-cc`:

- no JSONL patching
- no local title override
- title consistency between local Codex CLI and Feishu is native

### 7.7 `/cd`

Recommended behavior for v1:

- keep current `cwd` as a Feishu chat-level default
- changing cwd clears current thread binding and prepares a new thread on next user message

Do not mutate an existing thread across directories in v1.

Reason:

- session browsing semantics stay simple
- `thread/list(cwd=...)` continues to mean something stable
- matches the current `feishu-cc` mental model

## 8. Message and Streaming Model

### 8.1 Turn lifecycle

Use `turn/start` for new user input against the current thread.

`TurnStartParams` already supports:

- `threadId`
- `input`
- `cwd`
- `model`
- `approvalPolicy`
- `sandboxPolicy`

This is a better fit than building shell commands by hand.

### 8.2 Feishu streaming card

Maintain one active Feishu execution card per turn:

- assistant text deltas update the reply section
- command execution items render as tool / bash progress
- diff items render as concise summaries
- final turn completion seals the card

The card update path should operate on normalized events from the adapter, not raw Codex JSON.

### 8.3 Interrupt

Use native `turn/interrupt`.

Do not treat process kill as the first-line interrupt mechanism.

Process kill remains a last-resort recovery path only if:

- app-server subprocess hangs
- protocol connection is lost

## 9. Approval Model

Codex has a materially better approval model than Claude hook interception.

### 9.1 Native permission approvals

Verified protocol objects:

- `PermissionsRequestApprovalParams`
  - `threadId`
  - `turnId`
  - `itemId`
  - requested file system and network permissions
- `PermissionsRequestApprovalResponse`
  - granted permissions profile
  - scope: `turn` or `session`

This maps cleanly to Feishu buttons:

- allow once
- allow for session
- deny

### 9.2 Native exec approvals

Verified protocol object:

- `ExecCommandApprovalResponse`
  - `approved`
  - `approved_for_session`
  - `denied`
  - `abort`
  - protocol-specific policy-amendment variants

Recommended Feishu mapping:

- allow once
- allow this session
- deny but continue
- deny and stop current turn

The protocol is richer than `feishu-cc` today. V1 does not need to expose every advanced policy-amendment path in the UI.

### 9.3 Approval mode model

Do not copy Claude-specific permission modes exactly.

Use Codex-native concepts in the adapter, then map them to a Feishu-friendly UI:

- `interactive`
  - all protocol approval requests are surfaced to Feishu
- `session_relaxed`
  - session-scoped approvals can be granted from cards and cached naturally by Codex
- `dangerous`
  - only if explicitly configured
  - bypasses normal approval friction

The UI labels may stay compatible with `feishu-cc`, but the underlying model should be Codex-native.

## 10. User Input / Question Cards

`feishu-cc` needed dedicated handling for `AskUserQuestion` and `ExitPlanMode`.

For Codex, v1 should not assume an identical feature model exists.

Design choice:

- build the core bridge first around:
  - turns
  - streaming
  - approvals
  - session list / resume / rename / interrupt
- keep a generic `PromptRequest` / `UserResponse` abstraction in the adapter
- add specialized question cards only after verifying stable Codex protocol objects that require user choice

This prevents overfitting to Claude-specific interaction patterns.

## 11. Persistence Design

### 11.1 Local store contents

Suggested local store: `data/codex_threads.json`

Per user:

- `thread_id`
- `starred`
- optional Feishu-local tags later

Do not store:

- thread title
- cwd
- preview
- timestamps

### 11.2 Why favorites remain local

Codex has native thread naming, but not a clearly verified cross-client favorite concept.
Favorites are a Feishu UX concern, so keeping them local is acceptable.

Constraint:

- favorites must never override or shadow Codex thread metadata

## 12. Directory Semantics

`feishu-codex` should preserve the useful part of current `feishu-cc` behavior:

- Feishu chat has a current directory concept
- `/session` lists only current-directory candidates
- `/resume` can restore and switch current directory

Implementation with Codex protocol:

- `thread.list(cwd=current_dir, sourceKinds=[...])` for `/session`
- `thread.read` returns authoritative `cwd`
- restoring a thread updates the Feishu chat's `current_cwd`

## 13. Proposed Repository Structure

Suggested layout for the new project:

```text
feishu-codex/
  bot/
    feishu_bot.py
    cards.py
    handler.py
    codex_handler.py
    stores/
      favorites_store.py
      chat_state_store.py
    adapters/
      base.py
      codex_app_server.py
      codex_exec_fallback.py
    codex_protocol/
      client.py
      events.py
      models.py
      approvals.py
      threads.py
  config/
    codex.yaml.example
  docs/
    feishu-codex-design.md
```

If code reuse from `feishu-cc` is desired later, extract common Feishu and card infrastructure into a shared package after `feishu-codex` proves stable.

Do not start with a shared library first.

## 14. Implementation Plan

### Phase 0: Protocol probe

- start local `codex app-server` over stdio
- build a tiny JSON-RPC client
- verify:
  - initialize
  - thread/start
  - turn/start
  - streaming notifications
  - turn/interrupt
  - thread/list
  - thread/name/set
  - approval round-trip

### Phase 1: Core Feishu bridge

- new `CodexAppServerAdapter`
- new `codex_handler.py`
- prompt in, stream out
- `/new`
- `/status`
- `/cancel`

### Phase 2: Session management

- `/session`
- `/resume`
- `/rename`
- favorites
- current-directory semantics

### Phase 3: Native approvals

- permission approval cards
- exec approval cards
- session-scoped approval support

### Phase 4: Polishing

- model selection
- improved event rendering
- queue management
- fallback path via `codex exec --json`

## 15. Risks

### 15.1 app-server is marked experimental

Mitigation:

- keep the adapter isolated
- validate required APIs in Phase 0 before wider implementation
- keep `codex exec --json` fallback for smoke tests and emergency downgrade

### 15.2 Source-kind filtering can split session universes

Mitigation:

- always set explicit `sourceKinds`
- do not rely on protocol defaults

### 15.3 Protocol richness can tempt over-design

Mitigation:

- implement only thread lifecycle, turn lifecycle, streaming, rename, interrupt, approvals in v1

## 16. Recommendation

Build `feishu-codex` as a new project using `codex app-server` over stdio as the primary integration.

Do not fork `feishu-cc` and swap binaries.
Do not build on private Codex disk formats.
Do not start with `codex exec --json` as the main runtime.

This gives:

- cleaner session model
- native rename and resume
- cleaner approval handling
- less protocol guessing
- better long-term maintainability than `feishu-cc`
