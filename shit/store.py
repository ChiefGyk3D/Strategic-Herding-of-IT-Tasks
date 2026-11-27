"""SQLite store foundation (issue 59, epic 57; design in docs/store.md).

This is the only module that imports `sqlite3`. `connect` is the only way to open the database. It
refuses an unsafe file before opening it, applies every pragma, and verifies the schema fingerprint
before it hands the connection out. Queue operations are not here yet.

Rules for callers: every write goes through `write_transaction` (BEGIN IMMEDIATE, never a deferred
upgrade), SQL text is a literal with `?` parameters, and errors leave this module as `ShitError`
with a fixed message (never a path or SQL text).
"""

import contextlib
import functools
import hashlib
import os
import re
import sqlite3
import stat
import sys

from . import dependencies, lifecycle, queue_records, records
from .errors import (
    BUSY,
    CONFINEMENT,
    FAILED_CLOSED,
    ILLEGAL_TRANSITION,
    INTERNAL,
    LEASE_REFUSED,
    PRECONDITION,
    REJECTED,
    USAGE,
    ShitError,
)
from .log import GENESIS
from .project import _os_error

DB_NAME = "store.sqlite3"
DB_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600
_PRIVATE_FILE = stat.S_IRUSR | stat.S_IWUSR
_PRIVATE_DIR = stat.S_IRWXU
# ASCII "SHIT". A different application id in the header means this is not our database.
APPLICATION_ID = 0x53484954
SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 60000
MAX_BUSY_TIMEOUT_MS = 600000
MIN_SQLITE = (3, 37, 0)  # STRICT tables
MIN_PYTHON = (3, 11)  # sqlite_errorcode on exceptions

RECORD_KINDS = ("task", "brief", "report", "evidence", "review", "decision", "handoff")
MAX_TEXT = 500
MAX_RECORD_BYTES = records.MAX_RECORD_BYTES

# Extended result codes (sqlite.org/rescode.html), numeric so no constant has to exist in `sqlite3`.
_BUSY, _LOCKED, _READONLY, _IOERR, _CORRUPT, _FULL, _CANTOPEN, _NOTADB = 5, 6, 8, 10, 11, 13, 14, 26
_CONSTRAINT_FOREIGNKEY, _CONSTRAINT_TRIGGER = 787, 1811
_FAIL_CLOSED_PRIMARY = {_READONLY, _IOERR, _CORRUPT, _FULL, _CANTOPEN, _NOTADB}

# RAISE(ABORT, 'E<n>') tokens in the triggers below -> exit code. The token is the only text a trigger
# emits. A token not in this table is still refused with the fail-closed code.
TOKEN_EXIT = {
    "E1": FAILED_CLOSED,  # events: update
    "E2": FAILED_CLOSED,  # events: delete
    "E3": FAILED_CLOSED,  # queue_entries: update
    "E4": FAILED_CLOSED,  # queue_entries: delete
    "E5": FAILED_CLOSED,  # task_history: update
    "E6": FAILED_CLOSED,  # task_history: delete
    "E7": FAILED_CLOSED,  # tasks: delete
    "E8": ILLEGAL_TRANSITION,  # queue entry: state change the transition table does not allow
    "E9": LEASE_REFUSED,  # queue entry: lease missing or not the latest claim
    "E10": LEASE_REFUSED,  # queue entry: claim generation is not the previous one plus one
    "E11": FAILED_CLOSED,  # events or queue entries: hash chain link broken or time going backwards
    "E12": REJECTED,  # queue entry: task enqueued twice, or never enqueued
    "E13": LEASE_REFUSED,  # lease row that is not the latest claim
    "E14": LEASE_REFUSED,  # lease update that is not exactly one renewal
    "E15": LEASE_REFUSED,  # lease deleted while an apply slot is still held
    "E16": LEASE_REFUSED,  # apply slot without a matching apply-step lease, or edited
    "E17": REJECTED,  # started entry whose attempt is not the previous one plus one
}
_TOKEN = re.compile(r"E[0-9]{1,3}")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
_VOCAB = re.compile(r"[a-z0-9_-]{1,40}")


# -- floor and error mapping --------------------------------------------------


def _check_floor(sqlite_version=None, python_version=None):
    """Refuse (exit 8) an SQLite older than 3.37 or a Python older than 3.11."""
    sqlite_version = sqlite3.sqlite_version_info if sqlite_version is None else sqlite_version
    python_version = sys.version_info[:2] if python_version is None else python_version[:2]
    if tuple(sqlite_version) < MIN_SQLITE or tuple(python_version) < MIN_PYTHON:
        raise ShitError(FAILED_CLOSED)


