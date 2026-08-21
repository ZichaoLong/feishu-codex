# Feishu Attachment Ingress and Local Staging Boundary

Document role: synchronized English peer. Canonical Chinese: `docs/decisions/feishu-attachment-ingress.zh-CN.md`.

See also:

- `docs/architecture/focus-design.md` for the current architecture and
  repository boundaries
- `docs/contracts/codex-permissions-model.md` for `sandbox`, `approval`, and
  writable-root semantics
- `docs/contracts/group-chat-contract.md` for group activation, group modes, and
  triggering boundaries
- `docs/decisions/feishu-output-images.md` for the opposite direction: Codex
  image results back to Feishu

## 1. Problem Statement

Users want to send attachments in Feishu and continue working with local Codex
through FOCUS.

The historical **pre-rollout** baseline was:

- file messages were rejected explicitly
- app-server turn input was mostly text-centric
- complex file parsing should not become a long-term maintenance obligation of
  FOCUS itself

The first two points explain why this boundary was chosen; they are not a
description of the current implementation. The current first-stage ingress is
implemented and summarized in Sections 4 and 12. Apart from the explicit
native image input described below, complex attachment interpretation still
does not become a FOCUS responsibility.

If this repository tries to embed PDF extraction, Office parsing, OCR,
audio/video transcription, and archive handling directly, it creates:

- unclear product boundaries
- high maintenance cost
- a large amount of ambiguity tied to local environments, MCP setup, and
  upstream model/tool capability

This document defines a narrower and more stable boundary.

## 2. Decision Summary

The first-phase repository decision for Feishu attachment support is:

1. FOCUS is only responsible for **accepting downloadable Feishu
   attachment messages, downloading the resource, staging it under the current
   working directory, and handing the local path to Codex**.
2. Complex file interpretation is not a repository responsibility. It should be
   handled by:
   - Codex native capability
   - user-configured MCP / Apps
   - local tools already available in the environment
3. Images are the only attachment type that should receive an explicit input
   upgrade:
   - besides being saved locally, they may be passed as `localImage` only
     after byte-signature and current-model capability checks succeed
4. Non-image attachments are treated uniformly as local file paths:
   - FOCUS does not guarantee that the model will understand them
     directly
   - it only guarantees that the file is staged locally and that the path is
     made explicit to Codex
5. This repository does **not** automatically perform:
   - OCR
   - text extraction
   - format conversion
   - archive extraction
   - attachment execution
6. Attachment/message types that Feishu itself cannot support reliably must be
   rejected explicitly rather than degraded ambiguously.

## 3. Why This Boundary Fits Better

This boundary keeps responsibility split across two clear layers:

- FOCUS
  - owns message ingress, permission / group-activation checks, download,
    staging, and turn
    binding
- Codex / MCP / local environment
  - own the actual interpretation, conversion, and analysis of the file

Benefits:

- clearer architecture: attachment ingress and file parsing stay separate
- lower maintenance: the repository does not need to build its own document
  parsing ecosystem
- clearer behavior: the supported feature is "bring the file into the local
  workspace", not "guarantee first-class understanding of every format"
- more fail-closed: unsupported cases reject clearly

## 4. Supported Surface

"Attachment support" in this design means only **Feishu message resources that
can actually be downloaded**.

### 4.1 Message Types Included in Phase 1

| Feishu message type | Downloaded | Passed into Codex as | Repository guarantee |
| --- | --- | --- | --- |
| `image` | yes | local file; conditional `localImage` | path is always explicit; native input requires verified image bytes and explicit model capability |
| `file` | yes | local file path | staged successfully and path made explicit to Codex |
| `audio` | yes | local file path | staged successfully; interpretation is delegated to Codex / MCP / local tools |
| `media` | yes | local file path | staged successfully; interpretation is delegated to Codex / MCP / local tools |

### 4.2 Explicitly Out of Phase 1

| Type | Why it stays out |
| --- | --- |
| `folder` | Feishu API exposes key/name only and cannot download the folder payload |
| `sticker` | Feishu message-resource API does not support sticker download |
| attachment resources inside `merge_forward` | Feishu message-resource API does not support merged-forward child resources |
| card-embedded resources in `interactive` | Feishu message-resource API does not support card resources |

### 4.3 Additional Clarifications

- `text`, `post`, and text extracted from `interactive` remain part of the
  existing text-message path rather than this attachment-ingress path.
- `file` may include PDF, DOCX, XLSX, PPTX, ZIP, source code, CSV, and similar
  payloads.
- For those formats, the repository guarantee is only "stage locally and make
  the path explicit to Codex". It does **not** guarantee successful parsing.

## 5. Lifecycle Contract

### 5.1 An Attachment Message Does Not Start a Turn by Itself

When a user sends an attachment message, the repository should:

- perform normal permission / group-trigger checks
- download the resource into the current working directory's attachment
  subdirectory
