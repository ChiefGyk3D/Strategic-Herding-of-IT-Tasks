"""Task, event and records-index write helpers of the SQLite store (issue 61, epic 57).

Real database files in a temporary directory; nothing about sqlite is mocked. A crash is a child process that kills
itself with SIGKILL between two statements of one helper. Every helper test ends with `store.verify` and
`store.verify_tasks`. Tampering uses a plain `sqlite3` connection opened by the test, which is allowed outside `shit/`.
"""

import hashlib
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from _bootstrap import ROOT, make_git_dir

from shit import errors, records, store
from shit.errors import ShitError
from shit.project import Project

T0 = "2026-11-27T10:00:00Z"
T1 = "2026-11-27T10:01:00Z"
T2 = "2026-11-27T10:02:00Z"
T3 = "2026-11-27T10:03:00Z"
T4 = "2026-11-27T10:04:00Z"
SHA = hashlib.sha256(b"body").hexdigest()


def code_of(func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except ShitError as exc:
        return exc.code
    return None


def task_dict(task_id="alpha", now=T0, owner="human"):
    return {"schema": "task", "schema_version": 1, "id": task_id, "title": "Title", "summary": "Summary",
            "state": "open", "owner": owner, "created_at": now, "updated_at": now, "history": []}


def digest_of(task):
    data = records.dump_task(task)
    return hashlib.sha256(data).hexdigest(), len(data)


def record_dict(rid="ev-1", task_id="alpha", actor="wrapper", now=T1):
    return {"id": rid, "task_id": task_id, "actor": actor, "created_at": now}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        self.repo.mkdir()
        make_git_dir(self.repo / ".git")
        self.proj = Project.init(self.repo)
        self.addCleanup(self.proj.close)
        store.init_store(self.proj)
        self.conn = self.connect()
        self.db = self.repo / ".shit" / store.DB_NAME

    def connect(self, **kwargs):
        conn = store.connect(self.proj, **kwargs)
        self.addCleanup(conn.close)
        return conn

    def raw(self):
        conn = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(conn.close)
        return conn

    def new(self, task_id="alpha", now=T0, conn=None):
        task = task_dict(task_id, now)
        sha, size = digest_of(task)
        store.create_task(conn or self.conn, task, "human", sha, size)
        return task

    def add(self, kind="evidence", rid="ev-1", task_id="alpha", actor="wrapper", now=T1, **kwargs):
        return store.add_record(self.conn, kind, record_dict(rid, task_id, actor, now), SHA, 4, **kwargs)

    def consistent(self, conn=None):
        conn = conn or self.conn
        self.assertEqual(store.verify(conn), [])
        self.assertEqual(store.verify_tasks(conn), [])

    def counts(self):
        out = []
        for table in ("tasks", "task_history", "events", "records"):
            out.append(self.raw().execute("SELECT count(*) FROM " + table).fetchone()[0])
        return tuple(out)

    def run_to_review(self, task_id="alpha", start=T1):
        stamps = [start, "2026-11-27T10:10:00Z", "2026-11-27T10:11:00Z"]
        for to, actor, stamp in (("running", "wrapper", stamps[0]), ("review", "wrapper", stamps[1]),
                                 ("approved", "human", stamps[2])):
            store.transition_task(self.conn, task_id, to, actor, "step", stamp)


class CreateTaskTests(Base):
    def test_one_call_writes_task_index_and_event_and_verifies(self):
        task = self.new()
        self.assertEqual(self.counts(), (1, 0, 1, 1))
        row = store.get_task(self.conn, "alpha")
        self.assertEqual((row["state"], row["owner"], row["created_at"], row["updated_at"], row["history"]),
                         ("open", "human", T0, T0, []))
        sha, size = digest_of(task)
        self.assertEqual(store.get_record(self.conn, "task", "alpha")["sha256"], sha)
        self.assertEqual(store.get_record(self.conn, "task", "alpha")["size"], size)
        self.consistent()

    def test_the_event_equals_the_file_protocol_line_and_hash(self):
        self.new()
        event = store.list_events(self.conn)[0]
        self.assertEqual(event, {
            "schema": "event-log", "schema_version": 1, "id": "event-0001", "task_id": "alpha", "timestamp": T0,
            "actor": "human", "event": "task_created", "message": "Task record created.", "record_id": "alpha"})
        line = records.dump_event(event)
        want = hashlib.sha256(("0" * 64).encode() + b"\n" + line).hexdigest()
        self.assertEqual(self.raw().execute("SELECT hash FROM events").fetchone()[0], want)

    def test_a_second_event_links_to_the_first(self):
        self.new("alpha")
        self.new("beta", T1)
        rows = self.raw().execute("SELECT seq, prev_hash, hash FROM events ORDER BY seq").fetchall()
        self.assertEqual(rows[1][1], rows[0][2])
        self.assertEqual([r[0] for r in rows], [1, 2])
        self.consistent()

    def test_duplicate_id_is_a_usage_error_and_changes_nothing(self):
        self.new()
        before = self.counts()
        task = task_dict("alpha", T1)
        sha, size = digest_of(task)
        self.assertEqual(code_of(store.create_task, self.conn, task, "human", sha, size), errors.USAGE)
        self.assertEqual(self.counts(), before)
        self.consistent()

    def test_bad_arguments_are_rejected_and_change_nothing(self):
        good = task_dict()
        sha, size = digest_of(good)
        wrong_state = {**good, "state": "running"}
        history = {**good, "history": [{"from": "open", "to": "running", "actor": "wrapper", "at": T0, "reason": "x"}],
                   "state": "running"}
        later = {**good, "updated_at": T1}
        for bad, args in ((wrong_state, (sha, size)), (history, (sha, size)), (later, (sha, size)),
                          (good, ("zz", size)), (good, (sha.upper(), size)), (good, (sha, -1)),
                          (good, (sha, True)), (good, (sha, records.MAX_RECORD_BYTES + 1)),
                          ({**good, "id": "Bad Id"}, (sha, size)), ({"id": "alpha"}, (sha, size))):
            with self.subTest(bad=str(bad)[:60], args=args):
                self.assertEqual(code_of(store.create_task, self.conn, bad, "human", *args), errors.REJECTED)
        self.assertEqual(code_of(store.create_task, self.conn, good, "robot", sha, size), errors.REJECTED)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_event_time_never_goes_back(self):
        self.new("alpha", T2)
        self.assertEqual(code_of(lambda: self.new("beta", T1)), errors.REJECTED)
        self.assertEqual(self.counts(), (1, 0, 1, 1))
        self.consistent()


class TransitionTests(Base):
    def test_a_full_lifecycle_keeps_task_history_and_events_together(self):
        self.new()
        self.run_to_review()
        store.transition_task(self.conn, "alpha", "closed", "human", "done", "2026-11-27T10:12:00Z")
        row = store.get_task(self.conn, "alpha")
        self.assertEqual(row["state"], "closed")
        self.assertEqual([(h["from"], h["to"], h["actor"]) for h in row["history"]],
                         [("open", "running", "wrapper"), ("running", "review", "wrapper"),
                          ("review", "approved", "human"), ("approved", "closed", "human")])
        self.assertEqual(row["updated_at"], "2026-11-27T10:12:00Z")
        kinds = [e["event"] for e in store.list_events(self.conn)]
        self.assertEqual(kinds, ["task_created"] + ["state_transition"] * 4)
        self.consistent()

    def test_the_helper_returns_the_new_state(self):
        self.new()
        row = store.transition_task(self.conn, "alpha", "running", "wrapper", "go", T1)
        self.assertEqual((row["state"], row["updated_at"]), ("running", T1))

    def test_each_refusal_keeps_its_exit_code_and_changes_nothing(self):
        self.new()
        self.new("beta", T3)
        before = self.counts()
        cases = (
            (("alpha", "review", "wrapper", "skip", T4), errors.ILLEGAL_TRANSITION, {}),
            (("alpha", "running", "model", "x", T4), errors.ILLEGAL_TRANSITION, {}),
            (("alpha", "nonsense", "human", "x", T4), errors.ILLEGAL_TRANSITION, {}),
            (("ghost", "running", "wrapper", "x", T4), errors.PRECONDITION, {}),
            (("Bad Id", "running", "wrapper", "x", T4), errors.REJECTED, {}),
            (("alpha", "running", "wrapper", "", T4), errors.REJECTED, {}),
            (("alpha", "running", "wrapper", "x" * 501, T4), errors.REJECTED, {}),
            (("alpha", "running", "wrapper", "x", "yesterday"), errors.REJECTED, {}),
            (("alpha", "running", "wrapper", "x", "2026-11-27T09:00:00Z"), errors.REJECTED, {}),  # before the task
            (("alpha", "running", "wrapper", "x", T2), errors.REJECTED, {}),  # before the event log tip (beta, T3)
            (("alpha", "blocked", "human", "x", T4), errors.REJECTED, {}),  # blocked needs both fields
            (("alpha", "blocked", "human", "x", T4), errors.REJECTED, {"blocked_reason": "why"}),
            (("alpha", "running", "wrapper", "x", T4), errors.REJECTED, {"blocked_reason": "why", "blocking_item": "it"}),
        )
        for args, code, kwargs in cases:
            with self.subTest(args=args, kwargs=kwargs):
                self.assertEqual(code_of(store.transition_task, self.conn, *args, **kwargs), code)
                self.assertEqual(self.counts(), before)
                self.assertFalse(self.conn.in_transaction)
        self.consistent()

    def test_a_model_or_wrapper_cannot_approve(self):
        self.new()
        store.transition_task(self.conn, "alpha", "running", "wrapper", "go", T1)
        store.transition_task(self.conn, "alpha", "review", "wrapper", "done", T2)
        for actor in ("wrapper", "model"):
            self.assertEqual(code_of(store.transition_task, self.conn, "alpha", "approved", actor, "ok", T3),
                             errors.HUMAN_APPROVAL)
        self.assertEqual(store.get_task(self.conn, "alpha")["state"], "review")
        self.consistent()

    def test_blocked_fields_are_set_and_cleared_with_the_state(self):
        self.new()
        store.transition_task(self.conn, "alpha", "blocked", "human", "stuck", T1, blocked_reason="waiting",
                              blocking_item="issue 7")
        row = store.get_task(self.conn, "alpha")
        self.assertEqual((row["blocked_reason"], row["blocking_item"]), ("waiting", "issue 7"))
        store.transition_task(self.conn, "alpha", "open", "human", "back", T2)
        row = store.get_task(self.conn, "alpha")
        self.assertNotIn("blocked_reason", row)
        self.assertNotIn("blocking_item", row)
        self.consistent()

    def test_precheck_runs_inside_the_transaction_and_can_refuse(self):
        self.new()
        seen = []

        def precheck(current):
            seen.append((current["state"], self.conn.in_transaction))

        store.transition_task(self.conn, "alpha", "running", "wrapper", "go", T1, precheck=precheck)
        self.assertEqual(seen, [("open", True)])

        def refuse(current):
            raise ShitError(errors.PRECONDITION)

        before = self.counts()
        self.assertEqual(code_of(store.transition_task, self.conn, "alpha", "review", "wrapper", "x", T2,
                                 precheck=refuse), errors.PRECONDITION)
        self.assertEqual(self.counts(), before)
        self.consistent()


class AddRecordTests(Base):
    KINDS = (("evidence", "evidence_recorded", "Evidence"), ("report", "report_recorded", "Report"),
             ("review", "review_recorded", "Review"), ("decision", "decision_recorded", "Decision"),
             ("brief", "brief_recorded", "Brief"))

    def test_each_kind_writes_the_index_row_and_its_event_together(self):
        self.new()
        for number, (kind, event_kind, label) in enumerate(self.KINDS):
            with self.subTest(kind=kind):
                rid = "rec-" + str(number)
                store.add_record(self.conn, kind, record_dict(rid), SHA, 4)
                row = store.get_record(self.conn, kind, rid)
                self.assertEqual((row["task_id"], row["sha256"], row["size"], row["created_at"]), ("alpha", SHA, 4, T1))
                event = store.record_event(self.conn, kind, rid)
                self.assertEqual((event["event"], event["record_id"], event["actor"], event["timestamp"]),
                                 (event_kind, rid, "wrapper", T1))
                self.assertEqual(event["message"], label + " record added; sha256 " + SHA + ".")
                self.consistent()
        self.assertEqual(store.record_ids(self.conn, "report", "alpha"), ["rec-1"])

    def test_a_handoff_is_indexed_without_an_event(self):
        self.new()
        store.add_record(self.conn, "handoff", record_dict("hand-1"), SHA, 4)
        self.assertIsNotNone(store.get_record(self.conn, "handoff", "hand-1"))
        self.assertEqual(self.counts(), (1, 0, 1, 2))
        self.consistent()

    def test_record_ids_follow_the_order_they_were_recorded(self):
        self.new()
        for number, rid in enumerate(("zz-last-name", "aa-first-name", "mm-middle")):
            store.add_record(self.conn, "evidence", record_dict(rid, now="2026-11-27T10:0" + str(number + 1) + ":00Z"),
                             SHA, 4)
        self.assertEqual(store.record_ids(self.conn, "evidence", "alpha"), ["zz-last-name", "aa-first-name", "mm-middle"])

    def test_refusals_keep_their_codes_and_change_nothing(self):
        self.new()
        self.add()
        store.transition_task(self.conn, "alpha", "running", "wrapper", "go", T2)
        before = self.counts()
        for kwargs, code in (
                ({"rid": "ev-1"}, errors.USAGE),                      # the same record again
                ({"rid": "ev-2", "task_id": "ghost"}, errors.PRECONDITION),
                ({"rid": "ev-3", "now": T0}, errors.REJECTED),        # before the event log tip
                ({"rid": "Bad Id"}, errors.REJECTED),
                ({"rid": "ev-4", "actor": "robot"}, errors.REJECTED),
                ({"rid": "ev-5", "now": "later"}, errors.REJECTED)):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(code_of(self.add, **kwargs), code)
                self.assertEqual(self.counts(), before)
        for bad in (("nonsense", SHA, 4), ("evidence", "xyz", 4), ("evidence", SHA, -1), ("evidence", SHA, False),
                    ("task", SHA, 4)):
            with self.subTest(bad=bad):
                self.assertEqual(code_of(store.add_record, self.conn, bad[0], record_dict("ev-9", now=T3), *bad[1:]),
                                 errors.REJECTED)
        self.assertEqual(self.counts(), before)
        self.consistent()

    def test_a_closed_task_takes_no_more_records(self):
        self.new()
        self.run_to_review()
        store.transition_task(self.conn, "alpha", "closed", "human", "done", "2026-11-27T10:12:00Z")
        before = self.counts()
        self.assertEqual(code_of(self.add, rid="late", now="2026-11-27T10:13:00Z"), errors.ILLEGAL_TRANSITION)
        self.assertEqual(self.counts(), before)

    def test_check_runs_inside_the_transaction_and_can_refuse(self):
        self.new()
        seen = []
        self.add(check=lambda task: seen.append((task["state"], self.conn.in_transaction)))
        self.assertEqual(seen, [("open", True)])
        before = self.counts()

        def refuse(task):
            raise ShitError(errors.ILLEGAL_TRANSITION)

        self.assertEqual(code_of(self.add, rid="ev-2", check=refuse), errors.ILLEGAL_TRANSITION)
        self.assertEqual(self.counts(), before)
        self.consistent()


class OneTransactionTests(Base):
    """Each helper is one write transaction: a fault between any two statements leaves nothing behind."""

    HELPERS = ("create_task", "transition_task", "add_record")

    def operation(self, name):
        """A fresh environment with task alpha, and the helper call to test (not yet made)."""
        self.setUp()
        self.new("alpha")
        return {
            "create_task": lambda: self.new("beta", T1),
            "transition_task": lambda: store.transition_task(self.conn, "alpha", "running", "wrapper", "go", T2),
            "add_record": lambda: self.add(rid="ev-9", now=T2),
        }[name]

    def stages(self, name):
        found = []
        operation = self.operation(name)
        original = store._fault
        store._fault = found.append
        try:
            operation()
        finally:
            store._fault = original
        return found

    def test_every_helper_announces_its_stages(self):
        for name in self.HELPERS:
            with self.subTest(name=name):
                stages = self.stages(name)
                self.assertGreaterEqual(len(stages), 2, name)
                self.assertEqual(stages[0], "checked")

    def test_a_fault_at_any_stage_rolls_everything_back(self):
        for name in self.HELPERS:
            for stage in self.stages(name):
                with self.subTest(helper=name, stage=stage):
                    operation = self.operation(name)
                    before = self.counts()

                    def boom(seen, stage=stage):
                        if seen == stage:
                            raise RuntimeError("injected")

                    original = store._fault
                    store._fault = boom
                    try:
                        with self.assertRaises(RuntimeError):
                            operation()
                    finally:
                        store._fault = original
                    self.assertFalse(self.conn.in_transaction)
                    self.assertEqual(self.counts(), before)
                    self.consistent()
                    operation()  # the connection is still usable and the operation works afterwards
                    self.consistent()

    def test_the_write_lock_is_held_from_the_first_read_to_the_commit(self):
        for name in self.HELPERS:
            operation = self.operation(name)
            codes = []

            def probe(stage, codes=codes):
                other = store.connect(self.proj, busy_timeout_ms=0)
                try:
                    codes.append(code_of(self.begin, other))
                finally:
                    other.close()

            original = store._fault
            store._fault = probe
            try:
                operation()
            finally:
                store._fault = original
            with self.subTest(helper=name):
                self.assertGreaterEqual(len(codes), 2)
                self.assertEqual(set(codes), {errors.BUSY})
            self.consistent()

    @staticmethod
    def begin(conn):
        with store.write_transaction(conn):
            pass

    def test_the_task_check_is_read_after_the_lock_is_taken(self):
        # A second connection that took the same id first is seen by the helper: it is refused, not overwritten.
        other = self.connect()
        self.new("alpha", conn=self.conn)
        task = task_dict("alpha", T1)
        sha, size = digest_of(task)
        self.assertEqual(code_of(store.create_task, other, task, "human", sha, size), errors.USAGE)
        self.consistent(other)


class ConcurrencyTests(Base):
    def test_parallel_writers_keep_one_contiguous_chain(self):
        failures = []

        def work(worker):
            try:
                conn = store.connect(self.proj)
                try:
                    for number in range(5):
                        name = f"t-{worker}-{number}"
                        task = task_dict(name, "2026-11-27T10:00:00Z")
                        sha, size = digest_of(task)
                        store.create_task(conn, task, "human", sha, size)
                finally:
                    conn.close()
            except BaseException as exc:  # noqa: BLE001 - reported to the main thread below
                failures.append(repr(exc))

        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(self.counts(), (20, 0, 20, 20))
        self.consistent()

    def test_two_writers_on_one_task_id_have_exactly_one_winner(self):
        results = []

        def work():
            conn = store.connect(self.proj)
            try:
                task = task_dict("same", T0)
                sha, size = digest_of(task)
                results.append(code_of(store.create_task, conn, task, "human", sha, size))
            finally:
                conn.close()

        threads = [threading.Thread(target=work) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results, key=lambda c: (c is not None, c)), [None, errors.USAGE, errors.USAGE, errors.USAGE])
        self.assertEqual(self.counts(), (1, 0, 1, 1))
        self.consistent()


