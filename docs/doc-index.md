# Docs Index

This directory is the source of truth for repository architecture, runtime boundaries, and feature contracts.

`AGENTS.md` only records the repo owner's engineering preferences. Do not use it as a substitute for the docs in this directory.

## How To Use This Directory

- Start with [`feishu-codex-design.md`](./feishu-codex-design.md) when you need the overall architecture, current repository structure, or high-level feature boundaries.
- Then read only the topic-specific document that matches the change you are making.
- If code behavior and docs disagree, treat that as a contract gap. Tighten the code, the docs, or both.

## Read By Topic

| Question | Read |
| --- | --- |
| What is the current architecture, layering, module split, and repository structure? | [`feishu-codex-design.md`](./feishu-codex-design.md) |
| What phased plan should be followed for further `CodexHandler` ownership decomposition? | [`codex-handler-decomposition-plan.md`](./codex-handler-decomposition-plan.md) |
| What is the Feishu-side thread lifecycle, and what states must stay distinct? | [`feishu-thread-lifecycle.md`](./feishu-thread-lifecycle.md) |
| What shared state vocabulary and admin-surface contract apply to `/status`, `/release-feishu-runtime`, and `feishu-codexctl`? | [`runtime-control-surface.md`](./runtime-control-surface.md) |
| What information architecture and semantic rules does the Feishu `/help` navigation surface follow? | [`feishu-help-navigation.md`](./feishu-help-navigation.md) |
| How does `fcodex` shared-backend mode work, including wrapper, proxy, and `--cd` semantics? | [`fcodex-shared-backend-runtime.md`](./fcodex-shared-backend-runtime.md) |
| What do `/session`, `/resume`, `/profile`, and `/rm` mean across Feishu, `fcodex`, and the TUI? | [`session-profile-semantics.md`](./session-profile-semantics.md) |
| What safety rules apply to shared backend reuse and `/resume`? | [`shared-backend-resume-safety.md`](./shared-backend-resume-safety.md) |
| How do approval, sandbox, writable roots, and protected paths behave? | [`codex-permissions-model.md`](./codex-permissions-model.md) |
| What should be covered in manual group-chat regression testing? | [`group-chat-manual-test-checklist.zh-CN.md`](./group-chat-manual-test-checklist.zh-CN.md) |

## Practical Reading Paths

- For architecture or large refactors: read [`feishu-codex-design.md`](./feishu-codex-design.md) first, then follow the topic-specific docs above.
- For continued `CodexHandler` / runtime ownership decomposition: read [`feishu-codex-design.md`](./feishu-codex-design.md) first, then [`codex-handler-decomposition-plan.md`](./codex-handler-decomposition-plan.md).
- For session or runtime bugs: read [`feishu-thread-lifecycle.md`](./feishu-thread-lifecycle.md), [`runtime-control-surface.md`](./runtime-control-surface.md), [`session-profile-semantics.md`](./session-profile-semantics.md), and [`shared-backend-resume-safety.md`](./shared-backend-resume-safety.md).
- For Feishu help, command discoverability, or button/slash consistency work: read [`feishu-help-navigation.md`](./feishu-help-navigation.md) and then the feature-specific docs it references.
- For wrapper or backend work: read [`fcodex-shared-backend-runtime.md`](./fcodex-shared-backend-runtime.md) and [`shared-backend-resume-safety.md`](./shared-backend-resume-safety.md).
- For permission or execution issues: read [`codex-permissions-model.md`](./codex-permissions-model.md) before changing product wording or runtime behavior.

## Language

- Most technical docs have both English and Simplified Chinese versions.
- The current group-chat manual test checklist is only available in Simplified Chinese.
