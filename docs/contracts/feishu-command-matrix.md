# Feishu Command Matrix

Chinese original: `docs/contracts/feishu-command-matrix.zh-CN.md`

This file is the source-of-truth matrix for the Feishu command surface.

It answers four questions only:

- which slash commands are formally supported
- which commands are reachable from the `/help` workbench
- who may execute them in P2P and group chats
- which local surface is the closest counterpart

It does not redefine:

- binding / attach / detach / backend state semantics
- fact sources or effectivity boundaries of the remaining setting family
- the full group-chat contract for `assistant / mention-only / all`

## 1. Reading Rules

- “Reachable from `/help`” means reachable through workbench buttons, forms, or follow-up result cards.
- “Admin only” means the current bot's admin check must pass.
- “Anyone” only means no admin check in that scope; it does not mean all group members may mutate shared state.

## 2. Commands

### 2.1 Navigation, Start, Thread Settings, and Connection Status

| Command | Purpose | Reachable from `/help` | P2P | Group | Closest local counterpart |
| --- | --- | --- | --- | --- | --- |
| `/help [overview\|start\|thread-settings\|turn\|connection\|group\|more]` | Open the workbench or a direct workspace page; legacy aliases `chat/thread/runtime/identity` remain compatible | yes | admin only | admin only | none |
| `/commands` | Show a plain-text list of common commands | yes; `More` page | admin only | admin only | none |
| `/h` | Alias for `/help` | no | admin only | admin only | none |
| `/pwd` | Show current working directory | no | admin only | admin only | none |
| `/status` | Show current chat directory, current thread, and current-session settings summary | yes; `Connection Status` page | admin only | admin only | `focusctl binding status <binding_id>` for deeper diagnostics |
| `/preflight` | Dry-run the next plain message and current-chat detach availability | yes; `Connection Status` page | admin only | admin only | partly overlaps `focusctl binding status <binding_id>` |
| `/cd [path]` | Show or switch current directory, with `~` expanded to the user home; switching clears the current thread binding | yes; `Start` form | admin only | admin only | none |
| `/new` | Create a new current thread | yes; `Start` page | admin only | admin only | none |
| `/threads` | Browse threads in the current directory | yes; `Start` page | admin only | admin only | `focusctl thread list --scope cwd` |
| `/resume <thread_id\|thread_name>` | Resume a target thread into the current chat | yes; `Start` form | admin only | admin only | use `focus resume <thread_id\|thread_name>` or `fcodex resume <thread_id\|thread_name>` for local live-thread continuation |
| `/goal [show\|text\|set <objective>\|pause\|resume\|clear]` | Show or manage the current thread's goal; `text` exports plain text | yes; `Thread Settings` page | admin only | admin only | none |
| `/compact` | Compact the current bound thread's context history | yes; `Thread Settings` page | admin only | admin only | no direct local equivalent |
| `/rename <title>` | Rename the current thread | yes; `Thread Settings` form | admin only | admin only | none |
| `/archive [thread_id\|thread_name]` | Archive the current thread, or archive an explicit target | yes; `Thread Settings` button or form | admin only | admin only | `focusctl thread archive --thread-id/--thread-name`; local CLI additionally supports repeated `--thread-id` for batch archive |
| `/detach` | Stop the current chat from receiving Feishu push for the current thread while keeping the binding record | yes; dynamic button on `Connection Status` | admin only | admin only | `focusctl binding detach <binding_id>`; thread scope is `focusctl thread detach ...` |
| `/attach [binding\|thread\|service]` | Restore Feishu push for the current chat, current thread, or current instance | yes; `Connection Status` and its lower-level page, and also contextual result cards | admin only | admin only | `focusctl binding/thread/service attach ...` |
| `/cancel` | Cancel the current execution | no; primary entry is the execution-card button | admin only | admin only | none |

### 2.2 Turn Settings and More