CHILD = """
import os, signal, sys
sys.path.insert(0, sys.argv[1])
from shit import store
from shit.project import Project
proj = Project.open(sys.argv[2])
conn = store.connect(proj)
stage, op = sys.argv[3], sys.argv[4]
def fault(seen):
    if seen == stage:
        os.kill(os.getpid(), signal.SIGKILL)
store._fault = fault
sha = "SHA_PLACEHOLDER"
if op == "create":
    task = {"schema": "task", "schema_version": 1, "id": "beta", "title": "T", "summary": "S", "state": "open",
            "owner": "human", "created_at": "2026-11-27T10:05:00Z", "updated_at": "2026-11-27T10:05:00Z", "history": []}
    store.create_task(conn, task, "human", sha, 4)
elif op == "transition":
    store.transition_task(conn, "alpha", "running", "wrapper", "go", "2026-11-27T10:05:00Z")
else:
    store.add_record(conn, "evidence", {"id": "ev-2", "task_id": "alpha", "actor": "wrapper",
                                        "created_at": "2026-11-27T10:05:00Z"}, sha, 4)
sys.exit(3)  # never reached when the child was killed
""".replace("SHA_PLACEHOLDER", SHA)


class CrashTests(Base):
    def kill_at(self, stage, op):
        proc = subprocess.run([sys.executable, "-I", "-c", CHILD, str(ROOT), str(self.repo), stage, op],
                              capture_output=True, timeout=60, check=False)
        self.assertEqual(proc.returncode, -signal.SIGKILL, proc.stderr[-300:])

    def test_a_kill_between_statements_leaves_nothing_half_written(self):
        self.new("alpha")
        self.add(rid="ev-1")
        before = self.counts()
        stage_names = {"create": ("checked", "task-row", "index-row"),
                       "transition": ("checked", "task-updated", "history-row"), "add": ("checked", "record-row")}
        for op, stages in stage_names.items():
            for stage in stages:
                with self.subTest(op=op, stage=stage):
                    self.kill_at(stage, op)
                    survivor = store.connect(self.proj)
                    try:
                        self.assertEqual(store.verify(survivor), [])
                        self.assertEqual(store.verify_tasks(survivor), [])
                        self.assertEqual(self.counts(), before)
                        self.assertFalse(survivor.in_transaction)
                    finally:
                        survivor.close()

    def test_the_stage_names_the_crash_test_uses_are_real(self):
        self.new("alpha")
        seen = set()
        original = store._fault
        store._fault = seen.add
        try:
            self.new("beta", T1)
            store.transition_task(self.conn, "alpha", "running", "wrapper", "go", T2)
            self.add(rid="ev-7", now=T3)
        finally:
            store._fault = original
        for stage in ("task-row", "index-row", "task-updated", "history-row", "record-row"):
            self.assertIn(stage, seen)

    def test_a_store_that_was_killed_mid_write_accepts_the_next_write(self):
        self.new("alpha")
        self.kill_at("history-row", "transition")
        survivor = store.connect(self.proj)
        self.addCleanup(survivor.close)
        store.transition_task(survivor, "alpha", "running", "wrapper", "go", T2)
        self.assertEqual(store.verify(survivor), [])
        self.assertEqual(store.verify_tasks(survivor), [])


