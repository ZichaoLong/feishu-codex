"""Canonical public command names and Python entry modules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PublicCommandSpec:
    name: str
    module: str
    wrapper_command: str = ""

    @property
    def console_script_target(self) -> str:
        return f"{self.module}:main"


# This stable order is also embedded in generated shell-completion scripts.
PUBLIC_COMMAND_SPECS: tuple[PublicCommandSpec, ...] = (
    PublicCommandSpec(name="focus", module="bot.fcodex.cli", wrapper_command="focus"),
    PublicCommandSpec(name="focusctl", module="bot.focusctl"),
    PublicCommandSpec(name="focusd", module="bot.__main__"),
    PublicCommandSpec(name="fcodex", module="bot.fcodex.cli", wrapper_command="fcodex"),
)

PUBLIC_COMMAND_NAMES: tuple[str, ...] = tuple(
    spec.name for spec in PUBLIC_COMMAND_SPECS
)
