"""A pure model of the queue lifecycle and a generator of operation sequences (issue 65, C8).

The model is a small state machine written from the queue rules (docs/queue-spec.md), not from `shit/store.py`: for
every operation it says whether the store must accept it, which refusal code it must give, and what state every task
must be in afterwards. `run_sequence` applies one generated sequence to a real database and compares the two after
every operation. At the end the model must also agree with `queue_records.replay` of the log the database holds.

Lazy expiry is part of the model, because it is part of the rules: every operation first expires the leases that are
due, but a refused operation rolls its whole transaction back, so that expiry is kept only when the operation is
accepted.

Run as a script (`python -I tests/store_model.py '{"root": ..., "seed": ..., "length": ...}'`) it imports `shit` from
the given root, so a mutated copy of the package can be checked. It prints the list of disagreements as JSON.
"""

import hashlib
import json
import random
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC)
CONFIG = {"lease_ttl_seconds": 60, "lease_max_seconds": 180, "lease_max_renewals": 2, "max_attempts": 3}
TASKS = ("m0", "m1", "m2", "m3", "m4")
STEPS = ("draft", "apply", "apply", "review")
# What a task in each queue state invites; the rest of the calls are refusals the model must predict too.
SUITABLE = {
    None: ("enqueue", "enqueue"),
    "ready": ("claim", "claim", "claim"),
    "dispatched": ("start", "start", "renew", "release", "hold", "fail", "reclaim"),
    "running": ("renew", "submit", "submit", "fail", "hold", "reclaim", "apply_start", "apply_finish"),
    "review": ("approve", "complete", "complete", "reopen"),
    "hold-owner": ("reopen",), "hold-ci": ("reopen",),
    "failed": ("reopen", "reopen", "approve"),
    "done": ("claim", "reopen", "complete"),
}
EVERY = ("enqueue", "claim", "start", "renew", "release", "submit", "fail", "hold", "reopen", "approve", "complete",
         "reclaim", "maintain", "apply_start", "apply_finish")


def at(seconds):
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds_of(stamp):
    return int((datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) - BASE).total_seconds())


class Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Model:
    """The expected queue and lifecycle state of every task. Times are whole seconds from `BASE`."""

    def __init__(self, codes, config):
        self.codes, self.config = codes, config
        self.tasks = {}  # id -> state, once enqueued
        self.known = set()  # created tasks
        self.life = {}  # task id -> lifecycle state of the task row

    def create(self, task_id):
        self.known.add(task_id)
        self.life[task_id] = "open"

    # -- time --------------------------------------------------------------------------------------------------

    def maintained(self, now):
        """A copy of the task states after the expiry of every due lease (the transaction's first step)."""
        tasks = {name: dict(task, lease=None if task["lease"] is None else dict(task["lease"]))
                 for name, task in self.tasks.items()}
        life = dict(self.life)
        for name, task in tasks.items():
            lease = task["lease"]
            if lease is not None and now >= lease["expires"]:
                self.expire(task, life, name)
        return tasks, life

    @staticmethod
    def expire(task, life, name):
        if task["state"] == "running":
            task["state"], life[name] = "failed", "failed"
        else:
            task["state"] = "ready"
        task["lease"], task["phase"] = None, None

    # -- one operation -------------------------------------------------------------------------------------------

    def lease_check(self, task, presented):
        if task is None:
            raise Refused(self.codes["precondition"])
        lease = task["lease"]
        if lease is None or presented["lease_id"] != lease["id"] or presented["generation"] != lease["generation"]:
            raise Refused(self.codes["lease"])
        return lease

    def apply(self, kind, args, now):
        """Accept or raise `Refused`; on acceptance the model has taken the same step as the store. Returns a hint."""
        codes = self.codes
        if kind == "enqueue":
            if args["actor"] not in ("human", "wrapper"):
                raise Refused(codes["human"])
            if args["task"] not in self.known:
                raise Refused(codes["precondition"])
        if kind == "reclaim" and args["actor"] != "human":
            raise Refused(codes["human"])
        # `approve` is a plain lifecycle step (`transition_task`): it expires nothing.
        tasks, life = self.maintained(now if kind != "approve" else float("-inf"))
        name = args.get("task")
        task = tasks.get(name)
        if kind == "maintain":
            pass
        elif kind == "enqueue":
            if task is not None:
                raise Refused(codes["rejected"])
            tasks[name] = {"state": "ready", "generation": 0, "attempts": 0, "lease": None, "phase": None}
        elif kind == "claim":
            if task is None:
                raise Refused(codes["precondition"])
            if task["state"] != "ready":
                raise Refused(codes["illegal"])
            task["generation"] += 1
            task["state"] = "dispatched"
            task["phase"] = None
            task["lease"] = {"id": None, "generation": task["generation"], "claimed": now, "step": args["step"],
                             "expires": now + self.config["lease_ttl_seconds"], "renewals": 0}
        elif kind == "start":
            self.lease_check(task, args["presented"])
            if task["state"] != "dispatched":
                raise Refused(codes["illegal"])
            task["state"], life[name] = "running", "running"
            task["attempts"] += 1
        elif kind == "renew":
            lease = self.lease_check(task, args["presented"])
            longest = lease["claimed"] + self.config["lease_max_seconds"]
            expiry = min(lease["expires"] + self.config["lease_ttl_seconds"], longest)
            if lease["renewals"] + 1 > self.config["lease_max_renewals"] or expiry <= lease["expires"]:
                raise Refused(codes["lease"])
            lease["expires"], lease["renewals"] = expiry, lease["renewals"] + 1
        elif kind == "release":
            self.lease_check(task, args["presented"])
            if task["state"] != "dispatched":
                raise Refused(codes["illegal"])
            task["state"], task["lease"] = "ready", None
        elif kind == "submit":
            self.lease_check(task, args["presented"])
            if task["state"] != "running":
                raise Refused(codes["illegal"])
            task["state"], life[name], task["lease"], task["phase"] = "review", "review", None, None
        elif kind == "fail":
            self.lease_check(task, args["presented"])
            task["state"], life[name], task["lease"], task["phase"] = "failed", "failed", None, None
        elif kind == "hold":
            self.lease_check(task, args["presented"])
            task["state"], life[name] = "hold-" + args["hold_kind"], "blocked"
            task["lease"], task["phase"] = None, None
        elif kind == "reopen":
            if task is None:
                raise Refused(codes["precondition"])
            if task["state"] not in ("review", "failed", "hold-owner", "hold-ci"):
                raise Refused(codes["illegal"])
            if args["actor"] != "human":
                raise Refused(codes["illegal"])  # the lifecycle matrix refuses a non-human reopen
            if task["state"] == "failed" and task["attempts"] >= self.config["max_attempts"]:
                raise Refused(codes["human"])  # no recorded decision in this model
            expected_life = {"review": "review", "failed": "failed"}.get(task["state"], "blocked")
            if life[name] != expected_life:
                raise Refused(codes["closed"])  # an approved task cannot be reopened: the task and queue disagree
            task["state"], life[name] = "ready", "open"
        elif kind == "approve":
            if name not in self.known or life.get(name) != "review":
                raise Refused(codes["illegal"])
            life[name] = "approved"
        elif kind == "complete":
            if task is None:
                raise Refused(codes["precondition"])
            if task["state"] != "review" or life[name] != "approved":
                raise Refused(codes["illegal"])
            if args["actor"] not in ("human", "wrapper"):
                raise Refused(codes["illegal"])  # the lifecycle matrix lets a human or the wrapper close
            task["state"], life[name] = "done", "closed"
        elif kind == "reclaim":
            if task is None or task["lease"] is None:
                raise Refused(codes["precondition"])
            self.expire(task, life, name)
        elif kind == "apply_start":
            lease = self.lease_check(task, args["presented"])
            if task["state"] != "running" or lease["step"] != "apply" or task["phase"] is not None:
                raise Refused(codes["illegal"])
            task["phase"] = "started"
        elif kind == "apply_finish":
            lease = self.lease_check(task, args["presented"])
            if task["state"] != "running" or lease["step"] != "apply" or task["phase"] != "started":
                raise Refused(codes["illegal"])
            task["phase"] = "finished"
        self.tasks, self.life = tasks, life


