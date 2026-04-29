# Feishu Card Text Projection and Round-Trip Boundary

Chinese version: `docs/decisions/feishu-card-text-projection.zh-CN.md`

See also:

- `docs/architecture/feishu-codex-design.md`: current architecture and module boundaries
- `docs/contracts/feishu-thread-lifecycle.md`: execution-card lifecycle and terminal finalization
- `docs/decisions/feishu-attachment-ingress.md`: attachment ingress and local staging boundary

## 1. Problem

Users want `feishu-codex` to recover useful text semantics from Feishu cards,
especially:

- terminal execution results emitted by `feishu-codex` itself
- visible text from ordinary external message cards

The current constraints are:

- this repository does not treat Feishu inbound card payloads as a stable,
  complete card AST contract
- execution cards are currently designed first as human-facing UI, not as an
  exchange format
- `process_log`, `reply_segments`, and `final_reply_text` have not yet been
  tightened into three separate outbound contracts

Continuing to expand an implicit "receive `interactive` and guess some text"
path would create:

- ambiguous behavior
- hard-to-debug failures
- tight coupling between UI layout and exchange semantics
- false impressions that complex third-party cards are "supported"

This calls for a narrower and more explicit boundary.

## 2. Decision Summary

The repository makes the following design decisions for Feishu card text
handling:

1. The repository only promises **card text projection**, not full UI / action /
   state equivalence.
2. Live execution cards emitted by `feishu-codex` remain primarily human-facing
   UI and are not the strong-contract round-trip carrier.
3. Only terminal turn results enter the strong contract, and they must expose an
   authoritative `final_reply_text`.
4. The sender may continue to show:
   - `process_log`
   - `reply_segments`
   but both are display-only and are not part of the strong exchange contract.
5. The normal strong-contract carrier should remain card-based rather than a
   separate large plain-text message.
6. The receiver's strong contract parses only `final_reply_text`.
7. The receiver may best-effort extract `process_log`, `reply_segments`, and
   visible text from ordinary external cards; failure there must not break the
   main flow.
8. Approval cards, form cards, dynamic cards, and other stateful/action-heavy
   cards remain out of scope.
9. Only when the terminal reply cannot fit losslessly inside an acceptable card
   carrier may the system fall back to plain text; it must not rely on a
   partially visible terminal card.

## 3. Why This Boundary

The core goal is to separate "for humans to read" from "for another agent to
consume".

That separation creates three clear responsibilities:

- execution card
  - human-facing UI
  - may contain process logs, work traces, and staged replies
- `final_reply_text`
  - strong receiver-facing contract
  - must be the complete terminal text result that can be given directly to
    Codex
- external ordinary-card extraction
  - best-effort text supplementation only
  - must not pretend to reconstruct button, form, or state semantics

Benefits:

- clearer contract: only terminal results need round-trip guarantees
- natural fail-closed behavior: if authoritative `final_reply_text` is not
  available, do not guess
- lower maintenance cost: no need for a general card AST interpreter
- better alignment with repository preferences: explicit contracts, one clear
  path, and sharp boundaries

## 4. Terms

### 4.1 `process_log`

`process_log` is the human-facing execution trace, including but not limited to:

- command execution
- command output fragments
- file-change summaries
- MCP / web / image tool traces
- runtime notes

It corresponds to the current execution transcript `process_blocks` /
`process_text()` layer.

### 4.2 `reply_segments`

`reply_segments` are assistant-authored staged text segments emitted during the
turn, for example:

- "I'm checking ..."
- "I'm about to modify ..."
- "One more important thing ..."
- interim summaries

They correspond to the current execution transcript `reply_segments` layer.

### 4.3 `final_reply_text`

`final_reply_text` is the terminal text result that another agent should consume
reliably.

Its expected source is the **last textual `agentMessage`** of the target turn.

It is not:

- "whatever the last visible segment happens to be"
- "all reply-panel text concatenated heuristically"

It must be an explicit authoritative terminal representation emitted by the
sender.

### 4.4 `terminal execution card`

`terminal execution card` is the same execution card after the turn has reached
its terminal state.

It may continue to carry:

- `process_log`
- `reply_segments`
- terminal visual finalization

If it also contains a dedicated `final_reply_text` block, it may serve as the
strong-contract carrier.

### 4.5 `terminal result card`

`terminal result card` is a **separate card template designed specifically to
carry terminal results**.

The difference is:

