"""Queue operations of the SQLite store (issue 61, C4): one write transaction per operation.

Real database files in a temporary directory; nothing about sqlite is mocked. Every helper test ends with
`store.verify`, `store.verify_tasks` and a replay of the whole log by `queue_records.replay`.
"""

import hashlib
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from _bootstrap import make_git_dir

from shit import dependencies, errors, queue_records, records, store
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


def task_dict(task_id, now):
    return {"schema": "task", "schema_version": 1, "id": task_id, "title": "Title", "summary": "Summary",
            "state": "open", "owner": "human", "created_at": now, "updated_at": now, "history": []}


def reached_all(edge):
    return True


class Recorder:
    """A real connection that remembers every statement it ran."""

    def __init__(self, real):
        self.real, self.sql = real, []

    @property
    def in_transaction(self):
        return self.real.in_transaction

    def execute(self, sql, *args):
        self.sql.append(sql)
        return self.real.execute(sql, *args)


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
        self.clock = 0

    def connect(self, **kwargs):
        conn = store.connect(self.proj, **kwargs)
        self.addCleanup(conn.close)
        return conn

    def raw(self):
        conn = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(conn.close)
        return conn

    def tick(self, seconds=1):
        self.clock += seconds
        return at(self.clock)

    def consistent(self, conn=None):
        conn = conn or self.conn
        self.assertEqual(store.verify(conn), [])
        self.assertEqual(store.verify_tasks(conn), [])
        # The log the database holds is one `queue_records.replay` accepts.
        queue_records.replay(store.queue_entries(conn))

    def task(self, task_id, enqueue=True, actor="human"):
        stamp = self.tick()
        task = task_dict(task_id, stamp)
        data = records.dump_task(task)
        store.create_task(self.conn, task, "human", hashlib.sha256(data).hexdigest(), len(data))
        if enqueue:
            store.queue_enqueue(self.conn, task_id, actor, self.tick())

    def claim(self, task_id, step="draft", worker="w1", kind="ai", **kwargs):
        request = {"task_id": task_id, "step": step, "worker": worker, "worker_kind": kind}
        return store.queue_claim(self.conn, request, self.tick(), **kwargs)

    def running(self, task_id, step="draft", **kwargs):
        lease = self.claim(task_id, step=step, **kwargs)
        store.queue_start(self.conn, self.present(lease), self.tick())
        return lease

    @staticmethod
    def present(lease):
        return {"task_id": lease["task_id"], "lease_id": lease["lease_id"], "generation": lease["generation"]}

    def state(self, task_id):
        return store.queue_state(self.conn, task_id)

    def lifecycle(self, task_id):
        return self.conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]

    def count(self, table):
        return self.conn.execute("SELECT count(*) FROM " + table).fetchone()[0]


class FlowTests(Base):
    def test_a_human_worker_goes_from_enqueue_to_review(self):
        self.task("t1")
        self.assertEqual(self.state("t1")["state"], "ready")
        self.consistent()
        lease = self.claim("t1", worker="alice", kind="human")
        self.assertEqual((lease["generation"], lease["renewals"], lease["step"]), (1, 0, "draft"))
        self.assertEqual(self.state("t1")["state"], "dispatched")
        self.assertEqual(self.lifecycle("t1"), "open")
        self.consistent()
        store.queue_start(self.conn, self.present(lease), self.tick())
        self.assertEqual((self.state("t1")["state"], self.lifecycle("t1")), ("running", "running"))
        self.consistent()
        renewed = store.queue_renew(self.conn, self.present(lease), self.tick())
        self.assertEqual(renewed["renewals"], 1)
        self.consistent()
        store.queue_submit(self.conn, self.present(lease), self.tick())
        self.assertEqual((self.state("t1")["state"], self.lifecycle("t1")), ("review", "review"))
        self.assertEqual(self.count("leases"), 0)
        self.consistent()

    def test_release_returns_to_ready_and_the_generation_rises(self):
        self.task("t1")
        first = self.claim("t1")
        store.queue_release(self.conn, self.present(first), self.tick())
        self.assertEqual(self.state("t1")["state"], "ready")
        self.assertEqual(self.count("leases"), 0)
        second = self.claim("t1")
        self.assertEqual(second["generation"], 2)
        self.consistent()


