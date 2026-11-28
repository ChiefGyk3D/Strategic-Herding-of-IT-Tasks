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
    HUMAN_APPROVAL,
    ILLEGAL_TRANSITION,
    INPUT_LIMIT,
    INTERNAL,
    LEASE_REFUSED,
    PRECONDITION,
    REJECTED,
    STABLE_EXIT_CODES,
    USAGE,
    ShitError,
)
from .log import GENESIS, chain_hash
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
        "PRIMARY KEY (task_id, needs_id, type), CHECK (task_id <> needs_id)) STRICT, WITHOUT ROWID",

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
PINNED_FINGERPRINT = "63962044ba8ad8c8fa97a8e7119b0382aa1dd11a4572b5c6d81191ee3dbbb7ca"


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


# == C3: tasks, events and the records index (issue 61, epic 57) =========================================
#
# One helper is one `write_transaction`: every check and every read happens after BEGIN IMMEDIATE, then the writes
# follow in a fixed order (task row, history row, index row, event). Record BODIES stay files. The caller writes the
# body to its final name before calling a helper and compares its digest with the index row afterwards
# (`shit/store_tasks.py`). The event hash is the file protocol's: sha256(previous hash, newline, the event's line).

# Record kind -> (event kind, label of the event message). A kind without an entry is indexed with no event.
_RECORD_EVENTS = {
    "brief": ("brief_recorded", "Brief"), "evidence": ("evidence_recorded", "Evidence"),
    "report": ("report_recorded", "Report"), "review": ("review_recorded", "Review"),
    "decision": ("decision_recorded", "Decision"),
}
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PLACEHOLDER = "-"  # title and summary live in the task body file; the image checked here needs some text


def _fault(stage):
    """Fault-injection seam: tests replace it to abort a helper between two statements of its one transaction."""


def _slug_or_reject(value):
    if not isinstance(value, str) or not records.SLUG.fullmatch(value):
        raise ShitError(REJECTED)
    return value


def _digest_or_reject(sha256, size):
    if (not isinstance(sha256, str) or not _HEX64.fullmatch(sha256) or type(size) is not int
            or not 0 <= size <= MAX_RECORD_BYTES):
        raise ShitError(REJECTED)


def record_message(kind, sha256):
    """The fixed event message of a recorded body: it carries the digest, as the file protocol's did."""
    return _RECORD_EVENTS[kind][1] + " record added; sha256 " + sha256 + "."


@contextlib.contextmanager
def read_snapshot(conn):
    """One consistent read: every SELECT inside sees the same commit, and nothing is written (ends in ROLLBACK).

    Reads outside a snapshot are fine for one statement; an operation that reads several rows (a task and its history)
    uses this, so a writer that commits between two reads is not seen half way."""
    try:
        conn.execute("BEGIN")
    except sqlite3.Error as exc:
        raise map_error(exc) from None
    try:
        yield conn
    except BaseException as exc:
        _fail_transaction(conn, exc)  # always raises
        raise
    try:
        conn.execute("ROLLBACK")
    except BaseException:
        _give_up(conn)  # always raises
        raise
    if _in_transaction(conn) is not False:
        _give_up(conn)


# -- reads (single statements; wrap several in `read_snapshot`) --------------------------------------------


