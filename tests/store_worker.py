"""One worker process of the store harness (issue 65, C8). Not a test: `tests/store_harness.py` starts it.

It runs as `python -I tests/store_worker.py <json spec>`. The spec names the package root to import `shit` from, so a
mutated copy of the package can be exercised with the same worker. This file imports nothing from the tests (the
tests' bootstrap would put the real repository back on the path).

The worker performs random queue operations against one database until it has done `ops` of them or the deadline
passes. It writes one JSON line per notable event to its journal (an `O_APPEND` descriptor, so the lines survive a
SIGKILL): every successful claim, every refusal with a code a healthy run never gives (1, 13), every exception that
is not a `ShitError`, and every inconsistency a snapshot check finds. At the end it prints a summary line.

Time is a logical clock shared by every worker: real elapsed time since the run's origin, multiplied by a scale, added
to a fixed base. A scale of 30 makes the shortest lease (60 seconds) last two real seconds.
"""

import hashlib
import json
import os
import random
import signal
import sys
import time
from datetime import UTC, datetime, timedelta

BASE = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC)
CONFIG = {"lease_ttl_seconds": 60, "lease_max_seconds": 180, "lease_max_renewals": 2, "max_attempts": 3}
STEPS = ("draft", "draft", "apply", "review")
TARGETS = ("tg/a", "tg/b", "tg/c")
# Operations a task in each queue state invites; a minority of calls ignore this and are refused.
SUITABLE = {
    None: ("enqueue", "new_task"),
    "waiting": ("claim", "maintain"),
    "ready": ("claim", "claim", "claim", "maintain"),
    "dispatched": ("start", "start", "start", "renew", "release", "hold", "fail", "reclaim"),
    "running": ("renew", "submit", "submit", "fail", "hold", "reclaim", "apply_start", "apply_finish"),
    "review": ("approve", "complete", "complete", "reopen"),
    "hold-owner": ("reopen",), "hold-ci": ("reopen",),
    "failed": ("reopen", "reopen", "approve"),
    "done": ("claim", "reopen"),
}
EVERY = ("enqueue", "claim", "start", "renew", "release", "submit", "fail", "hold", "reopen", "approve", "complete",
         "reclaim", "maintain", "apply_start", "apply_finish", "add_edge", "remove_edge", "new_task")
# Codes a healthy run never gives: another SQLite error (1) and a store that stayed busy past its timeout (13).
SUSPICIOUS = (1, 13)
# An exception that is not a `ShitError` is not caught: the worker dies with its traceback and the parent reports it.


