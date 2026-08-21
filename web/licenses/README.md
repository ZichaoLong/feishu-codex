# License Evidence Used by the Notice Generator

This directory contains only evidence that cannot reliably be obtained from a
locked npm package's own top-level `LICENSE`/`NOTICE` files.

- `kimi-web-MIT.txt` is the verbatim `LICENSE` from
  `MoonshotAI/kimi-code` commit
  `c497af60e6cd20aab05e590f98a28fb15dd3491d`. Its SHA-256 is checked by
  `scripts/third-party-notices.mjs` before a release notice is generated.
- `apache-2.0.txt` is the unmodified Apache License 2.0 text. It supplies the
  full text for the locked `@iconify-json/ri@1.2.10` icon data package, whose
  shipped `package.json` and `info.json` declare Apache-2.0 but do not include
  a license file. The generator records that package metadata as the versioned
  provenance; do not infer its terms from an arbitrary newer Remix Icon
  checkout.
- `tabler-icons-MIT.txt` preserves the MIT copyright notice for Tabler Icons.
  The locked `@iconify-json/tabler@1.2.37` metadata identifies Tabler Icons
  3.45.0 and Paweł Kuna but likewise does not include a license file.

Any new override needs a reviewable reason, a versioned source in the generator,
and a corresponding regression check. Do not add a generic fallback merely to
make a build pass.
