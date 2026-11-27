"""Validate a decision record written from templates/decision.md.

The fields are the decision record in docs/schemas.md: one `key: value` line each, the same shape as a
brief. Every value is one line, unpadded, bounded, and free of control and bidirectional characters.
Unknown and duplicate keys are rejected, and so is a decision that uses `supersedes_id` without being
`superseded`. An `accepted` decision recorded by a non-human actor is refused with exit code 10, because
a model or wrapper may propose but cannot grant approval. Errors never echo the rejected value.

    python3 -c "from shit import decision; print(decision.read('decision.md'))"
"""

import re
from datetime import datetime, timezone

from shit._text import check_size, decode, read_bounded, read_fields
from shit.errors import HUMAN_APPROVAL, INPUT_LIMIT, ContractError, rejected

MAX_BYTES = 65536

ACTORS = ("human", "wrapper", "model")
STATUSES = ("proposed", "accepted", "rejected", "superseded")

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _slug(value):
    return _SLUG.fullmatch(value) is not None


def _timestamp(value):
    if _TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return True


# key -> (required, maximum characters, check). Lengths follow docs/schemas.md.
SPEC = {
    "schema": (True, 64, lambda v: v == "decision"),
    "schema_version": (True, 8, lambda v: v == "1"),
    "id": (True, 64, _slug),
    "task_id": (True, 64, _slug),
    "created_at": (True, 20, _timestamp),
    "actor": (True, 16, lambda v: v in ACTORS),
    "status": (True, 16, lambda v: v in STATUSES),
    "supersedes_id": (False, 64, _slug),
    "question": (True, 2000, None),
    "choice": (True, 2000, None),
    "rationale": (True, 4000, None),
}


def parse(text):
    """Validate decision text and return its fields. Raises ContractError."""
    if not isinstance(text, str):
        raise rejected("input must be text")
    check_size(text, MAX_BYTES)
    fields = read_fields(text, SPEC)
    if "supersedes_id" in fields and fields["status"] != "superseded":
        raise rejected("supersedes_id: allowed only when status is superseded")
    if fields["status"] == "accepted" and fields["actor"] != "human":
        raise ContractError(HUMAN_APPROVAL, "human approval required")
    return fields


def read(path, base=None):
    """Read a decision file (at most 64 KiB, regular single-link file only) and validate it."""
    data = read_bounded(path, MAX_BYTES, base)
    if len(data) > MAX_BYTES:
        raise ContractError(INPUT_LIMIT, "input exceeds limit")
    return parse(decode(data))
