"""The adapter `shit/store_queue.py`: the names and arguments of `shit/queue.py` on the database store (issue 61, C4).

Real files and a real database in a temporary repository. The records that edges and closing depend on are written
with `shit/store_tasks.py`, so the milestone and closing rules the adapter supplies are exercised for real.
"""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from _bootstrap import make_git_dir

from shit import errors, store, store_queue, store_tasks
from shit.errors import ShitError
from shit.project import Project

BASE = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC)


def at(seconds):
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def code_of(func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except ShitError as exc:
        return exc.code
    return None


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        self.repo.mkdir()
        make_git_dir(self.repo / ".git")
        self.proj = Project.init(self.repo)
        self.addCleanup(self.proj.close)
        self.clock = 0

    def tick(self, seconds=1):
        self.clock += seconds
        return at(self.clock)

    def task(self, task_id, enqueue=True):
        store_tasks.create_task(self.proj, task_id=task_id, title="Title", summary="Summary", owner="human",
                                actor="human", now=self.tick())
        if enqueue:
            store_queue.enqueue(self.proj, task_id, "human", now=self.tick())

    def claim(self, task_id, **kwargs):
        request = {"task_id": task_id, "step": "draft", "worker": "w1", "worker_kind": "human"}
        return store_queue.claim(self.proj, request, now=self.tick(), **kwargs)

    @staticmethod
    def present(lease):
        return {"task_id": lease["task_id"], "lease_id": lease["lease_id"], "generation": lease["generation"]}

    def status(self, **kwargs):
        return store_queue.status(self.proj, now=self.tick(), **kwargs)

    def consistent(self):
        conn = store.connect(self.proj)
        try:
            self.assertEqual(store.verify(conn), [])
            self.assertEqual(store.verify_tasks(conn), [])
            self.assertEqual(store_tasks.verify_bodies(self.proj, conn), [])
        finally:
            conn.close()


class FlowTests(AdapterCase):
    def test_a_human_worker_path_through_the_names_of_queue_py(self):
        self.task("t1")
        lease = self.claim("t1")
        self.assertEqual(self.status()["t1"]["state"], "dispatched")
        store_queue.start(self.proj, self.present(lease), now=self.tick())
        self.assertEqual(store_tasks.get_task(self.proj, "t1")["state"], "running")
        store_queue.renew(self.proj, self.present(lease), now=self.tick())
        store_queue.submit(self.proj, self.present(lease), now=self.tick())
        row = self.status()["t1"]
        self.assertEqual((row["state"], row["lease"], row["attempts"]), ("review", None, 1))
        self.assertEqual(store_tasks.get_task(self.proj, "t1")["state"], "review")
        self.consistent()

    def test_hold_fail_reclaim_release_and_reopen(self):
        for name in ("a", "b", "c"):
            self.task(name)
        first = self.claim("a")
        store_queue.release(self.proj, self.present(first), now=self.tick())
        second = self.claim("b")
        store_queue.start(self.proj, self.present(second), now=self.tick())
        store_queue.hold(self.proj, self.present(second), "owner", "Waiting.", "item-1", now=self.tick())
        third = self.claim("c")
        store_queue.fail(self.proj, self.present(third), now=self.tick())
        store_queue.reopen(self.proj, "b", "human", "Answered.", now=self.tick())
        store_queue.reopen(self.proj, "c", "human", "Again.", now=self.tick())
        again = self.claim("a")
        store_queue.reclaim(self.proj, "a", "human", now=self.tick())
        self.assertEqual(again["generation"], 2)
        states = {name: row["state"] for name, row in self.status().items()}
        self.assertEqual(states, {"a": "ready", "b": "ready", "c": "ready"})
        self.consistent()

    def test_the_refusals_have_the_codes_of_queue_py(self):
        self.task("a")
        lease = self.claim("a")
        bad = {**self.present(lease), "lease_id": "0" * 32}
        self.assertEqual(code_of(store_queue.start, self.proj, bad, now=self.tick()), errors.LEASE_REFUSED)
        self.assertEqual(code_of(store_queue.submit, self.proj, self.present(lease), now=self.tick()),
                         errors.ILLEGAL_TRANSITION)
        self.assertEqual(code_of(store_queue.enqueue, self.proj, "a", "human", now=self.tick()), errors.REJECTED)
        self.assertEqual(code_of(store_queue.enqueue, self.proj, "a", "model", now=self.tick()), errors.HUMAN_APPROVAL)
        self.assertEqual(code_of(store_queue.status, self.proj, now=self.tick(), config={"nope": 1}), errors.REJECTED)
        self.consistent()


class RecordRulesTests(AdapterCase):
    def record(self, task_id, kind, rid, **kwargs):
        stamp = self.tick()
        if kind == "report":
            store_tasks.add_report(self.proj, task_id=task_id, report_id=rid, outcome="success", summary="ok",
                                   evidence_ids=[], actor="human", now=stamp)
        elif kind == "review":
            store_tasks.add_review(self.proj, task_id=task_id, review_id=rid, report_id=kwargs["report"],
                                   verdict="pass", summary="ok", actor="human", now=stamp)
        else:
            store_tasks.add_decision(self.proj, task_id=task_id, decision_id=rid, status="accepted", question="Q?",
                                     choice="Yes.", rationale="Because.", actor="human", now=stamp)

    def test_a_draft_edge_waits_for_a_success_report_and_promotion_follows(self):
        self.task("pre")
        self.task("dep", enqueue=False)
        store_queue.add_edge(self.proj, "dep", "pre", "draft", "human", now=self.tick())
        store_queue.enqueue(self.proj, "dep", "human", now=self.tick())
        self.assertEqual(self.status()["dep"]["state"], "waiting")
        self.assertEqual(code_of(self.claim, "dep"), errors.ILLEGAL_TRANSITION)
        self.record("pre", "report", "r1")
        self.assertEqual(self.status()["dep"]["state"], "ready")  # promoted by the maintenance of `status`
        self.assertEqual(self.claim("dep")["generation"], 1)
        self.consistent()

    def reviewed(self, name):
        self.task(name)
        lease = self.claim(name)
        store_queue.start(self.proj, self.present(lease), now=self.tick())
        store_queue.submit(self.proj, self.present(lease), now=self.tick())

    def test_complete_proves_the_close_rules_inside_its_transaction(self):
        self.reviewed("b")
        store_tasks.transition(self.proj, "b", "approved", "human", "ok", now=self.tick())
        self.assertEqual(code_of(store_queue.complete, self.proj, "b", "human", "Done.", now=self.tick()),
                         errors.PRECONDITION)  # no success report
        self.reviewed("a")
        self.record("a", "report", "r1")
        self.record("a", "review", "v1", report="r1")
        self.assertEqual(code_of(store_queue.complete, self.proj, "a", "human", "Done.", now=self.tick()),
                         errors.ILLEGAL_TRANSITION)  # not approved yet
        store_tasks.transition(self.proj, "a", "approved", "human", "ok", now=self.tick())
        self.assertEqual(code_of(store_queue.complete, self.proj, "a", "human", "Done.", now=self.tick()),
                         errors.HUMAN_APPROVAL)  # no accepted human decision yet
        self.record("a", "decision", "d1")
        store_queue.complete(self.proj, "a", "human", "Done.", now=self.tick())
        self.assertEqual(store_tasks.get_task(self.proj, "a")["state"], "closed")
        self.assertEqual(self.status()["a"]["state"], "done")
        self.consistent()

    def test_the_attempt_limit_needs_a_decision_after_the_failure(self):
        self.task("a")
        config = {"max_attempts": 1}
        lease = self.claim("a")
        store_queue.start(self.proj, self.present(lease), now=self.tick())
        store_queue.fail(self.proj, self.present(lease), now=self.tick())
        self.assertEqual(code_of(store_queue.reopen, self.proj, "a", "human", "again", now=self.tick(), config=config),
                         errors.HUMAN_APPROVAL)
        self.record("a", "decision", "d1")
        store_queue.reopen(self.proj, "a", "human", "again", now=self.tick(), config=config)
        self.assertEqual(self.status()["a"]["state"], "ready")
        self.consistent()


class AmbiguousCommitTests(AdapterCase):
    def test_a_repeat_after_an_ambiguous_commit_returns_the_first_answer_and_writes_nothing_more(self):
        self.task("a")
        real = store.queue_claim
        calls = []

        def landed_then_ambiguous(conn, request, now, **kwargs):
            lease = real(conn, request, now, **kwargs)
            calls.append(kwargs["key"])
            if len(calls) == 1:
                raise store.AmbiguousCommit  # the commit landed; the caller cannot know
            return lease

        with mock.patch.object(store, "queue_claim", landed_then_ambiguous):
            lease = self.claim("a")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        conn = store.connect(self.proj)
        try:
            claims = [e for e in store.queue_entries(conn) if e["kind"] == "claimed"]
            self.assertEqual(len(claims), 1)
            self.assertEqual(claims[0]["lease_id"], lease["lease_id"])
        finally:
            conn.close()
        self.consistent()

    def test_a_repeat_after_a_commit_that_did_not_land_writes_once(self):
        self.task("a")
        real = store.queue_claim
        calls = []

        def lost(conn, request, now, **kwargs):
            calls.append(kwargs["key"])
            if len(calls) == 1:
                raise store.AmbiguousCommit  # nothing was written
            return real(conn, request, now, **kwargs)

        with mock.patch.object(store, "queue_claim", lost):
            lease = self.claim("a")
        self.assertEqual(lease["generation"], 1)
        self.assertEqual(store_queue.entry_for_key(self.proj, calls[0])["lease_id"], lease["lease_id"])
        self.consistent()

    def test_the_error_reaches_the_caller_after_the_last_attempt(self):
        self.task("a")

        def always(conn, request, now, **kwargs):
            raise store.AmbiguousCommit

        with mock.patch.object(store, "queue_claim", always):
            self.assertEqual(code_of(self.claim, "a"), errors.FAILED_CLOSED)
        self.consistent()


if __name__ == "__main__":
    unittest.main()
