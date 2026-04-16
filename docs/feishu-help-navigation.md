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

The root help card must expose exactly three top-level navigation choices:

- `session`
- `settings`
- `group`

The root card may include short explanatory text for each choice, but it should not try to list every command inline.

Local `fcodex` usage is not part of the Feishu `/help` surface.

## 3. Navigation Reachability

“Reachable from `/help`” means reachable through one or more card buttons after entering `/help`.

It does not require every command to appear on the root card.

Multi-level navigation is preferred when it reduces clutter and clarifies responsibility.

## 4. Semantic Equivalence Rule

Help buttons and forms may differ from slash commands in presentation, but not in behavior.

Therefore:

- a help button that triggers a command must reuse the same command semantics as the slash command
- a help form may only collect missing arguments, then dispatch into the same command path
- help navigation must not introduce a second copy of command business logic

Different response shape is allowed:

- slash commands may reply with a new message
- card actions may update the current card or show a toast

But the underlying operation, validation, scope guard, and state transition must remain equivalent.

## 5. Session Surface

The `session` branch of `/help` should cover thread and working-directory operations.

It must make the following capabilities reachable:

- `/session`
- `/new`
- `/resume <thread_id|thread_name>` via a form
- `/cd <path>` via a form
- a current-thread page for current binding operations

The current-thread page should cover:

- `/status`
- `/release-feishu-runtime`
- `/rename <title>` for the currently bound thread, via a form
- `/rm` for the currently bound thread

The help surface does not need a global thread browser or a global archive form.

The existing `/session` card remains the current-directory thread browser and archive/resume surface for listed threads.

## 6. Settings Surface

The `settings` branch of `/help` should cover default profile and per-binding runtime settings.

It must make the following capabilities reachable:

- `/profile`
- `/permissions`
- `/approval`
- `/sandbox`
- `/mode`

It should also expose an identity/admin subpage that makes the following reachable:

- `/whoami`
- `/whoareyou`
- `/init <token>` via a form

## 7. Group Surface

The `group` branch of `/help` should cover group-only operating rules.

It must make the following capability reachable:

- `/groupmode`

`/acl` should be documented there as text guidance, with example slash usage, but it is intentionally not required to be button- or form-driven from `/help`.

Reason:

- `grant` and `revoke` commonly require mentions
- one action may target multiple users
- slash syntax is clearer than forcing an incomplete form model

## 8. Commands Intentionally Excluded From `/help` Navigation

The following are intentionally not required to be navigation-reachable from Feishu `/help`:

- `/h`
- `/cancel`
- `/pwd`
- `fcodex` local-wrapper commands

Specific rationale:

- `/cancel` already has a primary action on the execution card
- `/pwd` is effectively subsumed by `/cd` with no argument
- local wrapper usage belongs to local help, not Feishu help

## 9. Guard Semantics

Help-triggered command execution must preserve the same access rules as slash commands.

That includes:

- private-chat-only commands
- group-only commands
- group admin restrictions

If a slash command would be rejected in the current scope, the same operation triggered from `/help` must also be rejected.

## 10. Cross-Reference

Related contracts:

- `docs/session-profile-semantics.md`
- `docs/runtime-control-surface.md`
- `docs/feishu-thread-lifecycle.md`
