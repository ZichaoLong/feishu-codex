# Feishu Command Matrix

Chinese version: `docs/contracts/feishu-command-matrix.zh-CN.md`

See also:

- `docs/contracts/feishu-help-navigation.md`
- `docs/contracts/thread-profile-semantics.md`
- `docs/contracts/runtime-control-surface.md`
- `docs/contracts/group-chat-contract.md`

This document defines the current first-class Feishu command surface maintained
by this repository.

It answers five questions:

- which Feishu slash commands currently exist
- which commands are reachable from `/help`
- which commands are intentionally text-only
- who may execute them in p2p vs group chats
- which user-visible buttons belong to each command, and whether
  `feishu-codexctl` or `feishu-codex` has a local counterpart

If code and this document disagree, treat that as a contract gap and tighten
the code, the docs, or both.

## 1. Scope

This document covers two surfaces:

- slash commands registered on the Feishu ingress surface
- user-visible card buttons and form actions that directly belong to those
  commands

It does not redefine:

- thread lifecycle
- the low-level state semantics behind `/status`, `/preflight`, and
  `/release-runtime`
- thread semantics for `/threads`, `/resume`, and `/profile`
- upstream Codex commands after entering the `fcodex` TUI

Those remain defined by their dedicated contract docs.

## 2. Reading Convention

### 2.1 “Reachable from `/help`”

“Reachable from `/help`” means:

- the user can enter a topic from the `/help` root card
- then reach the command through buttons, forms, or follow-up state cards

It does not require the command to appear directly on the root card.

### 2.2 Meaning of the permission columns

The p2p / group executor columns follow the current product contract:

- `any user`
  - no admin identity is required in that context
- `admin only`
  - the actor must pass this bot's admin check
- `unsupported`
  - the scope does not allow the command in that context

Important:

- group members being allowed to chat does not mean they may run group slash
  commands
- under the current contract, group slash commands and shared-state settings
  remain admin surfaces by default
- ordinary group members mainly participate through direct prompts and by
  handling approval / supplemental-input cards for their own turns

### 2.3 “Local counterpart”

“Local counterpart” only answers:

- whether `feishu-codexctl` exposes a comparable local inspection / management
  surface
- whether `feishu-codex` exposes a comparable install / service / config
  surface

If the real local counterpart is actually `fcodex`, that is stated explicitly
so it is not confused with those two management CLIs.

## 3. Slash Command Matrix

### 3.1 Navigation, current-chat, and thread

