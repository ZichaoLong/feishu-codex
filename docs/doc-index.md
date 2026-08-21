# Docs Index

This directory is the source of truth for repository architecture, runtime
boundaries, and feature contracts.

## Reading Rule

When code and docs disagree, treat that as a contract gap. Tighten the code,
the docs, or both.

### Bilingual Fact Source

- Active bilingual pairs under `contracts/`, `architecture/`, and `decisions/`
  use the Chinese document as the canonical source and the English document as
  its synchronized peer. If their semantics conflict, the canonical Chinese
  document governs and the English drift is a contract gap that must be fixed.
- Each active Chinese document must declare itself the Chinese canonical source
  and link its exact English peer. Each English document must declare itself a
  synchronized English peer and link its exact canonical Chinese document.
- A semantic change should update both documents in the same change.
  `check-docs.sh` verifies pairs, roles, and exact reciprocal links; it does not
  pretend to prove semantic equivalence between two natural-language texts.
  That boundary still requires review.

### Cross-Frontend Precedence

For a question involving Web, Feishu, `focus` / `fcodex`, or the local control
plane acting on one shared thread, read the active contracts in this order:

1. `contracts/root-operation-owner.md` defines the sole main-turn writer and
   immediate release rule for lease-bearing Feishu/exclusive actions, plus the
   effect-specific boundary for ordinary Web/`fcodex` realtime input/control.
2. `contracts/fcodex-operation-owner.md` and
   `contracts/subagent-observation-and-recovery.md` add, respectively, fcodex
   transport details and the upstream-owned child-lifecycle, parent-history
   Tasks, direct-child-write, and cold-resume boundaries.
3. A frontend-specific lifecycle, settings, group, or command contract governs
   its own effect. It must neither extend lease-bearing main-turn ownership
   without a separately justified observable invariant nor reinterpret an
   ordinary realtime contributor as a writer.

This keeps retained recovery, subscription, presentation, or transport state
from accidentally becoming a durable main-turn writer rule.

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

- [`focusctl-command-matrix.md`](./contracts/focusctl-command-matrix.md)
- [`feishu-command-matrix.md`](./contracts/feishu-command-matrix.md)
- [`feishu-thread-lifecycle.md`](./contracts/feishu-thread-lifecycle.md)
- [`runtime-control-surface.md`](./contracts/runtime-control-surface.md)
- [`runtime-settings-fact-sources.md`](./contracts/runtime-settings-fact-sources.md)
- [`thread-next-load-settings-semantics.md`](./contracts/thread-next-load-settings-semantics.md)
- [`thread-profile-semantics.md`](./contracts/thread-profile-semantics.md)
- [`thread-resume-local-commit.md`](./contracts/thread-resume-local-commit.md)
- [`thread-create-local-commit.md`](./contracts/thread-create-local-commit.md)
- [`thread-memory-semantics.md`](./contracts/thread-memory-semantics.md)
- [`feishu-help-navigation.md`](./contracts/feishu-help-navigation.md)
- [`scheduled-prompts.md`](./contracts/scheduled-prompts.md)
- [`codex-config.md`](./contracts/codex-config.md)
- [`system-config.md`](./contracts/system-config.md)
- [`codex-permissions-model.md`](./contracts/codex-permissions-model.md)
- [`group-chat-contract.md`](./contracts/group-chat-contract.md)
- [`local-command-and-thread-profile-contract.md`](./contracts/local-command-and-thread-profile-contract.md)
- [`subagent-observation-and-recovery.md`](./contracts/subagent-observation-and-recovery.md)
- [`root-operation-owner.md`](./contracts/root-operation-owner.md)
- [`server-request-lifecycle.md`](./contracts/server-request-lifecycle.md)
- [`codex-app-server-schema-drift.md`](./contracts/codex-app-server-schema-drift.md)
- [`focus-web-wire.md`](./contracts/focus-web-wire.md)
- [`focus-web-prompt-mutation-recovery.md`](./contracts/focus-web-prompt-mutation-recovery.md)
- [`fcodex-operation-owner.md`](./contracts/fcodex-operation-owner.md)
- [`install-artifact-delivery.md`](./contracts/install-artifact-delivery.md)

