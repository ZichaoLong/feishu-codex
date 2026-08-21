---
name: feishu-scheduled-prompts
description: Use when the user wants a future one-shot or recurring task to continue the current Feishu-bound Codex thread later. This skill manages Linux systemd --user timers that eventually call focusctl prompt send back into the bound Feishu session.
---

# Feishu Scheduled Prompts

Use this skill when the user wants:

- "tomorrow morning continue this thread"
- "every trading day at 15:25 send a market recap into this Feishu chat"
- "list / remove / run now the scheduled tasks for this workspace"

This skill is intentionally narrow:

- it manages `systemd --user` timers on Linux
- it always routes execution back through `focusctl prompt send`
- it does not create a second Codex backend

Do not use it for:

- background loops or always-on daemons
- cross-machine scheduling
- vague "remind me somewhere later" requests without a concrete Feishu-bound thread target

Resolve local tools before running any command. Prefer the user's normal
`PATH`, but fall back to the repo-managed wrapper and venv because Codex turns
may not inherit the user's login shell:

```bash
FOCUS_DATA_ROOT="${FOCUS_DATA_ROOT:-$HOME/.local/share/focus}"
FOCUS_BIN_DIR="${FOCUS_BIN_DIR:-$HOME/.local/bin}"

CTL="$(command -v focusctl || true)"
if [ -z "$CTL" ] && [ -x "$FOCUS_BIN_DIR/focusctl" ]; then
  CTL="$FOCUS_BIN_DIR/focusctl"
fi
if [ -z "$CTL" ] && [ -x "$FOCUS_DATA_ROOT/.venv/bin/focusctl" ]; then
  CTL="$FOCUS_DATA_ROOT/.venv/bin/focusctl"
fi
if [ -z "$CTL" ]; then
  echo "focusctl not found; run bash install.sh or pass --ctl-path explicitly" >&2
  exit 1
fi

PY="$FOCUS_DATA_ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "managed Python not found: $PY; run bash install.sh first" >&2
  exit 1
fi
```

Run helper commands from this skill directory, or replace
`scripts/manage_scheduled_prompt.py` with its absolute path.

Workflow:

1. Confirm you are in a Codex turn with `CODEX_THREAD_ID` available.
2. Resolve the current thread's attached Feishu binding:
   - run `"$CTL" thread bindings --thread-id "$CODEX_THREAD_ID"`
   - if there is exactly one `[attached]` binding, use it
   - if there are zero attached bindings, stop and tell the user there is no attached Feishu chat to receive scheduled output
   - if there are multiple attached bindings, ask the user which one should receive the scheduled task
3. Resolve the owning instance for that thread:
   - prefer `"$CTL" thread status --thread-id "$CODEX_THREAD_ID"` and read `live runtime owner`
   - if the owner is `none`, ask the user which instance should own future scheduled execution
4. Convert the user's desired time into a concrete `systemd` `OnCalendar=` expression.
5. Write the scheduled prompt to a UTF-8 text file. For recurring tasks, include
   the task id, the exact completion condition, and the exact cleanup command
   when self-removal is part of the stop strategy.
6. Use the helper script in this skill:

   ```bash
   "$PY" scripts/manage_scheduled_prompt.py create \
     --task-id "<short-stable-id>" \
     --instance "<instance-name>" \
     --binding-id "<binding-id>" \
     --on-calendar "<systemd OnCalendar expression>" \
     --ctl-path "$CTL" \
     --prompt-file "<utf8-text-file>"
   ```

7. For inspection / cleanup, use:

   ```bash
   "$PY" scripts/manage_scheduled_prompt.py list
   "$PY" scripts/manage_scheduled_prompt.py show --task-id "<task-id>"
   "$PY" scripts/manage_scheduled_prompt.py remove --task-id "<task-id>"
   "$PY" scripts/manage_scheduled_prompt.py run-now --task-id "<task-id>"
   ```

Rules:

- Prefer `--prompt-file` over inline prompt text when the prompt is more than one short sentence.
- Keep task ids short, ASCII, and stable, for example `ashare-close-recap`.
- Default to `--synthetic-source schedule` and `--display-mode silent` unless the user explicitly wants a short announcement **after** the scheduled prompt has successfully started. `announce` never posts for a refused, failed, unknown, or merely queued submission.
- Treat helper failures as authoritative. Do not paper over `systemctl --user` or control-plane errors.
- Do not try to auto-pick a binding when multiple attached bindings exist.
- One-shot tasks should use an absolute `OnCalendar=` timestamp when practical,
  for example `2026-06-29 23:00:00`.
- Recurring tasks must have a stop strategy. Use self-removal, deterministic
  cleanup, or both.
- For recurring self-removal, put the concrete removal command in the scheduled
  prompt, for example:

  ```text
  If the job is complete, report the final result, then remove this scheduled task:
  <managed-python> <absolute-helper-path> remove --task-id <task-id>
  ```

- For recurring tasks with a known deadline, also create a one-shot cleanup
  scheduled prompt that removes the recurring task after that deadline.

When reporting back, include:

- the task id
- the target binding id
- the `OnCalendar` expression
- whether it was created, updated, removed, listed, or run immediately