- `terminal execution card` extends the existing execution-card lifecycle
- `terminal result card` exists only to express the terminal result and does not
  carry the running execution UI

## 5. Sender Contract

### 5.1 Keep the live execution-card experience

During an active turn, the sender may continue to show:

- a dedicated `process_log` area
- a dedicated `reply_segments` area
- a cancel button while running

This content primarily serves human readers and does not require strict
round-trip parsing on the receiver side.

### 5.2 Terminal results must expose authoritative `final_reply_text`

When a turn reaches a terminal state, the sender must provide an authoritative
`final_reply_text`.

Two normal formal representations are allowed:

1. `terminal execution card`
   - the existing execution card is patched into a terminal state and carries a
     dedicated `final_reply_text` semantic block
   - the same card keeps the execution UI and also becomes the strong-contract
     carrier
2. `terminal result card`
   - a separate dedicated terminal-result card is sent
   - the execution card may remain display-only for `process_log` /
     `reply_segments`
   - the dedicated result block on that card becomes the strong-contract carrier

Only when neither of those card carriers can represent the result losslessly may
the sender use a fallback form:

- `terminal result text`
  - a plain-text message is sent
  - that text is the authoritative `final_reply_text`
  - this is an overflow / failure fallback rather than the normal path

For the current phase-one rollout, the sender behavior is further tightened as
follows:

- prefer a separate `terminal result card` as the normal authoritative carrier
- the current card-title contract is fixed as:
  - execution card: `Codex 执行过程`
  - terminal result card: `Codex`
- once that carrier has been delivered successfully, and the terminal snapshot
  can identify the last textual `agentMessage`, patch the old execution card so
  its reply panel no longer repeats that last terminal answer
- the old execution card should then keep only `process_log` and earlier staged
  `reply_segments`
- if stripping that last answer leaves the old execution card with no visible
  process log or staged reply content, finalize the old execution card as a
  minimal terminal card instead of deleting the message; that minimal card
  currently renders a single `无` placeholder
- if the sender can only fall back to the local transcript, or if terminal
  result delivery fails, do not strip the final reply from the execution card

### 5.3 If the terminal result is too long, send plain text

If `final_reply_text` cannot be represented losslessly within the card budget:

- fall back to plain text
- do not keep a partial terminal card as the strong-contract carrier

This avoids the situation where a partially visible card is mistaken for a
complete terminal result.

### 5.4 `process_log` and `reply_segments` stay display-only

Even if terminal surfaces continue to include:

- a process-log panel
- a reply-segments panel

those remain display-only.

More specifically:

- once authoritative terminal output has already been delivered through a
  `terminal result card` or fallback plain text, the execution card should try
  to keep only staged/process reply segments
- if the terminal snapshot can distinguish "the last terminal answer" from
  earlier staged replies, that last answer should be removed from the execution
  card
- if the implementation only has the local transcript and cannot distinguish
  those boundaries reliably, it should keep the execution card text unchanged
  rather than risking result loss

The contract does not require the receiver to:

- reconstruct those panels fully
- preserve UI ordering or layout
- treat them as authoritative semantic input

## 6. Receiver Contract

### 6.1 Strong contract: parse only `final_reply_text`

The only formal success condition is:

- the receiver reliably obtains authoritative `final_reply_text`

That text becomes the primary card-derived message delivered to Codex.

For the current phase-one rollout, the strong receiver-side identification of a
`terminal result card` is further fixed as:

- header title is `Codex`
- header template is `green`
- the card contains at least one markdown block whose trailing content carries
  an invisible marker

The receiver interprets that markdown block as:

- the user-visible portion is the authoritative `final_reply_text`
- the invisible marker only declares that this card is an authoritative
  terminal-result carrier

In other words, the strong contract no longer depends on any extra explanatory
hint copy, and it does not treat user-visible hint prose as part of the
contract.

### 6.2 Best-effort: `process_log` and `reply_segments`

If the receiver can also extract:

- `process_log`
- `reply_segments`

it may attach them as supplementary context for Codex.

This extraction is best-effort:

- failure does not break the main flow
- uncertainty should bias toward omission, not guessing

### 6.3 Ordinary external cards: extract only effective text

For other ordinary message cards, the receiver performs limited text extraction
only.

The goal is not to restore the original card. The goal is to recover useful
visible text, such as:

- titles
- ordinary text
- obvious markdown / plain_text content
- simple explanatory paragraphs

The value here is mainly to let Codex use its own understanding ability, rather
than making this repository maintain a complex card-semantics interpreter.