class OperationTests(Base):
    def test_enqueue_is_waiting_while_an_edge_is_unmet_and_ready_otherwise(self):
        self.task("a", enqueue=False)
        self.task("b", enqueue=False)
        store.add_edge(self.conn, "b", "a", "draft")
        store.queue_enqueue(self.conn, "b", "human", self.tick())
        self.assertEqual(self.state("b")["state"], "waiting")
        store.queue_enqueue(self.conn, "a", "wrapper", self.tick())
        self.assertEqual(self.state("a")["state"], "ready")
        self.consistent()

    def test_enqueue_refusals(self):
        self.task("a", enqueue=False)
        self.assertEqual(code_of(store.queue_enqueue, self.conn, "a", "model", self.tick()), errors.HUMAN_APPROVAL)
        self.assertEqual(code_of(store.queue_enqueue, self.conn, "Bad Id", "human", self.tick()), errors.REJECTED)
        self.assertEqual(code_of(store.queue_enqueue, self.conn, "ghost", "human", self.tick()), errors.PRECONDITION)
        store.queue_enqueue(self.conn, "a", "human", self.tick())
        self.assertEqual(code_of(store.queue_enqueue, self.conn, "a", "human", self.tick()), errors.REJECTED)
        self.task("b", enqueue=False)
        store.transition_task(self.conn, "b", "failed", "human", "no", self.tick())
        self.assertEqual(code_of(store.queue_enqueue, self.conn, "b", "human", self.tick()), errors.ILLEGAL_TRANSITION)
        self.assertEqual(code_of(store.queue_enqueue, self.conn, "a", "human", "not a stamp"), errors.REJECTED)
        self.consistent()

    def test_claim_needs_a_ready_task_and_exact_fields(self):
        self.task("a")
        request = {"task_id": "a", "step": "draft", "worker": "w1", "worker_kind": "ai"}
        for bad in ({**request, "lease_id": "x" * 32}, {**request, "timestamp": at(1)}, {"task_id": "a"}, None):
            self.assertEqual(code_of(store.queue_claim, self.conn, bad, self.tick()), errors.USAGE)
        for bad in ({**request, "step": "nope"}, {**request, "worker": "Bad Worker"}, {**request, "worker_kind": "bot"}):
            self.assertEqual(code_of(store.queue_claim, self.conn, bad, self.tick()), errors.USAGE)
        self.assertEqual(code_of(store.queue_claim, self.conn, {**request, "task_id": "ghost"}, self.tick()),
                         errors.PRECONDITION)
        self.claim("a")
        self.assertEqual(code_of(self.claim, "a"), errors.ILLEGAL_TRANSITION)  # one live lease per task
        self.assertEqual(self.count("leases"), 1)
        self.consistent()

    def test_claim_waits_for_the_edge_and_promotes_in_the_same_transaction(self):
        self.task("a")
        self.task("b", enqueue=False)
        store.add_edge(self.conn, "b", "a", "draft")
        store.queue_enqueue(self.conn, "b", "human", self.tick())
        self.assertEqual(code_of(self.claim, "b"), errors.ILLEGAL_TRANSITION)
        entries = self.count("queue_entries")
        lease = self.claim("b", met=reached_all)  # promotion and the claim in one transaction
        self.assertEqual(lease["generation"], 1)
        self.assertEqual(self.count("queue_entries"), entries + 2)  # the `ready` entry and the claim
        self.consistent()

    def test_presented_lease_must_match(self):
        self.task("a")
        lease = self.claim("a")
        wrong_id = {**self.present(lease), "lease_id": "0" * 32}
        wrong_gen = {**self.present(lease), "generation": 2}
        for bad in (wrong_id, wrong_gen):
            for op in (store.queue_start, store.queue_renew, store.queue_release, store.queue_submit, store.queue_fail):
                self.assertEqual(code_of(op, self.conn, bad, self.tick()), errors.LEASE_REFUSED, op.__name__)
        self.assertEqual(code_of(store.queue_hold, self.conn, wrong_id, "owner", "r", "i", self.tick()),
                         errors.LEASE_REFUSED)
        self.assertEqual(code_of(store.queue_start, self.conn, {"task_id": "a"}, self.tick()), errors.USAGE)
        self.assertEqual(code_of(store.queue_start, self.conn, {**self.present(lease), "generation": "1"}, self.tick()),
                         errors.USAGE)
        self.assertEqual(code_of(store.queue_start, self.conn, {**self.present(lease), "task_id": "ghost"}, self.tick()),
                         errors.PRECONDITION)
        self.assertEqual(self.state("a")["state"], "dispatched")
        self.consistent()

    def test_a_stale_generation_is_refused_after_a_second_claim(self):
        self.task("a")
        first = self.claim("a")
        store.queue_release(self.conn, self.present(first), self.tick())
        second = self.claim("a")
        self.assertEqual(code_of(store.queue_release, self.conn, self.present(first), self.tick()), errors.LEASE_REFUSED)
        self.assertEqual(self.state("a")["lease"]["lease_id"], second["lease_id"])
        self.consistent()

    def test_release_of_a_running_task_and_submit_of_a_dispatched_one_are_refused(self):
        self.task("a")
        lease = self.claim("a")
        self.assertEqual(code_of(store.queue_submit, self.conn, self.present(lease), self.tick()), errors.ILLEGAL_TRANSITION)
        store.queue_start(self.conn, self.present(lease), self.tick())
        self.assertEqual(code_of(store.queue_release, self.conn, self.present(lease), self.tick()), errors.ILLEGAL_TRANSITION)
        self.assertEqual(code_of(store.queue_start, self.conn, self.present(lease), self.tick()), errors.ILLEGAL_TRANSITION)
        self.consistent()

    def test_renewal_bounds(self):
        self.task("a")
        config = {"lease_ttl_seconds": 60, "lease_max_seconds": 150, "lease_max_renewals": 5}
        lease = self.claim("a", config=config)
        renewed = store.queue_renew(self.conn, self.present(lease), self.tick(), config=config)
        self.assertEqual((renewed["renewals"], renewed["expires_at"]), (1, store._plus(lease["expires_at"], 60)))
        # The total length is capped at 150 seconds from the claim: the next renewal is clipped, the one after refused.
        third = store.queue_renew(self.conn, self.present(lease), self.tick(), config=config)
        self.assertEqual(third["expires_at"], store._plus(lease["claimed_at"], 150))
        self.assertEqual(code_of(store.queue_renew, self.conn, self.present(lease), self.tick(), config=config),
                         errors.LEASE_REFUSED)
        self.consistent()
        self.task("b")
        capped = {"lease_ttl_seconds": 600, "lease_max_seconds": 6000, "lease_max_renewals": 1}
        other = self.claim("b", config=capped)
        store.queue_renew(self.conn, self.present(other), self.tick(), config=capped)
        self.assertEqual(code_of(store.queue_renew, self.conn, self.present(other), self.tick(), config=capped),
                         errors.LEASE_REFUSED)
        self.consistent()

    def test_hold_and_fail_end_the_lease_and_a_human_reopens(self):
        self.task("a")
        lease = self.running("a")
        store.queue_hold(self.conn, self.present(lease), "owner", "Waiting for an answer.", "issue-1", self.tick())
        self.assertEqual((self.state("a")["state"], self.lifecycle("a")), ("hold-owner", "blocked"))
        row = self.conn.execute("SELECT blocked_reason, blocking_item FROM tasks WHERE id = 'a'").fetchone()
        self.assertEqual(tuple(row), ("Waiting for an answer.", "issue-1"))
        self.assertEqual(self.count("leases"), 0)
        self.consistent()
        self.assertEqual(code_of(store.queue_reopen, self.conn, "a", "wrapper", "go", self.tick()),
                         errors.ILLEGAL_TRANSITION)
        store.queue_reopen(self.conn, "a", "human", "Answered.", self.tick())
        self.assertEqual((self.state("a")["state"], self.lifecycle("a")), ("ready", "open"))
        self.assertIsNone(self.conn.execute("SELECT blocked_reason FROM tasks WHERE id = 'a'").fetchone()[0])
        self.consistent()
        second = self.running("a")
        store.queue_fail(self.conn, self.present(second), self.tick())
        self.assertEqual((self.state("a")["state"], self.lifecycle("a")), ("failed", "failed"))
        store.queue_reopen(self.conn, "a", "human", "Try again.", self.tick())
        self.assertEqual(self.state("a")["state"], "ready")
        self.consistent()

    def test_a_dispatched_task_can_fail_or_hold_from_open(self):
        self.task("a")
        self.task("b")
        first, second = self.claim("a"), self.claim("b")
        store.queue_fail(self.conn, self.present(first), self.tick())
        store.queue_hold(self.conn, self.present(second), "ci", "A check is red.", "check-1", self.tick())
        self.assertEqual((self.lifecycle("a"), self.lifecycle("b")), ("failed", "blocked"))
        self.assertEqual(self.state("b")["state"], "hold-ci")
        self.consistent()

    def test_hold_input_is_checked_before_anything_is_written(self):
        self.task("a")
        lease = self.claim("a")
        before = self.count("queue_entries")
        self.assertEqual(code_of(store.queue_hold, self.conn, self.present(lease), "other", "r", "i", self.tick()), errors.USAGE)
        for reason in ("", "bad" + chr(0) + "text", "tab" + chr(9) + "here", 5):
            self.assertEqual(code_of(store.queue_hold, self.conn, self.present(lease), "owner", reason, "i", self.tick()),
                             errors.REJECTED)
        self.assertEqual(code_of(store.queue_hold, self.conn, self.present(lease), "owner", "x" * 501, "i", self.tick()),
                         errors.REJECTED)
        self.assertEqual(self.count("queue_entries"), before)
        self.consistent()

    def test_attempt_limit_needs_a_decision_to_reopen(self):
        self.task("a")
        config = {"max_attempts": 1}
        lease = self.running("a")
        store.queue_fail(self.conn, self.present(lease), self.tick())
        self.assertEqual(code_of(store.queue_reopen, self.conn, "a", "human", "again", self.tick(), config=config),
                         errors.HUMAN_APPROVAL)
        seen = []
        store.queue_reopen(self.conn, "a", "human", "again", self.tick(), config=config,
                           decided=lambda stamp: seen.append(stamp) or True)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.state("a")["state"], "ready")
        self.consistent()

    def test_complete_needs_review_approval_and_the_check(self):
        self.task("a")
        lease = self.running("a")
        self.assertEqual(code_of(store.queue_complete, self.conn, "a", "human", "done", self.tick()),
                         errors.ILLEGAL_TRANSITION)
        store.queue_submit(self.conn, self.present(lease), self.tick())
        self.assertEqual(code_of(store.queue_complete, self.conn, "a", "human", "done", self.tick()),
                         errors.ILLEGAL_TRANSITION)  # review, not yet approved
        store.transition_task(self.conn, "a", "approved", "human", "ok", self.tick())
        self.consistent()
        self.assertEqual(code_of(store.queue_complete, self.conn, "a", "model", "done", self.tick()),
                         errors.ILLEGAL_TRANSITION)

        def refuse(row):
            raise ShitError(errors.PRECONDITION)

        self.assertEqual(code_of(store.queue_complete, self.conn, "a", "human", "done", self.tick(), check=refuse),
                         errors.PRECONDITION)
        seen = []
        store.queue_complete(self.conn, "a", "human", "done", self.tick(), check=seen.append)
        self.assertEqual(seen[0]["state"], "approved")
        self.assertEqual((self.state("a")["state"], self.lifecycle("a")), ("done", "closed"))
        self.consistent()
        self.assertEqual(code_of(store.queue_reopen, self.conn, "a", "human", "x", self.tick()), errors.ILLEGAL_TRANSITION)

    def test_reclaim_is_human_only_and_a_running_lease_ends_failed(self):
        self.task("a")
        self.task("b")
        first, second = self.claim("a"), self.running("b")
        self.assertEqual(code_of(store.queue_reclaim, self.conn, "a", "wrapper", self.tick()), errors.HUMAN_APPROVAL)
        self.assertEqual(code_of(store.queue_reclaim, self.conn, "ghost", "human", self.tick()), errors.PRECONDITION)
        store.queue_reclaim(self.conn, "a", "human", self.tick())
        store.queue_reclaim(self.conn, "b", "human", self.tick())
        self.assertEqual(self.state("a")["state"], "ready")  # a dispatched lease goes back to ready
        self.assertEqual((self.state("b")["state"], self.lifecycle("b")), ("failed", "failed"))  # a running one fails
        self.assertEqual(code_of(store.queue_reclaim, self.conn, "a", "human", self.tick()), errors.PRECONDITION)
        self.assertEqual(self.count("leases"), 0)
        self.assertEqual(code_of(store.queue_release, self.conn, self.present(first), self.tick()), errors.LEASE_REFUSED)
        self.assertEqual(second["generation"], 1)
        self.consistent()

    def test_expiry_is_applied_inside_the_next_operation(self):
        self.task("a")
        self.task("b")
        stale = self.running("a")
        later = at(self.clock + 905)
        # The next operation, by anyone, expires the lease first: the running task fails, in the same transaction.
        other = {"task_id": "b", "step": "draft", "worker": "w2", "worker_kind": "ai"}
        lease_b = store.queue_claim(self.conn, other, later)
        self.assertEqual((self.state("a")["state"], self.lifecycle("a")), ("failed", "failed"))
        self.assertEqual(code_of(store.queue_submit, self.conn, self.present(stale), store._plus(later, 1)),
                         errors.LEASE_REFUSED)
        self.assertEqual(self.state("b")["lease"]["lease_id"], lease_b["lease_id"])
        self.consistent()

    def test_a_dispatched_lease_that_expires_returns_to_ready(self):
        self.task("a")
        self.claim("a")
        store.queue_maintain(self.conn, at(self.clock + 1000))
        self.assertEqual(self.state("a")["state"], "ready")
        self.assertEqual(self.count("leases"), 0)
        self.consistent()

    def test_a_clock_earlier_than_the_log_fails_closed(self):
        self.task("a")
        before = self.count("queue_entries")
        self.assertEqual(code_of(store.queue_maintain, self.conn, at(0)), errors.FAILED_CLOSED)
        request = {"task_id": "a", "step": "draft", "worker": "w", "worker_kind": "ai"}
        self.assertEqual(code_of(store.queue_claim, self.conn, request, at(1)), errors.FAILED_CLOSED)
        self.assertEqual(self.count("queue_entries"), before)
        self.consistent()

    def test_promotion_is_part_of_maintenance(self):
        self.task("a", enqueue=False)
        self.task("b", enqueue=False)
        store.add_edge(self.conn, "b", "a", "review")
        store.queue_enqueue(self.conn, "b", "human", self.tick())
        store.queue_enqueue(self.conn, "a", "human", self.tick())
        self.assertEqual(self.state("b")["state"], "waiting")
        store.queue_maintain(self.conn, self.tick())
        self.assertEqual(self.state("b")["state"], "waiting")  # nothing reached the milestone
        store.queue_maintain(self.conn, self.tick(), met=reached_all)
        self.assertEqual(self.state("b")["state"], "ready")
        self.consistent()

    def test_submit_is_refused_while_a_blocking_edge_is_unmet(self):
        self.task("a", enqueue=False)
        self.task("b")
        lease = self.running("b")
        store.add_edge(self.conn, "b", "a", "draft")
        self.assertEqual(code_of(store.queue_submit, self.conn, self.present(lease), self.tick()), errors.PRECONDITION)
        store.queue_submit(self.conn, self.present(lease), self.tick(), met=reached_all)
        self.assertEqual(self.state("b")["state"], "review")
        self.consistent()

    def test_snapshot_reports_the_effective_state(self):
        self.task("a", enqueue=False)
        self.task("b", enqueue=False)
        store.add_edge(self.conn, "b", "a", "draft")
        store.queue_enqueue(self.conn, "a", "human", self.tick())
        store.queue_enqueue(self.conn, "b", "human", self.tick())
        snap = store.queue_snapshot(self.conn)
        self.assertEqual((snap["a"]["state"], snap["b"]["state"], snap["b"]["waiting_on"]), ("ready", "waiting", ["a"]))
        self.claim("a")
        self.assertEqual(store.queue_snapshot(self.conn)["a"]["lease"]["worker"], "w1")

    def test_arguments_are_checked_before_the_transaction(self):
        self.task("a")
        before = self.count("queue_entries")
        self.assertEqual(code_of(store.queue_maintain, self.conn, self.tick(), config={"nope": 1}), errors.REJECTED)
        self.assertEqual(code_of(store.queue_maintain, self.conn, self.tick(), config={"lease_ttl_seconds": 1}), errors.REJECTED)
        self.assertEqual(code_of(store.queue_maintain, self.conn, self.tick(), config="x"), errors.USAGE)
        self.assertEqual(code_of(store.queue_enqueue, self.conn, "a", "human", self.tick(), key="Bad Key"), errors.USAGE)
        self.assertEqual(code_of(store.queue_enqueue, self.conn, "a", "human", "2026-13-45T25:61:61Z"), errors.REJECTED)
        self.assertEqual(self.count("queue_entries"), before)
        self.assertFalse(self.conn.in_transaction)


