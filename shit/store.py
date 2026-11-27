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
    INTERNAL,
    PRECONDITION,
    REJECTED,
    USAGE,
    ShitError,
)
from .project import _os_error

DB_NAME = "store.sqlite3"
DB_MODE = 0o600
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


def _literal_list(values):
    """A SQL list of quoted words from the code's own vocabulary. Each word is validated, never escaped."""
    words = sorted(set(values))
    if not words or any(not _VOCAB.fullmatch(w) for w in words):
        raise ValueError("vocabulary word outside the allowed shape")
    return ", ".join("'" + w + "'" for w in words)


def _slug_check(column):
    if not _NAME.fullmatch(column):
        raise ValueError("column name outside the allowed shape")
    return (column + " GLOB '[a-z0-9]*' AND " + column + " NOT GLOB '*[^a-z0-9-]*' AND length(" + column
            + ") <= 64")


def _hex_check(column, length):
    if not _NAME.fullmatch(column) or type(length) is not int:
        raise ValueError("bad argument")
    return ("length(" + column + ") = " + str(length) + " AND " + column + " NOT GLOB '*[^0-9a-f]*'")


def schema_statements():
    """The DDL, in creation order, plus the version pragmas. Built only from this code's vocabulary."""
    stamp = ("'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'")
    states = _literal_list(lifecycle.STATES)
    actors = _literal_list(lifecycle.ACTORS)
    qstates = _literal_list(queue_records.STATES)
    kinds = _literal_list(queue_records.KINDS)
    steps = _literal_list(queue_records.STEPS)
    worker_kinds = _literal_list(queue_records.WORKER_KINDS)
    hold_kinds = _literal_list(queue_records.HOLD_KINDS)
    dep_types = _literal_list(dependencies.TYPES)
    events = _literal_list(records.EVENTS)
    record_kinds = _literal_list(RECORD_KINDS)
    for pair in lifecycle.LEGAL:
        if not all(_VOCAB.fullmatch(w) for w in pair):
            raise ValueError("vocabulary word outside the allowed shape")
    legal = " OR ".join("(from_state = '" + a + "' AND to_state = '" + b + "')" for a, b in sorted(lifecycle.LEGAL))
    ts = "GLOB " + stamp
    text = "length(message) <= " + str(MAX_TEXT)
    return list(_flatten([
        "CREATE TABLE meta (key TEXT NOT NULL PRIMARY KEY CHECK (" + _slug_check("key") + "), "
        "value TEXT NOT NULL) STRICT, WITHOUT ROWID",

        "CREATE TABLE tasks (id TEXT NOT NULL PRIMARY KEY CHECK (" + _slug_check("id") + "), "
        "state TEXT NOT NULL CHECK (state IN (" + states + ")), "
        "owner TEXT NOT NULL CHECK (owner IN (" + actors + ")), "
        "created_at TEXT NOT NULL CHECK (created_at " + ts + "), "
        "updated_at TEXT NOT NULL CHECK (updated_at " + ts + " AND updated_at >= created_at), "
        "blocked_reason TEXT CHECK (blocked_reason IS NULL OR length(blocked_reason) BETWEEN 1 AND 500), "
        "blocking_item TEXT CHECK (blocking_item IS NULL OR length(blocking_item) BETWEEN 1 AND 500), "
        "CHECK ((state = 'blocked') = (blocked_reason IS NOT NULL AND blocking_item IS NOT NULL)), "
        "CHECK (state = 'blocked' OR (blocked_reason IS NULL AND blocking_item IS NULL))) STRICT",

        "CREATE TABLE task_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT NOT NULL REFERENCES tasks (id), "
        "from_state TEXT NOT NULL CHECK (from_state IN (" + states + ")), "
        "to_state TEXT NOT NULL CHECK (to_state IN (" + states + ")), "
        "actor TEXT NOT NULL CHECK (actor IN (" + actors + ")), "
        "at TEXT NOT NULL CHECK (at " + ts + "), "
        "reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 500), "
        "CHECK (" + legal + ")) STRICT",

        "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "id TEXT NOT NULL UNIQUE CHECK (" + _slug_check("id") + "), "
        "task_id TEXT NOT NULL REFERENCES tasks (id), "
        "timestamp TEXT NOT NULL CHECK (timestamp " + ts + "), "
        "actor TEXT NOT NULL CHECK (actor IN (" + actors + ")), "
        "event TEXT NOT NULL CHECK (event IN (" + events + ")), "
        "message TEXT NOT NULL CHECK (" + text + "), "
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
        "created_at TEXT NOT NULL CHECK (created_at " + ts + "), "
        "PRIMARY KEY (kind, record_id)) STRICT, WITHOUT ROWID",

        "CREATE TABLE queue_entries (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "id TEXT NOT NULL UNIQUE CHECK (" + _slug_check("id") + "), "
        "task_id TEXT NOT NULL REFERENCES tasks (id), "
        "timestamp TEXT NOT NULL CHECK (timestamp " + ts + "), "
        "actor TEXT NOT NULL CHECK (actor IN (" + actors + ")), "
        "kind TEXT NOT NULL CHECK (kind IN (" + kinds + ")), "
        "state_after TEXT CHECK (state_after IS NULL OR state_after IN (" + qstates + ")), "
        "lease_id TEXT CHECK (lease_id IS NULL OR (" + _hex_check("lease_id", 32) + ")), "
        "generation INTEGER CHECK (generation IS NULL OR generation >= 1), "
        "worker TEXT CHECK (worker IS NULL OR (" + _slug_check("worker") + ")), "
        "worker_kind TEXT CHECK (worker_kind IS NULL OR worker_kind IN (" + worker_kinds + ")), "
        "step TEXT CHECK (step IS NULL OR step IN (" + steps + ")), "
        "hold_kind TEXT CHECK (hold_kind IS NULL OR hold_kind IN (" + hold_kinds + ")), "
        "not_before TEXT CHECK (not_before IS NULL OR not_before " + ts + "), "
        "attempt INTEGER CHECK (attempt IS NULL OR attempt >= 1), "
        "expires_at TEXT CHECK (expires_at IS NULL OR expires_at " + ts + "), "
        "message TEXT CHECK (message IS NULL OR length(message) <= " + str(MAX_TEXT) + "), "
        "shards TEXT CHECK (shards IS NULL OR length(shards) <= " + str(MAX_RECORD_BYTES) + "), "
        "prev_hash TEXT NOT NULL CHECK (" + _hex_check("prev_hash", 64) + "), "
        "hash TEXT NOT NULL UNIQUE CHECK (" + _hex_check("hash", 64) + "), "
        "CHECK ((kind = 'graph_changed') = (state_after IS NULL)), "
        "CHECK ((kind = 'graph_changed') = (shards IS NOT NULL))) STRICT",

        "CREATE TABLE leases (task_id TEXT NOT NULL PRIMARY KEY REFERENCES tasks (id), "
        "lease_id TEXT NOT NULL UNIQUE CHECK (" + _hex_check("lease_id", 32) + "), "
        "generation INTEGER NOT NULL CHECK (generation >= 1), "
        "step TEXT NOT NULL CHECK (step IN (" + steps + ")), "
        "worker TEXT NOT NULL CHECK (" + _slug_check("worker") + "), "
        "worker_kind TEXT NOT NULL CHECK (worker_kind IN (" + worker_kinds + ")), "
        "claimed_at TEXT NOT NULL CHECK (claimed_at " + ts + "), "
        "expires_at TEXT NOT NULL CHECK (expires_at " + ts + " AND expires_at > claimed_at), "
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
        "acquired_at TEXT NOT NULL CHECK (acquired_at " + ts + ")) STRICT, WITHOUT ROWID",

        _append_only("events", "E1", "E2"),
        _append_only("queue_entries", "E3", "E4"),
        _append_only("task_history", "E5", "E6"),
        _trigger("tasks_no_delete", "BEFORE DELETE ON tasks", "E7"),

        "PRAGMA application_id = " + str(int(APPLICATION_ID)),
        "PRAGMA user_version = " + str(int(SCHEMA_VERSION)),
    ]))


