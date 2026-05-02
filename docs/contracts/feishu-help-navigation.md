# Feishu Help Navigation Contract

This document defines the Feishu-side `/help` navigation surface.

It is the contract for:

- which commands are reachable from `/help`
- which commands are intentionally not reachable from `/help`
- how button and form navigation must relate to slash-command semantics

If implementation and this document disagree, treat that as a contract gap and tighten the implementation, the docs, or both.

## 1. Scope

This document only covers the Feishu-side help and navigation surface.

It does not redefine:

- thread lifecycle
- runtime control semantics
- session/profile semantics
- `fcodex` local-wrapper help

Those belong to their dedicated docs.

## 2. Root Structure

Feishu `/help` is a navigation entry, not a flat command dump.

The root help card must expose exactly five top-level entries, in this order:

- `Current Chat`, text topic `chat`
- `Group`, text topic `group`
- `Thread`, text topic `thread`
- `Runtime`, text topic `runtime`
- `Identity`, text topic `identity`

The button labels may be localized, but the textual `/help <topic>` contract
must stay explicit and stable.

The root card may include short explanatory text for these entries, but it
should not try to list every command inline.

Local `fcodex` usage is not a standalone Feishu `/help` page. If it appears at
all, it should only appear as brief text guidance on the overview or thread
pages.

## 3. Navigation Reachability

“Reachable from `/help`” means reachable through one or more card buttons after
entering `/help`.

It does not require every command to appear on the root card.

Multi-level navigation is preferred when it reduces clutter and clarifies
responsibility.

## 4. Semantic Equivalence Rule

Help buttons and forms may differ from slash commands in presentation, but not
in behavior.

Therefore:

- a help button that triggers a command must reuse the same command semantics as the slash command
- a help form may only collect missing arguments, then dispatch into the same command path
- help navigation must not introduce a second copy of command business logic

Different response shape is allowed:

- slash commands may reply with a new message
- card actions may update the current card or show a toast

But the underlying operation, validation, scope guard, and state transition
must remain equivalent.

Help/navigation card payloads must also stay minimal and explicit:

- routing is keyed by `action`
- payloads should only carry the parameters the target action actually consumes
- `plugin`, bot keyword, or other deployment-identifying fields are not part of the callback contract and must not be required for routing

## 5. Current-Chat Surface

The `chat` branch of `/help` owns **current chat binding** state and working-directory control.

It must make the following capabilities reachable:

- `/status`
- `/preflight`
- `/cd <path>` via a form

This branch may link onward to the thread surface, but it does not own thread
management semantics.

`/status` and `/preflight` remain chat-scoped commands:

- even in group chats, they still describe the current chat binding
- they are not a global thread-admin surface

## 6. Group Surface

The `group` branch of `/help` owns group-only operating rules and controls.

It must make the following capabilities reachable:

- `/group`
- `/groupmode`

The page text should cover:

- that groups start deactivated
- what `/group activate` and `/group deactivate` do
- the three group modes `assistant`, `mention-only`, and `all`
- the permission boundary between daily group usage, shared-state management,
  and approval-card handling

If the implementation keeps follow-up buttons on the `/group` and `/groupmode`
state cards, then `/group activate`, `/group deactivate`, and
`/groupmode <mode>` are also considered reachable from `/help`, even though
they are not flattened onto the help page itself.

## 7. Thread Surface

The `thread` branch of `/help` owns thread browsing, creation, resumption, and
current-thread management.

It must make the following capabilities reachable:

- `/session`
- `/new`
- `/resume <thread_id|thread_name>` via a form
- a current-thread page for the currently bound thread

The current-thread page should cover:

- `/profile [name]`
- `/rename <title>` for the current thread, via a form
- `/rm` for the current thread

That current-thread page is still an entry for the **currently bound thread**,
not a global thread-admin surface.

The existing `/session` card remains the current-directory thread browser and
archive/resume surface for listed threads.

`/unsubscribe` is intentionally not a first-class help-navigation capability:

- the main user-facing re-profile path should flow through `/profile [name]`
- if needed, help text may point users to `feishu-codexctl` for local
  troubleshooting, but no dedicated help button is required

## 8. Runtime Surface

The `runtime` branch of `/help` owns per-Feishu-binding runtime settings and
instance-level backend control.

It must make the following capabilities reachable:

- `/permissions`
- `/approval`
- `/sandbox`
- `/mode`
- `/reset-backend`

`/profile` does not belong here. It is a property of the current thread and
must remain under `Thread -> Current Thread`.

## 9. Identity Surface

The `identity` branch of `/help` owns identity and bootstrap.

It must make the following capabilities reachable:

- `/whoami`
- `/whoareyou`
- `/init <token>` via a form

`/debug-contact <open_id>` is not part of the normal help navigation surface
and is not required to be reachable from `/help`.

## 10. Commands Intentionally Excluded From `/help` Navigation

The following are intentionally not required to be navigation-reachable from
Feishu `/help`:

- `/h`
- `/cancel`
- `/pwd`
- `/unsubscribe`
- `/debug-contact <open_id>`
- `fcodex` local-wrapper commands

Specific rationale:

- `/h` is only an alias for `/help`
- `/cancel` already has a primary action on the execution card
- `/pwd` is effectively subsumed by `/cd` with no argument
- `/unsubscribe` is intentionally weakened in favor of `/profile`
- `/debug-contact` is a troubleshooting surface, not a normal navigation topic
- local wrapper usage belongs to local help, not Feishu help

## 11. Guard Semantics

Help-triggered command execution must preserve the same access rules as slash commands.

That includes:

- private-chat-only commands
- group-only commands
- group admin restrictions
- ordinary non-admin private chats remaining denied by default
- `/whoami`, `/whoareyou`, and `/init <token>` remaining directly reachable in private chat as identity/bootstrap commands, rather than being swallowed by a generic "admin private chat only" guard first

If a slash command would be rejected in the current scope, the same operation
triggered from `/help` must also be rejected.

## 12. Cross-Reference

Related contracts:

- `docs/contracts/session-profile-semantics.md`
- `docs/contracts/runtime-control-surface.md`
- `docs/contracts/feishu-thread-lifecycle.md`