def map_error(exc):
    """Map any exception to a ShitError by extended result code. Text of `exc` is never copied."""
    if isinstance(exc, ShitError):
        return exc
    if not isinstance(exc, sqlite3.Error):
        return ShitError(INTERNAL)
    extended = getattr(exc, "sqlite_errorcode", None)
    if not isinstance(extended, int):
        return ShitError(INTERNAL)
    primary = extended & 0xFF
    if isinstance(exc, sqlite3.IntegrityError):
        if extended == _CONSTRAINT_TRIGGER:
            token = exc.args[0] if exc.args and isinstance(exc.args[0], str) else ""
            return ShitError(TOKEN_EXIT.get(token, FAILED_CLOSED) if _TOKEN.fullmatch(token) else FAILED_CLOSED)
        if extended == _CONSTRAINT_FOREIGNKEY:
            return ShitError(PRECONDITION)
        return ShitError(REJECTED)
    if primary in (_BUSY, _LOCKED):
        return ShitError(BUSY)
    if primary in _FAIL_CLOSED_PRIMARY:
        return ShitError(FAILED_CLOSED)
    return ShitError(INTERNAL)


# -- schema -------------------------------------------------------------------

_STAMP_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z"
# Columns that only some queue-entry kinds carry. `queue_records.KINDS` says which kind may or must have which.
_EXTRA_COLUMNS = ("lease_id", "generation", "worker", "worker_kind", "step", "expires_at", "attempt", "not_before",
                  "hold_kind", "shards", "orphan_sha256")
# The state a task must be in before an entry of this kind (docs/queue-spec.md, "Allowed queue transitions").
_KIND_BEFORE = {
    "ready": ("waiting",), "claimed": ("ready",), "started": ("dispatched",), "released": ("dispatched",),
    "dispatch_failed": ("dispatched",), "submitted": ("running",), "apply_started": ("running",),
    "apply_finished": ("running",), "renewed": ("dispatched", "running"),
}
_SAME_STATE = ("renewed", "apply_started", "apply_finished", "orphan_removed")
_NO_PAIR_CHECK = ("enqueued", "graph_changed", "reopened", *_SAME_STATE)
_EXPIRED_PAIRS = (("dispatched", "ready"), ("running", "failed"))
_PREV_STATE = ("(SELECT q.state_after FROM queue_entries q WHERE q.task_id = NEW.task_id "
               "AND q.state_after IS NOT NULL ORDER BY q.seq DESC LIMIT 1)")
_LAST_CLAIM = ("(SELECT q.seq FROM queue_entries q WHERE q.task_id = NEW.task_id AND q.kind = 'claimed' "
               "ORDER BY q.seq DESC LIMIT 1)")
# Trigger conditions are written in this code only; this is the whole alphabet they may use (no quote
# escapes, semicolons, comments or double quotes).
_CONDITION = re.compile(r"[A-Za-z0-9_ ,()'<>=.*|+-]*")
_EVENTS = ("BEFORE INSERT", "BEFORE UPDATE", "BEFORE DELETE")


def _literal_list(values):
    """A SQL list of quoted words from the code's own vocabulary. Each word is validated, never escaped."""
    words = sorted(set(values))
    if not words or any(not _VOCAB.fullmatch(w) for w in words):
        raise ValueError("vocabulary word outside the allowed shape")
    return ", ".join("'" + w + "'" for w in words)


def _name(column):
    if not _NAME.fullmatch(column):
        raise ValueError("name outside the allowed shape")
    return column


def _slug_check(column):
    _name(column)
    return (column + " GLOB '[a-z0-9]*' AND " + column + " NOT GLOB '*[^a-z0-9-]*' AND length(" + column
            + ") <= 64")


def _hex_check(column, length):
    if type(length) is not int:
        raise ValueError("bad argument")
    return ("length(" + _name(column) + ") = " + str(length) + " AND " + column + " NOT GLOB '*[^0-9a-f]*'")


def _stamp_check(column):
    """Fixed-width UTC stamp that is also a real calendar time (9999-99-99 is refused)."""
    _name(column)
    # coalesce: strftime answers NULL for an impossible date, and a CHECK that evaluates to NULL passes.
    return (column + " GLOB '" + _STAMP_GLOB + "' AND coalesce(strftime('%Y-%m-%dT%H:%M:%SZ', " + column + ") = "
            + column + ", 0)")


def _text_check(column, maximum):
    _name(column)
    return "length(" + column + ") BETWEEN 1 AND " + str(int(maximum))


