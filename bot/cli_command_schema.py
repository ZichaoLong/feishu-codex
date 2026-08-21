"""Read-only command schemas exported from production argparse parsers."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CommandOption:
    names: tuple[str, ...]
    takes_value: bool
    choices: tuple[str, ...] = ()
    dest: str = ""


@dataclass(frozen=True, slots=True)
class CommandPositional:
    dest: str
    choices: tuple[str, ...] = ()
    maximum_count: int | None = 1


@dataclass(frozen=True, slots=True)
class CommandSchema:
    name: str
    options: tuple[CommandOption, ...] = ()
    positionals: tuple[CommandPositional, ...] = ()
    subcommands: tuple[CommandSchema, ...] = ()
    # Some wrappers pass unknown options through to another CLI.  These names
    # are not Focus-owned completion candidates; they are only needed to keep
    # their following value from being mistaken for a Focus subcommand.
    passthrough_options_with_value: frozenset[str] = field(default_factory=frozenset)

    def subcommand(self, name: str) -> CommandSchema | None:
        normalized = str(name or "")
        return next((item for item in self.subcommands if item.name == normalized), None)

    def option(self, name: str) -> CommandOption | None:
        normalized = str(name or "")
        return next(
            (item for item in self.options if normalized in item.names),
            None,
        )


def command_schema_from_argparse(
    parser: argparse.ArgumentParser,
    *,
    name: str | None = None,
) -> CommandSchema:
    """Project an argparse parser into the syntax needed by completion.

    The parser remains the command source of truth.  Hidden subcommands are
    omitted by following argparse's visible ``_choices_actions`` rather than
    copying a second allowlist into the completion layer.
    """

    options: list[CommandOption] = []
    help_options: list[CommandOption] = []
    positionals: list[CommandPositional] = []
    subcommands: list[CommandSchema] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            visible_names = {
                str(choice.dest)
                for choice in action._choices_actions
                if str(getattr(choice, "dest", "") or "")
            }
            for command_name, command_parser in action.choices.items():
                if command_name not in visible_names:
                    continue
                subcommands.append(
                    command_schema_from_argparse(
                        command_parser,
                        name=command_name,
                    )
                )
            continue
        if action.option_strings:
            projected = CommandOption(
                names=tuple(str(value) for value in action.option_strings),
                takes_value=action.nargs != 0,
                choices=_string_choices(action.choices),
                dest=str(action.dest or ""),
            )
            if isinstance(action, argparse._HelpAction):
                help_options.append(projected)
            else:
                options.append(projected)
            continue
        positionals.append(
            CommandPositional(
                dest=str(action.dest or ""),
                choices=_string_choices(action.choices),
                maximum_count=_maximum_positional_count(action.nargs),
            )
        )
    return CommandSchema(
        name=str(name if name is not None else parser.prog),
        options=tuple([*options, *help_options]),
        positionals=tuple(positionals),
        subcommands=tuple(subcommands),
    )


def argparse_subcommand_names(
    parser: argparse.ArgumentParser,
    *,
    visible_only: bool,
) -> tuple[str, ...]:
    action = next(
        (
            candidate
            for candidate in parser._actions
            if isinstance(candidate, argparse._SubParsersAction)
        ),
        None,
    )
    if action is None:
        return ()
    if not visible_only:
        return tuple(str(name) for name in action.choices)
    visible_names = {
        str(choice.dest)
        for choice in action._choices_actions
        if str(getattr(choice, "dest", "") or "")
    }
    return tuple(str(name) for name in action.choices if name in visible_names)


def argparse_subparser(
    parser: argparse.ArgumentParser,
    path: tuple[str, ...],
) -> argparse.ArgumentParser:
    current = parser
    for component in path:
        action = next(
            (
                candidate
                for candidate in current._actions
                if isinstance(candidate, argparse._SubParsersAction)
            ),
            None,
        )
        if action is None or component not in action.choices:
            raise KeyError(" ".join(path))
        current = action.choices[component]
    return current


def _string_choices(raw_choices: object) -> tuple[str, ...]:
    if raw_choices is None:
        return ()
    return tuple(str(value) for value in raw_choices)  # type: ignore[union-attr]


def _maximum_positional_count(nargs: object) -> int | None:
    if nargs in (None, "?"):
        return 1
    if nargs in ("*", "+", argparse.REMAINDER):
        return None
    if isinstance(nargs, int):
        return max(nargs, 0)
    return 1
