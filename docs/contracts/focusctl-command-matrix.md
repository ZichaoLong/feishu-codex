# `focusctl` Command Matrix

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/focusctl-command-matrix.zh-CN.md`.

This file defines the formal local `focusctl` management surface.

It answers:

- which resources `focusctl` owns
- which commands are read-only vs mutating
- how thread targets are selected
- how it maps to the Feishu command surface

## 1. Core Positioning

- `focusctl` is the local FOCUS management surface. It is not a second Codex frontend.
- Use `focus` or `fcodex` when you want to continue a live thread locally.
- Use `focusctl` for install, repair, service lifecycle, instance management, local service / binding / thread status, and local thread-scoped management.

## 2. Instance and Target Resolution

- Instance-directory commands such as `instance ...` are global; other instance-scoped commands accept `--instance <name>`.
- An explicit `--instance` always wins.
- A named instance used here must already have been created via
  `focusctl instance create <name>`; `focusctl` never creates it
  implicitly.
- Otherwise resolution follows `preferred-running -> unique-running -> default-running -> current-instance-paths`.
- Resolving an instance name or its local paths is not backend-liveness proof.
  `thread list` and every `--thread-name` selector require the target Focus
  instance to be running and to publish an endpoint for its current owned
  backend generation. A stopped instance, or a running entry without that
  publication, fails closed before an upstream query or mutation is sent.
  `codex.yaml.app_server_url` and a URL cached in the instance registry are
  never dialed as fallback liveness evidence; they could now identify an
  unrelated process occupying the same loopback port. The live control plane
  is the sole dialable endpoint source: `service/status` publishes a URL only
  while the exact current websocket generation is protocol-READY and its
  owned guardian is still live. `app_server_runtime.json` proves lifecycle and
  cleanup authority, not protocol readiness, and is never an endpoint
  fallback. ID-only offline behavior exists only where the individual command
  contract below says so.
- `thread status`, `thread bindings`, `thread goal`, `thread attach`, and `thread detach` require exactly one of:
  - `--thread-id <id>`
  - `--thread-name <name>`
- `thread clear-archived-bindings` requires exactly one of `--thread-id <id>` or `--all`; it does not accept `--thread-name`, so local binding deletion does not depend on another upstream thread-name resolution step.
  - `--thread-id` deletes local bindings that point at the given thread id without validating upstream archived state.
  - `--all` first queries upstream archived threads through a running instance, then deletes matching local bindings; without an available running instance it fails closed and does not mutate local data.
- `thread archive` supports two target forms:
  - single-thread: `--thread-name <name>` or `--thread-id <id>`
  - batch: repeat `--thread-id <id>`; each target thread is routed, archived, and locally cleaned up independently using the existing single-thread archive semantics
- `thread unarchive` is ID-only and accepts repeated `--thread-id <id>` options for batch restore.
- `thread delete` is ID-only and accepts exactly one `--thread-id <id>`.
- `focusctl` is not a child-administration escape hatch. Before mutating an
  explicit thread target, `thread goal`, `archive`, `unarchive`, `delete`,
  `attach`, and `detach` authoritatively require a direct non-`ThreadSpawn`
  root target. A direct parent-owned `ThreadSpawn` child is rejected
  fail-closed; `--force` never changes that rule.
- `focusctl` is not a main-turn writer bypass. A control mutation that could
  start work must reject while an exact submission/active-turn lease exists,
  and an identity-less local caller cannot start an autonomous goal turn
  because it has no interaction-delivery path.
- The identity-less maintenance exception is narrow: when no conflicting
  submission/active-turn lease exists and that fact is reliably checked,
  `thread goal set --status paused` (optionally with an objective) and
  `thread goal clear` may run. They create no writer, resume no thread,
  subscribe to no interaction, and start no turn. A conflicting lease or an
  uncertain check is rejected. `active`, omitted-status goal sets, and other
  potentially continuing mutations require a connected Web, Feishu, or
  `fcodex` frontend that can acquire the blank submission lease.
- An attach that changes a detached Feishu binding to `attached` is not only a
  local push-state change: it may call upstream `thread/resume` to establish
  the service's app-server subscription. Before sending, Focus checks the
  exact main-turn lease and whether a persisted goal could continue
  autonomously. An already-attached target is an observer-side no-op: it sends
  no resume and needs no submission admission.
- `focusctl service|binding|thread attach` is identity-less local control. It
  cannot impersonate the Feishu binding named by its target, and the
  external-control gate rejects a conflicting submission/active-turn lease or
  a resume that may autonomously start work without a delivery owner. A Feishu
  `/attach` command may use only its *current invoking binding* to acquire the
  exact blank lease when needed. `service attach` checks each candidate root
  independently rather than letting one allowed root authorize another.

## 3. Resource Layers

`focusctl` is split into these resource groups:

- `config`
- `instance`
- `service`
- `binding`
- `prompt`
- `thread`
- `image`
- `web`
- `skill`
- `migrate`
- `uninstall`
- `purge`

Important mental model:

- `binding` is the chat-scoped view
- `thread` is the thread-scoped view

Do not conflate them.

## 4. Commands

### 4.1 `migrate`

| Command | Purpose | Type | Feishu counterpart |
| --- | --- | --- | --- |
| `focusctl migrate from-feishu-codex` | One-shot transfer from the old `feishu-codex` local install to FOCUS | mutating | none |

Contract:

- This is the only supported old-name migration entry. The normal FOCUS path does not read old `feishu-codex` paths, env files, wrappers, completions, services, or data roots.
- This migration entry resolves the old install through the legacy `FC_*` path environment variables when they are set, including `FC_CONFIG_ROOT`, `FC_DATA_ROOT`, `FC_ENV_FILE`, `FC_BIN_DIR`, and shell completion path overrides. These variables are not runtime FOCUS fallbacks.
- The migration is a transfer, not a compatibility fallback. After success, FOCUS owns the active install surface and local persistent state.
- It stops and disables old `feishu-codex` services before copying local state, then refreshes the new FOCUS wrappers, completions, and service definitions.
- The target FOCUS output paths, including config/data roots, env file, wrapper directory, completion files, shell profile hooks, and service definition directories, must not overlap old `feishu-codex` config/data/scheduled roots. Migration fails during preflight if they do, because old roots are archived after the new surface is refreshed.
- It migrates configuration and non-runtime persistent local state:
  - `system.yaml`, `codex.yaml`, `init.token`
  - `feishu-codex.env` renamed to `focus.env`, including named-instance env files
  - bindings, configured settings, terminal result raw store, group state/logs, and other non-runtime local stores
  - Linux scheduled prompt timers, transferring `feishu-codex-scheduled-*` to `focus-scheduled-*`
- Scheduled prompt text gets only safe textual rewrites such as `feishu-codexctl` to `focusctl`; prompts that still contain concrete old helper paths or old roots are reported as migration warnings for manual inspection.
- It does not migrate runtime state:
  - PID / process state
  - service lease files
  - instance registry
  - thread runtime leases
  - interaction leases
  - backend URL discovery
  - websocket / capability tokens
  - running turns or in-memory queues
  - managed virtualenvs and logs
- It creates a backup under `~/.local/share/focus/migration-backups/feishu-codex-.../` or the platform-equivalent FOCUS data root. This backup is not a runtime fallback path.
- It is fail-closed. If the target FOCUS data/config roots already contain non-install generated state, migration stops rather than merging two active facts. If a critical stage fails, old install files are not deleted.

`bash install.sh --migrate-from-feishu-codex` installs the new FOCUS package and then invokes this same migration implementation.

### 4.2 `instance`

| Command | Purpose | Type | Feishu counterpart |
| --- | --- | --- | --- |
| `focusctl instance create <name>` | Create a named instance and prepare its config/data directories and service definition | mutating | none |
| `focusctl instance list` | List known local instances, service state, runtime availability, app-server summary, and local directories | read-only | none |
| `focusctl instance remove <name>` | Remove a named instance and its instance-level service registration material; cannot remove `default` | mutating | none |

### 4.3 `service`

| Command | Purpose | Type | Feishu counterpart |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] service start` | Start the target instance background service | mutating | none |
| `focusctl [--instance <name>] service stop` | Stop the target instance background service | mutating | none |
| `focusctl [--instance <name>] service restart` | Restart the target instance background service | mutating | none |
| `focusctl [--instance <name>] service status` | Show the target instance service-manager state and, when the service is running, a best-effort runtime summary | read-only | no exact single command |
| `focusctl [--instance <name>] service autostart enable\|disable\|status` | Manage login-time autostart for the target instance | mutating / read-only | none |
| `focusctl [--instance <name>] service log [--lines <n>]` | Tail the target instance log | read-only | none |
| `focusctl [--instance <name>] service reset-backend [--force]` | Reset the current instance backend for recovery without restarting the FOCUS service | mutating | Feishu `/reset-backend` |
| `focusctl [--instance <name>] service attach` | Try to restore detached Feishu push in the current instance; each changed binding may issue `thread/resume` and is checked per root | mutating | Feishu `/attach service`, and the post-reset `Attach Current Instance` button |