def _seconds(column):
    return "CAST(strftime('%s', " + _name(column) + ") AS INTEGER)"


def _pair_rows(pairs):
    for pair in pairs:
        if not all(_VOCAB.fullmatch(w) for w in pair):
            raise ValueError("vocabulary word outside the allowed shape")
    return ", ".join("('" + a + "', '" + b + "')" for a, b in sorted(pairs))


def _no_pair(pairs):
    """True when the task's previous queue state and the new state_after are not one of `pairs`."""
    return ("NOT EXISTS (SELECT 1 FROM (VALUES " + _pair_rows(pairs) + ") AS t WHERE t.column1 = " + _PREV_STATE
            + " AND t.column2 = NEW.state_after)")


def _kind_rules():
    """One alternative per queue-entry kind: its actors, state, required and forbidden columns."""
    alternatives = []
    for kind, (required, optional, actors, states) in sorted(queue_records.KINDS.items()):
        if not (required | optional) <= set(_EXTRA_COLUMNS):
            raise ValueError("a queue-entry field has no column")
        parts = ["kind = '" + kind + "'", "actor IN (" + _literal_list(actors) + ")"]
        parts.append("state_after IN (" + _literal_list(states) + ")" if states else "state_after IS NULL")
        for column in _EXTRA_COLUMNS:
            if column in required:
                parts.append(column + " IS NOT NULL")
            elif column not in optional:
                parts.append(column + " IS NULL")
        alternatives.append("(" + " AND ".join(parts) + ")")
    return " OR ".join(alternatives)


def _guard(name, event, table, condition, token):
    """A trigger that aborts with a fixed token. `condition` is code-written text from a small alphabet."""
    _name(name)
    _name(table)
    if event not in _EVENTS or token not in TOKEN_EXIT:
        raise ValueError("bad trigger")
    clause = ""
    if condition is not None:
        if not _CONDITION.fullmatch(condition) or "--" in condition:
            raise ValueError("trigger condition outside the allowed alphabet")
        clause = " WHEN " + condition
    return "CREATE TRIGGER " + name + " " + event + " ON " + table + clause + " BEGIN SELECT RAISE(ABORT, '" + token + "'); END"


def _append_only(table, update_token, delete_token):
    return (_guard(table + "_no_update", "BEFORE UPDATE", table, None, update_token),
            _guard(table + "_no_delete", "BEFORE DELETE", table, None, delete_token))


def _chain_guard(table):
    """A new row links to the previous row's hash and never goes back in time."""
    last = "(SELECT " + "{col}" + " FROM " + _name(table) + " ORDER BY seq DESC LIMIT 1)"
    tip_hash = last.replace("{col}", "hash")
    tip_time = last.replace("{col}", "timestamp")
    condition = ("NEW.prev_hash IS NOT coalesce(" + tip_hash + ", '" + GENESIS + "') OR NEW.timestamp < coalesce("
                 + tip_time + ", '')")
    return _guard(table + "_chain", "BEFORE INSERT", table, condition, "E11")


