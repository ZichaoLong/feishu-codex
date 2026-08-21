"""Connection-local upstream thread settings and active-turn snapshots.

The registry accepts only authoritative start/resume responses and complete
``thread/settings/updated`` notifications.  Requests and queued-operation ACKs
remain intent.  A turn freezes the then-current thread base, while an exact
matching model reroute may override only that active turn's model.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Literal

from bot.runtime_state import BACKEND_THREAD_STATUS_NOT_LOADED


EffectiveSettingsSource = Literal["thread_start", "thread_resume", "settings"]
EffectiveSettingDisclosureSource = Literal[
    "active_reroute",
    "inherited",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class EffectiveSettingFact:
    """One upstream field, preserving unknown separately from known null."""

    known: bool
    value: str | None = None


_UNKNOWN_SETTING = EffectiveSettingFact(known=False)


@dataclass(frozen=True, slots=True)
class ThreadEffectiveSettingsFact:
    model: EffectiveSettingFact
    reasoning_effort: EffectiveSettingFact
    approval_policy: EffectiveSettingFact
    permissions_profile_id: EffectiveSettingFact
    source: EffectiveSettingsSource


@dataclass(frozen=True, slots=True)
class ActiveTurnSettingsFact:
    turn_id: str
    settings: ThreadEffectiveSettingsFact


@dataclass(frozen=True, slots=True)
class ActiveModelRerouteFact:
    turn_id: str
    model: str


@dataclass(frozen=True, slots=True)
class EffectiveSettingDisclosure:
    value: str
    source: EffectiveSettingDisclosureSource


@dataclass(frozen=True, slots=True)
class ThreadEffectiveSettingsDisclosure:
    model: EffectiveSettingDisclosure
    reasoning_effort: EffectiveSettingDisclosure
    approval_policy: EffectiveSettingDisclosure
    permissions_profile_id: EffectiveSettingDisclosure


def _event_string(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        return ""
    return value


def _known_required_string(value: object, *, field: str) -> EffectiveSettingFact:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    return EffectiveSettingFact(known=True, value=value)


def _known_nullable_string(value: object, *, field: str) -> EffectiveSettingFact:
    if value is None:
        return EffectiveSettingFact(known=True, value=None)
    return _known_required_string(value, field=field)


def _optional_string(value: object, *, field: str) -> EffectiveSettingFact:
    if value is None:
        return _UNKNOWN_SETTING
    return _known_required_string(value, field=field)


def _permission_profile(value: object) -> EffectiveSettingFact:
    if value is None:
        return _UNKNOWN_SETTING
    if not isinstance(value, dict):
        raise ValueError("activePermissionProfile must be null or an object")
    return _known_required_string(value.get("id"), field="activePermissionProfile.id")


def _unknown_settings(*, source: EffectiveSettingsSource) -> ThreadEffectiveSettingsFact:
    return ThreadEffectiveSettingsFact(
        model=_UNKNOWN_SETTING,
        reasoning_effort=_UNKNOWN_SETTING,
        approval_policy=_UNKNOWN_SETTING,
        permissions_profile_id=_UNKNOWN_SETTING,
        source=source,
    )


class ThreadEffectiveSettingsRegistry:
    """Sole connection-local owner of upstream effective-setting facts."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread_base: dict[str, ThreadEffectiveSettingsFact] = {}
        self._active_turn: dict[str, ActiveTurnSettingsFact] = {}
        self._model_reroute: dict[str, ActiveModelRerouteFact] = {}
        self._external_unknown: set[str] = set()

    def record_start_or_resume(
        self,
        thread_id: object,
        *,
        model: object,
        reasoning_effort: object,
        approval_policy: object,
        permissions_profile_id: object,
        source: Literal["thread_start", "thread_resume"],
    ) -> None:
        """Replace the thread base without disturbing an already active turn."""

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id must be non-empty")
        settings = ThreadEffectiveSettingsFact(
            model=_known_required_string(model, field="model"),
            reasoning_effort=_known_nullable_string(
                reasoning_effort,
                field="reasoningEffort",
            ),
            approval_policy=_optional_string(
                approval_policy,
                field="approvalPolicy",
            ),
            permissions_profile_id=_optional_string(
                permissions_profile_id,
                field="activePermissionProfile.id",
            ),
            source=source,
        )
        with self._lock:
            if normalized_thread_id not in self._external_unknown:
                self._thread_base[normalized_thread_id] = settings

    def mark_external_unknown(self, thread_id: object) -> bool:
        """Retire one thread until the canonical backend epoch is replaced.

        A mutation sent through another app-server connection has no ordering
        relationship Focus can prove against notifications arriving on the
        canonical adapter connection.  Keep the exact thread unknown instead
        of allowing a delayed canonical notification to reinstall stale facts.
        """

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return False
        with self._lock:
            changed = normalized_thread_id not in self._external_unknown
            self._external_unknown.add(normalized_thread_id)
            base = self._thread_base.pop(normalized_thread_id, None)
            active = self._active_turn.pop(normalized_thread_id, None)
            reroute = self._model_reroute.pop(normalized_thread_id, None)
            changed = bool(base is not None or active is not None or reroute is not None or changed)
            return changed

    def invalidate_requested_settings_if_different(
        self,
        thread_id: object,
        *,
        model: object = None,
        reasoning_effort: object = None,
        approval_policy: object = None,
        permissions_profile_id: object = None,
    ) -> bool:
        """Invalidate explicit turn-setting fields across base and active scope."""

        normalized_thread_id = str(thread_id or "").strip()
        requested = self._requested_fields(
            model=model,
            reasoning_effort=reasoning_effort,
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id,
        )
        if not normalized_thread_id or not requested:
            return False
        with self._lock:
            changed = self._invalidate_base_fields_locked(
                normalized_thread_id,
                requested,
            )
            active = self._active_turn.get(normalized_thread_id)
            if active is not None:
                replacements = {
                    name: _UNKNOWN_SETTING
                    for name, value in requested.items()
                    if (fact := getattr(active.settings, name)).known
                    and fact.value != value
                }
                if replacements:
                    self._active_turn[normalized_thread_id] = replace(
                        active,
                        settings=replace(active.settings, **replacements),
                    )
                    changed = True
            requested_model = requested.get("model")
            reroute = self._model_reroute.get(normalized_thread_id)
            if (
                requested_model is not None
                and reroute is not None
                and reroute.model != requested_model
            ):
                active = self._active_turn.get(normalized_thread_id)
                if active is not None and active.turn_id == reroute.turn_id:
                    self._active_turn[normalized_thread_id] = replace(
                        active,
                        settings=replace(
                            active.settings,
                            model=_UNKNOWN_SETTING,
                        ),
                    )
                self._model_reroute.pop(normalized_thread_id, None)
                changed = True
            return changed

    def invalidate_thread_base_if_requested_settings_differ(
        self,
        thread_id: object,
        *,
        model: object = None,
        reasoning_effort: object = None,
        approval_policy: object = None,
        permissions_profile_id: object = None,
    ) -> bool:
        """Invalidate explicit future-turn fields before a settings ACK."""

        normalized_thread_id = str(thread_id or "").strip()
        requested = self._requested_fields(
            model=model,
            reasoning_effort=reasoning_effort,
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id,
        )
        if not normalized_thread_id or not requested:
            return False
        with self._lock:
            return self._invalidate_base_fields_locked(
                normalized_thread_id,
                requested,
            )

    def observe_notification(self, method: object, params: object) -> None:
        event = str(method or "").strip()
        payload = params if isinstance(params, dict) else {}
        thread_id = _event_string(payload.get("threadId"))
        if not thread_id:
            return
        with self._lock:
            if thread_id in self._external_unknown:
                # The other connection is deliberately not a value writer.
                # Cross-connection delivery has no revision or causal token,
                # so even a well-shaped canonical notification cannot retire
                # this negative fact. Only replacement of the canonical
                # backend epoch supplies a bounded refresh boundary.
                self._thread_base.pop(thread_id, None)
                self._active_turn.pop(thread_id, None)
                self._model_reroute.pop(thread_id, None)
                return
        if event == "thread/settings/updated":
            settings = self._settings_from_notification(payload.get("threadSettings"))
            if settings is None:
                settings = _unknown_settings(source="settings")
            with self._lock:
                if thread_id in self._external_unknown:
                    return
                self._thread_base[thread_id] = settings
            return
        if event == "turn/started":
            turn = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
            turn_id = _event_string(turn.get("id", payload.get("turnId")))
            if not turn_id:
                self._clear_active_turn(thread_id)
                return
            with self._lock:
                if thread_id in self._external_unknown:
                    return
                existing = self._active_turn.get(thread_id)
                if existing is not None and existing.turn_id == turn_id:
                    return
                frozen = self._thread_base.get(thread_id)
                if frozen is None:
                    frozen = _unknown_settings(source="settings")
                self._active_turn[thread_id] = ActiveTurnSettingsFact(
                    turn_id=turn_id,
                    settings=frozen,
                )
                self._model_reroute.pop(thread_id, None)
            return
        if event == "model/rerouted":
            turn_id = _event_string(payload.get("turnId"))
            if not turn_id:
                with self._lock:
                    if thread_id in self._external_unknown:
                        return
                    active = self._active_turn.get(thread_id)
                    if active is not None:
                        self._active_turn[thread_id] = replace(
                            active,
                            settings=replace(
                                active.settings,
                                model=_UNKNOWN_SETTING,
                            ),
                        )
                    self._model_reroute.pop(thread_id, None)
                return
            model = _event_string(payload.get("toModel"))
            with self._lock:
                if thread_id in self._external_unknown:
                    return
                active = self._active_turn.get(thread_id)
                if active is not None and active.turn_id == turn_id:
                    if model:
                        self._model_reroute[thread_id] = ActiveModelRerouteFact(
                            turn_id=turn_id,
                            model=model,
                        )
                    else:
                        self._active_turn[thread_id] = replace(
                            active,
                            settings=replace(
                                active.settings,
                                model=_UNKNOWN_SETTING,
                            ),
                        )
                        self._model_reroute.pop(thread_id, None)
            return
        if event == "turn/completed":
            turn = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
            turn_id = _event_string(turn.get("id", payload.get("turnId")))
            if not turn_id:
                self._clear_active_turn(thread_id)
                return
            with self._lock:
                active = self._active_turn.get(thread_id)
                if active is not None and active.turn_id == turn_id:
                    self._active_turn.pop(thread_id, None)
                    self._model_reroute.pop(thread_id, None)
            return
        if event == "thread/status/changed":
            status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
            status_type = str(status.get("type", "") or "").strip()
            if not status_type:
                self._clear_active_turn(thread_id)
                return
            if status_type == BACKEND_THREAD_STATUS_NOT_LOADED:
                self.clear_thread(thread_id)
            elif status_type != "active":
                self._clear_active_turn(thread_id)
            return
        if event in {"thread/closed", "thread/archived", "thread/deleted"}:
            self.clear_thread(thread_id)

    def resolve_model_for_request(
        self,
        thread_id: object,
        *,
        requested_model: object = "",
    ) -> str | None:
        normalized_thread_id = str(thread_id or "").strip()
        requested = str(requested_model or "").strip()
        if not normalized_thread_id:
            return None
        with self._lock:
            model = self._current_model_locked(normalized_thread_id)
            if not model.known or model.value is None:
                return None
            if requested and requested != model.value:
                return None
            return model.value

    def disclosure_for_active_turn(
        self,
        thread_id: object,
        turn_id: object,
    ) -> ThreadEffectiveSettingsDisclosure:
        normalized_thread_id = str(thread_id or "").strip()
        normalized_turn_id = str(turn_id or "").strip()
        with self._lock:
            active = self._active_turn.get(normalized_thread_id)
            if (
                not normalized_thread_id
                or not normalized_turn_id
                or active is None
                or active.turn_id != normalized_turn_id
            ):
                return self._unknown_disclosure()
            model = self._disclose(active.settings.model)
            reroute = self._model_reroute.get(normalized_thread_id)
            if reroute is not None and reroute.turn_id == normalized_turn_id:
                model = EffectiveSettingDisclosure(
                    value=reroute.model,
                    source="active_reroute",
                )
            return ThreadEffectiveSettingsDisclosure(
                model=model,
                reasoning_effort=self._disclose(active.settings.reasoning_effort),
                approval_policy=self._disclose(active.settings.approval_policy),
                permissions_profile_id=self._disclose(
                    active.settings.permissions_profile_id
                ),
            )

    @staticmethod
    def _settings_from_notification(value: object) -> ThreadEffectiveSettingsFact | None:
        if not isinstance(value, dict):
            return None
        required = {"model", "effort", "approvalPolicy", "activePermissionProfile"}
        if not required.issubset(value):
            return None
        try:
            return ThreadEffectiveSettingsFact(
                model=_known_required_string(value["model"], field="model"),
                reasoning_effort=_known_nullable_string(
                    value["effort"],
                    field="effort",
                ),
                approval_policy=_known_required_string(
                    value["approvalPolicy"],
                    field="approvalPolicy",
                ),
                permissions_profile_id=_permission_profile(
                    value["activePermissionProfile"]
                ),
                source="settings",
            )
        except ValueError:
            return None

    @staticmethod
    def _requested_fields(
        *,
        model: object,
        reasoning_effort: object,
        approval_policy: object,
        permissions_profile_id: object,
    ) -> dict[str, str]:
        requested: dict[str, str] = {}
        for name, value in (
            ("model", model),
            ("reasoning_effort", reasoning_effort),
            ("approval_policy", approval_policy),
            ("permissions_profile_id", permissions_profile_id),
        ):
            normalized = str(value or "").strip()
            if normalized:
                requested[name] = normalized
        return requested

    def _invalidate_base_fields_locked(
        self,
        thread_id: str,
        requested: dict[str, str],
    ) -> bool:
        base = self._thread_base.get(thread_id)
        if base is None:
            return False
        replacements = {
            name: _UNKNOWN_SETTING
            for name, value in requested.items()
            if (fact := getattr(base, name)).known and fact.value != value
        }
        if not replacements:
            return False
        self._thread_base[thread_id] = replace(base, **replacements)
        return True

    def _current_model_locked(self, thread_id: str) -> EffectiveSettingFact:
        active = self._active_turn.get(thread_id)
        if active is not None:
            reroute = self._model_reroute.get(thread_id)
            if reroute is not None and reroute.turn_id == active.turn_id:
                return EffectiveSettingFact(known=True, value=reroute.model)
            return active.settings.model
        base = self._thread_base.get(thread_id)
        return base.model if base is not None else _UNKNOWN_SETTING

    @staticmethod
    def _disclose(fact: EffectiveSettingFact) -> EffectiveSettingDisclosure:
        if not fact.known:
            return EffectiveSettingDisclosure(value="", source="unknown")
        return EffectiveSettingDisclosure(value=fact.value or "", source="inherited")

    @classmethod
    def _unknown_disclosure(cls) -> ThreadEffectiveSettingsDisclosure:
        unknown = cls._disclose(_UNKNOWN_SETTING)
        return ThreadEffectiveSettingsDisclosure(
            model=unknown,
            reasoning_effort=unknown,
            approval_policy=unknown,
            permissions_profile_id=unknown,
        )

    def _clear_active_turn(self, thread_id: str) -> None:
        with self._lock:
            self._active_turn.pop(thread_id, None)
            self._model_reroute.pop(thread_id, None)

    def clear_thread(self, thread_id: object) -> None:
        """Clear positive facts without weakening an external-unknown marker."""

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return
        with self._lock:
            self._thread_base.pop(normalized_thread_id, None)
            self._active_turn.pop(normalized_thread_id, None)
            self._model_reroute.pop(normalized_thread_id, None)

    def clear_all(self) -> None:
        """Replace the canonical backend epoch and discard every local fact."""

        with self._lock:
            self._thread_base.clear()
            self._active_turn.clear()
            self._model_reroute.clear()
            self._external_unknown.clear()
