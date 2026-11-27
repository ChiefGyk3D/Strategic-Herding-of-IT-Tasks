"""Version 1 evidence, report, review and decision records (docs/schemas.md).

Same rules as the task record: strict keys, exact version, no duplicate keys, size and depth limits
before parsing, ASCII-escaped output. Every string is also checked for hostile characters (control,
bidi, zero-width, separators), so a record can be printed to a terminal and read by a person safely.
"""

import json
import re

from . import records
from ._text import check_text
from .errors import INPUT_LIMIT, ContractError, ShitError

STATUSES = ("proposed", "accepted", "rejected", "superseded")
OUTCOMES = ("informational", "success", "blocked", "failed")
VERDICTS = ("pass", "changes", "fail")
KINDS = ("evidence", "report", "review", "decision")
SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_PATH = 500
MAX_EVIDENCE_IDS = 200

_COMMON = {"schema", "schema_version", "id", "task_id", "created_at", "actor"}
FIELDS = {
    "evidence": (_COMMON | {"description", "path", "sha256", "size"}, set()),
    "report": (_COMMON | {"outcome", "summary", "evidence_ids"}, {"log_reference"}),
    "review": (_COMMON | {"report_id", "verdict", "summary"}, set()),
    "decision": (_COMMON | {"status", "question", "choice", "rationale"}, {"supersedes_id"}),
}


def convert(exc):
    """Turn a contract error into the storage error type with the same stable code."""
    return ShitError(exc.code)


def clean_text(value):
    """Reject non-text and hostile characters; return the value unchanged."""
    try:
        check_text(value)
    except ContractError as exc:
        raise convert(exc) from None
    return value


def _strings(value):
    if isinstance(value, str):
        clean_text(value)
    elif isinstance(value, list):
        for item in value:
            _strings(item)


def validate(kind, obj):
    if kind not in FIELDS or not isinstance(obj, dict):
        raise ShitError(records.REJECTED)
    required, optional = FIELDS[kind]
    records._keys(obj, required, optional)
    records._version(obj, kind)
    records._slug(obj, "id")
    records._slug(obj, "task_id")
    records._stamp(obj, "created_at")
    records._choice(obj, "actor", records.ACTORS)
    for value in obj.values():
        _strings(value)
    _CHECKS[kind](obj)
    return obj


def _evidence(obj):
    records._text(obj, "description", 1000)
    path = records._text(obj, "path", MAX_PATH)
    parts = path.split("/")
    if path.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ShitError(records.REJECTED)
    if not SHA256.fullmatch(obj.get("sha256") if isinstance(obj.get("sha256"), str) else ""):
        raise ShitError(records.REJECTED)
    size = obj.get("size")
    if type(size) is not int or size < 0:
        raise ShitError(records.REJECTED)


def _report(obj):
    records._choice(obj, "outcome", OUTCOMES)
    records._text(obj, "summary", 4000)
    ids = obj.get("evidence_ids")
    if not isinstance(ids, list) or len(ids) > MAX_EVIDENCE_IDS:
        raise ShitError(records.REJECTED)
    for item in ids:
        if not isinstance(item, str) or not records.SLUG.fullmatch(item):
            raise ShitError(records.REJECTED)
    if "log_reference" in obj:
        records._slug(obj, "log_reference")


def _review(obj):
    records._slug(obj, "report_id")
    records._choice(obj, "verdict", VERDICTS)
    records._text(obj, "summary", 4000)


def _decision(obj):
    status = records._choice(obj, "status", STATUSES)
    records._text(obj, "question", 2000)
    records._text(obj, "choice", 2000)
    records._text(obj, "rationale", 4000)
    if "supersedes_id" in obj:
        records._slug(obj, "supersedes_id")
        if status != "superseded":
            raise ShitError(records.REJECTED)


_CHECKS = {"evidence": _evidence, "report": _report, "review": _review, "decision": _decision}


def load(kind, data):
    return validate(kind, records.parse(data))


def dump(kind, obj):
    validate(kind, obj)
    data = (json.dumps(obj, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
    if len(data) > records.MAX_RECORD_BYTES:
        raise ShitError(INPUT_LIMIT)
    return data
