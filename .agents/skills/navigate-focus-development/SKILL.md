---
name: navigate-focus-development
description: Navigate the Focus repository through a derived catalog of reviewed capability refs plus live changed-path impact, ref locations, and direct Python imports. Use when Codex must diagnose, review, implement, or refactor Focus code without loading unrelated repository context, or when it must choose and close a scoped change and verification cone.
---

# Navigate Focus Development

Work from the Focus repository root.

Follow `docs/architecture/development-navigation.zh-CN.md` as the canonical
discipline. This skill is its thin command entry point.

## Locate the change cone

1. Read the applicable `AGENTS.md` files.
2. List reviewed capabilities:

   ```bash
   python scripts/focus_nav.py list
   ```

3. Show the closest capability:

   ```bash
   python scripts/focus_nav.py show <capability>
   ```

4. Read the returned refs first; expand only on the document's evidence triggers.
5. Inspect one-hop Python dependencies when the change crosses a module boundary:

   ```bash
   python scripts/focus_nav.py module bot.example
   ```
6. Assist navigation-impact review with `python scripts/focus_nav.py paths
   <path>... [--locations]`. If unavailable or `unmapped`, follow the canonical
   stale/no-match path; the derived catalog is never completion authority.

## Verify the capability

Run the reviewed focused tests, sentinels, and guards:

```bash
python scripts/focus_verify.py <capability>
```

Pass `--python <exact-path>` when the task requires a specific interpreter. Use
`--dry-run` to inspect the fixed argv/cwd plan before execution.

Complete stale-index handling, navigation-impact closure, and wider gates as
required by the canonical discipline.
