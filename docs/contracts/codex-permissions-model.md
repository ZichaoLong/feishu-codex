# Codex Permissions Model

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/codex-permissions-model.zh-CN.md`.

This document records how FOCUS now exposes `approval_policy` and
`permissions_profile_id`, and how they relate to upstream legacy `sandbox` and
canonical `permissions`.

It exists for two reasons:

- keep Focus frontend wording aligned with upstream Codex behavior
- separate concise user-facing help from implementation and troubleshooting detail

Upstream baseline:

- Codex source repository: [`openai/codex`](https://github.com/openai/codex.git)
- Historical local validation baseline for the upstream details and line links
  below: `codex-cli 0.118.0`, resolved locally to
  upstream tag `rust-v0.118.0`
  (`b630ce9a4e754d35a1f33e4366ba638d18626142`) and checked on 2026-04-03
- Upstream file/line references below are pinned to that commit so later
  readers can recover the exact source snapshot discussed here
- It is not a claim that all current app-server behavior is pinned to this
  version; current Web/multi-frontend contracts identify their own baseline.
- Focus's closed approval/reviewer surface was rechecked against upstream commit
  [`f21dc4638803f40046c9e294b0349782928f6b36`](https://github.com/openai/codex/commit/f21dc4638803f40046c9e294b0349782928f6b36)
  (2026-08-05). That revision includes
  structured `granular` approval and `auto_review`; neither is enabled by
  Focus's current product contract.
- Managed-requirements lifecycle behavior was rechecked at upstream commit
  [`f4cfbaf90af76f7c0b3301e931d8a58f4b56cc31`](https://github.com/openai/codex/commit/f4cfbaf90af76f7c0b3301e931d8a58f4b56cc31)
  (2026-08-08). Depending on the
  concrete path and whether the thread is already loaded, it may fall back,
  ignore, or reject; it does not provide one uniform pre-effect admission rule.

## 1. The Three Layers

FOCUS now exposes two formal runtime settings and still needs to
explain one upstream legacy concept:

1. `approval_policy`
- when execution should pause for approval before continuing

2. `permissions_profile_id`
- which upstream permission profile id Focus injects for a later turn

3. legacy `sandbox`
- still supported upstream, but no longer a formal persisted Feishu-side
  setting

The important point is that the Feishu-side `/permissions` command no longer
means a product preset. It now maps directly to upstream canonical
`permissions` profile ids. Other Focus frontends use the same upstream setting
without changing its meaning, but that does not give them one persisted fact:
Feishu remains binding-wise, while Web uses durable `WebNextTurnSettings`
shared by every browser/thread in one instance. Neither auto-merges with local
TUI state. Main-turn admission routes a live mutation or reviewer request. It
neither owns nor blocks the instance-wide Web settings mutation and does not
auto-merge settings across frontends. The [runtime-settings fact-source
contract](./runtime-settings-fact-sources.md) alone defines application
boundaries.

## 2. Approval vs Sandbox

The cleanest mental model is:

- `sandbox` is the technical execution boundary
- `approval_policy` is the approval boundary

That model is substantially correct, but it needs a few precision notes.

### 2.1 Approval is not literally always "human approval" upstream

Upstream Codex models approval as a policy and a reviewer flow, not strictly as
"a human must click approve".

Focus's canonical adapter currently sends `approvals_reviewer=user`. At the Focus boundary, a
canonical command, file-change, or permission approval in the current backend
epoch, with a non-empty `turnId` for an exact direct root, is projected to its
exact Feishu binding and to every authenticated live Web/fcodex endpoint which
has materialized that root. Each
surface receives an exact one-shot capability; the first valid response wins.
Other Feishu chats never join this domain, and user-input, MCP, authentication,
dynamic-tool, and child-thread requests keep their existing exact route. Thus
describing `approval_policy` as the approval boundary remains accurate without
making the turn writer the only possible approver. A writer-less autonomous
goal turn does not lose its current canonical approval for that reason.

Each frontend may apply additional local actor rules after that cross-frontend
admission. In an **active** Feishu group, an administrator may be a fallback
operator for the exact-binding card under the group interaction guard. A
trusted local Web/fcodex endpoint has separate administrator-equivalent
authority for the same shared approval, but that never authorizes another
Feishu binding or grants writer, settings, goal, binding, or backend-reset
authority.
After group deactivation, member-origin pending requests are fail-closed, and
an unconfirmed cancellation is a blocker rather than an administrator
takeover. Only an original admin-origin request remains ordinary administrator
work. Browser/socket loss drops only the local projection; upstream resume
replay may create a fresh actionable capability.

The option surface follows upstream protocol shape rather than a single
request-field rule: command approval honors `availableDecisions`, while file
and permission approval expose their schema-defined choices, including session
approval and permission `strictAutoReview`. Focus never creates a response enum
which upstream does not define.

Current upstream also exposes `auto_review`, which delegates review to a
prompted subagent.  Focus does not yet have a reviewed interaction, ownership,
or audit contract for that route, so `codex.yaml` continues to accept only
`user`.  An upstream enum addition is a capability gap to assess, not an
implicit local feature. Native fcodex TUI transport is separate: it preserves
the TUI's upstream-owned reviewer field and does not turn that value into a
Focus setting, card route, or fact source.

This boundary must hold at the thread lifecycle RPCs, rather than relying only
on process configuration. Focus adapter `thread/start` / `thread/resume`
requests replace guest values and explicitly request
`approvalsReviewer=user`; only a response that also reports `user` is an
acceptable known success on that canonical adapter path. Native fcodex
`thread/start` / `thread/resume` instead preserves the upstream TUI payload
after exact-target admission. Upstream may ignore a reviewer override on an
already-loaded thread and report its existing reviewer, so the proxy does not
rewrite that field or use a mismatch as transport-quarantine evidence. This
does not grant fcodex reviewer ownership or change Focus adapter safety. See
[`fcodex-operation-owner.md`](./fcodex-operation-owner.md#direct-thread-targets).

For a continuation-capable cold resume carrying explicit approval/permissions,
Focus also requires the response `approvalPolicy` and
`activePermissionProfile.id` to prove that the requested values took effect.
This postcondition does not replace the pre-resume override; it turns an
ignored override, missing field, or protocol drift into an unknown outcome
instead of reporting success after a persisted goal continued under an
unproven safety profile.

Connection initialization validates the `configRequirements/read` response
envelope and the one optional constraint Focus actually depends on,
`allowedApprovalsReviewers`: missing/null means no such constraint; a non-null
value must be an array of strings containing `user`. `allowedApprovalPolicies`,
`allowedSandboxModes`, and `allowedPermissionProfiles` constrain concrete
values upstream; they do not assert that every value in Focus's static catalog
must be available on this connection. A partial or empty value in any of these
three non-reviewer allow-lists therefore does not quarantine the shared
backend, and Focus does not cache those fields as a cross-time, cross-cwd, or
cross-frontend availability catalog.

Focus menus consequently describe the static vocabulary this project can send,
not a promise that current upstream policy accepts every value. Upstream still
owns the concrete effect and may fall back, ignore, or reject. `thread/start`
records the effective values reported by its response without adding a new
exact-match contract, while a cold resume carrying explicit safety overrides
retains the preceding postcondition. A future UI which only shows currently
available values would require config revision/invalidation or atomic
validate-and-effect authority; it cannot be derived from a one-time snapshot.

Relevant references:

- [`codex-rs/protocol/src/protocol.rs:L627`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/protocol/src/protocol.rs#L627)
- [Focus `codex.yaml` configuration example](../../config/codex.yaml.example)

### 2.2 Sandbox is not a different toolset

Changing `sandbox` does not primarily swap the available tool list.
It changes the execution constraints applied to the same shell commands and
tools.

For example:

- `read-only` does not mean "only read commands exist"
- `workspace-write` does not mean "a different shell is used"
- `danger-full-access` does not mean "extra tools appear"

The more accurate statement is:

- the model receives different permission context
- the runtime applies different OS-level restrictions to command execution

That is why sandbox changes can feel like a tool change even when the core
tooling surface is the same.

## 3. Upstream Approval Semantics

Focus's current user-selectable approval surface includes:

- `untrusted`
  - only known-safe commands that only read files are auto-approved
- `on-request`
  - the model decides when to ask for approval
- `never`
  - approval is never requested; failures return directly
- `on-failure`
  - deprecated upstream

Current upstream additionally includes a structured `granular` policy.  Focus
does not expose it yet: its configuration and frontend contracts currently
store a scalar policy enum and have not defined how granular rule/sandbox/skill
approval controls interact with the exact reviewer/action-capability route.  Unsupported
does not mean silently mapping `granular` to another policy; local config
rejects it until that contract is designed.

This repository no longer exposes `on-failure` on the user-facing Feishu
surface. If an old local config still contains it, the config layer normalizes
it to `on-request`.

Relevant upstream references:

- [`codex-rs/protocol/src/protocol.rs:L627`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/protocol/src/protocol.rs#L627)
- [`codex-rs/core/src/codex.rs:L1648`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/core/src/codex.rs#L1648)

Wording to avoid:

- "untrusted means only read commands are allowed"
- "never means commands are unrestricted"

Those are wrong because approval policy is about escalation flow, not the full
runtime restriction model.

## 4. Upstream Sandbox Semantics

The platform sandbox selection is explicit upstream:

- macOS: Seatbelt
- Linux: Linux sandbox helper, using bubblewrap by default
- Windows: restricted-token sandbox, with an elevated pipeline available

Relevant upstream references:

- [`codex-rs/sandboxing/src/manager.rs:L49`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/sandboxing/src/manager.rs#L49)
- [`codex-rs/linux-sandbox/src/lib.rs:L1`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/linux-sandbox/src/lib.rs#L1)
- [`codex-rs/core/src/seatbelt.rs:L1`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/core/src/seatbelt.rs#L1)
- [`codex-rs/features/src/lib.rs:L110`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/features/src/lib.rs#L110)
- [`codex-rs/windows-sandbox-rs/src/elevated/command_runner_win.rs:L1`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/windows-sandbox-rs/src/elevated/command_runner_win.rs#L1)
- [`codex-rs/windows-sandbox-rs/src/token.rs:L308`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/windows-sandbox-rs/src/token.rs#L308)

This is why Docker is only a loose analogy.
Codex does not primarily switch to a separate image or alternate rootfs model.
It uses host-native process sandboxing mechanisms.

### 4.1 Linux

The Linux helper states this directly:

- in-process restrictions via `no_new_privs` and `seccomp`
- bubblewrap for filesystem isolation

So the practical model is closer to "lightweight process sandboxing on the host"
than "run this task in a full container image".

### 4.2 macOS

The macOS path uses Seatbelt policy generation and executes the command under
the Seatbelt entrypoint.

### 4.3 Windows

The Windows path uses restricted tokens, and upstream also contains an elevated
sandbox pipeline with a dedicated runner.

That makes "restricted token / elevated runner" a meaningful upstream reference,
not a hand-wavy analogy.

## 5. Writable Roots and Protected Paths

`workspace-write` should not be described too loosely as "can write the working
directory".

The more precise statement is:

- writes are allowed within configured writable roots
- some protected top-level paths inside those roots remain read-only by default

Upstream currently protects at least:

- `.git`
- `.agents`
- `.codex`

Relevant upstream reference:

- [`codex-rs/protocol/src/permissions.rs:L1098`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/protocol/src/permissions.rs#L1098)

This distinction matters because it explains why an agent can often edit project
files while still being blocked from repo metadata or Codex metadata.

## 6. Why Sandboxing Sometimes "Feels Broken"

Sandbox behavior often fails in one of two very different ways:

1. the sandbox is working and is correctly blocking a write, network access, or
   protected path
2. the sandbox backend itself failed to bootstrap

In the second case, even harmless read commands may fail before the target
command actually runs.

That can make users think:

- the read permission is wrong
- the tool is missing
- Codex changed the command set

But the real failure is often earlier in the sandbox setup path.

While verifying this repo, the local environment reproduced exactly that class
of failure:

```text
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
```

That is strong evidence that troubleshooting guidance belongs in documentation,
not just user folklore.

## 7. Troubleshooting Reference

Upstream CLI includes explicit sandbox debugging subcommands:

- `codex sandbox linux`
- `codex sandbox macos`
- `codex sandbox windows`

Relevant upstream reference:

- [`codex-rs/cli/src/main.rs:L252`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/cli/src/main.rs#L252)

Recommended troubleshooting flow:

1. distinguish policy denial from sandbox bootstrap failure
2. verify which platform backend is expected
3. test the platform sandbox subcommand directly
4. if an outer VM/container already provides isolation, consider whether the
   inner Codex sandbox is still useful or is just conflicting with the host
   environment

## 8. Recommended Product Wording

For user-facing docs in FOCUS, the safest wording is:

- `sandbox` controls the technical execution boundary
- `approval_policy` controls when approval is required before continuing
- `/permissions` sets the upstream canonical `permissions` profile id independently
- `/approval` sets the approval policy independently
- the Feishu surface no longer exposes a `/sandbox` user command

Good concise wording:

- "`/permissions` decides which permissions baseline future turns use."
- "`/approval` decides when the run must stop for approval."
- "The permissions baseline controls the execution boundary; the approval policy controls whether execution must pause for approval."

Avoid overcommitting to unstable implementation details in the top-level README.
The detailed backend references belong in a dedicated document like this one.
