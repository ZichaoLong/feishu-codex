# Feishu Raw Card Retrieval, JSON 2.0 Terminal Cards, and Forwarded-Card Read Decisions

Document role: synchronized English peer. Canonical Chinese: `docs/decisions/feishu-raw-card-retrieval.zh-CN.md`.

See also:

- `docs/decisions/feishu-card-text-projection.md`: current best-effort text projection boundary
- `docs/architecture/focus-design.md`: current architecture and module boundaries
- `docs/contracts/feishu-thread-lifecycle.md`: execution-card and terminal-finalization lifecycle
- `docs/doc-index.md`: document index

## 1. Problem Statement

Users want both of these outcomes at the same time:

- terminal cards should display headings, lists, quotes, code, and links correctly in Feishu
- after direct send, direct forward, or merge-forward, FOCUS should still read the card as faithfully as possible instead of falling back to text guessing

Two oversimplified claims appeared in earlier discussion:

- "JSON 1.0 is better for faithful reads, while JSON 2.0 only improves display"
- "once a message is `merge_forward`, we directly have the full original card JSON"

Neither is accurate.

With the current Feishu API contract:

- default card returns are receive-side projections, not the original sent card JSON
- `message/get` and `message/list` can return the original card JSON when `card_msg_content_type=user_card_content` is requested
- that capability covers both card JSON 1.0 and 2.0
- the outer `merge_forward` message body is fixed as `Merged and Forwarded Message`
- merge-forward should be handled by expanding child messages first, then querying those child messages individually

So the real design question is not "1.0 vs 2.0", but:

- when should the system prefer `message_id`-based raw-card retrieval
- when is only best-effort projection available
- how should the repository record what was actually received across restart, forwarding, cross-session reads, and incomplete historical logs

## 2. Decision Summary

This repository adopts the following decisions:

1. Terminal-result cards use JSON 2.0.
2. Faithful reads should not depend on default event bodies or default history-list shapes; they should prefer:
   - the target `message_id`
   - `message/get` or `message/list`
   - `card_msg_content_type=user_card_content`
3. The read architecture is three-tiered:
   - exact lookup by `message_id`: read raw card
   - `merge_forward`: expand children, then try raw-card reads on those children
   - everything else: best-effort projection
4. `merge_forward` is not the original full card JSON itself; it is only the entry point into child-message expansion.
5. Ordinary forwarding does not guarantee preservation of the original source message ID, but if the forwarded message itself is still `interactive`, its own `message_id` may still be enough to read the full card JSON.
6. `/last text` remains a fallback convenience path, not the only authoritative path.
7. This phase does not introduce a new `/text` command; priority goes to directly reading the forwarded card itself.
8. For restart-safe verification, the system provides explicit ingress observations behind
   a strict boolean configuration switch:
   - raw event `msg_type`
   - outer message `message_id`
   - child `message_id` values obtained after `merge_forward` expansion
   - whether raw card JSON was obtained
   - whether the final path used raw-card retrieval or projection fallback

## 3. Why JSON 2.0 Plus Raw-Card Retrieval

### 3.1 JSON 1.0 Mainly Fails at the Display Layer

In the current project, Feishu client support for JSON 1.0 markdown-subset headings is weak on the terminal-card body path.

That causes two direct costs:

- `#` and `##` style heading levels render poorly for users
- send-side display sanitization becomes necessary, which folds information

So the real advantage of staying on JSON 1.0 is not stronger fidelity by itself, but only:

- the existing best-effort projection path already knows how to consume it
- default history shapes are more likely to produce usable text projections

That is not a strong long-term design advantage.

### 3.2 JSON 2.0 Mainly Improves Display and Structure

JSON 2.0 is better for:

- structured terminal output
- correct display of heading levels, lists, quotes, code, and links
- a single card contract that serves both user-visible rendering and machine-readable structure

So terminal-card display should prefer JSON 2.0.

### 3.3 Fidelity Depends on Raw-Card Retrieval, Not on 1.0 vs 2.0

