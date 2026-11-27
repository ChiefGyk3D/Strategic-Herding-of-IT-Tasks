"""SQLite store foundation tests (issue 59): connect, confinement, floor, schema, fingerprint, errors, lint.

Real database files in a temporary directory; nothing about sqlite is mocked. Tampering is done with a
plain `sqlite3` connection opened by the test itself, which is allowed outside `shit/`.
"""

import ast
import os
import re
import sqlite3
import stat
import tempfile
import time
import unittest
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


# -- safety lint --------------------------------------------------------------

SQL_START = re.compile(r"\s*(select|insert|update|delete|create|drop|alter|pragma|begin|commit|rollback|with"
                       r"|replace|vacuum|attach|detach|savepoint|release)\b", re.IGNORECASE)
EXECUTORS = {"execute", "executemany", "executescript"}
# The only places that may assemble SQL text: schema DDL from validated vocabulary, never caller input.
BUILDERS = {("shit/store.py", "schema_statements"), ("shit/store.py", "_trigger")}


def _leaves(node):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _leaves(node.left) + _leaves(node.right)
    return [node]


def _first_text(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def sql_findings(source, rel="x.py", builders=frozenset()):
    """Lines that build SQL text by f-string, %, .format or +, or pass non-literal SQL to an executor."""
    tree = ast.parse(source)
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and (rel, node.name) in builders:
            skip.update(id(child) for child in ast.walk(node))
    found = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.JoinedStr):
            head = _first_text(node.values[0]) if node.values else None
            if head is not None and SQL_START.match(head):
                found.append((node.lineno, "f-string"))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            head = _first_text(node.left)
            if head is not None and SQL_START.match(head):
                found.append((node.lineno, "percent"))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            leaves = _leaves(node)
            head = _first_text(leaves[0])
            if len(leaves) > 1 and head is not None and SQL_START.match(head):
                found.append((node.lineno, "plus"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "format":
                head = _first_text(node.func.value)
                if head is not None and SQL_START.match(head):
                    found.append((node.lineno, "format"))
            elif node.func.attr in EXECUTORS and node.args:
                arg = node.args[0]
                if not (isinstance(arg, ast.Name) or _first_text(arg) is not None):
                    found.append((node.lineno, "executor-argument"))
    return found


def imports_sqlite(source):
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in ("sqlite3", "_sqlite3"):
            names.add(node.value)  # import_module("sqlite3") and the like
    return bool(names & {"sqlite3", "_sqlite3"})


def package_sources():
    files = sorted((ROOT / "shit").rglob("*.py"))
    return [(f.relative_to(ROOT).as_posix(), f.read_text(encoding="utf-8")) for f in files]


class SqlLintTests(unittest.TestCase):
    def test_lint_collects_the_package(self):
        sources = package_sources()
        self.assertGreater(len(sources), 15)
        self.assertIn("shit/store.py", {rel for rel, _ in sources})

    def test_no_module_builds_sql_from_text(self):
        for rel, source in package_sources():
            with self.subTest(module=rel):
                self.assertEqual(sql_findings(source, rel, BUILDERS), [])

    def test_builders_exist_so_the_allowlist_cannot_rot(self):
        tree = ast.parse((ROOT / "shit" / "store.py").read_text(encoding="utf-8"))
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for rel, name in BUILDERS:
            self.assertEqual(rel, "shit/store.py")
            self.assertIn(name, defined)

    def test_lint_bites_on_each_forbidden_form(self):
        bad = (
            'conn.execute(f"SELECT * FROM t WHERE a = {x}")',
            'q = f"DELETE FROM t WHERE a = {x}"',
            'q = "SELECT * FROM t WHERE a = %s" % x',
            'q = "UPDATE t SET a = {}".format(x)',
            'q = "SELECT * FROM t WHERE a = " + x',
            'q = "SELECT a " + "FROM t " + x',
            "conn.execute(make_sql(x))",
            'conn.executemany("SELECT " + x, rows)',
        )
        for source in bad:
            with self.subTest(source=source):
                self.assertTrue(sql_findings(source), source)

    def test_lint_allows_parameters_and_plain_text(self):
        good = (
            'conn.execute("SELECT * FROM t WHERE a = ?", (x,))',
            'conn.executemany("INSERT INTO t VALUES (?, ?)", rows)',
            "conn.execute(STATEMENT)",
            'msg = "released %s" % name',
            'msg = f"hello {name}"',
        )
        for source in good:
            with self.subTest(source=source):
                self.assertEqual(sql_findings(source), [], source)

    def test_builder_allowlist_is_scoped_to_its_function(self):
        source = 'def schema_statements():\n    return "CREATE TABLE " + name\n\ndef other():\n    return "SELECT " + x\n'
        found = sql_findings(source, "shit/store.py", BUILDERS)
        self.assertEqual([line for line, _ in found], [5])

    def test_only_the_store_imports_sqlite(self):
        importers = [rel for rel, source in package_sources() if imports_sqlite(source)]
        self.assertEqual(importers, ["shit/store.py"])

    def test_import_lint_bites(self):
        for source in ("import sqlite3", "import sqlite3 as db", "from sqlite3 import connect",
                       "import _sqlite3", 'importlib.import_module("sqlite3")'):
            with self.subTest(source=source):
                self.assertTrue(imports_sqlite(source))
        self.assertFalse(imports_sqlite("import json\nfrom os import path"))


if __name__ == "__main__":
    unittest.main()
