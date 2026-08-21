#!/usr/bin/env python3
"""Build the wheel-bearing Focus install bundle used locally and by Releases."""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.build_support.install_bundle import (  # noqa: E402
    ALL_CHANNELS,
    InstallBundleError,
    build_install_bundle,
)


def _source_revision(source_dir: pathlib.Path) -> str:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=source_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return revision + ("+dirty" if dirty else "")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a validated Focus install bundle. Run `npm run build` in web/ first; "
            "ordinary local builds never publish to GitHub."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        default=_REPO_ROOT / "build" / "install",
        help="Artifact output directory (default: build/install).",
    )
    parser.add_argument(
        "--channel",
        choices=sorted(ALL_CHANNELS),
        default="local",
        help="Bundle authority; local is for developer builds (default: local).",
    )
    parser.add_argument(
        "--release-tag",
        help=(
            "Existing Release tag required for stable; development derives one "
            "from build_id; forbidden for local."
        ),
    )
    parser.add_argument("--build-id", help="Optional publication/build identifier.")
    parser.add_argument("--source-revision", help="Defaults to current Git HEAD plus +dirty.")
    parser.add_argument("--source-dir", type=pathlib.Path, default=_REPO_ROOT, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        built = build_install_bundle(
            source_dir=args.source_dir,
            output_dir=args.output_dir,
            channel=args.channel,
            source_revision=args.source_revision or _source_revision(args.source_dir),
            build_id=args.build_id,
            release_tag=args.release_tag,
        )
    except InstallBundleError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"bundle={built.bundle_path}")
    if built.channel_manifest_path is not None:
        print(f"channel_manifest={built.channel_manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