def _task_row(conn, task_id):
    row = conn.execute("SELECT id, state, owner, created_at, updated_at, blocked_reason, blocking_item FROM tasks "
                       "WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    task = {"id": row[0], "state": row[1], "owner": row[2], "created_at": row[3], "updated_at": row[4]}
    if row[5] is not None:
        task["blocked_reason"], task["blocking_item"] = row[5], row[6]
    return task


def _history_rows(conn, task_id):
    return [{"from": a, "to": b, "actor": c, "at": d, "reason": e} for a, b, c, d, e in conn.execute(
        "SELECT from_state, to_state, actor, at, reason FROM task_history WHERE task_id = ? ORDER BY id",
        (task_id,)).fetchall()]


def get_task(conn, task_id):
    """The task row and its history as one dict (no title or summary: those are in the body file), or None."""
    task = _task_row(conn, task_id)
    if task is not None:
        task["history"] = _history_rows(conn, task_id)
    return task


def _kind_or_reject(kind, allowed):
    if kind not in allowed:
        raise ShitError(REJECTED)
    return kind


def get_record(conn, kind, record_id):
    """The index row of one record body (kind, id, task, digest, size, time), or None."""
    _kind_or_reject(kind, RECORD_KINDS)
    row = conn.execute("SELECT task_id, sha256, size, created_at FROM records WHERE kind = ? AND record_id = ?",
                       (kind, record_id)).fetchone()
    if row is None:
        return None
    return {"kind": kind, "record_id": record_id, "task_id": row[0], "sha256": row[1], "size": row[2],
            "created_at": row[3]}


def list_records(conn):
    """Every index row, in a fixed order (kind, then id)."""
    return [{"kind": kind, "record_id": record_id, "task_id": task_id, "sha256": sha256, "size": size}
            for kind, record_id, task_id, sha256, size in conn.execute(
                "SELECT kind, record_id, task_id, sha256, size FROM records ORDER BY kind, record_id").fetchall()]


def _event_dict(row):
    """An event as the record the file protocol dumped (same keys, same order), from the columns of one row."""
    event = {"schema": "event-log", "schema_version": 1, "id": row[0], "task_id": row[1], "timestamp": row[2],
             "actor": row[3], "event": row[4], "message": row[5]}
    if row[6] is not None:
        event["record_id"] = row[6]
    if row[7] is not None:
        event["from_state"], event["to_state"] = row[7], row[8]
    if row[9] is not None:
        event["exit_code"] = row[9]
    return event


def record_event(conn, kind, record_id):
    """The event that recorded a body of `kind` (not a task, not a handoff), or None."""
    _kind_or_reject(kind, _RECORD_EVENTS)
    row = conn.execute("SELECT id, task_id, timestamp, actor, event, message, record_id, from_state, to_state, "
                       "exit_code FROM events WHERE event = ? AND record_id = ?",
                       (_RECORD_EVENTS[kind][0], record_id)).fetchone()
    return None if row is None else _event_dict(row)


def record_ids(conn, kind, task_id):
    """Ids of the records of `kind` for `task_id`, in the order their events were written."""
    _kind_or_reject(kind, _RECORD_EVENTS)
    return [row[0] for row in conn.execute(
        "SELECT e.record_id FROM events e JOIN records r ON r.kind = ? AND r.record_id = e.record_id "
        "WHERE e.event = ? AND e.task_id = ? AND r.task_id = e.task_id ORDER BY e.seq",
        (kind, _RECORD_EVENTS[kind][0], task_id)).fetchall()]


def list_events(conn):
    """Every event, oldest first, as the dicts the file protocol's log held."""
    return [_event_dict(row) for row in conn.execute(
        "SELECT id, task_id, timestamp, actor, event, message, record_id, from_state, to_state, exit_code "
        "FROM events ORDER BY seq").fetchall()]


# -- the events of one operation ----------------------------------------------------------------------------


def _tip(conn):
    """(sequence number, hash, timestamp) of the last event, or the genesis values when there is none."""
    row = conn.execute("SELECT seq, hash, timestamp FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return (0, GENESIS, "") if row is None else (row[0], row[1], row[2])


def _next_event(conn, task_id, actor, kind, message, now, **extra):
    """Build and validate the next event of the log and its hash. Read-only: the caller inserts it."""
    seq, previous, stamp = _tip(conn)
    if not isinstance(now, str) or now < stamp:  # event time never goes back (the file log checked the same)
        raise ShitError(REJECTED)
    event = {"schema": "event-log", "schema_version": 1, "id": "event-" + format(seq + 1, "04d"), "task_id": task_id,
             "timestamp": now, "actor": actor, "event": kind, "message": message, "record_id": task_id, **extra}
    line = records.dump_event(event)
    return seq + 1, previous, chain_hash(previous, line), event


def _image(task, history):
    """The task as `records.validate_task` reads it, so the history is checked by the same code as before."""
    image = {"schema": "task", "schema_version": 1, "id": task["id"], "title": _PLACEHOLDER, "summary": _PLACEHOLDER,
             "state": task["state"], "owner": task["owner"], "created_at": task["created_at"],
             "updated_at": task["updated_at"], "history": history}
    for key in ("blocked_reason", "blocking_item"):
        if key in task:
            image[key] = task[key]
    return image


# -- writes: one helper, one transaction ------------------------------------------------------------------


def create_task(conn, task, actor, body_sha256, body_size):
    """Create a task: its row, the index row of its body file and the `task_created` event, in one transaction.

    `task` is the validated image the body file holds (state open, no history, created equals updated)."""
    records.validate_task(task)
    if (task["state"] != "open" or task["history"] or task["created_at"] != task["updated_at"]
            or "blocked_reason" in task or "blocking_item" in task):
        raise ShitError(REJECTED)
    _digest_or_reject(body_sha256, body_size)
    with write_transaction(conn):
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task["id"],)).fetchone() is not None:
            raise ShitError(USAGE)
        seq, previous, digest, event = _next_event(conn, task["id"], actor, "task_created", "Task record created.",
                                                   task["created_at"])
        _fault("checked")
        conn.execute("INSERT INTO tasks (id, state, owner, created_at, updated_at) VALUES (?, 'open', ?, ?, ?)",
                     (task["id"], task["owner"], task["created_at"], task["updated_at"]))
        _fault("task-row")
        conn.execute("INSERT INTO records (kind, record_id, task_id, sha256, size, created_at) "
                     "VALUES ('task', ?, ?, ?, ?, ?)", (task["id"], task["id"], body_sha256, body_size,
                                                        task["created_at"]))
        _fault("index-row")
        conn.execute("INSERT INTO events (seq, id, task_id, timestamp, actor, event, message, record_id, from_state, "
                     "to_state, exit_code, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                     (seq, event["id"], event["task_id"], event["timestamp"], event["actor"], event["event"],
                      event["message"], event["record_id"], previous, digest))


