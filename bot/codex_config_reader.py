"""
Read codex config.toml to resolve profile model/model_provider locally.

Workaround: the app-server's turn/start has no config field for profile,
and thread/resume's slow path overwrites model from persisted metadata.
The codex TUI avoids this by sending explicit model + model_provider
typesafe fields (resolved from its local Config). feishu-codex replicates
this by reading ~/.codex/config.toml directly.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedProfileConfig:
    model: str = ""
    model_provider: str = ""


def resolve_profile_from_codex_config(profile_name: str) -> ResolvedProfileConfig:
    """Extract model and model_provider for *profile_name* from config.toml."""
    if not profile_name:
        return ResolvedProfileConfig()
    config_path = _codex_config_path()
    if config_path is None:
        return ResolvedProfileConfig()
    try:
        with open(config_path, "rb") as fh:
            config = tomllib.load(fh)
    except Exception:
        logger.debug("failed to read %s", config_path, exc_info=True)
        return ResolvedProfileConfig()
    profile = (config.get("profiles") or {}).get(profile_name)
    if not isinstance(profile, dict):
        return ResolvedProfileConfig()
    return ResolvedProfileConfig(
        model=str(profile.get("model", "")),
        model_provider=str(profile.get("model_provider", "")),
    )


def _codex_config_path() -> Path | None:
    codex_home_env = os.environ.get("CODEX_HOME", "").strip()
    codex_home = Path(codex_home_env) if codex_home_env else Path.home() / ".codex"
    config_path = codex_home / "config.toml"
    return config_path if config_path.is_file() else None
