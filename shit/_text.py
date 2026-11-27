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


_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC


def _split(text):
    return [name for name in text.split(os.sep) if name not in ("", ".")]


def _resolve(path, base):
    """Return (real base directory, names of the path below the base).

    The base is the trusted root: the owner or the CLI chooses it, so it is resolved once with
    os.path.realpath and may itself sit behind symlinks (macOS /tmp is one). Only the names below
    the base are walked with O_NOFOLLOW. The path is compared lexically with the base as given; no
    symlink in the path is resolved, so one below the base is refused by the walk, not followed.
    A `..` anywhere in the path, and a path that does not lie strictly below the base, is refused.
    """
    root = os.getcwd() if base is None else os.fspath(base)
    if not os.path.isabs(root):
        root = os.path.join(os.getcwd(), root)
    text = os.fspath(path)
    if not os.path.isabs(text):
        text = os.path.join(os.getcwd(), text)
    root = os.path.abspath(root)
    base_names = _split(root)
    names = _split(text)
    if ".." in names or len(names) <= len(base_names) or names[: len(base_names)] != base_names:
        raise ContractError(CONFINEMENT_VIOLATION, "confinement violation")
    return os.path.realpath(root), names[len(base_names) :]


def open_regular(path, base=None):
    """Open a regular, single-link file for reading, below a trusted base directory.

    base is the directory that the caller (the owner or the CLI) trusts as the project or working
    root: the brief, evidence, decision and handoff readers pass it through, and tests pass their
    own temporary root. It defaults to the working directory, which is where relative paths are
    taken from. The path must lie below base. Below base, every name is opened from a directory
    descriptor with O_NOFOLLOW, so a symlink, a `..`, or an absolute name in the relative part is
    refused, as are a FIFO, a directory and a hard-linked file.
    """
    real, names = _resolve(path, base)
    dfd = None
    try:
        dfd = os.open(real, _DIR_FLAGS)
        for name in names[:-1]:
            nfd = os.open(name, _DIR_FLAGS, dir_fd=dfd)
            os.close(dfd)
            dfd = nfd
        fd = os.open(names[-1], _FILE_FLAGS, dir_fd=dfd)
    except FileNotFoundError:
        raise ContractError(PRECONDITION_MISSING, "precondition missing") from None
    except OSError:
        raise ContractError(CONFINEMENT_VIOLATION, "confinement violation") from None
    finally:
        if dfd is not None:
            os.close(dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise ContractError(CONFINEMENT_VIOLATION, "confinement violation")
    except BaseException:
        os.close(fd)
        raise
    return fd


def check_value(key, value, limit):
    """One-line field value: text, nonempty, unpadded, within the limit, no unfilled placeholder."""
    if not isinstance(value, str):
        raise rejected(f"{key}: must be text")
    check_text(value)
    if "\n" in value:
        raise rejected(f"{key}: must be one line")
    if value != value.strip() or not value:
        raise rejected(f"{key}: empty or padded")
    if len(value) > limit:
        raise rejected(f"{key}: too long")
    if has_placeholder(value):
        raise rejected(f"{key}: unfilled placeholder")


_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_KEY_LINE = re.compile(r"([a-z][a-z_]*): ?(.*)")


def read_fields(text, spec):
    """Read `key: value` lines of a filled template and validate them against spec.

    spec maps key -> (required, maximum characters, check or None). Lines inside HTML comments and fenced
    code do not count, and any other line is free prose. An unknown or duplicate key is rejected. Values
    are never stripped: a padded value is refused, as rendering would refuse it.
    """
    check_text(text)
    body = _COMMENT.sub("", text)
    if "<!--" in body:
        raise rejected("unterminated comment")
    fields = {}
    fenced = False
    for line in body.split("\n"):
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _KEY_LINE.fullmatch(line)
        if not m:
            continue
        key = m.group(1)
        if key not in spec:
            raise rejected("unknown field")
        if key in fields:
            raise rejected(f"{key}: duplicate")
        fields[key] = m.group(2)
    out = {}
    for key, (required, limit, check) in spec.items():
        if key not in fields:
            if required:
                raise rejected(f"{key}: required")
            continue
        value = fields[key]
        check_value(key, value, limit)
        if check is not None and not check(value):
            raise rejected(f"{key}: malformed")
        out[key] = value
    return out


def read_bounded(path, limit, base=None):
    """Read at most limit + 1 bytes of a regular file below base. The caller decides what over the limit means."""
    fd = open_regular(path, base)
    with os.fdopen(fd, "rb") as fh:
        return fh.read(limit + 1)


def decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise rejected("input is not valid UTF-8") from None
