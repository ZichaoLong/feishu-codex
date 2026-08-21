# Feishu `/help` Navigation Contract

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/feishu-help-navigation.zh-CN.md`.

This file defines only the navigation contract for `/help` and `/commands`.

## 1. Home goal

`/help` is not a full documentation site and not a flat dump of every command.

Its job is:

1. show a compact current-state summary
2. route the user into fixed workspaces
3. keep low-frequency actions on lower-level pages or result cards

## 2. Fixed home workspaces

The home must expose exactly these six workspaces:

- `Start`
- `Thread Settings`
- `Turn Settings`
- `Connection Status`
- `Group Settings`
- `More`

The home summary must include at least:

- current working directory
- current thread
- current push state
- current turn-setting summary

## 3. Page contracts

### 3.1 Start

Owns:

- `/new`
- `/threads`
- `/resume`
- `/cd`

Its body should remind the user that:

- the same thread may be observed from multiple endpoints. Feishu
  next-turn/FIFO and exclusive actions such as review/compact remain serialized
  by the exact main-turn lease; ordinary Web/`fcodex` prompts keep upstream-routed
  start-or-steer and acquire no writer. A live `fcodex` endpoint attached to the
  exact direct root, or a connected trusted-local Web document that has
  materialized the root, may steer its exact current regular turn. Either may
  interrupt its exact current/startup turn under the [canonical main-turn owner
  contract](root-operation-owner.md), without takeover or writer transfer. Only
  an exact pending-request capability may answer an interaction
- local access to the same live thread uses `focus resume <thread_id|thread_name>`
  or `fcodex resume <thread_id|thread_name>`. A resume that may start autonomous
  work and an exclusive action still pass blank-submission / active-main-turn
  admission; ordinary input and canonical steer/interrupt use their own
  effect-specific boundaries. None grants takeover

### 3.2 Thread Settings

Owns:

- `/goal`
- `/compact`
- `/archive`
- the rename form

Its body should remind the user that:

- thread creation, resume, and browsing belong to `Start`
- there is no longer a project-owned profile or thread-memory control surface here

### 3.3 Turn Settings

Owns:

- `/permissions`
- `/model`
- `/effort`
- `/approval`
- `/last text`

Its body should remind the user that:

- these settings affect future turns of the current Feishu binding
- `/permissions` is the recommended first entry

### 3.4 Connection Status

Owns:

- `/status`
- `/preflight`
- `/detach`
- `/attach`
- related attach subpages

### 3.5 Group Settings

Owns:

- `/group`
- `/group activate`
- `/group deactivate`
- `/group-mode`

### 3.6 More

Owns:

- `/commands`
- `/whoami`
- `/bot-status`
- `/init`
- `/reset-backend`
- `/debug-contact`

## 4. Back-button rules

- first-level workspace pages must expose `Back Home`
- lower-level pages must expose only `Back`
- command or result cards opened from `/help` must still provide a way back to
  the help home page, even after follow-up card actions or form submits
- every back button occupies its own row

## 5. Compatibility entries
