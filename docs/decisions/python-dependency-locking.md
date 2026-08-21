# Python Dependency Locking and Installation Reproducibility

Document role: synchronized English peer. Canonical Chinese: `docs/decisions/python-dependency-locking.zh-CN.md`.

## 1. Problem

Focus has three Python dependency layers: project runtime dependencies, build tooling used during installation, and
development/CI-only tools. Previously, `requirements-dev.txt` copied the runtime dependencies while the installer
separately hard-coded `setuptools<81` and `wheel`. That shape had two failure modes:

- one direct dependency could drift independently across `pyproject.toml`, development requirements, and the installer;
- only direct ranges were recorded, so CI and a real installation could resolve different transitive versions at
  different times.

This decision does not change the rule that installation and repair must use `bash install.sh` / `./install.ps1`. It
does not permit developers to run `pip install .` or `pip install -e .` in the current Python or Conda environment.

## 2. Fact Sources and Generated Projections

Each dependency layer has one direct declaration authority. Lock files are committed, reproducible resolution
projections rather than a second place to declare intent:

| Layer | Authority | Responsibility |
| --- | --- | --- |
| Project runtime | `project.dependencies` in `pyproject.toml` | Defines supported direct runtime ranges; no input file may copy this list |
| Build tooling | `requirements-build.in` | Defines the `setuptools` / `wheel` ranges needed to build Focus in the managed virtual environment |
| Development and CI tooling | `requirements-dev.in` | Defines only pytest, Ruff, and platform supplements; it does not repeat runtime dependencies |
| Installation resolution | `requirements.lock` | Python 3.11+ universal version projection generated from runtime and build inputs |
| Development resolution | `requirements-dev.lock` | Python 3.11+ universal version projection generated from runtime, build, and development inputs |

`requirements-dev.txt` is therefore removed. Change runtime dependencies only in `pyproject.toml`; change build or
development tooling only in the corresponding `.in` file.

## 3. Generation and Upgrade Contract

Locks have one repository-owned generation entry point:

```bash
bash scripts/lock-python-dependencies.sh
```

The generator pins `uv 0.8.14`, a Python 3.11 resolution lower bound, and `--universal`. Default mode treats the
pins in the existing output files as resolution preferences. It replays the current resolution, incorporates changes
required by direct constraints, and lets CI verify that generated output matches the commit; it does not proactively
upgrade every package to the latest version.

A full dependency upgrade must be explicit:

```bash
bash scripts/lock-python-dependencies.sh --upgrade
```

`--upgrade` ignores existing output pins. Both lock diffs must be reviewed together; passing tests alone is not a
substitute for reviewing the transitive changes. The script rejects other arguments and writes the stable repository
entry point into each generated header, avoiding a false diff caused only by switching between default and upgrade
mode.

`uv` is a generation tool, not a Focus runtime dependency. If the exact version is unavailable locally, the script
fails before writing a lock. CI installs that same exact version and runs default mode.

## 4. Installer and CI Boundary

`install.py` no longer builds or installs source from a checkout. It first resolves a
remote channel or local artifact under the
[install-artifact delivery contract](../contracts/install-artifact-delivery.md),
then completely validates the bundle, Focus wheel, and `requirements.lock` before
changing a service or managed `.venv`. Inside the managed transaction it:

1. creates a pip-bearing CPython 3.11+ `.venv` when needed;
2. uses the bundled lock as a constraint and `--force-reinstall`s the same validated
   wheel; pip reads Focus runtime dependencies from wheel metadata and resolves them
   under that lock;
3. invokes the installed management entry in isolated mode to refresh wrappers and
   service definitions, preventing checkout sources from shadowing the installed package.

The Focus wheel is produced only during bundle construction. The artifact builder
uses one temporary build and egg-info root, then requires the wheel's complete `bot/`
paths and bytes to equal the setuptools source manifest. Ignored checkout `build/`
and `*.egg-info/` trees do not participate. The clean-build child disables setuptools
user configuration and places every path-bearing build, bdist, and egg-info staging
directory under that temporary root without deleting checkout-local caches. It also
uses the ZIP timestamp floor (`SOURCE_DATE_EPOCH=315532800`) as a Focus-owned
normalization value rather than inheriting the caller's clock or reproducibility
policy. This changes archive metadata only; source-manifest paths and bytes remain
the payload authority. The installer consumes only the already-validated wheel in
the bundle and retains no source-install path.

The bootstrap interpreter for the public installer must be CPython 3.11+ with `venv` / `ensurepip`. The caller's
system Python or Conda environment does not need preinstalled pip packages, Focus runtime dependencies, `setuptools`,
or `wheel`; the installer bootstraps pip and installs them into the fixed `.venv` under the machine-level Focus data
root. Before reusing that environment, the installer probes its interpreter and recreates an older or non-CPython
venv with the selected CPython 3.11+; it does not defer that incompatibility to pip and misreport it as an index
failure. The Unix entry point probes candidate implementation and version, and accepts an explicit
`FOCUS_INSTALL_PYTHON=/path/to/python`. If the ensurepip subprocess fails while creating the venv, the installer points
to the common Debian/Ubuntu `python3-venv` or versioned venv package. Permission, disk-space, and other filesystem errors
retain their original exception instead of being relabeled as a missing ensurepip component.