def _queue_guards():
    guards = []
    entries = "queue_entries"
    seen = "EXISTS (SELECT 1 FROM queue_entries q WHERE q.task_id = NEW.task_id AND q.kind = 'enqueued')"
    guards.append(_guard("queue_entries_enqueue_once", "BEFORE INSERT", entries, "NEW.kind = 'enqueued' AND " + seen, "E12"))
    guards.append(_guard("queue_entries_needs_enqueue", "BEFORE INSERT", entries,
                         "NEW.kind NOT IN ('enqueued', 'graph_changed') AND NOT " + seen, "E12"))
    guards.append(_guard("queue_entries_transition", "BEFORE INSERT", entries,
                         "NEW.kind NOT IN (" + _literal_list(_NO_PAIR_CHECK) + ") AND " + _no_pair(queue_records.ALLOWED),
                         "E8"))
    guards.append(_guard("queue_entries_reopen", "BEFORE INSERT", entries,
                         "NEW.kind = 'reopened' AND "
                         + _no_pair(queue_records.ALLOWED | queue_records.REENTER_WAITING), "E8"))
    guards.append(_guard("queue_entries_expire", "BEFORE INSERT", entries,
                         "NEW.kind = 'expired' AND " + _no_pair(frozenset(_EXPIRED_PAIRS)), "E8"))
    for kind, before in sorted(_KIND_BEFORE.items()):
        guards.append(_guard("queue_entries_before_" + kind, "BEFORE INSERT", entries,
                             "NEW.kind = '" + kind + "' AND (" + _PREV_STATE + " IS NULL OR " + _PREV_STATE
                             + " NOT IN (" + _literal_list(before) + "))", "E8"))
    guards.append(_guard("queue_entries_same_state", "BEFORE INSERT", entries,
                         "NEW.kind IN (" + _literal_list(_SAME_STATE) + ") AND NEW.state_after IS NOT " + _PREV_STATE,
                         "E8"))
    guards.append(_guard("queue_entries_lease_presence", "BEFORE INSERT", entries,
                         "NEW.kind IN ('failed', 'held') AND (NEW.lease_id IS NOT NULL) <> coalesce("
                         + _PREV_STATE + " IN ('dispatched', 'running'), 0)", "E9"))
    guards.append(_guard("queue_entries_claim_generation", "BEFORE INSERT", entries,
                         "NEW.kind = 'claimed' AND NEW.generation IS NOT coalesce((SELECT max(q.generation) FROM "
                         "queue_entries q WHERE q.task_id = NEW.task_id AND q.kind = 'claimed'), 0) + 1", "E10"))
    guards.append(_guard("queue_entries_lease_match", "BEFORE INSERT", entries,
                         "NEW.lease_id IS NOT NULL AND NEW.kind NOT IN ('claimed', 'orphan_removed') AND NOT EXISTS "
                         "(SELECT 1 FROM queue_entries q WHERE q.seq = " + _LAST_CLAIM
                         + " AND q.lease_id = NEW.lease_id AND q.generation IS NEW.generation)", "E9"))
    guards.append(_guard("queue_entries_attempt", "BEFORE INSERT", entries,
                         "NEW.kind = 'started' AND NEW.attempt IS NOT (SELECT count(*) FROM queue_entries q WHERE "
                         "q.task_id = NEW.task_id AND q.kind = 'started') + 1", "E17"))
    return guards


def _lease_guards():
    claim = ("(SELECT q.seq FROM queue_entries q WHERE q.task_id = NEW.task_id AND q.kind = 'claimed' "
             "ORDER BY q.seq DESC LIMIT 1)")
    return [
        _guard("leases_match_claim", "BEFORE INSERT", "leases",
               "NOT EXISTS (SELECT 1 FROM queue_entries q WHERE q.seq = " + claim
               + " AND q.lease_id = NEW.lease_id AND q.generation = NEW.generation AND q.step = NEW.step "
               "AND q.worker = NEW.worker AND q.worker_kind = NEW.worker_kind AND q.expires_at = NEW.expires_at "
               "AND q.timestamp = NEW.claimed_at)", "E13"),
        _guard("leases_fixed_fields", "BEFORE UPDATE", "leases",
               "NEW.task_id IS NOT OLD.task_id OR NEW.lease_id IS NOT OLD.lease_id OR NEW.generation IS NOT "
               "OLD.generation OR NEW.step IS NOT OLD.step OR NEW.worker IS NOT OLD.worker OR NEW.worker_kind IS NOT "
               "OLD.worker_kind OR NEW.claimed_at IS NOT OLD.claimed_at OR NEW.expires_at <= OLD.expires_at OR "
               "NEW.renewals <> OLD.renewals + 1", "E14"),
        _guard("leases_slots_first", "BEFORE DELETE", "leases",
               "EXISTS (SELECT 1 FROM apply_slots s WHERE s.task_id = OLD.task_id)", "E15"),
        _guard("apply_slots_match_lease", "BEFORE INSERT", "apply_slots",
               "NOT EXISTS (SELECT 1 FROM leases l WHERE l.task_id = NEW.task_id AND l.lease_id = NEW.lease_id "
               "AND l.generation = NEW.generation AND l.step = 'apply')", "E16"),
        _guard("apply_slots_no_update", "BEFORE UPDATE", "apply_slots", None, "E16"),
    ]