### 6.4 Complex external cards remain unsupported

The formal support contract excludes:

- approval cards
- interactive form cards
- dynamic data-driven cards
- button-heavy workflow cards
- cards whose meaning depends strongly on backend state

For those cards, the repository does not promise readability, round-trip
support, or recovery of true business semantics.

## 7. What Counts as Round-Trip

### 7.1 Strong-contract round-trip

- terminal results emitted by `feishu-codex` itself, when they expose
  authoritative `final_reply_text`
- authoritative results carried by any of:
  - a dedicated block on a `terminal execution card`
  - a dedicated block on a `terminal result card`
  - only in overflow fallback scenarios, a `terminal result text` message

### 7.2 Not part of strong-contract round-trip

- running execution cards
- inferring "the last reply segment must be the final answer"
- inferring semantics from colors, buttons, or collapse state
- approval / form / dynamic cards

## 8. Architecture Boundary

Card text handling should be treated as a dedicated boundary rather than as
scattered special cases.

The intended split is:

- sender side
  - explicitly produce `final_reply_text`
  - prefer an appropriate terminal card carrier
  - fall back to plain text only when no card carrier can represent the result
    losslessly
- receiver side
  - prioritize authoritative `final_reply_text`
  - best-effort extract display-only content as optional context
  - best-effort extract useful text from ordinary external cards

The system should not continue to rely on:

- "receive `interactive` and concatenate whatever text is visible"
- treating current execution-card copy as if it were automatically a stable
  exchange format

## 9. Explicit Non-Goals

Without a stable complete card AST, the repository should not default to
implementing:

- generic card UI reconstruction
- button action-semantic recovery
- form-state recovery
- dynamic-card replay
- approval-context recovery
- complete peer parsing for arbitrary third-party cards

## 10. Verification Guidance

If this is implemented later, the minimum checks should include:

1. short terminal reply
   - sender emits authoritative `final_reply_text` through a terminal card
   - receiver gets the full result reliably
2. long terminal reply
   - sender falls back to plain text
   - receiver still gets the full result reliably
3. running execution card
   - humans still see `process_log` and `reply_segments`
   - receiver does not mistake it for a strong-contract terminal result
4. ordinary external cards
   - obviously useful text can be extracted
   - extraction failure does not break the main flow
5. complex external cards
   - they land clearly in unsupported / ignored instead of silently pretending
     to be supported

## 11. Recommended Implementation Path

This section is not a new contract. It is the recommended rollout and
implementation shape.

### 11.1 Start with the smallest reliable loop

The recommended first phase is this smallest reliable loop:

1. keep the current live execution card:
   - `process_log`
   - `reply_segments`
   - running-time buttons
2. when the turn reaches a terminal state, emit one authoritative
   `terminal result card` that contains a dedicated `final_reply_text` block
3. let the receiver's strong contract consume that terminal-result card
4. if terminal-result delivery succeeds and the snapshot can distinguish the
   last terminal answer, patch the old execution card to remove that last reply
5. only if even that result card cannot fit losslessly, fall back to plain text
6. add ordinary-card best-effort extraction separately afterward

Why this is recommended:

- it avoids forcing the current execution card to become a strict exchange
  format immediately
- it keeps execution-card UI and authoritative result semantics separate
- it preserves a card-first Feishu experience instead of making large plain text
  the default path

If the "same terminal execution card carries the authoritative result block"
template later proves stable enough, `terminal execution card` may be evaluated
as an alternative path; it is not part of the current phase-one rollout target.

### 11.2 Sender-side recommendation

The sender should separate "human-facing UI" from "authoritative terminal-result
carriers" into two explicit paths.

Recommended order:

1. keep the current live execution-card update path unchanged
2. when the turn becomes terminal, build `final_reply_text` from authoritative
   turn data
3. if `final_reply_text` is non-empty:
   - prefer sending a `terminal result card`
   - treat that card as the strong-contract carrier
   - if reply-based delivery fails, fall back to sending one top-level
     terminal-result card before dropping to plain text
4. if terminal-result delivery succeeds and the terminal snapshot can identify
   the last textual `agentMessage`:
   - patch the old execution card
   - keep only earlier `reply_segments`
   - remove the final terminal answer from that card
5. keep the execution card for:
   - terminal visual finalization
   - process logs
   - reply segments
6. if terminal-result delivery fails, or only the local transcript is
   available:
   - do not delete the terminal answer from the execution card
   - prefer fail-closed behavior over aggressive deduplication