| Command | Purpose | Reachable from `/help` | P2P | Group | User-visible buttons / forms | Local counterpart |
| --- | --- | --- | --- | --- | --- | --- |
| `/help [chat\|group\|thread\|runtime\|identity]` | Show the help navigation card or open a specific topic page | Yes; root entry | admin only | admin only | root entries `Current Chat(chat)`, `Group(group)`, `Thread(thread)`, `Runtime(runtime)`, `Identity(identity)` | `feishu-codex --help` and `feishu-codexctl --help` are only local help, not Feishu `/help` |
| `/h` | Text alias for `/help` | No | admin only | admin only | none | none |
| `/pwd` | Show the current working directory | No | admin only | admin only | none | none |
| `/status` | Show the compact state summary for the current chat binding | Yes; `/help -> chat` | admin only | admin only | none | `feishu-codexctl binding status <binding_id>` exposes a deeper local view; no `feishu-codex` counterpart |
| `/preflight` | Dry-run the next plain message and `/release-runtime` availability | Yes; `/help -> chat` | admin only | admin only | none | no exact one-line local equivalent; `feishu-codexctl binding status <binding_id>` exposes overlapping diagnostics |
| `/cd [path]` | Without args, show cwd; with args, switch cwd and clear the current thread binding | Yes; `/help -> chat` form | admin only | admin only | help form submission | none |
| `/new` | Create a new thread immediately and bind the current chat to it | Yes; `/help -> thread` | admin only | admin only | none | none |
| `/threads` | Show current-directory threads | Yes; `/help -> thread` | admin only | admin only | list-card buttons `Resume/Current`, `Archive`, `More`, `Collapse`, `Expand list` | closest local surface is `feishu-codexctl thread list --scope cwd`; no `feishu-codex` counterpart |
| `/resume <thread_id\|thread_name>` | Resume a specific thread into the current chat | Yes; `/help -> thread` form | admin only | admin only | help form submission; the `/threads` card `Resume` button uses the same semantics | no `feishu-codexctl` / `feishu-codex` counterpart; local continuation uses `fcodex resume <thread_id\|thread_name>` |
| `/profile [name]` | View or change the current bound thread's thread-wise resume profile | Yes; `/help -> thread -> Current Thread` | admin only | admin only | profile-name buttons; when needed, `Apply and reset backend` / `Force apply and reset backend` | no direct `feishu-codexctl` / `feishu-codex` counterpart; the related local path is `fcodex -p <profile>` |
| `/rename <title>` | Rename the current bound thread | Yes; `/help -> thread -> Current Thread` form | admin only | admin only | help form submission | no `feishu-codexctl` / `feishu-codex` counterpart |
| `/archive [thread_id\|thread_name]` | Archive the current thread, or archive a specific target thread | Yes; `/help -> thread -> Current Thread`, and also through `/threads` list cards | admin only | admin only | `/threads` list card `Archive`; the current-thread page may also invoke `/archive` directly | no `feishu-codexctl` / `feishu-codex` counterpart |
| `/release-runtime` | Release Feishu runtime residency for the current bound thread while keeping the binding | No | admin only | admin only | none | `feishu-codexctl thread unsubscribe --thread-id/--thread-name`; no `feishu-codex` counterpart |
| `/cancel` | Stop the current running turn | No | admin only | admin only | the execution card exposes the primary `Cancel run` button | none |

### 3.2 Runtime and identity

| Command | Purpose | Reachable from `/help` | P2P | Group | User-visible buttons / forms | Local counterpart |
| --- | --- | --- | --- | --- | --- | --- |
| `/reset-backend` | Preview and reset the current instance backend | Yes; `/help -> runtime` | admin only | admin only | `Reset backend` or `Force reset backend` | `feishu-codexctl service reset-backend`; no `feishu-codex` counterpart |
| `/permissions [read-only\|default\|full-access]` | Set approval policy and sandbox together | Yes; `/help -> runtime` | admin only | admin only | `read-only`, `default`, `full-access` | none |
| `/approval [untrusted\|on-request\|never]` | Set approval policy only | Yes; `/help -> runtime` | admin only | admin only | `untrusted`, `on-request`, `never` | none |
| `/sandbox [read-only\|workspace-write\|danger-full-access]` | Set sandbox policy only | Yes; `/help -> runtime` | admin only | admin only | `read-only`, `workspace-write`, `danger-full-access` | none |
| `/collab-mode [default\|plan]` | Set the Codex collaboration mode for future turns in the current Feishu binding | Yes; `/help -> runtime` | admin only | admin only | `default`, `plan` | none |
| `/whoami` | Show the user's identity information | Yes; `/help -> identity` | any user | unsupported | none | none |
| `/bot-status` | Show bot identity, configured values, and runtime-discovered values | Yes; `/help -> identity` | any user | admin only | none | none |
| `/init <token>` | Bootstrap admin identity and `bot_open_id` | Yes; `/help -> identity` form | any user | unsupported | help form submission | `feishu-codex config init-token` only reveals the token; it does not execute `/init` |
| `/debug-contact <open_id>` | Troubleshoot contact-name resolution, cache hits, and fallback reasons | No | admin only | unsupported | none | none |

### 3.3 Group-only

