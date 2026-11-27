"""SQLite store foundation tests (issue 59): connect, confinement, floor, schema, fingerprint, errors, lint.

Real database files in a temporary directory; nothing about sqlite is mocked. Tampering is done with a
plain `sqlite3` connection opened by the test itself, which is allowed outside `shit/`.
"""

import hashlib
import os
import re
import sqlite3
import stat
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from _bootstrap import ROOT, make_git_dir

from shit import errors, lifecycle, queue_records, store
from shit.errors import ShitError
from shit.project import Project

NOW = "2026-11-27T10:00:00Z"
H64 = "0" * 64


def code_of(func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except ShitError as exc:
        return exc.code
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        make_git_dir(self.repo / ".git")
        self.proj = Project.init(self.repo)
        self.addCleanup(self.proj.close)
        self.db = self.repo / ".shit" / store.DB_NAME

    def init_store(self):
        store.init_store(self.proj)

    def raw(self):
        conn = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(conn.close)
        return conn

    def add_task(self, conn, task_id="alpha"):
        conn.execute("INSERT INTO tasks (id, state, owner, created_at, updated_at) VALUES (?, 'open', 'human', ?, ?)",
                     (task_id, NOW, NOW))


class ConnectSettingsTests(Base):
    def test_settings_read_back(self):
        self.init_store()
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)

        def one(sql):
            return conn.execute(sql).fetchone()[0]

        self.assertEqual(one("PRAGMA foreign_keys"), 1)
        self.assertEqual(one("PRAGMA journal_mode"), "wal")
        self.assertEqual(one("PRAGMA synchronous"), 2)
        self.assertEqual(one("PRAGMA busy_timeout"), store.DEFAULT_BUSY_TIMEOUT_MS)
        self.assertEqual(one("PRAGMA trusted_schema"), 0)
        self.assertEqual(one("PRAGMA application_id"), store.APPLICATION_ID)
        self.assertEqual(one("PRAGMA user_version"), store.SCHEMA_VERSION)
        self.assertIsNone(conn.isolation_level)

    def test_defensive_flag_is_set_when_available(self):
        self.init_store()
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        if hasattr(conn, "getconfig"):
            self.assertTrue(conn.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE))
            conn.execute("PRAGMA writable_schema=ON")
            with self.assertRaises(sqlite3.Error):
                conn.execute("UPDATE sqlite_master SET sql = sql WHERE name = 'tasks'")

    def test_busy_timeout_is_configurable_and_exact(self):
        self.init_store()
        for ms in (0, 1, 1001, 1234):
            with self.subTest(ms=ms):
                conn = store.connect(self.proj, busy_timeout_ms=ms)
                self.addCleanup(conn.close)
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], ms)

    def test_bad_busy_timeout_is_a_usage_error(self):
        self.init_store()
        for bad in (-1, 600001, 1.5, True, "5"):
            with self.subTest(bad=bad):
                self.assertEqual(code_of(store.connect, self.proj, busy_timeout_ms=bad), errors.USAGE)

    def test_missing_database_is_a_missing_precondition(self):
        self.assertEqual(code_of(store.connect, self.proj), errors.PRECONDITION)

    def test_uninitialized_empty_file_is_refused_not_adopted(self):
        os.close(os.open(self.db, os.O_CREAT | os.O_WRONLY, 0o600))
        self.assertEqual(code_of(store.connect, self.proj), errors.FAILED_CLOSED)

    def test_awkward_directory_names_open_the_right_file(self):
        repo = self.base / "we ird%25?#name"
        repo.mkdir()
        make_git_dir(repo / ".git")
        proj = Project.init(repo)
        self.addCleanup(proj.close)
        store.init_store(proj)
        conn = store.connect(proj)
        self.addCleanup(conn.close)
        self.assertTrue((repo / ".shit" / store.DB_NAME).exists())
        self.assertFalse((self.base / "we ird").exists())

    def test_init_is_idempotent(self):
        self.init_store()
        self.init_store()
        conn = store.connect(self.proj)
        conn.close()


class ModeTests(Base):
    def modes(self):
        found = {}
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.db) + suffix)
            if path.exists():
                found[suffix] = stat.S_IMODE(path.stat().st_mode)
        return found

    def check_umask(self, mask):
        old = os.umask(mask)
        try:
            self.init_store()
            conn = store.connect(self.proj)
            self.addCleanup(conn.close)
            with store.write_transaction(conn):
                self.add_task(conn)
            modes = self.modes()
        finally:
            os.umask(old)
        self.assertEqual(set(modes), {"", "-wal", "-shm"})
        for suffix, mode in modes.items():
            self.assertEqual(mode, 0o600, suffix or "db")

    def test_files_are_0600_under_umask_022(self):
        self.check_umask(0o022)

    def test_files_are_0600_under_umask_077(self):
        self.check_umask(0o077)

    def test_umask_is_restored_after_init(self):
        old = os.umask(0o022)
        try:
            self.init_store()
            seen = os.umask(old)
        finally:
            os.umask(old)
        self.assertEqual(seen, 0o022)

    def test_store_directory_is_0700(self):
        self.init_store()
        self.assertEqual(stat.S_IMODE(self.db.parent.stat().st_mode), 0o700)

    def test_wide_database_mode_is_refused(self):
        self.init_store()
        os.chmod(self.db, 0o640)
        self.assertEqual(code_of(store.connect, self.proj), errors.CONFINEMENT)
        os.chmod(self.db, 0o700)
        self.assertEqual(code_of(store.connect, self.proj), errors.CONFINEMENT)

    def test_wide_store_directory_is_refused(self):
        self.init_store()
        os.chmod(self.db.parent, 0o755)
        self.assertEqual(code_of(store.connect, self.proj), errors.CONFINEMENT)


