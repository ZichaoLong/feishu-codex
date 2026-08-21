# AGENTS.example.md

This file is an optional template for local agent-collaboration preferences.

It is not the source of truth for repository architecture, module boundaries,
or feature semantics. Repository facts belong in `docs/`.

If you want a private local preference file, copy this to `AGENTS.md` and edit
it locally. If you prefer to keep a private Chinese-localized variant such as
`AGENTS.zh-CN.md`, keep that local as well. Both local files are intentionally
gitignored in this repository.

## Core Preference

Default toward:

- clear architecture
- easy maintenance
- unambiguous behavior

Do not default toward:

- preserving compatibility for its own sake
- keeping weak abstractions because they already exist
- encoding fuzzy product behavior directly into code

## Default Engineering Stance

When making changes:

- prefer explicit contracts over implicit conventions
- prefer one clear path over multiple half-supported paths
- prefer removing ambiguity over preserving legacy shape
- prefer simple control flow over clever layering
- prefer fail-closed behavior over ambiguous best-effort behavior

If a feature contract is unclear, surface the ambiguity and tighten the
contract in code, naming, validation, or docs.

## Compatibility

Compatibility is not a default goal in this repo.

Unless the user explicitly asks otherwise:

- internal APIs may be changed freely
- stale branches and compatibility shims may be removed
- behavior may be simplified if the result is cleaner and easier to reason
  about

## Refactoring Bias

Refactoring is encouraged when it improves clarity.

Good refactors usually:

- make ownership clearer
- reduce hidden coupling
- remove duplicate paths
- reduce ambiguity in runtime state or behavior

Bad refactors usually:

- move complexity without clarifying ownership
- add abstraction without simplifying the code
- preserve confusing structure just to avoid change

## Review Priorities

Prioritize, in order:

1. ambiguous or incorrect behavior
2. unclear ownership of state, events, or responsibilities
3. hidden coupling across modules
4. concurrency or lifecycle risk
5. missing regression coverage for high-risk flows
6. naming or structure that obscures intent

## Upstream Parity and Complexity Justification

When behavior is primarily owned by an upstream runtime, protocol, or client,
use its stable behavior as the default product baseline. Do not introduce a
stronger local contract merely because it sounds safer or more complete.

Before intentionally diverging from upstream behavior, establish and report:

- how upstream behaves, with the exact source version and evidence
- the concrete user or multi-surface scenario that upstream does not cover
- the smallest additional local rule needed for that scenario
- the user-visible benefit, rejection, delay, or recovery cost of that rule
- the authoritative evidence the system can actually observe to enforce it
- which local contract, state, coordinator, persistence, and tests can be
  removed if the divergence is not justified

Prefer upstream parity when it already gives a stable user experience. A
multi-surface frontend may add a local rule only for a demonstrated race or
shared authority that upstream does not have. Scope that rule to the smallest
relevant turn, request, field, destination, or external effect.

A bounded non-guarantee can be the correct contract. Stable fallback to the
upstream runtime or its persisted configuration is valid behavior when Focus
cannot observe or order the stronger outcome without taking ownership away
from upstream.

Forward upstream-owned fields semantically unchanged unless Focus owns a
proven exact shared-effect boundary. Do not whitelist, strip, normalize,
override, or add response postconditions merely because Focus cannot model a
field or prove how upstream used it.

Before a slice introduces or retains a stronger local guarantee, record the
upstream-parity and subtraction option first. If upstream already satisfies
the exit criterion, do not add polling, persistence, replay, quarantine,
coordinators, or state machines to make a local proof more complete. Otherwise
the sole campaign ledger must name the exact gap, observable invariant,
smallest additional guarantee, machinery delta, and deletion path. New local
machinery must show a measurable reduction in user risk and a net complexity
justification. This record must exist before any production edit.

- Fail closed at the smallest boundary that can prevent duplicate or corrupt
  effects. Do not let an unknown outcome quarantine unrelated threads,
  surfaces, fields, or the service without proof of a shared invariant.
- Presentation, delivery, telemetry, and cleanup are not lifecycle authority
  unless the product contract truly requires them to decide whether work ran
  or completed.
