"""Shared admission rules for Focus-owned network endpoints."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


FOCUS_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MIN_WEB_SESSION_TTL_SECONDS = 60.0
_CANONICAL_DNS_HOST_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z"
)
_IPV4_NUMBER_PATTERN = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)\Z")


@dataclass(frozen=True, slots=True)
class AppServerEndpoint:
    """Validated WebSocket endpoint used by the app-server client."""

    scheme: str
    host: str
    port: int
    path: str

    @property
    def url(self) -> str:
        netloc = (
            f"[{self.host}]:{self.port}"
            if ":" in self.host
            else f"{self.host}:{self.port}"
        )
        return urlunsplit((self.scheme, netloc, self.path, "", ""))


@dataclass(frozen=True, slots=True)
class OwnedWebGatewayEndpoint:
    """Canonical loopback HTTP origin published by Focus Web Gateway."""

    host: str
    port: int

    @property
    def origin(self) -> str:
        netloc = (
            f"[{self.host}]:{self.port}"
            if ":" in self.host
            else f"{self.host}:{self.port}"
        )
        return urlunsplit(("http", netloc, "", "", ""))


@dataclass(frozen=True, slots=True)
class TrustedProxyExternalOrigin:
    """Canonical HTTPS origin admitted through a configured trusted proxy."""

    host: str
    port: int | None

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port is None else f"{host}:{self.port}"

    @property
    def origin(self) -> str:
        return urlunsplit(("https", self.authority, "", "", ""))


def parse_app_server_endpoint(url: str) -> AppServerEndpoint:
    """Parse the WebSocket endpoint shape supported by attached clients."""

    value = str(url).strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("app-server URL 不能为空或包含空白字符")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"app-server URL 无效：{url}") from exc
    if parsed.scheme not in {"ws", "wss"}:
        raise ValueError(f"app-server URL 仅支持 ws/wss：{url}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"app-server URL 不支持内嵌凭据：{url}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"app-server URL 不支持 query/fragment：{url}")
    host = parsed.hostname
    if not host or port is None or port <= 0:
        raise ValueError(f"app-server URL 必须包含有效 host/port：{url}")
    return AppServerEndpoint(
        scheme=parsed.scheme,
        host=host.lower(),
        port=port,
        path=parsed.path,
    )


def parse_owned_app_server_listen_endpoint(url: str) -> AppServerEndpoint:
    """Parse the listen contract for Focus's owned app-server child."""

    endpoint = parse_app_server_endpoint(url)
    try:
        address = ipaddress.ip_address(endpoint.host)
    except ValueError as exc:
        raise ValueError(
            "owned app-server listen URL 必须使用 loopback IP，不能使用主机名"
        ) from exc
    if (
        endpoint.scheme != "ws"
        or not address.is_loopback
        or endpoint.path
    ):
        raise ValueError(
            "owned app-server listen URL 必须是无路径的 ws://loopback-IP:port"
        )
    return endpoint


def parse_owned_web_gateway_endpoint(url: str) -> OwnedWebGatewayEndpoint:
    """Parse the exact browser origin shape an owned Gateway may publish."""

    if type(url) is not str:
        raise ValueError("owned Web Gateway endpoint 必须是字符串")
    value = url
    if not value or value != value.strip() or any(
        character.isspace() for character in value
    ):
        raise ValueError("owned Web Gateway endpoint 不能为空或包含空白字符")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"owned Web Gateway endpoint 无效：{url}") from exc
    if parsed.scheme != "http":
        raise ValueError("owned Web Gateway endpoint 仅支持 http")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("owned Web Gateway endpoint 不支持内嵌凭据")
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("owned Web Gateway endpoint 不支持 path/query/fragment")
    host = parsed.hostname
    if host not in {"127.0.0.1", "::1"} or port is None or port <= 0:
        raise ValueError(
            "owned Web Gateway endpoint 必须是带有效端口的 numeric loopback origin"
        )
    endpoint = OwnedWebGatewayEndpoint(host=host, port=port)
    if endpoint.origin != value:
        raise ValueError("owned Web Gateway endpoint 必须使用 canonical origin")
    return endpoint


def parse_trusted_proxy_external_origin(
    value: str,
) -> TrustedProxyExternalOrigin:
    """Parse the exact external HTTPS origin asserted by a trusted proxy."""

    if type(value) is not str:
        raise ValueError("trusted proxy external origin 必须是字符串")
    if not value or value != value.strip() or any(
        character.isspace() for character in value
    ):
        raise ValueError("trusted proxy external origin 不能为空或包含空白字符")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"trusted proxy external origin 无效：{value}") from exc
    if parsed.scheme != "https":
        raise ValueError("trusted proxy external origin 仅支持 https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("trusted proxy external origin 不支持内嵌凭据")
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError(
            "trusted proxy external origin 不支持 path/query/fragment"
        )
    host = parsed.hostname
    if not host or "*" in host or port == 0:
        raise ValueError(
            "trusted proxy external origin 必须包含有效 host"
        )
    normalized_host = host.lower()
    if (
        normalized_host.endswith(".")
        or not normalized_host.isascii()
        or "%" in normalized_host
        or "\\" in normalized_host
    ):
        raise ValueError(
            "trusted proxy external origin 必须使用 canonical ASCII host"
        )
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        address = None
        if (
            len(normalized_host) > 253
            or _CANONICAL_DNS_HOST_PATTERN.fullmatch(normalized_host) is None
            or _IPV4_NUMBER_PATTERN.fullmatch(
                normalized_host.rsplit(".", 1)[-1]
            )
            is not None
        ):
            raise ValueError(
                "trusted proxy external origin 必须使用 canonical DNS host"
            )
    else:
        if (
            normalized_host != address.compressed
            or getattr(address, "ipv4_mapped", None) is not None
        ):
            raise ValueError(
                "trusted proxy external origin 必须使用 canonical IP host"
            )
    if (
        normalized_host == "localhost"
        or normalized_host.endswith(".localhost")
        or (
            address is not None
            and (address.is_loopback or address.is_unspecified)
        )
    ):
        raise ValueError(
            "trusted proxy external origin 不能使用 loopback/wildcard host"
        )
    endpoint = TrustedProxyExternalOrigin(
        host=normalized_host,
        port=None if port in {None, 443} else port,
    )
    if endpoint.origin != value:
        raise ValueError(
            "trusted proxy external origin 必须使用 canonical HTTPS origin"
        )
    return endpoint
