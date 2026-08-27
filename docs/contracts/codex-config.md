# `codex.yaml` admission contract

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/codex-config.zh-CN.md`.

## Purpose

`codex.yaml` contains instance defaults for the app-server adapter, Feishu
handler, Focus Web gateway, and local administrative clients.  A typo or an
implicit Python conversion at this boundary can change a safety default: for
example, `bool("false")` is true, and an unknown `approval_policy` spelling can
otherwise fall back to `never`.

This contract makes component-config admission one explicit, fail-closed
boundary. It does not define upstream `~/.codex/config.toml` or
binding-persisted runtime settings. Persistence and application semantics for
Web `WebNextTurnSettings` belong to the runtime-settings contract rather than
being inferred from the YAML schema.

## Authoritative source and projections

`bot.codex_config.CodexConfig` is the authoritative accepted-key inventory,
default-value owner, and typed parser for `codex.yaml`.

- `config/codex.yaml.example` is the human-facing projection of that schema.
  A regression test requires its documented key inventory to equal the parser
  inventory.
- `CodexAppServerConfig` is a narrow runtime projection of an already validated
  `CodexConfig`; it is not a second YAML schema.
- `FocusRuntime`, `focus` / `fcodex`, and runtime administrative adapter paths
  must validate the complete component document before consuming any field.

Adding a setting therefore requires changing the `CodexConfig` field/parser,
the example projection, and the focused schema tests together.  Adding a new
`dict.get()` conversion in a consumer does not create a supported setting.

## Admission rules

- The document is a string-keyed mapping. Duplicate mapping keys at any depth,
  unknown top-level keys, explicit nulls, and removed keys are rejected;
  duplicate keys never use last-value-wins semantics.
- Strings, booleans, integers, numbers, and string lists are distinct types.
  In particular, strings such as `"false"` are not booleans; booleans are not
  integers; and a scalar string is not a one-item list.
- Strings and `source_kinds` entries have surrounding whitespace removed before
  they enter the typed config; internal spaces are preserved. Fields whose
  contract permits empty values (such as model, service tier, and effort) may
  still be empty after trimming, while nonempty fields fail admission.
- Numeric values must be finite and satisfy the field's declared operational
  range.  Explicit empty values are accepted only for fields whose contract
  gives empty a meaning, such as automatic model or effort selection.
- The Focus service always launches and owns a same-host app-server;
  `app_server_mode` has been removed from the product configuration surface.
  `app_server_url` is only that child process's preferred listen address and
  must satisfy upstream `codex app-server --listen`: a pathless
  `ws://loopback-IP:port` with a nonzero port, not `wss`, a hostname, a
  non-loopback listener, embedded credentials, a query, or a fragment.
- `focusctl` and `focus` / `fcodex` can still connect to the endpoint published
  by a running local instance. That is an internal `attached_endpoint` client
  capability, not a second deployment mode and not a way for `codex.yaml` to
  target an external app-server. The runtime registry only selects the running
  instance and its data directory; the sole dialable endpoint source is that
  instance's authenticated live `service/status` response, after protocol
  readiness and replacement-generation admission.
- The Web Gateway always accepts only `127.0.0.1`, `localhost`, or `::1` as
  `web_host`, and `web_session_ttl_seconds` is at least 60 seconds. These are
  schema admission rules; matching Gateway checks are defensive runtime
  assertions, not a second configuration contract.
- `source_kinds` is a nonempty list of nonempty strings.  It is never expanded
  character by character from one scalar string.
- `approval_policy` is a closed local enum.  The sole legacy migration is
  `on-failure` to `on-request`, as recorded in the permissions contract.
- `approvals_reviewer` remains `user`, matching Focus's reviewed user-owned
  interaction route. `thread/start` / `thread/resume` must send it explicitly
  and verify that the response still reports `user`; checking server
  requirements alone does not cover an older reviewer persisted with a
  thread. Upstream auto-review is not enabled merely because a newer
  app-server exposes that enum value.
- `personality` follows the current app-server enum and accepts only
  `friendly`, `pragmatic`, or `none`; an arbitrary string does not remain
  latent until thread/turn dispatch.
