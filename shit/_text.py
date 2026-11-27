"""Shared input hygiene for the text contracts: bounded reads and hostile-character checks."""

import os
import re
import stat
import unicodedata

from shit.errors import (
    CONFINEMENT_VIOLATION,
    INPUT_LIMIT,
    PRECONDITION_MISSING,
    ContractError,
    rejected,
)

# Categories refused anywhere in a contract text: control (includes CR, tab, NUL, escape), format
# (zero-width, bidi override, byte order mark), surrogate, private use, unassigned, line/paragraph
# separators. The single allowed control character is the line feed.
_BAD_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
_PLACEHOLDER = re.compile(r"<[A-Za-z][^<>\n]*>")


def check_text(text):
    """Refuse non-text and hostile characters. The caller has already bounded the size."""
    if not isinstance(text, str):
        raise rejected("input must be text")
    for ch in text:
        if ch != "\n" and unicodedata.category(ch) in _BAD_CATEGORIES:
            raise rejected("input contains a forbidden character")


def check_size(text, limit):
    if len(text.encode("utf-8", "surrogatepass")) > limit:
        raise ContractError(INPUT_LIMIT, "input exceeds limit")


def has_placeholder(value):
    return bool(_PLACEHOLDER.search(value))


def open_regular(path):
    """Open a regular file for reading without following a final symlink and without blocking on a FIFO."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise ContractError(PRECONDITION_MISSING, "precondition missing") from None
    except OSError:
        raise ContractError(CONFINEMENT_VIOLATION, "confinement violation") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ContractError(CONFINEMENT_VIOLATION, "confinement violation")
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_bounded(path, limit):
    """Read at most limit + 1 bytes of a regular file. The caller decides what over the limit means."""
    fd = open_regular(path)
    with os.fdopen(fd, "rb") as fh:
        return fh.read(limit + 1)


def decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise rejected("input is not valid UTF-8") from None
