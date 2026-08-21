"""Shared facts and validation for same-host native media input.

Native ``localImage`` input crosses two independent trust boundaries:

* the file must be visible to the app-server on the same filesystem and its
  bytes, rather than its name or declared MIME type, must identify an image;
* the model which will consume the input must be an upstream-proven effective
  model whose ``model/list`` entry explicitly advertises the image modality.

This module is deliberately pure.  Web and Feishu use the same media rules
instead of growing separate MIME and capability interpretations.  Upstream
thread settings are owned separately by ``bot.thread_effective_settings``.
"""

from __future__ import annotations

import os
import pathlib
import stat
from typing import Iterable

from bot.adapters.base import RuntimeModelSummary


_SAFE_IMAGE_MEDIA_TYPES = frozenset(
    {
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/x-icon",
        "image/vnd.microsoft.icon",
    }
)
_SAFE_AUDIO_MEDIA_TYPES = frozenset(
    {
        "audio/aac",
        "audio/flac",
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
    }
)
_NATIVE_MEDIA_TYPE_ALIASES = {
    "audio/mp3": "audio/mpeg",
    "audio/x-m4a": "audio/mp4",
    "audio/x-wav": "audio/wav",
    "image/jpg": "image/jpeg",
    "image/vnd.microsoft.icon": "image/x-icon",
}
_NATIVE_INPUT_MEDIA_TYPES = _SAFE_IMAGE_MEDIA_TYPES | _SAFE_AUDIO_MEDIA_TYPES


def normalize_native_media_type(value: object) -> str:
    media_type = str(value or "application/octet-stream").split(";", 1)[0].strip().lower()
    if not media_type:
        return "application/octet-stream"
    return _NATIVE_MEDIA_TYPE_ALIASES.get(media_type, media_type)


def is_native_input_media_type(value: object) -> bool:
    return normalize_native_media_type(value) in _NATIVE_INPUT_MEDIA_TYPES


def is_safe_image_media_type(value: object) -> bool:
    return normalize_native_media_type(value) in _SAFE_IMAGE_MEDIA_TYPES


def detect_native_media_type(content: bytes) -> str:
    """Identify the bounded set of native media formats by byte signature."""

    payload = bytes(content)
    if (
        len(payload) >= 24
        and payload.startswith(b"\x89PNG\r\n\x1a\n")
        and payload[12:16] == b"IHDR"
    ):
        return "image/png"
    if len(payload) >= 4 and payload.startswith(b"\xff\xd8") and payload.endswith(b"\xff\xd9"):
        return "image/jpeg"
    if len(payload) >= 13 and payload[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if len(payload) >= 16 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    if len(payload) >= 26 and payload.startswith(b"BM"):
        return "image/bmp"
    if (
        len(payload) >= 22
        and payload[:4] in {b"\x00\x00\x01\x00", b"\x00\x00\x02\x00"}
        and int.from_bytes(payload[4:6], "little") > 0
    ):
        return "image/x-icon"
    if (
        len(payload) >= 16
        and payload[4:8] == b"ftyp"
        and any(brand in payload[8:32] for brand in (b"avif", b"avis"))
    ):
        return "image/avif"
    if (
        len(payload) >= 20
        and payload[:4] == b"RIFF"
        and payload[8:12] == b"WAVE"
        and b"fmt " in payload[:64]
    ):
        return "audio/wav"
    if payload.startswith(b"fLaC"):
        return "audio/flac"
    if payload.startswith(b"OggS") and any(
        marker in payload[:8192]
        for marker in (b"OpusHead", b"\x01vorbis", b"\x7fFLAC")
    ):
        return "audio/ogg"
    if (
        len(payload) >= 12
        and payload[4:8] == b"ftyp"
        and any(brand in payload[8:32] for brand in (b"M4A ", b"M4B ", b"M4P "))
    ):
        return "audio/mp4"
    if (
        len(payload) >= 8
        and payload.startswith(b"\x1aE\xdf\xa3")
        and b"webm" in payload[:4096]
        and b"A_" in payload[:8192]
    ):
        return "audio/webm"
    if payload.startswith(b"ID3") or (
        len(payload) >= 2
        and payload[0] == 0xFF
        and payload[1] & 0xE0 == 0xE0
        and payload[1] & 0x06 != 0
    ):
        return "audio/mpeg"
    if (
        len(payload) >= 2
        and payload[0] == 0xFF
        and payload[1] & 0xF6 == 0xF0
    ):
        return "audio/aac"
    return ""


def validate_declared_native_media(media_type: object, content: bytes) -> str:
    """Return the canonical type, rejecting spoofed native media declarations."""

    normalized = normalize_native_media_type(media_type)
    if normalized not in _NATIVE_INPUT_MEDIA_TYPES:
        return normalized
    if detect_native_media_type(content) != normalized:
        raise ValueError("Attachment bytes do not match the declared native media type.")
    return normalized


def verified_local_image_media_type(
    path: object,
    *,
    expected_parent: object = "",
) -> str:
    """Return a signature-proven staged image type, or empty.

    This rejects a symlink or non-regular file at the authorization boundary,
    checks an optional canonical staging parent, and reads through a no-follow
    descriptor where the platform supports it.  The app-server protocol still
    receives a path rather than this descriptor, so callers must not mistake
    this check for an immutable-file handoff.
    """

    target = pathlib.Path(str(path or "")).expanduser()
    expected_parent_text = str(expected_parent or "").strip()
    descriptor: int | None = None
    try:
        before = target.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            return ""
        resolved = target.resolve(strict=True)
        if expected_parent_text:
            expected = pathlib.Path(expected_parent_text).expanduser()
            if resolved.parent != expected:
                return ""
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            return ""
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = None
            content = stream.read()
        after = target.lstat()
        if (
            stat.S_ISLNK(after.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            return ""
        detected = detect_native_media_type(content)
    except (OSError, RuntimeError, ValueError):
        return ""
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return detected if detected in _SAFE_IMAGE_MEDIA_TYPES else ""


def model_supports_input(
    models: Iterable[RuntimeModelSummary],
    effective_model: object,
    modality: object,
) -> bool | None:
    """Resolve explicit model metadata without treating absence as denial."""

    model_id = str(effective_model or "").strip()
    input_kind = str(modality or "").strip().lower()
    if not model_id or not input_kind:
        return None
    summary = next((item for item in models if str(item.model or "").strip() == model_id), None)
    if summary is None or summary.input_modalities is None:
        return None
    return input_kind in {
        str(value or "").strip().lower()
        for value in summary.input_modalities
    }