class VerifyTasksTests(Base):
    """The cross-statement invariants the helpers keep; each break must be named (the check bites)."""

    def setUp(self):
        super().setUp()
        self.new()
        self.add(rid="ev-1")
        store.transition_task(self.conn, "alpha", "running", "wrapper", "go", T2)
        self.consistent()

    def tamper(self, *statements):
        raw = self.raw()
        for sql in statements:
            raw.execute(sql)
        return store.verify_tasks(self.conn)

    def test_a_clean_store_reports_nothing(self):
        self.assertEqual(store.verify_tasks(self.conn), [])

    def test_a_rewritten_event_changes_its_hash(self):
        found = self.tamper("DROP TRIGGER events_no_update",
                            "UPDATE events SET message = 'Task record edited.' WHERE seq = 1")
        self.assertIn("event_hash_wrong", found)

    def test_a_task_that_disagrees_with_its_history_is_named(self):
        self.assertIn("task_differs_from_history", self.tamper("UPDATE tasks SET state = 'review' WHERE id = 'alpha'"))

    def test_a_task_whose_time_disagrees_with_its_history_is_named(self):
        self.assertIn("task_differs_from_history", self.tamper("UPDATE tasks SET updated_at = '" + T4 + "'"))

    def test_history_that_differs_from_the_events_is_named(self):
        found = self.tamper("DROP TRIGGER task_history_no_update",
                            "UPDATE task_history SET at = '" + T3 + "' WHERE id = 1")
        self.assertIn("history_differs_from_events", found)

    def test_a_missing_history_row_is_named(self):
        found = self.tamper("DROP TRIGGER task_history_no_delete", "DELETE FROM task_history")
        self.assertIn("task_differs_from_history", found)
        self.assertIn("history_differs_from_events", found)

    def test_a_record_row_without_its_event_is_named(self):
        found = self.tamper("INSERT INTO records VALUES ('report', 'rep-9', 'alpha', '" + SHA + "', 4, '" + T2 + "')")
        self.assertIn("record_without_event", found)

    def test_an_event_without_its_record_row_is_named(self):
        self.assertIn("event_without_record", self.tamper("DELETE FROM records WHERE kind = 'evidence'"))

    def test_a_record_digest_that_differs_from_its_event_is_named(self):
        other = hashlib.sha256(b"other").hexdigest()
        found = self.tamper("UPDATE records SET sha256 = '" + other + "' WHERE kind = 'evidence'")
        self.assertIn("record_digest_differs_from_event", found)

    def test_a_task_without_its_created_event_is_named(self):
        found = self.tamper("INSERT INTO tasks (id, state, owner, created_at, updated_at) VALUES "
                            "('orphan', 'open', 'human', '" + T0 + "', '" + T0 + "')")
        self.assertIn("task_without_created_event", found)

    def test_a_task_without_its_body_index_is_named(self):
        found = self.tamper("DELETE FROM records WHERE kind = 'task'")
        self.assertIn("task_without_body_record", found)

    def test_a_sqlite_failure_while_reading_is_a_fixed_error(self):
        self.conn.close()
        self.assertEqual(code_of(store.verify_tasks, self.conn), errors.INTERNAL)


