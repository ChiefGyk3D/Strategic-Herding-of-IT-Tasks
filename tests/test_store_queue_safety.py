"""Safety properties of the queue operations (issue 61, C4): one transaction, write order, ambiguous commits, crashes,
races. Real database files; a crash is a child process that kills itself with SIGKILL at a numbered stage.

A scenario is plain data: the name of an operation, its arguments after the connection, and its keyword arguments.
The parent prepares the state, so a child process (which cannot share a closure) can run the same call.
"""

import json
import os
import signal
import subprocess
import sys
import time
import unittest

from _bootstrap import ROOT
from test_store_queue import Base, Recorder, at, code_of

from shit import errors, store
from shit.errors import ShitError


class Faulty:
    """A real connection whose COMMIT reports an error: after the commit landed, or after it was rolled back."""

    def __init__(self, real, commit):
        self.real, self.commit = real, commit

    @property
    def in_transaction(self):
        return self.real.in_transaction

    def execute(self, sql, *args):
        if sql == "COMMIT":
            exc = sqlite3_error()
            if self.commit == "landed":
                self.real.execute("COMMIT")
            else:
                self.real.execute("ROLLBACK")
            raise exc
        return self.real.execute(sql, *args)

    def close(self):
        self.real.close()


def sqlite3_error():
    import sqlite3

    exc = sqlite3.OperationalError("disk I/O error")
    exc.sqlite_errorcode = 10
    return exc


class Scenarios(Base):
    """Builds one prepared call per operation, each on its own tasks."""

    def present_of(self, task_id):
        row = self.conn.execute("SELECT lease_id, generation FROM leases WHERE task_id = ?", (task_id,)).fetchone()
        return {"task_id": task_id, "lease_id": row[0], "generation": row[1]}

    def prepared(self, key_prefix=None):
        """[(label, operation name, args, kwargs)] with the state each needs already in place."""
        out = []

        def add(label, op, args, **kwargs):
            if key_prefix is not None:
                kwargs["key"] = key_prefix + "-" + label
            out.append((label, op, args, kwargs))

        self.task("s-enq", enqueue=False)
        self.task("s-claim")
        self.task("s-start")
        self.claim("s-start")
        self.task("s-renew")
        self.running("s-renew")
        self.task("s-release")
        self.claim("s-release")
        self.task("s-submit")
        self.running("s-submit")
        self.task("s-fail")
        self.running("s-fail")
        self.task("s-hold")
        self.running("s-hold")
        self.task("s-reopen")
        lease = self.running("s-reopen")
        store.queue_fail(self.conn, self.present(lease), self.tick())
        self.task("s-complete")
        lease = self.running("s-complete")
        store.queue_submit(self.conn, self.present(lease), self.tick())
        store.transition_task(self.conn, "s-complete", "approved", "human", "ok", self.tick())
        self.task("s-reclaim")
        self.running("s-reclaim")
        self.task("s-apply-start")
        self.running("s-apply-start", step="apply")
        self.task("s-apply-finish")
        self.running("s-apply-finish", step="apply")
        store.queue_apply_start(self.conn, self.present_of("s-apply-finish"), ["pre/x"], self.tick())
        self.task("g-a", enqueue=False)
        self.task("g-b", enqueue=False)
        self.task("g-c", enqueue=False)
        store.queue_add_edges(self.conn, [("g-c", "g-a", "draft")], "human", self.tick())

        add("enqueue", "queue_enqueue", ["s-enq", "human", self.tick()])
        add("claim", "queue_claim", [{"task_id": "s-claim", "step": "draft", "worker": "w1", "worker_kind": "ai"},
                                     self.tick()])
        add("start", "queue_start", [self.present_of("s-start"), self.tick()])
        add("renew", "queue_renew", [self.present_of("s-renew"), self.tick()])
        add("release", "queue_release", [self.present_of("s-release"), self.tick()])
        add("submit", "queue_submit", [self.present_of("s-submit"), self.tick()])
        add("fail", "queue_fail", [self.present_of("s-fail"), self.tick()])
        add("hold", "queue_hold", [self.present_of("s-hold"), "owner", "Reason.", "item-1", self.tick()])
        add("reopen", "queue_reopen", ["s-reopen", "human", "Again.", self.tick()])
        add("complete", "queue_complete", ["s-complete", "human", "Done.", self.tick()])
        add("reclaim", "queue_reclaim", ["s-reclaim", "human", self.tick()])
        add("apply-start", "queue_apply_start", [self.present_of("s-apply-start"), ["t/a", "t/b"], self.tick()])
        add("apply-finish", "queue_apply_finish", [self.present_of("s-apply-finish"), self.tick()])
        add("add-edges", "queue_add_edges", [[("g-b", "g-a", "review")], "human", self.tick()])
        add("remove-edge", "queue_remove_edge", ["g-c", "g-a", "draft", "human", self.tick()])
        return out