def transition_task(conn, task_id, to, actor, reason, now, *, blocked_reason=None, blocking_item=None,
                    precheck=None):
    """Move a task: the new state, the history row and the `state_transition` event, in one transaction.

    `precheck(current)` runs inside the transaction on the task row as read there (not the body file) and may raise.
    Returns the task row after the change."""
    _slug_or_reject(task_id)
    with write_transaction(conn):
        current = _task_row(conn, task_id)
        if current is None:
            raise ShitError(PRECONDITION)
        frm = current["state"]
        if to == "approved" and actor != "human" and (frm, to) in lifecycle.LEGAL:
            raise ShitError(HUMAN_APPROVAL)
        lifecycle.check_transition(frm, to, actor)
        if precheck is not None:
            precheck(current)
        if not isinstance(now, str) or now < current["updated_at"]:
            raise ShitError(REJECTED)
        blocked = {}
        if to == "blocked":
            if not blocked_reason or not blocking_item:
                raise ShitError(REJECTED)
            blocked = {"blocked_reason": blocked_reason, "blocking_item": blocking_item}
        elif blocked_reason is not None or blocking_item is not None:
            raise ShitError(REJECTED)
        step = {"from": frm, "to": to, "actor": actor, "at": now, "reason": reason}
        after = {"id": task_id, "state": to, "owner": current["owner"], "created_at": current["created_at"],
                 "updated_at": now, **blocked}
        records.validate_task(_image(after, [*_history_rows(conn, task_id), step]))
        seq, previous, digest, event = _next_event(conn, task_id, actor, "state_transition",
                                                   "State transition recorded.", now, from_state=frm, to_state=to)
        _fault("checked")
        conn.execute("UPDATE tasks SET state = ?, updated_at = ?, blocked_reason = ?, blocking_item = ? WHERE id = ?",
                     (to, now, blocked.get("blocked_reason"), blocked.get("blocking_item"), task_id))
        _fault("task-updated")
        conn.execute("INSERT INTO task_history (task_id, from_state, to_state, actor, at, reason) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (task_id, frm, to, actor, now, reason))
        _fault("history-row")
        conn.execute("INSERT INTO events (seq, id, task_id, timestamp, actor, event, message, record_id, from_state, "
                     "to_state, exit_code, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                     (seq, event["id"], event["task_id"], event["timestamp"], event["actor"], event["event"],
                      event["message"], event["record_id"], frm, to, previous, digest))
    return after


