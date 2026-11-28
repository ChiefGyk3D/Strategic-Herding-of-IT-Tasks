"""Strict parsing and validation of version 1 task and event-log records (docs/schemas.md)."""

import json
import re
from datetime import datetime

from ._text import check_text
from .errors import INPUT_LIMIT, REJECTED, STABLE_EXIT_CODES, ContractError, ShitError
from .lifecycle import ACTORS, STATES, check_transition

MAX_RECORD_BYTES = 65536
MAX_DEPTH = 8

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

EVENTS = (
    "task_created", "state_transition", "brief_recorded", "report_recorded",
    "decision_recorded", "evidence_recorded", "review_recorded", "command_failed",
)

TASK_REQUIRED = {
    "schema", "schema_version", "id", "title", "summary", "state", "owner",
    "created_at", "updated_at", "history",
}
TASK_OPTIONAL = {"blocked_reason", "blocking_item"}
TRANSITION_KEYS = {"from", "to", "actor", "at", "reason"}
EVENT_REQUIRED = {"schema", "schema_version", "id", "task_id", "timestamp", "actor", "event", "message"}
EVENT_OPTIONAL = {"record_id", "from_state", "to_state", "exit_code"}


def _reject():
    return ShitError(REJECTED)


def scan_depth(data):
    """Return the maximum JSON nesting depth of `data` (bytes), ignoring brackets inside strings.

    Raises ShitError(7) as soon as the depth exceeds MAX_DEPTH, so deep input never reaches the parser.
    """
    depth = deepest = 0
    in_string = escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            deepest = max(deepest, depth)
            if deepest > MAX_DEPTH:
                raise ShitError(INPUT_LIMIT)
        elif byte in (0x7D, 0x5D):
            depth -= 1
    return deepest


def _pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _no_constant(_name):
    raise ValueError("non-finite number")


def parse(data):
    """Parse one record's bytes into a dict. Size and depth are checked before parsing."""
    if len(data) > MAX_RECORD_BYTES:
        raise ShitError(INPUT_LIMIT)
    if data.startswith(b"\xef\xbb\xbf"):
        raise _reject()
    scan_depth(data)
    try:
        text = data.decode("utf-8")
        obj = json.loads(text, object_pairs_hook=_pairs, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _reject() from None
    if not isinstance(obj, dict):
        raise _reject()
    _check_strings(obj)
    return obj


def _check_strings(value):
    """Reject strings that are not valid Unicode (lone surrogates), in keys and values."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise _reject() from None
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_strings(key)
            _check_strings(item)
    elif isinstance(value, list):
        for item in value:
            _check_strings(item)


def _keys(obj, required, optional):
    keys = set(obj)
    if not required <= keys or not keys <= required | optional:
        raise _reject()


def _version(obj, name):
    version = obj.get("schema_version")
    if obj.get("schema") != name or type(version) is not int or version != 1:
        raise _reject()


def _text(obj, key, maximum=None):
    value = obj.get(key)
    if not isinstance(value, str) or value == "" or (maximum is not None and len(value) > maximum):
        raise _reject()
    try:
        check_text(value)  # control, bidi, zero-width and separator characters, as in artifacts.py
    except ContractError:
        raise _reject() from None
    return value


def _slug(obj, key):
    value = _text(obj, key)
    if not SLUG.fullmatch(value):
        raise _reject()
    return value


def _choice(obj, key, allowed):
    value = obj.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise _reject()
    return value


def _stamp(obj, key):
    value = _text(obj, key)
    if not TIMESTAMP.fullmatch(value):
        raise _reject()
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise _reject() from None
    return value


def validate_task(obj):
    _keys(obj, TASK_REQUIRED, TASK_OPTIONAL)
    _version(obj, "task")
    _slug(obj, "id")
    _text(obj, "title", 200)
    _text(obj, "summary", 4000)
    state = _choice(obj, "state", STATES)
    _choice(obj, "owner", ACTORS)
    created = _stamp(obj, "created_at")
    updated = _stamp(obj, "updated_at")
    if updated < created:
        raise _reject()
    if state == "blocked":
        _text(obj, "blocked_reason")
        _text(obj, "blocking_item")
    elif TASK_OPTIONAL & set(obj):
        raise _reject()
    history = obj["history"]
    if not isinstance(history, list):
        raise _reject()
    _validate_history(history, state, created)
    return obj


def _validate_history(history, state, created):
    previous_to = None
    previous_at = created
    for entry in history:
        if not isinstance(entry, dict):
            raise _reject()
        _keys(entry, TRANSITION_KEYS, set())
        frm = _choice(entry, "from", STATES)
        to = _choice(entry, "to", STATES)
        actor = _choice(entry, "actor", ACTORS)
        at = _stamp(entry, "at")
        _text(entry, "reason")
        check_transition(frm, to, actor)
        if (previous_to is not None and frm != previous_to) or at < previous_at:
            raise _reject()
        previous_to, previous_at = to, at
    if history and previous_to != state:
        raise _reject()


def validate_event(obj):
    _keys(obj, EVENT_REQUIRED, EVENT_OPTIONAL)
    _version(obj, "event-log")
    _slug(obj, "id")
    _slug(obj, "task_id")
    _stamp(obj, "timestamp")
    actor = _choice(obj, "actor", ACTORS)
    event = _choice(obj, "event", EVENTS)
    _text(obj, "message", 500)
    if "record_id" in obj:
        _slug(obj, "record_id")
    transition_keys = {"from_state", "to_state"} & set(obj)
    if event == "state_transition":
        if transition_keys != {"from_state", "to_state"}:
            raise _reject()
        frm = _choice(obj, "from_state", STATES)
        to = _choice(obj, "to_state", STATES)
        check_transition(frm, to, actor)
    elif transition_keys:
        raise _reject()
    if event == "command_failed":
        code = obj.get("exit_code")
        if type(code) is not int or code not in STABLE_EXIT_CODES:
            raise _reject()
    elif "exit_code" in obj:
        raise _reject()
    return obj


def load_task(data):
    return validate_task(parse(data))


def load_event(data):
    return validate_event(parse(data))


def dump_task(obj):
    validate_task(obj)
    data = (json.dumps(obj, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
    if len(data) > MAX_RECORD_BYTES:
        raise ShitError(INPUT_LIMIT)
    return data


def dump_event(obj):
    """One JSON Lines line without the trailing newline."""
    validate_event(obj)
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    if len(data) > MAX_RECORD_BYTES:
        raise ShitError(INPUT_LIMIT)
    return data
