# Main-Turn Owner Contract

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/root-operation-owner.zh-CN.md`.

> The historical filename is retained so existing links keep working. This
> document no longer defines a root-operation writer that settles the root and
> all descendants together. It defines only Focus's minimal addition to
> upstream Codex: an Feishu or non-ordinary-prompt action that still explicitly
> requires serialization has at most one admission holder on a root thread.
> Ordinary Web/`fcodex` prompt is upstream-routed realtime input and neither reads nor
> acquires that holder. An active turn may accept multiple exact contributors,
> but a contributor does not become its writer.

## 1. Purpose and Scope

Upstream Codex ends a main turn immediately after the matching
`turn/completed`. Spawned subagents may continue on their own threads, but
they do not extend the root main turn or prevent the next user submission.

Focus additionally exposes the same shared app-server thread to Web, Feishu,
and `fcodex`. The shared lease serializes local submissions and active turns
that Focus has already observed, but it cannot turn official upstream
`turn/start` from start-or-steer into an atomic compare-and-set start. In the
narrow ordering window where Web, `fcodex`, or autonomous goal continuation
after resume becomes active upstream before the exact active-turn fact reaches
Feishu admission, Feishu's `turn/start` input and turn settings join that
active regular turn. This contract records that residual upstream race instead
of adding a private RPC or a second state machine to eliminate it.

Focus therefore retains a shared lease only for paths that still require
next-turn/FIFO or an exclusive action. It no longer serializes ordinary Web or
`fcodex` prompt as a start writer. During single-POST prepare, the Web server
freezes an exact active id: its external effect steers that exact turn when the
id exists and otherwise uses official `turn/start`;
`fcodex` passes admitted native `turn/start` params unchanged to the same
upstream start-or-steer semantics. This contract does not combine children,
interactions, delivery, presentation, goals, sockets, or cleanup into a larger
operation.

It covers:

- ordinary prompt, review, compact, and active-turn control from a Feishu
  binding;
- ordinary prompt, review, compact, and steer/interrupt from a Web document;
- the corresponding main-turn RPCs from an `fcodex` participant/connection;
- contention between those surfaces within one Focus instance.

Another Focus instance, an independently launched raw Codex client, thread
create, resume, goal mutation, server requests, and destination liveness have
separate contracts. None may expand the main-turn writer lifetime defined
here.

## 2. Core Rule

A root thread may have many observers and exact realtime contributors. A shared
lease authorizes only an action that still declares exclusive/next-turn semantics;
it is not an ordinary-input mutex and grants no exclusive active-turn steer,
interrupt, or approval authority:

- idle has no main-turn owner;
- before a turn-producing RPC that still declares Feishu next-turn/FIFO or
  exclusive semantics leaves Focus, the frontend acquires an exact blank
  submission lease;
- a known-success response for ordinary `turn/start` preserves the upstream
  `turn.id` turn identity. The response alone neither activates nor transfers a
  Focus lease and establishes no lifecycle/completion authority across a
  notification gap or reset. The same `FeishuRootOperationController` may retain
  it only as a one-shot, process-local interrupt candidate for that exact prompt admission;
- after matching `turn/started` yields the actual `turn_id`, the same lease
  becomes the active-turn lease;
- the lease holder remains the writer; a qualified live/attached `fcodex`
  endpoint or connected/materialized Web document may steer with
  effect-specific exact-turn authority, while trusted-local ordinary interrupt
  uses its separate exact-turn authority; none transfers the writer;
- matching `turn/completed(thread_id, turn_id)` releases the lease
  immediately;
- no surface has an authoritative receipt correlating an unbound completion to
  one ordinary `turn/start` submission. A completion therefore cannot bind or
  release any blank lease. If matching `turn/started` was missed, the blank
  remains fail-closed until authoritative terminal evidence or service restart;
- Feishu's `FeishuRootOperationController` may additionally settle its exact
  current ordinary-prompt blank after rereading the root as authoritatively
  inactive. That settlement proves only that no main turn remains; it does not
  attribute a completion to the submission;
- after matching completion, another surface may start the next turn without
  waiting for children, cards, delivery, interactions, or retained cleanup.
- ordinary Web/`fcodex` prompt acquires no blank or active lease. During
  RuntimeLoop prepare the Web server freezes an exact-id steer route or, without
  an id, an upstream `turn/start` route. The effect runs externally and returns
  to the loop for exact receipt settlement; ordinary fcodex start records only an exact request
  token. An existing lease, blank, or foreign holder cannot produce a
  cross-surface writer denial on either path.

A writer denial is not itself queue admission, implicit steering, or automatic
handoff. Only the separately proven Feishu binding FIFO may retain next-turn
input, and its queued item owns no writer before real dequeue. The rule is per
root thread, not a service-wide single-user lock.

## 3. Single Fact Source and States

`InteractionLeaseStore` is the sole main-turn writer fact. It needs no
separate four-state writer machine:

| State | Store fact | Permitted behavior |
| --- | --- | --- |
| idle | no lease for the thread | a Feishu/exclusive action may compete for a new submission; ordinary Web/`fcodex` input needs no lease |
| submission | lease exists and `turn_id` is empty | that exact exclusive/Feishu generation is submitting or awaiting authoritative identity/terminal reconciliation; ordinary Web/`fcodex` prompt may still request upstream-routed input; Section 6 retains one-shot candidate interrupt for the same exact Feishu prompt admission |
| active | the same lease has a non-empty `turn_id` | the holder retains its exclusive/Feishu activity identity; a connected/materialized Web document or qualified live/attached `fcodex` endpoint may contribute exact-turn input or interrupt the current turn; none transfers the holder |

A lease generation is named by `lease_id`; full-record CAS compares the entire
immutable `thread_id + holder + lease_id + updated_at + turn_id` value.
Activation and recovery compare the exact `lease_id` generation; an old
response, old notification, or holder with the same logical name cannot
replace a successor lease. Matching completion releases only the exact
`thread_id + turn_id`, preventing an old terminal event from releasing the
next turn.

Feishu's opaque admission/continuation token is a local transaction receipt,
not a second writer fact, and it cannot be recovered from projection or restart.
It lets `FeishuRootOperationController` retain and later settle only the still
fully-equal exact blank lease, including after an authoritative inactive-root
reread, but it does not correlate an arbitrary completion to that submission.
The current shared writer fact remains solely in `InteractionLeaseStore`. See the
[Feishu thread lifecycle contract](feishu-thread-lifecycle.md).

An ordinary prompt admission may also retain the response `turn.id` inside that
controller as one process-local interrupt candidate. The candidate preserves the
exact turn coordinate returned by upstream for that known-success RPC. It is not a
Focus lease, matching notification/completion fact, FIFO continuity, snapshot/log mutable fact,
or persisted fact. After the existing audit point is crossed, only the
redacted short hash of the exact id is logged, never candidate state or the raw
id. Matching actual lifecycle, owner loss, admission finish, or root-terminal
cleanup clears it. An unbound `turn/completed` cannot acquire correlation
authority from the candidate, and service restart never reconstructs it. Each
admission installs at most once; claim atomically empties the slot, and the
same token cannot re-arm after consumption.

Every main-turn holder is bound to the current Focus service PID and process
identity:

- Feishu: sender/binding chat identity;
- Web: document `client_id`;
- `fcodex` exclusive/autonomous submission: participant incarnation and exact
  connection.

A PID-0 or cross-restart retained holder is not a main-turn writer. After a
service restart, stale PID leases are pruned; Focus does not reconstruct a
writer from an old socket, document id, binding, or retained record.

A backend reset inside the same service process does not naturally change the
PID, so it uses a narrower confirmed-stop transaction. After ingress is fenced
and before stopping the old child, Focus read-only captures full leases whose
`owner_pid` and `owner_process_identity` match the current process. Only after
owned-child OS exit/wait is confirmed does it CAS-retire that entire captured
`thread_id + holder + lease_id + updated_at + turn_id` value. A missing or different full
record means the capture was already retired or a successor took over and must
not be cleared; other PIDs and PID zero never enter the capture. If capture,
stop, or retirement cannot be proved, ingress stays fenced and no replacement
starts. This rule is not writer handoff and never recovers or replays an old
submission.

## 4. Submission, Unknown Outcomes, and Release

A Feishu or exclusive submission that requires a lease uses the following
minimal transaction:

1. acquire a new blank lease before sending the start RPC;
2. a known local refusal or known upstream rejection releases only that exact
   blank lease;
3. an ordinary `turn/start` known-success response carries the authoritative
   `turn.id` returned by upstream for that RPC and preserves it unchanged. The
   response alone neither activates/transfers a Focus lease nor establishes
   matching notification/completion or lifecycle authority across reset. Feishu
   may retain it only as Section 6's one-shot interrupt candidate for the same
   exact prompt admission;
4. matching `turn/started` binds the same blank lease to the actual `turn_id`;
5. an unknown `turn/start` or other turn-producing effect, or a known-accepted
   submission whose identity is not yet bound, keeps only its PID-bound blank
   lease while awaiting authoritative lifecycle or terminal evidence;
6. matching `turn/completed` releases only an already-bound active lease with
   exact matching `thread_id + turn_id`. This is the ordinary active path for
   every surface;
7. if matching `turn/started` already passed or was missed, no surface has
   enough effect-correlation evidence to attribute any completion to the blank
   generation. It remains fail-closed until authoritative terminal evidence.
   Web/`fcodex` exclusive/autonomous blanks retain it until an exact thread terminal such as
   `thread/closed`, archive/delete, or service restart. During that interval,
   new main-turn admission to the same thread is denied, but other threads are
   unaffected;
8. Feishu keeps one narrower availability path: an exact process-local awaiting
   admission token from the same `FeishuRootOperationController` permits an
   exact reread of authoritative inactive root status to release only the still
   fully-equal blank. This does not bind a turn id or claim which completion
   belongs to the submission. Token mismatch, replacement/ABA, no local
   awaiting admission, or a blank owned by another surface cannot use the path;
9. exact thread-terminal facts such as `thread/closed`, archive, or delete
   may clear the thread's lease.

An ordinary Web prompt explicitly does not enter that transaction. Its one POST
prepares the exact browser mutation/route inside RuntimeLoop, executes at most
one upstream input effect in an external worker, and returns with an immutable
receipt for exact loop settlement. It neither acquires nor retains the main-turn
lease. A typed unknown observed inside the RPC and loss of the first HTTP
response are explained only by the bounded exact result receipt/browser locator
and must not be repackaged as a blank writer. An old unknown is never replayed
automatically, but it also does not reject a new explicit input with a new
mutation id.

Ordinary `fcodex turn/start` likewise does not enter that transaction. It
preserves native params, records only the exact participant, connection,
request id/token, and settles the response or connection loss without reading,
acquiring, or releasing a main-turn lease. Admission and request settlement do
not change an existing lease. If an fcodex exclusive/autonomous blank exists
concurrently, however, a later `turn/started` has no effect identity correlating
it to one call and may still activate that blank. This accepted narrow upstream
race is not eliminated with a new correlation state machine.

Web `thread/resume` has one method-specific exception that does not change the
turn-producing rules above. If the resume response is unknown, no later
prompt/review/compact effect was called, and an exact receipt proves that this
call freshly acquired the blank, Web releases it with a full-generation CAS
while retaining read-only runtime interest. Borrowed, pre-existing, activated,
or replaced leases are not released. An acknowledged resume whose local commit
failed is not transport-unknown and continues through the existing `RETAIN` /
`COMPENSATE` settlement and surface rules. This exception does not change
acknowledged settlement: only a `recovery_required` or stale/invariant-violation
incomplete settlement retains the corresponding Web blank; another settled
known failure still follows the existing surface rule.
This lets an explicit retry compete for admission, but Focus never retries or
takes over automatically. The honest cost is a very
narrow window: the first resume may have autonomously started work while
`turn/started` is not yet visible. Between blank release and that lifecycle
event, an explicit retry can meet the upstream work. Focus lacks correlation
evidence to eliminate this window and creates no durable writer or recovery
state for it.

A confirmed backend reset also asks each surface owner to idempotently retire the
old backend's process-local admission/attempt lineage. That local retirement and
the centralized lease CAS are one ordered transaction across the original
owners; fcodex, Web, and Feishu must not each scan and release a surface-only
lease subset.

Step 7 is an explicitly retained lifecycle-observation residual and availability
cost. Focus temporarily denies the next start on that same thread instead of
guessing release from a completion that cannot be correlated to the effect.
Feishu may shorten that interval only with the exact inactive-root reread in
step 8. It triggers no automatic retry, replay, or wider quarantine.

These rules are method-specific. At the pinned upstream revision, the inline
`review/start` response id is the actual inline-review turn id and remains
response-specific activation evidence; `fcodex` permits inline review only.
`thread/compact/start` has no turn id in its response and keeps its existing
lifecycle path. A known-accepted compact is not eligible for the inactive-root
reread until its identity wait has timed out into an unknown outcome; before
then a stale idle observation cannot settle it. Ordinary `turn/start` likewise
preserves the authoritative response `turn.id`, but only matching `turn/started`
activates the Focus lease in this contract. Response identity, lease activation,
and completion correlation are three distinct facts.

An unknown submission does not create a durable root fence or quarantine other
threads. It blocks only another submission to the same thread while the prior
start may already have taken effect. A service restart removes the old blank
lease; Focus uses upstream list/read/resume and later lifecycle facts to
recover visibility, rather than reconstructing the old writer.

Frontend disconnect, binding/document loss, or card-send failure does not
turn an active lease into `grace`, `orphaned`, or `stopping`. A known
unsent blank submission may be removed with its exact receipt; an unknown
turn-producing blank submission reconciles only from the authoritative
evidence above. Web/`fcodex` exclusive/autonomous blanks retain it until matching started, exact thread
terminal, or a process-generation change; Feishu adds only the narrow
inactive-root reread allowed by its exact process-local admission contract.

If the lease store cannot be read reliably, the relevant main-turn
admission/control is denied. Focus must not guess idle or use a retained record
as substitute evidence.

## 5. Facts That Do Not Extend a Main Turn

The following facts may have their own owners, recovery, and delivery
contracts, but none extends the main-turn lease:

- whether a spawned subagent remains active, arrives late, or has been
  completely discovered;
- pending approval or user-input interactions;
- Feishu queued items and same-binding FIFO continuity;
- Web, Feishu, or fcodex subscriber/document/socket liveness;
- execution cards/pages, terminal messages, images, delivery receipts, or
  send-unknown outcomes;
- local transactions for goals, resume, create, settings, or lifecycle
  mutation;
- thread-runtime leases, backend generation, and cross-instance runtime
  protection;
- transcripts, projections, telemetry, cleanup, and process-local recovery
  receipts.

Focus does not persist a root writer or reconstruct one after restart. Goal,
resume, create, and control-mutation uncertainty may keep only the exact
current-process request/effect needed to explain or reconcile that call. Such
evidence is not a writer, is not replayed, and cannot quarantine unrelated
threads or surfaces.

## 6. Ordinary Stop

Ordinary stop is exactly upstream-style active-turn interrupt, separated from
main-turn writer authority:

- ordinary Feishu cancellation first uses the exact actual active `turn_id`
  already held by the execution/lifecycle owner. Only while the execution is
  running and a matching lifecycle id is still absent may it claim the response
  `turn.id` candidate once from the exact prompt admission for the same
  binding/root. This contract does not expand Feishu input, cards, or
  administrator identity into generic stop authority;
- a connected Web document which has materialized the same exact direct root,
  and a live `fcodex` endpoint attached to that exact direct root, may interrupt
  its current turn even when it is not the initiator/writer;
- Web carries an unmodified exact `thread_id + turn_id` when it has an exact id.
  While active/submitting but the identity is not yet visible, it explicitly
  carries an empty `turn_id` and uses the pinned Codex 0.147.0 current/startup
  interrupt semantics. Web proves its materialized direct root with
  `thread/read(includeTurns=false)` and neither checks nor rewrites the id from a
  turns projection. `fcodex` likewise forwards exact-or-empty raw ids
  semantically unchanged. A non-empty id accepts either a current connection
  source or a matching exact active fcodex lease as attachment proof; an empty
  id accepts only the current connection's own exact-root runtime source, and
  no blank or active lease may substitute;
- the initiator lease is at most attachment proof here. It neither grants an
  exclusive interrupt effect nor transfers, releases, or replaces the writer;
- a non-empty id sends `turn/interrupt` only to that exact turn; an empty id
  uses only the pinned upstream current/startup path. A stale, terminal, or
  mismatched non-empty id is rejected by the upstream-owned exact-ID rejection
  boundary and is never retargeted to a successor turn. At the pinned upstream
  revision, interrupt checks the id under the app-server projection lock and
  later dispatches an id-less core interrupt outside that lock, so this
  contract does not claim a core-atomic compare-and-effect boundary;
- after the exact control path selects the turn, add neither a descendant scan
  nor a root/tree-stop fallback. Spawned children do not enter the local shared
  interrupt domain and require their own independent control contract.

Before a Feishu candidate call, the admission owner claims it exactly and
removes it from the slot. Only a typed pre-send failure before
`turn/interrupt` crosses its dispatch boundary may restore both the candidate
and pending cancel, and only while that same claim/admission remains current,
for a later explicit `/cancel` or actual lifecycle. A known exact-ID rejection,
known RPC response, or post-dispatch unknown outcome consumes the candidate.
With neither an actual id nor a candidate, the command reports that this call
did not cancel and retains only local cancel intent; it must not say "stop
requested." A consumed candidate is never automatically dispatched again when
`turn/started` later arrives.

A successful interrupt RPC response proves only that the request crossed the
dispatch boundary and the target later became terminal; it does not prove an
`interrupted` terminal status. A post-dispatch transport/protocol uncertainty
is reported separately as "possibly sent, result unknown." Only matching
`turn/completed.status=interrupted` marks the execution cancelled; every other
terminal status keeps its actual meaning.

To diagnose which local surface reached an interrupt boundary, the Web
document, Feishu binding, and `fcodex` endpoint paths each write one
best-effort, redacted `turn_interrupt_dispatch_attempt phase=attempt` to the
existing bounded process log after their own admission has selected the exact thread/turn and
immediately before the last local effect boundary. `source` comes only from a
closed internal vocabulary; Focus never reads it from an HTTP, JSON-RPC,
Feishu, or other client-supplied field. The record contains only the source and
short hashed thread/turn references, never full ids, user/chat identity,
prompt, body, token, or capability.

This record proves only that Focus reached a local dispatch attempt. It does
not prove that upstream received, accepted, or settled the interrupt. It
creates no durable journal or runtime fact, participates in no writer,
admission, retry, lifecycle, or outcome settlement, and logging failure cannot
block the effect. Backend reset remains a separate control contract rather
than masquerading as an ordinary `turn/interrupt` source. A client which writes
the app-server while bypassing Focus likewise has no local source evidence, and
Focus does not guess one.

Focus has no generalized `operator-stop`, descendant scan, durable stop
journal, or atomic tree-stop product path. It does not claim evidence that
upstream Codex does not provide. An unknown interrupt result may retain only
that exact current-process request/effect for reconciliation; it grants no
takeover authority and does not block other threads.

## 7. Shared Trust Is Not a Writer

Connecting surfaces to one shared backend is necessary to avoid runtime forks,
but it does not grant writer authority. An observer, another tab/socket for the
same person, the same Feishu user, or the local control plane cannot acquire an
exclusive writer merely by being connected. Ordinary Web/`fcodex` realtime dispatch,
shared Web/`fcodex` exact-turn steer, interrupt under Section 6, and shared
server-request response under the server-request contract are minimal effect-specific
authorities. None is a takeover: an ordinary prompt authorizes only that one
upstream start-or-steer input, and none grants settings, goal, binding,
lifecycle, or child control.

A server-selected Web prompt uses one process-local POST with
`prepare → external effect → exact settle`; a bounded result receipt gives one
effect slot to one mutation identity only while that receipt remains retained.
Terminal eviction, confirmed backend retirement, or service restart removes the
seen-identity evidence, so the same UUID may then acquire a new slot. Across
those boundaries the official browser only GETs or creates a new UUID for a new
gesture; F5 cannot reserve, execute, resolve, or replay. This boundary changes
no main-turn lease and expands no other capability. See the [Focus Web prompt mutation recovery
contract](focus-web-prompt-mutation-recovery.md). The non-empty exact
`expectedTurnId=A` frozen at prepare remains semantically unchanged. Focus still
proves a connected/materialized direct root, cwd/attachment/source scope,
runtime/backend generation, and exact mutation settlement, but does not use a
projected active id from `thread/read(includeTurns=true)` as a pre-send veto.
If upstream core reports successor B or no active turn, this request settles
known-no-effect and never automatically retargets B or falls back to
`turn/start`. Post-effect unknown can be confirmed
only by positive evidence such as a matching server-derived `clientId` and never
becomes pre-send lifecycle authority.

Explicit Feishu `/steer <text>` is another method-specific exact-turn
contribution authority, not an ordinary prompt or writer handoff. It accepts
only the non-empty exact turn mirrored by the current attached/running binding,
including an active observer. After group-`all` exclusivity, an authoritative
direct-root active reread, a backend connection-generation fence, and final
binding/execution CAS, it calls official `turn/steer` once. It acquires no
lease, enters no FIFO, carries no next-turn settings, consumes no attachment,
and neither falls back nor retries automatically. A known rejection reports no
contribution; an unknown after dispatch reports possibly sent and never
retargets a successor. See [Section 5.3.2 of the Feishu thread lifecycle
contract](feishu-thread-lifecycle.md#532-explicit-steer-exact-turn-contribution)
for the complete boundary.

Shared exact-turn authority does not make Focus deliberately send an ordinary
Feishu prompt through `turn/steer`. Every ordinary Feishu prompt sent
upstream--immediate, dequeued, or synthetic--uses official `turn/start`. It
normally starts a new turn while upstream is idle. If Web, `fcodex`, or
autonomous goal continuation after resume becomes active first in the narrow
window, upstream start-or-steer adds the Feishu input and turn settings to that
active regular turn. Upstream has consumed that input; it is not a queue
refusal and must not be resent. `/compact` keeps its separate contract.

A normal Feishu prompt may enter one exact binding's process-local FIFO and run
as a next turn after matching terminal settlement. Admission uses only that
binding's exact execution anchor, existing same-binding/root/epoch continuity,
or a current-process, non-Feishu, non-empty exact active lease re-read unchanged
under the shared binding lock before projection. Ordinary Web/`fcodex` prompt
creates no such preprojection proof; after the Feishu mirror has a non-empty
exact turn, however, its execution anchor is sufficient without an origin
lease. A `turn/start` response, its returned `turn.id`, writer denial, or absence of a
locally visible lease does not establish FIFO continuity. At most one Feishu
binding retains continuity for a root; later items keep same-binding/root/epoch order. Another binding,
foreign/stale process evidence, blank or mismatched turn evidence, and
`/compact` remain denied. No timer, scheduler, persistence, spin, or automatic
resend supplements the matching lifecycle-terminal wake-up.

The active-turn disclosure in Focus Web Runtime Details is read-only
presentation, not authority. It
classifies an initiator only when the main-turn lease matches the authoritative
active turn id, and lists only currently attached Feishu subscribers as the
audience. `turn/started` freezes the connection-local thread base then proved
by a response/settings event. Concrete values and known nulls are `inherited`,
missing fields are `unknown`, and later base changes do not backfill the active
snapshot. Only a matching model reroute is `active_reroute`; instance-wide
`WebNextTurnSettings` or Feishu next-turn settings must never be presented as
active settings.

The browser orders this disclosure with process-local revision floors, not a
second runtime owner. Every `owner_changed` or `thread_invalidated` event
advances the exact thread's floor. Turn start/completion or active-turn identity
change, `model/rerouted`, `thread/settings/updated`, archived/closed/deleted
lifecycle, and a non-active thread status do the same. `backend_disconnected`
and `projection_invalidated` advance a global floor. A response behind unrelated
newer stream state may replace only the exact matching `active_turn_context`
when it covers the applicable thread and global floors. It cannot roll back any
other snapshot field, trigger a disclosure polling/retry loop, or authorize
writer, lifecycle, settings, approval, FIFO, or mutation effects.

Conversely, an idle thread is not retained by a frontend that once owned it.
No active/submission lease means idle at the main-turn layer. Any exact
mutation or recovery gate in another domain must justify its own effect; it
must not be repackaged as a durable root writer.

## 8. Upstream Evidence and Implementation

This contract is pinned to public upstream
[`openai/codex@be6e8eac029b183056b7e4402879f15d2c85f61b`](https://github.com/openai/codex/commit/be6e8eac029b183056b7e4402879f15d2c85f61b)
(release `rust-v0.147.0`) and does not cite a machine-local checkout:

- [`turn_processor.rs#L474-L607`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L474-L607)
  returns the upstream `turn.id` unchanged for ordinary `turn/start`;
