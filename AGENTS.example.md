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
- the upstream codebase is located at `~/llm/codex`