class ApplyTests(Base):
    def apply_lease(self, task_id):
        self.task(task_id)
        lease = self.claim(task_id, step="apply")
        store.queue_start(self.conn, self.present(lease), self.tick())
        return lease

    def test_apply_markers_once_each_and_slots_follow_the_lease(self):
        lease = self.apply_lease("a")
        self.assertEqual(code_of(store.queue_apply_finish, self.conn, self.present(lease), self.tick()),
                         errors.ILLEGAL_TRANSITION)  # not started
        store.queue_apply_start(self.conn, self.present(lease), ["src/b.py", "src/a.py"], self.tick())
        slots = self.conn.execute("SELECT target FROM apply_slots ORDER BY target").fetchall()
        self.assertEqual([row[0] for row in slots], ["src/a.py", "src/b.py"])
        self.consistent()
        self.assertEqual(code_of(store.queue_apply_start, self.conn, self.present(lease), ["src/c.py"], self.tick()),
                         errors.ILLEGAL_TRANSITION)  # once per attempt
        store.queue_apply_finish(self.conn, self.present(lease), self.tick())
        self.assertEqual(self.count("apply_slots"), 0)
        self.assertTrue(self.state("a")["apply_finished"])
        self.assertEqual(code_of(store.queue_apply_finish, self.conn, self.present(lease), self.tick()),
                         errors.ILLEGAL_TRANSITION)  # once
        self.consistent()
        store.queue_submit(self.conn, self.present(lease), self.tick())
        self.consistent()

    def test_all_or_none_and_overlap_is_refused(self):
        first = self.apply_lease("a")
        second = self.apply_lease("b")
        store.queue_apply_start(self.conn, self.present(first), ["x.txt"], self.tick())
        before = self.count("queue_entries")
        self.assertEqual(code_of(store.queue_apply_start, self.conn, self.present(second), ["y.txt", "x.txt"], self.tick()),
                         errors.ILLEGAL_TRANSITION)
        self.assertEqual((self.count("apply_slots"), self.count("queue_entries")), (1, before))
        self.consistent()

    def test_every_way_a_lease_ends_drops_the_slots_first(self):
        for how in ("submit", "fail", "hold", "reclaim", "expire"):
            with self.subTest(how=how):
                name = "t-" + how
                lease = self.apply_lease(name)
                store.queue_apply_start(self.conn, self.present(lease), ["f-" + how], self.tick())
                self.assertEqual(self.count("apply_slots"), 1)
                if how == "submit":
                    store.queue_submit(self.conn, self.present(lease), self.tick())
                elif how == "fail":
                    store.queue_fail(self.conn, self.present(lease), self.tick())
                elif how == "hold":
                    store.queue_hold(self.conn, self.present(lease), "owner", "r", "i", self.tick())
                elif how == "reclaim":
                    store.queue_reclaim(self.conn, name, "human", self.tick())
                else:
                    self.clock += 5000
                    store.queue_maintain(self.conn, at(self.clock))
                self.assertEqual((self.count("apply_slots"), self.count("leases")), (0, 0))
                self.consistent()

    def test_apply_needs_an_apply_step_a_running_task_and_good_targets(self):
        self.task("a")
        lease = self.claim("a", step="draft")
        self.assertEqual(code_of(store.queue_apply_start, self.conn, self.present(lease), ["x"], self.tick()),
                         errors.ILLEGAL_TRANSITION)  # dispatched, and not an apply step
        store.queue_start(self.conn, self.present(lease), self.tick())
        self.assertEqual(code_of(store.queue_apply_start, self.conn, self.present(lease), ["x"], self.tick()),
                         errors.ILLEGAL_TRANSITION)  # a draft step
        other = self.apply_lease("b")
        cases = ((["../x"], errors.CONFINEMENT), (["/abs"], errors.CONFINEMENT), ([".shit/store"], errors.CONFINEMENT),
                 (["a//b"], errors.CONFINEMENT), (["./a"], errors.CONFINEMENT), ([""], errors.REJECTED),
                 ([], errors.REJECTED), (["x", "x"], errors.REJECTED), ("x", errors.REJECTED), ([5], errors.REJECTED),
                 (["bad" + chr(0)], errors.REJECTED), (["a" * 501], errors.REJECTED),
                 ([f"f{n}" for n in range(40)], errors.REJECTED))
        for bad, expected in cases:
            with self.subTest(targets=bad):
                self.assertEqual(code_of(store.queue_apply_start, self.conn, self.present(other), bad, self.tick()), expected)
        self.assertEqual(self.count("apply_slots"), 0)
        self.consistent()

    def test_an_apply_claim_waits_for_its_apply_prerequisite(self):
        pre = self.apply_lease("pre")
        self.task("dep", enqueue=False)
        store.add_edge(self.conn, "dep", "pre", "apply")
        store.queue_enqueue(self.conn, "dep", "human", self.tick())
        self.assertEqual(code_of(self.claim, "dep", step="apply"), errors.ILLEGAL_TRANSITION)
        store.queue_apply_start(self.conn, self.present(pre), ["p"], self.tick())
        self.assertEqual(code_of(self.claim, "dep", step="apply"), errors.ILLEGAL_TRANSITION)
        store.queue_apply_finish(self.conn, self.present(pre), self.tick())
        lease = self.claim("dep", step="apply")
        self.assertEqual(lease["step"], "apply")
        self.consistent()


