# Runtime Control Surface Contract

Chinese original: `docs/contracts/runtime-control-surface.zh-CN.md`

This document defines the formal semantics of the Feishu-side control surface.

## 1. Only one writable setting family remains

### 1.1 Binding-wise next-turn settings

Entry points:

- `/model`
- `/effort`
- `/approval`
- `/permissions`

Semantics:

- manage overrides for future turns of the current Feishu binding
- are primarily consumed at `turn/start`
- on unloaded-thread recovery, cold `thread/resume` may also carry a narrow
  one-shot subset for the first post-resume autonomous turn
- do not write any project-owned thread-level persisted state

## 2. Removed setting surfaces

The following entry points are no longer part of the formal project contract:

- legacy project-owned profile commands
- `/memory`
- any thread-wise memory control surface

If an operator wants to change process-level upstream capabilities such as
profile/provider or memory behavior, they must do it through upstream Codex
itself rather than a project-owned Feishu setting surface.

## 3. Other core state axes

Independent from settings, the control surface still separates three state
axes:

1. `binding`
   - which thread the current chat logically points to
2. `attach / detach`
   - whether the current chat receives Feishu push for that thread
3. `backend / live runtime`
   - whether the thread is loaded in the backend, and who currently owns live
     runtime

Those axes are parallel to settings and must not be conflated with them.

## 4. Formal semantics of turn-time settings

`/model`, `/effort`, `/approval`, `/permissions`:

- belong to the current binding's next-turn settings
- read back the current binding's persisted configuration facts by default
- are not the instance baseline
- are not thread-level persisted truth

Within that family:

- for `/model` and `/effort`, `auto` means "do not explicitly override"
- it no longer maps to any project-owned thread-level fallback state
- Focus treats model and effort as one constrained pair:
  - `validated`: effort is `auto`, or metadata for the explicit model advertises that effort
  - `deferred`: model is `auto`, or the explicit model has no usable metadata; Focus passes the explicit effort through and lets app-server decide
  - `rejected`: metadata exists for the explicit model and does not advertise the explicit effort
- `/model`, `/effort`, and card actions refuse newly requested `rejected` pairs; existing binding values are not migrated, and turn dispatch does not rewrite old values
- known canonical effort input is normalized to lowercase; unknown/custom effort only has surrounding whitespace trimmed and otherwise preserves case
- `ultra` is sent to Codex unchanged; Focus does not translate it to `max` or construct `collaborationMode`
- after explicit model / effort values are sent with a turn, they can affect the shared upstream thread's current and subsequent turns; local `focus` / `fcodex` may observe or overwrite that upstream state
- `auto` only means Focus omits the field; it does not restore `.codex/config.toml`, the model default, or an older value from another frontend
- model / effort are optional overrides: Focus reapplies only non-`auto` fields on each turn, while `auto` continues with the shared upstream thread's current state
- for `/approval` and `/permissions`, the persisted binding value is the
  safety baseline; a new binding is seeded from instance config, and once it is
  persisted it does not drift with later instance-default changes
- approval / permissions have no `auto` state: Focus explicitly reapplies the binding's safety baseline on every turn; another frontend may change the upstream thread, but the next Feishu turn applies this binding's value again

## 5. Side-effect boundary of reset-backend

`reset-backend` is a recovery/admin tool, not a routine settings-apply path.
Typical uses are:

- discard this instance's stale loaded runtime before cold continuation
  elsewhere
- rebuild this instance's backend view after the same persisted thread was
  changed outside this project, for example by bare upstream `codex`

When an instance resets its backend:

- the backend process restarts
- binding records stay
- related Feishu push paths detach first
- binding-wise next-turn settings stay

Reset-backend does not:

- rewrite thread history
- automatically re-attach every chat
- upgrade binding settings into thread-level settings
- act as a profile-switch surface

## 6. What `/status` should show

`/status` and related diagnostics should show separately:

- the current binding's next-turn overrides
- attach/detach state
- live-runtime / loaded state

They should no longer present:

- a project-owned profile setting
- a thread-wise memory setting
- "extra config that this project will inject on the next resume"
