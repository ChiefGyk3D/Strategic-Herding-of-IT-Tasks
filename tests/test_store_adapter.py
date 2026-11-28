"""The task and record operations on the SQLite store (issue 61): same signatures and results as the file protocol.

Two repositories run the same script: one through `task.py` and `commands.py` (files), one through
`store_tasks.py` (SQLite). Results, error codes, record bodies, event lines and the hash chain must agree. The rest
checks what is new: the body is a file written before the transaction and its digest is checked after it.
"""

import hashlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from _bootstrap import ROOT, make_git_dir

from shit import check, commands, errors, records, store, store_tasks
from shit import task as task_mod
from shit.errors import ShitError
from shit.log import EventLog
from shit.project import Project

T0 = "2026-11-27T10:00:00Z"
T1 = "2026-11-27T10:01:00Z"
T2 = "2026-11-27T10:02:00Z"
T3 = "2026-11-27T10:03:00Z"
T4 = "2026-11-27T10:04:00Z"
T5 = "2026-11-27T10:05:00Z"
T6 = "2026-11-27T10:06:00Z"
T7 = "2026-11-27T10:07:00Z"
T8 = "2026-11-27T10:08:00Z"
T9 = "2026-11-27T10:09:00Z"


def code_of(func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except ShitError as exc:
        return exc.code
    return None


class FileApi:
    """The file protocol, under the names the adapter uses."""

    new_task = staticmethod(commands.new_task)
    get_task = staticmethod(task_mod.get_task)
    transition = staticmethod(task_mod.transition)
    add_evidence = staticmethod(commands.add_evidence)
    add_report = staticmethod(commands.add_report)
    add_review = staticmethod(commands.add_review)
    add_decision = staticmethod(commands.add_decision)
    show = staticmethod(commands.show)
    close_task = staticmethod(commands.close_task)


class Env:
    """A temporary repository with a project, for either backend."""

    def __init__(self, case):
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name).resolve() / "repo"
        self.repo.mkdir()
        make_git_dir(self.repo / ".git")
        self.proj = Project.init(self.repo)
        case.addCleanup(self.proj.close)
        self.shit = self.repo / ".shit"
        (self.repo / "out.txt").write_text("evidence text\n")


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.repo, self.proj, self.shit = self.env.repo, self.env.proj, self.env.shit

    def conn(self):
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        return conn

    def consistent(self):
        conn = self.conn()
        self.assertEqual(store.verify(conn), [])
        self.assertEqual(store.verify_tasks(conn), [])
        self.assertEqual(store_tasks.verify_bodies(self.proj, conn), [])

    def make(self, tid="t1", now=T0):
        return store_tasks.new_task(self.proj, task_id=tid, title="Title", summary="Summary", owner="human",
                                    actor="human", now=now)

    def evidence(self, eid="ev-1", tid="t1", now=T1, proj=None, **kwargs):
        return store_tasks.add_evidence(proj or self.proj, task_id=tid, evidence_id=eid, path="out.txt", description="d",
                                        actor="wrapper", now=now, **kwargs)

    def count_rows(self, table):
        return self.conn().execute("SELECT count(*) FROM " + table).fetchone()[0]


# -- the same script through both backends -------------------------------------------------------------------


def script(api, proj):
    """Every step returns what the caller sees; the two backends must return equal values."""
    out = []
    out.append(api.new_task(proj, task_id="t1", title="Title", summary="Summary", owner="human", actor="human", now=T0))
    out.append(api.get_task(proj, "t1"))
    out.append(api.transition(proj, "t1", "running", "wrapper", "start", now=T1))
    out.append(api.add_evidence(proj, task_id="t1", evidence_id="ev-1", path="out.txt", description="the output",
                                actor="wrapper", now=T2))
    out.append(api.add_report(proj, task_id="t1", report_id="rep-1", outcome="success", summary="all passed",
                              evidence_ids=["ev-1"], actor="wrapper", now=T3))
    out.append(api.transition(proj, "t1", "review", "wrapper", "ready", now=T4))
    out.append(api.add_review(proj, task_id="t1", review_id="rev-1", report_id="rep-1", verdict="pass",
                              summary="fine", actor="human", now=T5))
    out.append(api.add_decision(proj, task_id="t1", decision_id="dec-1", status="accepted", question="ship?",
                                choice="yes", rationale="tests pass", actor="human", now=T6))
    out.append(api.transition(proj, "t1", "approved", "human", "approve", now=T7))
    out.append(api.close_task(proj, "t1", "human", "done", now=T8))
    out.append(api.get_task(proj, "t1"))
    for kind, rid in (("evidence", "ev-1"), ("report", "rep-1"), ("review", "rev-1"), ("decision", "dec-1")):
        out.append(api.show(proj, kind, rid))
    return out