class GraphEntryTests(Base):
    def test_an_edge_change_writes_graph_changed_with_the_shard_digest(self):
        self.task("a", enqueue=False)
        self.task("b", enqueue=False)
        self.task("c", enqueue=False)
        additions = [("b", "a", "draft"), ("b", "c", "review"), ("c", "a", "apply")]
        store.queue_add_edges(self.conn, additions, "human", self.tick())
        entries = [e for e in store.queue_entries(self.conn) if e["kind"] == "graph_changed"]
        self.assertEqual(len(entries), 1)
        self.assertEqual([item["task_id"] for item in entries[0]["shards"]], ["b", "c"])
        needs = [{"needs_id": "a", "type": "draft"}, {"needs_id": "c", "type": "review"}]
        expected = hashlib.sha256(dependencies.dump_shard("b", needs)).hexdigest()
        self.assertEqual(entries[0]["shards"][0]["sha256"], expected)
        self.assertEqual(len(store.list_edges(self.conn)), 3)
        self.consistent()
        store.queue_remove_edge(self.conn, "c", "a", "apply", "wrapper", self.tick())
        last = [e for e in store.queue_entries(self.conn) if e["kind"] == "graph_changed"][-1]
        self.assertEqual(last["shards"], [{"task_id": "c", "sha256": queue_records.ABSENT}])
        self.consistent()

    def test_refusals_leave_nothing_behind(self):
        self.task("a", enqueue=False)
        self.task("b", enqueue=False)
        self.assertEqual(code_of(store.queue_add_edges, self.conn, [("a", "b", "draft")], "model", self.tick()),
                         errors.HUMAN_APPROVAL)
        self.assertEqual(code_of(store.queue_add_edges, self.conn, [("a", "ghost", "draft")], "human", self.tick()),
                         errors.PRECONDITION)
        self.assertEqual(code_of(store.queue_add_edges, self.conn, [("a", "a", "draft")], "human", self.tick()), errors.REJECTED)
        self.assertEqual(code_of(store.queue_add_edges, self.conn, "nope", "human", self.tick()), errors.REJECTED)
        store.queue_add_edges(self.conn, [("a", "b", "draft")], "human", self.tick())
        before = (self.count("edges"), self.count("queue_entries"))
        self.assertEqual(code_of(store.queue_add_edges, self.conn, [("b", "a", "draft")], "human", self.tick()), errors.REJECTED)
        self.assertEqual(code_of(store.queue_add_edges, self.conn, [("a", "b", "draft")], "human", self.tick()), errors.REJECTED)
        self.assertEqual(code_of(store.queue_remove_edge, self.conn, "a", "b", "apply", "human", self.tick()), errors.PRECONDITION)
        self.assertEqual(code_of(store.queue_remove_edge, self.conn, "a", "b", "draft", "model", self.tick()),
                         errors.HUMAN_APPROVAL)
        self.assertEqual((self.count("edges"), self.count("queue_entries")), before)
        self.consistent()

    def test_the_clock_is_checked_before_the_shape_of_an_edge_to_remove(self):
        # `queue.remove_edge` checks the clock and applies maintenance first (found by the differential test).
        self.task("a", enqueue=False)
        self.task("b", enqueue=False)
        self.assertEqual(code_of(store.queue_remove_edge, self.conn, "a", "b", "bogus", "human", at(0)), errors.FAILED_CLOSED)
        self.assertEqual(code_of(store.queue_remove_edge, self.conn, "a", "b", "bogus", "human", self.tick()), errors.REJECTED)
        self.consistent()

    def test_an_edge_cannot_be_removed_from_a_task_with_a_live_lease(self):
        self.task("a", enqueue=False)
        self.task("b")
        store.queue_add_edges(self.conn, [("b", "a", "apply")], "human", self.tick())
        self.claim("b")
        self.assertEqual(code_of(store.queue_remove_edge, self.conn, "b", "a", "apply", "human", self.tick()),
                         errors.ILLEGAL_TRANSITION)
        self.assertEqual(self.count("edges"), 1)
        self.consistent()