`bot/public_command_contract.py` is the command-name/module catalog for the four stable public surfaces (`focus`,
`focusd`, `focusctl`, and `fcodex`). Bootstrap generates its wrappers from that catalog, and a contract test requires
the `pyproject.toml` console-script projection to match it exactly.

This can remain an online installation path. A remote channel first reaches GitHub;
even `--artifact`, which does not contact GitHub, can require pip to reach its default
or explicitly configured package index. The installer does not silently add another
index after failure: index selection is package-supply-chain authority, and crossing
to another source can violate a private-repository boundary or select a different
artifact for the same version. The operator repairs the configured source, network,
certificate, or local cache and reruns the installer. The bundle hashes the Focus
wheel, dependency lock, and complete ZIP, but contains no third-party wheelhouse or
third-party artifact hashes, so fully offline installation is not promised. The
[install-artifact delivery contract](../contracts/install-artifact-delivery.md) owns
the fuller source, proxy, and verification boundary.

After the Python package install, bootstrap also refreshes operating-system service definitions. Linux runs
`systemctl --user daemon-reload` and therefore requires a working user manager/bus; macOS writes a launchd definition;
Windows registers a fixed-name Task Scheduler task. These are installation-environment capabilities, not Python package
dependencies.

Install, uninstall, and purge share one machine-level mutex. A first install has no running instance. Later repair or
upgrade checks every known instance before mutation. Any active/submission turn, pending approval/input, non-idle
loaded thread, or unverifiable runtime state rejects the operation before changing `.venv`, wrappers, or service
definitions, with no force option. A running-idle instance first closes new ingress and stops; success restores only
that previously running set. Stopped instances and autostart settings remain unchanged.

A standalone `service/status` followed by stop cannot satisfy this contract because a new prompt can enter between
the two operations. The running process therefore gives `ServiceRuntimeLifecycle` one process-local offline-maintenance
admission: it atomically closes external ingress, then reuses backend-reset preview for the final idle verification.
A failed verification or rejection by another instance reopens ingress; success only waits for the service manager to
stop the process. This flag is not durable install state and does not duplicate loaded-thread, pending-request, or
backend status. Process exit is its final cleanup boundary.

This is not a hot upgrade and creates no multi-generation virtual environment or rollback state machine. A failed
repair of the fixed `.venv` returns non-zero and leaves instances stopped. After repairing the index, network,
certificate, permissions, disk, or cache problem, the operator reruns the same installer. This boundary prevents a
running process from consuming an environment being rewritten while retaining one simple repeatable repair path.

`uninstall` and `purge` reuse the same transaction but do not restore services on success. The former removes services,
wrappers, completions, and the managed `.venv` while retaining configuration and other data proved by matching markers;
the latter removes both matching managed roots. An existing data root without a matching marker must first be repaired;
the remover does not guess directory ownership. Windows self-removal uses the same lock owner's handoff barrier: while
the parent still holds the primary lock, the helper takes the barrier and returns a matching armed proof before the
parent releases the primary lock. The parent reports only the helper PID and result path; the helper's per-target result
is the final deletion proof.

Focus Web production assets are ignored generated output rather than tracked Git
content. They must be generated before bundle construction and, after payload
verification, are delivered only as Focus-wheel package data. End users do not need
Node.js to install or run the Web UI. Node.js belongs only to the Web development/rebuild
toolchain or to the upstream npm Codex distribution when the user chooses it. Users
may instead install the upstream standalone native Codex binary, which does not
require Node.js. Focus consumes an existing Codex CLI and does not install it.

A constraint does not install a package by itself, so build environments must still
install build tooling explicitly from a lock. At installation time, wheel metadata
projects `pyproject.toml` runtime dependencies instead of copying them into a second
requirements input. This keeps `pyproject.toml` authoritative while making the
actual installation consume the committed version set.

The main Python CI job first regenerates both locks in default mode and requires an empty `git diff`, then installs the
test environment from `requirements-dev.lock`. The macOS and Windows contract jobs consume the same universal dev lock
without regenerating it. When CI proves an installable artifact, it separately
generates production Web assets and builds and validates the bundle. An ordinary
verification build is not publication.

## 5. Reliability Boundary

The current locks fix package names, versions, environment markers, and the reviewable dependency graph. They are not
a complete immutable software-supply-chain guarantee:

- locks do not contain third-party artifact hashes, and the same version can select different wheels on different platforms;
- a universal resolution expresses marker and version solutions from Python 3.11 onward, but does not prove that every
  artifact for every platform will remain available;
- package-index, TLS, certificate, and artifact-availability trust remain the responsibility of pip / uv and the
  configured index;
- generator and CI checks establish consistency among `pyproject.toml`, `.in`, and lock files; a hand-written package
  name assertion alone cannot prove that consistency.

If artifact-level reproducibility or offline installation becomes a requirement, hashes, a wheelhouse, signatures,
and a platform matrix need a separate design. The current version locks must not be described as already providing
those properties.

## 6. Maintenance Workflow

Dependency changes follow one path:

1. edit only the direct declaration authority for the relevant layer;
2. use the default generation command for ordinary constraint changes and `--upgrade` only for a planned full upgrade;
3. review additions, removals, versions, markers, and source comments in both locks;
4. run installer contract tests, the full Python suite, and documentation checks;
5. do not hand-edit one transitive pin or reintroduce a duplicate runtime requirements list.