def stamp(seconds):
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Worker:
    def __init__(self, spec):
        self.spec = spec
        sys.path.insert(0, spec["root"])
        from shit import queue_records, records, store
        from shit.errors import ShitError
        from shit.project import Project

        self.store, self.records, self.ShitError = store, records, ShitError
        self.queue_records = queue_records
        self.project = Project.open(spec["repo"])
        self.rng = random.Random(spec["seed"])
        self.name = "w" + str(spec["index"])
        self.journal = os.open(spec["journal"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self.counts = {}
        self.created = 0
        self.conn = None
        self.reconnect()

    def reconnect(self):
        self.conn = self.store.connect(self.project, busy_timeout_ms=self.spec["busy_ms"])

    def note(self, **fields):
        os.write(self.journal, (json.dumps({"worker": self.name, **fields}) + "\n").encode())

    def clock(self):
        elapsed = time.time() - self.spec["epoch"]
        return stamp(self.spec["clock_base"] + int(elapsed * self.spec["scale"]))

    # -- choosing an operation -----------------------------------------------------------------------------

    def pick_task(self):
        tasks = self.spec["tasks"] + [name for name in self.known if name.startswith("w")]
        return self.rng.choice(tasks)

    def present(self, task):
        queued = self.store.queue_state(self.conn, task)
        lease = None if queued is None else queued["lease"]
        if lease is None or self.rng.random() < 0.12:
            lease_id = "".join(self.rng.choice("0123456789abcdef") for _ in range(32))
            return {"task_id": task, "lease_id": lease_id, "generation": self.rng.choice((1, 2, 3))}
        generation = lease["generation"] + (self.rng.choice((-1, 1)) if self.rng.random() < 0.08 else 0)
        return {"task_id": task, "lease_id": lease["lease_id"], "generation": generation}

    def choose(self, task):
        queued = self.store.queue_state(self.conn, task)
        if self.rng.random() < 0.8:
            return self.rng.choice(SUITABLE[queued["state"] if queued else None])
        return self.rng.choice(EVERY)

    def new_task_id(self):
        self.created += 1
        return "{}-{}-{}".format(self.name, self.spec["round"], self.created)

    def create_task(self, now):
        task_id = self.new_task_id()
        task = {"schema": "task", "schema_version": 1, "id": task_id, "title": "Title", "summary": "Summary",
                "state": "open", "owner": "human", "created_at": now, "updated_at": now, "history": []}
        data = self.records.dump_task(task)
        self.store.create_task(self.conn, task, "human", hashlib.sha256(data).hexdigest(), len(data))
        self.known.append(task_id)
        self.store.queue_enqueue(self.conn, task_id, "human", now, config=CONFIG)

    def call(self, kind, task, now):
        s, c, rng = self.store, self.conn, self.rng
        if kind == "enqueue":
            return s.queue_enqueue(c, task, rng.choice(("human", "human", "wrapper")), now, config=CONFIG)
        if kind == "claim":
            request = {"task_id": task, "step": rng.choice(STEPS), "worker": self.name, "worker_kind": "ai"}
            lease = s.queue_claim(c, request, now, config=CONFIG)
            self.note(kind="claim", task=task, lease_id=lease["lease_id"], generation=lease["generation"])
            return lease
        if kind in ("start", "renew", "release", "submit", "fail", "apply_finish"):
            return getattr(s, "queue_" + kind)(c, self.present(task), now, config=CONFIG)
        if kind == "hold":
            return s.queue_hold(c, self.present(task), rng.choice(("owner", "ci")), "Waiting.", "item-1", now,
                                config=CONFIG)
        if kind == "apply_start":
            targets = rng.sample(TARGETS, rng.choice((1, 2)))
            return s.queue_apply_start(c, self.present(task), targets, now, config=CONFIG)
        if kind == "reopen":
            return s.queue_reopen(c, task, "human", "Again.", now, config=CONFIG)
        if kind == "approve":
            return s.transition_task(c, task, "approved", "human", "ok", now)
        if kind == "complete":
            return s.queue_complete(c, task, "human", "Done.", now, config=CONFIG)
        if kind == "reclaim":
            return s.queue_reclaim(c, task, "human", now, config=CONFIG)
        if kind == "maintain":
            return s.queue_maintain(c, now, config=CONFIG)
        if kind == "add_edge":
            other = self.pick_task()
            return s.queue_add_edges(c, [(task, other, "apply")], "human", now, config=CONFIG)
        if kind == "remove_edge":
            other = self.pick_task()
            return s.queue_remove_edge(c, task, other, "apply", "human", now, config=CONFIG)
        if kind == "new_task":
            return self.create_task(now)
        raise AssertionError(kind)

    # -- one step --------------------------------------------------------------------------------------------

    def arm_kill(self):
        """Kill this process at the `stage`-th fault point of the next operation (a stage a helper reaches)."""
        seen = []
        target = self.rng.randrange(4)

        def fault(stage):
            seen.append(stage)
            if len(seen) - 1 == target:
                os.kill(os.getpid(), signal.SIGKILL)

        self.store._fault = fault

    def step(self, number, selfkill_at):
        task = self.pick_task()
        kind = self.choose(task)
        now = self.clock()
        if selfkill_at >= 0 and number >= selfkill_at:
            self.arm_kill()
        try:
            self.call(kind, task, now)
            result = "ok"
        except self.ShitError as exc:
            result = "code" + str(exc.code)
            if exc.code in SUSPICIOUS:
                self.note(kind="suspicious", op=kind, code=exc.code)
            if exc.code == 8:  # an ambiguous commit closes the connection; open a new one
                self.reconnect()
        finally:
            self.store._fault = lambda stage: None
        key = kind + ":" + result
        self.counts[key] = self.counts.get(key, 0) + 1

    def check(self):
        """Every cross-table invariant, on one consistent snapshot (so a concurrent writer cannot fake a failure)."""
        with self.store.read_snapshot(self.conn):
            found = self.store.verify(self.conn) + self.store.verify_tasks(self.conn)
        if found:
            self.note(kind="violation", found=found)

    def run(self):
        self.known = []
        ops, every = self.spec["ops"], self.spec["check_every"]
        selfkill_at = self.rng.randrange(min(ops, 40)) if self.spec["selfkill"] else -1
        for number in range(ops):
            if time.time() >= self.spec["deadline"]:
                break
            self.step(number, selfkill_at)
            if every and number % every == 0:
                self.check()
        if every:
            self.check()
        self.conn.close()
        print(json.dumps({"worker": self.name, "counts": self.counts}))


if __name__ == "__main__":
    Worker(json.loads(sys.argv[1])).run()