class Run:
    """One generated sequence applied to a real database and to the model, compared after every operation."""

    def __init__(self, root, seed, length):
        self.root, self.seed, self.length = root, seed, length
        self.problems = []

    def fail(self, step, what):
        if len(self.problems) < 12:
            self.problems.append(f"seed={self.seed} step={step}: {what}")

    def go(self):
        sys.path.insert(0, str(self.root))
        from shit import errors, queue_records, records, store
        from shit.errors import ShitError
        from shit.project import Project

        self.errors, self.store, self.records = errors, store, records
        codes = {"rejected": errors.REJECTED, "illegal": errors.ILLEGAL_TRANSITION, "precondition": errors.PRECONDITION,
                 "human": errors.HUMAN_APPROVAL, "lease": errors.LEASE_REFUSED,
                 "closed": errors.FAILED_CLOSED}
        rng = random.Random(self.seed)
        model = Model(codes, CONFIG)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve() / "repo"
            repo.mkdir()
            git = repo / ".git"
            git.mkdir()
            (git / "HEAD").write_text("ref: refs/heads/main\n")
            (git / "objects").mkdir()
            (git / "refs").mkdir()
            project = Project.init(repo)
            try:
                store.init_store(project)
                conn = store.connect(project)
                try:
                    self.sequence(conn, rng, model, ShitError, queue_records)
                finally:
                    conn.close()
            finally:
                project.close()
        return self.problems

    def create(self, conn, model, task_id, now):
        task = {"schema": "task", "schema_version": 1, "id": task_id, "title": "Title", "summary": "Summary",
                "state": "open", "owner": "human", "created_at": at(now), "updated_at": at(now), "history": []}
        data = self.records.dump_task(task)
        self.store.create_task(conn, task, "human", hashlib.sha256(data).hexdigest(), len(data))
        model.create(task_id)

    def presented(self, rng, model, name):
        task = model.tasks.get(name)
        lease = None if task is None else task["lease"]
        if lease is None or lease["id"] is None or rng.random() < 0.12:
            return {"task_id": name, "lease_id": "".join(rng.choice("0123456789abcdef") for _ in range(32)),
                    "generation": rng.choice((1, 2, 3))}
        shift = rng.choice((-1, 1)) if rng.random() < 0.08 else 0
        return {"task_id": name, "lease_id": lease["id"], "generation": lease["generation"] + shift}

    def sequence(self, conn, rng, model, ShitError, queue_records):
        clock = 10
        for number, name in enumerate(TASKS):
            self.create(conn, model, name, number)
        seen = {"ok": 0, "refused": 0, "expired": 0}
        for step in range(self.length):
            clock += rng.choice((0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 2, 5, 10, 70))
            busy = [t for t in TASKS if t in model.tasks and model.tasks[t]["state"] not in ("ready", "done")]
            name = rng.choice(busy if busy and rng.random() < 0.6 else TASKS)
            state = model.tasks[name]["state"] if name in model.tasks else None
            kind = rng.choice(SUITABLE[state] if rng.random() < 0.75 else EVERY)
            mine = model.tasks.get(name)
            if state == "running" and mine["lease"]["step"] == "apply" and rng.random() < 0.6:
                kind = "apply_finish" if mine["phase"] == "started" else "apply_start"
            now = at(clock)
            args = {"task": name, "actor": rng.choice(("human", "human", "human", "wrapper", "model")),
                    "step": rng.choice(STEPS), "presented": self.presented(rng, model, name),
                    "hold_kind": rng.choice(("owner", "ci"))}
            if kind in ("reopen", "reclaim"):
                args["actor"] = "human" if rng.random() < 0.9 else "wrapper"
            due = [n for n, t in model.tasks.items() if t["lease"] is not None and clock >= t["lease"]["expires"]]
            expected = None
            try:
                model.apply(kind, args, clock)
            except Refused as refusal:
                expected = refusal.code
            context = (kind, name, now)
            got = self.perform(conn, kind, args, now, ShitError)
            if expected is None:
                seen["ok"] += 1
                seen["ok:" + kind] = seen.get("ok:" + kind, 0) + 1
                seen["expired"] += len(due)
                if got[0] != "ok":
                    self.fail(step, f"model accepts {context} but the store gave code {got[1]}")
                    return
                if kind == "claim":
                    lease = got[1]
                    mine = model.tasks[name]["lease"]
                    if (lease["generation"], lease["step"], lease["expires_at"]) != (
                            mine["generation"], mine["step"], at(mine["expires"])):
                        self.fail(step, f"claim result differs from the model: {lease}")
                        return
                    mine["id"] = lease["lease_id"]
                if kind == "renew" and (got[1]["expires_at"], got[1]["renewals"]) != (
                        at(model.tasks[name]["lease"]["expires"]), model.tasks[name]["lease"]["renewals"]):
                    self.fail(step, f"renewal result differs from the model: {got[1]}")
                    return
            else:
                seen["refused"] += 1
                if got != ("code", expected):
                    self.fail(step, f"model refuses {context} with code {expected} but the store gave {got}")
                    return
            self.compare(conn, model, step, context, queue_records)
            if self.problems:
                return
        self.seen = seen

    def perform(self, conn, kind, args, now, ShitError):
        s, name = self.store, args["task"]
        try:
            if kind == "enqueue":
                return "ok", s.queue_enqueue(conn, name, args["actor"], now, config=CONFIG)
            if kind == "claim":
                request = {"task_id": name, "step": args["step"], "worker": "w1", "worker_kind": "ai"}
                return "ok", s.queue_claim(conn, request, now, config=CONFIG)
            if kind in ("start", "renew", "release", "submit", "fail", "apply_finish"):
                return "ok", getattr(s, "queue_" + kind)(conn, args["presented"], now, config=CONFIG)
            if kind == "hold":
                return "ok", s.queue_hold(conn, args["presented"], args["hold_kind"], "Waiting.", "item-1", now,
                                          config=CONFIG)
            if kind == "apply_start":
                return "ok", s.queue_apply_start(conn, args["presented"], ["tg/" + name], now, config=CONFIG)
            if kind == "reopen":
                return "ok", s.queue_reopen(conn, name, args["actor"], "Again.", now, config=CONFIG)
            if kind == "approve":
                return "ok", s.transition_task(conn, name, "approved", "human", "ok", now)
            if kind == "complete":
                return "ok", s.queue_complete(conn, name, args["actor"], "Done.", now, config=CONFIG)
            if kind == "reclaim":
                return "ok", s.queue_reclaim(conn, name, args["actor"], now, config=CONFIG)
            if kind == "maintain":
                return "ok", s.queue_maintain(conn, now, config=CONFIG)
        except ShitError as exc:
            return "code", exc.code
        raise AssertionError(kind)

    def compare(self, conn, model, step, context, queue_records):
        store = self.store
        for found in (store.verify(conn), store.verify_tasks(conn)):
            if found:
                self.fail(step, f"{context} left the store inconsistent: {found}")
                return
        for name in TASKS:
            expected = model.tasks.get(name)
            queued = store.queue_state(conn, name)
            if expected is None:
                if queued is not None:
                    self.fail(step, f"{context}: {name} is queued but the model says no")
                continue
            lease = queued["lease"]
            if (queued["state"], queued["generation"], queued["attempts"]) != (
                    expected["state"], expected["generation"], expected["attempts"]):
                self.fail(step, "{}: {} is {} gen {} attempts {}, model {} gen {} attempts {}".format(
                    context, name, queued["state"], queued["generation"], queued["attempts"], expected["state"],
                    expected["generation"], expected["attempts"]))
                return
            if (lease is None) != (expected["lease"] is None):
                self.fail(step, "{}: lease of {} is {}, model {}".format(context, name, lease, expected["lease"]))
                return
            if lease is not None and (lease["lease_id"], lease["expires_at"], lease["renewals"]) != (
                    expected["lease"]["id"], at(expected["lease"]["expires"]), expected["lease"]["renewals"]):
                self.fail(step, f"{context}: lease of {name} differs from the model")
                return
            row = conn.execute("SELECT state FROM tasks WHERE id = ?", (name,)).fetchone()[0]
            if row != model.life[name]:
                self.fail(step, f"{context}: task {name} is {row}, model {model.life[name]}")
                return
            table = conn.execute("SELECT count(*) FROM leases WHERE task_id = ?", (name,)).fetchone()[0]
            if table != (0 if expected["lease"] is None else 1):
                self.fail(step, f"{context}: lease rows of {name}: {table}")
                return
        slots = conn.execute("SELECT count(*) FROM apply_slots").fetchone()[0]
        held = sum(1 for t in model.tasks.values() if t["phase"] == "started")
        if slots != held:
            self.fail(step, f"{context}: {slots} apply slots, model {held}")
            return
        replayed = queue_records.replay(store.queue_entries(conn))
        for name, queued in replayed.items():
            expected = model.tasks[name]
            if (queued["state"], queued["generation"], queued["attempts"]) != (
                    expected["state"], expected["generation"], expected["attempts"]):
                self.fail(step, "{}: replay of the log says {} for {}, model {}".format(
                    context, queued["state"], name, expected["state"]))
                return


def run_sequence(root, seed, length):
    """The disagreements between the store under `root` and the model, for one seed (empty when they agree)."""
    run = Run(root, seed, length)
    problems = run.go()
    run.problems = problems
    return problems, getattr(run, "seen", {})


if __name__ == "__main__":
    spec = json.loads(sys.argv[1])
    found, counts = [], {}
    for seed in spec["seeds"]:
        problems, seen = run_sequence(spec["root"], seed, spec["length"])
        found += problems
        for key, value in seen.items():
            counts[key] = counts.get(key, 0) + value
    print(json.dumps({"problems": found, "seen": counts}))
