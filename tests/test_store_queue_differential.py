"""The queue operations of the store against `shit/queue.py` and `queue_records.replay` (issue 61, C4).

Random operation sequences, one seed per test, are applied through the store helpers and through the file queue
(which stays in place until C7). After every operation both must give the same exit code and the same result, and
after the same maintenance the two logs must be byte for byte equal: every entry, and every hash of the chain. At the
end the leases, the queue states, the task lifecycle states and the replay of the entries the database holds must
agree, and `store.verify` and `store.verify_tasks` must report nothing.

Only the file queue's own sources of randomness and of facts the store cannot read are replaced: the lease id (a
counter on each side, so ids match when the decisions match), the milestone reader (one shared set), the closing
rules (`complete`, proven elsewhere) and the decision lookup of `reopen`.
"""

import hashlib
import os
import random
import tempfile
import unittest
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from _bootstrap import make_git_dir

from shit import commands, errors, records, store
from shit import queue as q
from shit import queue_records as qr
from shit import task as task_mod
from shit.errors import ShitError
from shit.project import Project

BASE = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC)
CONFIG = {"lease_ttl_seconds": 300, "lease_max_seconds": 900, "lease_max_renewals": 3, "max_attempts": 2}
TASKS = ("t0", "t1", "t2", "t3", "t4")
TYPES = ("draft", "apply", "review", "decision")
SUITABLE = {None: ("enqueue", "enqueue", "add_edge"), "waiting": ("claim", "maintain", "add_edge"),
            "ready": ("claim", "claim", "add_edge"),
            "dispatched": ("start", "start", "renew", "release", "hold", "fail", "reclaim", "add_edge", "add_edge"),
            "running": ("renew", "submit", "submit", "fail", "hold", "reclaim", "add_edge", "add_edge", "add_edge"),
            "review": ("approve", "complete", "complete", "reopen"), "hold-owner": ("reopen",),
            "hold-ci": ("reopen",), "failed": ("reopen", "reopen", "approve"), "done": ("reopen", "claim")}
EVERY = ("enqueue", "claim", "start", "renew", "release", "submit", "fail", "hold", "reopen", "approve",
         "complete", "reclaim", "add_edge", "remove_edge", "maintain")

# t1 needs t0 (draft) and is enqueued first: it waits, and is promoted once t0's milestone is reached. t2 is submitted
# while a new edge is unmet (exit code 9), then once the milestone is reached. t4 fails at the attempt limit and
# is reopened only after a decision. Every step is checked against the file queue like a random one.
LIMIT = {**CONFIG, "max_attempts": 1}
PROLOGUE = (
    ("add_edge", "t1", {"edge": ("t1", "t0", "draft")}),
    ("enqueue", "t1", {}),
    ("enqueue", "t0", {}),
    ("maintain", "t0", {}),
    ("claim", "t1", {"expect": 4}),  # still waiting
    ("maintain", "t0", {"reached": ("t0", "draft")}),  # promotion of t1
    ("claim", "t1", {}),
    ("enqueue", "t2", {}),
    ("claim", "t2", {}),
    ("start", "t2", {}),
    ("add_edge", "t2", {"edge": ("t2", "t3", "review")}),
    ("submit", "t2", {"expect": 9}),
    ("maintain", "t2", {"reached": ("t3", "review")}),
    ("submit", "t2", {}),
    ("enqueue", "t4", {}),
    ("claim", "t4", {}),
    ("start", "t4", {}),
    ("fail", "t4", {}),
    ("reopen", "t4", {"cfg": LIMIT, "decided": False, "expect": 10}),
    ("reopen", "t4", {"cfg": LIMIT, "decided": True}),
)


def at(seconds):
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ids:
    """Lease ids from a counter: the two sides agree as long as they agree on every claim."""

    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1
        return hashlib.sha256(("lease-" + str(self.count)).encode()).hexdigest()[:32]


def outcome(func):
    """('ok', result) or ('code', exit code)."""
    try:
        return "ok", func()
    except ShitError as exc:
        return "code", exc.code


class DifferentialTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        self.repo.mkdir()
        make_git_dir(self.repo / ".git")
        self.proj = Project.init(self.repo)
        self.addCleanup(self.proj.close)
        store.init_store(self.proj)
        self.conn = store.connect(self.proj)
        self.addCleanup(self.conn.close)
        self.reached = set()
        self.decided = False
        for target, value in ((q, "_meter"), (commands, "_check_closable"), (q, "_decided_after")):
            patcher = mock.patch.object(target, value, self.replacement(value))
            patcher.start()
            self.addCleanup(patcher.stop)
        for module in (q, store):
            patcher = mock.patch.object(module, "_new_lease_id", Ids())
            patcher.start()
            self.addCleanup(patcher.stop)
        self.leases = {}
        self.floor = 0
        for number, name in enumerate(TASKS):
            stamp = at(number)
            task_mod.create_task(self.proj, task_id=name, title="Title", summary="Summary", owner="human",
                                 actor="human", now=stamp)
            task = {"schema": "task", "schema_version": 1, "id": name, "title": "Title", "summary": "Summary",
                    "state": "open", "owner": "human", "created_at": stamp, "updated_at": stamp, "history": []}
            data = records.dump_task(task)
            store.create_task(self.conn, task, "human", hashlib.sha256(data).hexdigest(), len(data))
        self.floor = len(TASKS)

    def replacement(self, name):
        if name == "_meter":
            return lambda project, get_events: self.met
        if name == "_check_closable":
            return lambda *args, **kwargs: None
        return lambda project, log, task_id, stamp: self.decided

    def met(self, edge):
        return edge["type"] not in qr_blocking() or (edge["needs_id"], edge["type"]) in self.reached

    # -- one operation on both sides ---------------------------------------------------------------------------

    def present(self, rng, task):
        lease = self.leases.get(task)
        if lease is None or rng.random() < 0.2:
            return {"task_id": task, "lease_id": "".join(rng.choice("0123456789abcdef") for _ in range(32)),
                    "generation": rng.choice((1, 2, 3))}
        presented = {"task_id": task, "lease_id": lease["lease_id"], "generation": lease["generation"]}
        roll = rng.random()
        if roll < 0.08:
            presented["generation"] += rng.choice((-1, 1))
        elif roll < 0.12:
            del presented["generation"]
        elif roll < 0.16:
            presented["time"] = "2026-01-01T00:00:00Z"
        return presented

    def choose(self, rng, task):
        """Mostly the operation the task's queue state invites, sometimes any other (it should be refused)."""
        queued = store.queue_state(self.conn, task) if task in TASKS else None
        if rng.random() < 0.7 and task in TASKS:
            return rng.choice(SUITABLE[queued["state"] if queued else None])
        return rng.choice(EVERY)

    def pairs(self, now, cfg, task, actor, presented, request, reason, hold_kind, edge):
        """{kind: (call on the file queue, call on the store)} for one set of arguments."""
        p = self.proj
        met = self.met
        c = self.conn
        return {
            "enqueue": (lambda: q.enqueue(p, task, actor, now=now, config=cfg),
                        lambda: store.queue_enqueue(c, task, actor, now, config=cfg, met=met)),
            "claim": (lambda: q.claim(p, request, now=now, config=cfg),
                      lambda: store.queue_claim(c, request, now, config=cfg, met=met)),
            "start": (lambda: q.start(p, presented, now=now, config=cfg),
                      lambda: store.queue_start(c, presented, now, config=cfg, met=met)),
            "renew": (lambda: q.renew(p, presented, now=now, config=cfg),
                      lambda: store.queue_renew(c, presented, now, config=cfg, met=met)),
            "release": (lambda: q.release(p, presented, now=now, config=cfg),
                        lambda: store.queue_release(c, presented, now, config=cfg, met=met)),
            "submit": (lambda: q.submit(p, presented, now=now, config=cfg),
                       lambda: store.queue_submit(c, presented, now, config=cfg, met=met)),
            "fail": (lambda: q.fail(p, presented, now=now, config=cfg),
                     lambda: store.queue_fail(c, presented, now, config=cfg, met=met)),
            "hold": (lambda: q.hold(p, presented, hold_kind, reason, "item-1", now=now, config=cfg),
                     lambda: store.queue_hold(c, presented, hold_kind, reason, "item-1", now, config=cfg, met=met)),
            "reopen": (lambda: q.reopen(p, task, actor, reason, now=now, config=cfg),
                       lambda: store.queue_reopen(c, task, actor, reason, now, config=cfg, met=met,
                                                  decided=lambda stamp: self.decided)),
            "approve": (lambda: task_mod.transition(p, task, "approved", "human", "ok", now=now),
                        lambda: store.transition_task(c, task, "approved", "human", "ok", now)),
            "complete": (lambda: q.complete(p, task, actor, reason, now=now, config=cfg),
                         lambda: store.queue_complete(c, task, actor, reason, now, config=cfg, met=met)),
            "reclaim": (lambda: q.reclaim(p, task, actor, now=now, config=cfg),
                        lambda: store.queue_reclaim(c, task, actor, now, config=cfg, met=met)),
            "add_edge": (lambda: q.add_edge(p, *edge, actor, now=now, config=cfg),
                         lambda: store.queue_add_edges(c, [edge], actor, now, config=cfg, met=met)),
            "remove_edge": (lambda: q.remove_edge(p, *edge, actor, now=now, config=cfg),
                            lambda: store.queue_remove_edge(c, *edge, actor, now, config=cfg, met=met)),
            "maintain": (lambda: q.status(p, now=now, config=cfg),
                         lambda: store.queue_maintain(c, now, config=cfg, met=met)),
        }

    def operations(self, rng, now):
        """[(label, file call, store call)] for one randomly chosen operation."""
        task = rng.choice((*TASKS, *TASKS, "ghost"))
        other = rng.choice(TASKS)
        actor = rng.choice(("human", "human", "wrapper", "model"))
        presented = self.present(rng, task)
        worker = rng.choice(("w1", "w2"))
        step = rng.choice(("draft", "draft", "review", "apply"))
        reason = rng.choice(("Because.", "Needs work.", "Waiting.", "Waiting for a check.", "bad" + chr(1), ""))
        hold_kind = rng.choice(("owner", "ci", "ci", "owner", "owner", "ci", "ci", "bogus"))
        edge = (task, other, rng.choice(("draft", "draft", "review", "decision", "apply", "bogus")))
        request = {"task_id": task, "step": step, "worker": worker, "worker_kind": rng.choice(("ai", "human"))}
        if rng.random() < 0.05:
            request["lease_id"] = "f" * 32
        kind = self.choose(rng, task)
        cfg = rng.choice((CONFIG, {**CONFIG, "max_attempts": 1}))
        table = self.pairs(now, cfg, task, actor, presented, request, reason, hold_kind, edge)
        return kind, task, table[kind][0], table[kind][1]

    def file_entries(self):
        lines = (self.repo / ".shit" / "queue.jsonl").read_bytes().splitlines() if (
            self.repo / ".shit" / "queue.jsonl").exists() else []
        chain = (self.repo / ".shit" / "queue.chain").read_bytes().split() if (
            self.repo / ".shit" / "queue.chain").exists() else []
        return [qr.load_entry(line) for line in lines], [c.decode() for c in chain]

    def compare_logs(self, context):
        entries, chain = self.file_entries()
        mine = store.queue_entries(self.conn)
        hashes = [row[0] for row in self.conn.execute("SELECT hash FROM queue_entries ORDER BY seq").fetchall()]
        self.assertEqual(len(entries), len(mine), (context, entries[-2:], mine[-2:]))
        for number, (a, b) in enumerate(zip(entries, mine, strict=True), start=1):
            self.assertEqual(a, b, (context, "entry", number))
        self.assertEqual(chain, hashes, context)

    def execute(self, kind, task, on_file, on_store, context, seen):
        """One operation on both sides: the same outcome and, for a lease, the same record."""
        expected = outcome(on_file)
        actual = outcome(on_store)
        self.assertEqual(actual[0], expected[0], (context, expected, actual))
        if expected[0] == "code" or kind in ("claim", "renew"):
            self.assertEqual(actual[1], expected[1], (context, expected, actual))
        seen[(kind, expected[0] if expected[0] == "ok" else expected[1])] += 1
        if expected[0] == "ok" and kind in ("claim", "renew"):
            self.leases[task] = expected[1]
        return expected

    def sync(self, context):
        """The store keeps no maintenance from a refused operation (it rolled back with it); the file queue does.
        Both apply the same maintenance now, and from here the logs must be equal."""
        self.assertEqual(outcome(lambda: q.status(self.proj, now=at(self.floor), config=CONFIG))[0],
                         outcome(lambda: store.queue_maintain(self.conn, at(self.floor), config=CONFIG,
                                                              met=self.met))[0])
        self.compare_logs(context)
        self.assertEqual(store.verify(self.conn), [], context)

    def scripted(self, steps):
        """Fixed steps that reach the paths a random run reaches only by luck, each checked as the random ones are."""
        seen = Counter()
        for number, (kind, task, args) in enumerate(steps):
            self.floor += 2
            now = at(self.floor)
            cfg = args.pop("cfg", CONFIG)
            if "reached" in args:
                self.reached ^= {args.pop("reached")}
            if "decided" in args:
                self.decided = args.pop("decided")
            lease = self.leases.get(task)
            presented = {"task_id": task, "lease_id": lease["lease_id"], "generation": lease["generation"]} if lease else {}
            request = {"task_id": task, "step": "draft", "worker": "w1", "worker_kind": "human"}
            table = self.pairs(now, cfg, task, args.get("actor", "human"), presented, request, "Because.", "owner",
                               args.get("edge"))
            expected = self.execute(kind, task, *table[kind], ("scripted", number, kind, task, now), seen)
            self.assertEqual(expected[0], args.get("expect", "ok") if args.get("expect") in (None, "ok") else "code")
            if "expect" in args and args["expect"] != "ok":
                self.assertEqual(expected[1], args["expect"], (number, kind, task))
            self.sync(("scripted", number))
        return seen

    def run_sequence(self, seed, length):
        rng = random.Random(seed)
        seen = Counter()
        clock = self.floor
        for number in range(length):
            clock += rng.choice((0, 0, 0, 1, 2, 5, 10, 30, 120))
            now = at(clock if rng.random() > 0.04 else max(0, clock - 400))  # now and then the clock goes back
            if rng.random() < 0.06:
                self.reached ^= {(rng.choice(TASKS), rng.choice(TYPES))}
            if rng.random() < 0.15:
                self.decided = not self.decided
            kind, task, on_file, on_store = self.operations(rng, now)
            self.execute(kind, task, on_file, on_store, (seed, number, kind, task, now), seen)
            self.floor = max(self.floor, clock)
            self.sync((seed, number))
        return seen

    def final_checks(self):
        self.assertEqual(store.verify(self.conn), [])
        self.assertEqual(store.verify_tasks(self.conn), [])
        entries = store.queue_entries(self.conn)
        replayed = qr.replay(entries)
        stamp = at(self.floor)
        snapshot = store.queue_snapshot(self.conn, self.met)
        file_status = q.status(self.proj, now=stamp, config=CONFIG)
        self.assertEqual(snapshot, file_status)
        leases = {row[0]: row for row in self.conn.execute(
            "SELECT task_id, lease_id, generation, step, worker, worker_kind, claimed_at, expires_at, renewals "
            "FROM leases").fetchall()}
        for name, queued in replayed.items():
            lease = queued["lease"]
            if lease is None:
                self.assertNotIn(name, leases)
            else:
                row = leases[name]
                self.assertEqual((row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8]),
                                 (lease["lease_id"], lease["generation"], lease["step"], lease["worker"],
                                  lease["worker_kind"], lease["claimed_at"], lease["expires_at"], lease["renewals"]))
            row = self.conn.execute("SELECT state FROM tasks WHERE id = ?", (name,)).fetchone()
            self.assertEqual(row[0], task_mod.get_task(self.proj, name)["state"], name)
            self.assertIn(row[0], qr.LIFECYCLE[queued["state"]])
            self.assertEqual(snapshot[name]["stored_state"], queued["state"])
        for name in TASKS:
            self.assertEqual(self.conn.execute("SELECT state FROM tasks WHERE id = ?", (name,)).fetchone()[0],
                             task_mod.get_task(self.proj, name)["state"])
        file_leases = sorted(p.name for p in (self.repo / ".shit" / "leases").glob("*.json")) if (
            self.repo / ".shit" / "leases").exists() else []
        self.assertEqual(file_leases, sorted(name + ".json" for name in leases))

    def test_random_sequences_agree_with_the_file_queue_and_with_replay(self):
        seeds = [int(x) for x in os.environ.get("SHIT_DIFF_SEEDS", "1,2,3").split(",")]
        length = int(os.environ.get("SHIT_DIFF_LENGTH", "140"))
        seen = Counter()
        seen.update(self.scripted(PROLOGUE))
        for seed in seeds:
            seen.update(self.run_sequence(seed, length))
        self.final_checks()
        accepted = {kind for (kind, result), _ in seen.items() if result == "ok"}
        refused = {result for (kind, result), _ in seen.items() if result != "ok"}
        self.assertGreaterEqual(len(accepted), 9, sorted(accepted))
        self.assertGreaterEqual(len(refused), 4, sorted(refused))
        entry_kinds = Counter(e["kind"] for e in store.queue_entries(self.conn))
        self.assertGreaterEqual(len(entry_kinds), 8, entry_kinds)
        self.assertIn(errors.LEASE_REFUSED, refused)


def qr_blocking():
    from shit import dependencies

    return dependencies.BLOCKING


if __name__ == "__main__":
    unittest.main()
