# Codex Permissions Model

This document records the upstream semantics behind `approval_policy`,
`sandbox`, and the `permissions` presets exposed by `feishu-codex`.

It exists for two reasons:

- keep the Feishu-side wording aligned with upstream Codex behavior
- separate concise user-facing help from implementation and troubleshooting detail

Upstream baseline:

- Codex source repository: [`openai/codex`](https://github.com/openai/codex.git)
- Current local validation baseline: `codex-cli 0.118.0` (checked on 2026-04-03)
- File/line references below are implementation breadcrumbs verified locally
  against that baseline; they are useful for orientation, not guaranteed stable
  permalinks

## 1. The Three Layers

`feishu-codex` exposes three closely related concepts:

1. `approval_policy`
- when execution should pause for approval before continuing

2. `sandbox`
- what filesystem and network restrictions commands run under

3. `permissions`
- a Feishu-side preset that changes both values together

The important point is that `permissions` is not an upstream primitive.
It is a product convenience layer over the two upstream knobs:

- `approval_policy`
- `sandbox`

## 2. Approval vs Sandbox

The cleanest mental model is:

- `sandbox` is the technical execution boundary
- `approval_policy` is the approval boundary

That model is substantially correct, but it needs a few precision notes.

### 2.1 Approval is not literally always "human approval" upstream

Upstream Codex models approval as a policy and a reviewer flow, not strictly as
"a human must click approve".

In this repo's current product contract, the default reviewer is still the
Feishu user, so describing `approval_policy` as the approval boundary is
accurate for current product behavior.

Relevant upstream references:

- [`protocol.rs:627`](/home/zlong/llm/codex/codex-rs/protocol/src/protocol.rs)
- [`codex.yaml.example:35`](/home/zlong/llm/feishu-codex/config/codex.yaml.example)

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

Upstream `AskForApproval` currently includes:

- `untrusted`
  - only known-safe commands that only read files are auto-approved
- `on-request`
  - the model decides when to ask for approval
- `never`
  - approval is never requested; failures return directly
- `on-failure`
  - deprecated upstream

This repository no longer exposes `on-failure` on the user-facing Feishu
surface. If an old local config still contains it, the config layer normalizes
it to `on-request`.

Relevant upstream references:

- [`protocol.rs:627`](/home/zlong/llm/codex/codex-rs/protocol/src/protocol.rs)
- [`codex.rs:1648`](/home/zlong/llm/codex/codex-rs/core/src/codex.rs)

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

- [`manager.rs:49`](/home/zlong/llm/codex/codex-rs/sandboxing/src/manager.rs)
- [`linux-sandbox/lib.rs:1`](/home/zlong/llm/codex/codex-rs/linux-sandbox/src/lib.rs)
- [`seatbelt.rs:1`](/home/zlong/llm/codex/codex-rs/core/src/seatbelt.rs)
- [`features/lib.rs:110`](/home/zlong/llm/codex/codex-rs/features/src/lib.rs)
- [`command_runner_win.rs:1`](/home/zlong/llm/codex/codex-rs/windows-sandbox-rs/src/elevated/command_runner_win.rs)
- [`token.rs:307`](/home/zlong/llm/codex/codex-rs/windows-sandbox-rs/src/token.rs)

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

- [`permissions.rs:1098`](/home/zlong/llm/codex/codex-rs/protocol/src/permissions.rs)

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

- [`main.rs:252`](/home/zlong/llm/codex/codex-rs/cli/src/main.rs)

Recommended troubleshooting flow:

1. distinguish policy denial from sandbox bootstrap failure
2. verify which platform backend is expected
3. test the platform sandbox subcommand directly
4. if an outer VM/container already provides isolation, consider whether the
   inner Codex sandbox is still useful or is just conflicting with the host
   environment

## 8. Recommended Product Wording

For user-facing docs in `feishu-codex`, the safest wording is:

- `sandbox` controls the technical execution boundary
- `approval_policy` controls when approval is required before continuing
- `permissions` is a preset that changes both together

Good concise wording:

- "`sandbox` decides what filesystem and network boundary commands run under."
- "`approval_policy` decides when the run must stop for approval."

Avoid overcommitting to unstable implementation details in the top-level README.
The detailed backend references belong in a dedicated document like this one.