def _trigger(name, when, token):
    if not _NAME.fullmatch(name) or token not in TOKEN_EXIT:
        raise ValueError("bad trigger")
    return "CREATE TRIGGER " + name + " " + when + " BEGIN SELECT RAISE(ABORT, '" + token + "'); END"


def _append_only(table, update_token, delete_token):
    return (_trigger(table + "_no_update", "BEFORE UPDATE ON " + table, update_token),
            _trigger(table + "_no_delete", "BEFORE DELETE ON " + table, delete_token))


def _flatten(statements):
    for item in statements:
        if isinstance(item, tuple):
            yield from item
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
    """Regular file, one link, owned by this user, no group or other bits (mode at most 0600)."""
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid()
            or st.st_mode & 0o177):
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
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
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


def _open(project, busy_timeout_ms, initialize):
    _check_floor()
    timeout = _timeout(busy_timeout_ms)
    _check_directory(project)
    path = _db_path(project)
    before = _lstat(path, required=True)
    for suffix in ("-wal", "-shm"):
        _lstat(path + suffix, required=False)
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
    old = os.umask(0o077)
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


@contextlib.contextmanager
def write_transaction(conn):
    """BEGIN IMMEDIATE, then commit on success or roll back on any exception (including KeyboardInterrupt)."""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise map_error(exc) from None
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
        if isinstance(exc, sqlite3.Error):
            raise map_error(exc) from None
        raise