def snapshot(conn):
    """Every row of every table, in a fixed order."""
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                                "ORDER BY name").fetchall():
        out[name] = conn.execute("SELECT * FROM " + name + " ORDER BY 1, 2").fetchall()
    return out


class OneTransactionTests(Scenarios):
    def test_every_operation_is_exactly_one_write_transaction(self):
        prepared = self.prepared()
        self.assertGreaterEqual(len(prepared), 15)
        for label, op, args, kwargs in prepared:
            with self.subTest(op=label):
                recorder = Recorder(self.conn)
                getattr(store, op)(recorder, *args, **kwargs)
                begins = [s for s in recorder.sql if s.upper().startswith("BEGIN")]
                self.assertEqual(begins, ["BEGIN IMMEDIATE"])
                self.assertEqual([s for s in recorder.sql if s in ("COMMIT", "ROLLBACK")], ["COMMIT"])
                self.assertEqual(recorder.sql[0], "BEGIN IMMEDIATE")
                self.assertEqual(recorder.sql[-1], "COMMIT")
                self.consistent()

    def test_a_refused_operation_rolls_everything_back_including_maintenance(self):
        self.task("a")
        self.task("b")
        stale = self.running("a")
        before = snapshot(self.conn)
        later = at(self.clock + 905)
        # `a` is past its expiry, so the claim would expire it first; the claim itself is refused: nothing is kept.
        request = {"task_id": "ghost", "step": "draft", "worker": "w", "worker_kind": "ai"}
        self.assertEqual(code_of(store.queue_claim, self.conn, request, later), errors.PRECONDITION)
        self.assertEqual(snapshot(self.conn), before)
        self.assertEqual(self.state("a")["lease"]["lease_id"], stale["lease_id"])
        self.consistent()


class WriteOrderTests(Scenarios):
    @staticmethod
    def writes(sql):
        out = []
        for statement in sql:
            words = statement.split()
            if words[0] == "INSERT":
                out.append(("INSERT", words[2]))
            elif words[0] == "UPDATE":
                out.append(("UPDATE", words[1]))
            elif words[0] == "DELETE":
                out.append(("DELETE", words[2]))
        return out

    cases = None

    def run_op(self, label):
        if self.cases is None:
            self.cases = self.prepared()
        for name, op, args, kwargs in self.cases:
            if name == label:
                recorder = Recorder(self.conn)
                getattr(store, op)(recorder, *args, **kwargs)
                self.consistent()
                return self.writes(recorder.sql)
        raise AssertionError(label)

    def test_a_claim_writes_the_entry_before_the_lease_row(self):
        self.assertEqual(self.run_op("claim"), [("INSERT", "queue_entries"), ("INSERT", "leases")])

    def test_a_renewal_writes_the_entry_and_updates_the_row_in_the_same_transaction(self):
        self.assertEqual(self.run_op("renew"), [("INSERT", "queue_entries"), ("UPDATE", "leases")])

    def test_the_lease_ends_after_its_slots_and_after_the_entry(self):
        order = self.run_op("submit")
        self.assertEqual(order[0], ("INSERT", "queue_entries"))
        self.assertEqual(order[-2:], [("DELETE", "apply_slots"), ("DELETE", "leases")])
        self.assertIn(("UPDATE", "tasks"), order)  # the task row's lifecycle state in the same transaction

    def test_an_apply_slot_is_taken_after_the_marker_and_released_by_the_finish(self):
        self.assertEqual(self.run_op("apply-start"),
                         [("INSERT", "queue_entries"), ("INSERT", "apply_slots"), ("INSERT", "apply_slots")])
        self.assertEqual(self.run_op("apply-finish"), [("INSERT", "queue_entries"), ("DELETE", "apply_slots")])

    def test_the_task_row_changes_with_the_queue_entry(self):
        order = self.run_op("start")
        self.assertEqual(order, [("INSERT", "queue_entries"), ("UPDATE", "tasks"), ("INSERT", "task_history"),
                                 ("INSERT", "events")])
        order = self.run_op("complete")
        self.assertEqual(order[0], ("INSERT", "queue_entries"))
        self.assertIn(("UPDATE", "tasks"), order)

    def test_a_graph_change_writes_its_edges_and_its_entry_together(self):
        self.assertEqual(self.run_op("add-edges"), [("INSERT", "edges"), ("INSERT", "queue_entries")])
        self.assertEqual(self.run_op("remove-edge"), [("DELETE", "edges"), ("INSERT", "queue_entries")])


