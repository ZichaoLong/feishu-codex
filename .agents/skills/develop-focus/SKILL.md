---
name: develop-focus
description: Execute an explicitly requested Focus repository diagnosis, review, feature implementation, behavior change, or refactoring task under the current applicable repository instructions and canonical navigation/change-cone discipline. Use when the user invokes $develop-focus and wants the task carried through scoped verification without restating the development discipline.
---

# Develop Focus

Work from the Focus repository root. This is an execution router, not authority
for architecture, behavior, permissions, campaign scope, or stop decisions.

## Load current authority

1. Read the user's task and every `AGENTS.md` applicable to the repository root
   and the paths already implicated by the task.
2. Re-resolve applicable instructions before reading or editing a newly reached
   directory and after moving files into a different instruction scope.
3. Apply current instructions for updates, approvals, campaigns, validation,
   convergence, stopping, and handoff; do not create a parallel status source.
4. Read `docs/architecture/development-navigation.zh-CN.md` completely and use
   `$navigate-focus-development` as its thin operational entry. That document
   alone governs source roles, read-cone expansion, stale-index handling,
   navigation-impact closure, and verification scope.
5. For install-artifact or publication work, read
   `docs/contracts/install-artifact-delivery.zh-CN.md`; an ordinary build or
   validation run is not publication.
6. If instructions conflict or a required source is unavailable, preserve
   normal instruction precedence and follow the current applicable stop path.
   This skill grants no exception or additional authority.

## Complete the task

1. Inspect the current branch, HEAD, and worktree without modifying or
   discarding unrelated user changes.
2. Give the opening progress update required by current instructions, then
   locate the smallest evidence-backed read/change cone.
3. Execute only work authorized by the request. Distinguish authoritative
   sources, regression evidence, derived navigation, and temporary campaign
   state using the canonical navigation document.
4. If current instructions require campaign governance, use their campaign
   procedure and sole active ledger before production edits; do not encode
   campaign rules in this skill.
5. Complete coherent task transactions. Whenever evidence expands the cone,
   reload newly applicable instructions before continuing.
6. After each transaction, run its required focused checks and close every
   contract, code, test, guard, and navigation impact required by the current
   sources. Run wider gates when the applicable instructions or risk require.
7. Continue until the requested outcome is verified or a current stop rule is
   met. Ask only for the exact decision or authority then required.
8. Finish outcome-first: report material changes, validation evidence,
   navigation-impact status, and any genuine remaining risk or blocker.