def add_record(conn, kind, record, body_sha256, body_size, check=None):
    """Index a record body and write its event, in one transaction (a `handoff` has no event, only the row).

    `record` holds the id, task id, actor and creation time of the body. `check(task)` runs inside the
    transaction on the task row and may raise. A closed task takes no more records."""
    _kind_or_reject(kind, RECORD_KINDS)
    if kind == "task":
        raise ShitError(REJECTED)  # a task's index row is written by create_task
    record_id, task_id = _slug_or_reject(record.get("id")), _slug_or_reject(record.get("task_id"))
    actor, stamp = record.get("actor"), record.get("created_at")
    _digest_or_reject(body_sha256, body_size)
    with write_transaction(conn):
        current = _task_row(conn, task_id)
        if current is None:
            raise ShitError(PRECONDITION)
        if current["state"] == "closed":
            raise ShitError(ILLEGAL_TRANSITION)
        if conn.execute("SELECT 1 FROM records WHERE kind = ? AND record_id = ?", (kind, record_id)).fetchone():
            raise ShitError(USAGE)
        if check is not None:
            check(current)
        planned = None
        if kind in _RECORD_EVENTS:
            planned = _next_event(conn, task_id, actor, _RECORD_EVENTS[kind][0], record_message(kind, body_sha256),
                                  stamp, record_id=record_id)
        elif actor not in lifecycle.ACTORS or not isinstance(stamp, str) or not records.TIMESTAMP.fullmatch(stamp):
            raise ShitError(REJECTED)
        _fault("checked")
        conn.execute("INSERT INTO records (kind, record_id, task_id, sha256, size, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (kind, record_id, task_id, body_sha256, body_size, stamp))
        _fault("record-row")
        if planned is not None:
            seq, previous, digest, event = planned
            conn.execute("INSERT INTO events (seq, id, task_id, timestamp, actor, event, message, record_id, "
                         "from_state, to_state, exit_code, prev_hash, hash) VALUES "
                         "(?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                         (seq, event["id"], event["task_id"], event["timestamp"], event["actor"], event["event"],
                          event["message"], event["record_id"], previous, digest))


# -- consistency of the task data ------------------------------------------------------------------------


def verify_tasks(conn):
    """Read-only: the names of the task, event and index invariants this database does not hold.

    These span several statements, so a trigger cannot refuse them (a crash cannot produce them through the helpers
    above, which write each operation in one transaction; a hand edit or a writer that skipped them can).
    `verify` covers the chain links; this adds the event hashes, the task rows against their history and events, and
    the records index against its events. An SQLite failure while reading raises the usual fixed `ShitError`."""
    try:
        return _verify_tasks_unmapped(conn)
    except sqlite3.Error as exc:
        raise map_error(exc) from None