`service status` is the service-manager view. It may be repeated with
multiple `--instance` values. When the platform service is running, the command
also attempts a best-effort FOCUS runtime query and prints either
`runtime: available` with control-plane / app-server / Web Gateway / binding / thread
diagnostics or `runtime: unavailable` with the reason. A running service with
unavailable runtime means the platform service manager sees a live process, but
the FOCUS control plane is not reachable; the command does not rewrite that as
`service: stopped`.
The Web Gateway line projects only the loopback listen origin currently
published by the live runtime. It prints `disabled` when configuration disables
Web, and `unavailable` when Web is enabled without an active endpoint or the
runtime itself cannot be read. It does not expose the bootstrap credential or
the configured trusted-proxy external origin.

`service reset-backend` requires that live control plane. Its `--force` accepts
interruption risk inside the current running backend generation; it cannot start a
service blocked by an unresolved `app_server_runtime.json`, retire that record, or
manufacture a missing cleanup receipt. On the Focus-owned control wire and Feishu
card action, an absent `force` means `false`; a present value must be an exact JSON
boolean. Truthy strings, integers, `null`, arrays, and objects are rejected before
any reset effect.

When the control response reports `ok: true`, `service reset-backend` validates
the complete seven-field reset result before printing `backend reset: ok`. A
missing, extra, malformed, or request-mismatched field means the mutation
outcome is unknown: the command prints no success output, exits `3`, and does
not retry. Use `focusctl --instance <same-name> service status` to inspect the
same target instance before deciding what to do; that inspection does not
reconcile or prove the previous request outcome.