class InTransactionHelperTests(Base):
    def test_the_shared_writers_refuse_to_run_outside_a_transaction(self):
        self.task("a")
        lease = {"task_id": "a", "lease_id": "a" * 32, "generation": 1, "step": "draft", "worker": "w",
                 "worker_kind": "ai", "claimed_at": at(1), "expires_at": at(60), "renewals": 0}
        calls = (
            lambda: store._append_entry(self.conn, {"task_id": "a", "actor": "wrapper", "kind": "ready",
                                                    "state_after": "ready"}, at(500)),
            lambda: store._lifecycle_step(self.conn, "a", "open", "running", "wrapper", "x", at(500)),
            lambda: store._insert_lease(self.conn, lease),
            lambda: store._renew_lease_row(self.conn, "a", at(70), 1),
            lambda: store._drop_lease(self.conn, "a"),
            lambda: store._take_slot(self.conn, "x", lease, at(2)),
            lambda: store._release_slots(self.conn, "a"),
            lambda: store._expire(self.conn, "a", at(500)),
            lambda: store._maintain(self.conn, at(500), None),
            lambda: store._begin(self.conn, at(500), None),
        )
        before = snapshot(self.conn)
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(ShitError) as caught:
                    call()
                self.assertEqual(caught.exception.code, errors.INTERNAL)
        self.assertEqual(snapshot(self.conn), before)
        self.consistent()


