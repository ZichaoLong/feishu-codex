"""Single surface-routing boundary for canonical Codex server requests."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from bot.interaction_auto_resolution import AutoResolutionTiming
from bot.interaction_contract import SHARED_APPROVAL_METHODS
from bot.server_request_contract import (
    ServerRequestIdentity,
    ServerRequestRoutingMode,
)
from bot.server_request_dispatch import (
    ServerRequestDispatchKnownNotCommitted,
    ServerRequestDispatchReceipt,
    ServerRequestSurfaceClaim,
    ServerRequestSurfaceIdentityConflict,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServerRequestSurfaceOffer:
    """Exact canonical request plus its proven routing audience."""

    identity: ServerRequestIdentity
    mode: ServerRequestRoutingMode
    auto_resolution_timing: AutoResolutionTiming | None = None


@dataclass(frozen=True, slots=True)
class ServerRequestSurfaceDispatcherPorts:
    """Required surface effects; none owns canonical routing identity."""

    share_approval: Callable[[ServerRequestIdentity], bool]
    share_desktop_interaction: Callable[[ServerRequestIdentity], bool]
    route_fcodex: Callable[[ServerRequestSurfaceOffer], ServerRequestSurfaceClaim]
    schedule_auto_resolution: Callable[
        [str, bool], AutoResolutionTiming | None
    ]
    route_web: Callable[[ServerRequestSurfaceOffer], ServerRequestSurfaceClaim]
    route_feishu: Callable[[ServerRequestSurfaceOffer], ServerRequestSurfaceClaim]
    web_has_pending: Callable[[str], bool]
    feishu_has_pending: Callable[[str], bool]
    cancel_auto_resolution: Callable[[str, AutoResolutionTiming], bool]


class ServerRequestSurfaceDispatcher:
    """Route shared callbacks to eligible surfaces and other requests to one."""

    def __init__(self, ports: ServerRequestSurfaceDispatcherPorts) -> None:
        self._ports = ports

    def dispatch(
        self,
        identity: ServerRequestIdentity,
    ) -> ServerRequestDispatchReceipt:
        """Return committed/no-effect/unknown without inferring from exceptions."""

        try:
            self._dispatch_once(identity)
        except ServerRequestDispatchKnownNotCommitted as exc:
            logger.info(
                "Server-request dispatch proved no effect: request=%s",
                identity.request_key,
                exc_info=True,
            )
            return ServerRequestDispatchReceipt.known_not_committed(str(exc))
        except ServerRequestSurfaceIdentityConflict as exc:
            logger.error(
                "Server-request surface identity conflict: request=%s reason=%s",
                identity.request_key,
                str(exc),
            )
            return ServerRequestDispatchReceipt.outcome_unknown(str(exc))
        except Exception as exc:
            logger.exception(
                "Server-request dispatch outcome is unknown: request=%s",
                identity.request_key,
            )
            return ServerRequestDispatchReceipt.outcome_unknown(str(exc))
        return ServerRequestDispatchReceipt.committed()

    def _dispatch_once(self, identity: ServerRequestIdentity) -> None:
        if (
            identity.method in SHARED_APPROVAL_METHODS
            and self._ports.share_approval(identity) is True
        ):
            self._dispatch_shared_approval(
                ServerRequestSurfaceOffer(identity, "shared_approval")
            )
            return

        if self._ports.share_desktop_interaction(identity) is True:
            self._dispatch_shared_interaction(identity)
            return

        offer = ServerRequestSurfaceOffer(identity, "single_surface")
        if self._claim_outcome(self._ports.route_fcodex(offer)) == "claimed":
            return

        request_key = identity.request_key
        params = identity.params
        timing = self._ports.schedule_auto_resolution(
            request_key,
            identity.method == "item/tool/requestUserInput"
            and "autoResolutionMs" in params
            and params.get("autoResolutionMs") is not None,
        )
        try:
            offer = ServerRequestSurfaceOffer(
                identity,
                "single_surface",
                auto_resolution_timing=timing,
            )
            claim = self._ports.route_web(offer)
            if self._claim_outcome(claim) == "declined":
                claim = self._ports.route_feishu(offer)
        except Exception:
            # The routing outcome may be unknown, but this exact schedule is
            # always safe to revoke.  It must not later manufacture a response
            # for an unreconciled surface effect.
            if timing is not None:
                self._ports.cancel_auto_resolution(request_key, timing)
            raise
        if (
            timing is not None
            and not self._ports.web_has_pending(request_key)
            and not self._ports.feishu_has_pending(request_key)
        ):
            self._ports.cancel_auto_resolution(request_key, timing)
        if self._claim_outcome(claim) != "claimed":
            raise ServerRequestDispatchKnownNotCommitted(
                "no surface retained the server request"
            )

    def _dispatch_shared_approval(
        self,
        offer: ServerRequestSurfaceOffer,
    ) -> None:
        claimed = False
        unknown: list[tuple[str, Exception]] = []
        for surface, route in (
            ("fcodex", self._ports.route_fcodex),
            ("web", self._ports.route_web),
            ("feishu", self._ports.route_feishu),
        ):
            try:
                outcome = self._claim_outcome(route(offer))
            except ServerRequestDispatchKnownNotCommitted:
                continue
            except Exception as exc:
                unknown.append((surface, exc))
                continue
            claimed = claimed or outcome == "claimed"
        if unknown:
            detail = "; ".join(
                f"{surface}: {type(exc).__name__}: {exc}"
                for surface, exc in unknown
            )
            if any(
                isinstance(exc, ServerRequestSurfaceIdentityConflict)
                for _surface, exc in unknown
            ):
                raise ServerRequestSurfaceIdentityConflict(detail)
            raise RuntimeError(
                f"shared approval surface outcome is unknown ({detail})"
            )
        if not claimed:
            raise ServerRequestDispatchKnownNotCommitted(
                "no surface retained the shared approval request"
            )

    def _dispatch_shared_interaction(
        self,
        identity: ServerRequestIdentity,
    ) -> None:
        """Offer ordinary callbacks to both desktop surfaces before Feishu."""

        request_key = identity.request_key
        params = identity.params
        timing = self._ports.schedule_auto_resolution(
            request_key,
            identity.method == "item/tool/requestUserInput"
            and "autoResolutionMs" in params
            and params.get("autoResolutionMs") is not None,
        )
        offer = ServerRequestSurfaceOffer(
            identity,
            "shared_interaction",
            auto_resolution_timing=timing,
        )
        claimed = False
        unknown: list[tuple[str, Exception]] = []
        for surface, route in (
            ("fcodex", self._ports.route_fcodex),
            ("web", self._ports.route_web),
        ):
            try:
                outcome = self._claim_outcome(route(offer))
            except ServerRequestDispatchKnownNotCommitted:
                outcome = "declined"
            except Exception as exc:
                unknown.append((surface, exc))
                continue
            claimed = claimed or outcome == "claimed"

        if unknown:
            # A known-retained Web projection owns this exact timer even when
            # the sibling desktop route has an unknown delivery outcome. Only
            # revoke the schedule when no timer-bearing local projection can
            # still consume it.
            if (
                timing is not None
                and not self._ports.web_has_pending(request_key)
                and not self._ports.feishu_has_pending(request_key)
            ):
                self._ports.cancel_auto_resolution(request_key, timing)
            detail = "; ".join(
                f"{surface}: {type(exc).__name__}: {exc}"
                for surface, exc in unknown
            )
            if any(
                isinstance(exc, ServerRequestSurfaceIdentityConflict)
                for _surface, exc in unknown
            ):
                raise ServerRequestSurfaceIdentityConflict(detail)
            raise RuntimeError(
                f"shared interaction surface outcome is unknown ({detail})"
            )

        if not claimed:
            feishu_offer = ServerRequestSurfaceOffer(
                identity,
                "single_surface",
                auto_resolution_timing=timing,
            )
            try:
                claimed = (
                    self._claim_outcome(self._ports.route_feishu(feishu_offer))
                    == "claimed"
                )
            except Exception:
                if timing is not None:
                    self._ports.cancel_auto_resolution(request_key, timing)
                raise

        if (
            timing is not None
            and not self._ports.web_has_pending(request_key)
            and not self._ports.feishu_has_pending(request_key)
        ):
            self._ports.cancel_auto_resolution(request_key, timing)
        if not claimed:
            raise ServerRequestDispatchKnownNotCommitted(
                "no surface retained the shared interaction request"
            )

    @staticmethod
    def _claim_outcome(claim: ServerRequestSurfaceClaim) -> str:
        if not isinstance(claim, ServerRequestSurfaceClaim) or claim.outcome not in {
            "claimed",
            "declined",
        }:
            raise RuntimeError("surface returned an invalid server-request claim")
        return claim.outcome