If the system only consumes:

- the receive event body
- default `message/list`
- the current `project_interactive_card_text(...)`

then both 1.0 and 2.0 are still using Feishu's projected receive shape. That is a projection path, not a high-fidelity read path.

The path becomes a raw-card read only when `card_msg_content_type=user_card_content` is requested.

At that point:

- both 1.0 and 2.0 can be read faithfully
- 2.0 is no longer inherently weaker than 1.0

So the real boundary is:

- whether a usable `message_id` exists
- whether the system actually performed raw-card retrieval

## 4. Terms

### 4.1 Default Projection Read

This means consuming:

- the default `content` inside receive events
- or the default card shape returned by `message/list` or `message/get`

and then extracting text through the repository's projection logic.

This is best-effort and does not promise full fidelity.

### 4.2 Raw-Card Read

This means calling:

- `message/get`
- or `message/list`

with:

- `card_msg_content_type=user_card_content`

so the original sent card JSON is returned.

### 4.3 High-Fidelity Read

In this decision, "high fidelity" means:

- the terminal body can be recovered
- heading levels can be recovered
- lists, quotes, code, and links can be recovered
- recovery comes from card structure rather than plain-text guessing

This does not require byte-for-byte restoration of the original markdown string.

### 4.4 Ordinary Forward

This is Feishu's normal "forward message" behavior that creates a new message.

It has its own `message_id`. The public contract does not guarantee preservation of the original source message ID.

### 4.5 `merge_forward`

This is Feishu's merge-forward message type.

Its outer message content is fixed as:

- `Merged and Forwarded Message`

The correct follow-up is to query and process its child messages.

## 5. Contract Boundaries

### 5.1 `message/get` and `message/list`

The current Feishu contract states:

- without `card_msg_content_type`:
  - callers get the default card shape
  - callers do not get the original sent card JSON
- with `user_card_content`:
  - callers get the original sent card JSON
  - this applies to both 1.0 and 2.0 cards

So the earlier assumption that "JSON 2.0 cannot be read back faithfully" should be treated as obsolete.

### 5.2 `merge_forward`

The current Feishu contract states:

- a merge-forward message body is fixed as `Merged and Forwarded Message`
- child messages can be retrieved through the message-content APIs
- `message/get` on a merge-forward returns one outer merge-forward item plus child items
- child items include `message_id`
- merge-forward scenarios may also return `upper_message_id`

But the contract does not promise:

- that a child `message_id` is always identical to the original source message ID
- that every message type remains information-complete after merge-forwarding

So the repository may only claim:

- `merge_forward` provides an official child-expansion path
- it does not prove "merge-forward can never lose information"

### 5.3 Ordinary Forward

Feishu's ordinary forward contract states:

- forwarding creates a new message
- the new message has its own `message_id`
- the new message type may still be `interactive`

But the contract does not promise:

- the original source message ID
- a universal source-reference metadata field

So the formal boundary is:

- the original source message ID may be lost
- if the forwarded message itself remains `interactive`, its own `message_id` may still be enough to retrieve the full card JSON

## 6. Read Architecture Decision

### 6.1 Overall Rule

Read paths should not branch on "1.0 vs 2.0". They should branch on authority and read fidelity:

1. local terminal result store hit: authoritative terminal text
2. raw card JSON by `message_id`: raw-card projection
3. remaining cases: payload / best-effort projection

### 6.2 Ordinary `interactive` Messages

When an ordinary `interactive` message arrives:

1. first query `message/get` with that message's own `message_id` and
   `card_msg_content_type=user_card_content`
2. if raw card JSON is returned, project it through the repository card contract
3. for new terminal result cards, only a local terminal result store hit for
   `fc_tr_<result_id>_<checksum>` yields authoritative text
4. store-missed new terminal cards, legacy marker-only terminal cards, and ordinary
   interactive cards remain non-authoritative projections
5. otherwise, fall back to payload / best-effort projection