- `permissions_profile_id` is the only permissions key accepted in
  `codex.yaml`.  Persisted binding-store compatibility for legacy `sandbox`
  does not make `sandbox` a component-config alias.
  Its value is limited to the three Focus-modeled built-ins: `:read-only`,
  `:workspace`, and `:danger-full-access`. Upstream custom profiles do not
  become free-form config strings before Focus defines cwd/availability rules.

### Focus Web deployment display name

`web_display_name` is the instance-configured label used by the Focus Web
browser tab:

- when absent it has the fixed default `Focus Web`; every Focus instance uses
  that same default, with no fallback inferred from the instance name, operating
  system hostname, browser-visible hostname, or trusted-proxy origin
- an explicit value follows ordinary nonempty-string admission: surrounding
  whitespace is removed and the result must remain nonempty
- the service captures it at startup and projects it through Focus Web meta to
  the current browser; a config change requires a service restart and page
  reload and creates no live-reload path.

This field is a deployment label, not a complete static page title. The
[Focus Web wire contract](focus-web-wire.md) owns active-conversation selection,
ordering, and browser-presentation semantics.

### Trusted-proxy Web configuration

`web_trusted_proxy_origin` and `web_trusted_proxy_proof_sha256` are the only new
canonical config scalars for trusted-proxy mode:

- both default to the empty string, which disables trusted-proxy mode; they
  must be empty together or populated together
- both are exact strings; the parser does not silently trim them or rewrite
  case
- `web_trusted_proxy_origin` is the one canonical HTTPS origin accepted by
  `bot.network_contract.parse_trusted_proxy_external_origin`: its host is
  lowercase ASCII DNS labels whose final label is not a WHATWG IPv4 number,
  or a strict canonical IPv4 / compressed IPv6 literal, and it omits default
  port 443. A trailing dot, Unicode/percent/backslash host, legacy IPv4,
  uncompressed or IPv4-mapped IPv6, `localhost` / `.localhost`, a
  loopback/unspecified IP, wildcard, credentials, path, query, fragment, or a
  list of origins is rejected
- `web_trusted_proxy_proof_sha256` is the 64-character lowercase hexadecimal
  SHA-256 verifier of the raw 32-byte random URL-safe proxy proof; the raw proof
  is not a Focus config value
- enabling the mode also requires `web_enabled: true` and a fixed nonzero
  `web_port`, while `web_host` remains loopback. Failure of any cross-field
  condition rejects the complete component config rather than partially
  enabling the mode.

The service captures both values once at startup. They do not enter runtime
discovery and create neither live reload nor a dual-verifier transition. The
[Focus Web external-access decision](../decisions/focus-web-external-access.md)
alone defines deployment, proxy-proof, opaque-label, and session-audience
semantics.

Invalid configuration stops that entry path with a diagnostic naming the
field.  It must not continue with a default, partially start a service, or
silently reinterpret the value.

## Relationship to runtime setting facts

As described in `runtime-settings-fact-sources.md`, validated instance values
provide two seeds: initial safety/runtime values for a new Feishu binding, and
instance-wide `WebNextTurnSettings` when no durable record exists at service
start. The service captures the latter only on startup. Web reads/turns do not
reread or mirror config; the first explicit mutation creates a record which
wins on later restarts. Once a binding persists its own values, that binding
remains its next-turn fact source. Strict parsing does not merge instance config
into an existing binding or Web record and creates no setting synchronization
among Feishu, Web, and local TUI state.

## Compatibility consequence

Configuration that previously relied on coercion now fails at startup or CLI
entry.  This includes misspelled keys, quoted booleans/numbers, scalar
`source_kinds`, and the old component keys `permissions`, `sandbox`, or
`app_server_mode`. An old remote deployment is not guessed into a replacement;
after removing the mode key, `app_server_url` must also be changed back to a
local loopback listener. The
repair is to use the typed spelling shown in `codex.yaml.example`; silently
preserving the old interpretation would retain the ambiguity this boundary is
intended to remove.
