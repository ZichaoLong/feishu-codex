from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


FINAL_REPLY_TEXT_OPEN = "<final_reply_text>"
FINAL_REPLY_TEXT_CLOSE = "</final_reply_text>"
TERMINAL_RESULT_CARD_TITLE = "Codex 最终结果"
TERMINAL_RESULT_CARD_HINT = (
    "*以下区块是本轮权威 `final_reply_text`，可被其他 Codex 机器人稳定解析。*"
)

_FINAL_REPLY_BLOCK_PATTERN = re.compile(
    rf"{re.escape(FINAL_REPLY_TEXT_OPEN)}\s*(.*?)\s*{re.escape(FINAL_REPLY_TEXT_CLOSE)}",
    re.DOTALL,
)
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
    return f"{FINAL_REPLY_TEXT_OPEN}\n{normalized}\n{FINAL_REPLY_TEXT_CLOSE}"


def can_render_terminal_result_card(final_reply_text: str, *, char_limit: int) -> bool:
    normalized = str(final_reply_text or "").strip()
    if not normalized:
        return False
    if FINAL_REPLY_TEXT_OPEN in normalized or FINAL_REPLY_TEXT_CLOSE in normalized:
        return False
    budget = max(int(char_limit), 0)
    if budget <= 0:
        return False
    payload = f"{TERMINAL_RESULT_CARD_HINT}\n\n{render_final_reply_text_block(normalized)}"
    return len(payload) <= budget


def project_interactive_card_text(content_dict: dict[str, Any]) -> CardTextProjection:
    visible_text = _extract_visible_card_text(content_dict)
    final_reply_text = _extract_authoritative_final_reply_text(visible_text)
    if final_reply_text:
        return CardTextProjection(
            text=final_reply_text,
            visible_text=visible_text,
            final_reply_text=final_reply_text,
        )
    return CardTextProjection(
        text=visible_text,
        visible_text=visible_text,
    )


def _extract_authoritative_final_reply_text(visible_text: str) -> str:
    normalized_visible = str(visible_text or "").strip()
    if not normalized_visible:
        return ""
    match = _FINAL_REPLY_BLOCK_PATTERN.search(normalized_visible)
    if not match:
        return ""
    return str(match.group(1) or "").strip()


def _extract_visible_card_text(content_dict: dict[str, Any]) -> str:
    blocks: list[str] = []
    _append_block(blocks, content_dict.get("title", ""))
    _collect_visible_blocks(content_dict, blocks)
    return "\n\n".join(blocks).strip()


def _append_block(blocks: list[str], text: Any) -> None:
    normalized = str(text or "").strip()
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
