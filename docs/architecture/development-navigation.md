# Repository Navigation and Change-Cone Discipline

Document role: synchronized English peer. Canonical Chinese: `docs/architecture/development-navigation.zh-CN.md`.

This document defines how routine diagnosis, review, feature implementation,
behavior change, and refactoring constrain reads, identify fact sources,
expand a change cone, and close navigation impact. A feature or behavior
change aligns its relevant contract, code, tests, guards, and navigation
impact in the same transaction that changes the fact. This document defines
neither product behavior nor permission, commit budgets, or campaign
exceptions.

## 1. Boundary Between the Two Disciplines

Repository navigation discipline applies to every development task. It answers
what to read first, when to read one more hop, which derived entry points a
change must update, and how far to verify. Large-change campaign discipline is
an additional layer for broad changes. It answers what the objective and
non-goals are, how many commits are available, and when to stop or escalate.
The two compose, but must not duplicate rules or mutable status.

To activate the complete workflow with one sentence, use
`Use $develop-focus to complete: <task>`. The skill only routes dynamically to
the `AGENTS.md` files applicable to the current paths, this document, and fixed
navigation tools. `AGENTS.md` owns progress updates, authority, campaigns,
escalation, and stop discipline; this document owns navigation and change-cone
discipline. The skill copies neither policy nor expands the authority or scope
granted by the user's task.

## 2. Fact Roles and Precedence

| Content | Authority | Navigation role |
| --- | --- | --- |
| Normative behavior, architecture intent, and boundaries | The relevant formal document under `docs/contracts/`, `docs/architecture/`, or `docs/decisions/` | Link only; do not restate |
| Mutable runtime facts and external-effect authority | The one owner in production code | Locate the owner symbol only |
| Configuration values and schemas | The canonical configuration surface and validator | Locate the entry or owner only |
| Regression and static evidence | Tests and guards | Form the verification cone only |
| Locations of capability entries, owners, contracts, and verification cones | `scripts/focus_capabilities.json` | Human-reviewed derived index |
| Agent procedure entry point | The `develop-focus` and `navigate-focus-development` skills and applicable `AGENTS.md` | Route only to current authorities and fixed tools |
| Temporary campaign status | The current `docs/_work/` ledger | Never enter long-lived navigation |

The capability catalog is a human-reviewed, incomplete, disposable, and
rebuildable derived index. An `owner` reference does not create ownership; it
only points to an owner already established by formal documentation and the
production implementation. Its purpose is to reduce the first read, not to
describe the whole repository. If it conflicts with code or a formal document,
the catalog is stale and cannot override either authority. A
code/formal-contract disagreement is a contract gap that navigation cannot
resolve. Tests are evidence, never a second behavior contract, and a missing
capability does not prove that the capability is absent from the repository.

## 3. Code-First Navigation Invariant

“Code as navigation” means that production structure is the primary map of
implementation location, dependency direction, and runtime authority. It does
not mean that code replaces formal contracts. Even if the capability catalog
were deleted, a developer should still be able to follow a public entry, a
clearly named owner, direct dependencies, and corresponding tests to locate a
change. Formal contracts continue to own normative behavior and architecture
intent.

Code and tests must preserve these discoverability properties:

- Name packages, modules, and symbols with capability or owner vocabulary, and
  give each mutable fact or external effect one obvious production owner.
- Keep public entries and cross-boundary dependencies explicit, direct, and
  directional. Do not hide authority behind broad re-exports, dynamic
  registration, compatibility aliases, `common.py`, or `utils.py`.
- Use formal-contract vocabulary in owner APIs and types. Module docstrings may
  state a local role and link to a formal contract, but must not copy behavior,
  defaults, or mutable facts.
- Give focused tests predictable capability- or owner-shaped topology, and
  protect cross-owner invariants with explicit sentinels or direction guards.
- Split packages or files only when ownership, change locality, or dependency
  direction becomes clearer; do not fragment an owner that must maintain one
  invariant merely to satisfy a metric.

The catalog and skills may accelerate the first hop, but they cannot become a
required side channel for understanding the code. If a recurring task needs
catalog prose to find its owner, call chain, or tests, treat that as a
discoverability defect in code structure or formal contracts. Correct it in a
bounded transaction or campaign instead of expanding a second architecture
description.

## 4. Read-Cone Discipline

1. Read the `AGENTS.md` files applicable to the current path, then query the
   closest reviewed capability with `focus_nav.py list` / `show`. A hit is a
   reading hypothesis, not proof of ownership.
2. For the first read, open only the returned entry, owner, contract,
   focused-test, sentinel, and guard references. Identify the task's fact type,
   authority, and external effect before expanding.
3. Expand one hop only when evidence shows that a call or import crosses the
   current boundary; another owner reads or writes the same mutable fact or
   effect; a formal contract links to a higher-precedence contract; a failing
   test or guard points to an adjacent module; or the change affects a known
   public entry or direct consumer.