`service list` is intentionally not a command. Use `focusctl instance list` for
the machine-wide instance overview.

### 4.4 `binding`

| Command | Purpose | Type | Feishu counterpart |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] binding list [--refresh-names]` | List bindings visible in the target instance, including cached chat display names and the authoritative raw thread name when present; `--refresh-names` explicitly refreshes chat display-name caches | read-only by default; `--refresh-names` may call Feishu/contact APIs and update local in-memory name caches | none |
| `focusctl [--instance <name>] binding status <binding_id>` | Show one binding's chat, thread, push state, next-prompt status, current-instance interaction-owner/lease diagnostic (a derived local check, not active-main-turn authority), and session settings | read-only | lower-level diagnostics behind Feishu `/status` and `/preflight` |
| `focusctl [--instance <name>] binding attach <binding_id>` | Restore Feishu push for one binding; changing detached to attached may issue `thread/resume` | mutating | Feishu `/attach binding` |
| `focusctl [--instance <name>] binding detach <binding_id>` | Pause Feishu push for one binding while keeping the binding record | mutating | binding-scoped counterpart of Feishu `/detach` |
| `focusctl [--instance <name>] binding clear <binding_id>` | Delete one local binding record | mutating | none |
| `focusctl [--instance <name>] binding clear-all` | Delete all local binding records in the target instance | mutating | none |
| `focusctl [--instance <name>] binding clear-stale [--dry-run]` | Delete stale binding records that point at threads that can no longer be verified as recoverable; by default scans all running instances and known stopped instances, while explicit `--instance` limits the action to that instance | mutating | none; this is a local binding-record repair / ops entry |

`binding clear` / `clear-all` / `clear-stale` are not `detach`:

- `clear` deletes the local binding record, including its thread pointer and
  binding-local settings
- `detach` removes the current Feishu push attachment

`binding list` is a compact inventory view. By default, its `CHAT` column uses
only local display-name caches and falls back to a short id on cache miss. It
does not call Feishu/contact APIs during the default inventory path. If any
`CHAT` value is missing, the CLI prints a note suggesting
`focusctl binding list --refresh-names`.

`--refresh-names` explicitly refreshes those caches. It deduplicates by
chat/user target inside one request, then calls the relevant Feishu/contact API
best-effort. Group chat names require the Feishu app to have
`im:chat:readonly` or an equivalent chat-read scope; without it, group bindings
intentionally show the short chat id.

For CLI waiting behavior, a refresh target is one unique external lookup:
`group:<chat_id>` for a group binding or `p2p:<sender_id>` for a direct-chat
binding. Multiple bindings that share the same target are counted once. Before
issuing the refresh request, the CLI takes a cache-only snapshot to estimate
that target count, then sets the control-plane wait budget from the configured
Feishu `request_timeout_seconds` plus a small local margin.

Its `THREAD` column shows a short thread id plus the raw app-server
`thread.name` when present; it does not fall back to binding-local
`current_thread_title` or thread preview text.

`binding clear-stale` is retain-oriented. Its source of truth is a cleanup-specific thread operability check, not the generic status display path:

- it first verifies each bound `current_thread_id` through a running app-server with a metadata-only `thread/read` presence check; it does not load full turns/history
- threads with successful metadata-only reads are retained; a readable thread whose generic status is `notLoaded` is not stale
- explicitly unreadable threads, threads that are not loaded and have no persisted metadata, and metadata-only threads that can no longer be recovered are stale, and their local binding records are deleted
- query failures, timeouts, protocol errors, and ambiguous states fail closed: the binding is retained and reported as unknown
- running instances are cleaned through their service control plane; known stopped instances are cleaned through this project's binding store API
- precise archived-thread cleanup is handled by `thread clear-archived-bindings`; `binding clear-stale` does not treat unstable path strings as an archived-state source of truth

### 4.5 `prompt`

| Command | Purpose | Type | Feishu counterpart |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] prompt send --binding-id <binding_id> (--text <text> \| --text-file <file>) [--synthetic-source <label>] [--display-mode silent\|announce]` | Use the target instance control plane to synthetically start one new prompt turn on a binding | mutating | none; this is the local control-plane synthetic prompt entry |