class ReadSnapshotTests(Base):
    def test_reads_inside_one_snapshot_do_not_see_a_later_commit(self):
        self.new("alpha")
        writer = self.connect()
        with store.read_snapshot(self.conn):
            first = store.get_task(self.conn, "alpha")
            store.transition_task(writer, "alpha", "running", "wrapper", "go", T1)
            second = store.get_task(self.conn, "alpha")
        self.assertEqual((first["state"], second["state"]), ("open", "open"))
        self.assertEqual(store.get_task(self.conn, "alpha")["state"], "running")
        self.assertFalse(self.conn.in_transaction)

    def test_the_snapshot_rolls_back_and_maps_errors(self):
        with self.assertRaises(RuntimeError), store.read_snapshot(self.conn):
            raise RuntimeError("boom")
        self.assertFalse(self.conn.in_transaction)
        with self.assertRaises(ShitError) as caught, store.read_snapshot(self.conn):
            self.conn.execute("SELECT * FROM missing_table")
        self.assertEqual(caught.exception.code, errors.INTERNAL)
        self.assertFalse(self.conn.in_transaction)

    def test_a_snapshot_cannot_nest_and_never_writes(self):
        with store.read_snapshot(self.conn):
            self.assertTrue(self.conn.in_transaction)
            self.assertRaises(ShitError, store.create_task, self.conn, task_dict(), "human", SHA, 4)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_a_snapshot_on_a_closed_connection_is_a_fixed_error(self):
        self.conn.close()
        with self.assertRaises(ShitError), store.read_snapshot(self.conn):
            pass


