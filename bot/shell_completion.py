"""Runtime shell completion projected from FOCUS production parsers."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import lru_cache

from bot.cli_command_schema import (
    CommandOption,
    CommandPositional,
    CommandSchema,
    command_schema_from_argparse,
)
from bot.instance_layout import DEFAULT_INSTANCE_NAME, list_known_instance_names
from bot.public_command_contract import PUBLIC_COMMAND_NAMES


@dataclass(frozen=True, slots=True)
class CompletionContext:
    words: tuple[str, ...]
    cword: int

    @property
    def current(self) -> str:
        if 0 <= self.cword < len(self.words):
            return self.words[self.cword]
        return ""

    @property
    def previous(self) -> str:
        if self.cword <= 0:
            return ""
        return self.words[self.cword - 1]

    @property
    def args_before_cursor(self) -> tuple[str, ...]:
        if self.cword <= 1:
            return ()
        return self.words[1 : min(self.cword, len(self.words))]


def complete_words(command_name: str, words: list[str], cword: int) -> list[str]:
    normalized_command = str(command_name or "").strip()
    if normalized_command not in PUBLIC_COMMAND_NAMES:
        return []
    context = CompletionContext(words=tuple(words), cword=max(cword, 0))
    return _complete_from_schema(_command_schema(normalized_command), context)


@dataclass(frozen=True, slots=True)
class _ResolvedSchemaContext:
    schema: CommandSchema
    command_path: tuple[str, ...]
    positionals: tuple[str, ...]
    pending_option: CommandOption | None
    pending_passthrough_value: bool
    options_enabled: bool


@lru_cache(maxsize=None)
def _command_schema(command_name: str) -> CommandSchema:
    if command_name == "focusctl":
        from bot.focusctl import focusctl_command_schema

        return focusctl_command_schema()
    if command_name == "focusd":
        from bot.__main__ import _build_parser

        return command_schema_from_argparse(_build_parser(), name="focusd")
    from bot.fcodex.cli import wrapper_command_schema

    return wrapper_command_schema(command_name)


def _complete_from_schema(
    schema: CommandSchema,
    context: CompletionContext,
) -> list[str]:
    resolved = _resolve_schema_context(schema, context.args_before_cursor)
    current = context.current
    if resolved.pending_passthrough_value:
        return []
    if resolved.pending_option is not None:
        return _complete_option_value(resolved.pending_option, current)

    if resolved.options_enabled and current.startswith("-") and current != "-":
        if "=" in current:
            option_name, value_prefix = current.split("=", 1)
            option = resolved.schema.option(option_name)
            if option is not None and option.takes_value:
                prefix = f"{option_name}="
                return [
                    f"{prefix}{candidate}"
                    for candidate in _complete_option_value(option, value_prefix)
                ]
        return _complete_candidates(
            current,
            [
                option_name
                for option in resolved.schema.options
                for option_name in option.names
            ],
        )

    if resolved.schema.subcommands and not resolved.positionals:
        candidates = [command.name for command in resolved.schema.subcommands]
        if not resolved.command_path and not context.args_before_cursor:
            candidates = [
                *(
                    option_name
                    for option in resolved.schema.options
                    for option_name in option.names
                ),
                *candidates,
            ]
        return _complete_candidates(current, candidates)

    positional = _active_positional(
        resolved.schema.positionals,
        len(resolved.positionals),
    )
    if positional is not None:
        choices = _positional_choices(resolved.command_path, positional)
        if choices:
            return _complete_candidates(current, choices)

    if not resolved.command_path and not resolved.positionals:
        return _complete_candidates(
            current,
            [
                option_name
                for option in resolved.schema.options
                for option_name in option.names
            ],
        )
    return []


def _resolve_schema_context(
    root: CommandSchema,
    args_before_cursor: tuple[str, ...],
) -> _ResolvedSchemaContext:
    schema = root
    command_path: list[str] = []
    positionals: list[str] = []
    pending_option: CommandOption | None = None
    pending_passthrough_value = False
    options_enabled = True
    for token in args_before_cursor:
        if pending_option is not None:
            pending_option = None
            continue
        if pending_passthrough_value:
            pending_passthrough_value = False
            continue
        if token == "--":
            options_enabled = False
            continue
        if options_enabled and token.startswith("-") and token != "-":
            option_name = token.split("=", 1)[0]
            option = schema.option(option_name)
            if option is not None:
                if option.takes_value and "=" not in token:
                    pending_option = option
                continue
            if option_name in schema.passthrough_options_with_value:
                if "=" not in token:
                    pending_passthrough_value = True
                continue
            # Unknown options are valid passthrough for the TUI wrapper and
            # invalid on strict argparse surfaces.  Neither case makes the
            # option itself a positional command.
            continue
        child = schema.subcommand(token)
        if child is not None and not positionals:
            schema = child
            command_path.append(child.name)
            positionals.clear()
            options_enabled = True
            continue
        positionals.append(token)
    return _ResolvedSchemaContext(
        schema=schema,
        command_path=tuple(command_path),
        positionals=tuple(positionals),
        pending_option=pending_option,
        pending_passthrough_value=pending_passthrough_value,
        options_enabled=options_enabled,
    )


def _complete_option_value(option: CommandOption, prefix: str) -> list[str]:
    if option.dest == "instance":
        return _complete_candidates(prefix, list_known_instance_names())
    return _complete_candidates(prefix, list(option.choices))


def _active_positional(
    positionals: tuple[CommandPositional, ...],
    consumed_count: int,
) -> CommandPositional | None:
    remaining = max(consumed_count, 0)
    for positional in positionals:
        if positional.maximum_count is None:
            return positional
        if remaining < positional.maximum_count:
            return positional
        remaining -= positional.maximum_count
    return None


def _positional_choices(
    command_path: tuple[str, ...],
    positional: CommandPositional,
) -> list[str]:
    if command_path == ("instance", "remove") and positional.dest == "name":
        return [
            name
            for name in list_known_instance_names()
            if name != DEFAULT_INSTANCE_NAME
        ]
    return list(positional.choices)


def _complete_candidates(prefix: str, candidates: list[str]) -> list[str]:
    normalized_prefix = str(prefix or "")
    seen: set[str] = set()
    matches: list[str] = []
    for candidate in candidates:
        normalized_candidate = str(candidate or "")
        if not normalized_candidate.startswith(normalized_prefix):
            continue
        if normalized_candidate in seen:
            continue
        seen.add(normalized_candidate)
        matches.append(normalized_candidate)
    return matches


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4 or args[0] != "complete":
        print("usage: python -m bot.shell_completion complete <command> <cword> <comp_words...>", file=sys.stderr)
        return 2
    command_name = str(args[1] or "").strip()
    try:
        cword = int(args[2])
    except ValueError:
        return 0
    words = list(args[3:])
    for candidate in complete_words(command_name, words, cword):
        print(candidate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
