# Codex app-server schema drift guard

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/codex-app-server-schema-drift.zh-CN.md`.

Status: accepted maintenance contract. The reviewed baseline is
[`codex-app-server-schema-baseline.json`](codex-app-server-schema-baseline.json).

## Purpose

Focus has a deliberately narrow app-server boundary: it adapts a subset of
Codex RPCs into Focus-owned Feishu, fcodex, and Web contracts. Upstream unions
grow independently. A new method must therefore never become supported merely
because one frontend can send or receive JSON with that name.

[`scripts/check_codex_app_server_drift.py`](../../scripts/check_codex_app_server_drift.py)
is an upgrade-time, fail-closed guard. It uses generated JSON Schema with
`--experimental`, because Focus currently relies on experimental history APIs
such as `thread/turns/list`.

It is intentionally not a browser schema, a public API, or a runtime JSON
Schema validator.

## What the baseline locks

The baseline records the upstream commit and generator command, then locks:

- the complete `ClientRequest`, `ServerRequest`, and `ServerNotification`
  method inventories;
- the complete `ThreadItem` type inventory;
- every actual outbound client request method in `CodexAppServerAdapter` must
  be statically provable as a literal or reviewed helper-parameter forwarding
  and belong to the pinned official `ClientRequest` inventory;
- a classification for every Focus-referenced method, plus every upstream
  client method whose top-level params contain `threadId`;
- the reviewed fcodex policy for a concrete client request that has no
  nonempty `threadId`: a literal allowlist plus an immutable default-deny
  action;
- semantic fingerprints of all classified request/notification parameter
  closures, selected response roots consumed by Focus, and classified
  `ThreadItem` variants.

The fingerprints include every reachable definition, while retaining the
direct normalized schema beside each hash for a useful review diff. Cosmetic
fields such as titles, descriptions, defaults, examples, and formats do not
create churn.

The client categories are deliberately explicit:

- `shared_operation_mutation`
- `observer_read`
- `connection_local_request`
- `explicit_admin_control_plane`
- explicit deny/unsupported categories

`thread/unsubscribe` is classified as connection-local, not as a global
operation mutation. `thread/resume` is classified as a shared-operation
mutation: it may be an idle subscription bootstrap for a conclusively
non-continuing goal, but it can also load a thread or trigger an
active persisted goal's autonomous continuation after its response. It
therefore must pass the main-turn lease/goal-preflight gate. `thread/goal/set` is
also a shared-operation mutation: a result of `active` can continue an idle
loaded thread, so it is never merely raw persisted-state control.
`explicit_admin_control_plane` is deliberately empty in this baseline: the
operation-owner contract currently grants no additional raw app-server control
through an administrator-style owner route. That is not a global-control
escape hatch: `fcodex_unscoped_client_request_policy` is default-deny, and the
guard statically verifies that the proxy's literal allowlist matches the
reviewed policy. See the fcodex owner contract for the small permitted
discovery/connection set and the optional-`threadId` rule.

Server requests distinguish interactive, owner-routed work from stateless
protocol utilities. `currentTime/read` is the only automatic utility in this
baseline. Focus validates its exact `{ threadId }` params and answers with an
integer host Unix timestamp on the same websocket generation; it is never
shown to a frontend and never creates or clears a main-turn lease or
server-request projection. An
unknown method, malformed request, or future utility shape does not inherit
that exemption. The complete parameter closures for `error` and `warning` are
explicitly fingerprinted as inputs to Web typed runtime notices; Focus projects
only reviewed fields and does not parse their natural-language text.
`model/rerouted` and `thread/settings/updated` are likewise
explicitly fingerprinted notification inputs. They are still not projected as
user-visible transcript events, but Focus consumes them as provenance for its
connection-local effective-settings registry, whose model is also used by
native-media admission, in addition to the operator diagnostic log.

## Normal upgrade check

Generate schema with the exact candidate Codex binary, then check it before
changing Focus code or the baseline:

```bash
schema_dir="$(mktemp -d)"
codex app-server generate-json-schema --out "$schema_dir" --experimental
python scripts/check_codex_app_server_drift.py --schema-dir "$schema_dir"
```

For a developer-provided, read-only upstream checkout, the protocol exporter
can produce the same input when a Rust build is available. Ask for the checkout
path when it was not supplied, keep it only in the current shell, and put Cargo
outputs outside the checkout:

```bash
codex_checkout="<developer-provided-read-only-checkout>"
schema_dir="$(mktemp -d)"
codex_target_dir="$(mktemp -d)"
upstream_head="$(git -C "$codex_checkout" rev-parse HEAD)"
upstream_status="$(git -C "$codex_checkout" status --porcelain=v1)"
CARGO_TARGET_DIR="$codex_target_dir" \
  cargo run --locked --manifest-path "$codex_checkout/codex-rs/Cargo.toml" \
    -p codex-app-server-protocol --bin export -- \
    --out "$schema_dir" --experimental