class ConfinementTests(Base):
    def test_symlinked_database_is_refused(self):
        self.init_store()
        target = self.base / "elsewhere.sqlite3"
        os.rename(self.db, target)
        os.symlink(target, self.db)
        self.assertEqual(code_of(store.connect, self.proj), errors.CONFINEMENT)

    def test_hard_linked_database_is_refused(self):
        self.init_store()
        os.link(self.db, self.base / "second-name")
        self.assertEqual(code_of(store.connect, self.proj), errors.CONFINEMENT)

    def test_symlinked_wal_sidecar_is_refused(self):
        self.init_store()
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        wal = Path(str(self.db) + "-wal")
        self.assertTrue(wal.exists())
        other = self.base / "other-wal"
        other.write_bytes(b"")
        os.chmod(other, 0o600)
        keep = self.base / "kept-wal"
        os.rename(wal, keep)
        os.symlink(other, wal)
        self.assertEqual(code_of(store.connect, self.proj), errors.CONFINEMENT)

    def test_non_regular_database_is_refused(self):
        self.init_store()
        os.unlink(self.db)
        os.mkfifo(self.db, 0o600)
        self.assertEqual(code_of(store.connect, self.proj), errors.CONFINEMENT)

    def test_swapped_inode_between_check_and_open_is_refused(self):
        self.init_store()
        real_connect = sqlite3.connect
        swapped = []

        def swapping_connect(*args, **kwargs):
            # The window between the pre-open lstat and the open: put another file at the same name.
            other = self.base / "replacement"
            conn = real_connect(other, isolation_level=None)
            conn.close()
            os.chmod(other, 0o600)
            os.rename(other, self.db)
            swapped.append(True)
            return real_connect(*args, **kwargs)

        with mock.patch.object(store.sqlite3, "connect", swapping_connect):
            self.assertEqual(code_of(store.connect, self.proj), errors.CONFINEMENT)
        self.assertEqual(swapped, [True])

    def test_unswapped_open_passes_the_inode_check(self):
        self.init_store()
        conn = store.connect(self.proj)
        conn.close()

    def test_special_permission_bits_are_refused(self):
        self.init_store()
        for name in ("S_ISUID", "S_ISGID", "S_ISVTX"):
            with self.subTest(bit=name):
                try:
                    os.chmod(self.db, stat.S_IRUSR | stat.S_IWUSR | getattr(stat, name))
                except PermissionError:
                    self.skipTest("this account may not set " + name)
                if not stat.S_IMODE(self.db.stat().st_mode) & getattr(stat, name):
                    self.skipTest("the filesystem dropped " + name)
                self.assertEqual(code_of(store.connect, self.proj), errors.CONFINEMENT)
        os.chmod(self.db, stat.S_IRUSR | stat.S_IWUSR)
        os.chmod(self.db.parent, stat.S_IRWXU | stat.S_ISVTX)
        self.assertEqual(code_of(store.connect, self.proj), errors.CONFINEMENT)
        os.chmod(self.db.parent, stat.S_IRWXU)
        store.connect(self.proj).close()

    def swap_sidecar_on_open(self, suffix, times):
        """Open a second connection while a first holds the sidecars; replace `suffix` inside the open call."""
        holder = store.connect(self.proj)
        self.addCleanup(holder.close)
        real_connect = sqlite3.connect
        calls = []

        def swapping_connect(*args, **kwargs):
            if len(calls) < times:
                other = self.base / ("replacement" + suffix)
                other.write_bytes(b"")
                os.chmod(other, stat.S_IRUSR | stat.S_IWUSR)
                os.rename(other, Path(str(self.db) + suffix))
            calls.append(True)
            return real_connect(*args, **kwargs)

        with mock.patch.object(store.sqlite3, "connect", swapping_connect):
            return code_of(store.connect, self.proj), len(calls)

    def test_swapped_wal_or_shm_during_open_is_refused(self):
        for suffix in ("-wal", "-shm"):
            with self.subTest(sidecar=suffix):
                self.setUp()
                self.init_store()
                code, calls = self.swap_sidecar_on_open(suffix, times=10)
                self.assertEqual(code, errors.CONFINEMENT)
                self.assertEqual(calls, 2)  # one retry, then a refusal

    def test_a_single_sidecar_replacement_is_retried_once(self):
        self.init_store()
        code, calls = self.swap_sidecar_on_open("-wal", times=1)
        self.assertIsNone(code)
        self.assertEqual(calls, 2)

    def test_refusals_carry_only_fixed_text(self):
        self.init_store()
        os.link(self.db, self.base / "second-name")
        try:
            store.connect(self.proj)
        except ShitError as exc:
            self.assertEqual(str(exc), errors.MESSAGES[errors.CONFINEMENT])
            self.assertNotIn(str(self.base), str(exc))
        else:
            self.fail("expected a refusal")


class FloorTests(Base):
    def test_old_sqlite_is_refused(self):
        self.assertEqual(code_of(store._check_floor, sqlite_version=(3, 36, 9)), errors.FAILED_CLOSED)
        self.assertIsNone(code_of(store._check_floor, sqlite_version=(3, 37, 0)))

    def test_old_python_is_refused(self):
        self.assertEqual(code_of(store._check_floor, python_version=(3, 10, 14)), errors.FAILED_CLOSED)
        self.assertIsNone(code_of(store._check_floor, python_version=(3, 11, 0)))

    def test_anything_but_wal_is_refused(self):
        for row in (("delete",), ("memory",), ("truncate",), (None,), None, ()):
            with self.subTest(row=row):
                self.assertEqual(code_of(store._require_wal, row), errors.FAILED_CLOSED)
        self.assertIsNone(code_of(store._require_wal, ("wal",)))
        self.assertIsNone(code_of(store._require_wal, ("WAL",)))

    def test_connect_and_init_apply_the_floor(self):
        with mock.patch.object(store.sqlite3, "sqlite_version_info", (3, 36, 0)):
            self.assertEqual(code_of(store.init_store, self.proj), errors.FAILED_CLOSED)
            self.assertEqual(code_of(store.connect, self.proj), errors.FAILED_CLOSED)
        self.assertFalse(self.db.exists())


