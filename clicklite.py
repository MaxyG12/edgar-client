"""
ClickLite — a simplified CLI framework modeled after Click.

Architectural reasoning embedded throughout.
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Sequence


# ── Types ──────────────────────────────────────────────────────────────────────
#
# A ParamType converts a raw string from argv into a typed Python value.
# Making this a class (not just a function) lets us attach metadata like
# .name for help text and .fail() for consistent error raising.
#
# Design decision: types are *instances*, not classes (STRING not String()).
# This matches Click's pattern and avoids accidental mutation if the same
# type is shared across many options.

class CLError(Exception):
    """Raised when argument parsing or type conversion fails."""


class ParamType:
    name: str = "VALUE"

    def convert(self, value: str) -> Any:
        return value

    def fail(self, message: str) -> None:
        raise CLError(message)

    def __repr__(self) -> str:
        return self.name


class StringType(ParamType):
    name = "TEXT"

    def convert(self, value: str) -> str:
        return value


class IntType(ParamType):
    name = "INTEGER"

    def convert(self, value: str) -> int:
        try:
            return int(value)
        except ValueError:
            self.fail(f"'{value}' is not a valid integer.")


class ChoiceType(ParamType):
    def __init__(self, choices: Sequence[str]) -> None:
        self.choices = list(choices)
        self.name = "|".join(choices)

    def convert(self, value: str) -> str:
        if value not in self.choices:
            self.fail(f"'{value}' is not one of {self.choices}.")
        return value


# Singleton instances — the public API uses these as type= values.
STRING = StringType()
INT = IntType()

def Choice(choices: Sequence[str]) -> ChoiceType:
    """Factory for choice types: type=Choice(['a', 'b', 'c'])."""
    return ChoiceType(choices)


# ── Parameter metadata ─────────────────────────────────────────────────────────
#
# OptionDef is pure metadata — it knows nothing about parsing. It stores what
# an option IS; Command.parse_args() implements how to find it in argv.
#
# Design decision: we derive param_name from the longest name (e.g. "--output"
# → "output"). Short aliases like "-o" are just alternate lookup keys.

class OptionDef:
    def __init__(
        self,
        names: list[str],
        type: ParamType = STRING,
        default: Any = None,
        required: bool = False,
        is_flag: bool = False,
        help: str = "",
    ) -> None:
        self.names = names
        # Canonical Python name: longest flag, stripped of dashes
        self.param_name = max(names, key=len).lstrip("-").replace("-", "_")
        self.type = type
        self.default = default
        self.required = required
        self.is_flag = is_flag
        self.help = help

    def __repr__(self) -> str:
        return f"<OptionDef {self.names}>"


# ── Command ────────────────────────────────────────────────────────────────────
#
# Command encapsulates three things:
#   1. What options exist (self.params)
#   2. How to parse argv into a kwargs dict (parse_args)
#   3. How to call the underlying function (invoke)
#
# Keeping parse_args separate from invoke lets us test parsing independently
# of any side effects the callback might have.

class Command:
    def __init__(
        self,
        name: str,
        callback: Callable,
        params: list[OptionDef],
        help: str = "",
    ) -> None:
        self.name = name
        self.callback = callback
        self.params = params
        self.help = help

    def _build_lookup(self) -> dict[str, OptionDef]:
        return {name: p for p in self.params for name in p.names}

    def parse_args(self, argv: list[str]) -> dict[str, Any]:
        """Walk argv left-to-right, match options, return typed kwargs dict.

        Parsing strategy: linear scan. Each token is either an option flag
        (starts with "-") or an error. Flags either consume the next token
        (value options) or don't (flags). This is intentionally simpler than
        Click's full parser — no interspersed args, no = syntax.
        """
        lookup = self._build_lookup()
        result: dict[str, Any] = {}
        i = 0
        while i < len(argv):
            token = argv[i]
            if token in lookup:
                p = lookup[token]
                if p.is_flag:
                    result[p.param_name] = True
                    i += 1
                else:
                    if i + 1 >= len(argv):
                        raise CLError(f"Option '{token}' requires a value.")
                    result[p.param_name] = p.type.convert(argv[i + 1])
                    i += 2
            elif token.startswith("-"):
                raise CLError(f"No such option: {token!r}")
            else:
                raise CLError(f"Got unexpected argument: {token!r}")

        # Fill in defaults; enforce required options
        for p in self.params:
            if p.param_name not in result:
                if p.required:
                    raise CLError(f"Missing required option: {p.names[0]}")
                result[p.param_name] = False if p.is_flag else p.default

        return result

    def invoke(self, argv: list[str]) -> None:
        if "--help" in argv or "-h" in argv:
            print(self.get_help())
            return
        kwargs = self.parse_args(argv)
        self.callback(**kwargs)

    def get_help(self) -> str:
        lines = []
        if self.help:
            lines += [self.help, ""]
        lines.append("Options:")
        for p in self.params:
            flags = ", ".join(p.names)
            meta = "" if p.is_flag else f" {p.type}"
            tag = ""
            if p.required:
                tag = "  [required]"
            elif p.default is not None:
                tag = f"  [default: {p.default}]"
            desc = f"  {p.help}" if p.help else ""
            lines.append(f"  {flags}{meta}{tag}{desc}")
        lines.append("  -h, --help    Show this message and exit.")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"<Command {self.name!r}>"


# ── Group ──────────────────────────────────────────────────────────────────────
#
# Group is NOT a subclass of Command here (unlike Click). In Click, Group
# extends Command so it inherits all the parsing machinery and can have its
# own options. We replicate that behavior but keep them separate for clarity.
#
# The routing algorithm:
#   1. Parse group's own options from the left, stop at first non-option token
#   2. The next token is the subcommand name
#   3. Remaining tokens go to the subcommand
#
# Design decision: group callback runs BEFORE the subcommand. This is the
# right place for setup work (opening DB connections, configuring logging, etc.)

class Group:
    def __init__(
        self,
        name: str,
        callback: Callable | None,
        params: list[OptionDef],
        help: str = "",
    ) -> None:
        self.name = name
        self.callback = callback
        self.params = params
        self.help = help
        self.commands: dict[str, Command | Group] = {}

    def add_command(self, cmd: Command | Group, name: str | None = None) -> None:
        key = name or cmd.name
        self.commands[key] = cmd

    # Sub-decorator: @grp.command registers a command on this group.
    # This mirrors Click's pattern — it lets you define commands inline:
    #   @cli.command
    #   def greet(): ...
    def command(self, func: Callable | None = None, *, name: str | None = None):
        def decorator(f: Callable) -> Command:
            cmd = _make_command(f, name)
            self.add_command(cmd)
            return cmd
        return decorator(func) if func is not None else decorator

    def group(self, func: Callable | None = None, *, name: str | None = None):
        def decorator(f: Callable) -> Group:
            grp = _make_group(f, name)
            self.add_command(grp)
            return grp
        return decorator(func) if func is not None else decorator

    def _parse_own_args(self, argv: list[str]) -> tuple[dict[str, Any], list[str]]:
        """Parse options that belong to the group itself; stop at first non-option.

        Returns (group_kwargs, remaining_argv_for_subcommand).
        This is how groups can have their own flags while still routing to
        subcommands — we peel off what we own, pass the rest down.
        """
        lookup = {name: p for p in self.params for name in p.names}
        result: dict[str, Any] = {}
        i = 0
        while i < len(argv):
            token = argv[i]
            if token in lookup:
                p = lookup[token]
                if p.is_flag:
                    result[p.param_name] = True
                    i += 1
                else:
                    if i + 1 >= len(argv):
                        raise CLError(f"Option '{token}' requires a value.")
                    result[p.param_name] = p.type.convert(argv[i + 1])
                    i += 2
            else:
                break  # Non-option token → stop; this is the subcommand name

        for p in self.params:
            if p.param_name not in result:
                result[p.param_name] = False if p.is_flag else p.default

        return result, argv[i:]

    def invoke(self, argv: list[str]) -> None:
        if "--help" in argv or "-h" in argv:
            print(self.get_help())
            return

        group_kwargs, remaining = self._parse_own_args(argv)

        if not remaining:
            # No subcommand given: run group callback if present, else show help
            if self.callback is not None:
                self.callback(**group_kwargs)
            else:
                print(self.get_help())
            return

        sub_name, sub_argv = remaining[0], remaining[1:]

        if sub_name not in self.commands:
            available = list(self.commands)
            raise CLError(
                f"No such command: {sub_name!r}. Available: {available}"
            )

        # Run group callback (setup) before dispatching to subcommand
        if self.callback is not None:
            self.callback(**group_kwargs)

        self.commands[sub_name].invoke(sub_argv)

    def get_help(self) -> str:
        lines = []
        if self.help:
            lines += [self.help, ""]
        lines.append(f"Usage: {self.name} [OPTIONS] COMMAND [ARGS]...")
        if self.commands:
            lines += ["", "Commands:"]
            for name, cmd in self.commands.items():
                short = cmd.help.split("\n")[0] if cmd.help else ""
                lines.append(f"  {name:<18}{short}")
        if self.params:
            lines += ["", "Options:"]
            for p in self.params:
                flags = ", ".join(p.names)
                lines.append(f"  {flags:<20}{p.help}")
        lines.append("  -h, --help          Show this message and exit.")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"<Group {self.name!r}>"


# ── Decorator machinery ────────────────────────────────────────────────────────
#
# The accumulation pattern:
#   - @option appends to func.__clicklite_params__ (a list on the function object)
#   - @command reads that list, reverses it (decorators apply bottom-up), and
#     creates a Command object
#
# Why reverse? Decorators stack like this:
#
#   @command          ← applied 3rd (outermost)
#   @option("--age")  ← applied 2nd → appends age to __clicklite_params__
#   @option("--name") ← applied 1st → appends name to __clicklite_params__
#   def greet(...): ...
#
# After both @option calls: __clicklite_params__ = [name_opt, age_opt]
# After reversing: [age_opt, name_opt] — wait, that's wrong...
#
# Actually: decorators apply bottom-up, so "--name" runs first (it's closer
# to the function), then "--age". So params = [name, age].
# Reversing gives [age, name] — that's the wrong order.
#
# Click reverses because it extends the list (not appends) and processes it
# differently. Here we simply append and then reverse to get declaration order.
# Let's trace again:
#
#   @option("--name")  ← runs 2nd → params = [age, name]  (age was added first)
#   @option("--age")   ← runs 1st → params = [age]
#   def greet(...): ...
#
# So params = [age, name]. Reversing = [name, age] = declaration order. Correct!

def _make_command(func: Callable, name: str | None = None) -> Command:
    raw: list[OptionDef] = getattr(func, "__clicklite_params__", [])
    params = list(reversed(raw))  # restore top-to-bottom declaration order
    if hasattr(func, "__clicklite_params__"):
        del func.__clicklite_params__
    return Command(
        name=name or func.__name__.replace("_", "-"),
        callback=func,
        params=params,
        help=func.__doc__ or "",
    )


def _make_group(func: Callable, name: str | None = None) -> Group:
    raw: list[OptionDef] = getattr(func, "__clicklite_params__", [])
    params = list(reversed(raw))
    if hasattr(func, "__clicklite_params__"):
        del func.__clicklite_params__
    return Group(
        name=name or func.__name__.replace("_", "-"),
        callback=func,
        params=params,
        help=func.__doc__ or "",
    )


def command(func: Callable | None = None, *, name: str | None = None):
    """Decorator: turns a function into a Command.

    Works bare (@command) or with arguments (@command(name='my-cmd')).
    """
    def decorator(f: Callable) -> Command:
        return _make_command(f, name)
    return decorator(func) if func is not None else decorator


def group(func: Callable | None = None, *, name: str | None = None):
    """Decorator: turns a function into a Group.

    Works bare (@group) or with arguments (@group(name='my-grp')).
    """
    def decorator(f: Callable) -> Group:
        return _make_group(f, name)
    return decorator(func) if func is not None else decorator


def option(
    *names: str,
    type: ParamType = STRING,
    default: Any = None,
    required: bool = False,
    is_flag: bool = False,
    help: str = "",
):
    """Decorator: attaches option metadata to a function.

    Must be applied before @command or @group — they consume the metadata.
    Applied bottom-up by Python; reversed at finalization to preserve order.
    """
    def decorator(f: Callable) -> Callable:
        if not hasattr(f, "__clicklite_params__"):
            f.__clicklite_params__ = []
        f.__clicklite_params__.append(
            OptionDef(list(names), type=type, default=default,
                      required=required, is_flag=is_flag, help=help)
        )
        return f
    return decorator


# ── Entry point ────────────────────────────────────────────────────────────────

def run(
    cmd: Command | Group,
    argv: list[str] | None = None,
    prog_name: str | None = None,
) -> None:
    """Parse argv and invoke cmd. Defaults to sys.argv[1:]."""
    if argv is None:
        argv = sys.argv[1:]
    try:
        cmd.invoke(argv)
    except CLError as e:
        prog = prog_name or cmd.name or sys.argv[0]
        print(f"Error: {e}", file=sys.stderr)
        print(f"Try '{prog} --help' for help.", file=sys.stderr)
        sys.exit(1)