- record a pending-attachment state
- reply with a confirmation such as "attachment saved; continue with a text
  instruction"

The attachment message itself should not start a Codex turn.

Reasons:

- most attachments still require user intent
- this avoids "send one file and execution starts unexpectedly"
- it makes "multiple attachments followed by one instruction" the natural path

### 5.2 Consumption Rule

A later text message from the **same sender, same chat, and same thread
semantics** consumes the current pending attachment set.

More precisely:

- `p2p`: key by `sender + chat`
- group main flow: key by `sender + chat`
- group thread: key by `sender + chat + thread_id`

All still-pending attachments under the same key should be consumed together in
receive order.

If any attachment in that pending set is missing locally or no longer belongs to
the current working directory, the whole set must fail closed rather than
partially continuing with only the surviving subset.

### 5.3 Group Isolation Rule

In group chats:

- attachments sent by A must never be consumed by B's text
- pending attachments cannot be shared at chat scope alone
- group attachment ingress must still obey the existing group mode, group
  activation, and
  trigger rules

### 5.4 Relationship Between Group History Recovery and Attachment Ingress

Group history recovery and attachment ingress both operate under the same outer
group rules such as mode, activation, and thread scope, but they are not the same
semantic path.

The formal boundary is:

- group history recovery restores **text discussion context only**
- whether an attachment has been downloaded locally, remains available, or has
  already been consumed by later text is owned only by pending-attachment state
- history recovery does not restore attachment availability
- history recovery does not rebuild attachment-consumption state

Therefore:

- even if file names or file-like placeholder text appear in group context, they
  must not be interpreted as proof that the corresponding attachment is still
  available
- if a later rollout enables attachment ingress, neither assistant-mode group
  logs nor history recovery should carry attachment lifecycle state
- attachment lifecycle must remain a separate state layer rather than being
  implicitly encoded as part of text context

### 5.5 Expiry Rule

Pending attachments must have a TTL.

Once expired:

- they are no longer auto-bound to later text
- local state should become eligible for cleanup
- a later user reference should get a clear "attachment expired; please resend"
  response

## 6. Local Staging Contract

### 6.1 Save Location

All downloadable attachments are staged under a fixed subdirectory beneath the
current working directory:

- `_feishu_attachments/`

The Focus service now always launches and owns its app-server on the same host,
so Focus staging paths and the backend share one local filesystem. This is a
fixed product boundary rather than a deployment-mode boolean interpreted at
runtime. `focusctl` / `fcodex` attached clients only connect to that running
local backend and do not change attachment delivery. If an external backend is
ever reintroduced, it first needs a separately designed reliable file-transfer
transaction; a URL or mode string must not be restored as shared-filesystem
proof.

They should not be scattered directly into the workspace root.

Reasons:

- reduce pollution of the repository root
- make it obvious which files came from Feishu ingress
- give cleanup and debugging one stable path

If the host workspace is itself a git repository, that repository should also
ignore this directory in its own `.gitignore`.

### 6.2 File Naming

Staged file names must not rely on the user-supplied original filename alone.

Implementation should at minimum ensure:

- original extension is preserved
- path-unsafe filename components are sanitized
- the staged path includes a collision-resistant discriminator such as
  timestamp, `message_id`, or hash

This prevents:

- overwriting existing user files
- collisions between same-name uploads from different messages
- malicious filenames breaking directory structure

### 6.3 Relationship to the Permission Model

This design carries an intentional product meaning:

- once an attachment is staged beneath the current working directory, it is
  treated as **part of the current workspace**

Therefore:

- under `read-only`, Codex may inspect but not directly modify those files
- under `workspace-write`, they behave like normal editable workspace files
- under `danger-full-access`, they follow the broader host permission boundary

This is not accidental. It is the tradeoff accepted to keep the UX simple.

Corollary:

- if the working directory changes before pending attachments are consumed, that
  pending set must be invalidated instead of being rebound into the new
  workspace
- this invalidation only removes automatic-consumption eligibility; it does not
  imply deletion of already-staged local files from the original workspace
- staged files remain in that original workspace for manual inspection,
  deliberate reuse, or explicit path-based prompts

## 7. Codex Input Contract

### 7.1 Images

When a turn starts, image attachments always keep their controlled local path.
They are additionally elevated to `localImage` only when all of these facts are
proved:

1. the staged file is still a regular, non-symlink file under the expected
   `_feishu_attachments/` directory
2. its current bytes match one of the shared native-image signatures; Feishu
   message type, filename extension, and Content-Type are not sufficient
3. the current thread effective model comes from a successful `thread/start`
   or `thread/resume` response, from `thread/settings/updated`, or from a
   `model/rerouted` event whose `turnId` matches the current authoritative
   active turn; the queued ACK from `thread/settings/update` is not evidence
4. a fresh `model/list` entry for that exact model explicitly includes
   `image` in `inputModalities`