Notes:

- `prompt send` is **binding-scoped**, not thread-scoped.
- Actual execution still goes through the normal running-turn / attach / interaction protections inside the service.
- When the target binding is not writable, the command must fail closed with a refusal reason instead of silently queueing work.

### 4.6 `thread`

| Command | Purpose | Type | Feishu counterpart |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] thread list [--scope cwd\|global] [--cwd <path>] [--archived]` | Browse persisted threads; default is current-directory scope, while `--archived` browses archived threads | read-only | target-discovery counterpart of Feishu `/threads`; Feishu currently has no archived inventory |
| `focusctl [--instance <name>] thread status (--thread-id <id> \| --thread-name <name>)` | Show backend status, live runtime owner / holders, and bound / attached / detached bindings for one thread | read-only | no exact single command |
| `focusctl [--instance <name>] thread bindings (--thread-id <id> \| --thread-name <name>)` | Show all bindings currently pointing at one thread | read-only | none |
| `focusctl [--instance <name>] thread goal (--thread-id <id> \| --thread-name <name>)` | Show the current goal for one thread; this is the default show form | read-only | Feishu `/goal` |
| `focusctl [--instance <name>] thread goal set (--thread-id <id> \| --thread-name <name>) [--objective <text>] [--status active\|paused]` | At least one of `--objective` or `--status` is required. An `active` or omitted-status result can start an idle loaded turn, so identity-less control rejects it. Only explicit `--status paused` (optionally with an objective) may use the idle, non-continuing maintenance exception after exact lease checks. | mutating | Feishu `/goal set <objective>` is binding-aware; there is no raw local equivalent for execution-starting `active` |
| `focusctl [--instance <name>] thread goal clear (--thread-id <id> \| --thread-name <name>)` | Clear a goal only after exact direct-target and submission/active-turn lease checks; this is a non-continuing identity-less maintenance mutation. | mutating | Feishu `/goal clear` |
| `focusctl [--instance <name>] thread archive (--thread-id <id> [--thread-id <id> ...] \| --thread-name <name>)` | Archive one or more target threads; after a successful archive, clear local bindings that still point to it in the target instance, other reachable running instances, and known stopped instances | mutating | local operational counterpart of Feishu `/archive`; batch and cross-instance local binding cleanup are local-CLI only |
| `focusctl [--instance <name>] thread unarchive --thread-id <id> [--thread-id <id> ...]` | Ask upstream, one target at a time, to restore one or more archived threads; reject targets still referenced by known local Focus bindings, and do not create a binding after success | mutating | none |
| `focusctl [--instance <name>] thread delete --thread-id <id> [--force]` | Ask upstream to permanently delete the root thread; upstream may cascade to spawned descendants, and Focus does not promise a complete preview of that set | destructive | none |
| `focusctl [--instance <name>] thread clear-archived-bindings (--thread-id <id> \| --all) [--dry-run]` | Delete local binding records left behind for archived threads; does not call upstream archive; `--thread-id` deletes bindings pointing at one specified thread, while `--all` queries upstream archived threads before deleting matching bindings; by default scans all running instances and known stopped instances, while explicit `--instance` limits the action to that instance | mutating | none; this is a local binding-record repair / ops entry |
| `focusctl [--instance <name>] thread attach (--thread-id <id> \| --thread-name <name>)` | Restore Feishu push for all detached bindings on one target thread; changing them may issue `thread/resume` | mutating | Feishu `/attach thread`, and the post-reset `Attach Current Thread` button |
| `focusctl [--instance <name>] thread detach (--thread-id <id> \| --thread-name <name>)` | Pause Feishu push for one target thread while keeping thread / binding relationships intact | mutating | no exact single Feishu command |

Implementation note:

- local `thread detach` goes through the running FOCUS service control plane
- the lower layer may still call upstream `thread/unsubscribe`, but that is an internal protocol detail, not the user-facing command name
- `service attach`, `binding attach`, and `thread attach` use `thread/resume`
  when they actually reattach a detached binding, so they are not an
  unconditional subscription shortcut. An identity-less local attach must
  pass exact lease and goal-continuation checks for every affected root; it
  cannot claim the target binding or start autonomous work. The equivalent
  Feishu `/attach` path may acquire a blank submission lease for its own
  current binding, but it cannot resume a Web- or `fcodex`-owned active turn. See
  [`root-operation-owner.md`](./root-operation-owner.md).
- If every requested binding is already attached, attach is a no-op and does
  not send `thread/resume` or acquire writer authority. This is distinct from
  Web's documented observer-only read/subscription path.
- local `thread archive` calls upstream Codex archive exactly once; after that succeeds, binding cleanup is split by instance state:
  - other running instances are cleaned through their service control plane, and that local cleanup does not call upstream archive again
  - known stopped instances are cleaned through this project's binding store API by deleting binding records with the matching `thread_id`; the command does not hand-edit `chat_bindings.json`
- if cleanup in a running instance fails because of a running turn, pending request, or unreachable control plane, the upstream archive remains done, but the command exits non-zero and prints a cleanup warning
- `archive`, `unarchive`, and `delete` report mutation results along two independent dimensions:
  - `upstream_outcome=success|error|unknown` only records whether an authoritative upstream RPC result was received; `success` does not mean Focus independently verified the complete spawned subtree
  - `focus_cleanup=complete|incomplete|skipped` only describes local Focus state discovered by this command; it says nothing about other machines or bare Codex frontends
  - timeout, EOF, connection reset, or corrupt response after the control request may have been sent returns exit code `3`; Focus does not retry automatically or clear bindings
  - local validation or JSON serialization errors detected before websocket send are definite local errors, not `unknown`
  - app-server connection or `initialize` failure before the target lifecycle mutation is sent is also a definite pre-send local error
  - a malformed lifecycle result returned after the control request was accepted is `unknown` and returns exit code `3`
  - a JSON-RPC response envelope must be an object containing exactly one of `result` or a structurally valid `error`; malformed envelopes are protocol errors, and lifecycle mutations classify them as `unknown`
  - lifecycle mutation control timeouts cover one bounded startup budget plus the operation's normal internal RPC chain: two requests for unarchive (local loaded inventory plus mutation) and three for archive/delete by id; the cross-instance loaded preflight uses one bounded total control-plane budget rather than accumulating a full timeout per instance
  - `thread archive --thread-name` first resolves a unique id through the existing read-only paginated app-server listing path; only the resolved id is sent to the lifecycle control plane, so a lookup timeout or interruption is safely retryable and cannot leave an archive mutation running in the service
  - an explicit upstream error, or upstream success with incomplete Focus cleanup, returns `1`; explicit success with complete cleanup returns `0`
- `thread unarchive` and `thread delete` accept only `--thread-id`; name selectors are deliberately omitted to avoid archived/active duplicate-name and source-visibility ambiguity
- `thread unarchive` accepts repeated `--thread-id` options; targets run independently in sequence, ordinary failures are summarized and processing continues, an `unknown` result stops the batch immediately, and completed items are not rolled back
- `thread delete` accepts exactly one `--thread-id`; repeated options fail before target resolution or mutation, so permanent deletion remains an explicitly confirmed one-at-a-time operation
- lifecycle mutations perform a root-thread loaded preflight across known local Focus instances: archive/delete allow the target instance itself to be loaded but reject another loaded or unverifiable instance, while unarchive requires both the target and all other known instances to be confirmed not loaded
- `thread unarchive` performs a fail-closed local Focus binding/loaded preflight. It does not create a cross-frontend transaction and cannot prevent a bare Codex client from racing after the check.
- `thread delete` confirms only the root ID. Upstream may cascade through its own persisted/live agent graph; Focus does not treat an incomplete descendant query as a confirmation set. After success, Focus clears known local bindings for the root, and `binding clear-stale --dry-run` is the repair path for undiscovered descendant leftovers.
- both `thread archive` and `thread delete` reject a root whose machine-level runtime lease still contains a non-service holder such as `fcodex`; delete additionally rejects a root that is known `active` or whose backend status cannot be read reliably. A conflicting submission/active-turn lease or an unreadable admission fact also rejects the lifecycle mutation.
- if upstream succeeds but an interaction lease cannot be released, Focus retains the binding record as an explicit retry marker and reports `focus_cleanup=incomplete`.
- if binding removal itself fails or cannot be verified, Focus also retains the service runtime lease instead of weakening cross-instance ownership protection.
- `thread delete --force` only skips interactive confirmation; it does not bypass loaded, running, pending, or unknown safety checks
- these commands are thin wrappers over the public upstream lifecycle APIs and coordinate only registered local Focus/fcodex runtimes. They do not detect or lock bare Codex, IDEs, standalone app-servers, or other machines, and do not provide rollout import/migration, automatic rollback, filesystem scanning, or cross-machine consistency guarantees.
- `thread clear-archived-bindings` reuses the same local binding cleanup logic but does not archive. It is for repairing leftovers from older versions, externally archived threads, or no-live-owner archive routing after a service restart.
  - `--thread-id` is the explicit repair path; the command does not query upstream just to validate archived state.
  - `--all` is an archived-aware sweep: it first calls upstream `thread/list archived=true` through a running app-server to collect archived thread ids, then reuses the local cleanup path for each id. Without `--instance`, it prefers a running `default` instance for the query, otherwise picks one running instance by name, and cleans all visible instances; with explicit `--instance`, that instance must be running and only that instance is cleaned.
- direct binding-store mutation for a stopped instance first acquires maintenance ownership through the same lock used by the service; mutation is rejected while the service is starting or running. Handler construction performs read-only hydration, then replaces its in-memory binding view from disk only after service ownership is acquired, preventing startup and offline maintenance from overwriting each other.

### 4.7 `image`

| Command | Purpose | Type | Feishu counterpart |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] image send --path <file> [--thread-id <id> \| --thread-name <name>]` | Send one local image file to all currently attached Feishu bindings on the target thread | mutating | none; this is a local control-plane action |