def refusals(api, proj):
    """Operations that must fail, each with its exit code; the order matters (they build on the script)."""
    api.new_task(proj, task_id="t2", title="Other", summary="Second", owner="human", actor="human", now=T9)
    cases = (
        lambda: api.new_task(proj, task_id="t2", title="Other", summary="Second", owner="human", actor="human", now=T9),
        lambda: api.transition(proj, "ghost", "running", "wrapper", "x", now=T9),
        lambda: api.transition(proj, "t2", "review", "wrapper", "skip", now=T9),
        lambda: api.transition(proj, "t2", "approved", "wrapper", "no", now=T9),
        lambda: api.transition(proj, "t2", "running", "wrapper", "back in time", now=T0),
        lambda: api.transition(proj, "t2", "blocked", "human", "x", now=T9),
        lambda: api.get_task(proj, "ghost"),
        lambda: api.show(proj, "evidence", "ghost"),
        lambda: api.show(proj, "nonsense", "x"),
        lambda: api.close_task(proj, "t2", "human", "too early", now=T9),
        lambda: api.add_review(proj, task_id="t2", review_id="rev-9", report_id="rep-1", verdict="pass", summary="s",
                               actor="human", now=T9),
        lambda: api.add_report(proj, task_id="t2", report_id="rep-9", outcome="success", summary="s",
                               evidence_ids=["ghost"], actor="wrapper", now=T9),
        lambda: api.add_decision(proj, task_id="t2", decision_id="dec-9", status="accepted", question="q", choice="c",
                                 rationale="r", actor="wrapper", now=T9),
        lambda: api.add_evidence(proj, task_id="t1", evidence_id="ev-9", path="out.txt", description="late",
                                 actor="wrapper", now=T9),
        lambda: api.add_evidence(proj, task_id="t2", evidence_id="ev-1", path="out.txt", description="d",
                                 actor="wrapper", now=T9),
        lambda: api.add_evidence(proj, task_id="t2", evidence_id="ev-8", path="../escape.txt", description="d",
                                 actor="wrapper", now=T9),
        lambda: api.add_evidence(proj, task_id="t2", evidence_id="ev-7", path="missing.txt", description="d",
                                 actor="wrapper", now=T9),
        lambda: api.add_evidence(proj, task_id="ghost", evidence_id="ev-6", path="out.txt", description="d",
                                 actor="wrapper", now=T9),
    )
    codes = []
    for case in cases:
        try:
            case()
        except ShitError as exc:
            codes.append(exc.code)
        else:
            codes.append(None)
    return codes


