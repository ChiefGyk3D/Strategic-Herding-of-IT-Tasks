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
    STABLE_EXIT_CODES,
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
    "E18": LEASE_REFUSED,  # renewal outside its window, or worker, step or lease row that disagree
    "E19": ILLEGAL_TRANSITION,  # apply marker for a non-apply claim, repeated, or out of order
    "E20": FAILED_CLOSED,  # events or queue entries: sequence number is not the previous one plus one
    "E21": REJECTED,  # queue entry whose id is not `queue-` and its sequence number
    "E22": REJECTED,  # graph_changed entry whose shards are not a sorted list of task id and digest objects
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
_CONDITION = re.compile(r"[A-Za-z0-9_ ,()'<>=.*|+%$^{}\[\]-]*")
_EVENTS = ("BEFORE INSERT", "BEFORE UPDATE", "BEFORE DELETE", "AFTER INSERT")


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


def _no_nul(column):
    """No embedded NUL byte: length(), GLOB and friends stop at one, so every text check needs this first."""
    return "instr(CAST(" + _name(column) + " AS BLOB), X'00') = 0"


def _slug_check(column):
    return (_no_nul(column) + " AND " + column + " GLOB '[a-z0-9]*' AND " + column + " NOT GLOB '*[^a-z0-9-]*' AND length("
            + column + ") <= 64")


def _hex_check(column, length):
    if type(length) is not int:
        raise ValueError("bad argument")
    return (_no_nul(column) + " AND length(" + column + ") = " + str(length) + " AND " + column
            + " NOT GLOB '*[^0-9a-f]*'")


def _stamp_check(column):
    """Fixed-width UTC stamp that is also a real calendar time (9999-99-99 is refused)."""
    _name(column)
    # coalesce: strftime answers NULL for an impossible date, and a CHECK that evaluates to NULL passes.
    # Year 0000 and hour 24 are refused to match the Python validators (SQLite accepts both).
    return (_no_nul(column) + " AND " + column + " GLOB '" + _STAMP_GLOB + "' AND coalesce(strftime('%Y-%m-%dT%H:%M:%SZ', "
            + column + ") = " + column + ", 0) AND substr(" + column + ", 1, 4) >= '0001' AND substr(" + column
            + ", 12, 2) <= '23'")


def _text_check(column, maximum):
    _name(column)
    return _no_nul(column) + " AND length(" + column + ") BETWEEN 1 AND " + str(int(maximum))


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


def _sec(expression):
    """Whole seconds of a fixed-width stamp expression."""
    return "CAST(strftime('%s', " + expression + ") AS INTEGER)"


def _claim(column):
    """A column of the task's latest claim entry."""
    return "(SELECT c." + _name(column) + " FROM queue_entries c WHERE c.seq = " + _LAST_CLAIM + ")"


_LAST_EXPIRY = ("(SELECT x.expires_at FROM queue_entries x WHERE x.task_id = NEW.task_id "
                "AND x.kind IN ('claimed', 'renewed') ORDER BY x.seq DESC LIMIT 1)")
_RENEWED_SO_FAR = ("(SELECT count(*) FROM queue_entries r WHERE r.task_id = NEW.task_id AND r.kind = 'renewed' "
                   "AND r.seq > " + _LAST_CLAIM + ")")


def _since_claim(kind):
    return ("EXISTS (SELECT 1 FROM queue_entries a WHERE a.task_id = NEW.task_id AND a.kind = '" + _name(kind)
            + "' AND a.seq > " + _LAST_CLAIM + ")")


