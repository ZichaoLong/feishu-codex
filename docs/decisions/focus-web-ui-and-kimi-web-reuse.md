# Focus Web UI and kimi-web Reuse Decision

Document role: synchronized English peer. Canonical Chinese: `docs/decisions/focus-web-ui-and-kimi-web-reuse.zh-CN.md`.

> Status: accepted. Focus Web initially derived source from kimi-web, but its
> product, visual design, features, and architecture are now Focus-owned.
> kimi-web is no longer an ongoing alignment or merge target.

## 1. Problem and Decision

Focus needed a desktop- and mobile-friendly browser frontend that could reliably
render Markdown, code, math, diagrams, diffs, and structured tools. Reimplementing
that mature presentation stack had no product benefit, so the initial frontend
reused kimi-web's proven Vue shell, responsive layout, rich renderers, design
system, and structured presentation components.

This decision grants only a source-reuse and derived-maintenance boundary. It
does not import kimi-web product or backend semantics into Focus:

- kap-server transport, Kimi session/provider semantics, product branding, and
  the equal-writer queue are not reused;
- the browser connects only to the Focus-owned Gateway and consumes only
  Focus-owned DTOs and projection events;
- Gateway, adapter, projection, state owners, and mutation admission follow
  Focus's formal contracts;
- imported code may be freely changed, renamed, split, or replaced according
  to Focus ownership, maintainability, and product needs.

This decision is therefore neither a current Web feature inventory nor a runtime
contract. Current product and protocol facts belong only to the formal sources
linked in Section 5.

## 2. Focus-owned Adapter and Projection

`web/src/focus/` is the browser-side Focus ownership layer. It owns Focus
transport, wire decoding, projection, browser-local state owners, and mutation
coordination. The Python Gateway and corresponding runtime owners project
app-server facts into the Focus wire. The browser neither depends on the full
Codex schema nor connects directly to app-server.

A Kimi-derived component may render a read-only projection and invoke a typed
callback. It cannot acquire transport, thread-lifecycle, settings, pending-
interaction, mutation-recovery, or runtime authority. Component directories and
filenames may evolve with Focus architecture; provenance does not require Kimi's
module boundaries to remain intact.

Focus does not provide a kap-server compatibility facade. Protocol differences
are interpreted explicitly at the Focus-owned adapter/projection boundary rather
than preserving two backend semantics behind a simulated legacy API.

## 3. Source-Reuse Boundary

The initial import retained kimi-web implementation assets for the browser shell,
mobile layout, Markdown, KaTeX, Mermaid, Shiki, diffs, and structured tool
presentation. Retaining an imported component is not a promise to expose its
former backend API or product capability. A surface without a formal Focus
contract stays unavailable.

Focus-owned evolution does not require:

- tracking new Kimi commits or UI changes;
- minimizing the source diff between Focus and Kimi;
- preserving old directories, APIs, or abstractions for a future merge;
- automatically absorbing later Kimi features.

A future import from a newer Kimi revision requires a separate explicit decision
and review of the actual change. It is not a routine development, build, or
release gate.

## 4. Provenance, License, and Release Obligations

The source facts are fixed as follows:

- upstream repository: [`MoonshotAI/kimi-code`](https://github.com/MoonshotAI/kimi-code);
- initial imported commit:
  [`c497af60e6cd20aab05e590f98a28fb15dd3491d`](https://github.com/MoonshotAI/kimi-code/commit/c497af60e6cd20aab05e590f98a28fb15dd3491d);
- detailed import record and maintenance procedure: [`web/UPSTREAM.md`](../../web/UPSTREAM.md);
- per-file provenance inventory: [`web/provenance/kimi-web-files.json`](../../web/provenance/kimi-web-files.json);
- retained MIT license: [`web/licenses/kimi-web-MIT.txt`](../../web/licenses/kimi-web-MIT.txt);
- shipped notices: [`web/THIRD_PARTY_NOTICES.md`](../../web/THIRD_PARTY_NOTICES.md).

Changing a registered Kimi-derived file requires updating and reviewing its local
modification digest through the procedure in `web/UPSTREAM.md`.
`focus_owned_files` identifies source with no Kimi counterpart; adding or
reclassifying a file requires explicit review and cannot be guessed by a script.
Builds and releases continue to ship the applicable MIT, font, icon, and
dependency notices.

Provenance verification may read a developer-supplied local kimi-code checkout.
Runtime, installation, ordinary builds, and release do not depend on that
checkout. Synchronizing digests is not synchronization with a newer Kimi version.

## 5. Formal Sources for Current Behavior

| Question | Current source of truth |
| --- | --- |
| Overall layering, Gateway, and application-transaction owners | [`focus-design.md`](../architecture/focus-design.md) |
| Shared-backend topology across Feishu, Web, and `focus` / `fcodex` | [`focus-shared-backend-runtime.md`](../architecture/focus-shared-backend-runtime.md) |
| Web endpoints, events, DTOs, history, and tool-detail projection | [`focus-web-wire.md`](../contracts/focus-web-wire.md) |
| One-effect existing-thread prompt and F5/unknown recovery | [`focus-web-prompt-mutation-recovery.md`](../contracts/focus-web-prompt-mutation-recovery.md) |
| Fact sources and application boundaries for Web and Feishu settings | [`runtime-settings-fact-sources.md`](../contracts/runtime-settings-fact-sources.md) |
| Ordinary realtime input, exclusive actions, steer, and interrupt | [`root-operation-owner.md`](../contracts/root-operation-owner.md) |
| Local commit after `thread/start` / `thread/resume` | [`thread-create-local-commit.md`](../contracts/thread-create-local-commit.md), [`thread-resume-local-commit.md`](../contracts/thread-resume-local-commit.md) |
| Multi-frontend lifecycle for approvals, questions, and MCP requests | [`server-request-lifecycle.md`](../contracts/server-request-lifecycle.md) |
| Subagent and parent-history Tasks boundaries | [`subagent-observation-and-recovery.md`](../contracts/subagent-observation-and-recovery.md) |
| Trusted-proxy external access | [`focus-web-external-access.md`](./focus-web-external-access.md) |

This decision explains only source provenance and derived ownership. Changes to
the contracts above must not be copied back here as another behavior description.
