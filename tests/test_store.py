"""SQLite store foundation tests (issue 59): connect, confinement, floor, schema, fingerprint, errors, lint.

Real database files in a temporary directory; nothing about sqlite is mocked. Tampering is done with a
plain `sqlite3` connection opened by the test itself, which is allowed outside `shit/`.
"""

import hashlib
import json
import os
import random
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

from shit import errors, lifecycle, queue_records, records, store
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

    def test_symlinked_rollback_journal_is_refused(self):
        self.init_store()
        other = self.base / "other-journal"
        other.write_bytes(b"")
        os.chmod(other, stat.S_IRUSR | stat.S_IWUSR)
        os.symlink(other, str(self.db) + "-journal")
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

    def test_a_sidecar_that_disappears_is_not_accepted(self):
        self.init_store()
        holder = store.connect(self.proj)
        self.addCleanup(holder.close)
        path = str(self.db)
        for suffix in ("-wal", "-shm"):
            with self.subTest(sidecar=suffix):
                snapshot = {s: store._lstat(path + s, required=False) for s in store._SIDECARS}
                self.assertIsNotNone(snapshot[suffix])
                os.rename(path + suffix, path + suffix + ".away")
                try:
                    self.assertRaises(store._SidecarMoved, store._verify_sidecars, path, snapshot, False)
                    with self.assertRaises(ShitError) as caught:
                        store._verify_sidecars(path, snapshot, True)
                    self.assertEqual(caught.exception.code, errors.CONFINEMENT)
                finally:
                    os.rename(path + suffix + ".away", path + suffix)
        none = dict.fromkeys(store._SIDECARS)
        os.rename(path + "-wal", path + "-wal.away")
        try:
            store._verify_sidecars(path, none, True)  # absent before and after: fine
        finally:
            os.rename(path + "-wal.away", path + "-wal")

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
                "VALUES ('queue-0001', 'alpha', ?, 'human', 'enqueued', 'waiting', ?, ?)", (NOW, H64, "2" * 64))
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
        position = self.conn.execute("SELECT count(*) FROM queue_entries").fetchone()[0] + 1
        row = {"id": f"queue-{position:04d}", "task_id": task, "timestamp": self.stamp(), "actor": actor, "kind": kind,
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
        self.put("graph_changed", None, task="beta", shards='[{"task_id":"beta","sha256":"' + "1" * 64 + '"}]')  # no queue state needed

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


def entry_dicts(conn):
    """The queue log of a store, as the validated records `queue_records.replay` reads."""
    cursor = conn.execute("SELECT * FROM queue_entries WHERE kind <> 'graph_changed' ORDER BY seq")
    names = [d[0] for d in cursor.description]
    out = []
    for number, row in enumerate(cursor.fetchall(), start=1):
        entry = {"schema": "queue-entry", "schema_version": 1}
        for name, value in zip(names, row, strict=True):
            if value is not None and name not in ("seq", "prev_hash", "hash", "shards"):
                entry[name] = value
        entry["id"] = f"queue-{number:04d}"
        out.append(queue_records.validate_entry(entry))
    return out


class LeaseConsistencyTests(RowFixture):
    """Queue rows must agree with the lease they claim, as `queue_records.replay` demands (review round 2)."""

    def replays(self):
        queue_records.replay(entry_dicts(self.conn))

    def test_start_and_renewal_must_use_the_claims_worker_and_step(self):
        self.reach("dispatched")
        self.assertEqual(self.put_code("started", "running", **{**self.started_cols(), "worker": "other"}),
                         errors.LEASE_REFUSED)
        self.assertEqual(self.put_code("started", "running", **{**self.started_cols(), "step": "review"}),
                         errors.LEASE_REFUSED)
        self.put("started", "running", **self.started_cols())
        for change in ({"worker": "other"}, {"step": "review"}):
            with self.subTest(change=change):
                self.assertEqual(self.put_code("renewed", "running", **{**self.renew_cols(1800), **change}),
                                 errors.LEASE_REFUSED)
        self.replays()

    def test_a_renewal_must_extend_and_come_before_expiry_within_the_ceilings(self):
        self.reach("running")  # claim expires 901 seconds after the claim
        cases = {
            "shortens the lease": self.renew_cols(60),
            "does not change the expiry": self.renew_cols(901 - 4),
            "more than a day past the renewal time": self.renew_cols(90000),
        }
        for label, cols in cases.items():
            with self.subTest(label=label):
                self.assertEqual(self.put_code("renewed", "running", **cols), errors.LEASE_REFUSED)
        self.put("renewed", "running", **self.renew_cols(1800))
        self.clock += timedelta(seconds=2000)  # now past the new expiry
        self.assertEqual(self.put_code("renewed", "running", **self.renew_cols(2800 + 2000)), errors.LEASE_REFUSED)
        self.replays()

    def test_a_renewal_after_expiry_is_refused(self):
        self.reach("running")
        self.clock += timedelta(seconds=1000)
        self.assertEqual(self.put_code("renewed", "running", **self.renew_cols(1500)), errors.LEASE_REFUSED)

    def test_total_lifetime_and_renewal_count_are_capped(self):
        self.reach("running")
        claim = datetime.strptime(self.conn.execute("SELECT timestamp FROM queue_entries WHERE kind = 'claimed'")
                                  .fetchone()[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        expiry = claim + timedelta(seconds=901)
        accepted, code = 0, None
        for _ in range(12):
            self.clock = expiry - timedelta(seconds=100)  # renew just before the lease would lapse
            expiry += timedelta(seconds=80000)
            cols = {**self.renew_cols(0), "expires_at": expiry.strftime("%Y-%m-%dT%H:%M:%SZ")}
            code = self.put_code("renewed", "running", **cols)
            if code is not None:
                break
            accepted += 1
        self.assertEqual(code, errors.LEASE_REFUSED)
        self.assertEqual(accepted, 7)  # 901 + 8 * 80,000 would pass the 604,800-second ceiling
        self.replays()

    def test_renewal_count_ceiling(self):
        self.reach("running")
        claim_expiry = self.conn.execute("SELECT expires_at FROM queue_entries WHERE kind = 'claimed'").fetchone()[0]
        expiry = datetime.strptime(claim_expiry, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        base = self.tip("queue_entries")
        position = self.conn.execute("SELECT count(*) FROM queue_entries").fetchone()[0] + 1
        with store.write_transaction(self.conn):
            for number in range(queue_records.MAX_RENEWALS + 1):
                expiry += timedelta(seconds=1)
                row = {"id": f"queue-{position + number:04d}", "task_id": "alpha", "timestamp": self.stamp(number),
                       "actor": "wrapper", "kind": "renewed", "state_after": "running", "lease_id": self.LEASE,
                       "generation": 1, "worker": "worker-one", "step": "draft",
                       "expires_at": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"), "prev_hash": base,
                       "hash": hashlib.sha256(str(number + 5000).encode()).hexdigest()}
                columns = list(row)
                try:
                    self.conn.execute("INSERT INTO queue_entries (" + ", ".join(columns) + ") VALUES ("
                                      + ", ".join("?" for _ in columns) + ")", [row[c] for c in columns])
                except sqlite3.IntegrityError as exc:
                    self.assertEqual(number, queue_records.MAX_RENEWALS)
                    self.assertEqual(store.map_error(exc).code, errors.LEASE_REFUSED)
                    return
                base = row["hash"]
        self.fail("the 1,001st renewal was accepted")

    def apply_claim(self):
        self.put("enqueued", "waiting", actor="human")
        self.put("ready", "ready")
        self.put("claimed", "dispatched", **{**self.lease_cols(), "step": "apply"})
        self.put("started", "running", **{**self.started_cols(), "step": "apply"})

    def test_apply_markers_need_an_apply_claim_and_the_order_none_started_finished(self):
        self.reach("running")  # a draft claim
        marker = {"lease_id": self.LEASE, "generation": 1}
        self.assertEqual(self.put_code("apply_started", "running", **marker), errors.ILLEGAL_TRANSITION)
        self.setUp()
        self.apply_claim()
        self.assertEqual(self.put_code("apply_finished", "running", **marker), errors.ILLEGAL_TRANSITION)
        self.put("apply_started", "running", **marker)
        self.assertEqual(self.put_code("apply_started", "running", **marker), errors.ILLEGAL_TRANSITION)
        self.put("apply_finished", "running", **marker)
        self.assertEqual(self.put_code("apply_finished", "running", **marker), errors.ILLEGAL_TRANSITION)
        self.replays()

    def test_a_claim_needs_no_live_lease_row_and_respects_not_before(self):
        self.reach("dispatched")
        self.put("dispatch_failed", "ready", lease_id=self.LEASE, generation=1, not_before=self.stamp(600))
        self.assertEqual(self.put_code("claimed", "dispatched", **self.lease_cols(generation=2, lease=self.LEASE_B)),
                         errors.LEASE_REFUSED)  # before not_before
        self.clock += timedelta(seconds=700)
        self.put("claimed", "dispatched", **self.lease_cols(generation=2, lease=self.LEASE_B))
        self.replays()

    def lease_row_for(self, claim_cols):
        row = self.conn.execute("SELECT timestamp, expires_at FROM queue_entries WHERE kind = 'claimed' "
                                "ORDER BY seq DESC LIMIT 1").fetchone()
        return {"task_id": "alpha", "lease_id": claim_cols["lease_id"], "generation": claim_cols["generation"],
                "step": claim_cols["step"], "worker": claim_cols["worker"], "worker_kind": claim_cols["worker_kind"],
                "claimed_at": row[0], "expires_at": row[1], "renewals": 0}

    def test_claim_is_refused_while_a_lease_row_is_live(self):
        self.reach("dispatched")
        self.insert("leases", self.lease_row_for(self.lease_cols()))
        self.put("released", "ready", lease_id=self.LEASE, generation=1)
        self.assertEqual(self.put_code("claimed", "dispatched", **self.lease_cols(generation=2, lease=self.LEASE_B)),
                         errors.LEASE_REFUSED)  # the old lease row was not deleted first
        with store.write_transaction(self.conn):
            self.conn.execute("DELETE FROM leases")
        self.put("claimed", "dispatched", **self.lease_cols(generation=2, lease=self.LEASE_B))

    def update_lease(self, expires, renewals):
        def run():
            with store.write_transaction(self.conn):
                self.conn.execute("UPDATE leases SET expires_at = ?, renewals = ?", (expires, renewals))
        return code_of(run)

    def test_entries_and_the_lease_row_agree_in_both_write_orders(self):
        self.reach("dispatched")
        claim = self.lease_cols()
        self.insert("leases", self.lease_row_for(claim))
        self.put("started", "running", **self.started_cols())
        base = self.clock

        def at(seconds):
            return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def renew(seconds):
            return {**self.renew_cols(0), "expires_at": at(seconds)}

        # Entry first, then the lease row.
        self.put("renewed", "running", **renew(1800))
        self.assertIsNone(self.update_lease(at(1800), 1))
        # Lease row first, then the entry.
        self.assertIsNone(self.update_lease(at(2700), 2))
        self.put("renewed", "running", **renew(2700))
        # An entry that disagrees with the lease row is refused.
        self.assertIsNone(self.update_lease(at(3600), 3))
        self.assertEqual(self.put_code("renewed", "running", **renew(3700)), errors.LEASE_REFUSED)
        self.put("renewed", "running", **renew(3600))
        self.replays()

    def test_the_lease_row_may_not_be_shorter_than_the_log_or_jump_more_than_a_day(self):
        self.reach("running")
        self.insert("leases", self.lease_row_for(self.lease_cols()))
        self.put("renewed", "running", **self.renew_cols(1800))
        logged = self.conn.execute("SELECT expires_at FROM queue_entries WHERE kind = 'renewed'").fetchone()[0]
        self.assertEqual(self.update_lease(self.stamp(1000), 1), errors.LEASE_REFUSED)  # shorter than the log
        self.assertEqual(self.update_lease(self.stamp(1800 + 90000), 1), errors.LEASE_REFUSED)  # beyond a day
        self.assertIsNone(self.update_lease(logged, 1))

    def test_a_started_entry_must_match_an_existing_lease_row(self):
        self.reach("dispatched")
        row = self.lease_row_for(self.lease_cols())
        self.insert("leases", row)
        self.put("started", "running", **self.started_cols())


class ReplayDifferentialTests(RowFixture):
    """Whatever the database accepts, `queue_records.replay` must accept too (seeded random sequences)."""

    KINDS = ("enqueued", "ready", "claimed", "started", "renewed", "released", "expired", "dispatch_failed",
             "apply_started", "apply_finished", "submitted", "held", "failed", "reopened", "done", "orphan_removed")

    def candidate(self, rng, task, last):
        kind = rng.choice(self.KINDS)
        lease = last.get("lease") or self.LEASE
        generation = last.get("generation", 1)
        worker, step = last.get("worker", "worker-one"), last.get("step", "draft")
        if rng.random() < 0.15:
            worker = "other"
        if rng.random() < 0.15:
            step = rng.choice(("apply", "review", "draft"))
        if rng.random() < 0.1:
            generation += rng.choice((-1, 1))
        if rng.random() < 0.1:
            lease = rng.choice((self.LEASE, self.LEASE_B))
        stamp = self.stamp
        states = {"enqueued": ("waiting", "ready"), "ready": ("ready",), "claimed": ("dispatched",),
                  "started": ("running",), "renewed": ("dispatched", "running"), "released": ("ready",),
                  "expired": ("ready", "failed"), "dispatch_failed": ("ready",), "apply_started": ("running",),
                  "apply_finished": ("running",), "submitted": ("review",),
                  "held": ("hold-owner", "hold-ci"), "failed": ("failed",), "reopened": ("ready", "waiting"),
                  "done": ("done",), "orphan_removed": queue_records.STATES}[kind]
        state = rng.choice(states)
        cols = {}
        actor = "human" if kind in ("enqueued", "reopened") else "wrapper"
        if kind == "claimed":
            cols = {"lease_id": lease, "generation": last.get("generation", 0) + rng.choice((1, 1, 1, 2)),
                    "worker": worker, "worker_kind": "ai", "step": step,
                    "expires_at": stamp(rng.choice((100, 900, 5000)))}
        elif kind == "started":
            cols = {"lease_id": lease, "generation": generation, "worker": worker, "step": step,
                    "attempt": last.get("attempts", 0) + rng.choice((1, 1, 2))}
        elif kind == "renewed":
            cols = {"lease_id": lease, "generation": generation, "worker": worker, "step": step,
                    "expires_at": stamp(rng.choice((50, 1000, 2000, 100000)))}
        elif kind in ("released", "expired", "submitted", "apply_started", "apply_finished"):
            cols = {"lease_id": lease, "generation": generation}
        elif kind == "dispatch_failed":
            cols = {"lease_id": lease, "generation": generation, "not_before": stamp(rng.choice((10, 400)))}
        elif kind in ("held", "failed"):
            if rng.random() < 0.6:
                cols = {"lease_id": lease, "generation": generation}
            if kind == "held":
                cols["hold_kind"] = state.split("-")[1]
        return kind, state, actor, cols

    def test_database_acceptance_implies_replay_acceptance(self):
        rng = random.Random(59)
        accepted_kinds = set()
        last = {}
        for round_number in range(350):
            kind, state, actor, cols = self.candidate(rng, "alpha", last)
            number = self.conn.execute("SELECT count(*) FROM queue_entries WHERE kind <> 'graph_changed'").fetchone()[0]
            self.count = number
            try:
                self.put(kind, state, actor=actor, **cols)
            except ShitError:
                self.clock += timedelta(seconds=rng.choice((0, 0, 30, 300)))
                continue
            accepted_kinds.add(kind)
            if kind == "claimed":
                last = {**last, "lease": cols["lease_id"], "generation": cols["generation"], "worker": cols["worker"],
                        "step": cols["step"], "attempts": last.get("attempts", 0)}
            if kind == "started":
                last["attempts"] = cols["attempt"]
            queue_records.replay(entry_dicts(self.conn))  # raises ShitError(3) if the log is not replayable
        self.assertGreaterEqual(len(accepted_kinds), 9, accepted_kinds)
        self.assertGreater(self.conn.execute("SELECT count(*) FROM queue_entries").fetchone()[0], 15)


class IntegrityTests(RowFixture):
    """Review round 3: REPLACE, sequence numbers, NULs, lifecycle authority on events, shards, timestamp parity."""

    def run_sql(self, sql, params=()):
        def run():
            with store.write_transaction(self.conn):
                self.conn.execute(sql, params)
        return code_of(run)

    def event_row(self, **overrides):
        n = self.tick()
        return {"id": f"event-{n:04d}", "task_id": "alpha", "timestamp": self.stamp(), "actor": "human",
                "event": "task_created", "message": "created", "prev_hash": self.tip("events"),
                "hash": hashlib.sha256(str(n).encode()).hexdigest(), **overrides}

    def test_recursive_triggers_are_on(self):
        self.assertEqual(self.conn.execute("PRAGMA recursive_triggers").fetchone()[0], 1)

    def test_insert_or_replace_cannot_rewrite_or_delete_protected_rows(self):
        self.insert("events", self.event_row())
        self.put("enqueued", "waiting", actor="human")
        self.insert("task_history", {"task_id": "alpha", "from_state": "open", "to_state": "running",
                                     "actor": "wrapper", "at": NOW, "reason": "x"})
        tables = ("events", "queue_entries", "task_history", "tasks")
        before = {t: self.conn.execute("SELECT * FROM " + t).fetchall() for t in tables}
        shards = '[{"task_id":"alpha","sha256":"' + "1" * 64 + '"}]'
        events_sql = ("INSERT OR REPLACE INTO events (seq, id, task_id, timestamp, actor, event, message, prev_hash, "
                      "hash) VALUES (1, 'event-9999', 'alpha', ?, 'human', 'task_created', 'changed', ?, ?)")
        history_sql = ("INSERT OR REPLACE INTO task_history (id, task_id, from_state, to_state, actor, at, reason) "
                       "VALUES (1, 'alpha', 'open', 'running', 'wrapper', ?, 'changed')")
        queue_sql = ("INSERT OR REPLACE INTO queue_entries (seq, id, task_id, timestamp, actor, kind, shards, "
                     "prev_hash, hash) VALUES (1, 'queue-0001', 'alpha', ?, 'human', 'graph_changed', ?, ?, ?)")
        tasks_sql = ("INSERT OR REPLACE INTO tasks (id, state, owner, created_at, updated_at) "
                     "VALUES ('alpha', 'closed', 'human', ?, ?)")
        replace_sql = ("REPLACE INTO events (seq, id, task_id, timestamp, actor, event, message, prev_hash, hash) "
                       "VALUES (1, 'event-9998', 'alpha', ?, 'human', 'task_created', 'again', ?, ?)")
        attempts = (
            (events_sql, (self.stamp(), H64, "7" * 64)),
            (history_sql, (NOW,)),
            (queue_sql, (self.stamp(), shards, H64, "8" * 64)),
            (tasks_sql, (NOW, NOW)),
            (replace_sql, (self.stamp(), H64, "6" * 64)),
        )
        for sql, params in attempts:
            with self.subTest(sql=sql[:40]):
                self.assertEqual(self.run_sql(sql, params), errors.FAILED_CLOSED)
        after = {t: self.conn.execute("SELECT * FROM " + t).fetchall() for t in before}
        self.assertEqual(after, before)

    def test_sequence_numbers_are_previous_plus_one_however_supplied(self):
        self.insert("events", self.event_row())
        for seq in (0, -1, 1, 3, 100):
            with self.subTest(table="events", seq=seq):
                code = code_of(self.insert, "events", {**self.event_row(), "seq": seq})
                self.assertIn(code, (errors.FAILED_CLOSED, errors.REJECTED))  # a taken number is a key clash
        self.insert("events", {**self.event_row(), "seq": 2})
        self.put("enqueued", "waiting", actor="human")
        for seq in (0, -3, 1, 3, 50):
            with self.subTest(table="queue_entries", seq=seq):
                row = {"id": f"queue-{seq:04d}", "task_id": "alpha", "timestamp": self.stamp(5), "actor": "wrapper",
                       "kind": "ready", "state_after": "ready", "prev_hash": self.tip("queue_entries"),
                       "hash": "5" * 64, "seq": seq}
                code = code_of(self.insert, "queue_entries", row)
                # Everything but the number is valid, so only the sequence rule (or the key, for a taken number) refuses.
                self.assertEqual(code, errors.REJECTED if seq == 1 else errors.FAILED_CLOSED)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM queue_entries").fetchone()[0], 1)

    def test_the_first_row_must_be_number_one(self):
        self.assertEqual(code_of(self.insert, "events", {**self.event_row(), "seq": 2}), errors.FAILED_CLOSED)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], 0)

    def test_queue_ids_follow_the_sequence(self):
        self.put("enqueued", "waiting", actor="human")
        self.assertEqual(self.put_code("ready", "ready", id="attacker-chosen"), errors.REJECTED)

    def test_embedded_nul_bytes_are_refused_everywhere_text_is_checked(self):
        nul = chr(0)
        cases = (
            ("tasks", {"id": "gamma" + nul + "../../x", "state": "open", "owner": "human", "created_at": NOW,
                       "updated_at": NOW}),
            ("tasks", {"id": "d" + nul + "a" * 5000, "state": "open", "owner": "human", "created_at": NOW,
                       "updated_at": NOW}),
            ("tasks", {"id": "delta", "state": "open", "owner": "human", "created_at": NOW + nul + "x",
                       "updated_at": NOW}),
            ("tasks", {"id": "epsilon", "state": "blocked", "owner": "human", "created_at": NOW, "updated_at": NOW,
                       "blocked_reason": "ok" + nul + "x" * 600, "blocking_item": "y"}),
            ("events", self.event_row(message="ok" + nul + "x" * 1000)),
            ("events", self.event_row(hash="1" * 64 + nul + "not-hex")),
            ("events", self.event_row(id="event-x" + nul + "y")),
            ("meta", {"key": "k" + nul + "x", "value": "v"}),
            ("meta", {"key": "kk", "value": "v" + nul + "x"}),
        )
        for table, row in cases:
            with self.subTest(table=table, row=str(row)[:70]):
                self.assertEqual(code_of(self.insert, table, row), errors.REJECTED)

    def test_events_obey_the_lifecycle_actor_table_like_history_does(self):
        for frm in lifecycle.STATES:
            for to in lifecycle.STATES:
                for actor in lifecycle.ACTORS:
                    with self.subTest(frm=frm, to=to, actor=actor):
                        python_ok = code_of(lifecycle.check_transition, frm, to, actor) is None
                        sql_ok = code_of(self.insert, "events", self.event_row(
                            event="state_transition", from_state=frm, to_state=to, actor=actor)) is None
                        self.assertEqual(sql_ok, python_ok)

    def test_exit_code_vocabulary_is_one_set(self):
        for code in range(16):
            with self.subTest(code=code):
                event = {"schema": "event-log", "schema_version": 1, "id": "event-0001", "task_id": "alpha",
                         "timestamp": NOW, "actor": "wrapper", "event": "command_failed", "message": "failed",
                         "exit_code": code}
                python_ok = code_of(records.validate_event, event) is None
                sql_ok = code_of(self.insert, "events", self.event_row(event="command_failed", exit_code=code)) is None
                self.assertEqual(sql_ok, python_ok)
                self.assertEqual(python_ok, code in errors.STABLE_EXIT_CODES)
        self.assertIn(12, errors.STABLE_EXIT_CODES)
        self.assertIn(13, errors.STABLE_EXIT_CODES)

    def test_timestamps_agree_with_the_python_validators(self):
        stamps = ("0000-01-01T00:00:00Z", "0001-01-01T00:00:00Z", "2026-11-27T24:00:00Z", "2026-11-27T24:59:59Z",
                  "2026-11-27T23:59:59Z", "2026-11-27T00:00:00Z", "2024-02-29T12:00:00Z", "2026-02-29T12:00:00Z",
                  "9999-12-31T23:59:59Z", "2026-04-31T00:00:00Z", "2026-11-27T12:60:00Z", "2026-11-27T12:00:60Z",
                  "2026-00-10T00:00:00Z", "2026-11-00T00:00:00Z")
        for number, stamp in enumerate(stamps):
            with self.subTest(stamp=stamp):
                try:
                    queue_records._parse(stamp)
                    python_ok = True
                except ValueError:
                    python_ok = False
                row = {"id": f"alpha-{number}", "state": "open", "owner": "human", "created_at": stamp,
                       "updated_at": "9999-12-31T23:59:59Z"}
                self.assertEqual(code_of(self.insert, "tasks", row) is None, python_ok)

    def shards(self, *pairs):
        return json.dumps([{"task_id": task, "sha256": digest} for task, digest in pairs], separators=(",", ":"))

    def test_graph_shards_must_be_a_sorted_list_of_task_and_digest(self):
        digest = "1" * 64
        good = self.shards(("alpha", digest), ("beta", digest))
        bad = {
            "not json": "x", "object": '{"task_id":"alpha"}', "empty": "[]",
            "missing digest": '[{"task_id":"alpha"}]',
            "extra key": '[{"task_id":"alpha","sha256":"' + digest + '","more":1}]',
            "digest not hex": self.shards(("alpha", "z" * 64)), "digest short": self.shards(("alpha", "1" * 63)),
            "unsorted": self.shards(("beta", digest), ("alpha", digest)),
            "duplicate": self.shards(("alpha", digest), ("alpha", digest)),
            "first is not the entry's task": self.shards(("beta", digest)),
            "task id not a slug": self.shards(("Alpha", digest)),
            "non-object element": '["alpha"]',
        }
        for label, text in bad.items():
            with self.subTest(label):
                self.assertEqual(self.put_code("graph_changed", None, shards=text), errors.REJECTED)
        self.put("graph_changed", None, shards=good)
        many = self.shards(*[(f"t{i:03d}", digest) for i in range(33)])
        self.assertEqual(self.put_code("graph_changed", None, task="t000", shards=many), errors.REJECTED)


class VerifyTests(RowFixture):
    """`store.verify` names every cross-statement divergence the triggers cannot see."""

    def lease_row_for(self, cols):
        return LeaseConsistencyTests.lease_row_for(self, cols)

    def test_a_consistent_store_has_no_findings(self):
        self.assertEqual(store.verify(self.conn, deep=True), [])
        self.reach("dispatched")
        self.insert("leases", self.lease_row_for(self.lease_cols()))
        self.assertEqual(store.verify(self.conn), [])  # dispatched work is an open task

    def test_claim_without_a_lease_row(self):
        self.reach("dispatched")
        self.assertIn("claim_without_lease", store.verify(self.conn))

    def test_lease_row_without_a_live_claim(self):
        self.reach("dispatched")
        self.insert("leases", self.lease_row_for(self.lease_cols()))
        self.put("released", "ready", lease_id=self.LEASE, generation=1)
        self.assertIn("lease_without_live_claim", store.verify(self.conn))

    def test_lease_row_that_differs_from_the_claim(self):
        self.reach("dispatched")
        self.insert("leases", self.lease_row_for(self.lease_cols()))
        self.conn.close()
        raw = sqlite3.connect(self.db, isolation_level=None)  # a writer that bypassed the triggers
        self.addCleanup(raw.close)
        raw.execute("DROP TRIGGER leases_fixed_fields")
        raw.execute("UPDATE leases SET worker = 'someone-else'")
        self.assertEqual(store.verify(raw), ["lease_differs_from_claim"])

    def test_row_first_renewal_without_its_entry(self):
        self.reach("running")
        self.insert("leases", self.lease_row_for(self.lease_cols()))
        later = self.stamp(1500)
        with store.write_transaction(self.conn):
            self.conn.execute("UPDATE leases SET expires_at = ?, renewals = 1", (later,))
        self.assertIn("lease_expiry_differs_from_log", store.verify(self.conn))

    def test_entry_first_renewal_without_its_row_update(self):
        self.reach("running")
        self.insert("leases", self.lease_row_for(self.lease_cols()))
        self.put("renewed", "running", **self.renew_cols(1800))
        self.assertIn("lease_expiry_differs_from_log", store.verify(self.conn))

    def test_slot_without_a_live_apply_lease(self):
        self.put("enqueued", "waiting", actor="human")
        self.put("ready", "ready")
        self.put("claimed", "dispatched", **{**self.lease_cols(), "step": "apply"})
        self.put("started", "running", **{**self.started_cols(), "step": "apply"})
        self.insert("leases", self.lease_row_for({**self.lease_cols(), "step": "apply"}))
        self.insert("apply_slots", {"target": "docs/a.md", "task_id": "alpha", "lease_id": self.LEASE,
                                    "generation": 1, "acquired_at": self.stamp()})
        self.assertNotIn("slot_without_live_apply_lease", store.verify(self.conn))
        self.put("submitted", "review", lease_id=self.LEASE, generation=1)
        self.assertIn("slot_without_live_apply_lease", store.verify(self.conn))

    def test_task_state_that_differs_from_the_queue(self):
        self.reach("running")  # the task row still says open
        self.assertIn("task_state_differs_from_queue", store.verify(self.conn))

    def test_chain_and_sequence_breaks_are_found_when_the_guards_are_gone(self):
        self.put("enqueued", "waiting", actor="human")
        self.put("ready", "ready")
        self.conn.close()
        raw = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(raw.close)
        for name in ("queue_entries_no_update", "queue_entries_chain", "queue_entries_seq", "queue_entries_id"):
            raw.execute("DROP TRIGGER " + name)
        raw.execute("UPDATE queue_entries SET prev_hash = ? WHERE seq = 2", ("9" * 64,))
        self.assertEqual(store.verify(raw), ["queue_chain_broken"])

    def test_deep_check_runs_integrity_check(self):
        self.assertEqual(store.verify(self.conn, deep=True), [])


class WriteOrderTests(RowFixture):
    """Review round 3: the statement-level rules that remain for lease rows, slots and renewals."""

    def lease_row_for(self, cols):
        return LeaseConsistencyTests.lease_row_for(self, cols)

    def update_lease(self, expires, renewals):
        return LeaseConsistencyTests.update_lease(self, expires, renewals)

    def test_a_new_lease_row_starts_with_zero_renewals(self):
        self.reach("dispatched")
        row = self.lease_row_for(self.lease_cols())
        self.assertEqual(code_of(self.insert, "leases", {**row, "renewals": 1000}), errors.LEASE_REFUSED)
        self.insert("leases", row)

    def test_a_delayed_lease_row_for_an_ended_claim_is_refused(self):
        self.reach("dispatched")
        row = self.lease_row_for(self.lease_cols())
        self.put("released", "ready", lease_id=self.LEASE, generation=1)
        self.assertEqual(code_of(self.insert, "leases", row), errors.LEASE_REFUSED)

    def test_a_slot_cannot_be_taken_on_an_ended_claim(self):
        self.put("enqueued", "waiting", actor="human")
        self.put("ready", "ready")
        self.put("claimed", "dispatched", **{**self.lease_cols(), "step": "apply"})
        self.insert("leases", self.lease_row_for({**self.lease_cols(), "step": "apply"}))
        self.put("released", "ready", lease_id=self.LEASE, generation=1)  # the row was not deleted yet
        slot = {"target": "docs/a.md", "task_id": "alpha", "lease_id": self.LEASE, "generation": 1,
                "acquired_at": self.stamp()}
        self.assertEqual(code_of(self.insert, "apply_slots", slot), errors.LEASE_REFUSED)

    def test_done_and_reopen_need_the_lease_row_gone(self):
        self.reach("dispatched")
        self.insert("leases", self.lease_row_for(self.lease_cols()))
        self.put("started", "running", **self.started_cols())
        self.put("submitted", "review", lease_id=self.LEASE, generation=1)
        self.assertEqual(self.put_code("done", "done"), errors.ILLEGAL_TRANSITION)  # stale row still there
        with store.write_transaction(self.conn):
            self.conn.execute("DELETE FROM leases")
        self.put("done", "done")

    def test_only_one_row_first_renewal_may_be_outstanding_and_entries_must_match_it(self):
        self.reach("running")
        self.insert("leases", self.lease_row_for(self.lease_cols()))
        base = self.clock

        def at(seconds):
            return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

        self.assertIsNone(self.update_lease(at(1800), 1))  # row first
        self.assertEqual(self.update_lease(at(2700), 2), errors.LEASE_REFUSED)  # a second one before the entry
        wrong = {**self.renew_cols(0), "expires_at": at(1900)}
        self.assertEqual(self.put_code("renewed", "running", **wrong), errors.LEASE_REFUSED)  # not the row's expiry
        self.put("renewed", "running", **{**self.renew_cols(0), "expires_at": at(1800)})
        self.assertEqual(store.verify(self.conn).count("lease_expiry_differs_from_log"), 0)

    def test_entry_first_renewal_pins_the_exact_row_update(self):
        self.reach("running")
        self.insert("leases", self.lease_row_for(self.lease_cols()))
        base = self.clock

        def at(seconds):
            return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

        self.put("renewed", "running", **{**self.renew_cols(0), "expires_at": at(1800)})
        self.assertEqual(self.update_lease(at(1900), 1), errors.LEASE_REFUSED)  # not the logged expiry
        self.assertEqual(self.update_lease(at(1800), 2), errors.LEASE_REFUSED)  # not the logged count
        self.assertIsNone(self.update_lease(at(1800), 1))


class WriteTransactionEdgeTests(Base):
    def setUp(self):
        super().setUp()
        self.init_store()

    def test_unreadable_connection_state_after_a_commit_error_is_ambiguous_and_leaves_no_cause(self):
        real = store.connect(self.proj, busy_timeout_ms=200)

        class Closing(FaultyConnection):
            @property
            def in_transaction(self):
                if self.closed:
                    raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
                return self.real.in_transaction

            def execute(self, sql, *args):
                if sql == "COMMIT":
                    self.closed = True
                    self.real.close()
                    raise io_error()
                return self.real.execute(sql, *args)

        proxy = Closing(real)
        with self.assertRaises(store.AmbiguousCommit) as caught, store.write_transaction(proxy):
            pass
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_an_interrupt_during_rollback_abandons_the_connection(self):
        real = store.connect(self.proj, busy_timeout_ms=200)

        class Interrupting(FaultyConnection):
            def execute(self, sql, *args):
                if sql == "ROLLBACK":
                    raise KeyboardInterrupt
                return self.real.execute(sql, *args)

        proxy = Interrupting(real)
        with self.assertRaises(ShitError) as caught, store.write_transaction(proxy):
            raise busy_error()
        self.assertEqual(caught.exception.code, errors.FAILED_CLOSED)
        self.assertTrue(proxy.closed)


class InitTests(Base):
    def test_an_existing_empty_file_is_refused_unless_adoption_is_explicit(self):
        os.close(os.open(self.db, os.O_CREAT | os.O_WRONLY, stat.S_IRUSR | stat.S_IWUSR))
        self.assertEqual(code_of(store.init_store, self.proj), errors.FAILED_CLOSED)
        self.assertEqual(self.db.stat().st_size, 0)  # nothing was written into it
        store.init_store(self.proj, adopt_empty=True)
        store.connect(self.proj).close()

    def test_adoption_never_touches_a_file_that_already_has_content(self):
        self.db.write_bytes(b"not a database" * 100)
        os.chmod(self.db, stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(code_of(store.init_store, self.proj, adopt_empty=True), errors.FAILED_CLOSED)
        self.assertEqual(self.db.read_bytes(), b"not a database" * 100)

    def test_a_failure_in_the_middle_of_the_schema_leaves_an_empty_database(self):
        broken = [*store.schema_statements()[:12], "CREATE TABLE (broken"]
        with mock.patch.object(store, "schema_statements", lambda: broken):
            self.assertIsNotNone(code_of(store.init_store, self.proj))
        raw = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 0)
        raw.close()
        store.init_store(self.proj, adopt_empty=True)  # recoverable

    def test_a_replaced_store_directory_is_refused(self):
        store.init_store(self.proj)
        shit_dir = self.db.parent
        os.rename(shit_dir, self.base / "moved-away")
        os.mkdir(shit_dir, stat.S_IRWXU)
        self.assertEqual(code_of(store.connect, self.proj), errors.CONFINEMENT)
        self.assertEqual(code_of(store.init_store, self.proj), errors.CONFINEMENT)


class ReplayAgreementTests(RowFixture):
    """The database and `queue_records.replay` must agree on accepted and on rejected candidates (real ids, graph rows)."""

    KINDS = ReplayDifferentialTests.KINDS

    def dicts(self):
        cursor = self.conn.execute("SELECT * FROM queue_entries ORDER BY seq")
        names = [d[0] for d in cursor.description]
        out = []
        for row in cursor.fetchall():
            entry = {"schema": "queue-entry", "schema_version": 1}
            for name, value in zip(names, row, strict=True):
                if value is not None and name not in ("seq", "prev_hash", "hash"):
                    entry[name] = json.loads(value) if name == "shards" else value
            out.append(queue_records.validate_entry(entry))
        return out

    def shards_text(self, rng):
        ids = sorted(rng.sample(["alpha", "beta", "gamma", "delta"], rng.choice((1, 2))))
        if rng.random() < 0.2:
            ids = ids[::-1]
        return json.dumps([{"task_id": i, "sha256": "1" * 64} for i in ids])

    def test_candidates_are_judged_the_same_by_the_database_and_by_replay(self):
        rng = random.Random(61)
        helper = ReplayDifferentialTests
        last, disagreements, accepted, rejected = {}, [], 0, 0
        for _ in range(300):
            kind, state, actor, cols = helper.candidate(self, rng, "alpha", last)
            if rng.random() < 0.08:
                kind, state, actor, cols = "graph_changed", None, "wrapper", {"shards": self.shards_text(rng)}
            position = self.conn.execute("SELECT count(*) FROM queue_entries").fetchone()[0] + 1
            candidate = {"schema": "queue-entry", "schema_version": 1, "id": f"queue-{position:04d}",
                         "task_id": "alpha", "timestamp": self.stamp(1), "actor": actor, "kind": kind, **cols}
            if state is not None:
                candidate["state_after"] = state
            if "shards" in candidate:
                candidate["shards"] = json.loads(candidate["shards"])
            try:
                queue_records.replay([*self.dicts(), queue_records.validate_entry(dict(candidate))])
                replay_ok = True
            except ShitError:
                replay_ok = False
            try:
                self.put(kind, state, actor=actor, **cols)
                db_ok = True
            except ShitError:
                db_ok = False
            if db_ok != replay_ok:
                disagreements.append(("database" if db_ok else "replay", "accepted the other rejected", kind, state))
            if db_ok:
                accepted += 1
                if kind == "claimed":
                    last = {**last, "lease": cols["lease_id"], "generation": cols["generation"],
                            "worker": cols["worker"], "step": cols["step"]}
                if kind == "started":
                    last["attempts"] = cols["attempt"]
            else:
                rejected += 1
                self.clock += timedelta(seconds=rng.choice((0, 30, 300)))
        self.assertGreater(accepted, 10)
        self.assertGreater(rejected, 100)
        self.assertEqual(disagreements, [])


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
        self.assertEqual(code_of(run), errors.LEASE_REFUSED)  # past a day from the old expiry; the CHECK is a second line

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
        for args in (("bad name", "BEFORE INSERT", "tasks", None, "E7"), ("ok", "INSTEAD OF INSERT", "tasks", None, "E7"),
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

    def test_the_pinned_fingerprint_equals_what_the_ddl_generates(self):
        # A DDL change must update the literal on purpose, together with the schema review.
        self.assertEqual(store.PINNED_FINGERPRINT, store.expected_fingerprint())

    def test_open_checks_against_the_pinned_literal_not_the_builders(self):
        self.init_store()
        with mock.patch.object(store, "PINNED_FINGERPRINT", "0" * 64):
            self.assertEqual(code_of(store.connect, self.proj), errors.FAILED_CLOSED)
        with mock.patch.object(store, "expected_fingerprint", lambda: "0" * 64):
            store.connect(self.proj).close()  # the generator is not consulted at open

    def test_untouched_store_verifies_and_fingerprint_is_stable(self):
        self.init_store()
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        self.assertEqual(store.fingerprint(conn), store.PINNED_FINGERPRINT)
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

    def test_added_view_is_refused(self):
        self.tamper("CREATE VIEW extra_view AS SELECT 1")
        self.assertEqual(code_of(store.connect, self.proj), errors.FAILED_CLOSED)

    def test_added_index_is_refused(self):
        self.tamper("CREATE INDEX extra_index ON tasks (state)")
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

    def test_error_after_a_commit_that_landed_is_ambiguous_not_a_plain_failure(self):
        real = store.connect(self.proj, busy_timeout_ms=200)
        proxy = FaultyConnection(real, commit="commit-then-error")
        with self.assertRaises(store.AmbiguousCommit) as caught, store.write_transaction(proxy):
            self.add_task(proxy)
        self.assertEqual(caught.exception.code, errors.FAILED_CLOSED)
        self.assertEqual(str(caught.exception), errors.MESSAGES[errors.FAILED_CLOSED])
        self.assertTrue(proxy.closed)
        with self.assertRaises(sqlite3.ProgrammingError):
            real.execute("SELECT 1")
        # The write is durable: the caller must re-read before it retries anything.
        self.assertEqual(self.raw().execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_error_after_a_commit_that_did_not_land_is_also_ambiguous(self):
        real = store.connect(self.proj, busy_timeout_ms=200)
        proxy = FaultyConnection(real, commit="rollback-then-error")
        with self.assertRaises(store.AmbiguousCommit), store.write_transaction(proxy):
            self.add_task(proxy)
        self.assertTrue(proxy.closed)
        self.assertEqual(self.raw().execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_ambiguous_commit_is_a_shit_error_with_a_fixed_message(self):
        self.assertTrue(issubclass(store.AmbiguousCommit, ShitError))
        self.assertEqual(store.AmbiguousCommit().code, errors.FAILED_CLOSED)

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
            with self.assertRaises(ShitError) as caught:
                store.write_transaction(second).__enter__()
            self.assertEqual(str(caught.exception), errors.MESSAGES[errors.BUSY])
            self.assertNotIn(str(self.base), str(caught.exception))


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
        if sql == "COMMIT" and self.commit == "commit-then-error":
            self.real.execute("COMMIT")  # the write is durable, then the API reports an error
            raise io_error()
        if sql == "COMMIT" and self.commit == "rollback-then-error":
            self.real.execute("ROLLBACK")
            raise io_error()
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
