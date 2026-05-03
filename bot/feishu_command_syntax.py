"""
Feishu 用户可见命令语法辅助。
"""

from __future__ import annotations

import re

_ANGLE_PLACEHOLDER_RE = re.compile(r"<([^<>\n]+)>")


def feishu_visible_command_syntax(text: str) -> str:
    """把 `<arg>` 占位符渲染为 Feishu 可见的 `〈arg〉`。

    Feishu 富文本会把 ASCII `<...>` 当作标签处理，即使位于代码样式里也可能不显示。
    这里只应用于用户可见的命令示例，不应用于协议标签或机器可读标记。
    """

    return _ANGLE_PLACEHOLDER_RE.sub(lambda match: f"〈{match.group(1)}〉", text)
