"""Pure transformations for app-server prompt input items."""

from __future__ import annotations

from typing import Any


def replace_text_input_items(
    input_items: list[dict[str, Any]],
    text: str,
) -> list[dict[str, Any]]:
    """Replace all text items with one item while preserving other inputs."""

    normalized_text = str(text or "")
    replaced: list[dict[str, Any]] = []
    inserted_text = False
    for item in input_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            if not inserted_text:
                replacement = dict(item)
                replacement["text"] = normalized_text
                replaced.append(replacement)
                inserted_text = True
            continue
        replaced.append(dict(item))
    if not inserted_text:
        replaced.insert(0, {"type": "text", "text": normalized_text})
    return replaced