class PopulationTests(Base):
    def test_queued_tasks_and_shard_owners_together_stay_within_the_cap(self):
        with mock.patch.object(dependencies, "MAX_TASKS", 3):
            for name in ("a", "b", "c", "d"):
                self.task(name, enqueue=False)
            store.queue_enqueue(self.conn, "a", "human", self.tick())
            store.queue_add_edges(self.conn, [("b", "a", "draft")], "human", self.tick())  # owners: b; queued: a
            store.queue_enqueue(self.conn, "c", "human", self.tick())  # a, b, c
            before = self.count("queue_entries")
            self.assertEqual(code_of(store.queue_enqueue, self.conn, "d", "human", self.tick()), errors.INPUT_LIMIT)
            self.assertEqual(code_of(store.queue_add_edges, self.conn, [("d", "a", "draft")], "human", self.tick()),
                             errors.INPUT_LIMIT)
            self.assertEqual(self.count("queue_entries"), before)
            self.assertEqual(self.count("edges"), 1)
            self.consistent()


class QueueLiveStateTests(Base):
    """A queued task changes lifecycle state only through its queue operation (docs/store.md, C4)."""

    def test_the_queue_state_is_read_inside_the_transaction_that_writes_the_task(self):
        self.task("a")
        lease = self.claim("a")
        recorder = Recorder(self.conn)
        self.assertEqual(code_of(store.transition_task, recorder, "a", "failed", "wrapper", "x", self.tick()),
                         errors.ILLEGAL_TRANSITION)
        self.assertEqual(recorder.sql[0], "BEGIN IMMEDIATE")
        self.assertTrue(any("FROM queue_entries" in sql for sql in recorder.sql[1:]))
        self.assertEqual(recorder.sql[-1], "ROLLBACK")
        self.assertEqual(self.lifecycle("a"), "open")
        self.assertEqual(self.state("a")["lease"]["lease_id"], lease["lease_id"])
        self.consistent()

    def test_transition_task_refuses_a_change_the_queue_state_does_not_allow(self):
        self.task("a")
        lease = self.claim("a")
        for to in ("running", "blocked", "failed"):
            self.assertEqual(code_of(store.transition_task, self.conn, "a", to, "wrapper", "x", self.tick()),
                             errors.ILLEGAL_TRANSITION, to)
        store.queue_start(self.conn, self.present(lease), self.tick())
        self.assertEqual(code_of(store.transition_task, self.conn, "a", "review", "wrapper", "x", self.tick()),
                         errors.ILLEGAL_TRANSITION)
        self.assertEqual((self.state("a")["state"], self.lifecycle("a")), ("running", "running"))
        self.consistent()

    def test_an_unqueued_task_and_the_review_approval_still_move_freely(self):
        self.task("loose", enqueue=False)
        store.transition_task(self.conn, "loose", "blocked", "human", "x", self.tick(), blocked_reason="r",
                              blocking_item="i")
        self.task("a")
        lease = self.claim("a")
        store.queue_start(self.conn, self.present(lease), self.tick())
        store.queue_submit(self.conn, self.present(lease), self.tick())
        store.transition_task(self.conn, "a", "approved", "human", "ok", self.tick())
        self.assertEqual(code_of(store.transition_task, self.conn, "a", "closed", "human", "x", self.tick()),
                         errors.ILLEGAL_TRANSITION)  # closing a queued task is `queue_complete`
        self.assertEqual(code_of(store.transition_task, self.conn, "a", "open", "human", "x", self.tick()),
                         errors.ILLEGAL_TRANSITION)
        self.consistent()


if __name__ == "__main__":
    unittest.main()