test "$(git -C "$codex_checkout" rev-parse HEAD)" = "$upstream_head"
test "$(git -C "$codex_checkout" status --porcelain=v1)" = "$upstream_status"
python scripts/check_codex_app_server_drift.py --schema-dir "$schema_dir"
```

The check itself and its unit tests need neither the upstream checkout nor a
Codex binary. They only need a previously generated schema directory at
refresh time; the committed baseline is sufficient for ordinary repository
tests. Never record `codex_checkout` or another machine-local path in the
baseline or other durable evidence.

## Updating a baseline

An upstream upgrade is not complete when the generator happens to succeed.
The reviewer must classify every changed method/item and decide whether Focus
should support, reject, or keep it out of scope.

```bash
reviewed_upstream_commit="$(git -C "$codex_checkout" rev-parse HEAD)"
python scripts/check_codex_app_server_drift.py \
  --schema-dir "$schema_dir" \
  --write-baseline \
  --upstream-commit "$reviewed_upstream_commit" \
  --generator "<portable exact binary/version and command; no local path>"
git diff -- docs/contracts/codex-app-server-schema-baseline.json
python -m pytest -q tests/test_codex_app_server_schema_drift.py
```

`--write-baseline` is intentionally explicit and requires a commit. It only
refreshes generated inventory/fingerprint fields after policy validation; it
does not invent classifications. Review the resulting diff before committing.

## Fail-closed meaning

The guard fails when any of the following happens:

- the input was generated without `--experimental`;
- any upstream method or `ThreadItem` inventory changes;
- the adapter emits a method absent from the pinned official `ClientRequest`
  inventory, or constructs a method through an unreviewed dynamic expression;
- Focus source starts referring to an upstream method/item without a reviewed
  classification;
- a current or new client method with direct `threadId` lacks a classification;
- the fcodex no-thread-target policy is not default-deny, names an unavailable
  or required-thread-target method, or differs from the runtime proxy
  allowlist;
- a classified method disappears, or a focused request/response/item shape
  changes.

This is only the upgrade-time half of fail-closed behavior. The runtime owner
router remains authoritative for live messages. In particular, fcodex must
only deliver server requests that its service-owned interaction router has
classified; an unknown server request is rejected upstream, not passed to an
existing writer. Unknown client requests carrying `threadId` likewise need a
classification before they can be admitted to the thread-owner path. The
schema guard detects the upstream inventory change that requires reviewing
those runtime rules; it does not itself send JSON-RPC responses or grant a
writer lease.

## Runtime connection admission

The generated-schema guard is complemented by a connection-local protocol
gate. Each websocket moves explicitly through `DISCONNECTED`, `HANDSHAKING`,
and `READY`; ordinary requests cannot use it before `READY`. The one handshake
owner performs, in order:

1. `initialize` request;
2. required `initialized` notification;
3. `configRequirements/read` response-envelope validation and optional
   `allowedApprovalsReviewers=user` admission.

The third step is not a complete managed-availability validator. Focus does not
compare `allowedApprovalPolicies`, `allowedSandboxModes`, or
`allowedPermissionProfiles` against every entry in its static catalogs; a
partial or empty restriction in any of these three non-reviewer fields cannot
invalidate the whole connection. Upstream owns each concrete lifecycle/settings
effect. Because the response has no config revision, invalidation, or atomic
receipt for a later effect, this handshake snapshot is also not hard prefilter
authority across time, cwd, or frontends.

Only those exact request/notification methods are available to the handshake
owner. Notifications received in the small interval before requirements
admission are buffered in order with a hard limit of 128. A server request,
malformed ingress, or buffer overflow during that interval fails the handshake
closed. Buffered notifications are released only after the same websocket
generation reaches `READY`.

Ordinary requests and ordinary server-request responses obtain an opaque
outbound permit immediately before their transport boundary and confirm that
same permit after a successful return. Disconnect, supersession, cleanup or
activation failure, and intentional backend reset advance the outbound epoch.
A response which returns after that change is explicit transport-unknown, not
a success which may update local state. While reset or cleanup remains fenced,
the permit fails before the RPC client can reconnect or send.

For a server-request response, this permit is not identity authority by itself.
The canonical identity must carry the positive generation on which the request
arrived, and transport must claim the exact typed id/generation pair without a
generation-less fallback. Web/Feishu additionally require the exact
`response_capability`, while fcodex requires the exact `response_token`; those
surface nonces prevent stale actions when a Focus restart reuses a generation.

Handshake initialization is a narrower connection capability. Generation-
pinned reset/cleanup reads and fail-close responses use a bounded
existing-backend capability which cannot reconnect; they do not gain ordinary
request authority and do not reopen the epoch. Delayed callbacks from an older
generation likewise cannot revive connection-local facts. This
runtime gate is the authority for connection lifecycle. It is related to, but
not replaced by, the upgrade-time schema inventory above.

## Deliberate boundaries

The guard does not claim to prove all future app-server behavior. It cannot
infer lifecycle/root semantics or turn an unknown upstream feature into a
supported Focus feature. Adapter outbound methods must be statically provable
literals or reviewed helper forwarding; an unprovable dynamic construction
fails closed. Source-literal scanning elsewhere in the repository remains a
classification signal rather than a complete call-graph proof. The guard also
does not replace focused adapter/projection tests.

In particular, the fcodex owner coordinator remains a thread-scoped boundary:
a client RPC without a concrete `threadId` has no thread target to classify. The
runtime proxy closes that gap with a narrow default-deny policy, rather than
pretending that a global request belongs to an existing main-turn holder. This guard
does not infer whether a future global method is semantically safe; it keeps
that method visible and guarantees it remains locally denied unless a reviewer
changes both the explicit policy and the proxy implementation.