### Architecture

- [`focus-design.md`](./architecture/focus-design.md)
- [`focus-shared-backend-runtime.md`](./architecture/focus-shared-backend-runtime.md)
- [`development-navigation.md`](./architecture/development-navigation.md)
- [`architecture-debt-register.md`](./architecture/architecture-debt-register.md)

### Decisions

- [`python-dependency-locking.md`](./decisions/python-dependency-locking.md)
- [`cross-instance-live-runtime-admission.md`](./decisions/cross-instance-live-runtime-admission.md)
- [`feishu-attachment-ingress.md`](./decisions/feishu-attachment-ingress.md)
- [`feishu-card-text-projection.md`](./decisions/feishu-card-text-projection.md)
- [`feishu-raw-card-retrieval.md`](./decisions/feishu-raw-card-retrieval.md)
- [`feishu-output-images.md`](./decisions/feishu-output-images.md)
- [`focus-web-ui-and-kimi-web-reuse.md`](./decisions/focus-web-ui-and-kimi-web-reuse.md)
- [`focus-web-external-access.md`](./decisions/focus-web-external-access.md)

### Verification

- [`group-chat-manual-test-checklist.zh-CN.md`](./verification/group-chat-manual-test-checklist.zh-CN.md)

### Archive

- [`codex-handler-decomposition-plan.md`](./archive/codex-handler-decomposition-plan.md)

## Read By Question