Missing model facts, missing model metadata, `inputModalities=null`, an
explicit text-only modality list, catalog-read failure, or signature mismatch
all keep the local-path text and omit native input. If the pending
`turn/start` requests a model different from the known effective model, Focus
clears the old fact and treats capability as unknown until upstream confirms
the replacement. Disconnect/reconnect and thread unload/delete also clear the
ephemeral fact.

That preserves the local path while still using Codex's native image-input
support only within its proved boundary.

Focus repeats the regular-file, no-symlink, expected-parent, inode, and byte
checks immediately before constructing `localImage`. The current app-server
contract accepts a path rather than an already-verified file descriptor, so a
local actor able to mutate the staging directory can still replace that path
between Focus's final check and the backend opening it. Fully closing that
residual TOCTOU requires an immutable/content-addressed handoff or an upstream
descriptor/upload contract; the staging directory is therefore a trusted
same-host boundary, not a hostile multi-writer upload directory.

### 7.2 Non-Image Attachments

Non-image attachments do not receive a modality upgrade.

When a turn starts:

- inject explicit local absolute paths, display names, and type notes into the
  text prompt
- let Codex decide whether to read the file directly, invoke local tools, or
  call MCP / Apps

The key point is:

- FOCUS does not decide how a PDF / DOCX / MP4 should be interpreted
- FOCUS only brings the file into the local workspace reliably and
  makes the path explicit to Codex

## 8. Explicit Non-Goals

Phase 1 should not default to any of the following:

- automatic OCR
- automatic PDF / Office text extraction
- automatic audio transcription
- automatic video frame extraction
- automatic archive extraction
- automatic execution of script or binary attachments
- automatic "best parser" inference from extension

If administrators or users need those capabilities, they should provide them
through:

- MCP / Apps
- locally installed tools
- explicit instructions telling Codex which environment command to use

## 9. Error and Rejection Semantics

Unsupported cases and failures must prefer **clear reasons** over generic
"attachment processing failed" wording.

At minimum, the implementation should distinguish:

- unsupported types:
  - folders
  - stickers
  - merged-forward child attachments
  - card resources
- Feishu resource download limits:
  - over 100 MB
  - resource not visible to the current bot
  - external-group / secret / DLP restrictions
- local staging failures:
  - target directory not writable
  - insufficient disk space
  - filename conflict that could not be resolved safely
- delivery/capability boundaries:
  - image bytes which do not match a native image signature (path-only delivery)
  - unknown or text-only effective-model capability (path-only delivery)

## 10. Administrator and User Responsibility

### 10.1 Administrator Responsibility

- choose the working directory consciously, because attachments enter the active
  workspace
- configure MCP / Apps / local tooling when stronger parsing is required
- choose permission presets that match whether attachments may be edited in
  place
- configure or accept attachment TTL, cleanup policy, and storage limits

### 10.2 User Responsibility

- send a follow-up text instruction after sending attachments
- use MCP or local tooling for complex formats when stronger parsing is needed
- not interpret "file staged locally" as "the model is guaranteed to understand
  this format directly"

## 11. Completed First-Stage Implementation Checklist

The current first-stage implementation covers:

1. expands the former file-only ingress into an attachment ingress for:
   - `image`
   - `file`
   - `audio`
   - `media`
2. uses one unified message-resource downloader that handles:
   - `type=image`
   - `type=file`
3. stages attachments under `cwd/_feishu_attachments/`
4. keeps pending-attachment state keyed by `sender + chat + thread`
5. keeps attachment messages as download-and-confirm only, without starting a turn
6. lets a later text message consume the pending attachment set
7. widens adapter / turn input so signature-verified images enter as
   `localImage` only for an upstream-proven image-capable effective model
8. injects non-image paths into turn text with explicit local-path wording
9. returns clear failures for unsupported types and Feishu download errors
10. includes TTL and cleanup handling
11. includes regression coverage for at least:
    - p2p image
    - p2p generic file
    - group image / file
    - cross-user isolation in group chats
    - unsupported-type rejection
    - TTL expiry
    - signature mismatch, model-capability unknown/text-only/image, and
      requested/effective model mismatch

## 12. Implementation Entry Points

The current implementation mainly lives in:

- `bot/file_message_domain.py`
- `bot/feishu_bot.py`
- `bot/adapters/base.py`
- `bot/adapters/codex_app_server.py`
- `bot/prompt_turn_entry_controller.py`
- `bot/stores/pending_attachment_store.py`

The repository now implements the first-stage attachment ingress described here:

- supports `image`, `file`, `audio`, and `media`
- stages attachments under `cwd/_feishu_attachments/`
- consumes pending attachments on a later text message with the same
  `sender + chat + thread` semantics
- always injects controlled local-path text and conditionally upgrades a
  signature-verified image to `localImage` only when the authoritative
  effective model explicitly supports image input
- includes TTL cleanup and regression coverage