4. For Python boundaries, prefer `focus_nav.py module` to compute live direct
   imports/importers. For other languages and non-import relations, use narrow
   searches for an exact symbol, route, event, configuration key, or test name.
   Follow only one evidence-backed hop at a time; do not preload sibling files
   by directory.
5. If the catalog has no match, start a narrow search from user vocabulary, a
   public entry, a formal contract, or a failing test. Do not guess an owner or
   invent capability references. Register a capability only after human review
   of its owner, contract, and verification cone.
6. Read `docs/_work/` only for an explicitly active governance task. Routine
   development must not treat historical ledgers as current architecture or
   behavior evidence.

Stop expanding once the evidence explains the target behavior, locates the one
change authority, and selects the regression cone. Directory size or possible
relevance alone is not evidence to expand.

## 5. Change and Navigation-Impact Closure

Before editing, identify the formal contract, production owner, public entry,
direct consumers, focused tests, sentinels, and direction guards in the change
cone. Keep edits inside that cone unless an evidence trigger from section 4
requires expansion.

Review and update or remove affected capability references in the same
transaction whenever any of the following changes:

- an entry or owner's path, symbol, responsibility, or effect authority;
- a formal contract's path, heading, precedence, or behavior boundary;
- focused-test, sentinel, or test-package topology;
- import direction, direct consumers, or applicable guards;
- capability creation, merge, split, or removal.

If no catalog capability is affected, navigation-impact review may close
without adding one. The catalog does not target full repository coverage; it
contains only stable entries that have been reviewed and have clear repeated
development value.

Establish the initial cone before editing and manually review the transaction's
explicit changed paths before committing. The live reverse lookup can assist
that review:

```bash
python scripts/focus_nav.py paths <repo-relative-path>... [--locations]
```

The tool does not read Git or infer a capability or semantic owner from a path.
It returns only exact current-catalog matches with their capability, role, and
reference. A pytest `::node` reference matches its base file. `--locations`
parses current source to locate a symbol, Markdown heading section, or test
target. It fails closed if any selector is not unique or if a directory-level
test target has no exact line range. This command is a derived-index aid, not
transaction completion authority. `unmapped` means only that no catalog
reference matched; it is not proof of no semantic impact and does not replace
manual navigation-impact review. If an unrelated stale ref prevents the whole
catalog from loading, complete that review from formal contracts, code, and
tests. Never change an authority to fit the catalog or automatically expand the
current scope to repair unrelated refs.

A path migration must atomically remove the old reference. Unless a formal
external contract requires compatibility, do not retain old navigation paths,
shims, broad re-exports, or dual old/new entry points. Behavior fixes, owner
extractions, and package moves retain their separate transaction boundaries;
the navigation projection changes with the transaction that actually affects
it.

## 6. Fail-Closed Handling of a Stale Index

An existing path or symbol proves only that a reference resolves, not that its
owner, contract, or regression cone remains correct. If a reference is
missing, code evidence contradicts its classification, ownership moved, tests
no longer cover the behavior, or a capability points to ambiguous paths:

1. Stop using that catalog item as evidence.
2. Reconstruct the change cone narrowly from formal documents, production
   code, and tests.
3. Correct or remove stale references in the current transaction.
4. Rerun catalog validation and the affected verification cone.

Never change code or contracts to conform to a stale catalog conclusion.

## 7. Allowed and Forbidden Long-Lived Navigation Content

The capability catalog contains only reviewed entry, owner, contract,
focused-test, sentinel, and guard references. The skill contains only the
steps needed to invoke fixed tools and route to this document.

Long-lived navigation must not store:

- behavior narratives, invariants, architecture rationale, or product defaults;
- branches, HEADs, counts, digests, manifests, markers, or campaign evidence;
- complete or transitive import, call, or test graphs;
- arbitrary shell commands, mutable environment facts, or `docs/_work/` refs;
- speculative entries without owner, contract, and verification-cone review.

Direct Python imports/importers, changed-path reverse lookup, and reference
locations must be computed live from current source and explicit input. Do not
persist their graph, reverse index, or line numbers. The catalog's strict schema
and reference validation reject forbidden shapes, but semantic correctness
remains the responsibility of change-impact review.

## 8. Verification Cone and Completion

`focus_verify.py <capability>` is the reviewed minimum verification cone, not a
replacement for transaction or campaign exit gates. A real change must also
include direct-consumer regressions introduced by the edit, tests named by
failure evidence, applicable static guards, contract/document checks, and any
wider gates required by its risk.

A development transaction is complete only when:

- authoritative contracts, code, and tests agree for the changed semantics;
- navigation impact for every affected capability is updated, removed, or
  confirmed to have no catalog match;
- old paths and stale references are absent;
- focused verification and all wider gates required by the transaction pass.
