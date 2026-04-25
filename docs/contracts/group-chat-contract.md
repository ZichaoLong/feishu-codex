# Group Chat Contract

Chinese version: `docs/contracts/group-chat-contract.zh-CN.md`

This document defines the formal behavior contract for group-chat features in
`feishu-codex`.

It answers:

- what defaults a new group starts with
- what group activation, group mode, and admin-command rules each control
- how `assistant` context, history recovery, and thread boundaries behave
- who may handle runtime approval and supplemental-input cards in groups
- which behaviors are guaranteed and which are current limitations

See also:

- `docs/architecture/feishu-codex-design.md`
- `docs/contracts/feishu-thread-lifecycle.md`
- `docs/contracts/feishu-help-navigation.md`
- `docs/verification/group-chat-manual-test-checklist.zh-CN.md`

## 1. Scope

This document only defines the group-chat contract.

It does not redefine:

- p2p thread lifecycle
- the shared state vocabulary for `/status`, `/unsubscribe`, and the local
  admin surface
- `fcodex` wrapper semantics

Those remain owned by their dedicated documents.

## 2. Defaults

- new groups default to `assistant`
- new groups default to a **deactivated** state
- group administrators come from `system.yaml.admin_open_ids`
- `system.yaml.admin_open_ids` is authoritative; the runtime admin set is only
  a cache
- admins may still bootstrap and manage a group before it is activated
- runtime identity decisions use `open_id` only
- `user_id` is retained only for logs and `/whoami` diagnostics
- `contact:user.employee_id:readonly` is required if you want `user_id` to be
  populated reliably

## 3. Group Activation Boundary

- group activation is a **chat-level** switch, not a member-level ACL
- while a group is deactivated, non-admin users may not use the bot there
- once a group is activated, both current members and later-joined members may
  use the bot normally
- an activated group stays usable even if the admin later leaves the group; it
  remains so until an admin comes back and deactivates it, or the group state
  is explicitly cleared by an admin surface
- group activation state should persist across service restarts
- activate / deactivate / re-activate only change the authorization state and
  activation metadata; they do not automatically clear group logs,
  `assistant` boundaries, the current thread binding, or the group mode
- only admins may activate or deactivate a group
- the current activation-management surface is:
  - `/group`
  - `/group activate`
  - `/group deactivate`

## 4. Group Modes

- strict explicit-mention matching depends on `system.yaml.bot_open_id`
- realtime discovery from `/whoareyou` and `/init` is only for diagnostics and
  bootstrap; it does not replace the runtime value read from
  `system.yaml.bot_open_id`
- if `system.yaml.trigger_open_ids` is configured, mentions that hit those
  `open_id`s are also treated as valid triggers
- `trigger_open_ids` only extends which mentions count as a trigger; it does
  not bypass group activation and it does not replace `bot_open_id`
- p2p backend state stays user-isolated; group backend state is shared by
  `chat_id`

### 4.1 `assistant`

- only logs messages from human users who are currently allowed to use the bot
- replies only when a valid trigger mention is present
- includes group context since the last trigger boundary
- maintains separate context boundaries for the main chat flow and each group
  thread; the main flow does not automatically read thread replies, and a
  thread does not automatically read the main flow
- still uses one shared group backend session, so the model may remember
  conclusions established elsewhere in the same group

### 4.2 `mention-only`

- does not cache group context
- triggers only on valid trigger mentions
- backend input contains only the current group message, without history
  context
- the current group message is sent through a lightweight
  `group_chat_current_turn` wrapper and should prefer `sender_name`

### 4.3 `all`

- group messages from users currently allowed to use the bot can trigger
  directly
- backend input is passed through like p2p by default: no history context and
  no extra `group turn` wrapper
- has the highest spam risk
- `/groupmode all` must reject when the currently bound thread is already
  shared by other Feishu chats
- once a group is in `all` mode, that thread must not be shared with other
  Feishu chats; for the exact runtime vocabulary and rejection rules, see
  `docs/contracts/runtime-control-surface.md`

## 5. Group Commands and Shared-State Rules

- all group `/` commands are admin-only
- in group `assistant` and `mention-only`, admin commands themselves must also
  explicitly mention a trigger target first
- in group `all`, admins can send group commands directly
- "group commands" here includes both group-specific commands such as
  `/group` and `/groupmode`, and generic Feishu commands triggered from a group
  context such as `/status` and `/unsubscribe`
- group commands do not enter the `assistant` context log and do not advance
  the assistant boundary