def _lease_consistency_guards():
    """Entry fields against the claim they belong to and against the lease row, in either write order."""
    entries = "queue_entries"
    row = "EXISTS (SELECT 1 FROM leases l WHERE l.task_id = NEW.task_id)"
    return [
        _guard("queue_entries_start_matches_claim", "BEFORE INSERT", entries,
               "NEW.kind IN ('started', 'renewed') AND (NEW.worker IS NOT " + _claim("worker")
               + " OR NEW.step IS NOT " + _claim("step") + ")", "E9"),
        _guard("queue_entries_renewal_window", "BEFORE INSERT", entries,
               "NEW.kind = 'renewed' AND (NEW.expires_at <= " + _LAST_EXPIRY + " OR NEW.timestamp >= " + _LAST_EXPIRY
               + " OR " + _sec("NEW.expires_at") + " - " + _sec("NEW.timestamp") + " > " + str(queue_records.MAX_TTL)
               + " OR " + _sec("NEW.expires_at") + " - " + _sec(_LAST_EXPIRY) + " > " + str(queue_records.MAX_TTL)
               + " OR " + _sec("NEW.expires_at") + " - " + _sec(_claim("timestamp")) + " > "
               + str(queue_records.MAX_LIFETIME) + " OR " + _RENEWED_SO_FAR + " + 1 > "
               + str(queue_records.MAX_RENEWALS) + ")", "E18"),
        _guard("queue_entries_apply_claim", "BEFORE INSERT", entries,
               "NEW.kind IN ('apply_started', 'apply_finished') AND " + _claim("step") + " IS NOT 'apply'", "E19"),
        _guard("queue_entries_apply_start_once", "BEFORE INSERT", entries,
               "NEW.kind = 'apply_started' AND " + _since_claim("apply_started"), "E19"),
        _guard("queue_entries_apply_finish_after_start", "BEFORE INSERT", entries,
               "NEW.kind = 'apply_finished' AND (NOT " + _since_claim("apply_started") + " OR "
               + _since_claim("apply_finished") + ")", "E19"),
        _guard("queue_entries_no_lease_to_reopen", "BEFORE INSERT", entries,
               "NEW.kind IN ('reopened', 'done') AND (" + _PREV_STATE + " IN ('dispatched', 'running') OR " + row + ")", "E8"),
        _guard("queue_entries_claim_needs_no_lease_row", "BEFORE INSERT", entries,
               "NEW.kind = 'claimed' AND " + row, "E10"),
        _guard("queue_entries_claim_after_not_before", "BEFORE INSERT", entries,
               "NEW.kind = 'claimed' AND NEW.timestamp < coalesce((SELECT d.not_before FROM queue_entries d WHERE "
               "d.task_id = NEW.task_id AND d.kind IN ('dispatch_failed', 'started') ORDER BY d.seq DESC LIMIT 1), '')",
               "E10"),
        _guard("queue_entries_renewal_matches_lease_row", "BEFORE INSERT", entries,
               "NEW.kind = 'renewed' AND " + row + " AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.task_id = "
               "NEW.task_id AND ((l.expires_at = NEW.expires_at AND l.renewals = " + _RENEWED_SO_FAR + " + 1) OR "
               "(l.expires_at = " + _LAST_EXPIRY + " AND l.renewals = " + _RENEWED_SO_FAR + ")))", "E18"),
    ]


def _seq_guard(table):
    """A new row's sequence number is the previous maximum plus one (the first is 1), however it was supplied."""
    condition = ("NEW.seq IS NOT coalesce((SELECT max(s.seq) FROM " + _name(table) + " s WHERE s.seq <> NEW.seq), 0) + 1")
    return _guard(table + "_seq", "AFTER INSERT", table, condition, "E20")


_SHARD_FIELDS_BAD = (
    "(SELECT count(*) FROM json_each(j.value)) <> 2 "
    "OR json_type(j.value, '$.task_id') IS NOT 'text' OR json_type(j.value, '$.sha256') IS NOT 'text' "
    "OR length(json_extract(j.value, '$.sha256')) <> 64 OR json_extract(j.value, '$.sha256') GLOB '*[^0-9a-f]*' "
    "OR json_extract(j.value, '$.task_id') NOT GLOB '[a-z0-9]*' OR json_extract(j.value, '$.task_id') GLOB '*[^a-z0-9-]*' "
    "OR length(json_extract(j.value, '$.task_id')) > 64")