def _verify_tasks_unmapped(conn):
    found = set()
    events = []
    for row in conn.execute("SELECT id, task_id, timestamp, actor, event, message, record_id, from_state, to_state, "
                            "exit_code, prev_hash, hash FROM events ORDER BY seq").fetchall():
        event = _event_dict(row)
        try:
            line = records.dump_event(event)
        except ShitError:
            found.add("event_invalid")
            continue
        if chain_hash(row[10], line) != row[11]:
            found.add("event_hash_wrong")
        events.append(event)
    created, steps = {}, {}
    for event in events:
        if event["event"] == "task_created":
            created[event["task_id"]] = event["timestamp"]
        elif event["event"] == "state_transition":
            steps.setdefault(event["task_id"], []).append(
                (event["from_state"], event["to_state"], event["actor"], event["timestamp"]))
    history = {}
    for task_id, frm, to, actor, at in conn.execute(
            "SELECT task_id, from_state, to_state, actor, at FROM task_history ORDER BY id").fetchall():
        history.setdefault(task_id, []).append((frm, to, actor, at))
    for task_id, state, created_at, updated_at in conn.execute(
            "SELECT id, state, created_at, updated_at FROM tasks").fetchall():
        rows = history.get(task_id, [])
        previous = "open"
        for frm, to, _actor, _at in rows:
            if frm != previous:
                found.add("task_differs_from_history")
            previous = to
        if state != previous or updated_at != (rows[-1][3] if rows else created_at):
            found.add("task_differs_from_history")
        if created.get(task_id) != created_at:
            found.add("task_without_created_event")
        if rows != steps.get(task_id, []):
            found.add("history_differs_from_events")
        if conn.execute("SELECT 1 FROM records WHERE kind = 'task' AND record_id = ? AND task_id = ?",
                        (task_id, task_id)).fetchone() is None:
            found.add("task_without_body_record")
    recorded = {}
    for event in events:
        for kind, (event_kind, _label) in _RECORD_EVENTS.items():
            if event["event"] == event_kind:
                recorded[(kind, event["record_id"])] = event
    indexed = set()
    for kind, record_id, task_id, sha256 in conn.execute(
            "SELECT kind, record_id, task_id, sha256 FROM records").fetchall():
        if kind not in _RECORD_EVENTS:
            continue  # a task body is checked above; a handoff has no event
        indexed.add((kind, record_id))
        event = recorded.get((kind, record_id))
        if event is None or event["task_id"] != task_id:
            found.add("record_without_event")
        elif event["message"] != record_message(kind, sha256):
            found.add("record_digest_differs_from_event")
    if set(recorded) - indexed:
        found.add("event_without_record")
    return sorted(found)


# == dependency graph (issue 62, epic 57; docs/store.md, "Dependency graph") ===============================
#
# The same observable behavior as `shit/dependencies.py` (shape, missing node, duplicate, caps, cycle, in that
# order) and the removal guard of `shit/queue.remove_edge`, as write helpers. Each add or remove is ONE
# `write_transaction`. Edge `(task_id, needs_id, type)` means `task_id` needs `needs_id`.

_GRAPH_SHAPE = ("task_id", "needs_id", "type")


def _graph_slug(value):
    if not isinstance(value, str) or not records.SLUG.fullmatch(value):
        raise ShitError(REJECTED)
    return value


def _graph_edge(edge):
    """The shape check of `dependencies._endpoints`: two slugs and a known type, else exit code 3."""
    if not isinstance(edge, (tuple, list)) or len(edge) != len(_GRAPH_SHAPE):
        raise ShitError(REJECTED)
    task_id, needs_id, kind = edge
    _graph_slug(task_id)
    _graph_slug(needs_id)
    if not isinstance(kind, str) or kind not in dependencies.TYPES:
        raise ShitError(REJECTED)
    return task_id, needs_id, kind


def _graph_task_exists(conn, task_id):
    return conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is not None


def _graph_has_path(conn, start, goal):
    """True when `goal` is `start` or one of the tasks `start` needs, directly or through other tasks."""
    return conn.execute(
        "SELECT 1 FROM (WITH RECURSIVE reach(id) AS (SELECT ? UNION SELECT e.needs_id FROM edges e "
        "JOIN reach r ON e.task_id = r.id) SELECT id FROM reach) WHERE id = ? LIMIT 1", (start, goal)).fetchone() is not None


def _graph_check_caps(conn, wanted):
    """Exit code 7 past the per-task or total cap, counting `wanted` (every new edge, self edges included)."""
    per_source = {}
    for task_id, _, _ in wanted:
        per_source[task_id] = per_source.get(task_id, 0) + 1
    new_sources = 0
    for task_id, count in per_source.items():
        stored = conn.execute("SELECT count(*) FROM edges WHERE task_id = ?", (task_id,)).fetchone()[0]
        if stored + count > dependencies.MAX_NEEDS_PER_TASK:
            raise ShitError(INPUT_LIMIT)
        if stored == 0:
            new_sources += 1
    if new_sources and conn.execute("SELECT count(DISTINCT task_id) FROM edges").fetchone()[0] + new_sources \
            > dependencies.MAX_TASKS:
        raise ShitError(INPUT_LIMIT)