| Question | Read |
| --- | --- |
| What `focusctl` subcommands exist, which state layer each operates on, which mutate state, what the parameter constraints are, and how they map to the Feishu surface? | [`focusctl-command-matrix.md`](./contracts/focusctl-command-matrix.md) |
| What Feishu slash commands currently exist, which are reachable from `/help`, who may execute them, what buttons belong to them, and how do they map to local CLIs? | [`feishu-command-matrix.md`](./contracts/feishu-command-matrix.md) |
| What is the current architecture, layering, module split, and repository structure? | [`focus-design.md`](./architecture/focus-design.md) |
| How should repository work limit its initial read, expand a change cone, handle stale navigation, and close navigation impact without creating another fact source? | [`development-navigation.md`](./architecture/development-navigation.md) |
| Which architecture debts and upstream capability gaps remain active, in what dependency order, and with what acceptance criteria? | [`architecture-debt-register.md`](./architecture/architecture-debt-register.md) |
| What is the Feishu-side thread lifecycle, and what states must stay distinct? | [`feishu-thread-lifecycle.md`](./contracts/feishu-thread-lifecycle.md) |
| What shared state vocabulary and admin-surface contract apply to `/status`, `/detach`, and `focusctl`? | [`runtime-control-surface.md`](./contracts/runtime-control-surface.md) |
| How should questions like “what was just set”, “what is persisted now”, “when does it actually take effect”, and “does provisional state already have a formal fact source” be separated for runtime settings? | [`runtime-settings-fact-sources.md`](./contracts/runtime-settings-fact-sources.md), [`runtime-control-surface.md`](./contracts/runtime-control-surface.md) |
| Why were historical thread-wise next-load settings retired, and what remains after that retirement? | [`thread-next-load-settings-semantics.md`](./contracts/thread-next-load-settings-semantics.md), [`thread-profile-semantics.md`](./contracts/thread-profile-semantics.md) |
| What do `/threads`, `/resume`, and `/archive` mean across Feishu, `focus` / `fcodex`, and the TUI? | [`thread-profile-semantics.md`](./contracts/thread-profile-semantics.md) |
| After app-server reports `thread/resume` success, which local owner/interest must commit before the resume transaction is settled, and when may failure compensate instead of retain recovery? | [`thread-resume-local-commit.md`](./contracts/thread-resume-local-commit.md) |
| Which minimal local commit follows a typed Web/Feishu or targetless `focus` / `fcodex` `thread/start` response, and why must an unknown create stay scoped to that request without automatic retry? | [`thread-create-local-commit.md`](./contracts/thread-create-local-commit.md) |
| Why was the historical thread-memory surface removed, and which two setting layers replace it now? | [`thread-memory-semantics.md`](./contracts/thread-memory-semantics.md), [`runtime-settings-fact-sources.md`](./contracts/runtime-settings-fact-sources.md) |
| What is the formal boundary for continuing a Feishu-bound thread later, including `binding/submit-prompt`, `focusctl prompt send`, and the Linux `systemd --user` skill? | [`scheduled-prompts.md`](./contracts/scheduled-prompts.md) |
| Which keys and types does `codex.yaml` accept, and where are defaults and validation authoritative? | [`codex-config.md`](./contracts/codex-config.md) |
| Which instance identity, trigger, network, and history-recovery fields does `system.yaml` accept, and how is `/init` allowed to update it? | [`system-config.md`](./contracts/system-config.md) |
| What is the current contract for `/detach`, a thinner `focus` / `fcodex`, and the `focusctl` split? | [`local-command-and-thread-profile-contract.md`](./contracts/local-command-and-thread-profile-contract.md) |
| How do multi-instance `default` / named-instance behavior, shared thread visibility, `focus --instance` / `fcodex --instance`, and the global runtime lease work? | [`thread-profile-semantics.md`](./contracts/thread-profile-semantics.md), [`runtime-control-surface.md`](./contracts/runtime-control-surface.md), [`focus-shared-backend-runtime.md`](./architecture/focus-shared-backend-runtime.md) |
| What information architecture and semantic rules does the Feishu `/help` navigation surface follow? | [`feishu-help-navigation.md`](./contracts/feishu-help-navigation.md) |
| What is the formal behavior contract for group activation, group modes, history recovery, and group-command triggering? | [`group-chat-contract.md`](./contracts/group-chat-contract.md) |
| How do approval, sandbox, writable roots, and protected paths behave? | [`codex-permissions-model.md`](./contracts/codex-permissions-model.md) |
| How does `focus` / `fcodex` shared-backend mode work, including wrapper, proxy, and `--cd` semantics? | [`focus-shared-backend-runtime.md`](./architecture/focus-shared-backend-runtime.md) |
| What safety rules apply to shared backend reuse and `/resume`? | [`focus-shared-backend-runtime.md`](./architecture/focus-shared-backend-runtime.md), [`thread-resume-local-commit.md`](./contracts/thread-resume-local-commit.md), [`root-operation-owner.md`](./contracts/root-operation-owner.md) |
| What boundary should Feishu attachment / file-message support follow, including what gets downloaded and what remains outside this repository? | [`feishu-attachment-ingress.md`](./decisions/feishu-attachment-ingress.md) |
| What is the boundary for Feishu card text projection, terminal `final_reply_text`, and best-effort extraction from ordinary cards? | [`feishu-card-text-projection.md`](./decisions/feishu-card-text-projection.md) |
| How should Feishu card reads move from JSON 2.0 display output to `message_id`-based raw-card retrieval, and what is the read decision across ordinary forwards, merge-forwards, and best-effort projection? | [`feishu-raw-card-retrieval.md`](./decisions/feishu-raw-card-retrieval.md) |
| What is the current boundary for Feishu outbound generated images, including text-before-image ordering and why arbitrary workspace images are out of scope? | [`feishu-output-images.md`](./decisions/feishu-output-images.md) |
| Why does Focus Web reuse kimi-web source, who owns the adapter/projection after import, and how are provenance and license obligations maintained? | [`focus-web-ui-and-kimi-web-reuse.md`](./decisions/focus-web-ui-and-kimi-web-reuse.md) |
| What are the upstream-lifecycle, parent-history Tasks, direct-child-write, and cold-resume boundaries for spawned subagents, and why does Focus not observe or recover children or redeliver child results? | [`subagent-observation-and-recovery.md`](./contracts/subagent-observation-and-recovery.md) |
| Which Feishu/exclusive actions still acquire a main-turn writer, why ordinary Web/`fcodex` input does not, why effect-specific steer/interrupt does not transfer a writer, and when an existing writer is released? | [`root-operation-owner.md`](./contracts/root-operation-owner.md) |
| Who owns pending Codex server requests, how are replay, response, lifecycle, disconnect, and backend reset projected, and why is there no durable Focus request fence? | [`server-request-lifecycle.md`](./contracts/server-request-lifecycle.md) |
| How does Focus detect Codex app-server method/schema drift during an upgrade, and which generated artifacts and classifications must be reviewed? | [`codex-app-server-schema-drift.md`](./contracts/codex-app-server-schema-drift.md) |
| Where is the single source for Focus Web endpoints, events, DTO required fields, and closed enums, and where do invalid HTTP responses or events fail closed? | [`focus-web-wire.md`](./contracts/focus-web-wire.md) |
| How does an existing-thread Web prompt use one POST and a bounded result receipt to limit duplicate effects, while F5, disconnect, reordering, or an unknown outcome only queries the result without replaying the payload or restoring attachments? | [`focus-web-prompt-mutation-recovery.md`](./contracts/focus-web-prompt-mutation-recovery.md) |
| How do fcodex connections separate ordinary realtime input from exclusive actions, constrain targetless app-server RPCs, and keep transport/server-request recovery separate from writer authority? | [`root-operation-owner.md`](./contracts/root-operation-owner.md), [`fcodex-operation-owner.md`](./contracts/fcodex-operation-owner.md) |
| What is Focus Web's self-hosted external-access boundary, including loopback defaults, reverse proxy, shared trust, and future public exposure? | [`focus-web-external-access.md`](./decisions/focus-web-external-access.md) |
| After cloning, where does the default install obtain Focus and the Web build, and what governs stable, development, local bundles, download validation, and explicit publication? | [`install-artifact-delivery.md`](./contracts/install-artifact-delivery.md), [`python-dependency-locking.md`](./decisions/python-dependency-locking.md) |
| Where are Python runtime, build, and development dependencies declared; how are locks regenerated or explicitly upgraded; and what reproducibility does installation actually guarantee? | [`python-dependency-locking.md`](./decisions/python-dependency-locking.md) |
| What cross-instance safety rule applies before attach / resume, and why is `ThreadRuntimeLease` alone not enough? | [`cross-instance-live-runtime-admission.md`](./decisions/cross-instance-live-runtime-admission.md), [`runtime-control-surface.md`](./contracts/runtime-control-surface.md) |
| What should be covered in manual group-chat regression testing? | [`group-chat-manual-test-checklist.zh-CN.md`](./verification/group-chat-manual-test-checklist.zh-CN.md) |
| What historical rollout plan was used to decompose `CodexHandler` ownership? | [`codex-handler-decomposition-plan.md`](./archive/codex-handler-decomposition-plan.md) |