- An owner must own a mutable fact or external-effect authority. Synchronous
  call ordering alone does not justify a durable owner, state machine, ledger,
  or coordinator.
- Tests prove that code follows the chosen contract; they do not prove that the
  contract is supported by upstream facts or is good product behavior.
- At each campaign boundary, ask what can now be deleted and whether the same
  outcome can be obtained with fewer states, persisted facts, recovery paths,
  and cross-module dependencies.

Stop and align with the developer before continuing when the proposed contract
is stronger than upstream without a proven user need, requires evidence the
system cannot obtain, turns a local unknown into broader unavailability, or
adds substantial machinery without a measurable reduction in user risk.

## Architecture and Context Review

Treat source size as an ownership and context-cost signal, not as an automatic
split instruction.

- A hand-written source file at or above 1,500 lines requires an ownership and
  context review when it newly crosses that threshold, is materially changed,
  or is brought into the current campaign. The threshold alone neither requires
  a split nor makes an otherwise valid task fail.
- Newly reaching 2,000 lines or 96 KiB, or materially worsening an already
  reviewed oversized file, is a pause-and-align trigger before further
  expansion or commit. Report why the change belongs in that file; the expected
  impact on future agent context pressure, owner/path/call-chain discoverability,
  behavior/state/effect tracing, change locality, and regression scope; and the
  recommendation and risk for immediate organization versus explicit deferral.
- The developer chooses whether to organize immediately, record a deferral, or
  keep a coherent single owner intact. A reviewed oversized file may grow after
  that decision; do not impose a monotonic size ratchet or split code merely to
  satisfy a metric.
- Review ownership density, change locality, fan-in and fan-out, state-transition
  and external-effect span, concurrency and lifecycle boundaries, test coupling,
  and behavioral discoverability and predictability.
- A large single-lock state owner may remain intact when splitting it would
  duplicate facts or weaken invariants. Record the reason instead of splitting
  mechanically.
- Review a large flat directory when unrelated owners share one namespace,
  import direction is hard to infer, or related behavior is hard to discover.
  Create packages around proven owner or capability boundaries, not file-count
  targets, and establish directional import guards before physical moves.
- Do not repeatedly report an unchanged reviewed finding. Routine local changes
  inside the same owner do not require a new escalation unless they change the
  review conclusion. Report newly crossed thresholds, newly mixed
  responsibilities, material discoverability/context regressions, and changed
  ownership conclusions.

## Single Source and Change Closure

Single source of truth is scoped by fact type:

- normative behavior and architecture intent belong in the relevant contract or
  architecture document
- mutable runtime facts belong to one explicit runtime owner
- configuration values belong to one canonical configuration surface
- temporary campaign status belongs to one execution ledger
- tests provide evidence and regression protection; they do not become a second
  behavior contract

For significant behavior, state-machine, or ownership changes, keep contract,
code, tests, and enforcement guards aligned. Use the same vocabulary across
them and link to the canonical fact instead of copying competing descriptions.

## Repository Navigation and Change-Cone Discipline

For an explicitly requested end-to-end repository task, use the `develop-focus`
skill. It dynamically routes to the current applicable instructions and the
canonical navigation discipline without copying either one. For scoped
repository navigation, use the `navigate-focus-development` skill and follow
the canonical `docs/architecture/development-navigation.zh-CN.md`; its
English synchronized peer is `docs/architecture/development-navigation.md`.
Only the canonical document defines source roles, read-cone expansion,
stale-index handling, navigation-impact closure, and verification scope; do
not restate those rules here.

## Large Change Campaigns

Before a broad change starts, establish a finite campaign contract containing:

- objective and non-goals
- proven current facts and target ownership
- invariants and explicit exit criteria
- ordered vertical slices
- a finite production-commit budget and a separate, limited correctness-fix
  budget
- the required targeted and phase-exit validation

Use one temporary execution ledger as the campaign status source. Record the
current HEAD, last green validation, completed commits, current slice, next
single action, blockers, and deferred debt. A handoff is a snapshot or pointer
to that ledger, not a competing live plan.