def schema_statements():
    """The DDL, in creation order, plus the version pragmas. Built only from this code's vocabulary."""
    states = _literal_list(lifecycle.STATES)
    actors = _literal_list(lifecycle.ACTORS)
    qstates = _literal_list(queue_records.STATES)
    kinds = _literal_list(queue_records.KINDS)  # every kind appears in the per-kind rules below too
    steps = _literal_list(queue_records.STEPS)
    worker_kinds = _literal_list(queue_records.WORKER_KINDS)
    dep_types = _literal_list(dependencies.TYPES)
    events = _literal_list(records.EVENTS)
    record_kinds = _literal_list(RECORD_KINDS)
    _pair_rows(lifecycle.LEGAL)  # validates every state word
    legal = " OR ".join(
        "(from_state = '" + a + "' AND to_state = '" + b + "' AND actor IN (" + _literal_list(who) + "))"
        for (a, b), who in sorted(lifecycle.LEGAL.items(), key=lambda item: item[0]))
    ttl = str(queue_records.MAX_TTL)
    return list(_flatten([
        "CREATE TABLE meta (key TEXT NOT NULL PRIMARY KEY CHECK (" + _slug_check("key") + "), "
        "value TEXT NOT NULL) STRICT, WITHOUT ROWID",

        "CREATE TABLE tasks (id TEXT NOT NULL PRIMARY KEY CHECK (" + _slug_check("id") + "), "
        "state TEXT NOT NULL CHECK (state IN (" + states + ")), "
        "owner TEXT NOT NULL CHECK (owner IN (" + actors + ")), "
        "created_at TEXT NOT NULL CHECK (" + _stamp_check("created_at") + "), "
        "updated_at TEXT NOT NULL CHECK (" + _stamp_check("updated_at") + " AND updated_at >= created_at), "
        "blocked_reason TEXT CHECK (blocked_reason IS NULL OR length(blocked_reason) BETWEEN 1 AND 500), "
        "blocking_item TEXT CHECK (blocking_item IS NULL OR length(blocking_item) BETWEEN 1 AND 500), "
        "CHECK ((state = 'blocked') = (blocked_reason IS NOT NULL AND blocking_item IS NOT NULL)), "
        "CHECK (state = 'blocked' OR (blocked_reason IS NULL AND blocking_item IS NULL))) STRICT",

        "CREATE TABLE task_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT NOT NULL REFERENCES tasks (id), "
        "from_state TEXT NOT NULL CHECK (from_state IN (" + states + ")), "
        "to_state TEXT NOT NULL CHECK (to_state IN (" + states + ")), "
        "actor TEXT NOT NULL CHECK (actor IN (" + actors + ")), "
        "at TEXT NOT NULL CHECK (" + _stamp_check("at") + "), "
        "reason TEXT NOT NULL CHECK (" + _text_check("reason", MAX_TEXT) + "), "
        "CHECK (" + legal + ")) STRICT",

        "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "id TEXT NOT NULL UNIQUE CHECK (" + _slug_check("id") + "), "
        "task_id TEXT NOT NULL REFERENCES tasks (id), "
        "timestamp TEXT NOT NULL CHECK (" + _stamp_check("timestamp") + "), "
        "actor TEXT NOT NULL CHECK (actor IN (" + actors + ")), "
        "event TEXT NOT NULL CHECK (event IN (" + events + ")), "
        "message TEXT NOT NULL CHECK (" + _text_check("message", MAX_TEXT) + "), "
        "record_id TEXT CHECK (record_id IS NULL OR (" + _slug_check("record_id") + ")), "
        "from_state TEXT CHECK (from_state IS NULL OR from_state IN (" + states + ")), "
        "to_state TEXT CHECK (to_state IS NULL OR to_state IN (" + states + ")), "
        "exit_code INTEGER CHECK (exit_code IS NULL OR exit_code BETWEEN 1 AND 255), "
        "prev_hash TEXT NOT NULL CHECK (" + _hex_check("prev_hash", 64) + "), "
        "hash TEXT NOT NULL UNIQUE CHECK (" + _hex_check("hash", 64) + "), "
        "CHECK ((event = 'state_transition') = (from_state IS NOT NULL AND to_state IS NOT NULL)), "
        "CHECK (event = 'state_transition' OR (from_state IS NULL AND to_state IS NULL)), "
        "CHECK ((event = 'command_failed') = (exit_code IS NOT NULL))) STRICT",

        "CREATE TABLE records (kind TEXT NOT NULL CHECK (kind IN (" + record_kinds + ")), "
        "record_id TEXT NOT NULL CHECK (" + _slug_check("record_id") + "), "
        "task_id TEXT NOT NULL REFERENCES tasks (id), "
        "sha256 TEXT NOT NULL CHECK (" + _hex_check("sha256", 64) + "), "
        "size INTEGER NOT NULL CHECK (size BETWEEN 0 AND " + str(MAX_RECORD_BYTES) + "), "
        "created_at TEXT NOT NULL CHECK (" + _stamp_check("created_at") + "), "
        "PRIMARY KEY (kind, record_id)) STRICT, WITHOUT ROWID",

        "CREATE TABLE queue_entries (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "id TEXT NOT NULL UNIQUE CHECK (" + _slug_check("id") + "), "
        "task_id TEXT NOT NULL REFERENCES tasks (id), "
        "timestamp TEXT NOT NULL CHECK (" + _stamp_check("timestamp") + "), "
        "actor TEXT NOT NULL CHECK (actor IN (" + actors + ")), "
        "kind TEXT NOT NULL CHECK (kind IN (" + kinds + ")), "
        "state_after TEXT CHECK (state_after IS NULL OR state_after IN (" + qstates + ")), "
        "lease_id TEXT CHECK (lease_id IS NULL OR (" + _hex_check("lease_id", 32) + ")), "
        "generation INTEGER CHECK (generation IS NULL OR generation >= 1), "
        "worker TEXT CHECK (worker IS NULL OR (" + _slug_check("worker") + ")), "
        "worker_kind TEXT CHECK (worker_kind IS NULL OR worker_kind IN (" + worker_kinds + ")), "
        "step TEXT CHECK (step IS NULL OR step IN (" + steps + ")), "
        "hold_kind TEXT CHECK (hold_kind IS NULL OR hold_kind IN (" + _literal_list(queue_records.HOLD_KINDS) + ")), "
        "not_before TEXT CHECK (not_before IS NULL OR (" + _stamp_check("not_before") + ")), "
        "attempt INTEGER CHECK (attempt IS NULL OR attempt >= 1), "
        "expires_at TEXT CHECK (expires_at IS NULL OR (" + _stamp_check("expires_at") + ")), "
        "orphan_sha256 TEXT CHECK (orphan_sha256 IS NULL OR (" + _hex_check("orphan_sha256", 64) + ")), "
        "message TEXT CHECK (message IS NULL OR (" + _text_check("message", MAX_TEXT) + ")), "
        "shards TEXT CHECK (shards IS NULL OR length(shards) BETWEEN 1 AND " + str(MAX_RECORD_BYTES) + "), "
        "prev_hash TEXT NOT NULL CHECK (" + _hex_check("prev_hash", 64) + "), "
        "hash TEXT NOT NULL UNIQUE CHECK (" + _hex_check("hash", 64) + "), "
        "CHECK (" + _kind_rules() + "), "
        "CHECK (kind NOT IN ('failed', 'held') OR ((lease_id IS NULL) = (generation IS NULL))), "
        "CHECK (kind <> 'held' OR state_after = 'hold-' || hold_kind), "
        "CHECK (kind <> 'claimed' OR (expires_at > timestamp AND " + _seconds("expires_at") + " - "
        + _seconds("timestamp") + " <= " + ttl + "))) STRICT",

        "CREATE TABLE leases (task_id TEXT NOT NULL PRIMARY KEY REFERENCES tasks (id), "
        "lease_id TEXT NOT NULL UNIQUE CHECK (" + _hex_check("lease_id", 32) + "), "
        "generation INTEGER NOT NULL CHECK (generation >= 1), "
        "step TEXT NOT NULL CHECK (step IN (" + steps + ")), "
        "worker TEXT NOT NULL CHECK (" + _slug_check("worker") + "), "
        "worker_kind TEXT NOT NULL CHECK (worker_kind IN (" + worker_kinds + ")), "
        "claimed_at TEXT NOT NULL CHECK (" + _stamp_check("claimed_at") + "), "
        "expires_at TEXT NOT NULL CHECK (" + _stamp_check("expires_at") + " AND expires_at > claimed_at AND "
        + _seconds("expires_at") + " - " + _seconds("claimed_at") + " <= " + str(queue_records.MAX_LIFETIME) + "), "
        "renewals INTEGER NOT NULL CHECK (renewals BETWEEN 0 AND " + str(queue_records.MAX_RENEWALS)
        + ")) STRICT",

        "CREATE TABLE edges (task_id TEXT NOT NULL REFERENCES tasks (id), "
        "needs_id TEXT NOT NULL REFERENCES tasks (id), "
        "type TEXT NOT NULL CHECK (type IN (" + dep_types + ")), "
        "PRIMARY KEY (task_id, needs_id), CHECK (task_id <> needs_id)) STRICT, WITHOUT ROWID",

        "CREATE TABLE apply_slots (target TEXT NOT NULL PRIMARY KEY CHECK (length(target) BETWEEN 1 AND 500), "
        "task_id TEXT NOT NULL REFERENCES tasks (id), "
        "lease_id TEXT NOT NULL CHECK (" + _hex_check("lease_id", 32) + "), "
        "generation INTEGER NOT NULL CHECK (generation >= 1), "
        "acquired_at TEXT NOT NULL CHECK (" + _stamp_check("acquired_at") + ")) STRICT, WITHOUT ROWID",

        _append_only("events", "E1", "E2"),
        _append_only("queue_entries", "E3", "E4"),
        _append_only("task_history", "E5", "E6"),
        _guard("tasks_no_delete", "BEFORE DELETE", "tasks", None, "E7"),
        _chain_guard("events"),
        _chain_guard("queue_entries"),
        _queue_guards(),
        _lease_guards(),

        "PRAGMA application_id = " + str(int(APPLICATION_ID)),
        "PRAGMA user_version = " + str(int(SCHEMA_VERSION)),
    ]))


