"""Validate a handoff written from templates/handoff.md.

A handoff moves work from one worker or session to the next. No record schema in docs/schemas.md covers
it, so its field set is the template itself. The same rules as a decision apply: one `key: value` line per
field, one line each, unpadded, bounded, no control or bidirectional characters, no unknown or duplicate
keys, no unfilled placeholder.

Approval grants nothing here. The `approval` field may say `none`, `pending`, or `human - <where the
human's own message is>`. Any other value, such as `granted` with no reference, is refused, so a file
cannot claim approval by its own text. Errors never echo the rejected value.

    python3 -c "from shit import handoff; print(handoff.read('handoff.md'))"
"""

import re

from shit._text import check_size, decode, read_bounded, read_fields
from shit.errors import INPUT_LIMIT, ContractError, rejected

MAX_BYTES = 65536

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_SHA = re.compile(r"[0-9a-f]{7,40}")
_RANGE = re.compile(r"[0-9a-f]{7,40}\.\.[0-9a-f]{7,40}")
_APPROVAL = re.compile(r"none|pending|human - \S.*")


def _slug(value):
    return _SLUG.fullmatch(value) is not None


def _sha(value):
    return _SHA.fullmatch(value) is not None


def _range(value):
    return _RANGE.fullmatch(value) is not None


def _absolute(value):
    return value.startswith("/") and "\\" not in value


def _packs(value):
    if value == "none":
        return True
    names = [p.strip() for p in value.split(",")]
    return all(_SLUG.fullmatch(n) for n in names) and len(set(names)) == len(names)


def _approval(value):
    return _APPROVAL.fullmatch(value) is not None


# key -> (required, maximum characters, check or None), in template order.
SPEC = {
    "task": (True, 64, _slug),
    "from_role": (True, 64, _slug),
    "to_role": (True, 64, _slug),
    "runtime": (True, 500, None),
    "branch": (True, 200, None),
    "worktree": (True, 500, _absolute),
    "base_sha": (True, 40, _sha),
    "commit_range": (True, 100, _range),
    "report_path": (True, 500, None),
    "gate": (True, 500, None),
    "other_gates": (True, 1000, None),
    "red_evidence": (True, 1000, None),
    "packs": (True, 1000, _packs),
    "data_class": (True, 500, None),
    "constraints": (True, 1000, None),
    "open_items": (True, 2000, None),
    "approval": (True, 500, _approval),
    "next_owner": (True, 1000, None),
}


def parse(text):
    """Validate handoff text and return its fields. Raises ContractError."""
    if not isinstance(text, str):
        raise rejected("input must be text")
    check_size(text, MAX_BYTES)
    return read_fields(text, SPEC)


def read(path):
    """Read a handoff file (at most 64 KiB, regular single-link file only) and validate it."""
    data = read_bounded(path, MAX_BYTES)
    if len(data) > MAX_BYTES:
        raise ContractError(INPUT_LIMIT, "input exceeds limit")
    return parse(decode(data))
