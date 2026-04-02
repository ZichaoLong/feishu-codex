"""
共享本地默认 profile 解析逻辑。

用于保护 feishu-codex 自己维护的 `profile_state.json`：
- 若本地默认 profile 仍存在，则继续生效
- 若已不存在，则忽略并可由调用方自动清空本地覆盖
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from bot.adapters.base import RuntimeConfigSummary
from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig


@dataclass(slots=True)
class DefaultProfileResolution:
    stored_profile: str = ""
    effective_profile: str = ""
    stale_profile: str = ""
    available_profiles: tuple[str, ...] = ()


def resolve_local_default_profile(
    stored_profile: str,
    runtime_config: RuntimeConfigSummary | None,
) -> DefaultProfileResolution:
    normalized = str(stored_profile).strip()
    if not normalized:
        return DefaultProfileResolution()
    if runtime_config is None:
        # 无法确认时，安全起见不注入本地覆盖，避免 stale profile 直接把流程打坏。
        return DefaultProfileResolution(stored_profile=normalized)

    available_profiles = tuple(
        profile.name.strip()
        for profile in runtime_config.profiles
        if isinstance(profile.name, str) and profile.name.strip()
    )
    if normalized in available_profiles:
        return DefaultProfileResolution(
            stored_profile=normalized,
            effective_profile=normalized,
            available_profiles=available_profiles,
        )
    return DefaultProfileResolution(
        stored_profile=normalized,
        stale_profile=normalized,
        available_profiles=available_profiles,
    )


def resolve_local_default_profile_via_remote_backend(
    *,
    base_config: CodexAppServerConfig,
    app_server_url: str,
    stored_profile: str,
) -> DefaultProfileResolution:
    adapter = CodexAppServerAdapter(
        replace(
            base_config,
            app_server_mode="remote",
            app_server_url=app_server_url,
        )
    )
    try:
        runtime_config = adapter.read_runtime_config()
    except Exception:
        runtime_config = None
    finally:
        adapter.stop()
    return resolve_local_default_profile(stored_profile, runtime_config)