class AmbiguousCommitTests(Scenarios):
    """After `AmbiguousCommit` the caller opens a new connection and repeats the call with the same key: the call
    either finds its entry (the commit landed) and returns the same answer, or writes it once (it did not)."""

    def entries_with(self, conn, key):
        return [e for e in store.queue_entries(conn) if e["message"].endswith("key " + key + ".")]

    def test_a_commit_that_landed_is_found_by_its_key_and_not_repeated(self):
        for label, op, args, kwargs in self.prepared(key_prefix="amb"):
            with self.subTest(op=label):
                key = kwargs["key"]
                faulty = Faulty(self.connect(), "landed")
                with self.assertRaises(store.AmbiguousCommit):
                    getattr(store, op)(faulty, *args, **kwargs)
                fresh = self.connect()
                found = self.entries_with(fresh, key)
                self.assertEqual(len(found), 1)
                count = fresh.execute("SELECT count(*) FROM queue_entries").fetchone()[0]
                first = getattr(store, op)(fresh, *args, **kwargs)  # the repeat
                self.assertEqual(fresh.execute("SELECT count(*) FROM queue_entries").fetchone()[0], count)
                self.assertEqual(len(self.entries_with(fresh, key)), 1)
                if label in ("claim", "renew"):
                    self.assertEqual(first["lease_id"], found[0].get("lease_id"))
                self.assertEqual(store.queue_entry_for_key(fresh, key)["id"], found[0]["id"])
                self.assertEqual(store.verify(fresh), [])
                self.assertEqual(store.verify_tasks(fresh), [])

    def test_a_commit_that_did_not_land_is_written_once_by_the_repeat(self):
        for label, op, args, kwargs in self.prepared(key_prefix="lost"):
            with self.subTest(op=label):
                key = kwargs["key"]
                faulty = Faulty(self.connect(), "lost")
                with self.assertRaises(store.AmbiguousCommit):
                    getattr(store, op)(faulty, *args, **kwargs)
                fresh = self.connect()
                self.assertEqual(self.entries_with(fresh, key), [])
                self.assertIsNone(store.queue_entry_for_key(fresh, key))
                getattr(store, op)(fresh, *args, **kwargs)
                self.assertEqual(len(self.entries_with(fresh, key)), 1)
                self.assertEqual(store.verify(fresh), [])
                self.assertEqual(store.verify_tasks(fresh), [])

    def test_a_repeat_without_the_key_is_not_idempotent_and_says_so(self):
        self.task("a")
        request = {"task_id": "a", "step": "draft", "worker": "w1", "worker_kind": "ai"}
        faulty = Faulty(self.connect(), "landed")
        with self.assertRaises(store.AmbiguousCommit):
            store.queue_claim(faulty, request, self.tick())
        fresh = self.connect()
        self.assertEqual(code_of(store.queue_claim, fresh, request, self.tick()), errors.ILLEGAL_TRANSITION)

    def test_a_key_names_one_entry_and_cannot_be_reused_for_another(self):
        self.task("a", enqueue=False)
        self.task("b", enqueue=False)
        store.queue_enqueue(self.conn, "b", "human", self.tick(), key="one")
        self.assertEqual(code_of(store.queue_enqueue, self.conn, "a", "human", self.tick(), key="one"), errors.REJECTED)
        request = {"task_id": "b", "step": "draft", "worker": "w1", "worker_kind": "ai"}
        self.assertEqual(code_of(store.queue_claim, self.conn, request, self.tick(), key="one"), errors.REJECTED)
        self.assertEqual(code_of(store.queue_entry_for_key, self.conn, None), errors.USAGE)
        self.consistent()

    def test_a_busy_commit_that_stays_in_its_transaction_is_not_ambiguous(self):
        import sqlite3

        class Busy(Faulty):
            def execute(self, sql, *args):
                if sql == "COMMIT":
                    exc = sqlite3.OperationalError("database is locked")
                    exc.sqlite_errorcode = 5
                    raise exc
                return self.real.execute(sql, *args)

        self.task("a", enqueue=False)
        busy = Busy(self.connect(), "x")
        self.assertEqual(code_of(store.queue_enqueue, busy, "a", "human", self.tick(), key="k"), errors.BUSY)
        self.assertEqual(self.state("a"), None)
        store.queue_enqueue(busy.real, "a", "human", self.tick(), key="k")
        self.assertEqual(self.state("a")["state"], "ready")


CHILD = """
import json, os, signal, sys
sys.path.insert(0, sys.argv[1])
from shit import store
from shit.project import Project
proj = Project.open(sys.argv[2])
conn = store.connect(proj)
kill_at = int(sys.argv[3])
op, args, kwargs = json.loads(sys.argv[4])
seen = []
def fault(stage):
    seen.append(stage)
    if len(seen) - 1 == kill_at:
        os.kill(os.getpid(), signal.SIGKILL)
store._fault = fault
if op == "queue_add_edges":
    args[0] = [tuple(edge) for edge in args[0]]
getattr(store, op)(conn, *args, **kwargs)
print(json.dumps(seen))
"""


