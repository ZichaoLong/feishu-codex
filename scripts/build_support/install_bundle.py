"""Build and validate the only installable Focus artifact shape."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import stat
import sys
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from typing import Any

from scripts.build_support.python_distribution import (
    PythonDistributionBuildError,
    build_validated_wheel,
)


BUNDLE_SCHEMA = "focus-install-bundle"
CHANNEL_SCHEMA = "focus-install-channel"
SCHEMA_VERSION = 1
BUNDLE_MANIFEST_NAME = "manifest.json"
DEPENDENCY_LOCK_NAME = "requirements.lock"
CHANNEL_MANIFEST_NAMES = {
    "stable": "focus-install-stable.json",
    "development": "focus-install-development.json",
    "local": "focus-install-local.json",
}
REMOTE_CHANNELS = frozenset({"stable", "development"})
ALL_CHANNELS = frozenset(CHANNEL_MANIFEST_NAMES)

_MAX_BUNDLE_BYTES = 256 * 1024 * 1024
_MAX_EXPANDED_BYTES = 512 * 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REQUIRED_WEB_FILES = (
    "bot/web_assets/dist/index.html",
    "bot/web_assets/dist/THIRD_PARTY_NOTICES.html",
    "bot/web_assets/dist/THIRD_PARTY_NOTICES.md",
    "bot/web_assets/dist/THIRD_PARTY_SBOM.json",
    "bot/web_assets/THIRD_PARTY_NOTICES.md",
)


class InstallBundleError(RuntimeError):
    """Raised when an install artifact cannot satisfy the Focus bundle contract."""


@dataclass(frozen=True, slots=True)
class ArtifactFile:
    name: str
    role: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BundleMetadata:
    channel: str
    version: str
    build_id: str
    source_revision: str
    files: tuple[ArtifactFile, ...]

    def file_for_role(self, role: str) -> ArtifactFile:
        matches = tuple(item for item in self.files if item.role == role)
        if len(matches) != 1:
            raise InstallBundleError(f"bundle manifest必须恰好包含一个{role}文件")
        return matches[0]


@dataclass(frozen=True, slots=True)
class ValidatedInstallBundle:
    root: pathlib.Path
    metadata: BundleMetadata
    wheel_path: pathlib.Path
    dependency_lock_path: pathlib.Path


@dataclass(frozen=True, slots=True)
class BuiltInstallBundle:
    bundle_path: pathlib.Path
    channel_manifest_path: pathlib.Path | None
    metadata: BundleMetadata


@dataclass(frozen=True, slots=True)
class ChannelBundle:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ChannelManifest:
    channel: str
    release_tag: str
    version: str
    build_id: str
    source_revision: str
    bundle: ChannelBundle


class _DuplicateJsonKey(ValueError):
    pass


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _load_json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise InstallBundleError(f"{label}超过{_MAX_MANIFEST_BYTES}字节")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJsonKey(key)
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallBundleError(f"{label}不是严格UTF-8 JSON object") from exc
    if not isinstance(payload, dict):
        raise InstallBundleError(f"{label}必须是JSON object")
    return payload


def _require_exact_keys(payload: dict[str, Any], expected: set[str], *, label: str) -> None:
    if set(payload) != expected:
        missing = sorted(expected - set(payload))
        unknown = sorted(set(payload) - expected)
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise InstallBundleError(f"{label}字段不符合闭合schema：" + "; ".join(details))


def _require_string(value: Any, *, label: str, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise InstallBundleError(f"{label}必须是非空字符串")
    if identifier and _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise InstallBundleError(f"{label}不是安全identifier")
    return value


def _require_positive_int(value: Any, *, label: str, maximum: int) -> int:
    if type(value) is not int or value <= 0 or value > maximum:
        raise InstallBundleError(f"{label}必须是1到{maximum}之间的整数")
    return value


def _require_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise InstallBundleError(f"{label}必须是小写SHA-256")
    return value


def _safe_basename(value: Any, *, label: str, suffix: str | None = None) -> str:
    name = _require_string(value, label=label)
    relative = pathlib.PurePosixPath(name)
    if (
        relative.is_absolute()
        or len(relative.parts) != 1
        or relative.name != name
        or "\\" in name
        or name in {".", ".."}
    ):
        raise InstallBundleError(f"{label}必须是不含目录的POSIX文件名")
    if suffix is not None and not name.endswith(suffix):
        raise InstallBundleError(f"{label}必须以{suffix}结尾")
    return name


def _artifact_file_payload(item: ArtifactFile) -> dict[str, Any]:
    return {
        "name": item.name,
        "role": item.role,
        "sha256": item.sha256,
        "size": item.size,
    }


def _bundle_manifest_payload(metadata: BundleMetadata) -> dict[str, Any]:
    return {
        "build_id": metadata.build_id,
        "channel": metadata.channel,
        "files": [_artifact_file_payload(item) for item in metadata.files],
        "schema": BUNDLE_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "source_revision": metadata.source_revision,
        "version": metadata.version,
    }


def parse_bundle_manifest(raw: bytes) -> BundleMetadata:
    payload = _load_json_object(raw, label="bundle manifest")
    _require_exact_keys(
        payload,
        {
            "build_id",
            "channel",
            "files",
            "schema",
            "schema_version",
            "source_revision",
            "version",
        },
        label="bundle manifest",
    )
    if payload["schema"] != BUNDLE_SCHEMA or payload["schema_version"] != SCHEMA_VERSION:
        raise InstallBundleError("bundle manifest schema或version不受支持")
    channel = _require_string(payload["channel"], label="bundle channel")
    if channel not in ALL_CHANNELS:
        raise InstallBundleError(f"bundle channel不受支持：{channel}")
    version = _require_string(payload["version"], label="bundle version", identifier=True)
    build_id = _require_string(payload["build_id"], label="bundle build_id", identifier=True)
    source_revision = _require_string(
        payload["source_revision"],
        label="bundle source_revision",
        identifier=True,
    )
    raw_files = payload["files"]
    if not isinstance(raw_files, list) or len(raw_files) != 2:
        raise InstallBundleError("bundle manifest files必须恰好包含两个条目")
    files: list[ArtifactFile] = []
    for index, raw_file in enumerate(raw_files):
        if not isinstance(raw_file, dict):
            raise InstallBundleError(f"bundle manifest files[{index}]必须是object")
        _require_exact_keys(
            raw_file,
            {"name", "role", "sha256", "size"},
            label=f"bundle manifest files[{index}]",
        )
        role = _require_string(raw_file["role"], label=f"bundle file role[{index}]")
        name = _safe_basename(raw_file["name"], label=f"bundle file name[{index}]")
        files.append(
            ArtifactFile(
                name=name,
                role=role,
                size=_require_positive_int(
                    raw_file["size"],
                    label=f"bundle file size[{index}]",
                    maximum=_MAX_EXPANDED_BYTES,
                ),
                sha256=_require_sha256(
                    raw_file["sha256"],
                    label=f"bundle file sha256[{index}]",
                ),
            )
        )
    if len({item.name for item in files}) != len(files):
        raise InstallBundleError("bundle manifest包含重复文件名")
    if {item.role for item in files} != {"focus-wheel", "python-dependency-lock"}:
        raise InstallBundleError("bundle manifest必须只声明focus wheel和Python dependency lock")
    metadata = BundleMetadata(
        channel=channel,
        version=version,
        build_id=build_id,
        source_revision=source_revision,
        files=tuple(files),
    )
    _safe_basename(
        metadata.file_for_role("focus-wheel").name,
        label="focus wheel name",
        suffix=".whl",
    )
    if metadata.file_for_role("python-dependency-lock").name != DEPENDENCY_LOCK_NAME:
        raise InstallBundleError(f"dependency lock必须命名为{DEPENDENCY_LOCK_NAME}")
    return metadata


def channel_manifest_payload(
    *,
    metadata: BundleMetadata,
    release_tag: str,
    bundle_name: str,
    bundle_size: int,
    bundle_sha256: str,
) -> dict[str, Any]:
    if metadata.channel not in REMOTE_CHANNELS:
        raise InstallBundleError("只有stable/development bundle可生成远端channel manifest")
    return {
        "build_id": metadata.build_id,
        "bundle": {
            "name": _safe_basename(bundle_name, label="channel bundle name", suffix=".zip"),
            "sha256": _require_sha256(bundle_sha256, label="channel bundle sha256"),
            "size": _require_positive_int(
                bundle_size,
                label="channel bundle size",
                maximum=_MAX_BUNDLE_BYTES,
            ),
        },
        "channel": metadata.channel,
        "release_tag": _require_string(release_tag, label="release tag", identifier=True),
        "schema": CHANNEL_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "source_revision": metadata.source_revision,
        "version": metadata.version,
    }


def parse_channel_manifest(raw: bytes, *, expected_channel: str) -> ChannelManifest:
    if expected_channel not in REMOTE_CHANNELS:
        raise InstallBundleError(f"不是远端安装channel：{expected_channel}")
    payload = _load_json_object(raw, label="channel manifest")
    _require_exact_keys(
        payload,
        {
            "build_id",
            "bundle",
            "channel",
            "release_tag",
            "schema",
            "schema_version",
            "source_revision",
            "version",
        },
        label="channel manifest",
    )
    if payload["schema"] != CHANNEL_SCHEMA or payload["schema_version"] != SCHEMA_VERSION:
        raise InstallBundleError("channel manifest schema或version不受支持")
    if payload["channel"] != expected_channel:
        raise InstallBundleError(
            f"channel manifest声明{payload['channel']!r}，预期为{expected_channel!r}"
        )
    raw_bundle = payload["bundle"]
    if not isinstance(raw_bundle, dict):
        raise InstallBundleError("channel manifest bundle必须是object")
    _require_exact_keys(
        raw_bundle,
        {"name", "sha256", "size"},
        label="channel manifest bundle",
    )
    return ChannelManifest(
        channel=expected_channel,
        release_tag=_require_string(
            payload["release_tag"],
            label="channel release_tag",
            identifier=True,
        ),
        version=_require_string(payload["version"], label="channel version", identifier=True),
        build_id=_require_string(payload["build_id"], label="channel build_id", identifier=True),
        source_revision=_require_string(
            payload["source_revision"],
            label="channel source_revision",
            identifier=True,
        ),
        bundle=ChannelBundle(
            name=_safe_basename(
                raw_bundle["name"],
                label="channel bundle name",
                suffix=".zip",
            ),
            size=_require_positive_int(
                raw_bundle["size"],
                label="channel bundle size",
                maximum=_MAX_BUNDLE_BYTES,
            ),
            sha256=_require_sha256(
                raw_bundle["sha256"],
                label="channel bundle sha256",
            ),
        ),
    )


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def _read_wheel_identity(wheel_path: pathlib.Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            infos = [item for item in archive.infolist() if not item.is_dir()]
            archive_names = [item.filename for item in infos]
            if len(archive_names) != len(set(archive_names)):
                raise InstallBundleError("Focus wheel包含重复entry")
            for info in infos:
                relative = pathlib.PurePosixPath(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or "\\" in info.filename
                    or mode == stat.S_IFLNK
                ):
                    raise InstallBundleError(f"Focus wheel包含不安全entry：{info.filename}")
            metadata_infos = [
                item
                for item in infos
                if len(pathlib.PurePosixPath(item.filename).parts) == 2
                and pathlib.PurePosixPath(item.filename).parts[0].endswith(".dist-info")
                and pathlib.PurePosixPath(item.filename).name == "METADATA"
            ]
            if len(metadata_infos) != 1:
                raise InstallBundleError("Focus wheel必须恰好包含一个.dist-info/METADATA")
            message = BytesParser().parsebytes(archive.read(metadata_infos[0]))
    except (OSError, zipfile.BadZipFile) as exc:
        raise InstallBundleError(f"无法读取Focus wheel：{wheel_path}") from exc
    metadata_names = message.get_all("Name", [])
    versions = message.get_all("Version", [])
    if (
        len(metadata_names) != 1
        or metadata_names[0].strip().lower().replace("_", "-") != "focus"
    ):
        raise InstallBundleError("wheel METADATA Name必须唯一且为focus")
    if len(versions) != 1 or not versions[0].strip():
        raise InstallBundleError("wheel METADATA Version必须唯一且非空")
    missing_web = sorted(set(_REQUIRED_WEB_FILES) - set(archive_names))
    if missing_web:
        raise InstallBundleError(
            "Focus wheel缺少production Web payload：" + ",".join(missing_web)
        )
    return metadata_names[0].strip(), versions[0].strip()


def _require_web_build(source_dir: pathlib.Path) -> None:
    missing = [relative for relative in _REQUIRED_WEB_FILES if not (source_dir / relative).is_file()]
    if missing:
        raise InstallBundleError(
            "Focus install bundle缺少production Web build；请先在web目录执行`npm run build`。"
            " missing=" + ",".join(missing)
        )


def build_install_bundle(
    *,
    source_dir: pathlib.Path,
    output_dir: pathlib.Path,
    channel: str,
    source_revision: str,
    build_id: str | None = None,
    release_tag: str | None = None,
    python_executable: pathlib.Path | str = sys.executable,
) -> BuiltInstallBundle:
    """Build a deterministic wheel-bearing bundle and optional remote descriptor."""

    if channel not in ALL_CHANNELS:
        raise InstallBundleError(f"bundle channel不受支持：{channel}")
    source_revision = _require_string(
        source_revision,
        label="source_revision",
        identifier=True,
    )
    if channel in REMOTE_CHANNELS and not release_tag:
        raise InstallBundleError("stable/development bundle必须声明release_tag")
    if channel == "local" and release_tag is not None:
        raise InstallBundleError("local bundle不能声明release_tag")
    if channel == "development" and release_tag != "development-builds":
        raise InstallBundleError(
            "development bundle必须使用固定release_tag development-builds"
        )

    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    lock_path = source_dir / DEPENDENCY_LOCK_NAME
    if not lock_path.is_file() or lock_path.is_symlink():
        raise InstallBundleError(f"source缺少普通Python dependency lock：{lock_path}")
    _require_web_build(source_dir)
    lock_bytes = lock_path.read_bytes()
    try:
        lock_text = lock_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstallBundleError("requirements.lock必须是UTF-8") from exc
    if "==" not in lock_text or "\x00" in lock_text:
        raise InstallBundleError("requirements.lock不是有效的locked requirements投影")
    if len(lock_bytes) > _MAX_EXPANDED_BYTES:
        raise InstallBundleError("requirements.lock超过bundle上限")

    output_dir.mkdir(parents=True, exist_ok=True)
    import tempfile

    with tempfile.TemporaryDirectory(
        prefix="focus-install-bundle-",
        ignore_cleanup_errors=True,
    ) as raw_stage:
        stage = pathlib.Path(raw_stage)
        try:
            wheel_path = build_validated_wheel(
                source_dir=source_dir,
                output_dir=stage,
                python_executable=python_executable,
            )
        except PythonDistributionBuildError as exc:
            raise InstallBundleError(str(exc)) from exc
        _, version = _read_wheel_identity(wheel_path)
        version = _require_string(version, label="wheel version", identifier=True)
        if channel == "stable" and release_tag is not None:
            normalized_tag = release_tag[1:] if release_tag.startswith("v") else release_tag
            if normalized_tag != version:
                raise InstallBundleError(
                    f"stable release tag {release_tag!r}与wheel version {version!r}不一致"
                )
        wheel_bytes = wheel_path.read_bytes()
        if not wheel_bytes or len(wheel_bytes) > _MAX_EXPANDED_BYTES:
            raise InstallBundleError("Focus wheel大小不符合bundle上限")
        if build_id is None:
            build_id = hashlib.sha256(
                wheel_bytes + b"\0" + lock_bytes + b"\0" + channel.encode("ascii")
            ).hexdigest()[:16]
        build_id = _require_string(build_id, label="build_id", identifier=True)
        metadata = BundleMetadata(
            channel=channel,
            version=version,
            build_id=build_id,
            source_revision=source_revision,
            files=(
                ArtifactFile(
                    name=DEPENDENCY_LOCK_NAME,
                    role="python-dependency-lock",
                    size=len(lock_bytes),
                    sha256=_sha256_bytes(lock_bytes),
                ),
                ArtifactFile(
                    name=wheel_path.name,
                    role="focus-wheel",
                    size=len(wheel_bytes),
                    sha256=_sha256_bytes(wheel_bytes),
                ),
            ),
        )
        manifest_bytes = _json_bytes(_bundle_manifest_payload(metadata))
        bundle_name = f"focus-install-{version}-{build_id}.zip"
        bundle_path = output_dir / bundle_name
        created_bundle = False
        try:
            with bundle_path.open("xb") as raw_bundle:
                created_bundle = True
                with zipfile.ZipFile(
                    raw_bundle,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                ) as archive:
                    archive.writestr(_zip_info(BUNDLE_MANIFEST_NAME), manifest_bytes)
                    archive.writestr(_zip_info(DEPENDENCY_LOCK_NAME), lock_bytes)
                    archive.writestr(_zip_info(wheel_path.name), wheel_bytes)
        except FileExistsError as exc:
            raise InstallBundleError(f"输出bundle已存在，拒绝覆盖：{bundle_path}") from exc
        except BaseException:
            if created_bundle:
                bundle_path.unlink(missing_ok=True)
            raise
        bundle_size = bundle_path.stat().st_size
        if bundle_size <= 0 or bundle_size > _MAX_BUNDLE_BYTES:
            bundle_path.unlink(missing_ok=True)
            raise InstallBundleError("生成的bundle大小不符合上限")

        if channel == "local":
            channel_manifest_path = None
        else:
            channel_manifest_path = output_dir / CHANNEL_MANIFEST_NAMES[channel]
            channel_payload = channel_manifest_payload(
                metadata=metadata,
                release_tag=release_tag or "",
                bundle_name=bundle_path.name,
                bundle_size=bundle_size,
                bundle_sha256=sha256_file(bundle_path),
            )
            try:
                with channel_manifest_path.open("xb") as handle:
                    handle.write(_json_bytes(channel_payload))
            except FileExistsError as exc:
                bundle_path.unlink(missing_ok=True)
                raise InstallBundleError(
                    f"输出channel manifest已存在，拒绝覆盖：{channel_manifest_path}"
                ) from exc
        return BuiltInstallBundle(
            bundle_path=bundle_path,
            channel_manifest_path=channel_manifest_path,
            metadata=metadata,
        )


def _validate_archive_entry(info: zipfile.ZipInfo) -> None:
    relative = pathlib.PurePosixPath(info.filename)
    mode = (info.external_attr >> 16) & 0o170000
    if (
        info.is_dir()
        or relative.is_absolute()
        or len(relative.parts) != 1
        or relative.name != info.filename
        or "\\" in info.filename
        or info.filename in {".", ".."}
        or mode == stat.S_IFLNK
        or info.flag_bits & 0x1
    ):
        raise InstallBundleError(f"bundle包含不安全entry：{info.filename}")


def validate_install_bundle(
    artifact_path: pathlib.Path,
    *,
    extraction_dir: pathlib.Path,
    expected_channel: str | None = None,
    expected_version: str | None = None,
    expected_build_id: str | None = None,
    expected_source_revision: str | None = None,
) -> ValidatedInstallBundle:
    """Validate every byte and extract only the two declared payload files."""

    artifact_path = artifact_path.resolve()
    if not artifact_path.is_file():
        raise InstallBundleError(f"install artifact不是普通文件：{artifact_path}")
    artifact_size = artifact_path.stat().st_size
    if artifact_size <= 0 or artifact_size > _MAX_BUNDLE_BYTES:
        raise InstallBundleError("install artifact大小不符合bundle上限")
    extraction_dir = extraction_dir.resolve()
    extraction_dir.mkdir(parents=True, exist_ok=True)
    if any(extraction_dir.iterdir()):
        raise InstallBundleError(f"bundle extraction目录必须为空：{extraction_dir}")

    try:
        with zipfile.ZipFile(artifact_path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                raise InstallBundleError("bundle包含重复entry")
            if len(infos) != 3 or BUNDLE_MANIFEST_NAME not in names:
                raise InstallBundleError("bundle必须恰好包含manifest、wheel和dependency lock")
            for info in infos:
                _validate_archive_entry(info)
            if sum(item.file_size for item in infos) > _MAX_EXPANDED_BYTES:
                raise InstallBundleError("bundle展开大小超过上限")
            manifest_info = archive.getinfo(BUNDLE_MANIFEST_NAME)
            if manifest_info.file_size > _MAX_MANIFEST_BYTES:
                raise InstallBundleError("bundle manifest超过上限")
            metadata = parse_bundle_manifest(archive.read(manifest_info))
            expected_names = {BUNDLE_MANIFEST_NAME, *(item.name for item in metadata.files)}
            if set(names) != expected_names:
                raise InstallBundleError("bundle entries与manifest声明不一致")
            for declared in metadata.files:
                info = archive.getinfo(declared.name)
                if info.file_size != declared.size:
                    raise InstallBundleError(f"bundle entry size不匹配：{declared.name}")
                target = extraction_dir / declared.name
                digest = hashlib.sha256()
                written = 0
                with archive.open(info) as source, target.open("xb") as destination:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        written += len(chunk)
                        if written > declared.size:
                            raise InstallBundleError(f"bundle entry展开越界：{declared.name}")
                        digest.update(chunk)
                        destination.write(chunk)
                if written != declared.size or digest.hexdigest() != declared.sha256:
                    raise InstallBundleError(f"bundle entry内容校验失败：{declared.name}")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, InstallBundleError):
            raise
        raise InstallBundleError(f"无法读取install artifact：{artifact_path}") from exc

    if expected_channel is not None and metadata.channel != expected_channel:
        raise InstallBundleError(
            f"bundle channel为{metadata.channel!r}，预期为{expected_channel!r}"
        )
    if expected_version is not None and metadata.version != expected_version:
        raise InstallBundleError("bundle version与channel manifest不一致")
    if expected_build_id is not None and metadata.build_id != expected_build_id:
        raise InstallBundleError("bundle build_id与channel manifest不一致")
    if (
        expected_source_revision is not None
        and metadata.source_revision != expected_source_revision
    ):
        raise InstallBundleError("bundle source_revision与channel manifest不一致")

    wheel = metadata.file_for_role("focus-wheel")
    lock = metadata.file_for_role("python-dependency-lock")
    wheel_path = extraction_dir / wheel.name
    _, wheel_version = _read_wheel_identity(wheel_path)
    if wheel_version != metadata.version:
        raise InstallBundleError("Focus wheel version与bundle manifest不一致")
    try:
        lock_text = (extraction_dir / lock.name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallBundleError("bundle dependency lock不是UTF-8") from exc
    if "==" not in lock_text or "\x00" in lock_text:
        raise InstallBundleError("bundle dependency lock不是有效的locked requirements投影")
    return ValidatedInstallBundle(
        root=extraction_dir,
        metadata=metadata,
        wheel_path=wheel_path,
        dependency_lock_path=extraction_dir / lock.name,
    )