| Command | Purpose | Reachable from `/help` | P2P | Group | Closest local counterpart |
| --- | --- | --- | --- | --- | --- |
| `/permissions [read-only\|workspace\|danger-full-access]` | Set the permission baseline independently from approval policy | yes; `Turn Settings` page | admin only | admin only | none |
| `/model [name\|auto]` | Set the current Feishu session's turn-time model override; no-arg opens the shared model/effort card | yes; `Turn Settings` page | admin only | admin only | none |
| `/effort [auto\|none\|minimal\|low\|medium\|high\|xhigh]` | Set the current Feishu session's turn-time effort override; no-arg opens the shared model/effort card | yes; `Turn Settings` page | admin only | admin only | none |
| `/approval [untrusted\|on-request\|never]` | Set approval policy | yes; `Turn Settings` page | admin only | admin only | none |
| `/last text` | Export the latest authoritative terminal text from the current session; prefers terminal result, falls back to the latest execution card | yes; `Turn Settings` page | admin only | admin only | none |
| `/reset-backend` | Preview and reset the current instance backend for recovery | yes; `More -> Advanced Actions` | admin only | admin only | `focusctl service reset-backend` |
| `/whoami` | Show the caller's identity | yes; `More` page | anyone | unsupported | none |
| `/bot-status` | Show bot identity and config probe results | yes; `More` page | anyone | admin only | none |
| `/init <token>` | Initialize admins and `bot_open_id` | yes; `More` form | anyone | unsupported | `focusctl config init-token` only shows the token |
| `/debug-contact <open_id>` | Debug contact-name resolution | yes; `More -> Advanced Actions` form | admin only | unsupported | none |

### 2.3 Group Settings

| Command | Purpose | Reachable from `/help` | P2P | Group | Closest local counterpart |
| --- | --- | --- | --- | --- | --- |
| `/group` | Show whether the current group is activated | yes; `Group Settings` page | unsupported | admin only | none |
| `/group activate` | Activate the current group | yes; `Group Settings` page | unsupported | admin only | none |
| `/group deactivate` | Deactivate the current group | yes; `Group Settings` page | unsupported | admin only | none |
| `/group-mode` | Show the current group working mode | yes; `Group Settings` page | unsupported | admin only | none |
| `/group-mode assistant` | Switch to `assistant` | yes; `Group Settings` page | unsupported | admin only | none |
| `/group-mode mention-only` | Switch to `mention-only` | yes; `Group Settings` page | unsupported | admin only | none |
| `/group-mode all` | Switch to `all` | yes; `Group Settings` page | unsupported | admin only | none |

## 3. Commands Intentionally Not on the Workbench Home

These remain alias, plain-text, or result-card entry points rather than fixed home-card buttons:

- `/h`
- `/pwd`
- `/cancel`

Additional notes:

- `/commands` is now reachable from `More`
- `/attach` is now reachable from `Connection Status`
- `/debug-contact` is now reachable from `More -> Advanced Actions`

## 4. Result-card Buttons

These are part of the formal Feishu user surface and must be maintained together with slash commands:

- execution card: `Cancel Execution`
- `/threads` list card: `Resume/Current`, `Archive`, `More`, `Collapse`
- `/goal` card: `Refresh`, `Pause` / `Resume`, `Clear`
- `/reset-backend` result cards: attach/detach follow-up buttons such as `Attach Current Thread`, `Attach Current Instance`, and `Keep Detached`
- shared `/model` / `/effort` card, plus `/permissions` / `/approval` cards: turn-time runtime-setting toggle buttons
- approval / extra-input cards: their request-type-specific allow / deny / submit buttons

## 5. Boundary

- FOCUS owns install, instance, and service lifecycle. It is not the Feishu chat command surface.
- `focusctl` owns local inspection and management of bindings, threads, and services. It is not a second frontend.
- `focus` / `fcodex` are the local live-thread continuation entry points.

If any Feishu command is added, removed, renamed, or moved in `/help`, or if button entry points or permission boundaries change, this file must be updated with the code.
