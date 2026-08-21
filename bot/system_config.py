"""Strict schema for the instance-local ``system.yaml`` configuration.

The YAML loader owns syntax only.  This module owns the complete accepted-key
inventory, defaults, type/range validation, and the small normalizations that
belong to Focus at the system-config admission boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from typing import Any, Mapping

from bot.feishu_ws_proxy import normalize_feishu_ws_proxy_mode


def _invalid_type(key: str, expected: str, value: object) -> ValueError:
    return ValueError(
        f"system.yaml 配置 `{key}` 必须是 {expected}，实际为 {type(value).__name__}"
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
        raise ValueError(f"system.yaml 配置 `{key}` 不能为空")
    return normalized


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
) -> int:
    if key not in config:
        return default
    value = config[key]
    if type(value) is not int:
        raise _invalid_type(key, "整数", value)
    if minimum is not None and value < minimum:
        raise ValueError(f"system.yaml 配置 `{key}` 不能小于 {minimum}")
    return value


def _number(
    config: Mapping[str, Any],
    key: str,
    default: float,
    *,
    exclusive_minimum: float | None = None,
) -> float:
    if key not in config:
        return default
    value = config[key]
    if type(value) not in {int, float}:
        raise _invalid_type(key, "数字", value)
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"system.yaml 配置 `{key}` 必须是有限数字")
    if exclusive_minimum is not None and parsed <= exclusive_minimum:
        raise ValueError(
            f"system.yaml 配置 `{key}` 必须大于 {exclusive_minimum:g}"
        )
    return parsed


def _string_list(
    config: Mapping[str, Any],
    key: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    if key not in config:
        return default
    value = config[key]
    if type(value) is not list:
        raise _invalid_type(key, "字符串列表", value)

    normalized: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(
                f"system.yaml 配置 `{key}` 的每一项都必须是字符串，"
                f"第 {index + 1} 项实际为 {type(item).__name__}"
            )
        item = item.strip()
        if not item:
            raise ValueError(
                f"system.yaml 配置 `{key}` 的第 {index + 1} 项不能为空"
            )
        if item in seen:
            raise ValueError(
                f"system.yaml 配置 `{key}` 包含重复值：{item!r}"
            )
        normalized.append(item)
        seen.add(item)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class SystemConfig:
    """Validated, typed projection of the complete ``system.yaml`` surface."""

    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    request_timeout_seconds: float = 5.0
    feishu_ws_proxy: str = "env"
    admin_open_ids: tuple[str, ...] = field(default_factory=tuple)
    bot_open_id: str = ""
    trigger_open_ids: tuple[str, ...] = field(default_factory=tuple)
    group_history_fetch_limit: int = 50
    group_history_fetch_lookback_seconds: int = 24 * 60 * 60
    debug_raw_card_ingress: bool = False

    @classmethod
    def accepted_keys(cls) -> frozenset[str]:
        """Return the authoritative accepted-key inventory for projections/tests."""

        return frozenset(item.name for item in fields(cls))

    @classmethod
    def from_dict(
        cls,
        config: Mapping[str, Any],
        *,
        require_credentials: bool = True,
    ) -> "SystemConfig":
        if not isinstance(config, Mapping):
            raise ValueError(
                "system.yaml 顶层必须是键值映射，"
                f"实际为 {type(config).__name__}"
            )

        for key in config:
            if not isinstance(key, str):
                raise ValueError(
                    "system.yaml 顶层键必须是字符串，"
                    f"实际包含 {type(key).__name__}"
                )

        unknown_keys = sorted(set(config) - cls.accepted_keys())
        if unknown_keys:
            rendered = "、".join(f"`{key}`" for key in unknown_keys)
            raise ValueError(f"system.yaml 包含未知配置键：{rendered}")

        defaults = cls()

        app_id = _string(
            config,
            "app_id",
            defaults.app_id,
            nonempty=require_credentials,
        )
        app_secret = _string(
            config,
            "app_secret",
            defaults.app_secret,
            nonempty=require_credentials,
        )
        if require_credentials:
            if "app_id" not in config:
                raise ValueError("system.yaml 配置 `app_id` 不能为空")
            if "app_secret" not in config:
                raise ValueError("system.yaml 配置 `app_secret` 不能为空")

        proxy_raw = _string(
            config,
            "feishu_ws_proxy",
            defaults.feishu_ws_proxy,
            nonempty=True,
        )

        return cls(
            app_id=app_id,
            app_secret=app_secret,
            request_timeout_seconds=_number(
                config,
                "request_timeout_seconds",
                defaults.request_timeout_seconds,
                exclusive_minimum=0,
            ),
            feishu_ws_proxy=normalize_feishu_ws_proxy_mode(proxy_raw),
            admin_open_ids=_string_list(
                config,
                "admin_open_ids",
                defaults.admin_open_ids,
            ),
            bot_open_id=_string(config, "bot_open_id", defaults.bot_open_id),
            trigger_open_ids=_string_list(
                config,
                "trigger_open_ids",
                defaults.trigger_open_ids,
            ),
            group_history_fetch_limit=_integer(
                config,
                "group_history_fetch_limit",
                defaults.group_history_fetch_limit,
                minimum=0,
            ),
            group_history_fetch_lookback_seconds=_integer(
                config,
                "group_history_fetch_lookback_seconds",
                defaults.group_history_fetch_lookback_seconds,
                minimum=0,
            ),
            debug_raw_card_ingress=_boolean(
                config,
                "debug_raw_card_ingress",
                defaults.debug_raw_card_ingress,
            ),
        )


DEFAULT_SYSTEM_CONFIG = SystemConfig()