- commands and settings that mutate shared state remain strictly admin-only,
  including:
  - `/new`
  - `/session`
  - `/resume`
  - `/unsubscribe`
  - `/profile`
  - `/approval`
  - `/sandbox`
  - `/permissions`
  - group activation and group-mode management commands

## 6. Runtime Approval and Supplemental Input

- once a group is activated, ordinary members may handle approval cards or
  supplemental-input cards created by **their own turn**
- admins may always act as the fallback operator for those cards
- ordinary non-admin members may not operate pending requests created by
  someone else's turn
- both "allow once" and "allow for this session" approval actions remain valid
  for the current request actor
- this runtime card ownership only applies to the active turn interaction; it
  does not grant shared-state management rights

## 7. `assistant` Context Contract

- `assistant` writes group messages from users who are currently allowed to use
  the bot into a local log
- when a group is deactivated, ordinary non-admin messages do not enter the
  assistant-mode context log
- only effective human mentions can trigger a reply
- because Feishu does not push other bots' messages to bots in real time,
  `assistant` backfills recent history on every effective mention
- history backfill and live group logs are merged into one context pipeline
- each new effective mention sees two sources of context:
  - local live-log messages after the previous boundary
  - history messages returned by Feishu that are still missing from the local
    log
- the current message that actually triggers backend execution must not be
  flattened back into the history block; it should be sent as a separate
  current-turn block and should prefer `sender_name`
- only when the current sender name cannot be resolved may that current-turn
  block fall back to a short `sender_id` / `open_id` form
- "context" in this contract means text discussion context only; it does not
  include attachment lifecycle state such as whether a file was downloaded,
  remains available, or has already been consumed
- attachment download / availability / consumption state now lives in a
  separate attachment lifecycle rather than being reconstructed from history
  recovery or assistant-mode logs
- main-flow (`chat` container) history recovery is constrained by
  `group_history_fetch_limit` and `group_history_fetch_lookback_seconds`
- main-flow recovery keeps a small backward slack window around the boundary
  timestamp, then dedupes with boundary `message_id`s so messages are not
  missed at the edge of the time window
- `group_history_fetch_limit` and `group_history_fetch_lookback_seconds` also
  act as the global recovery switch; setting either to `0` disables both
  main-flow and thread recovery
- thread (`thread` container) history recovery does not promise a strict
  `group_history_fetch_lookback_seconds` cutoff, because the public Feishu API
  does not support `start_time` / `end_time` for thread containers
- thread recovery prefers `ByCreateTimeDesc` and stops as soon as it crosses
  the stored boundary; it only falls back to ascending scan if descending
  ordering is not usable in practice
- when the missing history exceeds `group_history_fetch_limit`, this contract
  keeps the most recent missing messages rather than the oldest slice
- the context boundary tracks:
  - local log sequence `seq`
  - boundary timestamp `created_at`
  - the set of already-consumed `message_id`s at that timestamp
- tracking boundary `message_id`s prevents same-millisecond missing messages
  from being misclassified as old data on the next trigger
- the implementation guarantees:
  - same-millisecond unconsumed messages are not dropped
  - same-millisecond already-consumed messages are not replayed
- the implementation does not guarantee a fully reconstructed total order for
  different-source messages that share the same millisecond timestamp
- when the trigger happens inside a group thread, execution cards, activation
  denials, and long-text follow-ups should stay in that thread instead of
  jumping back to the main flow

## 8. Denial Feedback

- while a group is deactivated, non-admin users in `assistant` /
  `mention-only` receive a denial message when they explicitly mention the
  trigger target
- while a group is deactivated, non-admin users in `all` are silently ignored
  for plain messages to avoid noise
- while a group is deactivated, non-admin users in `all` still receive a
  denial message when they explicitly mention the trigger target or send a
  group command

## 9. Other Bots and History

- other bots cannot directly trigger `feishu-codex`
- if group history is visible to the bot, messages from other bots can still
  enter the `assistant` context through per-trigger history recovery
- if history recovery is disabled, other bots' messages do not automatically
  enter the `assistant` context

## 10. Explicit Limitations

- thread history recovery cannot enforce the same strict time-window cutoff as
  the main flow; that is a public Feishu API limitation, not an intentional
  product-side relaxation in this repo
- `all` mode is inherently easier to spam; that is product risk, not a runtime
  correctness bug
- group commands and ordinary group messages share one backend session, but
  group commands intentionally do not enter the `assistant` context log
- even if file names or file-like placeholder text appear in group context,
  they must not be interpreted as meaning the corresponding attachment is still
  available; attachment availability is outside the history-recovery contract
- this version intentionally does not implement a fine-grained multi-user
  permission system; group activation is a chat-level shared authorization
  model, not member-level shared-state isolation
