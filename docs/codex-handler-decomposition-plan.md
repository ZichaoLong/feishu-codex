# CodexHandler Ownership Decomposition Plan

Chinese version: `docs/codex-handler-decomposition-plan.zh-CN.md`

This document is an implementation plan, not a runtime contract.

It answers:

- why the next step should not be more scattered fixes
- how `CodexHandler` should be split by ownership boundaries
- what each phase changes, does not change, and must prove before it is done

If the rollout order changes later, update this document rather than mixing
planning content into the formal runtime semantics docs.

## 1. Background

The repository has already completed several important tightening passes:

- binding persistence schema is now fail-closed
- binding clear / clear-all are now formal control-plane and admin-CLI actions
- shared command surface now has consistency tests
- help/card action payloads no longer carry the unused `plugin` field
- binding resolution and runtime-state hydrate/create now flow through one
  resolver path

Those changes reduced local ambiguity, but they did not solve the main
structural problem:

- `CodexHandler` still owns multiple state machines at once
- many constraints still depend on remembering call ordering
- `RuntimeLoop` and `_lock` still protect an overly broad shared state surface

So the next step should be ownership decomposition, not more scattered repairs.

## 2. Goal

The goal of this plan is to turn `CodexHandler` from a state-owning God object
into an orchestrator.

That means:

- making ownership boundaries explicit
- reducing dependence on one broad shared lock
- reducing reliance on cross-method implicit ordering
- preserving user-visible behavior while preparing the codebase for stricter
  contracts and better tests

## 3. Non-Goals

This plan does not aim to:

- create fake decoupling by only splitting one large file into many files
- optimize lock granularity first
- change user-visible behavior during the first ownership-extraction phases
- keep adding helper paths back into `CodexHandler` once the boundary is known

## 4. Principles

- split state ownership before tuning locks
- extract explicit interfaces before changing internals
- preserve behavior first, then discuss product/runtime changes
- divide components by who owns which state transitions, not by file size

## 5. Current Problem

`CodexHandler` still centrally owns at least four distinct concerns:

1. binding / subscribe / attach / released runtime state
2. Feishu write owner / interaction owner / thread lease rules
3. turn / execution lifecycle
4. control-plane and adapter event-bridge orchestration

That leads to:

- one change often touching binding, owner, execution, and UI anchor state
- correctness arguments that still depend on call-order reasoning
- tests that can lock behavior but not ownership boundaries
- ongoing fixes that improve local correctness without reducing global
  reasoning cost

## 6. Overall Plan

The recommended rollout is:

1. extract `BindingRuntimeManager`
2. extract `TurnExecutionCoordinator`
3. finish remaining contract and naming cleanup

These phases are intentionally ordered and should not be inverted.

Current progress:

- Phase 1 is complete: `BindingRuntimeManager`
- Phase 2 is now split into narrower execution-lifecycle ownership slices:
  - `TurnExecutionCoordinator` owns execution state transitions
  - `ExecutionOutputController` owns execution-card and follow-up publishing
  - `ExecutionRecoveryController` owns watchdog, snapshot reconcile, terminal
    backfill, and degraded-channel marking
  - `InteractionRequestController` owns approval / ask-user request lifecycle
  - `AdapterNotificationController` owns adapter-notification interpretation
    and dispatch
- `CodexHandler` is not yet a pure orchestrator, but it no longer directly
  owns those execution details

## 7. Phase 1: BindingRuntimeManager

### 7.1 Goal

Extract binding/runtime ownership from `CodexHandler` into a clear internal
component.

### 7.2 Responsibilities

The new component should own:

- binding resolution
- runtime-state hydrate/create
- bound / attached / released / unbound transitions
- subscribe / unsubscribe
- binding persistence sync
- Feishu write owner
- interaction owner / interaction lease
- thread write lease
- binding status snapshot
- low-level execution of binding clear / clear-all
- low-level execution of `/release-feishu-runtime`

### 7.3 Out Of Scope For Phase 1

Phase 1 should not own:

- turn/start / cancel / finalize
- execution transcript
- approval / ask-user pending requests
- patch timer / watchdog / follow-up
- adapter notification interpretation

Those stay with a later execution-lifecycle component.

### 7.4 Suggested Interface

The component should expose intent-level operations rather than raw dict/store
access, for example:

- `resolve_binding(...)`
- `get_runtime_view(...)`
- `bind_thread(...)`
- `clear_thread_binding(...)`
- `release_feishu_runtime(...)`
- `clear_binding(...)`
- `clear_all_bindings(...)`
- `snapshot(...)`
- `acquire_write_lease(...)`
- `release_write_lease(...)`
- `acquire_interaction_lease(...)`
- `release_interaction_lease(...)`

Callers should stop directly manipulating `_runtime_state_by_binding`,
`_chat_binding_store`, `_thread_lease_registry`, and
`_interaction_lease_store`.