class CrashTests(Scenarios):
    def run_child(self, op, args, kwargs, kill_at):
        payload = json.dumps([op, args, kwargs])
        return subprocess.run([sys.executable, "-I", "-c", CHILD, str(ROOT), str(self.repo), str(kill_at), payload],
                              capture_output=True, timeout=120, check=False)

    def test_a_kill_at_every_stage_of_every_operation_leaves_nothing_half_written(self):
        stages_seen = {}
        for label, op, args, kwargs in self.prepared():
            before = snapshot(self.conn)
            kill_at = 0
            while True:
                proc = self.run_child(op, args, kwargs, kill_at)
                after = snapshot(self.connect())
                if proc.returncode == 0:
                    stages_seen[label] = json.loads(proc.stdout.decode())
                    break
                self.assertEqual(proc.returncode, -signal.SIGKILL, (label, kill_at, proc.stderr[-300:]))
                self.assertEqual(after, before, (label, kill_at))
                survivor = self.connect()
                self.assertEqual(store.verify(survivor), [], (label, kill_at))
                self.assertEqual(store.verify_tasks(survivor), [], (label, kill_at))
                self.assertFalse(survivor.in_transaction)
                kill_at += 1
                self.assertLess(kill_at, 40)
            self.assertGreaterEqual(kill_at, 1, label)  # something was killed: every operation has a stage
            survivor = self.connect()
            self.consistent(survivor)
            # The completed run committed; the operation really happened.
            self.assertNotEqual(snapshot(survivor), before, label)
        self.assertEqual(set(stages_seen), {label for label, *_ in self.prepared_labels()})
        # A claim is killed after its entry and before its lease row, among others.
        self.assertEqual(stages_seen["claim"][:3], ["checked", "entry", "lease-row"])
        self.assertIn("lease-updated", stages_seen["renew"])
        self.assertIn("slots-dropped", stages_seen["submit"])

    def prepared_labels(self):
        labels = ("enqueue", "claim", "start", "renew", "release", "submit", "fail", "hold", "reopen", "complete",
                  "reclaim", "apply-start", "apply-finish", "add-edges", "remove-edge")
        return [(label,) for label in labels]

    def test_the_next_write_is_accepted_after_a_kill_inside_a_claim(self):
        self.task("a")
        request = {"task_id": "a", "step": "draft", "worker": "w1", "worker_kind": "ai"}
        proc = self.run_child("queue_claim", [request, self.tick()], {}, 1)  # after the entry, before the lease row
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        survivor = self.connect()
        self.assertEqual(store.queue_state(survivor, "a")["state"], "ready")
        lease = store.queue_claim(survivor, request, self.tick())
        self.assertEqual(lease["generation"], 1)
        self.assertEqual(store.verify(survivor), [])


RACE_CHILD = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
from shit import store
from shit.errors import ShitError
from shit.project import Project
proj = Project.open(sys.argv[2])
conn = store.connect(proj)
start, task, worker, now = float(sys.argv[3]), sys.argv[4], sys.argv[5], sys.argv[6]
request = {"task_id": task, "step": "draft", "worker": worker, "worker_kind": "ai"}
while time.time() < start:
    time.sleep(0.001)
try:
    lease = store.queue_claim(conn, request, now)
    print(json.dumps({"ok": lease["lease_id"]}))
except ShitError as exc:
    print(json.dumps({"code": exc.code}))
"""


class RaceTests(Base):
    def test_two_processes_claiming_one_task_have_exactly_one_winner(self):
        trials = int(os.environ.get("SHIT_RACE_TRIALS", "6"))
        for trial in range(trials):
            name = "race-" + str(trial)
            self.task(name)
            now = self.tick()
            start = time.time() + 1.5
            procs = [subprocess.Popen([sys.executable, "-I", "-c", RACE_CHILD, str(ROOT), str(self.repo), str(start),
                                       name, "w" + str(n), now], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for n in range(2)]
            results = []
            for proc in procs:
                out, err = proc.communicate(timeout=120)
                self.assertEqual(proc.returncode, 0, err[-300:])
                results.append(json.loads(out.decode()))
            winners = [r for r in results if "ok" in r]
            losers = [r for r in results if "code" in r]
            self.assertEqual((len(winners), len(losers)), (1, 1), results)
            self.assertEqual(losers[0]["code"], errors.ILLEGAL_TRANSITION)  # one live lease per task
            self.assertEqual(self.state(name)["lease"]["lease_id"], winners[0]["ok"])
            self.assertEqual(self.count("leases"), trial + 1)
            self.consistent()


if __name__ == "__main__":
    unittest.main()