class ParityTests(unittest.TestCase):
    def setUp(self):
        self.files, self.sql = Env(self), Env(self)
        self.a = script(FileApi, self.files.proj)
        self.b = script(store_tasks, self.sql.proj)

    def test_every_step_returns_what_the_file_protocol_returned(self):
        self.assertEqual(len(self.a), len(self.b))
        self.assertGreater(len(self.a), 10)
        for number, (left, right) in enumerate(zip(self.a, self.b, strict=True)):
            with self.subTest(step=number):
                self.assertEqual(left, right)

    def test_the_event_log_and_its_chain_are_the_same_bytes(self):
        file_events = EventLog(self.files.proj).verify()
        conn = store.connect(self.sql.proj)
        self.addCleanup(conn.close)
        store_events = store.list_events(conn)
        self.assertEqual(file_events, store_events)
        self.assertEqual(len(store_events), 9)
        chain = (self.files.shit / "events.chain").read_text().split()
        hashes = [row[0] for row in conn.execute("SELECT hash FROM events ORDER BY seq").fetchall()]
        self.assertEqual(chain, hashes)
        lines = (self.files.shit / "events.jsonl").read_bytes().split(b"\n")[:-1]
        self.assertEqual(lines, [records.dump_event(e) for e in store_events])

    def test_record_bodies_are_the_same_bytes(self):
        for directory, name in (("evidence", "ev-1"), ("reports", "rep-1"), ("reviews", "rev-1"),
                                ("decisions", "dec-1")):
            with self.subTest(directory=directory):
                left = (self.files.shit / directory / (name + ".json")).read_bytes()
                right = (self.sql.shit / directory / (name + ".json")).read_bytes()
                self.assertEqual(left, right)

    def test_the_sqlite_backend_leaves_no_file_protocol_state_behind(self):
        for name in ("events.jsonl", "events.chain", "pending.json"):
            self.assertFalse((self.sql.shit / name).exists(), name)

    def test_the_final_store_is_consistent_and_the_task_body_matches_its_index(self):
        conn = store.connect(self.sql.proj)
        self.addCleanup(conn.close)
        self.assertEqual(store.verify(conn), [])
        self.assertEqual(store.verify_tasks(conn), [])
        self.assertEqual(store_tasks.verify_bodies(self.sql.proj, conn), [])

    def test_refusals_carry_the_same_exit_codes(self):
        left = refusals(FileApi, self.files.proj)
        right = refusals(store_tasks, self.sql.proj)
        self.assertEqual(left, right)
        for name in ("ev-9", "ev-8", "ev-7", "ev-6"):  # a refused record leaves no body file behind
            self.assertFalse((self.sql.shit / "evidence" / (name + ".json")).exists(), name)
        self.assertNotIn(None, left)
        self.assertEqual(left[:3], [errors.USAGE, errors.PRECONDITION, errors.ILLEGAL_TRANSITION])
        conn = store.connect(self.sql.proj)
        self.addCleanup(conn.close)
        self.assertEqual(store.verify_tasks(conn), [])
        self.assertEqual(store_tasks.verify_bodies(self.sql.proj, conn), [])


# -- what is new: the body is a file written first, checked by digest after ------------------------------------