class ReadHelperTests(Base):
    def test_missing_things_read_as_none_or_empty(self):
        self.assertIsNone(store.get_task(self.conn, "ghost"))
        self.assertIsNone(store.get_record(self.conn, "evidence", "ghost"))
        self.assertIsNone(store.record_event(self.conn, "evidence", "ghost"))
        self.assertEqual(store.record_ids(self.conn, "evidence", "ghost"), [])
        self.assertEqual(store.list_events(self.conn), [])

    def test_unknown_kinds_are_rejected_not_queried(self):
        self.assertEqual(code_of(store.get_record, self.conn, "nonsense", "x"), errors.REJECTED)
        self.assertEqual(code_of(store.record_ids, self.conn, "nonsense", "x"), errors.REJECTED)
        self.assertEqual(code_of(store.record_event, self.conn, "task", "x"), errors.REJECTED)

    def test_hostile_ids_are_data_not_sql(self):
        self.new()
        for hostile in ("alpha' OR '1'='1", "alpha; DROP TABLE tasks", "%", "alpha\x00x"):
            with self.subTest(hostile=hostile):
                self.assertIsNone(store.get_task(self.conn, hostile))
        self.assertEqual(code_of(store.transition_task, self.conn, "alpha' OR '1'='1", "running", "wrapper", "x", T1),
                         errors.REJECTED)
        self.assertEqual(self.counts(), (1, 0, 1, 1))


if __name__ == "__main__":
    unittest.main()