### 4.8 `web`

| Command | Purpose | Type | Feishu counterpart |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] web open [--no-browser]` | Read the running target instance's published loopback Gateway state, print a one-time browser bootstrap URL, and normally open it in the default browser | local UI entry | none; this opens the Web frontend for the selected Focus instance |

Notes:

- `web open` does not start the service or Gateway. The target instance must be
  running with `web_enabled: true`.
- `--instance` selects exactly one instance. Without it, normal target-instance
  resolution applies.
- `--no-browser` prints the URL without invoking the system browser.
- `web open` reads only the loopback local endpoint from runtime discovery. It
  does not read, select, print, or open the configured trusted-proxy external
  origin.
- the local bootstrap credential is carried in the URL fragment, exchanged for
  a short-lived same-origin browser session, and rotated after use. It cannot
  be consumed through an external audience.
- the browser session cookie is isolated by canonical Focus instance name.
  Authenticating multiple local instances on different ports of the same
  loopback hostname must not let a later instance replace an already
  authenticated instance session; bootstrap credentials remain one-time and
  per-instance.
- the published discovery endpoint must be the exact canonical origin
  `http://127.0.0.1:<port>` or `http://[::1]:<port>`, with no credentials,
  path, query, or fragment. The record must also carry a non-empty process
  incarnation that exactly matches the current owner process.
