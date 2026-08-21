"""
Local websocket auth helpers.

This module owns the small set of secrets and helpers used to tighten the
localhost websocket surfaces between `focusctl`, `focusd`, and `focus`/`fcodex`.
"""

from __future__ import annotations

import pathlib
import secrets

from bot.atomic_file import ensure_private_token
from bot.file_permissions import ensure_private_file_permissions

APP_SERVER_WEBSOCKET_TOKEN_FILENAME = "app_server_websocket.token"
FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR = "FOCUS_REMOTE_AUTH_TOKEN"
FOCUS_SERVICE_TOKEN_ENV_VAR = "FOCUS_SERVICE_TOKEN"


class MissingAppServerWebsocketAuthTokenError(RuntimeError):
    """Raised when an attached app-server client cannot load its local token."""


def build_bearer_authorization_headers(token: str) -> dict[str, str]:
    normalized = str(token or "").strip()
    if not normalized:
        return {}
    return {"Authorization": f"Bearer {normalized}"}


def parse_bearer_authorization_header(raw_value: str | None) -> str:
    normalized = str(raw_value or "").strip()
    if not normalized:
        return ""
    scheme, sep, remainder = normalized.partition(" ")
    if not sep or not scheme or not scheme.lower() == "bearer":
        return ""
    token = remainder.strip()
    if not token:
        return ""
    return token


class AppServerWebsocketAuthTokenStore:
    def __init__(self, data_dir: pathlib.Path | str) -> None:
        self._data_dir = pathlib.Path(data_dir)

    @property
    def path(self) -> pathlib.Path:
        return self._data_dir / APP_SERVER_WEBSOCKET_TOKEN_FILENAME

    def load(self) -> str:
        path = self.path
        if not path.exists():
            return ""
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if not token:
            return ""
        ensure_private_file_permissions(path)
        return token

    def require(self) -> str:
        token = self.load()
        if token:
            return token
        raise MissingAppServerWebsocketAuthTokenError(
            "backend websocket auth token 不存在；"
            f"请确认目标实例已升级并重启，然后重试。缺失文件：{self.path}"
        )

    def ensure(self) -> str:
        return ensure_private_token(self.path, lambda: secrets.token_urlsafe(32))