## Practical Reading Paths

- For any repository diagnosis, implementation, review, or refactor:
  - use `Use $develop-focus to complete: <task>` to activate the current full development discipline
  - [`development-navigation.md`](./architecture/development-navigation.md)
  - then only the capability refs and evidence-triggered adjacent sources it directs
- For architecture or large refactors:
  - [`focus-design.md`](./architecture/focus-design.md)
  - [`architecture-debt-register.md`](./architecture/architecture-debt-register.md)
  - then the relevant `contracts/` and `decisions/` docs
- For session or runtime bugs:
  - [`feishu-thread-lifecycle.md`](./contracts/feishu-thread-lifecycle.md)
  - [`runtime-control-surface.md`](./contracts/runtime-control-surface.md)
  - [`runtime-settings-fact-sources.md`](./contracts/runtime-settings-fact-sources.md)
  - [`thread-profile-semantics.md`](./contracts/thread-profile-semantics.md)
  - [`thread-resume-local-commit.md`](./contracts/thread-resume-local-commit.md)
  - [`local-command-and-thread-profile-contract.md`](./contracts/local-command-and-thread-profile-contract.md)
  - [`cross-instance-live-runtime-admission.md`](./decisions/cross-instance-live-runtime-admission.md)
- For group-chat work:
  - [`feishu-command-matrix.md`](./contracts/feishu-command-matrix.md)
  - [`group-chat-contract.md`](./contracts/group-chat-contract.md)
  - [`feishu-help-navigation.md`](./contracts/feishu-help-navigation.md)
  - [`group-chat-manual-test-checklist.zh-CN.md`](./verification/group-chat-manual-test-checklist.zh-CN.md)