The key point is:

- ordinary forwarding does not need the original source message ID to be useful
- it is enough that the newly forwarded `interactive` message can still be queried as a complete card
- high-fidelity raw-card retrieval is not the same as terminal text authority

### 6.3 `merge_forward`

When a `merge_forward` message arrives:

1. do not treat the outer message body as meaningful content
2. expand child messages first
3. for each child:
   - if the child is `interactive`, prefer raw-card retrieval by that child's `message_id`
   - otherwise consume the child's ordinary message content
4. then aggregate child messages into the forwarded-message read surface

### 6.4 Remaining Cases

If the repository cannot get:

- a usable `message_id`
- or raw-card retrieval for that message fails

then the system should explicitly downgrade to projection fallback.

Projection fallback remains an important compatibility path, especially for:

- cards sent by other bots
- historical messages stored before this repository had raw-card support
- partially available history records

## 7. Terminal-Card Protocol

### 7.1 One Authoritative Body Copy

The formal contract for the current JSON 2.0 terminal result card is:

- keep only one authoritative body copy
- do not add a second hidden copy of the same body merely out of concern that
  semantics might be lost

The reasons are:

- when raw-card retrieval is available, the card body can serve as high-fidelity
  projection input; whether the text is authoritative still depends on a hit in
  the local terminal result store
- duplicate body copies were only a compensation for limitations in the default
  projection path

### 7.2 Structured Body Block

The terminal card uses one stable, addressable body block for:

- user-visible display
- machine reads

The formal requirements are:

- terminal body content must reside in one fixed rich-text / content block
- the parser recognizes only that block
- headings, lists, quotes, code, links, and other structure are recovered from
  that block

### 7.3 Role of the Structure Summary

This compatibility layer has been removed.

The current terminal-card protocol keeps only:

- the title and template contract
- the `final_reply_text` body
- the hidden marker
- the `fc_tr_<result_id>_<checksum>` reference on the body element of new cards

The resulting behavior is:

- when raw-card retrieval succeeds and `result_id` resolves through the local
  terminal result store, the store body is the authoritative result
- when raw-card retrieval succeeds but the store misses, the card body is only
  a degraded projection fallback
- when raw-card retrieval fails, only best-effort projection remains; no
  structure summary is used to restore heading levels

## 8. Current Implementation Status

The repository has implemented the main path of this decision:

- terminal result cards use JSON 2.0, have a stable body location, and carry an
  `fc_tr_<result_id>_<checksum>` reference
- ordinary `interactive` messages with a `message_id` first request
  `card_msg_content_type=user_card_content`
- after raw-card retrieval succeeds, the local terminal result store first
  decides whether authoritative text is available; a store miss explicitly
  degrades to card projection
- `merge_forward` first expands child messages, then requests the raw card for
  each `interactive` child
- payload / historical-shape best-effort projection remains available when
  raw-card retrieval fails
- `/last text` remains an export and fallback entry for this bot instance; it
  does not replace the read stack above

## 9. Implementation Boundaries

### 9.1 Raw and Default Reads Share One Feishu Adapter Boundary

The message-query capability accepts `card_msg_content_type` explicitly. The
raw-card path passes `user_card_content`; ordinary history and default-projection
paths omit it. Neither path replaces the other through an implicit default.

### 9.2 Read Decisions Degrade by Authority

The current read order is:

1. local terminal result store hit: authoritative terminal text
2. raw card obtained by `message_id`: raw-card projection
3. event payload or default historical shape: best-effort projection

This order does not change depending on whether the card was received directly,
ordinarily forwarded, or found as a `merge_forward` child.

### 9.3 Cross-Instance Authority Is Intentionally Limited

The terminal result store is a local fact source for one bot instance. Even
when a complete raw card can be obtained, cards from other bots, other
instances, or historical environments remain non-authoritative projections.
An implementation must not promote text to an authoritative result merely
because a card marker resembles this repository's protocol.

## 10. Observability and Debugging