def _flatten(statements):
    for item in statements:
        if isinstance(item, (tuple, list)):
            yield from _flatten(item)
        else:
            yield item


# -- fingerprint --------------------------------------------------------------


def _fingerprint_rows(rows):
    """sha256 over length-prefixed (type, name, tbl_name, sql) rows, so no two row sets share a byte string."""
    digest = hashlib.sha256()
    for row in rows:
        for cell in row:
            data = b"\x00" if cell is None else cell.encode("utf-8")
            digest.update(str(len(data)).encode("ascii") + b":" + data)
        digest.update(b"\n")
    return digest.hexdigest()


def fingerprint(conn):
    """The fingerprint of the schema objects of the open database, in a fixed order."""
    rows = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name, tbl_name").fetchall()
    return _fingerprint_rows(rows)


@functools.lru_cache(maxsize=1)
def expected_fingerprint():
    """The fingerprint the DDL in this code produces, computed once on a scratch in-memory database."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for sql in schema_statements():
            conn.execute(sql)
        return fingerprint(conn)
    finally:
        conn.close()


# -- opening ------------------------------------------------------------------


def _timeout(value):
    if value is None:
        return DEFAULT_BUSY_TIMEOUT_MS
    if type(value) is not int or not 0 <= value <= MAX_BUSY_TIMEOUT_MS:
        raise ShitError(USAGE)
    return value


def _db_path(project):
    return os.path.join(project._root, DB_NAME)


def _check_file(st):
    """Regular file, one link, owned by this user, permission bits within owner read and write.

    The whole mode is compared, so setuid, setgid, sticky, owner-execute, group and other bits all refuse."""
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) & ~_PRIVATE_FILE):
        raise ShitError(CONFINEMENT)


def _lstat(path, required):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise ShitError(PRECONDITION) from None
        return None
    except OSError as exc:
        raise _os_error(exc) from None
    _check_file(st)
    return st


def _check_directory(project):
    try:
        st = os.fstat(project._fd)
    except OSError as exc:
        raise _os_error(exc) from None
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) & ~_PRIVATE_DIR):
        raise ShitError(CONFINEMENT)


def _opened_identities(path):
    """(device, inode) of every descriptor this process holds on `path`, or None without /proc/self/fd."""
    try:
        names = os.listdir("/proc/self/fd")
    except OSError:
        return None
    found = set()
    for name in names:
        link = "/proc/self/fd/" + name
        try:
            target = os.readlink(link)
            if target not in (path, path + " (deleted)"):
                continue
            st = os.stat(link)  # follows the descriptor link: the file that is actually open
        except OSError:
            continue
        found.add((st.st_dev, st.st_ino))
    return found


def _verify_opened(path, before):
    """Refuse unless the file sqlite opened is the file `before` described (docs/store.md, residual window)."""
    want = (before.st_dev, before.st_ino)
    opened = _opened_identities(path)
    if opened is None:  # no /proc: compare again by path, a weaker check
        after = _lstat(path, required=True)
        if (after.st_dev, after.st_ino) != want:
            raise ShitError(CONFINEMENT)
        return
    if opened != {want}:
        raise ShitError(CONFINEMENT)


def _uri(path):
    """A file URI for `path` that opens an existing file only (mode=rw never creates). Only % ? # need escaping."""
    return "file:" + path.replace("%", "%25").replace("?", "%3f").replace("#", "%23") + "?mode=rw"


