"""HTTP request admission and response hardening for the Focus Web Gateway.

The process-local bootstrap/session mutable fact remains owned by
``WebAuthManager``.  This owner only applies that fact to one aiohttp request;
document, socket, writer, and lifecycle authority remain with ``WebGateway``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web

from bot.instance_layout import DEFAULT_INSTANCE_NAME, validate_instance_name
from bot.network_contract import (
    FOCUS_LOOPBACK_HOSTS,
    TrustedProxyExternalOrigin,
    parse_trusted_proxy_external_origin,
)
from bot.web_runtime.auth import (
    LOCAL_WEB_AUTH_AUDIENCE,
    WebAuthAudience,
    WebAuthManager,
    WebAuthSession,
)
from bot.web_runtime.contract import WebRuntimeError

_SESSION_COOKIE_PREFIX = "focus_web_session_"
_CSRF_HEADER = "X-Focus-Web-Csrf"
_TRUSTED_PROXY_PROOF_HEADER = "X-Focus-Trusted-Proxy-Proof"
_TRUSTED_PROXY_IDENTITY_HEADER = "X-Focus-Trusted-Proxy-Identity"
_PUBLIC_API_PATHS = frozenset({"/api/health", "/api/auth/bootstrap"})
_TRUSTED_PROXY_PROOF_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_TRUSTED_PROXY_IDENTITY_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,127}\Z"
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")

# aiohttp added the request-specific typed key after the project's declared
# minimum version. Prefer it when available; AppKey remains supported there.
_REQUEST_KEY_FACTORY = getattr(web, "RequestKey", web.AppKey)
_AUTH_SESSION_KEY = _REQUEST_KEY_FACTORY("focus_web_session", WebAuthSession)
_AUTH_AUDIENCE_KEY = _REQUEST_KEY_FACTORY("focus_web_audience", WebAuthAudience)


class WebGatewayRequestAdmission:
    """Apply Host, Origin, session, CSRF, cookie, and response policy."""

    def __init__(
        self,
        *,
        auth: WebAuthManager,
        logger: logging.Logger,
        instance_name: str = DEFAULT_INSTANCE_NAME,
        trusted_proxy_origin: str = "",
        trusted_proxy_proof_sha256: str = "",
    ) -> None:
        self._auth = auth
        self._logger = logger
        normalized_instance_name = validate_instance_name(instance_name)
        if normalized_instance_name != instance_name:
            raise ValueError("instance name must already be canonical")
        self._session_cookie_name = (
            f"{_SESSION_COOKIE_PREFIX}{normalized_instance_name}"
        )
        if type(trusted_proxy_origin) is not str:
            raise ValueError("trusted proxy origin must be a string")
        if type(trusted_proxy_proof_sha256) is not str:
            raise ValueError("trusted proxy proof verifier must be a string")
        origin = trusted_proxy_origin
        proof_sha256 = trusted_proxy_proof_sha256
        if bool(origin) != bool(proof_sha256):
            raise ValueError(
                "trusted proxy origin and proof verifier must be configured together"
            )
        if proof_sha256 and _SHA256_PATTERN.fullmatch(proof_sha256) is None:
            raise ValueError("trusted proxy proof verifier must be lowercase SHA-256")
        self._trusted_proxy_origin: TrustedProxyExternalOrigin | None = (
            parse_trusted_proxy_external_origin(origin) if origin else None
        )
        self._trusted_proxy_proof_sha256 = proof_sha256

    @web.middleware
    async def error_middleware(self, request: web.Request, handler):
        try:
            return await handler(request)
        except WebRuntimeError as exc:
            error_payload = {"code": exc.code, "message": str(exc)}
            if exc.details:
                error_payload["details"] = dict(exc.details)
            response = web.json_response(
                {"error": error_payload},
                status=exc.status,
            )
            if request.path == "/api/backend-reset":
                response.headers["Cache-Control"] = "no-store"
            return response
        except web.HTTPException:
            raise
        except Exception:
            self._logger.exception(
                "Focus Web request failed: %s %s",
                request.method,
                request.path,
            )
            response = web.json_response(
                {
                    "error": {
                        "code": "internal_error",
                        "message": "Internal server error.",
                    }
                },
                status=500,
            )
            if request.path == "/api/backend-reset":
                response.headers["Cache-Control"] = "no-store"
            return response

    @web.middleware
    async def security_middleware(self, request: web.Request, handler):
        request[_AUTH_AUDIENCE_KEY] = self._admit_request_audience(request)
        response = await handler(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "object-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; media-src 'self' data: blob:; "
            "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:"
        )
        return response

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler):
        if request.path in _PUBLIC_API_PATHS or not request.path.startswith("/api/"):
            return await handler(request)
        audience = self.audience(request)
        session = self._auth.authenticate(self.session_cookie_token(request))
        if session is None or session.audience != audience:
            if (
                request.method == "POST"
                and request.path == "/api/client/register"
                and audience.kind == "external"
            ):
                self.validate_origin(request)
                external_session = self._auth.issue_external_session(
                    external_origin=audience.external_origin,
                    proxy_identity=audience.proxy_identity,
                )
                if external_session is None:
                    raise web.HTTPTooManyRequests(
                        text=(
                            '{"error":{"code":"external_session_limit",'
                            '"message":"Too many external Web sessions."}}'
                        ),
                        content_type="application/json",
                    )
                request[_AUTH_SESSION_KEY] = external_session
                try:
                    response = await handler(request)
                except BaseException:
                    self._auth.revoke(external_session.session_token)
                    raise
                if response.status >= 400:
                    self._auth.revoke(external_session.session_token)
                    return response
                self.set_session_cookie(response, request, external_session)
                return response
            unauthorized_text = (
                '{"error":{"code":"unauthorized",'
                '"message":"Reload the configured external Focus Web origin."}}'
                if audience.kind == "external"
                else (
                    '{"error":{"code":"unauthorized",'
                    '"message":"Run focusctl web open again."}}'
                )
            )
            raise web.HTTPUnauthorized(
                text=unauthorized_text,
                content_type="application/json",
            )
        request[_AUTH_SESSION_KEY] = session
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            self.validate_origin(request)
            # Registration is the one session-authenticated, same-origin
            # mutation that cannot require the CSRF token it is creating.
            if request.path == "/api/client/register":
                return await handler(request)
            csrf_token = str(request.headers.get(_CSRF_HEADER, "") or "").strip()
            if not csrf_token or not secrets.compare_digest(
                csrf_token,
                session.csrf_token,
            ):
                raise web.HTTPForbidden(
                    text=(
                        '{"error":{"code":"csrf_failed",'
                        '"message":"Invalid CSRF token."}}'
                    ),
                    content_type="application/json",
                )
        return await handler(request)

    @staticmethod
    def session(request: web.Request) -> WebAuthSession:
        session = request.get(_AUTH_SESSION_KEY)
        if not isinstance(session, WebAuthSession):
            raise web.HTTPUnauthorized()
        return session

    @staticmethod
    def audience(request: web.Request) -> WebAuthAudience:
        audience = request.get(_AUTH_AUDIENCE_KEY)
        if not isinstance(audience, WebAuthAudience):
            raise web.HTTPBadRequest(text="Invalid Web request audience.")
        return audience

    def require_local_audience(self, request: web.Request) -> None:
        if self.audience(request) != LOCAL_WEB_AUTH_AUDIENCE:
            raise web.HTTPForbidden(
                text="The local bootstrap cannot authenticate an external request."
            )

    def session_cookie_token(self, request: web.Request) -> str:
        return str(
            request.cookies.get(self._session_cookie_name, "") or ""
        ).strip()

    def set_session_cookie(
        self,
        response: web.StreamResponse,
        request: web.Request,
        session: WebAuthSession,
    ) -> None:
        response.set_cookie(
            self._session_cookie_name,
            session.session_token,
            httponly=True,
            secure=request.secure or session.audience.kind == "external",
            samesite="Strict",
            path="/",
            max_age=max(int(session.expires_at - time.time()), 60),
        )

    def clear_session_cookie(self, response: web.StreamResponse) -> None:
        response.del_cookie(self._session_cookie_name, path="/")

    @staticmethod
    def validate_websocket_csrf(
        session: WebAuthSession,
        presented_token: str,
    ) -> None:
        token = str(presented_token or "").strip()
        if not token or not secrets.compare_digest(token, session.csrf_token):
            raise web.HTTPForbidden(text="Invalid WebSocket session token.")

    def _admit_request_audience(self, request: web.Request) -> WebAuthAudience:
        request_host = str(request.host or "")
        try:
            host, _port = self._authority(request_host, request.scheme)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid Host header.") from None
        proof = str(request.headers.get(_TRUSTED_PROXY_PROOF_HEADER, "") or "")
        identity = str(
            request.headers.get(_TRUSTED_PROXY_IDENTITY_HEADER, "") or ""
        )
        if host in FOCUS_LOOPBACK_HOSTS:
            if proof or identity:
                raise web.HTTPBadRequest(
                    text="Trusted proxy headers are not accepted on local requests."
                )
            return LOCAL_WEB_AUTH_AUDIENCE

        external_origin = self._trusted_proxy_origin
        if external_origin is None:
            raise web.HTTPBadRequest(
                text="Focus Web only accepts loopback Host headers."
            )
        if request_host != external_origin.authority:
            raise web.HTTPBadRequest(text="Invalid external Host header.")
        if _TRUSTED_PROXY_PROOF_PATTERN.fullmatch(proof) is None:
            raise web.HTTPForbidden(text="Invalid trusted proxy proof.")
        proof_sha256 = hashlib.sha256(proof.encode("ascii")).hexdigest()
        if not secrets.compare_digest(
            proof_sha256,
            self._trusted_proxy_proof_sha256,
        ):
            raise web.HTTPForbidden(text="Invalid trusted proxy proof.")
        if _TRUSTED_PROXY_IDENTITY_PATTERN.fullmatch(identity) is None:
            raise web.HTTPForbidden(text="Invalid trusted proxy identity.")
        return WebAuthAudience(
            kind="external",
            external_origin=external_origin.origin,
            proxy_identity=identity,
        )

    def validate_origin(
        self,
        request: web.Request,
        *,
        allow_missing: bool = False,
    ) -> None:
        origin = str(request.headers.get("Origin", "") or "").strip()
        if not origin:
            if allow_missing:
                return
            raise web.HTTPForbidden(text="Missing Origin header.")
        audience = self.audience(request)
        if audience.kind == "external":
            if origin != audience.external_origin:
                raise web.HTTPForbidden(
                    text="Cross-origin requests are not allowed."
                )
            return
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed.")
        try:
            origin_authority = self._authority(parsed.netloc, parsed.scheme)
            request_authority = self._authority(request.host, request.scheme)
        except ValueError:
            raise web.HTTPForbidden(
                text="Cross-origin requests are not allowed."
            ) from None
        if parsed.scheme != request.scheme or origin_authority != request_authority:
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed.")

    @staticmethod
    def _authority(value: str, scheme: str) -> tuple[str, int]:
        raw = str(value or "")
        if (
            not raw
            or raw != raw.strip()
            or any(character.isspace() for character in raw)
        ):
            raise ValueError("invalid authority")
        parsed = urlsplit(f"//{raw}")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid authority")
        host = (parsed.hostname or "").lower()
        if not host:
            raise ValueError("missing host")
        default_port = 443 if scheme == "https" else 80
        return host, parsed.port or default_port