Structured ingress logging is implemented, but it is an explicit debugging
surface rather than routine business logging. This evidence chain distinguishes
the original Feishu event shape, the raw-card query result, and the final
projection path.

### 10.1 Currently Recorded Facts

When the debugging surface is enabled, the current implementation records:

- ingress-event `msg_type`, `message_id`, `chat_id`, `thread_id`, `parent_id`,
  `root_id`, and bounded raw `content`
- whether `merge_forward` expansion succeeded, its item count, and its child
  message IDs
- whether an `interactive` child entered
  `raw_card_from_merge_forward_child`
- whether raw-card retrieval succeeded and, on success, a schema and title
  summary
- whether final resolution used `raw_card_direct` or
  `best_effort_projection`, and whether the text is authoritative

Logs are not another card or terminal-result fact source. They only record the
evidence used by the read decision.

### 10.2 Structured Log Events

The current event names can be grepped individually:

- `card_ingress_event`
- `card_ingress_merge_forward_expansion`
- `card_ingress_raw_card_fetch`
- `card_ingress_resolution`

The implementation may also emit `card_ingress_merge_forward_child`. It marks
the raw-card replacement path for one `interactive` child.

### 10.3 Debug Switch Contract

The `debug_raw_card_ingress` contract is:

- the default is `false`
- only the YAML booleans `true` and `false` are accepted; the strings `"true"`
  and `"false"` are configuration errors
- the ingress events above are recorded only when the setting is explicitly
  `true`
- when disabled, raw-card retrieval success or failure does not emit this set
  of INFO logs

### 10.4 Boundary

These logs contain message and session identifiers, title summaries, errors,
and bounded raw content, so they must not be enabled by default. They help
distinguish "Feishu returned only a projection", "child messages were not
expanded", "raw-card retrieval failed", and "the repository deliberately
degraded". They cannot prove fidelity across every forwarding shape in Feishu.

## 11. Manual Verification Order

### 11.1 First Pass: Receive an Ordinary Card Directly

Goal:

- send a JSON 2.0 terminal result card
- confirm that its own `message_id` supports raw-card retrieval

Success criteria:

- the `schema` is returned
- the raw-card body block is returned
- terminal content can be recovered without depending on the current
  projection logic

### 11.2 Second Pass: Ordinary Forward

Goal:

- forward that card directly to the bot
- observe whether the received message is:
  - `interactive`
  - or degraded to `text`

Success criteria:

- if it remains `interactive`, its own post-forward `message_id` supports
  raw-card retrieval directly
- if it degrades to `text`, record clearly that ordinary forwarding is not
  suitable as the main path

### 11.3 Third Pass: Merge-Forward

Goal:

- forward that card to the bot through `merge_forward`
- confirm that expanding the outer message returns child messages

Success criteria:

- `message/get` returns `1 + N` items
- the child messages include an `interactive` item
- that child supports a subsequent raw-card read

### 11.4 Fourth Pass: Fallback Verification

Goal:

- simulate a raw-card retrieval failure
- confirm that the current best-effort projection and `/last text` still work

## 12. Current Product Conclusion

The repository's current formal product behavior is:

- display prefers JSON 2.0
- high-fidelity reads prefer raw-card JSON lookup by `message_id`
- `merge_forward` is a child-expansion entry point, not the complete content
  itself
- ordinary-forward reliability depends on whether the forwarded message remains
  a queryable new `interactive` message
- `/last text` is a fallback, not the only authoritative path
- forwarding semantics remain constrained by the actual Feishu event shape;
  structured debugging logs support empirical verification without turning an
  officially unguaranteed forwarding shape into a product promise

## 13. Maintenance Rule

If the repository changes any of the following facts, review this document and
`docs/decisions/feishu-card-text-projection.md` together:

- terminal result card send format
- raw-card retrieval strategy
- `merge_forward` child-message expansion behavior
- `/last text` read semantics
- the ingress debug switch's default, type, or log fields