7. only when card limits prevent a lossless result card, fall back to plain
   text

The most important implementation guidance here is:

- **do not derive `final_reply_text` from whatever the current card happens to
  display**
- generate `final_reply_text` separately from authoritative terminal data

### 11.3 Recommended source of `final_reply_text`

If upstream does not expose a dedicated "final answer" field, use this priority
order:

1. the last textual `agentMessage` in the terminal snapshot of the target turn
2. earlier textual `agentMessage` items from the same turn should remain on the
   execution card as display-only staged replies
3. only if the snapshot is unavailable or incomplete, fall back to the merged
   local transcript result

The key points are:

- prefer terminal snapshot / turn items
- do not prefer live-card display content
- do not assume that "the last visible reply segment" is automatically reliable
- if later reconciliation discovers a different authoritative
  `final_reply_text`, the sender must emit a corrected terminal-result carrier
  again instead of only patching the old execution card

The current code already suggests that this path is feasible:

- `snapshot_reply()` can already read turn items, the full reply text, and the
  last textual `agentMessage` from thread snapshots
- `ExecutionTranscript` already separates local reply and process channels

But the later implementation should promote terminal authoritative text into an
explicit field instead of leaving it implicit inside `reply_segments`.

### 11.4 Receiver-side recommendation

The receiver should be split into two explicit stages:

1. strong-contract stage
   - recognize and consume only authoritative `final_reply_text`
   - preferably consume the `terminal result card`
   - if the same-card path is adopted in the future, consume the dedicated result block
     from the `terminal execution card`
   - only in overflow fallback scenarios consume the corresponding plain-text
     message
2. best-effort stage
   - then parse visible text from ordinary `interactive` cards
   - extract what is available and stop there

This means the receiver should not merge these concerns into one path:

- self-emitted terminal-result recognition
- ordinary external-card text extraction
- complex-card semantic recovery

Recommended priority:

1. make self-result round-trip reliable first
2. then add useful-text extraction for ordinary external cards
3. keep complex-card parsing out of scope

### 11.5 Recommended extraction scope for ordinary external cards

For ordinary external cards, extract only low-ambiguity visible text:

- titles
- ordinary text
- `plain_text`
- `markdown`
- simple explanatory paragraphs

Explicitly give up on:

- button-action semantics
- form values
- approval-state-machine semantics
- dynamic-card binding semantics

If a card yields only a small amount of text, that is still acceptable:

- this path is best-effort by design
- the main value is to let Codex interpret the remaining context itself

### 11.6 Suggested module boundary

To avoid scattering this logic further inside transport-layer helpers such as
`_extract_text()`, introduce a dedicated boundary module, for example:

- `bot/card_text_projection.py`

That boundary should ideally own two responsibilities:

1. sender-side terminal-result projection
   - input: turn snapshot / runtime transcript
   - output: `final_reply_text`, terminal-card carrier selection, and optional
     display-only supplements
2. receiver-side card-text extraction
   - input: Feishu `interactive` message content
   - output: either strong-contract text or best-effort text

Existing modules should stay narrow:

- `bot/runtime_card_publisher.py`
  - continue to render and publish execution cards
- `bot/execution_output_controller.py`
  - add terminal-result-card delivery and plain-text overflow fallback
    orchestration
- `bot/feishu_bot.py`
  - remain a message-routing boundary rather than a complex card-semantics
    owner

### 11.7 Suggested rollout order

Roll out in this order:

1. sender-only
   - emit an authoritative `terminal result card`
   - do not change ordinary external-card parsing yet
2. self-consumption
   - let the receiver prioritize and consume that terminal-result card
   - make self-result round-trip work end to end
3. ordinary-card best-effort
   - add useful-text extraction for ordinary external cards
   - keep it outside the strong contract
4. optional terminal-execution-card
   - only if later needed, and only if the same-card terminal template is
     proven stable, let `terminal execution card` itself carry the dedicated
     strong-contract block
5. overflow fallback
   - only when card limits prevent a lossless result carrier, fall back to
     plain text

### 11.8 Approaches that are not recommended

Do not:

- treat the current execution card as the exchange format
- infer the final answer from UI layout
- parse self terminal results and external ordinary cards through the same rule
  path
- turn the plain-text overflow fallback into the default path

At this stage the priorities should be:

- reliable terminal results
- best-effort ordinary-card text
- fail-closed behavior for complex cards
