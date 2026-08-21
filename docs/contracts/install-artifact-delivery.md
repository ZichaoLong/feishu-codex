# Focus Install Artifact Delivery Contract

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/install-artifact-delivery.zh-CN.md`.

This document defines the boundary among Focus sources, installable bundles,
GitHub Release channels, local builds, and explicit publication. It preserves one
verifiable installation path after generated Web production assets leave Git
history. It does not promise to deliver Python, third-party wheels, or the Codex
CLI as one offline payload.

## 1. Terms and Owners

| Fact or action | Sole owner |
| --- | --- |
| Closed bundle/channel-manifest schemas, construction, and validation | `scripts/build_support/install_bundle.py` |
| Stable/development/local-artifact selection, download, and install-transaction boundary | `install.py` |
| Isolated argv shape for installed Python modules | `bot/managed_python.py` |
| Clean Focus-wheel build and source-payload verification | `scripts/build_support/python_distribution.py` |
| GitHub Release validation, upload ordering, and development retention | `scripts/build_support/github_publication.py` |
| Local bundle entry point | `scripts/build_install_bundle.py` |
| Sole repository artifact-upload entry point | `scripts/publish_install_bundle.py` |
| Manual publication gate | `.github/workflows/publish-installable.yml` |
| Python declarations and lock semantics | [Python dependency-locking decision](../decisions/python-dependency-locking.md) |

A “bundle” is a ZIP file, not a directory, and must not be unpacked before it is
passed to `--artifact`. A source checkout is build input rather than installation
payload. Generated `bot/web_assets/dist/` and `build/install/` trees are ignored
local artifacts.

## 2. Closed Bundle Schema

A bundle contains exactly three regular, unencrypted, top-level ZIP entries:

- `manifest.json`;
- one Focus wheel;
- `requirements.lock`.

Directories, absolute paths, parent traversal, backslash paths, symlinks,
duplicate entries, and additional files are rejected. `manifest.json` is a strict
UTF-8 JSON object that rejects duplicate keys, unknown fields, and missing fields.
The current schema is:

| Field | Contract |
| --- | --- |
| `schema` | Exactly `focus-install-bundle` |
| `schema_version` | Integer `1` |
| `channel` | `stable`, `development`, or `local` |
| `version` | Nonempty safe identifier matching the Focus wheel version |
| `build_id` | Nonempty safe identifier for this build |
| `source_revision` | Declared source revision; publication accepts a 40-character lowercase commit SHA and requires inner/outer equality |
| `files` | Exactly two records with distinct names |

Each file record allows only `name`, `role`, `size`, and `sha256`. `name` is a
top-level POSIX filename, `size` is a bounded positive integer, and `sha256` is a
lowercase SHA-256. The two roles each occur exactly once:

- `focus-wheel`: the filename ends in `.whl`; wheel metadata has the name `focus`
  and the manifest version, and the wheel contains the Focus Web production
  payload and notices;
- `python-dependency-lock`: the filename is exactly `requirements.lock`, containing
  a UTF-8 locked-requirements projection.

The validator checks archive shape, expansion limits, and every payload size and
SHA-256 before writing only the two declared payloads into an empty temporary
directory. Any mismatch rejects the whole bundle before the install transaction.

## 3. Outer Channel Manifest

Remote `stable` and `development` bundles also carry one outer channel manifest in
the same GitHub Release:

- stable: `focus-install-stable.json`;
- development: `focus-install-development.json`.

It is another strict UTF-8 JSON object that rejects duplicate keys, unknown fields,
and missing fields:

| Field | Contract |
| --- | --- |
| `schema` | Exactly `focus-install-channel` |
| `schema_version` | Integer `1` |
| `channel` | Exactly the requested remote channel |
| `release_tag` | Exactly the containing GitHub Release tag |
| `version`, `build_id`, `source_revision` | Exactly match the inner bundle manifest |
| `bundle` | Contains only `name`, `size`, and `sha256`, identifying one ZIP asset in the same Release |

The installer cross-checks GitHub asset metadata, downloaded byte counts, the
outer SHA-256, inner/outer manifest identity, and wheel identity. The outer digest
binds the channel pointer to exact bundle bytes under the same repository authority.
It is not an independent signature or trust root from GitHub and HTTPS, and this
contract does not claim otherwise.

## 4. Three Installation Authorities

### Stable

With no explicit source, the installer behaves as `--channel stable`: it reads the
repository's latest non-draft, non-prerelease GitHub Release and requires exactly
one stable channel manifest plus its referenced bundle. Removing an optional
leading `v`, the stable Release tag equals the wheel version. Stable bundle and
channel-manifest assets are immutable; publication rejects an existing name with
different bytes.

Stable publication uses an already-created formal Release. If the latest Release
has no bundle, the installer does not fall back to development, checkout sources,
or an older Release.

### Development

`--channel development` reads only the non-draft prerelease at the fixed
`development-builds` tag. That tag remains in `main` history and is never moved to
a feature branch merely to publish its build. The bundle's `source_revision` still
records the actual build commit.

Each development bundle has a unique, immutable filename.
`focus-install-development.json` is the replaceable pointer to the latest successful
build. Publication then retains the newest five development bundles on a best-effort
basis. Cleanup failure warns but does not revoke the committed latest pointer.

### Local Artifact

`--artifact PATH` uses the explicitly selected bundle ZIP without contacting
GitHub or requiring an outer channel manifest. It still validates the complete
inner schema, every payload byte, wheel identity, and Web payload. `local` is the
default developer-build shape. A separately downloaded stable/development ZIP can
also be installed through `--artifact`, but doing so does not reinterpret its
channel.

There is no implicit fallback among the three authorities. A caller repairs the
selected source or explicitly chooses another one.

## 5. Install Transaction and Network Boundary

Remote Release lookup, channel-manifest and bundle download, and complete validation
of either a local or remote bundle all finish before Focus acquires offline-maintenance
admission, stops a service, or mutates the managed `.venv`. A preflight failure
leaves the current installation and service state unchanged.

Only after validation does the installer enter the existing managed transaction:
it proves all instances idle, deletes and rebuilds the Focus-exclusive CPython
3.11+ `.venv` on every install, then force-reinstalls the validated wheel under
the bundled `requirements.lock` constraint. Both pip and the post-install
consistency check run under an isolated interpreter, and install subprocesses do
not inherit `PYTHON*` import settings. Packages from the system, Conda, user site,
current directory, or `PYTHONPATH` are outside the managed environment. The
installer preserves configured index, proxy, and certificate authority, but
rejects effective pip `target`, `prefix`, `root`, or `user` configuration before
dependency writes so packages cannot be redirected outside the managed `.venv`.
Wrappers, completion, and service definitions are refreshed only after isolated
`pip check` succeeds. All four public wrappers and completion launch an absolute
managed interpreter as `-I -m <module>`; service definitions persist that same
isolated module argv directly instead of traversing a user wrapper. Isolated mode
controls import authority only for the current Focus Python process and does not
delete ordinary environment variables, so PATH and existing Focus/provider/Codex
configuration remain available to downstream tools. This is not a hot upgrade,
multi-generation environment, or automatic rollback state machine.

Remote channels require GitHub access. Python networking honors standard
`HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY`. `--artifact` removes only the Focus
bundle's GitHub download; pip can still download third-party dependencies according
to its configured index, proxy, certificates, and cache. The bundle contains no
Python interpreter, third-party wheelhouse, or third-party artifact hashes. A user
may download the ZIP elsewhere and transfer it to the target machine, but Focus
does not promise a fully zero-network installation.

## 6. Build and Publication Are Separate Actions

A developer first generates production Web assets under `web/`, then runs
`python scripts/build_install_bundle.py`. The default produces a `local` bundle in
ignored `build/install/`; `bash install.sh --artifact <zip>` or
`./install.ps1 --artifact <zip>` installs it. The builder requires both the Web
payload and `requirements.lock` in the source and constructs and verifies one
deterministic Focus wheel containing them.

Ordinary commits, pull requests, CI verification, local Web builds, and local
bundle builds never publish artifacts. GitHub upload is explicit: manually dispatch
`publish-installable.yml`, or deliberately invoke the sole upload command with an
already-built and validated bundle plus its matching channel manifest.

Publication completes bundle, channel-manifest, source-revision, and target-Release
preflight first. It uploads the uniquely named bundle before the channel manifest.
A bundle alone is not channel authority; successful upload and read-back of the
channel manifest is the publication commit point. An ambiguous upload result is
reconciled from GitHub by size and SHA-256, and fails closed if identical bytes
cannot be proved.

The formal workflow writes the checked-out, gated `HEAD` to `source_revision`.
The standalone upload command can prove only a 40-character commit SHA and
inner/outer equality; it cannot independently prove a clean caller worktree or
that the commit is reachable from a particular remote ref.

Stable requires an existing formal Release and immutable assets. Development uses
the fixed prerelease and may replace only its channel manifest. Ordinary validation
must not become publication by reusing upload side effects or implicitly creating
tags.

## 7. Maintenance Closure

A change to schemas, channel authority, the install-transaction boundary, or upload
ordering must update this contract, the owner implementation, installer help, the
publication workflow, and focused tests in one transaction. A Python dependency-lock
semantic change also updates the
[Python dependency-locking decision](../decisions/python-dependency-locking.md)
without maintaining competing explanations in both documents.