class _SidecarMoved(Exception):
    """A -wal or -shm file changed identity while the database was being opened (retried once)."""


_SIDECARS = ("-wal", "-shm")


def _verify_sidecars(path, snapshot, final):
    """After open: every sidecar that exists must be a safe file and the same file that was there before the open."""
    for suffix in _SIDECARS:
        sidecar = path + suffix
        now = _lstat(sidecar, required=False)  # a wide mode, link or foreign owner refuses here
        if now is None:
            continue
        before = snapshot[suffix]
        if before is not None and (before.st_dev, before.st_ino) != (now.st_dev, now.st_ino):
            if final:
                raise ShitError(CONFINEMENT)
            raise _SidecarMoved


def _open(project, busy_timeout_ms, initialize):
    _check_floor()
    timeout = _timeout(busy_timeout_ms)
    # A sidecar can legitimately be replaced when another process closes the last connection while this one
    # opens. One retry takes a fresh snapshot; a second change refuses.
    for final in (False, True):
        try:
            return _open_once(project, timeout, initialize, final)
        except _SidecarMoved:
            continue
    raise ShitError(CONFINEMENT)


def _open_once(project, timeout, initialize, final):
    _check_directory(project)
    path = _db_path(project)
    before = _lstat(path, required=True)
    snapshot = {suffix: _lstat(path + suffix, required=False) for suffix in _SIDECARS}
    conn = None
    try:
        # (ms + 0.5) / 1000 so the conversion to whole milliseconds inside sqlite3 never rounds down.
        conn = sqlite3.connect(_uri(path), uri=True, timeout=(timeout + 0.5) / 1000, isolation_level=None)
        _verify_opened(path, before)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA trusted_schema = OFF")
        if hasattr(conn, "setconfig") and hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        if initialize:
            _install_schema(conn)
        _check_header(conn)
        _require_wal(conn.execute("PRAGMA journal_mode = WAL").fetchone())
        conn.execute("PRAGMA synchronous = FULL")
        _check_settings(conn, timeout)
        if fingerprint(conn) != expected_fingerprint():
            raise ShitError(FAILED_CLOSED)
        _verify_sidecars(path, snapshot, final)
        return conn
    except BaseException as exc:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        if isinstance(exc, sqlite3.Error):
            raise map_error(exc) from None
        raise