| Command | Purpose | Reachable from `/help` | P2P | Group | User-visible buttons / forms | Local counterpart |
| --- | --- | --- | --- | --- | --- | --- |
| `/group` | Show whether the current group is activated | Yes; `/help -> group` | unsupported | admin only | the state card may expose `Activate this group` and `Deactivate this group` | no `feishu-codexctl` / `feishu-codex` counterpart |
| `/group activate` | Activate the current group chat | Yes; `/help -> group -> /group` state card | unsupported | admin only | `/group` card button `Activate this group` | none |
| `/group deactivate` | Deactivate the current group chat | Yes; `/help -> group -> /group` state card | unsupported | admin only | `/group` card button `Deactivate this group` | none |
| `/group-mode` | Show the current group work mode | Yes; `/help -> group` | unsupported | admin only | the state card may expose group-mode switch buttons | none |
| `/group-mode assistant` | Switch to `assistant` | Yes; `/help -> group -> /group-mode` state card | unsupported | admin only | `/group-mode` card button `assistant` | none |
| `/group-mode all` | Switch to `all` | Yes; `/help -> group -> /group-mode` state card | unsupported | admin only | `/group-mode` card button `all` | none |
| `/group-mode mention-only` | Switch to `mention-only` | Yes; `/help -> group -> /group-mode` state card | unsupported | admin only | `/group-mode` card button `mention-only` | none |

## 4. Commands intentionally kept text-only

The following commands are intentionally not required to be pure button-driven
from `/help`:

- `/h`
- `/pwd`
- `/cancel`
- `/release-runtime`
- `/debug-contact <open_id>`

The reasons are:

- `/h` is only an alias for `/help`
- `/pwd` is largely covered by no-argument `/cd`
- `/cancel` already has its primary entry on the execution card
- `/release-runtime` is intentionally weakened in favor of `/profile`
- `/debug-contact` is a troubleshooting surface, not a common navigation topic

## 5. Non-slash but first-class user card surfaces

These actions are not slash commands, but they are formal user-facing Feishu
surfaces and must be maintained together with the slash contract.

| Card surface | Purpose | Who may click | Reachable from `/help` | Local counterpart |
| --- | --- | --- | --- | --- |
| Execution card `Cancel run` | Stop the current turn | p2p: current operator; group: admin or the current turn actor | No | none |
| Command-approval card `Allow once / Allow for session / Deny / Abort turn` | Resolve a command approval request | p2p: current operator; group: admin or that request's current actor | No | none |
| File-change approval card `Allow once / Allow for session / Deny / Abort turn` | Resolve a file-change approval request | p2p: current operator; group: admin or that request's current actor | No | none |
| Extra-permissions approval card `Allow once / Allow for session / Deny` | Resolve a permissions approval request | p2p: current operator; group: admin or that request's current actor | No | none |
| Supplemental-input card `option buttons / custom submit` | Answer a `requestUserInput` question | p2p: current operator; group: admin or that request's current actor | No | none |

## 6. Local command-surface boundary

To avoid conflating Feishu commands with local management surfaces, the current
contract is:

- `feishu-codex`
  - owns installation, service lifecycle, autostart, instance management, and
    config entrypoints
  - it is not the Feishu session / thread operation surface
- `feishu-codexctl`
  - owns local inspection / management for service, binding, and thread state
  - it is not a second Codex frontend
- `fcodex`
  - is the local entrypoint for continuing live threads and entering the Codex
    TUI

So the following expectations are intentionally false:

- `feishu-codex status` is not the same thing as Feishu `/status`
- `feishu-codexctl` is not a local UI-equivalent of Feishu `/threads`
- `feishu-codex` and `feishu-codexctl` do not expose chat-scoped interactive
  commands such as Feishu `/new`, `/rename`, or `/archive`

## 7. Related implementation fact sources

The main implementation fact sources for this document include:

- `bot/codex_handler.py`
- `bot/inbound_surface_controller.py`
- `bot/codex_help_domain.py`
- `bot/codex_threads_ui_domain.py`
- `bot/codex_settings_domain.py`
- `bot/codex_group_domain.py`
- `bot/cards.py`
- `bot/feishu_codexctl.py`
- `bot/manage_cli.py`

If any future change adds, removes, renames, or re-scopes a Feishu command, or
changes `/help` reachability, group-admin boundaries, button permissions, or
the local CLI correspondence, this document must be updated together with that
change.
