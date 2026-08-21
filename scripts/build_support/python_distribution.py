"""Build Focus Python artifacts without trusting checkout-local staging trees."""

from __future__ import annotations

import contextlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass


_EXTRA_CONFIG_ENV = "DIST_EXTRA_CONFIG"
_REPRODUCIBLE_WHEEL_EPOCH = "315532800"  # 1980-01-01, the ZIP timestamp floor.


class PythonDistributionBuildError(RuntimeError):
    """Raised when a Focus wheel cannot be proven to match its source payload."""


@dataclass(frozen=True, slots=True)
class IsolatedSetuptoolsBuild:
    root: pathlib.Path
    build_base: pathlib.Path
    egg_base: pathlib.Path
    environment: dict[str, str]

    @property
    def source_manifest_candidates(self) -> tuple[pathlib.Path, ...]:
        return tuple(sorted(self.egg_base.glob("*.egg-info/SOURCES.txt")))


def _distutils_config_value(path: pathlib.Path) -> str:
    # ConfigParser interpolation treats a literal percent as an escape marker.
    # Temporary paths can contain one on Windows through the user profile.
    return str(path).replace("%", "%%")


@contextlib.contextmanager
def isolated_setuptools_build(
    *,
    base_environment: Mapping[str, str] | None = None,
) -> Iterator[IsolatedSetuptoolsBuild]:
    """Give locked setuptools fresh build and egg-info roots for one child build."""

    with tempfile.TemporaryDirectory(
        prefix="focus-python-build-",
        ignore_cleanup_errors=True,
    ) as raw_root:
        root = pathlib.Path(raw_root).resolve()
        build_base = root / "build"
        build_lib = build_base / "lib"
        build_temp = build_base / "temp"
        build_scripts = build_base / "scripts"
        bdist_base = root / "bdist"
        wheel_bdist = root / "wheel-bdist"
        egg_base = root / "egg"
        egg_base.mkdir()
        config_path = root / "distutils.cfg"
        config_path.write_text(
            "[build]\n"
            f"build_base = {_distutils_config_value(build_base)}\n"
            f"build_purelib = {_distutils_config_value(build_lib)}\n"
            f"build_platlib = {_distutils_config_value(build_lib)}\n"
            f"build_lib = {_distutils_config_value(build_lib)}\n"
            f"build_temp = {_distutils_config_value(build_temp)}\n"
            f"build_scripts = {_distutils_config_value(build_scripts)}\n"
            "\n"
            "[bdist]\n"
            f"bdist_base = {_distutils_config_value(bdist_base)}\n"
            "\n"
            "[bdist_wheel]\n"
            f"bdist_dir = {_distutils_config_value(wheel_bdist)}\n"
            "skip_build = false\n"
            "\n"
            "[egg_info]\n"
            f"egg_base = {_distutils_config_value(egg_base)}\n",
            encoding="utf-8",
        )
        environment = dict(os.environ if base_environment is None else base_environment)
        environment[_EXTRA_CONFIG_ENV] = str(config_path)
        # Wheel uses SOURCE_DATE_EPOCH for every ZIP entry.  Use one Focus-owned
        # normalization value instead of inheriting the caller's clock or policy.
        environment["SOURCE_DATE_EPOCH"] = _REPRODUCIBLE_WHEEL_EPOCH
        yield IsolatedSetuptoolsBuild(
            root=root,
            build_base=build_base,
            egg_base=egg_base,
            environment=environment,
        )


