# Codebase Audit — 2026-04-24

Status: resolved working material under `docs/_work/`. Not a repository fact.

This audit was rechecked and then fully addressed in code and docs. It is kept
only as a lightweight closure note so future readers do not mistake the older
review draft for an active worklist.

The following items were resolved:

- removed user-facing `on-failure` selection while keeping old config values
  normalized to `on-request`
- made thread-wise `/profile` writability checks shared and fail-closed on
  adapter errors
- rewrote binding persistence away from whole-record equality-to-default
  clearing
- fixed the interaction-lease rollback to release by the preattached thread id
- added a proxy idle-timeout safety net even when `--parent-pid` is present
- made local JSON-RPC error responses require a request id explicitly
- added explicit reject reasons for `merge_forward` and `interactive`
  attachment-resource cases
- serialized forward-timeout side effects under an explicit aggregator-owned
  lock
- extracted shared CLI instance-selection logic
- clarified `BindingRuntimeManager` hydration naming and added `build/` ignore

Follow-up expectation:

- if a new audit is needed, write a fresh `_work` note against the then-current
  code rather than editing this resolved closure file back into an active bug
  list.
