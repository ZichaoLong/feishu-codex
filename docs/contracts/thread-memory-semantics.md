# Thread Memory Semantics (Retired)

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/thread-memory-semantics.zh-CN.md`.

This file is retained only as a retirement note under its historical name.

## Current status

The project has removed its thread-wise memory control surface.

The following entry points are no longer part of the formal contract:

- `/memory`
- `focusctl thread memory`
- `new_thread_memory_mode_seed`
- any project-owned thread-memory persistence and restore path

## What to use instead

If an operator wants to change upstream memory/provider-related behavior, use:

- upstream Codex config
- upstream profile-v2 files
- upstream launch parameters outside this project's command surface

If an operator wants to change future-turn behavior for one Feishu binding, use:

- `/model`
- `/effort`
- `/approval`
- `/permissions`

## Current normative docs

- setting layers: `docs/contracts/runtime-settings-fact-sources.md`
- Feishu control surface: `docs/contracts/runtime-control-surface.md`
- next-load retirement note: `docs/contracts/thread-next-load-settings-semantics.md`
