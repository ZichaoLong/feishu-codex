"""本地绑定状态管理命令。"""

from __future__ import annotations

import os
from pathlib import Path

from bot.constants import FC_DATA_DIR
from bot.stores.chat_binding_store import ChatBindingStore


def main() -> None:
    data_dir = Path(os.environ.get("FC_DATA_DIR", "")).expanduser() if os.environ.get("FC_DATA_DIR") else FC_DATA_DIR
    ChatBindingStore(data_dir).clear_all()
    print(f"已清空 Feishu 聊天绑定：{data_dir / 'chat_bindings.json'}")


if __name__ == "__main__":
    main()