class BodyTests(StoreCase):
    def test_the_body_is_on_disk_and_unindexed_when_the_transaction_starts(self):
        self.make()
        seen = []

        def probe(stage):
            if stage == "body-published":
                conn = self.conn()
                seen.append((os.path.exists(self.shit / "evidence" / "ev-1.json"),
                             store.get_record(conn, "evidence", "ev-1")))

        original = store_tasks._fault
        store_tasks._fault = probe
        try:
            self.evidence()
        finally:
            store_tasks._fault = original
        self.assertEqual(seen, [(True, None)])
        self.consistent()

    def test_the_index_holds_the_digest_of_the_bytes_on_disk(self):
        self.make()
        self.evidence()
        data = (self.shit / "evidence" / "ev-1.json").read_bytes()
        row = store.get_record(self.conn(), "evidence", "ev-1")
        self.assertEqual((row["sha256"], row["size"]), (hashlib.sha256(data).hexdigest(), len(data)))
        task_body = (self.shit / "tasks" / "t1.json").read_bytes()
        self.assertEqual(store.get_record(self.conn(), "task", "t1")["sha256"], hashlib.sha256(task_body).hexdigest())

    def test_a_fault_after_the_body_leaves_an_unindexed_file_and_a_retry_completes_it(self):
        self.make()

        def boom(stage):
            if stage == "body-published":
                raise RuntimeError("injected")

        original = store_tasks._fault
        store_tasks._fault = boom
        try:
            with self.assertRaises(RuntimeError):
                self.evidence()
        finally:
            store_tasks._fault = original
        orphan = (self.shit / "evidence" / "ev-1.json").read_bytes()
        self.assertIsNone(store.get_record(self.conn(), "evidence", "ev-1"))
        self.assertEqual(self.count_rows("events"), 1)
        self.assertEqual(store_tasks.verify_bodies(self.proj, self.conn(), include_orphans=True), ["body_orphan"])
        result = self.evidence(now=T1)  # an exact retry adopts the body that is on disk
        self.assertEqual(result["created_at"], T1)
        self.assertEqual((self.shit / "evidence" / "ev-1.json").read_bytes(), orphan)
        self.assertEqual(store.record_event(self.conn(), "evidence", "ev-1")["timestamp"], T1)
        self.consistent()

    def test_an_orphan_body_of_another_task_or_actor_is_not_adopted(self):
        self.make("t1")
        self.make("t2", T1)
        self.proj.ensure_dir("evidence")
        other_task = commands.artifacts.dump("evidence", commands._base(
            "evidence", "ev-1", "t2", "wrapper", T2, description="d", path="out.txt", sha256="0" * 64, size=1))
        self.proj.write_atomic("evidence/ev-1.json", other_task)
        before = (self.count_rows("events"), self.count_rows("records"))
        self.assertEqual(code_of(self.evidence, "ev-1", "t1", T3), errors.REJECTED)
        self.assertEqual((self.count_rows("events"), self.count_rows("records")), before)
        self.proj.write_atomic("evidence/ev-1.json", b"not json")
        self.assertEqual(code_of(self.evidence, "ev-1", "t1", T3), errors.REJECTED)
        self.assertEqual((self.count_rows("events"), self.count_rows("records")), before)

    def test_an_orphan_body_with_different_submitted_content_is_refused_and_reported(self):
        self.make()
        self.proj.ensure_dir("decisions")
        orphan = commands.artifacts.dump("decision", commands._base(
            "decision", "d-1", "t1", "human", T1, status="accepted", question="q", choice="old",
            rationale="old"))
        self.proj.write_atomic("decisions/d-1.json", orphan)
        before = (self.count_rows("events"), self.count_rows("records"))
        code = code_of(store_tasks.add_decision, self.proj, task_id="t1", decision_id="d-1", status="rejected",
                       question="q", choice="new", rationale="new", actor="human", now=T2)
        self.assertEqual(code, errors.REJECTED)
        self.assertEqual((self.count_rows("events"), self.count_rows("records")), before)
        self.assertEqual((self.shit / "decisions" / "d-1.json").read_bytes(), orphan)
        check_code, lines = check.run_check(self.repo)
        self.assertEqual(check_code, errors.FAILED_CLOSED)
        self.assertIn("store: record body_orphan does not verify", lines)

    def test_an_orphan_task_body_with_the_same_content_is_adopted_and_another_is_refused(self):
        def data_for(title):
            return records.dump_task({
                "schema": "task", "schema_version": 1, "id": "t1", "title": title, "summary": "Summary",
                "state": "open", "owner": "human", "created_at": T0, "updated_at": T0, "history": []})

        self.proj.write_atomic("tasks/t1.json", data_for("Different"))
        self.assertEqual(code_of(self.make, "t1", T1), errors.USAGE)
        self.assertEqual(self.count_rows("tasks"), 0)
        self.proj.write_atomic("tasks/t1.json", data_for("Title"))
        created = self.make("t1", T1)
        self.assertEqual(created["created_at"], T0)  # the body that was on disk wins
        self.consistent()

    def test_an_indexed_duplicate_is_refused_before_the_body_is_touched(self):
        self.make()
        self.evidence()
        body = (self.shit / "evidence" / "ev-1.json").read_bytes()
        self.assertEqual(code_of(self.evidence, "ev-1", "t1", T2), errors.USAGE)
        self.assertEqual((self.shit / "evidence" / "ev-1.json").read_bytes(), body)

    def test_a_missing_task_leaves_no_body_behind(self):
        self.make()
        self.assertEqual(code_of(self.evidence, "ev-1", "ghost", T1), errors.PRECONDITION)
        self.assertFalse((self.shit / "evidence" / "ev-1.json").exists())

    def test_a_publication_pair_left_by_a_crash_fails_closed_and_another_id_works(self):
        self.make()
        self.proj.ensure_dir("evidence")
        data = commands.artifacts.dump("evidence", commands._base(
            "evidence", "ev-1", "t1", "wrapper", T1, description="d", path="out.txt", sha256="0" * 64, size=1))
        self.proj.write_atomic("evidence/ev-1.json", data)
        os.link(self.shit / "evidence" / "ev-1.json", self.shit / "evidence" / ".tmp-deadbeef")
        before = (self.count_rows("events"), self.count_rows("records"))
        self.assertEqual(code_of(self.evidence), errors.CONFINEMENT)  # the second link makes the body unreadable
        self.assertEqual((self.count_rows("events"), self.count_rows("records")), before)
        self.evidence("ev-2")
        self.consistent()


class TamperTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.make()
        self.evidence()
        self.consistent()

    def test_a_changed_body_is_refused_on_every_read_and_named_by_verify(self):
        path = "evidence/ev-1.json"
        data = self.proj.read(path)
        self.proj.write_atomic(path, data.replace(b'"d"', b'"x"'))
        self.assertEqual(code_of(store_tasks.show, self.proj, "evidence", "ev-1"), errors.REJECTED)
        self.assertEqual(store_tasks.verify_bodies(self.proj, self.conn()), ["body_differs_from_index"])

    def test_a_changed_task_body_is_refused(self):
        data = self.proj.read("tasks/t1.json")
        self.proj.write_atomic("tasks/t1.json", data.replace(b"Title", b"Other"))
        self.assertEqual(code_of(store_tasks.get_task, self.proj, "t1"), errors.REJECTED)
        self.assertEqual(store_tasks.verify_bodies(self.proj, self.conn()), ["body_differs_from_index"])

    def test_a_missing_body_is_named_and_reads_as_a_missing_precondition(self):
        os.unlink(self.shit / "evidence" / "ev-1.json")
        self.assertEqual(code_of(store_tasks.show, self.proj, "evidence", "ev-1"), errors.PRECONDITION)
        self.assertEqual(store_tasks.verify_bodies(self.proj, self.conn()), ["body_missing"])

    def test_a_body_swapped_between_the_commit_and_the_check_is_refused(self):
        def swap(stage):
            if stage == "committed":
                self.proj.write_atomic("evidence/ev-2.json", b"{}")

        original = store_tasks._fault
        store_tasks._fault = swap
        try:
            self.assertEqual(code_of(self.evidence, "ev-2", "t1", T2), errors.REJECTED)
        finally:
            store_tasks._fault = original
        self.assertIsNotNone(store.get_record(self.conn(), "evidence", "ev-2"))  # committed; the swap is reported
        self.assertEqual(store_tasks.verify_bodies(self.proj, self.conn()), ["body_differs_from_index"])

    def test_a_symlinked_body_is_refused_not_followed(self):
        outside = self.repo / "elsewhere.json"
        outside.write_bytes((self.shit / "evidence" / "ev-1.json").read_bytes())
        os.unlink(self.shit / "evidence" / "ev-1.json")
        os.symlink(outside, self.shit / "evidence" / "ev-1.json")
        self.assertEqual(code_of(store_tasks.show, self.proj, "evidence", "ev-1"), errors.CONFINEMENT)
        self.assertEqual(store_tasks.verify_bodies(self.proj, self.conn()), ["body_unreadable"])


