# Upstream Provenance

The Focus Web frontend is derived from `apps/kimi-web` in
<https://github.com/MoonshotAI/kimi-code>.

- Imported commit: `c497af60e6cd20aab05e590f98a28fb15dd3491d`
- Imported on: 2026-07-27
- Upstream package version: `0.1.2`
- License: MIT

The imported `LICENSE` is retained verbatim as
`licenses/kimi-web-MIT.txt`. Its SHA-256 is
`23cc68e17992e0b512ae2e80afc5787d7d8e0fbfbdb4fff54ec0245508fa400e`, matching
the `LICENSE` blob at the imported commit. This is evidence for the copied
kimi-web source only; it does not replace notices for browser dependencies
that the Focus bundle distributes.

`provenance/kimi-web-files.json` is the canonical, machine-readable inventory
of every copied Kimi file in Focus Web's declared source scope. Each file is
relative to `apps/kimi-web` at the commit above: a `null` value is a byte-for-
byte copy; a SHA-256 value records the reviewed Focus modification of that
upstream-derived file. `focus_owned_files` names the source files that have no
Kimi counterpart, so a new source file cannot silently acquire ambiguous
provenance.

Focus initially retained the imported Vue design system, responsive shell, rich
Markdown renderer, diff, and diagnostic components. The kap-server transport,
Kimi session semantics, product branding, provider management, and equal-writer
queue model were replaced by Focus-owned APIs and projections.

This is a source-provenance record, not an ongoing product-alignment contract.
Focus-owned product, visual, and feature evolution may freely diverge from Kimi.
There is no requirement to track new Kimi commits, keep changes narrow for a
future merge, or absorb later Kimi features. Code should be organized according
to Focus ownership and maintenance needs.

## Per-File Provenance Verification and Manifest Update Procedure

The provenance check deliberately needs a local Kimi Git checkout containing
the imported commit. It reads Git objects at the recorded commit rather than
the checkout's working tree, then verifies every declared file, the Kimi
package version, and the retained MIT license evidence:

```bash
npm run check:kimi-provenance -- --upstream /path/to/kimi-code
```

After intentionally editing an upstream-derived file, run the explicit manifest
update command with the same checkout. It updates only the digest for already-listed
Kimi-derived files. It never adds a source path, reclassifies a Focus-owned
file, or changes the imported commit; make those changes deliberately in the
manifest and this document, then review the resulting diff:

```bash
npm run sync:kimi-provenance -- --upstream /path/to/kimi-code
npm run check:kimi-provenance -- --upstream /path/to/kimi-code
```

Despite the script name, `sync:kimi-provenance` synchronizes local-modification
digests with the manifest; it does not synchronize Focus with a newer Kimi
commit. A new import would require a separate explicit decision and is not a
routine development or release requirement.

This guard is intentionally not part of `npm run build`: build and release
remain reproducible from checked-in code and license evidence, without a
developer-specific `~/llm/kimi-code` path. The notice generator reads the
same provenance record, so source attribution and shipped notices cannot
silently point to different Kimi imports.

## Release Notice Procedure

`npm run build` obtains rendered package roots from the final Rollup chunks,
uses the exact resolutions in `package-lock.json`, explicitly records generated
font and icon assets, and writes the complete notices plus a machine-readable
inventory to `bot/web_assets/dist`. It also synchronizes
`web/THIRD_PARTY_NOTICES.md` and `bot/web_assets/THIRD_PARTY_NOTICES.md`, so
the latter remains present in Python wheels.

Run `npm run check:notices` after a build. It rejects a stale lockfile digest,
missing Kimi/MIT, OFL, or Apache evidence, or a package whose license expression
has not been reviewed by the generator. The generator is intentionally offline:
do not depend on a developer's local `~/llm/kimi-code` checkout at build or
release time.