# Element checks only run on objects (json_extract on a bare string raises), so every use is inside a CASE.
_SHARD_BAD = "j.type IS NOT 'object' OR CASE WHEN j.type = 'object' THEN (" + _SHARD_FIELDS_BAD + ") ELSE 1 END"
_SHARD_ORDER_BAD = ("EXISTS (SELECT 1 FROM json_each(NEW.shards) a JOIN json_each(NEW.shards) b ON b.key = a.key + 1 "
                    "WHERE json_extract(b.value, '$.task_id') <= json_extract(a.value, '$.task_id')) "
                    "OR json_extract(NEW.shards, '$[0].task_id') IS NOT NEW.task_id")


def _shards_guard():
    """graph_changed shards: a sorted array of at most 32 {task_id, sha256} objects whose first task is the entry's."""
    condition = ("NEW.kind = 'graph_changed' AND (NOT json_valid(NEW.shards) OR json_type(NEW.shards) IS NOT 'array' "
                 "OR json_array_length(NEW.shards) NOT BETWEEN 1 AND " + str(queue_records.MAX_SHARDS_PER_ENTRY)
                 + " OR EXISTS (SELECT 1 FROM json_each(NEW.shards) j WHERE " + _SHARD_BAD + ") OR "
                 "CASE WHEN EXISTS (SELECT 1 FROM json_each(NEW.shards) j WHERE j.type IS NOT 'object') THEN 1 "
                 "ELSE (" + _SHARD_ORDER_BAD + ") END)")
    return _guard("queue_entries_shards", "BEFORE INSERT", "queue_entries", condition, "E22")


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
    return guards + _lease_consistency_guards()


def _prev_state_of(task):
    """The latest queue state of `task` (an expression such as NEW.task_id)."""
    return ("(SELECT q.state_after FROM queue_entries q WHERE q.task_id = " + task
            + " AND q.state_after IS NOT NULL ORDER BY q.seq DESC LIMIT 1)")


