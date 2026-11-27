"""Shared command-line plumbing: a parser that never echoes its input, and one fixed-output runner.

A subcommand module exposes `register(subparsers)`, which adds its parser and sets `func` to a handler
`handler(args) -> (exit_code, stdout_lines)`. `run` turns a handler result, or any error, into a stable
exit code with the fixed message from docs/exit-codes.md. Nothing here prints a rejected value.
"""

import argparse
import sys

from .errors import INTERNAL, MESSAGES, OK, USAGE, ShitError

# Codes in docs/exit-codes.md that shit/errors.py does not carry yet.
PRIVACY_HIT = 6
HOOK_FAILED_CLOSED = 8
TOOL_UNAVAILABLE = 11
ALL_MESSAGES = {
    **MESSAGES,
    PRIVACY_HIT: "privacy scan hit",
    HOOK_FAILED_CLOSED: "hook verification failed",
    TOOL_UNAVAILABLE: "subprocess or tool unavailable",
}


class CodedError(Exception):
    """An error for one of the codes above. Like ShitError it carries only the code and its fixed message."""

    def __init__(self, code):
        super().__init__(ALL_MESSAGES[code])
        self.code = code


EXPECTED = (ShitError, CodedError, ValueError, OSError, LookupError, TypeError, RecursionError, MemoryError)


class SafeParser(argparse.ArgumentParser):
    """argparse echoes the offending argument in its error text; this parser raises the fixed code instead."""

    def error(self, message):
        raise ShitError(USAGE)


def error_code(exc):
    """Exit code carried by a ShitError or ContractError; anything else is the internal-error code."""
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) and code in ALL_MESSAGES else INTERNAL


def run(build, argv=None, out=None, err=None):
    """Parse `argv` with the parser from `build()`, run the handler, print, and return the exit code."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    try:
        args = build().parse_args(argv)
        code, lines = args.func(args)
    except SystemExit as exc:  # --help
        return exc.code if isinstance(exc.code, int) else OK
    except EXPECTED as exc:
        code, lines = error_code(exc), []
    for line in lines:
        print(line, file=out)
    if code != OK:
        print(f"shit: {ALL_MESSAGES.get(code, ALL_MESSAGES[INTERNAL])} (exit {code})", file=err)
    return code
