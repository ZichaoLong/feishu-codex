# Thread Create and Local Commit Contract

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/thread-create-local-commit.zh-CN.md`.

## Scope

Codex app-server owns `thread/start` and the created thread. Focus owns only
the local consequences of a typed successful response:

1. retain the machine runtime lease for the returned thread;
2. record the response-side effective-settings fact;
3. commit the returned thread to the requesting surface;
4. for Web, prepare and submit the first turn as a separate operation.

This boundary is not a durable distributed transaction. It does not make
`thread/start` idempotent and does not own thread inventory.

## Upstream baseline

Codex TUI sends typed `thread/start`, switches its local session after a valid
success response, and displays an error otherwise. Upstream has no durable
frontend create journal or global mutation quarantine. A thread created before
a response is lost remains discoverable through `thread/list`, `thread/read`,
or `thread/resume`.

Focus follows that behavior. The fact that one create result is unknown is not
evidence that an existing thread is unsafe to mutate.

## Persisted history-mode boundary

A Focus thread created through canonical `CodexAppServerAdapter.create_thread()`
is persistent. Web and Feishu share this adapter / `ThreadCreateTransaction`
path. The adapter explicitly sends `historyMode: paginated` and requires the
upstream `Thread.historyMode` in the typed success response to remain exactly
`paginated`. A missing field, a future value, or `legacy` is not success, and
Focus does not automatically retry after removing the field. Focus neither
parses nor rewrites rollouts and does not persist a second copy of this response
fact.

This choice applies only to Web/Feishu threads created after the upgrade.
Existing `legacy` threads are not migrated and remain usable through their
existing lifecycle; capabilities that require the paginated store report
unavailable instead. `ThreadSummary.history_mode=None` is reserved for a
Focus-owned provisional/temporary summary constructed without an upstream
Thread DTO. Every real upstream Thread DTO must strictly decode to `legacy` or
`paginated`.

An fcodex `thread/start` is sent by the upstream TUI through the local proxy and
does not call canonical adapter create. Focus retains only its existing cwd
injection and admission/settlement behavior; it does not add, remove, or
overwrite `historyMode`. Requesting paginated history and the upstream TUI's
own bounded fallback therefore remain upstream-owned.

## Same-stack create

Web and Feishu call `ThreadCreateTransaction.create_and_commit_thread()` on the
serialized RuntimeLoop. The order is:

```text
typed thread/start response
  -> validate a non-empty thread id
  -> validate persisted historyMode is exactly paginated
  -> acquire the machine runtime lease
  -> record the effective-settings fact
  -> run one surface-local commit callback
```

The callback is the only surface-specific local-commit boundary:

- Feishu commits the exact binding transition;
- Web validates and returns the exact created-thread identity before first-turn
  setup; the later document projection is best-effort presentation and creates
  no temporary main-turn owner.

There is no owner descriptor, write-ahead attempt, durable phase ledger, lease
provenance journal, terminal tombstone, or cross-restart callback replay.

## fcodex external transport

fcodex transports `thread/start` on its own websocket. Before forwarding it,
Focus issues one opaque `ExternalThreadCreateAttempt` for the current backend
generation. A valid typed success consumes that exact capability once and
commits the returned direct-root identity to
`FcodexParticipantRuntimeRegistry`.

The capability is process-local. Disconnect or backend replacement invalidates
it. A copied, replayed, already-consumed, or old-generation capability cannot
settle another request. This is the only additional multi-frontend rule at the
create boundary.

An error or unknown response consumes the exact request and is reported to the
fcodex client. It does not quarantine the proxy connection merely because the
create effect may be unknown, and it does not block another explicit create.

## Outcomes

### Known no effect

If transport proves bytes were not sent, the original typed error is returned.
No local consequence is applied. A later retry is a new explicit user action.

### Outcome unknown

If transport may have sent the request, or a nominal success lacks a valid
thread identity, Focus reports `ThreadCreateOutcomeUnknown` and never
automatically retries that intent. The surface should tell the user to inspect
the global thread list before deciding whether to create again.

The uncertainty is scoped to that exact request. It creates no durable record
and blocks no known thread, surface, lifecycle command, or backend replacement.

### Successful response, local failure

If the runtime lease, effective-settings projection, or surface-local callback
fails after a valid success response, Focus reports
`ThreadCreateLocalCommitFailed` with the known `thread_id` and failing stage.
The thread remains discoverable and can be opened from inventory.

Focus does not infer that an exception means a partially applied local callback
did or did not commit. It therefore does not replay the callback and does not
build a recovery quarantine around it. Other threads remain usable.

### Success

The surface receives `CommittedThreadCreate(response, local_result)`. For Web,
this commits the new thread identity. The first ordinary prompt then directly
uses upstream `turn/start` start-or-steer semantics without reading, acquiring,
activating, or releasing a shared `InteractionLease`. An unknown first-turn
outcome is still never replayed automatically. Presentation and first-turn
outcome do not alter create identity.
When Focus knows the created thread id but the first `turn/start` returns
`turn_submission_unknown`, Web must first durably save a browser-local
possibly-sent record in the current Tab's `sessionStorage` before settling the
original Composer and attempting to open that thread. Its user payload stores
only the original text and whether the original submission had attachments;
the record also carries stable-client, created-thread, cwd, local-operation/key,
and fixed empty/zero schema metadata. It invents no mutation id,
client-message id, or server recovery identity, and it removes already-submitted
thread-scoped attachment receipts before persistence. The user may explicitly
discard this browser-local record from any current UI scope. Only after opening
the created thread may the user hand off its text as a new unsent message with a
new identity;
neither local action calls the server unknown-resolution API. If the record
cannot be durably saved, Web must not claim that it settled the original
Composer.
In that case Web retains the original draft scope, does not navigate
automatically, and exposes the known created thread id in the error. This avoids
losing the payload still owned by that Composer when scope changes; the user can
open the thread manually from the global list to inspect it.
A known first-turn failure attempts to restore the exact attachment set to
pending. `thread_created_turn_not_started` reports the actual result as
`attachment_disposition=restored|reupload_required`. Only `restored` makes the
original attachment ids directly retryable; a store-write failure cannot
masquerade as restoration, and the user must remove and re-upload those
attachments in the already-created thread.

## Reset and observability

Connection invalidation and backend reset revoke old fcodex create
capabilities. There is no create ledger to clear, recover, or expose in service
status. Machine process proof and runtime leases remain owned by their existing
runtime authorities.

Operational warnings may report a known thread id and local stage. They are
diagnostics, not mutation admission authority.

## Regression requirements

Tests must lock down:

- typed success ordering and one local callback;
- canonical Web/Feishu create explicitly requests and validates
  `historyMode=paginated`;
- unsupported history mode or a legacy response is not automatically retried;
- fcodex forwards the `historyMode` in its `thread/start` payload unchanged;
- known-no-effect versus unknown transport classification;
- no automatic retry after an unknown result;
- no thread/global mutation quarantine after unknown or local failure;
- no Web creation, read, or mutation of a foreign blank/active main-turn lease
  for the first ordinary prompt; exact attachment restore/reupload disposition
  after known failure; and, for a known-thread unknown outcome, durable
  text-only fencing before opening the created thread, with no attachment reuse,
  invented server identity, or automatic replay;
- one-shot fcodex capability consumption and backend-generation invalidation;
- a local fcodex acknowledgement retry cannot replay create or its callback;
- Web and Feishu surface errors expose a known thread id when available.