- a malformed endpoint, missing incarnation, unknown current incarnation,
  unconfirmed owner liveness, or incarnation mismatch is never printed or
  passed to the browser. Unknown incarnation or liveness retains the record for
  diagnosis; only a proven incarnation mismatch may retire it as stale.
- the Gateway always remains loopback-only. Forwarding the loopback port
  directly to a browser through SSH remains local mode. Trusted-proxy mode may
  use a same-host HTTPS proxy or a B-host HTTPS proxy connected through a
  protected persistent SSH tunnel to the loopback Gateway on A.
- trusted-proxy users bookmark the configured HTTPS origin directly and need
  neither `web open` nor a fragment token. The proxy injects the proxy proof
  only after its own authentication/ACL and never gives it to the browser. See
  the [Focus Web external-access decision](../decisions/focus-web-external-access.md)
  for the complete contract.
- selecting an otherwise unsubscribed persisted thread in Web may call
  `thread/resume` through the target service to establish an app-server
  subscription. Closed preflight (no continuing goal) keeps this observer-only
  and claims no writer. If resume may continue a goal, only the exact connected
  Web document may acquire the blank submission lease; another tab/device is
  refused rather than using resume to take over. It may still read an
  already-current projection.
- Web mutations share the RuntimeLoop, loaded gate, runtime lease, and
  method-specific admission used by the other Focus frontends. An ordinary Web
  prompt does not read or acquire a main-turn lease; only lease-bearing
  exclusive/autonomous control mutations use the matching
  submission/active-turn admission. The browser never receives the app-server
  capability token and never connects to app-server directly.

