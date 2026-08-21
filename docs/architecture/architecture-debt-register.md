# FOCUS Active Architecture Debt Register

Document role: synchronized English peer. Canonical Chinese: `docs/architecture/architecture-debt-register.zh-CN.md`.

This document records only unresolved architecture debt, external capability gaps,
and acceptance criteria. `docs/contracts/` defines feature semantics. Work slices,
commit budgets, and temporary evidence may live in `docs/_work/`, but closing an item
here requires code, formal contracts, and regression coverage.

## 1. Maintenance Rules

There are four statuses:

- `open`: the problem is established, but a complete implementation has not begun;
- `in_progress`: one closure chain has implementation, with explicit exit criteria
  remaining;
- `upstream_blocked`: this repository can only narrow the product promise and cannot
  reliably manufacture the missing upstream primitive;
- `closed`: current code, contracts, and regressions satisfy the exit criteria.

Maintenance constraints:

- An active item records only root cause, current owner, target boundary, and
  verifiable exit criteria, not a commit-by-commit diary.
- A closed item leaves a short index. Git history and frozen work ledgers retain
  details.
- File size triggers ownership/context review, not completion. The three `AGENTS`
  documents own thresholds and execution discipline.
- Do not mechanically split files to satisfy a gate, and do not mix package moves
  with behavior changes.
- Removed durable root writers, descendant gates, operator tree-stop, create/resume
  quarantine, and durable server-request ledgers are not compatibility targets and
  must not return through refactoring.

## 2. Current Execution Order

1. Keep `GAP-003` as an explicit macOS product boundary; local diagnostics or
   manual recovery must not impersonate a missing platform primitive.
2. Oversized files, package layout, and directory cleanup are not a standing
   queue. They enter a bounded campaign only when a concrete task demonstrates an
   ownership mix, dependency-direction problem, or context blocker; size alone does
   not justify recurring slices.

## 3. Active Debt

There is currently no repository-owned active item in this campaign. External
capability gaps remain below.

## 4. External Capability Gaps

### GAP-001 Missing Codex App-Server Reliability Primitives

- **Status:** `upstream_blocked`
- **Established gaps:** generic mutation idempotency/exactly-once, durable event
  cursors, authenticated backend process incarnation, and atomic complete
  descendant-tree freeze/interrupt are absent from the current upstream contract.
- **Local boundary:** do not automatically retry an unknown exact effect; rebuild UI
  from snapshot/history; ordinary stop interrupts one exact active turn; create,
  resume, and server requests create no durable/global quarantine.
- **Exit:** expand a corresponding product promise only when upstream provides a
  verifiable primitive, or Focus designs and verifies a complete constrained
  transaction for one concrete scenario. Caches, PIDs, lineage guesses, and raw bytes
  RPCs are not proof.

### GAP-003 macOS Escaped-Descendant Containment

- **Status:** `upstream_blocked`
- **Gap:** the macOS guardian proves app-server and its process group, but cannot
  contain arbitrary tool/MCP descendants that deliberately create a new
  session/group.
- **Local boundary:** stop, crash cleanup, diagnostics, cleanup receipts, and manual
  operator action cover only that process group. They cannot claim parity with a
  Linux subreaper or Windows Job Object, and cannot prove that an escaped descendant
  is absent.
- **Exit:** obtain and verify a reliable containment primitive with real-platform
  coverage. Until then the narrower support boundary remains a product limitation.

## 5. Closed Index

| ID | Current closure boundary |
| --- | --- |
| AD-001 | `ServiceRuntimeLifecycle` owns startup, public ingress, shutdown barrier, and authority release order. |
| AD-002 | `AdapterIngressGate` owns connection generation, reset fence, and ordinary outbound epoch. |
| AD-003 | Create uses only typed response and immediate local callback; unknown is not retried automatically and does not quarantine other threads. |
| AD-004 | The old durable root-operation/descendant writer is removed; only an exact active-main-turn lease remains. |
| AD-005 | Web backend/frontend aggregate state was split into document, profile, interest, read-model, mutation/action, and related owners; AD-008 later closed the wire boundary. |
| AD-006 | High-risk cross-owner ordering has named commands/coordinators and RuntimeLoop guards; the fact-free construction required-port graph, empty effect, and test-only façades were removed. |
| AD-007 | Raw mutable binding runtime state is confined to its trust zone and typed transition/snapshot boundary. |
| AD-008 | A versioned catalog owns Focus Web endpoints, events, DTO required fields, and closed enums; generated guards, producer fixtures, and a formal wire contract prevent parallel inventories and drift. |
| AD-009 | The old Handler/app-server test aggregates and zero-production-consumer `_bind_thread` façade are gone; suites are independently discoverable by owner, with wiring confined to bounded harness/integration suites. |
| AD-010 | An enforceable dependency-direction guard and canonical adapters, protocol, stores, and Runtime Admin packages are established; remaining flat modules are reviewed case by case on proven owner boundaries rather than treated as blanket debt. |
| AD-011 | fcodex targetless create retains only a current-generation one-shot capability. |
| AD-012 | Install/uninstall/purge share idle-only lifecycle and one machine lock owner. Uninstall removes managed `.venv` while preserving data; Windows self-removal uses an armed handoff barrier and asynchronous exact result. There is no hot upgrade, force, or generation rollback. |
| AD-013 | Ordinary guardian shutdown/crash recovery already retires an exact generation through process identity plus a matching cleanup receipt. A missing/mismatched receipt or legacy direct-child record is intentionally fail-closed: after independent platform-bounded process verification, the operator may remove only the exact instance record. No recovery/force command converts unknown into proof. |
| AD-014 | Server requests align with upstream process-local callback/replay; durable request/root/global fences are removed. |
| AD-015 | Destination loss is confined to matching binding/delivery cleanup; page uncertainty stays with one original UUID, and terminal presentation never gates main-turn retirement or FIFO. |
| AD-016 | Runtime Admin, Feishu ingress/cache/codec, managed process, RPC stop, and RPC connection facts have explicit owners; former aggregates are presentation, SDK, composition, or typed façades. |
| GAP-002 | External app-server deployment was removed; internal attached clients gain no lifecycle authority. |

The closed table is not a compatibility promise. Restoring an old mechanism requires
fresh upstream evidence, a concrete user scenario, a minimal contract, and verifiable
exit criteria; historical implementation may not simply be revived.