class SchemaTests(Base):
    def test_every_table_is_strict_and_present(self):
        self.init_store()
        conn = self.raw()
        names = {row[0] for row in conn.execute("SELECT name FROM pragma_table_list WHERE schema='main'")}
        for table in ("meta", "tasks", "task_history", "events", "records", "queue_entries", "leases", "edges",
                      "apply_slots"):
            self.assertIn(table, names)
        for name, strict in conn.execute("SELECT name, strict FROM pragma_table_list WHERE schema='main' "
                                         "AND name NOT LIKE 'sqlite_%'"):
            self.assertEqual(strict, 1, name)

    def test_checks_come_from_the_queue_vocabulary(self):
        sql = " ".join(store.schema_statements())
        for state in queue_records.STATES:
            self.assertIn(f"'{state}'", sql)
        for kind in queue_records.KINDS:
            self.assertIn(f"'{kind}'", sql)
        for pair in queue_records.ALLOWED:
            self.assertIsNotNone(pair)
        for state in lifecycle.STATES:
            self.assertIn(f"'{state}'", sql)
        for frm, to in lifecycle.LEGAL:
            self.assertIn(f"from_state = '{frm}' AND to_state = '{to}'", sql)

    def test_constraints_reject_bad_rows(self):
        self.init_store()
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        with self.assertRaises(ShitError) as caught, store.write_transaction(conn):
            conn.execute("INSERT INTO tasks (id, state, owner, created_at, updated_at) "
                         "VALUES ('a', 'nonsense', 'human', ?, ?)", (NOW, NOW))
        self.assertEqual(caught.exception.code, errors.REJECTED)
        with self.assertRaises(ShitError) as caught, store.write_transaction(conn):
            conn.execute("INSERT INTO edges (task_id, needs_id, type) VALUES ('a', 'b', 'draft')")
        self.assertEqual(caught.exception.code, errors.PRECONDITION)  # foreign key: no such task
        with self.assertRaises(ShitError) as caught, store.write_transaction(conn):
            self.add_task(conn, "a")
            conn.execute("INSERT INTO edges (task_id, needs_id, type) VALUES ('a', 'a', 'draft')")
        self.assertEqual(caught.exception.code, errors.REJECTED)  # self edge
        self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_append_only_and_no_delete_triggers(self):
        self.init_store()
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        with store.write_transaction(conn):
            self.add_task(conn)
            conn.execute(
                "INSERT INTO events (id, task_id, timestamp, actor, event, message, prev_hash, hash) "
                "VALUES ('e1', 'alpha', ?, 'human', 'task_created', 'created', ?, ?)", (NOW, H64, "1" * 64))
            conn.execute(
                "INSERT INTO queue_entries (id, task_id, timestamp, actor, kind, state_after, prev_hash, hash) "
                "VALUES ('q1', 'alpha', ?, 'human', 'enqueued', 'waiting', ?, ?)", (NOW, H64, "2" * 64))
        attempts = (
            "UPDATE events SET message = 'x'", "DELETE FROM events",
            "UPDATE queue_entries SET message = 'x'", "DELETE FROM queue_entries",
            "DELETE FROM tasks",
        )
        for sql in attempts:
            with self.subTest(sql=sql):
                self.assertEqual(self.code_in_transaction(conn, sql), errors.FAILED_CLOSED)
        self.assertEqual(conn.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM queue_entries").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def code_in_transaction(self, conn, sql):
        try:
            with store.write_transaction(conn):
                conn.execute(sql)
        except ShitError as exc:
            return exc.code
        return None

    def test_every_trigger_token_has_an_exit_code(self):
        self.init_store()
        conn = self.raw()
        tokens = set()
        for (sql,) in conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger'"):
            tokens.update(re.findall(r"RAISE\(ABORT, '(E\d+)'\)", sql))
        self.assertTrue(tokens)
        self.assertEqual(tokens, set(store.TOKEN_EXIT))


class RowFixture(Base):
    """A real store with helpers that insert chained rows the way the later queue code will."""

    LEASE = "a" * 32
    LEASE_B = "b" * 32

    def setUp(self):
        super().setUp()
        self.init_store()
        self.conn = store.connect(self.proj, busy_timeout_ms=200)
        self.addCleanup(self.conn.close)
        self.count = 0
        self.clock = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC)
        with store.write_transaction(self.conn):
            self.add_task(self.conn, "alpha")
            self.add_task(self.conn, "beta")

    def stamp(self, seconds=0):
        return (self.clock + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def tick(self):
        self.count += 1
        self.clock += timedelta(seconds=1)
        return self.count

    def tip(self, table):
        row = self.conn.execute("SELECT hash FROM " + table + " ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else H64

    def insert(self, table, row):
        columns = list(row)
        with store.write_transaction(self.conn):
            self.conn.execute("INSERT INTO " + table + " (" + ", ".join(columns) + ") VALUES ("
                              + ", ".join("?" for _ in columns) + ")", [row[c] for c in columns])

    def put(self, kind, state, actor="wrapper", task="alpha", **cols):
        n = self.tick()
        row = {"id": f"queue-{n:04d}", "task_id": task, "timestamp": self.stamp(), "actor": actor, "kind": kind,
               "state_after": state, "prev_hash": self.tip("queue_entries"),
               "hash": hashlib.sha256(str(n).encode()).hexdigest(), **cols}
        self.insert("queue_entries", row)

    def code(self, func, *args, **kwargs):
        return code_of(func, *args, **kwargs)

    def put_code(self, *args, **kwargs):
        return code_of(self.put, *args, **kwargs)

    def assertRefused(self, code, *allowed):
        """A row can break a CHECK and a trigger at once; SQLite runs BEFORE triggers first. Accept either code."""
        self.assertIn(code, allowed)

    def lease_cols(self, generation=1, lease=None, **more):
        return {"lease_id": lease or self.LEASE, "generation": generation, "worker": "worker-one",
                "worker_kind": "ai", "step": "draft", "expires_at": self.stamp(901), **more}

    def renew_cols(self, expires, lease=None, generation=1):
        return {"lease_id": lease or self.LEASE, "generation": generation, "worker": "worker-one", "step": "draft",
                "expires_at": self.stamp(expires)}

    def started_cols(self, lease=None, generation=1, attempt=1):
        return {"lease_id": lease or self.LEASE, "generation": generation, "worker": "worker-one",
                "step": "draft", "attempt": attempt}

    def reach(self, state):
        """Walk task alpha through a legal sequence to `state`."""
        self.put("enqueued", "waiting", actor="human")
        if state == "waiting":
            return
        self.put("ready", "ready")
        if state == "ready":
            return
        self.put("claimed", "dispatched", **self.lease_cols())
        if state == "dispatched":
            return
        self.put("started", "running", **self.started_cols())
        if state == "running":
            return
        self.put("submitted", "review", lease_id=self.LEASE, generation=1)
        if state == "review":
            return
        self.put("done", "done")


class SemanticHoleTests(RowFixture):
    """Each case reproduces a hole named in the review: the database must refuse what the validators refuse."""

    def history(self, frm, to, actor, at=NOW, reason="x"):
        row = {"task_id": "alpha", "from_state": frm, "to_state": to, "actor": actor, "at": at, "reason": reason}
        return code_of(self.insert, "task_history", row)

    def test_history_actor_must_be_allowed_for_the_transition(self):
        self.assertEqual(self.history("review", "approved", "model"), errors.REJECTED)
        self.assertEqual(self.history("review", "approved", "wrapper"), errors.REJECTED)
        self.assertIsNone(self.history("review", "approved", "human"))
        self.assertEqual(self.history("open", "running", "human"), errors.REJECTED)
        self.assertIsNone(self.history("open", "running", "wrapper"))
        self.assertEqual(self.history("open", "closed", "human"), errors.REJECTED)
        self.assertEqual(self.history("open", "open", "human"), errors.REJECTED)

    def test_every_legal_transition_and_actor_pair_is_accepted_and_only_those(self):
        for frm in lifecycle.STATES:
            for to in lifecycle.STATES:
                for actor in lifecycle.ACTORS:
                    with self.subTest(frm=frm, to=to, actor=actor):
                        expected = actor in lifecycle.LEGAL.get((frm, to), ())
                        self.assertEqual(self.history(frm, to, actor) is None, expected)

    def event(self, **overrides):
        n = self.tick()
        row = {"id": f"event-{n:04d}", "task_id": "alpha", "timestamp": self.stamp(), "actor": "human",
               "event": "task_created", "message": "created", "prev_hash": self.tip("events"),
               "hash": hashlib.sha256(str(n).encode()).hexdigest(), **overrides}
        return code_of(self.insert, "events", row)

    def test_empty_messages_are_refused(self):
        self.assertEqual(self.event(message=""), errors.REJECTED)
        self.assertIsNone(self.event(message="ok"))
        self.assertEqual(self.history("open", "running", "wrapper", reason=""), errors.REJECTED)
        self.assertEqual(self.history("open", "running", "wrapper", reason="x" * 501), errors.REJECTED)

    def test_timestamps_must_be_real_calendar_times(self):
        for bad in ("9999-99-99T99:99:99Z", "2026-02-30T10:00:00Z", "2026-11-27T25:00:00Z", "2026-11-27T10:60:00Z",
                    "2026-13-01T00:00:00Z", "2026-11-27 10:00:00Z", "2026-11-27T10:00:00", "2026-11-27T10:00:00z"):
            with self.subTest(bad=bad):
                self.assertEqual(self.event(timestamp=bad), errors.REJECTED)
        self.assertIsNone(self.event(timestamp="2028-02-29T23:59:59Z"))

    def test_event_chain_must_link_and_time_must_not_go_back(self):
        self.assertIsNone(self.event())
        self.assertEqual(self.event(prev_hash="9" * 64), errors.FAILED_CLOSED)
        self.assertEqual(self.event(prev_hash=H64), errors.FAILED_CLOSED)  # genesis only for the first row
        self.assertEqual(self.event(timestamp="2020-01-01T00:00:00Z"), errors.FAILED_CLOSED)
        self.assertIsNone(self.event())

    def test_queue_chain_must_link_and_time_must_not_go_back(self):
        self.put("enqueued", "waiting", actor="human")
        self.assertEqual(self.put_code("ready", "ready", prev_hash="9" * 64), errors.FAILED_CLOSED)
        self.assertEqual(self.put_code("ready", "ready", timestamp="2020-01-01T00:00:00Z"), errors.FAILED_CLOSED)
        self.put("ready", "ready")

    def test_each_kind_requires_and_forbids_its_own_columns(self):
        self.put("enqueued", "waiting", actor="human")
        self.put("ready", "ready")
        claim = self.lease_cols()
        for name in ("lease_id", "generation", "worker", "worker_kind", "step", "expires_at"):
            with self.subTest(missing=name):
                cols = {k: v for k, v in claim.items() if k != name}
                self.assertRefused(self.put_code("claimed", "dispatched", **cols), errors.REJECTED, errors.LEASE_REFUSED)
        self.assertEqual(self.put_code("claimed", "dispatched", **claim, attempt=1), errors.REJECTED)
        self.assertEqual(self.put_code("claimed", "dispatched", actor="human", **claim), errors.REJECTED)
        self.assertRefused(self.put_code("claimed", "ready", **claim), errors.REJECTED,
                           errors.ILLEGAL_TRANSITION)  # wrong state for the kind
        self.assertRefused(self.put_code("ready", "ready", lease_id=self.LEASE), errors.REJECTED, errors.LEASE_REFUSED,
                           errors.ILLEGAL_TRANSITION)
        self.assertEqual(self.put_code("graph_changed", None), errors.REJECTED)  # shards required
        self.assertEqual(self.put_code("graph_changed", "ready", shards="[]"), errors.REJECTED)  # no state
        self.assertEqual(self.put_code("enqueued", "done", actor="human", task="beta"), errors.REJECTED)
        self.assertRefused(self.put_code("done", "done", actor="model"), errors.REJECTED, errors.ILLEGAL_TRANSITION)

    def test_every_kind_column_rule_comes_from_the_validator(self):
        # The generated rule must accept exactly what queue_records.KINDS lists, so no kind is lost or invented.
        sql = self.raw().execute("SELECT sql FROM sqlite_master WHERE name = 'queue_entries'").fetchone()[0]
        for kind in queue_records.KINDS:
            self.assertIn(f"kind = '{kind}'", sql)
        self.assertEqual(sql.count("(kind = '"), len(queue_records.KINDS))

    def test_held_state_matches_its_hold_kind_and_lease_fields_pair_up(self):
        self.reach("running")
        self.assertEqual(self.put_code("held", "hold-ci", hold_kind="owner", lease_id=self.LEASE, generation=1),
                         errors.REJECTED)
        self.assertRefused(self.put_code("held", "hold-ci", hold_kind="ci", lease_id=self.LEASE), errors.REJECTED,
                           errors.LEASE_REFUSED)
        self.put("held", "hold-ci", hold_kind="ci", lease_id=self.LEASE, generation=1)

    def test_claim_lifetime_is_bounded(self):
        self.put("enqueued", "waiting", actor="human")
        self.put("ready", "ready")
        self.assertEqual(self.put_code("claimed", "dispatched", **{**self.lease_cols(), "expires_at": self.stamp(86402)}),
                         errors.REJECTED)
        self.assertEqual(self.put_code("claimed", "dispatched", **{**self.lease_cols(), "expires_at": self.stamp(-5)}),
                         errors.REJECTED)
        self.put("claimed", "dispatched", **{**self.lease_cols(), "expires_at": self.stamp(86398)})


class TransitionTests(RowFixture):
    def test_a_full_legal_sequence_is_accepted(self):
        self.reach("done")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM queue_entries").fetchone()[0], 6)

    def test_trigger_table_has_exactly_the_allowed_pairs(self):
        sql = self.raw().execute("SELECT sql FROM sqlite_master WHERE name = 'queue_entries_transition'").fetchone()[0]
        pairs = set(re.findall(r"\('([a-z-]+)', '([a-z-]+)'\)", sql))
        self.assertEqual(pairs, set(queue_records.ALLOWED))
        sql = self.raw().execute("SELECT sql FROM sqlite_master WHERE name = 'queue_entries_reopen'").fetchone()[0]
        pairs = set(re.findall(r"\('([a-z-]+)', '([a-z-]+)'\)", sql))
        self.assertEqual(pairs, set(queue_records.ALLOWED | queue_records.REENTER_WAITING))

    def test_illegal_state_changes_are_refused_with_the_transition_code(self):
        self.reach("waiting")
        self.assertRefused(self.put_code("started", "running", **self.started_cols()), errors.ILLEGAL_TRANSITION,
                           errors.LEASE_REFUSED)  # no claim exists either
        self.assertEqual(self.put_code("done", "done"), errors.ILLEGAL_TRANSITION)  # waiting -> done
        self.assertEqual(self.put_code("held", "hold-ci", hold_kind="ci"), errors.ILLEGAL_TRANSITION)
        self.put("ready", "ready")
        self.assertEqual(self.put_code("ready", "ready"), errors.ILLEGAL_TRANSITION)  # a repeat
        self.assertEqual(self.put_code("failed", "failed"), errors.ILLEGAL_TRANSITION)  # ready -> failed

    def test_kind_specific_before_states(self):
        self.reach("dispatched")
        self.assertEqual(self.put_code("submitted", "review", lease_id=self.LEASE, generation=1),
                         errors.ILLEGAL_TRANSITION)
        self.assertEqual(self.put_code("apply_started", "running", lease_id=self.LEASE, generation=1),
                         errors.ILLEGAL_TRANSITION)
        self.put("renewed", "dispatched", **self.renew_cols(1800))
        self.assertEqual(self.put_code("renewed", "running", **self.renew_cols(1900)),
                         errors.ILLEGAL_TRANSITION)  # a renewal never changes state
        self.assertEqual(self.put_code("expired", "failed", lease_id=self.LEASE, generation=1),
                         errors.ILLEGAL_TRANSITION)  # dispatched -> failed is not an expiry
        self.put("expired", "ready", lease_id=self.LEASE, generation=1)

    def test_kind_must_start_from_its_own_state_even_when_the_pair_is_in_the_table(self):
        self.reach("dispatched")
        # dispatched -> ready is a legal pair (a release), but the kind `ready` only follows `waiting`.
        self.assertEqual(self.put_code("ready", "ready"), errors.ILLEGAL_TRANSITION)
        self.put("started", "running", **self.started_cols())
        self.put("held", "hold-ci", hold_kind="ci", lease_id=self.LEASE, generation=1)
        # hold-ci -> running is a legal pair, but only a claim-and-start path gives `started`.
        self.assertEqual(self.put_code("started", "running", **self.started_cols(attempt=2)), errors.ILLEGAL_TRANSITION)

    def test_reopen_and_done_rules(self):
        self.reach("done")
        self.assertEqual(self.put_code("reopened", "ready", actor="human"), errors.ILLEGAL_TRANSITION)  # done is final
        self.setUp()
        self.reach("review")
        self.put("reopened", "waiting", actor="human")

    def test_a_task_is_enqueued_once_and_before_anything_else(self):
        self.assertRefused(self.put_code("ready", "ready"), errors.REJECTED, errors.ILLEGAL_TRANSITION)  # never enqueued
        self.put("enqueued", "waiting", actor="human")
        self.assertEqual(self.put_code("enqueued", "waiting", actor="human"), errors.REJECTED)
        self.put("graph_changed", None, task="beta", shards='[{"task_id":"beta"}]')  # no queue state needed

    def test_claim_generation_goes_up_by_exactly_one(self):
        self.reach("dispatched")
        self.put("released", "ready", lease_id=self.LEASE, generation=1)
        for bad in (1, 3):
            with self.subTest(generation=bad):
                self.assertEqual(self.put_code("claimed", "dispatched", **self.lease_cols(generation=bad, lease=self.LEASE_B)),
                                 errors.LEASE_REFUSED)
        self.put("claimed", "dispatched", **self.lease_cols(generation=2, lease=self.LEASE_B))

    def test_entries_must_name_the_latest_claim(self):
        self.reach("dispatched")
        self.assertEqual(self.put_code("started", "running", **self.started_cols(lease=self.LEASE_B)),
                         errors.LEASE_REFUSED)
        self.assertEqual(self.put_code("started", "running", **self.started_cols(generation=2)), errors.LEASE_REFUSED)
        self.assertEqual(self.put_code("started", "running", **self.started_cols(attempt=2)), errors.REJECTED)
        self.put("started", "running", **self.started_cols())

    def test_held_and_failed_carry_a_lease_exactly_when_one_is_live(self):
        self.reach("running")
        self.assertEqual(self.put_code("failed", "failed"), errors.LEASE_REFUSED)  # lease live but not named
        self.put("failed", "failed", lease_id=self.LEASE, generation=1)
        self.assertEqual(self.put_code("reopened", "ready", actor="human", lease_id=self.LEASE, generation=1),
                         errors.REJECTED)
        self.put("reopened", "ready", actor="human")
        self.put("claimed", "dispatched", **self.lease_cols(generation=2, lease=self.LEASE_B))
        self.assertEqual(self.put_code("held", "hold-owner", hold_kind="owner"), errors.LEASE_REFUSED)


class LeaseSlotTests(RowFixture):
    def lease_row(self, **more):
        return {"task_id": "alpha", "lease_id": self.LEASE, "generation": 1, "step": "draft", "worker": "worker-one",
                "worker_kind": "ai", "claimed_at": self.claimed_at, "expires_at": self.stamp(901), "renewals": 0,
                **more}

    def claim(self, step="draft"):
        self.put("enqueued", "waiting", actor="human")
        self.put("ready", "ready")
        self.put("claimed", "dispatched", **{**self.lease_cols(), "step": step})
        row = self.conn.execute("SELECT timestamp, expires_at FROM queue_entries WHERE kind = 'claimed'").fetchone()
        self.claimed_at, self.expires = row
        return self.lease_row(step=step, expires_at=self.expires)

    def test_a_lease_must_be_the_latest_claim(self):
        self.claimed_at = self.stamp()
        self.assertEqual(code_of(self.insert, "leases", self.lease_row()), errors.LEASE_REFUSED)  # no claim yet
        row = self.claim()
        for name, value in (("worker", "other"), ("step", "review"), ("lease_id", self.LEASE_B), ("generation", 2),
                            ("worker_kind", "human")):
            with self.subTest(field=name):
                self.assertEqual(code_of(self.insert, "leases", {**row, name: value}), errors.LEASE_REFUSED)
        self.insert("leases", row)

    def test_lease_updates_are_exactly_one_renewal(self):
        row = self.claim()
        self.insert("leases", row)
        def update(sql, *params):
            def run():
                with store.write_transaction(self.conn):
                    self.conn.execute(sql, params)
            return code_of(run)
        later = self.stamp(5000)
        self.assertEqual(update("UPDATE leases SET worker = 'x'"), errors.LEASE_REFUSED)
        self.assertEqual(update("UPDATE leases SET lease_id = ?", self.LEASE_B), errors.LEASE_REFUSED)
        self.assertEqual(update("UPDATE leases SET expires_at = ?, renewals = 2", later), errors.LEASE_REFUSED)
        self.assertEqual(update("UPDATE leases SET expires_at = ?, renewals = 0", later), errors.LEASE_REFUSED)
        self.assertEqual(update("UPDATE leases SET expires_at = claimed_at, renewals = 1"), errors.LEASE_REFUSED)
        self.assertIsNone(update("UPDATE leases SET expires_at = ?, renewals = 1", later))

    def test_lease_lifetime_and_renewal_bounds(self):
        row = self.claim()
        self.insert("leases", row)
        too_long = self.stamp(700000)
        def run():
            with store.write_transaction(self.conn):
                self.conn.execute("UPDATE leases SET expires_at = ?, renewals = 1", (too_long,))
        self.assertEqual(code_of(run), errors.REJECTED)

    def test_apply_slots_need_an_apply_step_lease_and_cannot_be_edited(self):
        row = self.claim(step="draft")
        self.insert("leases", row)
        slot = {"target": "docs/a.md", "task_id": "alpha", "lease_id": self.LEASE, "generation": 1,
                "acquired_at": self.stamp()}
        self.assertEqual(code_of(self.insert, "apply_slots", slot), errors.LEASE_REFUSED)  # a draft lease

    def test_apply_slot_lifecycle(self):
        row = self.claim(step="apply")
        self.insert("leases", row)
        slot = {"target": "docs/a.md", "task_id": "alpha", "lease_id": self.LEASE, "generation": 1,
                "acquired_at": self.stamp()}
        self.assertEqual(code_of(self.insert, "apply_slots", {**slot, "generation": 2}), errors.LEASE_REFUSED)
        self.assertEqual(code_of(self.insert, "apply_slots", {**slot, "lease_id": self.LEASE_B}), errors.LEASE_REFUSED)
        self.insert("apply_slots", slot)

        def run(sql):
            def inner():
                with store.write_transaction(self.conn):
                    self.conn.execute(sql)
            return code_of(inner)
        self.assertEqual(run("UPDATE apply_slots SET target = 'docs/b.md'"), errors.LEASE_REFUSED)
        self.assertEqual(run("DELETE FROM leases"), errors.LEASE_REFUSED)  # slot still held
        self.assertIsNone(run("DELETE FROM apply_slots"))
        self.assertIsNone(run("DELETE FROM leases"))


class GuardBuilderTests(unittest.TestCase):
    def test_trigger_builder_refuses_anything_outside_its_alphabet(self):
        for condition in ("1; DROP TABLE tasks", "NEW.a = \"x\"", "NEW.a -- x", "1 /* x */", "NEW.a = 'x' ; --",
                          "NEW.a = \\1"):
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                store._guard("ok_name", "BEFORE INSERT", "tasks", condition, "E7")
        for args in (("bad name", "BEFORE INSERT", "tasks", None, "E7"), ("ok", "AFTER INSERT", "tasks", None, "E7"),
                     ("ok", "BEFORE INSERT", "ta;sks", None, "E7"), ("ok", "BEFORE INSERT", "tasks", None, "E99")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                store._guard(*args)
        self.assertIn("RAISE(ABORT, 'E7')", store._guard("ok_name", "BEFORE DELETE", "tasks", None, "E7"))


class FingerprintTests(Base):
    def tamper(self, *statements):
        self.init_store()
        conn = self.raw()
        for sql in statements:
            conn.execute(sql)
        conn.close()

    def test_untouched_store_verifies_and_fingerprint_is_stable(self):
        self.init_store()
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        self.assertEqual(store.fingerprint(conn), store.expected_fingerprint())
        self.assertRegex(store.expected_fingerprint(), r"^[0-9a-f]{64}$")

    def test_dropped_trigger_is_refused(self):
        self.tamper("DROP TRIGGER events_no_update")
        self.assertEqual(code_of(store.connect, self.proj), errors.FAILED_CLOSED)

    def test_recreated_noop_trigger_is_refused(self):
        self.tamper("DROP TRIGGER events_no_update",
                    "CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT 1; END")
        self.assertEqual(code_of(store.connect, self.proj), errors.FAILED_CLOSED)

    def test_added_table_is_refused(self):
        self.tamper("CREATE TABLE extra (a TEXT)")
        self.assertEqual(code_of(store.connect, self.proj), errors.FAILED_CLOSED)

    def test_added_view_and_index_are_refused(self):
        self.tamper("CREATE VIEW extra_view AS SELECT 1")
        self.assertEqual(code_of(store.connect, self.proj), errors.FAILED_CLOSED)

    def test_altered_table_is_refused(self):
        self.tamper("ALTER TABLE meta ADD COLUMN extra TEXT")
        self.assertEqual(code_of(store.connect, self.proj), errors.FAILED_CLOSED)

    def test_wrong_application_id_is_refused(self):
        self.tamper("PRAGMA application_id = 12345")
        self.assertEqual(code_of(store.connect, self.proj), errors.FAILED_CLOSED)

    def test_other_user_version_is_refused(self):
        for version in (0, 2):
            with self.subTest(version=version):
                self.tamper(f"PRAGMA user_version = {version}")
                self.assertEqual(code_of(store.connect, self.proj), errors.FAILED_CLOSED)
                self.db.unlink()
                for suffix in ("-wal", "-shm"):
                    Path(str(self.db) + suffix).unlink(missing_ok=True)

    def test_non_database_file_is_refused(self):
        self.db.write_bytes(b"not a database" * 100)
        os.chmod(self.db, 0o600)
        self.assertEqual(code_of(store.connect, self.proj), errors.FAILED_CLOSED)

    def test_row_hash_is_unambiguous(self):
        a = store._fingerprint_rows([("ab", "c")])
        b = store._fingerprint_rows([("a", "bc")])
        self.assertNotEqual(a, b)


class TransactionTests(Base):
    def setUp(self):
        super().setUp()
        self.init_store()
        self.conn = store.connect(self.proj, busy_timeout_ms=200)
        self.addCleanup(self.conn.close)

    def test_commit_on_success(self):
        with store.write_transaction(self.conn):
            self.add_task(self.conn)
        other = self.raw()
        self.assertEqual(other.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        self.assertFalse(self.conn.in_transaction)

    def test_rollback_on_any_exception(self):
        class Boom(Exception):
            pass

        with self.assertRaises(Boom), store.write_transaction(self.conn):
            self.add_task(self.conn)
            raise Boom
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_rollback_on_keyboard_interrupt(self):
        with self.assertRaises(KeyboardInterrupt), store.write_transaction(self.conn):
            self.add_task(self.conn)
            raise KeyboardInterrupt
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_sqlite_error_inside_is_mapped_and_rolled_back(self):
        with self.assertRaises(ShitError) as caught, store.write_transaction(self.conn):
            self.add_task(self.conn)
            self.add_task(self.conn)  # primary key clash
        self.assertEqual(caught.exception.code, errors.REJECTED)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_failed_rollback_closes_the_connection_and_fails_closed(self):
        for how in ("error", "silent"):
            with self.subTest(how=how):
                real = store.connect(self.proj, busy_timeout_ms=200)
                proxy = FaultyConnection(real, rollback=how)
                with self.assertRaises(ShitError) as caught, store.write_transaction(proxy):
                    self.add_task(proxy)
                    raise busy_error()
                self.assertEqual(caught.exception.code, errors.FAILED_CLOSED)
                self.assertTrue(proxy.closed)
                with self.assertRaises(sqlite3.ProgrammingError):
                    real.execute("SELECT 1")

    def test_failed_rollback_after_an_interrupt_still_fails_closed(self):
        real = store.connect(self.proj, busy_timeout_ms=200)
        proxy = FaultyConnection(real, rollback="error")
        with self.assertRaises(ShitError) as caught, store.write_transaction(proxy):
            raise KeyboardInterrupt
        self.assertEqual(caught.exception.code, errors.FAILED_CLOSED)
        self.assertTrue(proxy.closed)

    def test_failed_commit_with_a_good_rollback_keeps_the_connection(self):
        real = store.connect(self.proj, busy_timeout_ms=200)
        self.addCleanup(real.close)
        proxy = FaultyConnection(real, commit="busy")
        with self.assertRaises(ShitError) as caught, store.write_transaction(proxy):
            self.add_task(proxy)
        self.assertEqual(caught.exception.code, errors.BUSY)
        self.assertFalse(proxy.closed)
        self.assertFalse(real.in_transaction)
        self.assertEqual(real.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_begin_immediate_is_used(self):
        seen = []
        self.conn.set_trace_callback(seen.append)
        with store.write_transaction(self.conn):
            pass
        self.conn.set_trace_callback(None)
        self.assertEqual(seen[0], "BEGIN IMMEDIATE")
        self.assertNotIn("BEGIN", [s for s in seen if s != "BEGIN IMMEDIATE"])

    def test_second_writer_blocks_then_gets_the_busy_code(self):
        second = store.connect(self.proj, busy_timeout_ms=200)
        self.addCleanup(second.close)
        with store.write_transaction(self.conn):
            start = time.monotonic()
            with self.assertRaises(ShitError) as caught, store.write_transaction(second):
                self.fail("the second writer must not enter")
            elapsed = time.monotonic() - start
        self.assertEqual(caught.exception.code, errors.BUSY)
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 5)
        self.assertFalse(second.in_transaction)
        with store.write_transaction(second):  # free again once the first writer committed
            pass

    def test_zero_timeout_fails_immediately(self):
        second = store.connect(self.proj, busy_timeout_ms=0)
        self.addCleanup(second.close)
        with store.write_transaction(self.conn):
            start = time.monotonic()
            self.assertEqual(code_of(lambda: store.write_transaction(second).__enter__()), errors.BUSY)
            self.assertLess(time.monotonic() - start, 0.15)

    def test_readers_are_not_blocked_by_a_writer(self):
        reader = store.connect(self.proj, busy_timeout_ms=0)
        self.addCleanup(reader.close)
        with store.write_transaction(self.conn):
            self.add_task(self.conn)
            self.assertEqual(reader.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_busy_message_is_fixed(self):
        second = store.connect(self.proj, busy_timeout_ms=0)
        self.addCleanup(second.close)
        with store.write_transaction(self.conn):
            try:
                store.write_transaction(second).__enter__()
            except ShitError as exc:
                self.assertEqual(str(exc), errors.MESSAGES[errors.BUSY])
                self.assertNotIn(str(self.base), str(exc))


def busy_error():
    exc = sqlite3.OperationalError("database is locked")
    exc.sqlite_errorcode = 5
    return exc


def io_error():
    exc = sqlite3.OperationalError("disk I/O error")
    exc.sqlite_errorcode = 10
    return exc


class FaultyConnection:
    """A real connection with one injected fault: ROLLBACK that errors or does nothing, or a COMMIT that is busy."""

    def __init__(self, real, rollback=None, commit=None):
        self.real, self.rollback, self.commit, self.closed = real, rollback, commit, False

    @property
    def in_transaction(self):
        return self.real.in_transaction

    def execute(self, sql, *args):
        if sql == "ROLLBACK" and self.rollback == "error":
            raise io_error()
        if sql == "ROLLBACK" and self.rollback == "silent":
            return None  # the transaction stays open
        if sql == "COMMIT" and self.commit == "busy":
            raise busy_error()
        return self.real.execute(sql, *args)

    def close(self):
        self.closed = True
        self.real.close()


class ErrorMappingTests(Base):
    def mapped(self, cls, extended, text="detail with /some/path and SELECT secret"):
        exc = cls(text)
        exc.sqlite_errorcode = extended
        err = store.map_error(exc)
        self.assertEqual(str(err), errors.MESSAGES[err.code])
        self.assertNotIn("path", str(err))
        return err.code

    def test_table(self):
        op, integ, db = sqlite3.OperationalError, sqlite3.IntegrityError, sqlite3.DatabaseError
        cases = (
            (op, 5, errors.BUSY), (op, 6, errors.BUSY), (op, 261, errors.BUSY),
            (op, 517, errors.BUSY),  # BUSY_SNAPSHOT
            (op, 262, errors.BUSY),  # LOCKED_SHAREDCACHE
            (op, 8, errors.FAILED_CLOSED), (op, 1544, errors.FAILED_CLOSED),  # READONLY, READONLY_DIRECTORY
            (op, 14, errors.FAILED_CLOSED),  # CANTOPEN
            (db, 11, errors.FAILED_CLOSED), (db, 26, errors.FAILED_CLOSED),  # CORRUPT, NOTADB
            (op, 10, errors.FAILED_CLOSED), (op, 13, errors.FAILED_CLOSED),  # IOERR, FULL
            (op, 1, errors.INTERNAL),
            (integ, 787, errors.PRECONDITION),  # FOREIGNKEY
            (integ, 275, errors.REJECTED), (integ, 1299, errors.REJECTED),  # CHECK, NOTNULL
            (integ, 1555, errors.REJECTED), (integ, 2067, errors.REJECTED),  # PRIMARYKEY, UNIQUE
        )
        for cls, extended, expected in cases:
            with self.subTest(cls=cls.__name__, extended=extended):
                self.assertEqual(self.mapped(cls, extended), expected)

    def test_unknown_error_is_internal(self):
        self.assertEqual(store.map_error(sqlite3.ProgrammingError("x")).code, errors.INTERNAL)
        self.assertEqual(store.map_error(ValueError("x")).code, errors.INTERNAL)

    def test_trigger_tokens_map_by_table(self):
        for token, code in store.TOKEN_EXIT.items():
            with self.subTest(token=token):
                self.assertEqual(self.mapped(sqlite3.IntegrityError, 1811, token), code)
        self.assertEqual(self.mapped(sqlite3.IntegrityError, 1811, "E999"), errors.FAILED_CLOSED)
        self.assertEqual(self.mapped(sqlite3.IntegrityError, 1811, "free text"), errors.FAILED_CLOSED)

    def test_read_only_directory_is_a_fixed_error(self):
        if os.geteuid() == 0:
            self.skipTest("permissions are not enforced for root")
        self.init_store()
        os.chmod(self.db.parent, 0o500)
        self.addCleanup(os.chmod, self.db.parent, 0o700)
        self.assertEqual(code_of(store.connect, self.proj), errors.FAILED_CLOSED)

    def test_busy_code_is_documented_and_distinct(self):
        self.assertEqual(errors.BUSY, 13)
        self.assertEqual(errors.MESSAGES[13], "store busy")
        text = (ROOT / "docs" / "exit-codes.md").read_text(encoding="utf-8")
        self.assertRegex(text, r"\| 13 \| store_busy \| `store busy` \|")


if __name__ == "__main__":
    unittest.main()
