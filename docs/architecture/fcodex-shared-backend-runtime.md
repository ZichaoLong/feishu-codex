# `fcodex` Shared-Backend Runtime

This document is the implementation note for the current shared-backend and
wrapper runtime model in `feishu-codex`. If you want to understand why
`fcodex`, the shared backend, dynamic port fallback, or the cwd proxy exist,
start here.

This document explains the implementation model behind:

- `fcodex --cd`
- the local websocket proxy
- the shared Codex remote app-server used by `feishu-codex`

See also:

- `docs/contracts/session-profile-semantics.md`
- `docs/decisions/shared-backend-resume-safety.md`
- `docs/architecture/feishu-codex-design.md`

## 1. Upstream Baseline

- Upstream project: [`openai/codex`](https://github.com/openai/codex.git)
- Current local validation baseline: `codex-cli 0.118.0` (checked on 2026-04-03)
- This document describes the runtime model verified by the current
  `feishu-codex` integration against stock Codex CLI / `codex app-server` /
  `--remote` behavior. If upstream changes those behaviors later, this document
  should be updated accordingly.

## 2. Runtime Pieces

At steady state, the local/shared path looks like this:

```text
shared CODEX_HOME
machine-global coordination (`FC_GLOBAL_DATA_DIR`)
  - instance registry
  - thread runtime lease

instance A / default
  Feishu client
    -> feishu-codex service
       -> instance-local shared codex app-server
          (prefers ws://127.0.0.1:8765; auto-falls back to a free local port)

fcodex shell wrapper
  -> select target instance backend
  -> local owner-filtering proxy
     -> selected instance-local shared codex app-server
        -> upstream Codex TUI
```

The important points are:

- `shared backend` now means an instance-local shared backend
- multiple instances share `CODEX_HOME`, not one universal live app-server
  backend
- if Feishu and `fcodex` should safely continue the same live thread, they are
  expected to talk to the same instance backend

## 3. Why `fcodex` Exists

Bare `codex` normally owns its own backend lifecycle. That is fine for normal
local use, but it is the wrong default when you want Feishu and local TUI to
operate on the same live thread.

`fcodex` exists to provide:

- one shared backend with the selected Feishu instance
- a default-profile seed for new threads started through that selected instance
- `resume <thread_name>` resolution on top of the shared backend
- a compatibility patch for remote-mode working-directory behavior

## 4. Installed Wrapper Environment

In multi-instance mode, distinguish three local path layers:

1. shared `CODEX_HOME`
2. per-instance `FC_CONFIG_DIR` / `FC_DATA_DIR`
3. machine-global coordination under `FC_GLOBAL_DATA_DIR`

Specifically:

- the `default` instance remains path-compatible with the original
  single-instance layout
- named instances live under `instances/<name>` subdirectories
- `FC_GLOBAL_DATA_DIR` defaults to `_global/` under the data root

The installed `fcodex` wrapper first prepares the base environment, then hands
off to the Python wrapper for actual instance selection. That layer:

1. loads the shared `feishu-codex.env` from the machine config root when present
2. prepares default-instance `FC_CONFIG_DIR` / `FC_DATA_DIR` root information
3. resolves `--instance`, the instance registry, and the runtime lease to pick
   the target instance for this launch

For code ownership, the launch path is intentionally split:

- the wrapper owns selected-instance resolution and launch-time profile
  decisions
- the proxy owns only transport fixes plus one-time persistence of the initial
  new-thread profile seed after a real `thread_id` exists

So "wrapper and service share local state" should now be read as:

- the wrapper and service of the same instance share that instance's config,
  profile-state, and runtime backend-discovery state
- the wrapper and daemon both load the same machine-level `feishu-codex.env`
  provider environment file
- all instances share `CODEX_HOME`
- all instances share the machine-level instance registry and thread runtime
  lease

When the default `ws://127.0.0.1:8765` endpoint is unavailable and one instance
service falls back to another free port, `fcodex` uses that instance's local
runtime-discovery state to find the active backend.

## 5. How `--cd` Actually Works

`fcodex` resolves one effective working directory per launch:

- if the user passes `--cd` or `-C`, use that
- otherwise, use the current shell cwd
- if the user explicitly passes `--cd` / `-C` but omits the value, the wrapper
  should fail fast instead of silently falling back to the current cwd

It then does two separate things with that value:

1. pass `--cd` through to upstream `codex`
2. pass the same cwd into the local proxy

This double handling is intentional.

## 6. Why a Local Proxy Is Needed

The original problem was:

- in remote mode, upstream Codex TUI did not reliably send `cwd` on
  `thread/start`
- the shared app-server then fell back to its own process working directory
- for `feishu-codex`, that fallback directory is typically
  `~/.local/share/feishu-codex`

Result:

- plain `fcodex` fresh starts could end up in the service data directory instead
  of the caller's shell directory

The local proxy fixes that specific gap:

- it forwards websocket traffic to the shared backend
- when it sees `thread/start` with missing or empty `params.cwd`, it injects the
  effective cwd chosen by the wrapper
- all other traffic is forwarded unchanged

This keeps the patch very narrow.

## 7. Why Proxy Lifetime Follows the Parent Process

During investigation we confirmed that upstream remote resume is not a
single-connection flow.

`codex --remote ... resume <id>` may:

1. connect once for session lookup / startup work
2. disconnect
3. reconnect for the actual TUI session

Therefore, the proxy cannot safely shut down after the first websocket client
disconnects.

Current model:

- when launched by `fcodex`, the proxy receives the wrapper process PID
- the proxy stays alive until that parent process exits
- when used in tests without a parent PID, it can still fall back to a short
  idle-timeout mode

This is why the current implementation is robust against resume-time reconnects.

## 8. What Uses the Shared Backend

By default:

- Feishu commands use the shared backend
- plain `fcodex`
- `fcodex <prompt>`
- `fcodex resume <thread_id>`
- `fcodex resume <thread_name>` after wrapper-side resolution

Here "shared backend" always means the selected instance backend.
The shell layer no longer exposes wrapper slash commands such as
`fcodex /session`; local thread discovery now belongs to
`feishu-codexctl thread list`.

## 9. Explicit `--remote` Is a Special Case

If the user explicitly passes `--remote` to `fcodex`, the wrapper does not try
to force the shared-backend path.

That means:

- no local cwd-fixing proxy is inserted
- no shared-backend guarantee is implied
- the user is choosing a custom remote target

This is intentional. Explicit `--remote` means "use the target I asked for."

## 10. Differences from Bare `codex`

Compared with bare Codex TUI, `fcodex` adds these semantics:

- shared backend with the selected Feishu instance by default
- new-thread default-profile seeding when `-p/--profile` is absent
- thread-name resume resolution plus thread-wise profile injection / persistence
- cwd patching through a thin local proxy

The profile split is explicit:

- wrapper: read/write thread-wise resume profile state for existing threads,
  and decide whether this launch carries an initial new-thread seed
- proxy: persist that initial seed exactly once, only after the first
  successful `thread/start` returns a concrete `thread_id`

Inside the running TUI, however, command semantics return to upstream Codex
behavior.

## 11. Known Caveats

### Upstream remote protocol may change

The cwd proxy exists because of current upstream remote-mode behavior. If
upstream later changes:

- `thread/start` payload shape
- remote session startup order
- reconnect timing

the wrapper may need adjustment. For the upstream implementation and release
history, refer to [`openai/codex`](https://github.com/openai/codex.git).

### Bare `codex` is still outside the shared-thread contract

If a user opens the same thread through bare `codex` using its own backend while
Feishu or `fcodex` is also writing that thread, `feishu-codex` cannot make that
safe.

### TUI discovery remains upstream

Inside the TUI, `/resume` picker behavior remains upstream and may differ from:

- Feishu `/session`
- `feishu-codexctl thread list`
- `fcodex resume <thread_name>`

### Shared backend availability matters

If the selected instance's shared app-server is not running or not reachable,
`fcodex` cannot do its job. In that case, startup fails fast rather than
silently falling back to an isolated local backend.

## 12. Developer Pointers

Relevant implementation files:

- wrapper argument handling and shared-backend launch:
  - `bot/fcodex.py`
- proxy transport and cwd injection:
  - `bot/fcodex_proxy.py`
- Feishu-side adapter/handler:
  - `bot/codex_handler.py`
  - `bot/adapters/codex_app_server.py`
- shared discovery logic:
  - `bot/session_resolution.py`