- Commit by complete transaction, owner, or capability closure, not by file
  count or elapsed time.
- Every production refactor commit must remove an old path or reduce a named
  blocker. An atomic structural or package migration counts when it deletes the
  old path, materially improves discoverability, context cost, or dependency
  direction, and keeps behavior green. A wrapper-only change, broad re-export,
  generic `common.py` / `utils.py` bucket, or old-and-new dual path is not
  progress.
- Keep behavior fixes, owner extraction, and package moves in separate commits.
- Run focused regression and static checks for each commit; run the full agreed
  gate at campaign exit.
- Adjacent cleanup that does not block an exit criterion goes into the deferred
  register instead of extending the campaign.

## Convergence and Stop Rule

Stop local patching and report to the developer when any of these occurs:

- the same root cause remains after two local fix attempts
- two production commits fail to reduce a named blocker or remove an old path
- the declared commit budget is exhausted
- correctness requires a new upstream capability, a product-contract change,
  or evidence that the current system cannot obtain
- an external side effect has an unknown outcome and no authoritative
  idempotency or reconciliation mechanism exists

Classify the blocking cause as an upstream capability gap, contract
contradiction, observability or evidence gap, ownership or state-model gap,
unknown external-effect outcome, or environment/infrastructure gap. Report the
proven facts, attempted fixes, remaining correctness gap, workaround risk,
available contract or upstream options, and the exact developer decision
needed.

Discovering another bug does not reset the campaign budget. Only a bug that
directly blocks an agreed exit criterion may consume the limited correctness-fix
budget.

## Testing Preference

Do not stop at “tests pass”.

When practical, add or update tests that lock down the intended behavior of the
change, especially for bugs, state transitions, ownership transfer, and other
high-risk flows.

## Docs Policy

Keep repository facts out of this file.

- Architecture, boundaries, and runtime design belong in dedicated docs.
- Feature contracts and behavior semantics belong in dedicated docs.
- When adding or changing an important feature, command, concept, or
  abstraction for a concrete scenario, prefer recording its design intent in
  the relevant doc under `docs/`, not just its surface behavior.
- Prefer documenting three points whenever practical:
  - what problem or scenario it is meant to solve
  - which layer of state or abstraction boundary it operates on
  - why existing mechanisms were not sufficient
- This is mainly to preserve the reason something exists, so later refactors
  can still tell whether it should be kept, split, simplified, or removed.
- Read those docs only when the task needs them.

## Local Install Discipline

During development, do not use:

- `pip install .`
- `pip install -e .`

They create extra launchers outside the repository-managed install lifecycle.
Use `bash install.sh` or `./install.ps1` as the only supported install and repair
path.

## Documentation UX Preference

Prefer progressive disclosure over front-loading explanation.

- Keep `README` focused on the minimum path needed to get started.
- Do not duplicate too much command detail in `README` when the same detail is
  better delivered by install-time output, CLI `--help`, or task-specific docs.
- Let usage guidance unfold along the user's actual path:
  install first, then command help, then deeper docs only when needed.
- When in doubt, prefer making in-product help (`--help`, error messages,
  summaries after install) more usable before expanding `README`.

## Reference Preference

When Feishu / Lark behavior matters:

- prefer official documentation and public protocols as the reference
- if direct access to the needed material is blocked, locate the relevant
  public URL first
- if the content still cannot be retrieved, ask the developer to download it
  and pass it in

When Codex app-server behavior or frontend / backend behavior matters:

- treat upstream code and public documentation as the source of truth
- inspect upstream code first when behavior is defined more clearly in
  implementation than in secondary descriptions
- treat every upstream checkout as strictly read-only by default; unless the
  current task explicitly authorizes preparing an upstream PR, do not modify
  its worktree, index, refs, branches, commits, or in-checkout build outputs
- if a local checkout is needed and its path was not provided, ask the
  developer; use the path only as a task-local parameter and never persist it
  in repository files or generated metadata
- use public GitHub commit/blob permalinks pinned to a full 40-character commit
  for durable evidence; a local checkout path is not evidence
- inspect upstream HEAD and status before and after reading it, and leave any
  pre-existing differences untouched
