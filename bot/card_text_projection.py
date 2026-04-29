from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TERMINAL_RESULT_CARD_TITLE = "Codex"
TERMINAL_RESULT_CARD_MARKER = "\u2063\u2060\u2064\u2060\u2063"
_TEXT_NODE_TAGS = {"markdown", "plain_text", "lark_md"}
_IGNORED_TAGS = {
    "action",
    "button",
    "checkbox",
    "date_picker",
    "form",
    "input",
    "overflow",
    "picker_date",
    "picker_datetime",
    "picker_time",
    "select_img",
    "select_person",
    "select_static",
    "text_area",
    "textarea",
}


@dataclass(frozen=True, slots=True)
class CardTextProjection:
    text: str
    visible_text: str
    final_reply_text: str = ""

    @property
    def has_authoritative_final_reply(self) -> bool:
        return bool(self.final_reply_text)


def render_final_reply_text_block(final_reply_text: str) -> str:
    normalized = str(final_reply_text or "").strip()
    if not normalized:
        return ""
    return f"{normalized}{TERMINAL_RESULT_CARD_MARKER}"


def can_render_terminal_result_card(final_reply_text: str, *, char_limit: int) -> bool:
    normalized = str(final_reply_text or "").strip()
    if not normalized:
        return False
    if TERMINAL_RESULT_CARD_MARKER in normalized:
        return False
    budget = max(int(char_limit), 0)
    if budget <= 0:
        return False
    payload = render_final_reply_text_block(normalized)
    return len(payload) <= budget


def _contains_terminal_result_marker(text: str) -> bool:
    return TERMINAL_RESULT_CARD_MARKER in str(text or "")


def _strip_terminal_result_marker(text: str) -> str:
    return str(text or "").replace(TERMINAL_RESULT_CARD_MARKER, "")


def project_interactive_card_text(content_dict: dict[str, Any]) -> CardTextProjection:
    terminal_projection = _project_terminal_result_card_text(content_dict)
    if terminal_projection is not None:
        return terminal_projection
    visible_text = _extract_visible_card_text(content_dict)
    return CardTextProjection(text=visible_text, visible_text=visible_text)


def _project_terminal_result_card_text(
    content_dict: dict[str, Any],
) -> CardTextProjection | None:
    if not _matches_terminal_result_card_contract(content_dict):
        return None
    visible_text = _extract_visible_card_text(content_dict)
    final_reply_text = _extract_terminal_result_card_final_reply_text(content_dict)
    if not final_reply_text:
        return CardTextProjection(text="", visible_text=visible_text)
    return CardTextProjection(
        text=final_reply_text,
        visible_text=visible_text,
        final_reply_text=final_reply_text,
    )


def _matches_terminal_result_card_contract(content_dict: dict[str, Any]) -> bool:
    header = content_dict.get("header") or {}
    if not isinstance(header, dict):
        return False
    title = header.get("title") or {}
    if not isinstance(title, dict):
        return False
    if str(title.get("content", "") or "").strip() != TERMINAL_RESULT_CARD_TITLE:
        return False
    if str(header.get("template", "") or "").strip() != "green":
        return False

    elements = content_dict.get("elements") or []
    if not isinstance(elements, list):
        return False
    has_final_reply_block = any(
        isinstance(element, dict)
        and str(element.get("tag", "") or "").strip() == "markdown"
        and _contains_terminal_result_marker(str(element.get("content", "") or ""))
        for element in elements
    )
    return has_final_reply_block


def _extract_terminal_result_card_final_reply_text(content_dict: dict[str, Any]) -> str:
    elements = content_dict.get("elements") or []
    if not isinstance(elements, list):
        return ""
    for element in elements:
        if not isinstance(element, dict):
            continue
        if str(element.get("tag", "") or "").strip() != "markdown":
            continue
        content = str(element.get("content", "") or "")
        if _contains_terminal_result_marker(content):
            return _strip_terminal_result_marker(content).strip()
    return ""


def _extract_visible_card_text(content_dict: dict[str, Any]) -> str:
    blocks: list[str] = []
    _append_block(blocks, content_dict.get("title", ""))
    _collect_visible_blocks(content_dict, blocks)
    return "\n\n".join(blocks).strip()


def _append_block(blocks: list[str], text: Any) -> None:
    normalized = _strip_terminal_result_marker(str(text or "")).strip()
    if not normalized:
        return
    if blocks and blocks[-1] == normalized:
        return
    blocks.append(normalized)


def _collect_visible_blocks(node: Any, blocks: list[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _collect_visible_blocks(item, blocks)
        return
    if not isinstance(node, dict):
        return

    tag = str(node.get("tag", "") or "").strip()
    if tag in _IGNORED_TAGS:
        return
    if tag == "text":
        _append_block(blocks, node.get("text") or node.get("content"))
        return
    if tag in _TEXT_NODE_TAGS:
        _append_block(blocks, node.get("content", ""))
        return
    if tag == "img":
        alt = node.get("alt")
        if isinstance(alt, dict):
            _collect_visible_blocks(alt, blocks)
        else:
            _append_block(blocks, alt)
        return
    if tag == "div":
        _collect_visible_blocks(node.get("text"), blocks)
        _collect_visible_blocks(node.get("fields"), blocks)
        return
    if tag == "note":
        _collect_visible_blocks(node.get("elements"), blocks)
        return
    if tag == "column_set":
        _collect_visible_blocks(node.get("columns"), blocks)
        return
    if tag == "column":
        _collect_visible_blocks(node.get("elements"), blocks)
        return
    if tag == "collapsible_panel":
        header = node.get("header") or {}
        if isinstance(header, dict):
            _collect_visible_blocks(header.get("title"), blocks)
        _collect_visible_blocks(node.get("elements"), blocks)
        return
    if tag == "header":
        _collect_visible_blocks(node.get("title"), blocks)
        return
    if tag == "body":
        _collect_visible_blocks(node.get("elements"), blocks)
        return
    if tag == "hr":
        return

    header = node.get("header")
    if isinstance(header, dict):
        _collect_visible_blocks(header, blocks)
    body = node.get("body")
    if isinstance(body, dict):
        _collect_visible_blocks(body, blocks)
    elif isinstance(body, list):
        _collect_visible_blocks(body, blocks)
    title = node.get("title")
    if isinstance(title, dict):
        _collect_visible_blocks(title, blocks)
    text = node.get("text")
    if isinstance(text, dict):
        _collect_visible_blocks(text, blocks)
    fields = node.get("fields")
    if isinstance(fields, list):
        _collect_visible_blocks(fields, blocks)
    elements = node.get("elements")
    if isinstance(elements, list):
        _collect_visible_blocks(elements, blocks)