### 4.9 `uninstall` and `purge`

| Command | Purpose | Type | Feishu counterpart |
| --- | --- | --- | --- |
| `focusctl uninstall` | Stop and uninstall every known instance's service definition, autostart registration, wrappers, completions, and managed `.venv` while retaining configuration and other data | mutating | none |
| `focusctl purge` | Run the same uninstall flow, then delete the verified machine-level FOCUS config and data roots | destructive mutation | none |

Safety contract:

- One machine-level lock owner serializes install, uninstall, and purge. Uninstall and purge apply the installer's same
  idle-only admission to running instances. Active, pending, or unknown state rejects before uninstalling any service;
  there is no force option and the command does not interrupt user work.
- Install and repair atomically write a private `.focus-managed-root` into the
  config and data roots. The marker records its `role` and canonical target. It
  is the identity fact that the installation layout owns this leaf directory;
  Focus does not infer ownership from a directory name alone.
- `purge` accepts only the two independent managed leaves resolved by the
  instance layout. Before changing any service, wrapper, or data, it resolves
  canonical paths and rejects filesystem roots, the user home, current working
  directory, the Focus source checkout and their ancestors, over-broad shallow
  directories, symlink roots, and config/data roots that are equal or nested.
  A custom root reaches marker verification only when it is at least two
  components below the filesystem root and a shallow leaf explicitly contains
  `focus`, or when it is part of a deeper layout.
- A missing target directory is an idempotent no-op. An existing target must
  have a non-symlink marker whose `role` and canonical target match exactly.
  An older installation without markers must first run `bash install.sh` or
  `./install.ps1` from the current source as a repair. `uninstall` also requires
  a matching marker on an existing data root before deleting `.venv`; neither
  operation guesses that an existing unmarked directory is safe to delete.
- If any instance service uninstall, shared service-definition uninstall,
  post-uninstall status verification, or post-stop offline-maintenance
  ownership check fails, the command stops and does not remove wrappers,
  configuration, or data. The ownership check uses
  the same instance lock as a running service, so a returned uninstall call is
  not treated as proof that the service exited.
- Config/data deletion never uses `ignore_errors`. Any `rmtree` failure returns
  non-zero, reports which roots were already deleted, and never prints complete
  success. The two directory removals are not a cross-filesystem transaction;
  an earlier successful removal is not rolled back.
- `uninstall` treats the fixed `.venv` as program surface and removes it while preserving other config/data for a
  future reinstall. Only `purge` removes both managed roots. Neither traverses source checkouts, upstream Codex data,
  or workspace skills.
- On Windows, Python running inside `.venv` cannot prove its own deletion. The command hands exact canonical targets
  to a temporary PowerShell helper. While retaining the primary lock, the parent first transfers the same lock owner's
  handoff barrier to the helper; only a matching armed proof lets the parent release the primary lock and exit. A
  concurrent install therefore cannot enter the parent-exit/helper-deletion window. The helper then deletes and writes
  per-target `deleted`, `missing`, or `failed` results. Parent success means only that handoff was reliably committed and
  reports the helper PID and exact result path; armed status is never reported as completed deletion.

### 4.10 `config`