def add_edges(conn, additions):
    """Add every `(task_id, needs_id, type)` in one transaction, or none.

    Exit codes as `dependencies.add_edges`: 3 for a bad shape, a duplicate, a self edge or a cycle, 9 for an unknown
    task, 7 past the per-task cap (200) or the total cap (2,000 dependent tasks). The order of the checks is the
    same: each edge's shape and then its tasks, in the order given; then duplicates; then the caps; then cycles.
    """
    if not isinstance(additions, (list, tuple)):
        raise ShitError(REJECTED)
    with write_transaction(conn):
        wanted = []
        for addition in additions:
            edge = _graph_edge(addition)
            if not (_graph_task_exists(conn, edge[0]) and _graph_task_exists(conn, edge[1])):
                raise ShitError(PRECONDITION)
            wanted.append(edge)
        seen = set()
        for edge in wanted:
            if edge in seen or conn.execute(
                    "SELECT 1 FROM edges WHERE task_id = ? AND needs_id = ? AND type = ?", edge).fetchone():
                raise ShitError(REJECTED)
            seen.add(edge)
        _graph_check_caps(conn, wanted)
        if any(task_id == needs_id for task_id, needs_id, _ in wanted):
            raise ShitError(REJECTED)
        for edge in wanted:
            conn.execute("INSERT INTO edges (task_id, needs_id, type) VALUES (?, ?, ?)", edge)
        # With every edge in, a cycle exists exactly when some new edge's prerequisite reaches its dependent.
        if any(_graph_has_path(conn, needs_id, task_id) for task_id, needs_id, _ in wanted):
            raise ShitError(REJECTED)


def add_edge(conn, task_id, needs_id, kind):
    """One edge, one transaction (see `add_edges`)."""
    add_edges(conn, [(task_id, needs_id, kind)])


def remove_edge(conn, task_id, needs_id, kind):
    """Remove one edge in one transaction. Exit code 3 for a bad shape, 9 when the edge is not there, and 4 (the
    removal guard) while the dependent task holds a live lease, as `queue.remove_edge`."""
    edge = _graph_edge((task_id, needs_id, kind))
    with write_transaction(conn):
        if conn.execute("SELECT 1 FROM edges WHERE task_id = ? AND needs_id = ? AND type = ?", edge).fetchone() is None:
            raise ShitError(PRECONDITION)
        if conn.execute("SELECT 1 FROM leases WHERE task_id = ?", (task_id,)).fetchone() is not None:
            raise ShitError(ILLEGAL_TRANSITION)
        conn.execute("DELETE FROM edges WHERE task_id = ? AND needs_id = ? AND type = ?", edge)


def list_edges(conn):
    """Every edge as `{task_id, needs_id, type}`, sorted by those three."""
    try:
        rows = conn.execute("SELECT task_id, needs_id, type FROM edges ORDER BY task_id, needs_id, type").fetchall()
    except sqlite3.Error as exc:
        raise map_error(exc) from None
    return [{"task_id": a, "needs_id": b, "type": c} for a, b, c in rows]


def needs_of(conn, task_id):
    """What `task_id` needs directly, as `{needs_id, type}` sorted by those two (`dependencies.group_by_task`)."""
    _graph_slug(task_id)
    try:
        rows = conn.execute("SELECT needs_id, type FROM edges WHERE task_id = ? ORDER BY needs_id, type",
                            (task_id,)).fetchall()
    except sqlite3.Error as exc:
        raise map_error(exc) from None
    return [{"needs_id": a, "type": b} for a, b in rows]


def prerequisite_closure(conn, task_id):
    """Ids of every task `task_id` needs, directly or through others (sorted; never `task_id` itself)."""
    _graph_slug(task_id)
    try:
        rows = conn.execute(
            "SELECT id FROM (WITH RECURSIVE reach(id) AS (SELECT e.needs_id FROM edges e WHERE e.task_id = ? "
            "UNION SELECT e.needs_id FROM edges e JOIN reach r ON e.task_id = r.id) SELECT id FROM reach) "
            "ORDER BY id", (task_id,)).fetchall()
    except sqlite3.Error as exc:
        raise map_error(exc) from None
    return [row[0] for row in rows]
