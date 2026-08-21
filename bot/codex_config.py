"""Strict schema for the instance-local ``codex.yaml`` component config.

The YAML loader deliberately only loads syntax.  This module owns the next
boundary: the complete accepted key inventory, defaults, type checks, and the
small amount of value normalization that belongs to Focus rather than to the
upstream app-server.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from typing import Any, Mapping

from bot.approval_policy import SUPPORTED_APPROVAL_POLICIES, normalize_approval_policy
from bot.constants import (
    DEFAULT_APP_SERVER_URL,
    DEFAULT_HISTORY_PREVIEW_ROUNDS,
    DEFAULT_SOURCE_KINDS,
    DEFAULT_STREAM_PATCH_INTERVAL_MS,
    DEFAULT_THREAD_LIST_QUERY_LIMIT,
    DEFAULT_THREADS_INITIAL_LIMIT,
)
from bot.network_contract import (
    FOCUS_LOOPBACK_HOSTS,
    MIN_WEB_SESSION_TTL_SECONDS,
    parse_owned_app_server_listen_endpoint,
    parse_trusted_proxy_external_origin,
)
from bot.permissions_profile import (
    BUILTIN_PERMISSION_PROFILE_IDS,
    BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS,
)


_REMOVED_KEYS: dict[str, str] = {
    "app_server_mode": (
        "`app_server_mode` 已移除；Focus service 现在始终拉起并拥有本机 app-server。"
        "请删除该键；连接本实例已运行 backend 的 focusctl/fcodex 内部链路不受影响。"
    ),
    "managed_startup_profile": (
        "`managed_startup_profile` 已移除；本项目不再提供 profile 启动基线功能。"
        "如需使用 profile，请直接使用上游 Codex 自己的配置或启动参数。"
    ),
    "default_thread_memory_mode": "`default_thread_memory_mode` 已移除；请改用上游 Codex memories 配置。",
    "new_thread_memory_mode_seed": "`new_thread_memory_mode_seed` 已移除；请改用上游 Codex memories 配置。",
    "collaboration_mode": "`collaboration_mode` 已移除；如需使用 collaboration mode，请改用上游 Codex 配置。",
    "model_provider": "`model_provider` 已移除；请改用上游 Codex 配置或显式调用方 provider hint。",
    "permissions": "`permissions` 不是 codex.yaml 配置键；请使用 `permissions_profile_id`。",
    "sandbox": "`sandbox` 不是 codex.yaml 配置键；请使用 `permissions_profile_id`。",
}

SUPPORTED_PERSONALITIES = frozenset({"friendly", "pragmatic", "none"})


def _invalid_type(key: str, expected: str, value: object) -> ValueError:
    return ValueError(
        f"codex.yaml 配置 `{key}` 必须是 {expected}，实际为 {type(value).__name__}"
    )


def _string(
    config: Mapping[str, Any],
    key: str,
    default: str,
    *,
    nonempty: bool = False,
) -> str:
    if key not in config:
        return default
    value = config[key]
    if not isinstance(value, str):
        raise _invalid_type(key, "字符串", value)
    normalized = value.strip()
    if nonempty and not normalized:
        raise ValueError(f"codex.yaml 配置 `{key}` 不能为空")
    return normalized


def _exact_string(
    config: Mapping[str, Any],
    key: str,
    default: str,
) -> str:
    if key not in config:
        return default
    value = config[key]
    if not isinstance(value, str):
        raise _invalid_type(key, "字符串", value)
    return value


def _boolean(config: Mapping[str, Any], key: str, default: bool) -> bool:
    if key not in config:
        return default
    value = config[key]
    if type(value) is not bool:
        raise _invalid_type(key, "布尔值 true/false", value)
    return value


def _integer(
    config: Mapping[str, Any],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if key not in config:
        return default
    value = config[key]
    if type(value) is not int:
        raise _invalid_type(key, "整数", value)
    if minimum is not None and value < minimum:
        raise ValueError(f"codex.yaml 配置 `{key}` 不能小于 {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"codex.yaml 配置 `{key}` 不能大于 {maximum}")
    return value


def _number(
    config: Mapping[str, Any],
    key: str,
    default: float,
    *,
    minimum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    if key not in config:
        return default
    value = config[key]
    if type(value) not in {int, float}:
        raise _invalid_type(key, "数字", value)
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"codex.yaml 配置 `{key}` 必须是有限数字")
    if minimum is not None:
        if exclusive_minimum and parsed <= minimum:
            raise ValueError(f"codex.yaml 配置 `{key}` 必须大于 {minimum:g}")
        if not exclusive_minimum and parsed < minimum:
            raise ValueError(f"codex.yaml 配置 `{key}` 不能小于 {minimum:g}")
    return parsed


def _source_kinds(
    config: Mapping[str, Any],
    default: tuple[str, ...],
) -> tuple[str, ...]:
    if "source_kinds" not in config:
        return default
    value = config["source_kinds"]
    if type(value) is not list:
        raise _invalid_type("source_kinds", "字符串列表", value)
    if not value:
        raise ValueError("codex.yaml 配置 `source_kinds` 不能为空列表")
    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(
                "codex.yaml 配置 `source_kinds` 的每一项都必须是字符串，"
                f"第 {index + 1} 项实际为 {type(item).__name__}"
            )
        normalized_item = item.strip()
        if not normalized_item:
            raise ValueError(f"codex.yaml 配置 `source_kinds` 的第 {index + 1} 项不能为空")
        normalized.append(normalized_item)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class CodexConfig:
    """Validated, typed projection of the complete ``codex.yaml`` surface."""

    default_working_dir: str = ""
    codex_command: str = "codex"
    app_server_url: str = DEFAULT_APP_SERVER_URL
    web_enabled: bool = False
    web_host: str = "127.0.0.1"
    web_port: int = 0
    web_trusted_proxy_origin: str = ""
    web_trusted_proxy_proof_sha256: str = ""
    web_session_ttl_seconds: float = 8 * 60 * 60
    web_disconnect_grace_seconds: float = 15.0
    web_static_dir: str = ""
    connect_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 30.0
    service_name: str = "focus"
    permissions_profile_id: str = BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS
    approval_policy: str = "never"
    approvals_reviewer: str = "user"
    personality: str = "pragmatic"
    model: str = ""
    service_tier: str = ""
    reasoning_effort: str = ""
    source_kinds: tuple[str, ...] = field(default_factory=lambda: tuple(DEFAULT_SOURCE_KINDS))
    threads_initial_limit: int = DEFAULT_THREADS_INITIAL_LIMIT
    thread_list_query_limit: int = DEFAULT_THREAD_LIST_QUERY_LIMIT
    history_preview_rounds: int = DEFAULT_HISTORY_PREVIEW_ROUNDS
    show_history_preview_on_resume: bool = True
    attachment_ttl_seconds: float = 1800.0
    mirror_watchdog_seconds: float = 8.0
    compact_start_timeout_seconds: float = 60.0
    stream_patch_interval_ms: int = DEFAULT_STREAM_PATCH_INTERVAL_MS
    terminal_result_card_limit: int = 26000

    @classmethod
    def accepted_keys(cls) -> frozenset[str]:
        """Return the authoritative accepted-key inventory for projections/tests."""

        return frozenset(item.name for item in fields(cls))

    @classmethod
    def from_dict(cls, config: Mapping[str, Any]) -> "CodexConfig":
        if not isinstance(config, Mapping):
            raise ValueError(
                "codex.yaml 顶层必须是键值映射，"
                f"实际为 {type(config).__name__}"
            )

        for key in config:
            if not isinstance(key, str):
                raise ValueError(
                    "codex.yaml 顶层键必须是字符串，"
                    f"实际包含 {type(key).__name__}"
                )
            removed_message = _REMOVED_KEYS.get(key)
            if removed_message is not None:
                raise ValueError(removed_message)

        unknown_keys = sorted(set(config) - cls.accepted_keys())
        if unknown_keys:
            rendered = "、".join(f"`{key}`" for key in unknown_keys)
            raise ValueError(f"codex.yaml 包含未知配置键：{rendered}")

        defaults = cls()

        approval_policy_raw = _string(
            config,
            "approval_policy",
            defaults.approval_policy,
            nonempty=True,
        )
        if approval_policy_raw.lower() not in SUPPORTED_APPROVAL_POLICIES:
            choices = "、".join(sorted(SUPPORTED_APPROVAL_POLICIES))
            raise ValueError(
                "codex.yaml 配置 `approval_policy` 不受支持："
                f"{approval_policy_raw!r}；可选值为 {choices}"
            )
        approval_policy = normalize_approval_policy(approval_policy_raw)

        permissions_raw = _string(
            config,
            "permissions_profile_id",
            defaults.permissions_profile_id,
            nonempty=True,
        )
        permissions_profile_id = permissions_raw.lower()
        if permissions_profile_id not in BUILTIN_PERMISSION_PROFILE_IDS:
            choices = "、".join(sorted(BUILTIN_PERMISSION_PROFILE_IDS))
            raise ValueError(
                "codex.yaml 配置 `permissions_profile_id` 不受支持："
                f"{permissions_raw!r}；可选值为 {choices}"
            )

        app_server_url = _string(
            config,
            "app_server_url",
            defaults.app_server_url,
            nonempty=True,
        )
        try:
            app_server_endpoint = parse_owned_app_server_listen_endpoint(
                app_server_url
            )
        except ValueError as exc:
            raise ValueError(
                f"codex.yaml 配置 `app_server_url` 无效：{exc}"
            ) from exc
        app_server_url = app_server_endpoint.url

        web_host = _string(
            config,
            "web_host",
            defaults.web_host,
            nonempty=True,
        ).lower()
        if web_host not in FOCUS_LOOPBACK_HOSTS:
            choices = "、".join(sorted(FOCUS_LOOPBACK_HOSTS))
            raise ValueError(
                "codex.yaml 配置 `web_host` 当前仅支持 loopback："
                f"{choices}"
            )
        web_enabled = _boolean(config, "web_enabled", defaults.web_enabled)
        web_port = _integer(
            config, "web_port", defaults.web_port, minimum=0, maximum=65535
        )
        web_trusted_proxy_origin = _exact_string(
            config,
            "web_trusted_proxy_origin",
            defaults.web_trusted_proxy_origin,
        )
        web_trusted_proxy_proof_sha256 = _exact_string(
            config,
            "web_trusted_proxy_proof_sha256",
            defaults.web_trusted_proxy_proof_sha256,
        )
        if bool(web_trusted_proxy_origin) != bool(
            web_trusted_proxy_proof_sha256
        ):
            raise ValueError(
                "codex.yaml 配置 `web_trusted_proxy_origin` 与 "
                "`web_trusted_proxy_proof_sha256` 必须同时为空或同时有值"
            )
        if web_trusted_proxy_origin:
            try:
                external_origin = parse_trusted_proxy_external_origin(
                    web_trusted_proxy_origin
                )
            except ValueError as exc:
                raise ValueError(
                    "codex.yaml 配置 `web_trusted_proxy_origin` 无效："
                    f"{exc}"
                ) from exc
            web_trusted_proxy_origin = external_origin.origin
            if (
                len(web_trusted_proxy_proof_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in web_trusted_proxy_proof_sha256
                )
            ):
                raise ValueError(
                    "codex.yaml 配置 `web_trusted_proxy_proof_sha256` "
                    "必须是 64 位小写十六进制"
                )
            if not web_enabled:
                raise ValueError(
                    "codex.yaml trusted proxy mode 要求 `web_enabled: true`"
                )
            if web_port == 0:
                raise ValueError(
                    "codex.yaml trusted proxy mode 要求 `web_port` 为固定非零端口"
                )
        approvals_reviewer = _string(
            config,
            "approvals_reviewer",
            defaults.approvals_reviewer,
            nonempty=True,
        ).lower()
        if approvals_reviewer != "user":
            raise ValueError(
                "codex.yaml 配置 `approvals_reviewer` 当前仅支持 user；"
                "Focus 尚未建立 auto-review 的交互与安全合同"
            )
        personality = _string(
            config,
            "personality",
            defaults.personality,
            nonempty=True,
        ).lower()
        if personality not in SUPPORTED_PERSONALITIES:
            choices = "、".join(sorted(SUPPORTED_PERSONALITIES))
            raise ValueError(
                "codex.yaml 配置 `personality` 不受当前 Codex app-server 支持："
                f"{personality!r}；可选值为 {choices}"
            )

        return cls(
            default_working_dir=_string(
                config, "default_working_dir", defaults.default_working_dir
            ),
            codex_command=_string(
                config, "codex_command", defaults.codex_command, nonempty=True
            ),
            app_server_url=app_server_url,
            web_enabled=web_enabled,
            web_host=web_host,
            web_port=web_port,
            web_trusted_proxy_origin=web_trusted_proxy_origin,
            web_trusted_proxy_proof_sha256=web_trusted_proxy_proof_sha256,
            web_session_ttl_seconds=_number(
                config,
                "web_session_ttl_seconds",
                defaults.web_session_ttl_seconds,
                minimum=MIN_WEB_SESSION_TTL_SECONDS,
            ),
            web_disconnect_grace_seconds=_number(
                config,
                "web_disconnect_grace_seconds",
                defaults.web_disconnect_grace_seconds,
                minimum=0,
            ),
            web_static_dir=_string(
                config, "web_static_dir", defaults.web_static_dir
            ),
            connect_timeout_seconds=_number(
                config,
                "connect_timeout_seconds",
                defaults.connect_timeout_seconds,
                minimum=0,
                exclusive_minimum=True,
            ),
            request_timeout_seconds=_number(
                config,
                "request_timeout_seconds",
                defaults.request_timeout_seconds,
                minimum=0,
                exclusive_minimum=True,
            ),
            service_name=_string(
                config, "service_name", defaults.service_name, nonempty=True
            ),
            permissions_profile_id=permissions_profile_id,
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
            personality=personality,
            model=_string(config, "model", defaults.model),
            service_tier=_string(config, "service_tier", defaults.service_tier),
            reasoning_effort=_string(
                config, "reasoning_effort", defaults.reasoning_effort
            ),
            source_kinds=_source_kinds(config, defaults.source_kinds),
            threads_initial_limit=_integer(
                config,
                "threads_initial_limit",
                defaults.threads_initial_limit,
                minimum=1,
            ),
            thread_list_query_limit=_integer(
                config,
                "thread_list_query_limit",
                defaults.thread_list_query_limit,
                minimum=1,
            ),
            history_preview_rounds=_integer(
                config,
                "history_preview_rounds",
                defaults.history_preview_rounds,
                minimum=1,
            ),
            show_history_preview_on_resume=_boolean(
                config,
                "show_history_preview_on_resume",
                defaults.show_history_preview_on_resume,
            ),
            attachment_ttl_seconds=_number(
                config,
                "attachment_ttl_seconds",
                defaults.attachment_ttl_seconds,
                minimum=0,
                exclusive_minimum=True,
            ),
            mirror_watchdog_seconds=_number(
                config,
                "mirror_watchdog_seconds",
                defaults.mirror_watchdog_seconds,
                minimum=0,
                exclusive_minimum=True,
            ),
            compact_start_timeout_seconds=_number(
                config,
                "compact_start_timeout_seconds",
                defaults.compact_start_timeout_seconds,
                minimum=0,
                exclusive_minimum=True,
            ),
            stream_patch_interval_ms=_integer(
                config,
                "stream_patch_interval_ms",
                defaults.stream_patch_interval_ms,
                minimum=0,
            ),
            terminal_result_card_limit=_integer(
                config,
                "terminal_result_card_limit",
                defaults.terminal_result_card_limit,
                minimum=1,
            ),
        )


DEFAULT_CODEX_CONFIG = CodexConfig()