### 7.5 Migration Strategy

Recommended order:

1. move resolver / hydrate / snapshot logic first
2. switch `CodexHandler` to manager-backed binding/runtime access
3. move attach / release / clear / owner-lease operations
4. remove remaining direct handler access to binding/runtime internals

### 7.6 Exit Criteria

- no user-visible behavior change
- existing binding / attach / release / clear / owner tests still pass
- new manager-level tests cover:
  - binding resolution
  - hydrate/create
  - attach/release
  - write owner / interaction owner
  - clear / clear-all rejection conditions

## 8. Phase 2: TurnExecutionCoordinator

### 8.1 Goal

Extract turn/execution lifecycle ownership from `CodexHandler` and keep it
separate from binding/runtime management.

### 8.2 Responsibilities

Phase 2 is now realized as three cooperating components that together own:

- `TurnExecutionCoordinator`
  - prompt turn start
  - cancel turn
  - execution anchor
  - execution transcript
  - plan state
  - explicit state transitions before terminal finalize
- `ExecutionOutputController`
  - patch timer
  - execution-card send / patch
  - follow-up send decisions
  - plan-card publish / patch
- `ExecutionRecoveryController`
  - mirror watchdog
  - snapshot reconcile
  - terminal reconcile backfill
  - runtime degraded marking
- `InteractionRequestController`
  - pending approval requests
  - pending ask-user requests
  - request fail-close / resolved cleanup
  - request-card delivery / patch driving
- `AdapterNotificationController`
  - adapter notification method -> handler routing
  - semantic interpretation of thread / turn / item notifications
  - dispatch from notifications into execution / output / recovery /
    request components

### 8.3 Boundary With BindingRuntimeManager

The execution-lifecycle component should not decide what the binding is and
should not directly manage attach/release.

It should query `BindingRuntimeManager` for:

- current binding
- current thread
- current runtime view
- owner / lease write availability

In short:

- `BindingRuntimeManager` owns "whose thread/runtime state is this?"
- `TurnExecutionCoordinator` owns "how does this turn start, run, and end?"

### 8.4 Migration Strategy

Recommended order:

1. move the start / cancel / retire main path first
2. move pending requests and execution anchor
3. move transcript / plan / patch / watchdog / follow-up
4. move snapshot reconcile / finalize last

Current status: most of steps 3 and 4 are now extracted into dedicated
execution components, but `CodexHandler` still owns:

- non-execution command/UI glue
- top-level runtime entrypoints and cross-domain orchestration

### 8.5 Exit Criteria

- existing start / cancel / pending-request / finalize / reconcile tests still
  pass
- new coordinator-level tests cover:
  - terminal notifications
  - no duplicate follow-up
  - approval / ask-user transitions
  - watchdog fallback
  - snapshot reconcile effects on anchor/transcript state

## 9. Phase 3: Remaining Contract Cleanup

After the first two ownership extractions, clean up the remaining items that
fit better once boundaries are explicit:

- `#2` single-source-of-truth contract for `admin_open_ids`
- `#9` naming and docs for authoritative read vs bounded-list best-effort lookup
- `#15` concurrency contract for `ThreadLeaseRegistry`

This cleanup is intentionally later because doing it earlier would mostly add
more helpers back into `CodexHandler`.

## 10. Why Not Start Elsewhere

### 10.1 Do Not Start With Lock Splitting

Starting with locks risks getting:

- more locks
- less clear ownership

That is not the desired long-term architecture.

### 10.2 Do Not Prioritize More Scattered Review Fixes

Many local bugs and local contracts have already been tightened. More isolated
fixes now have lower marginal value than reducing total reasoning cost.

### 10.3 Do Not Start With File-Level Slicing

If the work only turns one big file into multiple files while leaving state
ownership implicit, it is still navigation refactoring, not real decoupling.

## 11. Rollout Constraints

The first two phases should follow these constraints:

- default to no user-visible behavior changes
- extract the boundary before moving all call sites
- add component-level tests in the same phase
- avoid mixing unrelated contract cleanup into the same patch series
- allow internal API renames; do not preserve intermediate compatibility layers
  just for their own sake

## 12. Suggested Commit Shape

Each phase should be split into commits roughly like:

1. docs and boundary statement
2. component skeleton and minimum interface
3. handler switched to the new interface
4. component-level regression tests
5. removal of old direct access paths and stale helpers

This keeps review clearer, rollback smaller, and boundary definition separate
from behavior movement.

## 13. Recommended Next Step

The immediate next step should be Phase 1: `BindingRuntimeManager`.

The first patch set should focus only on:

- the manager doc entry point
- the manager skeleton
- migration of binding resolver / hydrate / runtime view / snapshot

That creates a real binding/runtime ownership boundary before moving the
attach/release/lease operations that depend on it.