- [`session/mod.rs#L789-L830`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/session/mod.rs#L789-L830)
  creates that turn identity, while
  [`handlers.rs#L189-L270`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/session/handlers.rs#L189-L270)
  performs the actual start-or-steer admission. The idle-path test
  [`turn_start.rs#L227-L253`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/tests/suite/v2/turn_start.rs#L227-L253)
  proves that response, started, and completed ids coincide when no race occurs;
  Focus still does not activate a lease from the response alone;
- [`turn_processor.rs#L1249-L1268`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L1249-L1268)
  and [`review.rs#L116-L179`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/tests/suite/v2/review.rs#L116-L179)
  establish that the inline-review response id is the actual turn id used by
  matching review lifecycle;
- [`thread_processor.rs#L1876-L1887`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/thread_processor.rs#L1876-L1887)
  establishes that compact returns an empty response;
- [`turn.rs#L175-L203`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L175-L203)
  fixes the non-empty exact `expectedTurnId`, input, and response `turnId`
  protocol for `turn/steer`;
- [`tasks/mod.rs#L783-L824`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/tasks/mod.rs#L783-L824)
  and [`thread_events.rs#L124-L139`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/tui/src/app/thread_events.rs#L124-L139)
  clear the active main turn after matching terminal lifecycle;
- [`turn_processor.rs#L1409-L1471`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/request_processors/turn_processor.rs#L1409-L1471)
  checks the exact active-turn projection for a non-empty interrupt under the
  app-server lock, then dispatches an id-less core `Op::Interrupt` outside that
  lock;
- [`bespoke_event_handling.rs#L1499-L1522`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/bespoke_event_handling.rs#L1499-L1522),
  [`#L187-L202`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/bespoke_event_handling.rs#L187-L202), and
  [`#L1096-L1112`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/src/bespoke_event_handling.rs#L1096-L1112)
  show that both natural completion and aborted terminal paths finish the
  interrupt RPC, while only the aborted lifecycle projects
  `turn/completed.status=interrupted`; `{}` is therefore not confirmed
  interruption;
- [`subagent_notifications.rs#L1611-L1707`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/tests/suite/subagent_notifications.rs#L1611-L1707)
  covers a parent follow-up turn and later mailbox delivery from a still-running
  child.

Primary implementation locations:

- `bot/stores/interaction_lease_store.py`
- `bot/feishu_root_operation_controller.py`
- `bot/web_runtime/operation_service.py`
- `bot/web_runtime/mutation_recovery.py`
- `bot/active_turn_disclosure.py`
- `bot/feishu_execution_queue_service.py`
- `bot/feishu_turn_steer.py`
- `bot/thread_access_policy.py`
- `bot/thread_runtime_authority.py`
- `bot/fcodex/main_turn_owner.py`
- `bot/adapter_event_bridge.py`
- `bot/adapter_notification_pipeline.py`

If an implementation or another active document still treats retained
root/descendant state, presentation, or endpoint liveness as ordinary
main-turn writer authority, this contract governs. The difference is contract
drift to remove, not a compatibility requirement.