| Command | Purpose | Type | Feishu counterpart |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] config` | Print the target instance's `system` / `codex` / `init-token` paths and the machine-level `env` path | read-only | none |
| `focusctl [--instance <name>] config system\|codex\|init-token [--open]` | Print one instance-level configuration path; `--open` opens it in the local editor | read-only / explicit editing entry | none |
| `focusctl config env [--open]` | Print the shared `focus.env` path; `--open` opens it in the local editor | read-only / explicit editing entry | none |

Contract:

- `system`, `codex`, and `init-token` are instance-level targets; a named
  instance must already exist.
- `env` is machine-level shared configuration and does not acquire a separate
  copy when `--instance` is present.
- `--open` explicitly enters the user's selected editor; `focusctl` itself does
  not rewrite configuration content.

### 4.11 `skill`

| Command | Purpose | Type | Feishu counterpart |
| --- | --- | --- | --- |
| `focusctl skill install` | Install the workspace skills packaged by FOCUS under the current directory's `.agents/skills` | mutating | none |
| `focusctl skill uninstall` | Remove workspace skills in the current directory that FOCUS explicitly marked as managed installations | mutating | none |

Contract:

- `skill` is current-working-directory scoped and does not accept top-level
  `--instance`.
- Installation does not overwrite a different unmanaged directory, and
  uninstall does not remove a skill without the FOCUS managed marker.
- Machine-level `focusctl uninstall` / `purge` do not traverse or remove skills
  previously installed in individual workspaces.

## 5. Mapping to Feishu

| Local command | Closest Feishu entry | Key difference |
| --- | --- | --- |
| `service reset-backend` | `/reset-backend` | both are instance-level backend actions; one is CLI, one is a Feishu card flow |
| `service attach` | `/attach service` | both may resume each changed detached binding; local control has no binding-continuity proof and is gated per root, while Feishu can prove only its invoking binding |
| `binding status <binding_id>` | `/status`, `/preflight` | local output is lower-level and includes binding ids, reason codes, and current-instance interaction-owner/lease details; it is not an active-main-turn authority report |
| `binding attach <binding_id>` | `/attach binding` | both may call `thread/resume`; local control can target any known id, but neither entry may bypass an existing blank/active main-turn lease, while Feishu defaults to its current binding |
| `binding detach <binding_id>` | `/detach` | Feishu `/detach` is only current-chat scoped; local command can target any known binding id directly |
| `prompt send --binding-id <binding_id>` | none | local CLI can synthesize a future or system-triggered prompt through the service control plane; there is no equivalent Feishu slash command today |
| `thread attach --thread-id/--thread-name` | `/attach thread` | both may call `thread/resume`; Feishu scope is limited to its current binding's thread, while identity-less local control can target any direct root but cannot acquire or replace another surface's blank/active main-turn lease |
| `thread detach --thread-id/--thread-name` | no exact single Feishu command | Feishu `/detach` is current-binding scoped; the local thread action can affect all currently attached bindings on that thread |
| `thread goal --thread-id/--thread-name` | `/goal` | Feishu only operates on the current chat's current thread; local CLI is a thread-scoped debugging / ops surface and can read any explicit target thread goal |
| `thread goal set/clear` | `/goal set`, `/goal clear` | Feishu commands are binding-aware while local CLI targets an explicit thread. An `active` or omitted-status goal set can continue an idle loaded turn and therefore must acquire the ordinary blank main-turn lease; explicit paused set/clear are synchronous control mutations and create no main-turn writer |
| `thread list --scope cwd` | `/threads` | Feishu is a chat workflow entry point; local CLI is just thread discovery |
| `thread status` | lower-level diagnostics behind `/status`, `/preflight`, `/attach`, `/detach` | local CLI is a thread-scoped debugging surface |
| `web open` | none | opens the selected instance's loopback Web frontend; it is not a Feishu command or a direct app-server connection |
| `migrate from-feishu-codex` | none | local one-shot install/data transfer only; not a runtime command |

## 6. Boundary

The following expectations are explicitly wrong:

- `focusctl` is not a local UI for Feishu `/threads`
- `focusctl` does not enter the Codex TUI
- `focusctl migrate from-feishu-codex` is not an ongoing compatibility layer
- `binding clear` does not mean “stop push for the current thread”
- `thread goal set` is not a binding-aware runtime recovery / pause command and does not promise settings sync; an `active` or omitted-status result **can** immediately continue an idle loaded turn and must acquire the ordinary blank main-turn lease. Explicit `--status paused` is the only identity-less non-continuing maintenance exception, and only when no blank/active main-turn lease exists and direct-target/runtime checks pass
- a local goal/settings control request cannot bypass a Web, Feishu, or `fcodex` exact submission/active-turn lease; only the safe idle `paused` goal-set or clear maintenance mutation is identity-less

If any `focusctl` subcommand is added, removed, renamed, or changes its selector rules, instance resolution, or Feishu mapping, this document must change with the code.
