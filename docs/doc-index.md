# Docs Index

This directory is the source of truth for repository architecture, runtime
boundaries, and feature contracts.

`AGENTS.md` only records the repo owner's engineering preferences. It is not a
substitute for the docs in this directory.

## Reading Rule

When code and docs disagree, treat that as a contract gap. Tighten the code,
the docs, or both.

## Document Types

Active docs are now organized by role:

- `docs/contracts/`
  - normative feature and runtime behavior contracts
- `docs/architecture/`
  - current architecture, layering, module split, and implementation shape
- `docs/decisions/`
  - decision records and upstream-derived safety constraints that explain why a
    design boundary exists
- `docs/verification/`
  - manual test checklists and verification-oriented material
- `docs/archive/`
  - completed plans and historical rollout material; useful for context, but
    not part of the active runtime contract

Status guidance:

- treat `contracts/`, `architecture/`, and `decisions/` as active repository
  facts
- treat `verification/` as validation support, not product/runtime semantics
- treat `archive/` as historical context only
- treat local notes under `docs/_work/` as working material, not as repository
  facts

## Read By Type

### User-Facing Entry

- [README.md](../README.md)
  - quickstart, installation, common commands, operational pitfalls, and where
    to read next

### Contracts

- [`feishu-thread-lifecycle.md`](./contracts/feishu-thread-lifecycle.md)
- [`runtime-control-surface.md`](./contracts/runtime-control-surface.md)
- [`session-profile-semantics.md`](./contracts/session-profile-semantics.md)
- [`feishu-help-navigation.md`](./contracts/feishu-help-navigation.md)
- [`codex-permissions-model.md`](./contracts/codex-permissions-model.md)
- [`group-chat-contract.md`](./contracts/group-chat-contract.md)

### Architecture

- [`feishu-codex-design.md`](./architecture/feishu-codex-design.md)
- [`fcodex-shared-backend-runtime.md`](./architecture/fcodex-shared-backend-runtime.md)

### Decisions

- [`shared-backend-resume-safety.md`](./decisions/shared-backend-resume-safety.md)

### Verification

- [`group-chat-manual-test-checklist.zh-CN.md`](./verification/group-chat-manual-test-checklist.zh-CN.md)

### Archive

- [`codex-handler-decomposition-plan.md`](./archive/codex-handler-decomposition-plan.md)

## Read By Question

| Question | Read |
| --- | --- |
| What is the current architecture, layering, module split, and repository structure? | [`feishu-codex-design.md`](./architecture/feishu-codex-design.md) |
| What is the Feishu-side thread lifecycle, and what states must stay distinct? | [`feishu-thread-lifecycle.md`](./contracts/feishu-thread-lifecycle.md) |
| What shared state vocabulary and admin-surface contract apply to `/status`, `/release-feishu-runtime`, and `feishu-codexctl`? | [`runtime-control-surface.md`](./contracts/runtime-control-surface.md) |
| What do `/session`, `/resume`, `/profile`, and `/rm` mean across Feishu, `fcodex`, and the TUI? | [`session-profile-semantics.md`](./contracts/session-profile-semantics.md) |
| What information architecture and semantic rules does the Feishu `/help` navigation surface follow? | [`feishu-help-navigation.md`](./contracts/feishu-help-navigation.md) |
| What is the formal behavior contract for group chat modes, ACL, history recovery, and group-command triggering? | [`group-chat-contract.md`](./contracts/group-chat-contract.md) |
| How do approval, sandbox, writable roots, and protected paths behave? | [`codex-permissions-model.md`](./contracts/codex-permissions-model.md) |
| How does `fcodex` shared-backend mode work, including wrapper, proxy, and `--cd` semantics? | [`fcodex-shared-backend-runtime.md`](./architecture/fcodex-shared-backend-runtime.md) |
| What safety rules apply to shared backend reuse and `/resume`? | [`shared-backend-resume-safety.md`](./decisions/shared-backend-resume-safety.md) |
| What should be covered in manual group-chat regression testing? | [`group-chat-manual-test-checklist.zh-CN.md`](./verification/group-chat-manual-test-checklist.zh-CN.md) |
| What historical rollout plan was used to decompose `CodexHandler` ownership? | [`codex-handler-decomposition-plan.md`](./archive/codex-handler-decomposition-plan.md) |

## Practical Reading Paths

- For architecture or large refactors:
  - [`feishu-codex-design.md`](./architecture/feishu-codex-design.md)
  - then the relevant `contracts/` and `decisions/` docs
- For session or runtime bugs:
  - [`feishu-thread-lifecycle.md`](./contracts/feishu-thread-lifecycle.md)
  - [`runtime-control-surface.md`](./contracts/runtime-control-surface.md)
  - [`session-profile-semantics.md`](./contracts/session-profile-semantics.md)
  - [`shared-backend-resume-safety.md`](./decisions/shared-backend-resume-safety.md)
- For group-chat work:
  - [`group-chat-contract.md`](./contracts/group-chat-contract.md)
  - [`feishu-help-navigation.md`](./contracts/feishu-help-navigation.md)
  - [`group-chat-manual-test-checklist.zh-CN.md`](./verification/group-chat-manual-test-checklist.zh-CN.md)
- For wrapper or backend work:
  - [`fcodex-shared-backend-runtime.md`](./architecture/fcodex-shared-backend-runtime.md)
  - [`shared-backend-resume-safety.md`](./decisions/shared-backend-resume-safety.md)
- For permission or execution wording:
  - [`codex-permissions-model.md`](./contracts/codex-permissions-model.md)

## Language

- Most technical docs have both English and Simplified Chinese versions.
- The current manual group-chat verification checklist is only available in
  Simplified Chinese.