def _require_wal(row):
    """The journal_mode pragma answers with the mode in force. Anything but wal (a filesystem that cannot do it) is refused."""
    if not row or str(row[0]).lower() != "wal":
        raise ShitError(FAILED_CLOSED)


def _check_header(conn):
    app = conn.execute("PRAGMA application_id").fetchone()[0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if app != APPLICATION_ID or version != SCHEMA_VERSION:
        raise ShitError(FAILED_CLOSED)


def _check_settings(conn, timeout):
    expected = (("PRAGMA foreign_keys", 1), ("PRAGMA synchronous", 2), ("PRAGMA trusted_schema", 0),
                ("PRAGMA busy_timeout", timeout))
    for sql, value in expected:
        if conn.execute(sql).fetchone()[0] != value:
            raise ShitError(FAILED_CLOSED)


def _install_schema(conn):
    """Create the schema in one immediate transaction, only when the file is a brand-new empty database."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        empty = (conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
                 and conn.execute("PRAGMA application_id").fetchone()[0] == 0
                 and conn.execute("PRAGMA user_version").fetchone()[0] == 0)
        if empty:
            for sql in schema_statements():
                conn.execute(sql)
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def init_store(project, busy_timeout_ms=None):
    """Create the database as a 0600 file (umask 077) and its schema, once. Safe to call again.

    The umask is process-wide while this runs; call it from a single-threaded setup step.
    """
    _check_floor()
    _check_directory(project)
    old = os.umask(stat.S_IRWXG | stat.S_IRWXO)
    try:
        try:
            fd = os.open(DB_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, DB_MODE,
                         dir_fd=project._fd)
        except FileExistsError:
            fd = None
        except OSError as exc:
            raise _os_error(exc) from None
        if fd is not None:
            try:
                os.fchmod(fd, DB_MODE)
            finally:
                os.close(fd)
        _open(project, busy_timeout_ms, initialize=True).close()
    finally:
        os.umask(old)


def connect(project, busy_timeout_ms=None):
    """Open the project's store. The one and only way to get a connection."""
    return _open(project, busy_timeout_ms, initialize=False)


def _abandon(conn):
    """Close a connection whose transaction state cannot be trusted, so nothing can reuse it."""
    with contextlib.suppress(Exception):
        conn.close()


@contextlib.contextmanager
def write_transaction(conn):
    """BEGIN IMMEDIATE, then commit on success or roll back on any exception (including KeyboardInterrupt).

    If the rollback itself fails, or the connection is still inside a transaction afterwards, the connection
    is closed and exit code 8 is raised: a caller never gets back a connection in an unknown state.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise map_error(exc) from None
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException as exc:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            clean = not conn.in_transaction
        except sqlite3.Error:
            clean = False
        if not clean:
            _abandon(conn)
            raise ShitError(FAILED_CLOSED) from None
        if isinstance(exc, sqlite3.Error):
            raise map_error(exc) from None
        raise
