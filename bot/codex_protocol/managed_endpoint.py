"""Listen-endpoint allocation for one owned Codex app-server process."""

from __future__ import annotations

import logging
import socket
from typing import Any
from urllib.parse import urlunsplit

from bot.network_contract import parse_owned_app_server_listen_endpoint
from bot.stores.app_server_runtime_store import uses_default_app_server_url


logger = logging.getLogger(__name__)


class ManagedAppServerEndpointAllocator:
    """Select and allocate bindable endpoints without owning RPC state."""

    def __init__(self, configured_url: str) -> None:
        self._configured_url = str(configured_url or "").strip()

    def select(self) -> str:
        if not uses_default_app_server_url(self._configured_url):
            return self._configured_url
        if self.can_bind(self._configured_url):
            return self._configured_url
        fallback_url = self.allocate(self._configured_url)
        logger.warning(
            "Codex app-server 默认地址 %s 不可用，自动切换到 %s",
            self._configured_url,
            fallback_url,
        )
        return fallback_url

    @classmethod
    def can_bind(cls, url: str) -> bool:
        family, address = cls._socket_address(url)
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(address)
            except OSError:
                return False
        return True

    @classmethod
    def allocate(cls, url: str) -> str:
        endpoint = parse_owned_app_server_listen_endpoint(url)
        family = socket.AF_INET6 if ":" in endpoint.host else socket.AF_INET
        bind_address: tuple[Any, ...] = (
            (endpoint.host, 0, 0, 0)
            if family == socket.AF_INET6
            else (endpoint.host, 0)
        )
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind(bind_address)
            actual_port = int(sock.getsockname()[1])
        netloc = (
            f"[{endpoint.host}]:{actual_port}"
            if ":" in endpoint.host
            else f"{endpoint.host}:{actual_port}"
        )
        return urlunsplit((endpoint.scheme, netloc, endpoint.path, "", ""))

    @staticmethod
    def _socket_address(
        url: str,
    ) -> tuple[socket.AddressFamily, tuple[Any, ...]]:
        endpoint = parse_owned_app_server_listen_endpoint(url)
        if ":" in endpoint.host:
            return socket.AF_INET6, (endpoint.host, endpoint.port, 0, 0)
        return socket.AF_INET, (endpoint.host, endpoint.port)


def log_managed_stream(stream: Any, level: int, name: str) -> None:
    """Forward one managed process stream into the client logger."""

    for line in iter(stream.readline, ""):
        text = line.rstrip()
        if text:
            logger.log(level, "[codex app-server %s] %s", name, text)