class StoreAccessTests(StoreCase):
    def test_a_read_on_a_project_without_a_store_is_a_missing_precondition(self):
        self.assertEqual(code_of(store_tasks.get_task, self.proj, "t1"), errors.PRECONDITION)
        self.assertEqual(code_of(store_tasks.show, self.proj, "evidence", "x"), errors.PRECONDITION)
        self.assertFalse((self.shit / store.DB_NAME).exists())

    def test_creating_a_task_creates_the_store_and_a_second_call_reuses_it(self):
        self.make("t1")
        self.make("t2", T1)
        self.assertEqual(self.count_rows("tasks"), 2)
        self.consistent()

    def test_the_busy_timeout_is_passed_through(self):
        self.make()
        holder = self.conn()
        with store.write_transaction(holder):
            self.assertEqual(code_of(self.evidence, busy_timeout_ms=0), errors.BUSY)
        # The body was written first, so it is an unindexed file now; the repeated call adopts it.
        self.assertTrue((self.shit / "evidence" / "ev-1.json").exists())
        self.assertIsNone(store.get_record(self.conn(), "evidence", "ev-1"))
        self.evidence()
        self.consistent()

    def test_hostile_ids_are_refused_before_anything_is_written(self):
        for bad in ("../x", "a/b", "A", "", "x" * 65, "a\x00b"):
            with self.subTest(bad=bad):
                self.assertEqual(code_of(store_tasks.get_task, self.proj, bad), errors.REJECTED)
                self.assertEqual(code_of(self.make, bad), errors.REJECTED)
        self.assertFalse((self.shit / store.DB_NAME).exists())


class ConcurrentTests(StoreCase):
    def test_parallel_evidence_for_one_task_keeps_one_chain_and_matching_bodies(self):
        self.make()
        failures = []

        def work(worker):
            proj = Project.open(self.repo)  # a project object holds one lock descriptor, so one per thread
            try:
                for number in range(3):
                    self.evidence(eid=f"ev-{worker}-{number}", now=T1, proj=proj)
            except BaseException as exc:  # noqa: BLE001 - reported to the main thread
                failures.append(repr(exc))
            finally:
                proj.close()

        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(self.count_rows("records"), 13)
        self.consistent()

    def test_the_same_record_from_three_callers_has_one_winner(self):
        self.make()
        results = []

        def work():
            proj = Project.open(self.repo)
            try:
                results.append(code_of(self.evidence, "ev-same", "t1", T1, proj))
            finally:
                proj.close()

        threads = [threading.Thread(target=work) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results, key=lambda c: (c is not None, c)), [None, errors.USAGE, errors.USAGE])
        self.consistent()


CHILD = """
import os, signal, sys
sys.path.insert(0, sys.argv[1])
from shit import store, store_tasks
from shit.project import Project
proj = Project.open(sys.argv[2])
where, stage = sys.argv[3], sys.argv[4]
def fault(seen):
    if seen == stage:
        os.kill(os.getpid(), signal.SIGKILL)
setattr(store_tasks if where == "adapter" else store, "_fault", fault)
store_tasks.add_evidence(proj, task_id="t1", evidence_id="ev-1", path="out.txt", description="d", actor="wrapper",
                         now="2026-11-27T10:01:00Z")
sys.exit(3)
"""


class CrashTests(StoreCase):
    def kill_at(self, where, stage):
        proc = subprocess.run([sys.executable, "-I", "-c", CHILD, str(ROOT), str(self.repo), where, stage],
                              capture_output=True, timeout=60, check=False)
        self.assertEqual(proc.returncode, -signal.SIGKILL, proc.stderr[-300:])

    def test_a_kill_at_every_stage_is_recovered_by_repeating_the_call(self):
        for where, stage in (("adapter", "body-published"), ("store", "checked"), ("store", "record-row")):
            with self.subTest(where=where, stage=stage):
                self.env = Env(self)
                self.repo, self.proj, self.shit = self.env.repo, self.env.proj, self.env.shit
                self.make()
                self.kill_at(where, stage)
                self.assertIsNone(store.get_record(self.conn(), "evidence", "ev-1"))
                self.assertEqual(self.count_rows("events"), 1)
                self.consistent()
                self.evidence(now=T1)
                self.assertIsNotNone(store.get_record(self.conn(), "evidence", "ev-1"))
                self.assertEqual(self.count_rows("events"), 2)
                self.consistent()

    def test_a_kill_after_the_commit_leaves_a_complete_record(self):
        self.make()
        self.kill_at("adapter", "committed")
        self.assertIsNotNone(store.get_record(self.conn(), "evidence", "ev-1"))
        self.consistent()
        self.assertEqual(code_of(self.evidence, "ev-1", "t1", T2), errors.USAGE)


if __name__ == "__main__":
    unittest.main()