def _lease_guards():
    claim = ("(SELECT q.seq FROM queue_entries q WHERE q.task_id = NEW.task_id AND q.kind = 'claimed' "
             "ORDER BY q.seq DESC LIMIT 1)")
    return [
        _guard("leases_match_claim", "BEFORE INSERT", "leases",
               "NOT EXISTS (SELECT 1 FROM queue_entries q WHERE q.seq = " + claim
               + " AND q.lease_id = NEW.lease_id AND q.generation = NEW.generation AND q.step = NEW.step "
               "AND q.worker = NEW.worker AND q.worker_kind = NEW.worker_kind AND q.expires_at = NEW.expires_at "
               "AND q.timestamp = NEW.claimed_at) OR NEW.renewals <> 0 OR coalesce(" + _prev_state_of("NEW.task_id")
               + " NOT IN ('dispatched', 'running'), 1)", "E13"),
        _guard("leases_fixed_fields", "BEFORE UPDATE", "leases",
               "NEW.task_id IS NOT OLD.task_id OR NEW.lease_id IS NOT OLD.lease_id OR NEW.generation IS NOT "
               "OLD.generation OR NEW.step IS NOT OLD.step OR NEW.worker IS NOT OLD.worker OR NEW.worker_kind IS NOT "
               "OLD.worker_kind OR NEW.claimed_at IS NOT OLD.claimed_at OR NEW.expires_at <= OLD.expires_at OR "
               "NEW.renewals <> OLD.renewals + 1 OR " + _sec("NEW.expires_at") + " - " + _sec("OLD.expires_at")
               + " > " + str(queue_records.MAX_TTL) + " OR NEW.expires_at < coalesce((SELECT x.expires_at FROM "
               "queue_entries x WHERE x.task_id = NEW.task_id AND x.kind IN ('claimed', 'renewed') "
               "ORDER BY x.seq DESC LIMIT 1), '') OR NOT ((NEW.expires_at = " + _LAST_EXPIRY + " AND NEW.renewals = "
               + _RENEWED_SO_FAR + ") OR (OLD.expires_at = " + _LAST_EXPIRY + " AND OLD.renewals = " + _RENEWED_SO_FAR
               + " AND NEW.renewals = " + _RENEWED_SO_FAR + " + 1))", "E14"),
        _guard("leases_slots_first", "BEFORE DELETE", "leases",
               "EXISTS (SELECT 1 FROM apply_slots s WHERE s.task_id = OLD.task_id)", "E15"),
        _guard("apply_slots_match_lease", "BEFORE INSERT", "apply_slots",
               "NOT EXISTS (SELECT 1 FROM leases l WHERE l.task_id = NEW.task_id AND l.lease_id = NEW.lease_id "
               "AND l.generation = NEW.generation AND l.step = 'apply') OR coalesce(" + _prev_state_of("NEW.task_id")
               + " NOT IN ('dispatched', 'running'), 1)", "E16"),
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
        "value TEXT NOT NULL CHECK (" + _no_nul("value") + ")) STRICT, WITHOUT ROWID",

        "CREATE TABLE tasks (id TEXT NOT NULL PRIMARY KEY CHECK (" + _slug_check("id") + "), "
        "state TEXT NOT NULL CHECK (state IN (" + states + ")), "
        "owner TEXT NOT NULL CHECK (owner IN (" + actors + ")), "
        "created_at TEXT NOT NULL CHECK (" + _stamp_check("created_at") + "), "
        "updated_at TEXT NOT NULL CHECK (" + _stamp_check("updated_at") + " AND updated_at >= created_at), "
        "blocked_reason TEXT CHECK (blocked_reason IS NULL OR (" + _text_check("blocked_reason", 500) + ")), "
        "blocking_item TEXT CHECK (blocking_item IS NULL OR (" + _text_check("blocking_item", 500) + ")), "
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
        "exit_code INTEGER CHECK (exit_code IS NULL OR exit_code BETWEEN 1 AND " + str(max(STABLE_EXIT_CODES)) + "), "
        "prev_hash TEXT NOT NULL CHECK (" + _hex_check("prev_hash", 64) + "), "
        "hash TEXT NOT NULL UNIQUE CHECK (" + _hex_check("hash", 64) + "), "
        "CHECK ((event = 'state_transition') = (from_state IS NOT NULL AND to_state IS NOT NULL)), "
        "CHECK (event = 'state_transition' OR (from_state IS NULL AND to_state IS NULL)), "
        "CHECK ((event = 'command_failed') = (exit_code IS NOT NULL)), "
        "CHECK (event <> 'state_transition' OR (" + legal + "))) STRICT",

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
        "shards TEXT CHECK (shards IS NULL OR (" + _no_nul("shards") + " AND length(shards) BETWEEN 1 AND "
        + str(MAX_RECORD_BYTES) + ")), "
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

        "CREATE TABLE apply_slots (target TEXT NOT NULL PRIMARY KEY CHECK (" + _text_check("target", 500) + "), "
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
        _seq_guard("events"),
        _seq_guard("queue_entries"),
        _guard("queue_entries_id", "AFTER INSERT", "queue_entries",
               "NEW.id <> 'queue-' || printf('%04d', NEW.seq)", "E21"),
        _shards_guard(),
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


# The reviewed fingerprint of schema version 1, written out so that the value checked at open does not depend on the
# builders above. A change to the DDL changes the generated value; `tests/test_store.py` fails until this literal is
# updated on purpose, in the same change and with the schema review.
PINNED_FINGERPRINT = "921add402caa4084ef47fccaa6bf2d33a6ae8fb127512e4922b5590a93e32284"


@functools.lru_cache(maxsize=1)
def expected_fingerprint():
    """The fingerprint the DDL in this code produces now (a development check: the open uses PINNED_FINGERPRINT)."""
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
    # The descriptor and the path SQLite opens must still be the same directory.
    try:
        named = os.lstat(project._root)
    except OSError as exc:
        raise _os_error(exc) from None
    if (named.st_dev, named.st_ino) != (st.st_dev, st.st_ino):
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


_SIDECARS = ("-wal", "-shm", "-journal")


def _verify_sidecars(path, snapshot, final):
    """After open: every sidecar that exists must be a safe file and the same file that was there before the open."""
    for suffix in _SIDECARS:
        sidecar = path + suffix
        now = _lstat(sidecar, required=False)  # a wide mode, link or foreign owner refuses here
        before = snapshot[suffix]
        if now is None and before is None:
            continue
        if now is None or (before is not None and (before.st_dev, before.st_ino) != (now.st_dev, now.st_ino)):
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
        conn.execute("PRAGMA recursive_triggers = ON")  # so that INSERT OR REPLACE fires the delete triggers
        if hasattr(conn, "setconfig") and hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        _check_json(conn)
        if initialize:
            _install_schema(conn)
        _check_header(conn)
        _require_wal(conn.execute("PRAGMA journal_mode = WAL").fetchone())
        conn.execute("PRAGMA synchronous = FULL")
        _check_settings(conn, timeout)
        if fingerprint(conn) != PINNED_FINGERPRINT:
            raise ShitError(FAILED_CLOSED)
        _verify_sidecars(path, snapshot, final)
        _check_directory(project)
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


def _check_json(conn):
    """The schema's triggers use SQLite's JSON functions; an SQLite built without them is an unsupported runtime."""
    try:
        row = conn.execute(
            "SELECT json_valid('[1]'), json_type('[1]'), json_array_length('[1]'), json_extract('{\"a\":1}', '$.a'), "
            "(SELECT count(*) FROM json_each('[1]'))").fetchone()
    except sqlite3.Error:
        raise ShitError(FAILED_CLOSED) from None
    if tuple(row) != (1, "array", 1, 1, 1):
        raise ShitError(FAILED_CLOSED)


def _check_header(conn):
    app = conn.execute("PRAGMA application_id").fetchone()[0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if app != APPLICATION_ID or version != SCHEMA_VERSION:
        raise ShitError(FAILED_CLOSED)


def _check_settings(conn, timeout):
    # Unrolled on purpose: every statement is a literal the lint can see.
    values = (conn.execute("PRAGMA foreign_keys").fetchone()[0], conn.execute("PRAGMA synchronous").fetchone()[0],
              conn.execute("PRAGMA trusted_schema").fetchone()[0], conn.execute("PRAGMA busy_timeout").fetchone()[0],
              conn.execute("PRAGMA recursive_triggers").fetchone()[0])
    if values != (1, 2, 0, timeout, 1):
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


def init_store(project, busy_timeout_ms=None, adopt_empty=False):
    """Create the database as a 0600 file (umask 077) and its schema, once. Safe to call again.

    A store that already exists is verified, never rewritten. A file this call did not create is refused unless it
    is a complete store; `adopt_empty=True` is the explicit recovery for a crash between creating the file and
    installing the schema (only a brand-new empty database is then initialized, anything else still refuses).
    The argument is validated before any file is created, and a failure after this call created the file removes
    that file (never one it did not create).

    The umask is process-wide while this runs; call it from a single-threaded setup step.
    """
    _check_floor()
    _timeout(busy_timeout_ms)  # an invalid argument must fail before any file exists
    _check_directory(project)
    old = os.umask(stat.S_IRWXG | stat.S_IRWXO)
    created = None
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
                st = os.fstat(fd)
                created = (st.st_dev, st.st_ino)
                os.fchmod(fd, DB_MODE)
            finally:
                os.close(fd)
        _open(project, busy_timeout_ms, initialize=fd is not None or adopt_empty).close()
    except BaseException:
        if created is not None:
            _remove_created(project, created)
        raise
    finally:
        os.umask(old)


def _remove_created(project, identity):
    """Remove the database this call created, and the SQLite files its failed open left, by name inside the pinned
    directory. A name that is no longer the file this call created (another inode) is left alone."""
    for suffix in ("", *_SIDECARS):
        name = DB_NAME + suffix
        try:
            st = os.stat(name, dir_fd=project._fd, follow_symlinks=False)
            if suffix == "" and (st.st_dev, st.st_ino) != identity:
                return  # the main file was replaced: it is not ours, and neither are its side files
            if stat.S_ISREG(st.st_mode):
                os.unlink(name, dir_fd=project._fd)
        except OSError:
            continue


def connect(project, busy_timeout_ms=None):
    """Open the project's store. The one and only way to get a connection."""
    return _open(project, busy_timeout_ms, initialize=False)


class AmbiguousCommit(ShitError):
    """COMMIT raised and the connection is no longer in a transaction, so the write may or may not be durable.

    The exit code is 8 with the usual fixed message. The connection is closed. A caller must open a new
    connection and re-read the state before it repeats anything that is not idempotent."""

    def __init__(self):
        super().__init__(FAILED_CLOSED)


def _abandon(conn):
    """Close a connection whose transaction state cannot be trusted, so nothing can reuse it.

    Even an interrupt during the close is swallowed: the caller raises the fail-closed error right after."""
    with contextlib.suppress(BaseException):
        conn.close()


def _in_transaction(conn):
    """True or False, or None when the connection cannot say (closed, broken)."""
    try:
        return bool(conn.in_transaction)
    except (sqlite3.Error, ValueError):
        return None


def _give_up(conn):
    """Close a connection that cannot be trusted and raise the fail-closed error."""
    _abandon(conn)
    raise ShitError(FAILED_CLOSED) from None


def _fail_transaction(conn, exc):
    """Roll back after `exc` and raise the mapped error; close the connection if the rollback cannot be trusted."""
    try:
        if _in_transaction(conn):
            conn.execute("ROLLBACK")
        clean = _in_transaction(conn) is False
    except BaseException:  # an interrupt or error during cleanup: the state is unknown, so give up the connection
        _give_up(conn)  # always raises
        raise
    if not clean:
        _give_up(conn)
    if isinstance(exc, sqlite3.Error):
        raise map_error(exc) from None
    raise exc


def _end_ambiguous(conn):
    """The commit's outcome is unknown: close the connection and raise AmbiguousCommit with no chained cause."""
    _abandon(conn)
    raise AmbiguousCommit() from None


@contextlib.contextmanager
def write_transaction(conn):
    """BEGIN IMMEDIATE, then commit on success or roll back on any exception (including KeyboardInterrupt).

    - A failed rollback, or a connection still in a transaction after it, closes the connection and raises exit 8.
    - If COMMIT itself raises while the connection is still in its transaction, the commit did not happen: it is
      rolled back and the error mapped (a busy store is exit 13).
    - If COMMIT raises and the connection is no longer in a transaction, or cannot say, the outcome is unknown (the
      write may have landed). The connection is closed and `AmbiguousCommit` (exit 8) is raised.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise map_error(exc) from None
    try:
        yield conn
    except BaseException as exc:
        _fail_transaction(conn, exc)  # always raises
        raise
    try:
        conn.execute("COMMIT")
    except BaseException as exc:
        if _in_transaction(conn) is True:
            _fail_transaction(conn, exc)  # the commit did not happen
        _end_ambiguous(conn)  # always raises
        raise


# -- consistency check --------------------------------------------------------

_LIVE = ("dispatched", "running")


def verify(conn, deep=False):
    """Read-only: the names of the cross-table invariants this database does not hold (empty when consistent).

    The triggers check each statement. Some invariants span several statements of one transaction (a claim entry and
    its lease row, an apply slot and the lease it hangs on), so a crash, a writer that commits halfway or a hand edit
    can leave a state no single statement would have produced. The write helpers of the queue run this in their tests
    after every operation (docs/store.md, "Invariants across statements"). `deep=True` adds `PRAGMA
    integrity_check`, which reads every page. An SQLite failure while reading raises the usual fixed `ShitError`.
    """
    try:
        return _verify_unmapped(conn, deep)
    except sqlite3.Error as exc:
        raise map_error(exc) from None


def _verify_unmapped(conn, deep):
    found = set()
    live = conn.execute(
        "SELECT q.task_id, q.state_after FROM queue_entries q WHERE q.seq = (SELECT max(m.seq) FROM queue_entries m "
        "WHERE m.task_id = q.task_id AND m.state_after IS NOT NULL)").fetchall()
    states = dict(live)
    leases = {row[0]: row for row in conn.execute(
        "SELECT task_id, lease_id, generation, step, worker, worker_kind, claimed_at, expires_at, renewals "
        "FROM leases").fetchall()}
    for task, state in states.items():
        if state in _LIVE and task not in leases:
            found.add("claim_without_lease")
    for task in leases:
        if states.get(task) not in _LIVE:
            found.add("lease_without_live_claim")
    for task, lease in leases.items():
        claim = conn.execute(
            "SELECT lease_id, generation, step, worker, worker_kind, timestamp FROM queue_entries WHERE task_id = ? "
            "AND kind = 'claimed' ORDER BY seq DESC LIMIT 1", (task,)).fetchone()
        if claim is None or tuple(claim) != (lease[1], lease[2], lease[3], lease[4], lease[5], lease[6]):
            found.add("lease_differs_from_claim")
            continue
        last = conn.execute(
            "SELECT expires_at, (SELECT count(*) FROM queue_entries r WHERE r.task_id = x.task_id AND r.kind = "
            "'renewed' AND r.seq > (SELECT max(c.seq) FROM queue_entries c WHERE c.task_id = x.task_id AND c.kind = "
            "'claimed')) FROM queue_entries x WHERE x.task_id = ? AND x.kind IN ('claimed', 'renewed') "
            "ORDER BY x.seq DESC LIMIT 1", (task,)).fetchone()
        if last is None or (last[0], last[1]) != (lease[7], lease[8]):
            found.add("lease_expiry_differs_from_log")
    for task, lease_id, generation in conn.execute("SELECT task_id, lease_id, generation FROM apply_slots").fetchall():
        lease = leases.get(task)
        if lease is None or (lease[1], lease[2], lease[3]) != (lease_id, generation, "apply") or states.get(task) not in _LIVE:
            found.add("slot_without_live_apply_lease")
    for task, state, queue_state in conn.execute(
            "SELECT t.id, t.state, q.state_after FROM tasks t JOIN queue_entries q ON q.task_id = t.id WHERE q.seq = "
            "(SELECT max(m.seq) FROM queue_entries m WHERE m.task_id = t.id AND m.state_after IS NOT NULL)").fetchall():
        if state not in queue_records.LIFECYCLE.get(queue_state, ()):
            found.add("task_state_differs_from_queue")
    for name, rows in (
            ("events_chain_broken", conn.execute("SELECT seq, prev_hash, hash, timestamp FROM events ORDER BY seq")),
            ("queue_chain_broken", conn.execute("SELECT seq, prev_hash, hash, timestamp FROM queue_entries ORDER BY seq"))):
        previous_hash, previous_stamp, expected = GENESIS, "", 1
        for seq, prev_hash, digest, stamp in rows.fetchall():
            if seq != expected or prev_hash != previous_hash or stamp < previous_stamp:
                found.add(name)
                break
            previous_hash, previous_stamp, expected = digest, stamp, expected + 1
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        found.add("foreign_key_violation")
    if deep and conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        found.add("integrity_check_failed")
    return sorted(found)