- For local `focusctl` inspection / management work:
  - [`focusctl-command-matrix.md`](./contracts/focusctl-command-matrix.md)
  - [`scheduled-prompts.md`](./contracts/scheduled-prompts.md)
  - [`local-command-and-thread-profile-contract.md`](./contracts/local-command-and-thread-profile-contract.md)
  - [`runtime-control-surface.md`](./contracts/runtime-control-surface.md)
  - [`runtime-settings-fact-sources.md`](./contracts/runtime-settings-fact-sources.md)
  - [`thread-profile-semantics.md`](./contracts/thread-profile-semantics.md)
- For install bundles, local artifact builds, or artifact publication:
  - [`install-artifact-delivery.md`](./contracts/install-artifact-delivery.md)
  - [`python-dependency-locking.md`](./decisions/python-dependency-locking.md)
- For `focus` / `fcodex` wrapper or backend work:
  - [`local-command-and-thread-profile-contract.md`](./contracts/local-command-and-thread-profile-contract.md)
  - [`root-operation-owner.md`](./contracts/root-operation-owner.md)
  - [`fcodex-operation-owner.md`](./contracts/fcodex-operation-owner.md)
  - [`focus-shared-backend-runtime.md`](./architecture/focus-shared-backend-runtime.md)
- For multi-instance behavior, shared thread visibility, `focusctl --instance`,
  or cross-instance runtime lease work:
  - [`thread-profile-semantics.md`](./contracts/thread-profile-semantics.md)
  - [`runtime-control-surface.md`](./contracts/runtime-control-surface.md)
  - [`cross-instance-live-runtime-admission.md`](./decisions/cross-instance-live-runtime-admission.md)
  - [`focus-shared-backend-runtime.md`](./architecture/focus-shared-backend-runtime.md)
- For Feishu attachment ingress, file messages, local staging, or image-input
  upgrade work:
  - [`feishu-attachment-ingress.md`](./decisions/feishu-attachment-ingress.md)
  - [`feishu-output-images.md`](./decisions/feishu-output-images.md)
  - [`codex-permissions-model.md`](./contracts/codex-permissions-model.md)
  - [`group-chat-contract.md`](./contracts/group-chat-contract.md)
- For Feishu card messages, terminal-result round-trip, or best-effort text
  extraction from ordinary cards:
  - [`feishu-card-text-projection.md`](./decisions/feishu-card-text-projection.md)
  - [`feishu-raw-card-retrieval.md`](./decisions/feishu-raw-card-retrieval.md)
  - [`feishu-output-images.md`](./decisions/feishu-output-images.md)
  - [`feishu-thread-lifecycle.md`](./contracts/feishu-thread-lifecycle.md)
  - [`focus-design.md`](./architecture/focus-design.md)
- For permission or execution wording:
  - [`codex-permissions-model.md`](./contracts/codex-permissions-model.md)
- For a browser frontend, rich Markdown, remote Web access, or kimi-web reuse:
  - [`focus-web-wire.md`](./contracts/focus-web-wire.md)
  - [`focus-web-prompt-mutation-recovery.md`](./contracts/focus-web-prompt-mutation-recovery.md)
  - [`focus-web-ui-and-kimi-web-reuse.md`](./decisions/focus-web-ui-and-kimi-web-reuse.md)
  - [`focus-web-external-access.md`](./decisions/focus-web-external-access.md)
  - [`root-operation-owner.md`](./contracts/root-operation-owner.md)
  - [`subagent-observation-and-recovery.md`](./contracts/subagent-observation-and-recovery.md)
  - [`focus-design.md`](./architecture/focus-design.md)
  - [`focus-shared-backend-runtime.md`](./architecture/focus-shared-backend-runtime.md)

## Language

- Most technical docs have both English and Simplified Chinese versions.
- The current manual group-chat verification checklist is only available in
  Simplified Chinese.
