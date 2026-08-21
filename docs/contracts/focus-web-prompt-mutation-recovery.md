# Focus Web Prompt Mutation Recovery Contract

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/focus-web-prompt-mutation-recovery.zh-CN.md`.

This document defines the single POST, single-upstream-effect authority,
process-local result receipt, and Composer settlement boundary for an ordinary
Focus Web prompt submitted to an existing thread. It does not upgrade upstream
`turn/start` or `turn/steer` into cross-process exactly-once behavior, and it does
not define fcodex, ordinary Feishu prompt/FIFO, explicit Feishu `/steer`, review,
compact, interrupt, or thread-create identity.

For a thread create with a known created thread but a typed-unknown first prompt,
the browser-local text-only record remains owned by `thread-create-local-commit`.
This contract requires only that an existing-thread prompt neither reuse nor
overwrite that record.

## 1. User Outcome and Upstream Boundary

The reviewed public upstream baseline is Codex CLI `0.147.0`:
[`openai/codex@be6e8eac029b183056b7e4402879f15d2c85f61b`](https://github.com/openai/codex/commit/be6e8eac029b183056b7e4402879f15d2c85f61b).
Official `turn/start` retains start-or-steer semantics. `turn/steer` accepts a
non-empty exact `expectedTurnId` and rejects a missing or mismatched current active
turn. Public evidence is in
[`turn_processor.rs#L474-L607`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L474-L607),
[`turn_processor.rs#L908-L976`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L908-L976),
and [`session/mod.rs#L3957-L3993`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/session/mod.rs#L3957-L3993).
The upstream TUI extracts a successor from version-specific error text and retries
at most once. That is TUI product behavior, not an app-server delivery guarantee;
see [`app.rs#L678-L737`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/tui/src/app.rs#L678-L737).

Focus has one minimal intentional difference for the proven shared-backend,
multi-frontend scenario. After the server freezes active turn A, an upstream report
of successor B or no active turn classifies this request as `known_no_effect`,
retains the original input in the live Composer, and never automatically retargets
B or falls back to `turn/start`. Web, fcodex, Feishu, or an autonomous goal may have
opened B for another context, so the user reviews the new context and clicks again
explicitly. Only an attempt whose prepare found no exact active id selects
`mode=start` and calls official `turn/start` once. One browser attempt therefore
sends one turn-producing RPC and produces at most one upstream input effect.

Web has one submit gesture. Enter and the send button enter the same synchronous
boundary, while Shift+Enter inserts a newline. Ordinary prompts have no browser
FIFO. One gesture produces one canonical mutation identity and one POST. Focus never
automatically resends an outcome-unknown prompt, never infers no effect from a
missing transcript input, and never lets one unknown prompt block a new mutation on
the same thread, another thread, or another surface.

## 2. Identity, Receipt, and Sole Owner

`ComposerSubmission` in `web/src/components/chat/composerSubmission.ts` is the sole
browser-local owner of whether the press-time text/chips remain pending and may be
cleared from the current Composer. It owns neither the upstream effect, turn
identity, nor result receipt.

Before yielding to HTTP, the browser freezes a canonical UUID `mutation_id` and the
exact payload. The POST body contains exactly:

- `text`;
- `attachment_ids`;
- `mutation_id`;
- the press-time `source_scope_generation`, `source_attachment_scope`, and
  `source_composer_scope_id`.

The current `client_id` and exact document receipt come from the Gateway-authenticated
request/header and are not repeated in the body. The server captures runtime epoch,
backend connection generation, active turn, and route during prepare. It derives the
exact `client_user_message_id = "focus-web:<mutation_id>"`; the browser neither sends
nor overrides that value.

For an existing-thread prompt the browser may save only the
`(thread_id, mutation_id)` locator in a bounded current-tab `sessionStorage` map so F5
can query it. It does not save that prompt's payload, attachment receipt, recovery
bearer, server generation, or execute continuation. A locator is not effect authority,
does not participate in `canSubmit`, and cannot construct another POST.
Failure to write `sessionStorage` degrades that locator to current-document memory
only; it never blocks the one POST. The explicit availability cost is that F5 cannot
resume the GET lookup if the original POST result is then lost.

The process-local `WebPromptResultRegistry` is the sole owner of exact mutation result
receipts. It indexes one bounded receipt by exact `(thread_id, mutation_id)` and stores
only identity, route/result coordinates, and status. It stores no prompt payload,
attachment ids, document token, or replayable request. It is neither persisted nor
restored across a Focus service restart and owns neither upstream lifecycle nor the
main turn. A terminal-receipt eviction or post-restart miss means only that Focus no
longer has a local explanation; it never proves that the effect did not occur.

One mutation identity still retained by the registry has one effect slot. During the
receipt-retention window, a duplicate POST can only read the same `pending` or terminal
receipt, or fail closed when identity/scope differs; it cannot obtain a second slot.
Reusing that retained mutation id for another thread, document incarnation, or source
scope fails before every upstream effect.

This is an explicit process-local, retention-bounded guarantee, not a permanent
idempotency key. Terminal eviction, confirmed backend-epoch retirement, or Focus
service restart removes the sole seen-identity evidence. The server then cannot
distinguish a fresh canonical UUID from a client deliberately reusing an expired UUID,
so the latter may acquire a new slot. The official browser never POSTs a locator again:
F5/poll uses GET only, and every new submit gesture creates a new mutation id, so it
does not automatically exercise this bounded non-guarantee. Rejecting the same UUID
after those boundaries would require a durable or unbounded spent-identity authority,
which this contract explicitly does not provide.

## 3. Single-POST Prepare / Effect / Settle

Gateway validates the exact closed body, connected document, and materialized
direct-root target before entering one staged transaction through the service-ingress
barrier:

1. **Prepare (inside RuntimeLoop):** validate and freeze only the Composer receipt's
   shape/identity, exact document/target, mutation identity, server-derived
   client-message id, backend connection generation, read observation, and current
   exact active turn A; this phase does not read `WebWriterProfileStore`. A present A
   freezes `mode=steer` and A; no exact id freezes `mode=start`. Prepare installs or
   reuses the same exact `pending` receipt and immediately releases RuntimeLoop.
2. **Effect (external worker):** a fresh worker first coherently loads one
   `WebWriterProfileStore` snapshot and exactly compares its `selected_thread_id` and
   `scope_generation` with the frozen Composer receipt. Only a passing check
   authorizes the original gesture to proceed. This check happens before attachment
   claim or any turn-producing RPC; failure remains known no-effect. The worker then
   performs the required direct metadata proof, attachment claim, and upstream RPC
   under the original service-ingress receipt and exact backend-generation pin.
   RuntimeLoop waits for neither app-server/store I/O nor the browser document lock.
3. **Settle (inside RuntimeLoop):** use only the original immutable receipt to CAS the
   exact registry generation/observation and install one terminal result. A replacement
   document, backend generation, runtime epoch, or newer observation cannot be
   overwritten by a late result. List/read after settlement is background projection
   convergence only.

`mode=steer` sends the frozen A unchanged as `expectedTurnId` exactly once. It does
not replace or reject A from a paginated or stale turns projection before sending.
A successor-B mismatch, no-active rejection, or any other steer rejection is
authoritative no-effect, settles as `known_no_effect`, and never retargets B or falls
back to `turn/start`. Only an attempt whose prepare found no exact active id selects
`mode=start` and uniquely calls official `turn/start`. That path retains existing
goal/next-turn safety but does not explicitly call `thread/resume`; it adds no local
compare-and-set start, writer lease, or replay guarantee.

The attachment store uses the existing exact submission claim/pin. Known-no-effect
attempts exact rollback before returning that result; known success or outcome unknown
leaves it submitted. If the upstream text effect is authoritatively known-no-effect
but rollback returns false or throws, the receipt remains `known_no_effect` and carries
`reason_code=attachment_rollback_failed`. That code says only that old attachments are
not provably reusable; it does not rewrite the known-unsent text as effect-unknown.
The attachment claim is not a payload store for the result registry, and GET cannot
resend attachments.

Loss of an authoritative response after the effect enters transport settles as
`outcome_unknown`. A normal RPC success, or later authoritative transcript evidence
with the matching server-derived `clientId`, may settle the exact receipt as
`succeeded`. One read that does not contain the id is not no-effect evidence; history
reconstruction may also omit `clientId`. Failure to publish a terminal result or to
refresh projection after the effect cannot rewrite a known success as unsent.

The external worker remains inside the service shutdown barrier from prepare through
settle. Executor-admission failure, handler cancellation, or shutdown may settle
known-no-effect only when the effect boundary is known not to have been crossed; once
transport outcome is ambiguous, only unknown is valid. An old worker cannot settle a
replacement backend/epoch and never automatically replays.

## 4. Result Receipt, F5, and HTTP Wire

POST `/api/threads/{thread_id}/prompt` and the query
GET `/api/threads/{thread_id}/prompt-result/{mutation_id}` use the same exact
`FocusPromptResultReceipt` with these required fields:

- `thread_id`, canonical `mutation_id`, and server-derived
  `client_user_message_id`;
- `status = pending | succeeded | known_no_effect | outcome_unknown`;
- `mode = start | steer`;
- `turn_id`: always frozen exact A for steer. A validated
  `mode=start + status=succeeded` preserves the authoritative `turn.id` from the
  upstream `turn/start` response. It is `""` when `pending`, pre-effect, or unknown
  lacks that evidence; a request/submission tracking id never masquerades as actual
  turn identity;
- `reason_code`: `""` without an additional classification.
  `attachment_rollback_failed` says exactly that the text effect is known not to have
  occurred but the old attachment chips are unsafe to reuse. Other values explain
  only this request; none grants lifecycle or retry authority.

After safety settlement, the original admitted POST returns its terminal receipt with
HTTP 200. A same-identity duplicate may read an existing `pending` or terminal
receipt. Strict validation and document/target/source-scope admission failures remain
effect-free HTTP errors and do not masquerade as result status. Transport loss, a
malformed response, or HTTP 5xx is possibly-sent to the browser and never authorizes
automatic retry.
The browser retains input after an HTTP refusal only when a strictly decoded Focus
error envelope and this prompt endpoint's closed HTTP contract establish explicit
pre-effect evidence. A status code alone, a proxy or non-envelope response, and every
HTTP 408 remain possibly-sent; browser code does not infer no-effect from a 4xx range
or error text.

The POST endpoint's closed pre-effect code set is `unauthorized`, `csrf_failed`,
`invalid_client`, `document_unregistered`, `document_replaced`, `invalid_json`,
`invalid_prompt`, `invalid_mutation_id`, `invalid_attachment`,
`invalid_submission_scope`, `empty_prompt`, `invalid_thread`,
`web_writer_disconnected`, `thread_not_materialized`, and
`prompt_result_capacity`. In particular, `prompt_result_unavailable`,
`prompt_result_pending`, `prompt_mutation_conflict`, and every unrecognized Focus
4xx code remain possibly-sent on POST: each can describe an identity or receipt whose
effect slot may already have run. The lookup-only GET independently treats a strict
`prompt_result_unavailable` as authoritative evidence that the bounded local receipt
can no longer be queried; it is not POST no-effect evidence.

The GET endpoint has no body, bearer, or mutation action. It still requires the
currently authenticated document to be connected and to have materialized the exact
thread; it only reads the bounded registry receipt. F5 and short polling may issue
only this GET. They never issue POST, reserve, execute, resolve, discard, retry,
attachment restoration, or any request that acquires an effect slot. `pending` says
only that the original worker is not terminal. A receipt miss, eviction, or restart
produces unavailable/unknown presentation, opens no retry, and blocks no new mutation.
The browser presents every non-success receipt and every unavailable or transient GET
failure explicitly while stating that it did not replay the prompt. It forgets the
locator only after `succeeded`, `known_no_effect`, or an authoritative
`prompt_result_unavailable`; `pending`, `outcome_unknown`, and transient lookup failure
retain it for a later GET.

A matching transcript `clientId` is positive evidence that upgrades the exact
`outcome_unknown` receipt to `succeeded`. Query and transcript reconciliation never
replay an effect. `known_no_effect` is the only terminal status that permits the live
original Composer to retain this payload as unsent.

## 5. Composer Settlement and Browser Recovery Boundary

The Composer does not clear text/chips before POST. When terminal safety settlement
arrives:

- `succeeded` commits the exact `ComposerSubmission` and clears the still-matching
  press-time payload;
- `outcome_unknown`, transport ambiguity, and decoder ambiguity commit it as
  possibly-sent, show that the result is unknown, and never resend automatically;
- ordinary `known_no_effect` or an authoritative pre-effect HTTP refusal retains the
  exact payload. The corresponding chips remain only after exact attachment rollback;
- `known_no_effect + attachment_rollback_failed` atomically retains text only and
  removes old chips when the exact `ComposerSubmission` is still the current owner.
  If newer input replaced that owner, it does not touch the newer content and instead
  explicitly asks the user to remove and add the old attachments again. If this narrow
  cleanup itself fails, it conservatively commits that exact Composer payload and
  never leaves old chips available for one-click reuse. This commit settles only
  attachment/UI safety and does not claim that the text was sent;
- a late settlement never clears newer Composer content entered by the user.

The HTTP mutation owner returns immediately after this commit/retain. Thread list/read,
turn projection, presentation events, and telemetry converge in the background and
cannot keep the input or send button pending. Projection failure affects display only
and never reverses mutation settlement.

An existing-thread locator stores no payload, so after F5 its GET only explains the
result; it cannot restore the pre-reload text/chips or create a retry draft. The
`known_no_effect` input-retention guarantee applies only to a live document that still
owns the exact `ComposerSubmission`. This is the explicit availability cost of
removing server payload replay and bearer recovery. A user may type again under a new
mutation id, but Focus neither copies nor sends the old payload for them.

The thread-create typed-unknown first-prompt text-only `sessionStorage` record and its
explicit handoff/discard remain intact. It has no existing-thread mutation id,
client-message id, or server receipt, and no reload path may upgrade it into POST
replay.

## 6. Removed Machinery and Unaffected Capabilities

The following old mechanisms are no longer part of the Web prompt contract and must
be removed atomically from production and wire:

- browser/server `dispatch → reserve → execute` and two-phase steer continuation;
- raw `recovery_capability`, server digest, base/reservation generation, and
  per-thread high-water;
- `reserved` / `executing` continuation, server-retained prompt payload, and replay;
- steer-specific resolve/retry/discard, attachment restoration, and backend-replaced
  explanation;
- snapshot `mutation_generation`, `steer_attempt_result`, and browser steer-attempt
  decoder/storage.

Generic `/mutation-unknown` continues to serve archive/unarchive/delete and existing
lifecycle/control mutations only. Its process-local unknown records, dispositions,
and UI vocabulary are unchanged. The thread-create local record is not merged into
the prompt-result registry.

An ordinary Web prompt neither reads nor acquires the main-turn lease and changes no
Feishu FIFO, explicit `/steer`, fcodex realtime input, shared approval, interrupt,
settings, goal, binding, or backend-reset authority. An old `pending` or
`outcome_unknown` receipt explains only its exact mutation and cannot block a new
mutation on the same thread or any other surface.

## 7. Verification and Stop Conditions

Regression covers at least: one POST; RuntimeLoop sentinel and realtime admission
while a slow RPC is pending; start/steer routing; exact-A success; successor-B
mismatch without retargeting; no-active known-no-effect without fallback; attachment rollback on
known-no-effect; submitted attachments on success/unknown; RPC success, typed unknown,
and positive transcript reconciliation; no second effect for a duplicate while its
receipt is retained, with expired-UUID reuse after eviction/backend retirement/restart
recorded as the explicit bounded non-guarantee; document/backend/runtime ABA;
cancellation, executor failure, and shutdown barrier; lost POST response; GET-only
F5/polling; malformed response; immediate Composer commit/retain without projection
refresh; no late clear
of newer Composer content; availability of a new same-thread mutation, other threads,
and other surfaces after unknown; and zero server replay for thread-create local
recovery.

Stop and realign if correctness requires a durable effect ledger, cross-process
idempotency/replay, negative-effect evidence unavailable from upstream, or spreading
one exact prompt's unknown outcome into thread- or service-wide unavailability.