def _manifest_bot_payload(
    *,
    source_dir: pathlib.Path,
    source_manifest: pathlib.Path,
) -> dict[str, bytes]:
    try:
        manifest_lines = source_manifest.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PythonDistributionBuildError(
            f"无法读取setuptools source manifest：{source_manifest}"
        ) from exc

    payload: dict[str, bytes] = {}
    for raw_path in manifest_lines:
        if not raw_path.startswith("bot/"):
            continue
        relative = pathlib.PurePosixPath(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise PythonDistributionBuildError(
                f"setuptools source manifest包含越界bot路径：{raw_path}"
            )
        normalized = relative.as_posix()
        if normalized in payload:
            raise PythonDistributionBuildError(
                f"setuptools source manifest包含重复bot路径：{normalized}"
            )
        source_path = source_dir.joinpath(*relative.parts)
        if not source_path.is_file() or source_path.is_symlink():
            raise PythonDistributionBuildError(
                f"setuptools source manifest中的bot文件不存在或不是普通文件：{normalized}"
            )
        payload[normalized] = source_path.read_bytes()
    if not payload:
        raise PythonDistributionBuildError("setuptools source manifest没有bot payload")
    return payload


def validate_wheel_bot_payload(
    wheel_path: pathlib.Path,
    *,
    source_dir: pathlib.Path,
    source_manifest: pathlib.Path,
) -> None:
    """Require the wheel's complete ``bot/`` payload to equal current sources."""

    expected = _manifest_bot_payload(
        source_dir=source_dir.resolve(),
        source_manifest=source_manifest,
    )
    try:
        with zipfile.ZipFile(wheel_path) as wheel:
            file_infos = [info for info in wheel.infolist() if not info.is_dir()]
            for info in file_infos:
                relative = pathlib.PurePosixPath(info.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise PythonDistributionBuildError(
                        f"wheel包含越界payload路径：{info.filename}"
                    )
                if not info.filename.startswith("bot/") and not relative.parts[0].endswith(
                    ".dist-info"
                ):
                    raise PythonDistributionBuildError(
                        f"wheel包含未声明的顶层payload：{info.filename}"
                    )
            bot_infos = [
                info
                for info in file_infos
                if info.filename.startswith("bot/")
            ]
            names = [info.filename for info in bot_infos]
            duplicate_names = sorted(
                name for name, count in Counter(names).items() if count > 1
            )
            if duplicate_names:
                raise PythonDistributionBuildError(
                    "wheel包含重复bot entries：" + ", ".join(duplicate_names)
                )
            actual = {info.filename: wheel.read(info) for info in bot_infos}
    except (OSError, zipfile.BadZipFile) as exc:
        raise PythonDistributionBuildError(f"无法读取Python wheel：{wheel_path}") from exc

    wheel_only = sorted(actual.keys() - expected.keys())
    source_only = sorted(expected.keys() - actual.keys())
    changed = sorted(name for name in actual.keys() & expected.keys() if actual[name] != expected[name])
    if wheel_only or source_only or changed:
        details: list[str] = []
        if wheel_only:
            details.append("wheel-only=" + ",".join(wheel_only))
        if source_only:
            details.append("source-only=" + ",".join(source_only))
        if changed:
            details.append("content-mismatch=" + ",".join(changed))
        raise PythonDistributionBuildError("wheel bot payload与当前source manifest不一致：" + "; ".join(details))


def build_validated_wheel(
    *,
    source_dir: pathlib.Path,
    output_dir: pathlib.Path,
    python_executable: pathlib.Path | str = sys.executable,
) -> pathlib.Path:
    """Build one clean Focus wheel and copy it out only after payload validation."""

    source_dir = source_dir.resolve()
    if not (source_dir / "pyproject.toml").is_file():
        raise PythonDistributionBuildError(f"Python source缺少pyproject.toml：{source_dir}")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    with isolated_setuptools_build() as isolated:
        wheel_stage = isolated.root / "wheel"
        wheel_stage.mkdir()
        command = [
            str(python_executable),
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--config-settings=--global-option=--no-user-cfg",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_stage),
            str(source_dir),
        ]
        try:
            subprocess.run(command, check=True, env=isolated.environment)
        except subprocess.CalledProcessError as exc:
            raise PythonDistributionBuildError("clean Python wheel构建失败") from exc

        wheels = tuple(sorted(wheel_stage.glob("*.whl")))
        if len(wheels) != 1:
            raise PythonDistributionBuildError(
                f"clean Python wheel构建必须恰好产生一个wheel，实际为{len(wheels)}"
            )
        manifests = isolated.source_manifest_candidates
        if len(manifests) != 1:
            raise PythonDistributionBuildError(
                f"clean Python wheel构建必须恰好产生一个source manifest，实际为{len(manifests)}"
            )
        validate_wheel_bot_payload(
            wheels[0],
            source_dir=source_dir,
            source_manifest=manifests[0],
        )

        target = output_dir / wheels[0].name
        created_target = False
        try:
            with wheels[0].open("rb") as source_handle, target.open("xb") as target_handle:
                created_target = True
                shutil.copyfileobj(source_handle, target_handle)
            shutil.copystat(wheels[0], target)
        except FileExistsError as exc:
            raise PythonDistributionBuildError(f"输出wheel已存在，拒绝覆盖：{target}") from exc
        except BaseException:
            if created_target:
                target.unlink(missing_ok=True)
            raise
        return target
