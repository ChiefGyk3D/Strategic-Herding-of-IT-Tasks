"""Dependency graph in the store (issue 62, epic 57): add and remove edge, caps, cycles, closure, and a differential
test against the file-based `shit/dependencies.py`.

Real database files in a temporary directory; nothing about sqlite is mocked. Every operation's test calls
`store.verify`. Tampering and fixtures use a plain `sqlite3` connection opened by the test itself.
"""

import hashlib
import random
import sqlite3
import tempfile
import unittest
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

from _bootstrap import make_git_dir

from shit import dependencies as dep
from shit import errors, store
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
        repo = Path(self._tmp.name).resolve() / "repo"
        repo.mkdir()
        make_git_dir(repo / ".git")
        self.proj = Project.init(repo)
        self.addCleanup(self.proj.close)
        self.db = repo / ".shit" / store.DB_NAME
        store.init_store(self.proj)
        self.conn = store.connect(self.proj, busy_timeout_ms=200)
        self.addCleanup(self.conn.close)
        self.clock = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC)
        self.count = 0

    def tasks(self, *ids):
        with store.write_transaction(self.conn):
            for task_id in ids:
                self.conn.execute("INSERT INTO tasks (id, state, owner, created_at, updated_at) "
                                  "VALUES (?, 'open', 'human', ?, ?)", (task_id, NOW, NOW))

    def raw(self):
        conn = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(conn.close)
        return conn

    def lease(self, task_id):
        """Give `task_id` a live lease the way the queue will: enqueued, ready, claimed, then the lease row."""
        def entry(kind, state, actor="wrapper", **cols):
            self.count += 1
            self.clock += timedelta(seconds=1)
            position = self.conn.execute("SELECT count(*) FROM queue_entries").fetchone()[0] + 1
            tip = self.conn.execute("SELECT hash FROM queue_entries ORDER BY seq DESC LIMIT 1").fetchone()
            row = {"id": f"queue-{position:04d}", "task_id": task_id,
                   "timestamp": self.clock.strftime("%Y-%m-%dT%H:%M:%SZ"), "actor": actor, "kind": kind,
                   "state_after": state, "prev_hash": tip[0] if tip else H64,
                   "hash": hashlib.sha256(str(self.count).encode()).hexdigest(), **cols}
            names = list(row)
            with store.write_transaction(self.conn):
                self.conn.execute("INSERT INTO queue_entries (" + ", ".join(names) + ") VALUES ("
                                  + ", ".join("?" for _ in names) + ")", [row[n] for n in names])

        lease_id = hashlib.sha256(task_id.encode()).hexdigest()[:32]
        expires = (self.clock + timedelta(seconds=1000)).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry("enqueued", "waiting", actor="human")
        entry("ready", "ready")
        entry("claimed", "dispatched", lease_id=lease_id, generation=1, worker="worker-one", worker_kind="ai",
              step="draft", expires_at=expires)
        claimed_at = self.conn.execute("SELECT timestamp FROM queue_entries WHERE task_id = ? AND kind = 'claimed'",
                                       (task_id,)).fetchone()[0]
        with store.write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO leases (task_id, lease_id, generation, step, worker, worker_kind, claimed_at, expires_at, "
                "renewals) VALUES (?, ?, 1, 'draft', 'worker-one', 'ai', ?, ?, 0)", (task_id, lease_id, claimed_at,
                                                                                    expires))

    def consistent(self):
        self.assertEqual(store.verify(self.conn), [])


class AddEdgeTests(Base):
    def test_add_and_list(self):
        self.tasks("a", "b", "c")
        self.assertIsNone(store.add_edge(self.conn, "b", "a", "draft"))
        self.consistent()
        store.add_edge(self.conn, "c", "b", "apply")
        store.add_edge(self.conn, "c", "a", "review")
        self.consistent()
        self.assertEqual(store.list_edges(self.conn), [
            {"task_id": "b", "needs_id": "a", "type": "draft"},
            {"task_id": "c", "needs_id": "a", "type": "review"},
            {"task_id": "c", "needs_id": "b", "type": "apply"}])
        self.assertEqual(store.needs_of(self.conn, "c"),
                         [{"needs_id": "a", "type": "review"}, {"needs_id": "b", "type": "apply"}])
        self.assertEqual(store.needs_of(self.conn, "a"), [])
        self.assertEqual(store.prerequisite_closure(self.conn, "c"), ["a", "b"])
        self.assertEqual(store.prerequisite_closure(self.conn, "a"), [])

    def test_the_same_pair_may_carry_two_types_like_the_file_graph(self):
        self.tasks("a", "b")
        store.add_edge(self.conn, "b", "a", "draft")
        store.add_edge(self.conn, "b", "a", "apply")
        self.consistent()
        self.assertEqual(len(store.list_edges(self.conn)), 2)
        self.assertEqual(code_of(store.add_edge, self.conn, "b", "a", "apply"), errors.REJECTED)
        self.assertEqual(store.prerequisite_closure(self.conn, "b"), ["a"])

    def test_error_codes_and_that_a_refusal_changes_nothing(self):
        self.tasks("a", "b", "c")
        store.add_edge(self.conn, "b", "a", "draft")
        store.add_edge(self.conn, "c", "b", "draft")
        before = store.list_edges(self.conn)
        cases = {
            "duplicate": (("b", "a", "draft"), errors.REJECTED),
            "cycle of two": (("a", "b", "draft"), errors.REJECTED),
            "cycle of three": (("a", "c", "review"), errors.REJECTED),
            "self edge": (("a", "a", "draft"), errors.REJECTED),
            "unknown dependent": (("ghost", "a", "draft"), errors.PRECONDITION),
            "unknown prerequisite": (("a", "ghost", "draft"), errors.PRECONDITION),
            "self edge on an unknown task": (("ghost", "ghost", "draft"), errors.PRECONDITION),
            "unknown type": (("a", "c", "other"), errors.REJECTED),
            "type is not text": (("a", "c", None), errors.REJECTED),
            "upper case": (("A", "c", "draft"), errors.REJECTED),
            "path": (("a", "../c", "draft"), errors.REJECTED),
            "NUL": (("a", "c\x00", "draft"), errors.REJECTED),
            "empty": (("a", "", "draft"), errors.REJECTED),
            "too long": (("a", "x" * 65, "draft"), errors.REJECTED),
            "very long": (("a", "x" * 100000, "draft"), errors.REJECTED),
            "bidi": (("a", "c‮", "draft"), errors.REJECTED),
            "not text": ((5, "c", "draft"), errors.REJECTED),
            "bytes": ((b"a", "c", "draft"), errors.REJECTED),
        }
        for name, (args, code) in cases.items():
            with self.subTest(name):
                self.assertEqual(code_of(store.add_edge, self.conn, *args), code)
                self.consistent()
                self.assertEqual(store.list_edges(self.conn), before)

    def test_error_codes_mapped_from_the_database_for_unrelated_unknown_ids(self):
        # A task that exists only as an id in a hostile string must not reach the database as SQL.
        self.tasks("a")
        for hostile in ("a'; DROP TABLE edges; --", "a\" OR 1=1", "%", "_"):
            with self.subTest(hostile=hostile):
                self.assertEqual(code_of(store.add_edge, self.conn, hostile, "a", "draft"), errors.REJECTED)
        self.assertEqual(store.list_edges(self.conn), [])
        self.consistent()

    def test_a_diamond_is_not_a_cycle(self):
        self.tasks("top", "left", "right", "base")
        for edge in (("left", "base"), ("right", "base"), ("top", "left"), ("top", "right")):
            store.add_edge(self.conn, edge[0], edge[1], "draft")
        self.consistent()
        self.assertEqual(store.prerequisite_closure(self.conn, "top"), ["base", "left", "right"])
        self.assertEqual(code_of(store.add_edge, self.conn, "base", "top", "draft"), errors.REJECTED)

    def test_a_batch_is_all_or_nothing(self):
        self.tasks("a", "b", "c")
        store.add_edge(self.conn, "b", "a", "draft")
        before = store.list_edges(self.conn)
        for name, batch, code in (
                ("cycle at the end", [("c", "b", "draft"), ("a", "c", "draft")], errors.REJECTED),
                ("unknown at the end", [("c", "b", "draft"), ("c", "ghost", "draft")], errors.PRECONDITION),
                ("duplicate inside the batch", [("c", "b", "draft"), ("c", "b", "draft")], errors.REJECTED),
                ("duplicate of a stored edge", [("c", "b", "draft"), ("b", "a", "draft")], errors.REJECTED),
                ("self edge", [("c", "b", "draft"), ("c", "c", "draft")], errors.REJECTED),
                ("bad shape after an unknown", [("ghost", "b", "draft"), ("c", "B", "draft")], errors.PRECONDITION)):
            with self.subTest(name):
                self.assertEqual(code_of(store.add_edges, self.conn, batch), code)
                self.assertEqual(store.list_edges(self.conn), before)
                self.consistent()
        store.add_edges(self.conn, [("c", "b", "draft"), ("c", "a", "apply")])
        self.assertEqual(len(store.list_edges(self.conn)), 3)
        self.consistent()
        self.assertIsNone(store.add_edges(self.conn, []))

    def test_a_batch_that_closes_a_cycle_among_its_own_edges(self):
        self.tasks("a", "b", "c")
        self.assertEqual(code_of(store.add_edges, self.conn, [("a", "b", "draft"), ("b", "c", "draft"),
                                                              ("c", "a", "draft")]), errors.REJECTED)
        self.assertEqual(store.list_edges(self.conn), [])
        self.consistent()

    def test_a_batch_argument_that_is_not_a_list_of_triples_is_refused(self):
        self.tasks("a", "b")
        for bad in (None, 5, "ab", [("a", "b")], [("a", "b", "draft", "x")], [None], [{"task_id": "a"}]):
            with self.subTest(bad=bad):
                self.assertEqual(code_of(store.add_edges, self.conn, bad), errors.REJECTED)
        self.assertEqual(store.list_edges(self.conn), [])

    def test_each_add_is_one_immediate_transaction(self):
        self.tasks("a", "b")
        other = store.connect(self.proj, busy_timeout_ms=0)
        self.addCleanup(other.close)
        with store.write_transaction(other):  # another writer holds the lock
            self.assertEqual(code_of(store.add_edge, self.conn, "b", "a", "draft"), errors.BUSY)
            self.assertEqual(code_of(store.remove_edge, self.conn, "b", "a", "draft"), errors.BUSY)
        self.assertIsNone(store.add_edge(self.conn, "b", "a", "draft"))
        self.assertFalse(self.conn.in_transaction)
        self.consistent()

    def test_the_database_itself_still_refuses_an_edge_between_missing_tasks(self):
        # Defense in depth: the foreign keys and the no-self-edge check are in the schema, not only in the helper.
        self.tasks("a")
        conn = self.raw()
        conn.execute("PRAGMA foreign_keys = ON")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO edges (task_id, needs_id, type) VALUES ('a', 'nope', 'draft')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO edges (task_id, needs_id, type) VALUES ('a', 'a', 'draft')")


class CapTests(Base):
    def seed_tasks(self, count, prefix="t"):
        names = [f"{prefix}{i}" for i in range(count)]
        raw = self.raw()
        raw.execute("BEGIN")
        raw.executemany("INSERT INTO tasks (id, state, owner, created_at, updated_at) VALUES (?, 'open', 'human', ?, ?)",
                        [(n, NOW, NOW) for n in names])
        raw.execute("COMMIT")
        return names

    def test_per_source_cap(self):
        names = self.seed_tasks(dep.MAX_NEEDS_PER_TASK + 3)
        source, others = names[0], names[1:]
        store.add_edges(self.conn, [(source, other, "draft") for other in others[:dep.MAX_NEEDS_PER_TASK]])
        self.consistent()
        self.assertEqual(len(store.needs_of(self.conn, source)), dep.MAX_NEEDS_PER_TASK)
        self.assertEqual(code_of(store.add_edge, self.conn, source, others[-1], "draft"), errors.INPUT_LIMIT)
        # A second type on a pair already present counts too, and a batch over the cap is refused whole.
        self.assertEqual(code_of(store.add_edge, self.conn, source, others[0], "apply"), errors.INPUT_LIMIT)
        self.assertEqual(len(store.needs_of(self.conn, source)), dep.MAX_NEEDS_PER_TASK)
        # Another source is not affected.
        store.add_edge(self.conn, others[0], others[1], "draft")
        self.consistent()
        # Removing one frees a place.
        store.remove_edge(self.conn, source, others[0], "draft")
        store.add_edge(self.conn, source, others[-1], "draft")
        self.consistent()

    def test_the_cap_is_checked_before_the_cycle_like_the_file_graph(self):
        names = self.seed_tasks(dep.MAX_NEEDS_PER_TASK + 2)
        source = names[0]
        store.add_edges(self.conn, [(source, other, "draft") for other in names[1:dep.MAX_NEEDS_PER_TASK + 1]])
        # A self edge as the 201st would also be a cycle; the file graph reports the cap first.
        self.assertEqual(code_of(store.add_edge, self.conn, source, source, "draft"), errors.INPUT_LIMIT)
        self.assertEqual(code_of(dep.add_edge, {**dep.empty_graph(), "edges": [
            {"task_id": source, "needs_id": o, "type": "draft"} for o in names[1:dep.MAX_NEEDS_PER_TASK + 1]]},
            source, source, "draft", set(names)), errors.INPUT_LIMIT)

    def test_total_source_cap(self):
        names = self.seed_tasks(dep.MAX_TASKS + 2)
        hub = names[-1]
        store.add_edges(self.conn, [(n, hub, "draft") for n in names[:dep.MAX_TASKS]])
        self.consistent()
        self.assertEqual(len({e["task_id"] for e in store.list_edges(self.conn)}), dep.MAX_TASKS)
        self.assertEqual(code_of(store.add_edge, self.conn, names[-2], hub, "draft"), errors.INPUT_LIMIT)
        # An existing source may still grow.
        store.add_edge(self.conn, names[0], names[1], "apply")
        self.consistent()
        # Freeing one source makes room for a new one.
        store.remove_edge(self.conn, names[5], hub, "draft")
        store.add_edge(self.conn, names[-2], hub, "draft")
        self.consistent()
        self.assertEqual(code_of(store.add_edge, self.conn, names[5], hub, "draft"), errors.INPUT_LIMIT)

    def test_total_cap_counts_a_self_edge_the_file_graph_counts(self):
        names = self.seed_tasks(dep.MAX_TASKS + 1)
        hub = names[-1]
        store.add_edges(self.conn, [(n, hub, "draft") for n in names[:dep.MAX_TASKS]])
        self.assertEqual(code_of(store.add_edge, self.conn, hub, hub, "draft"), errors.INPUT_LIMIT)

    def test_a_deep_chain_within_the_caps(self):
        depth = 1900
        names = self.seed_tasks(depth + 1, "n")
        chain = [(names[i + 1], names[i], "draft") for i in range(depth)]
        store.add_edges(self.conn, chain)
        self.consistent()
        self.assertEqual(store.prerequisite_closure(self.conn, names[depth]), sorted(names[:depth]))
        self.assertEqual(code_of(store.add_edge, self.conn, names[0], names[depth], "draft"), errors.REJECTED)
        self.assertEqual(code_of(store.add_edge, self.conn, names[0], names[depth // 2], "apply"), errors.REJECTED)
        store.add_edge(self.conn, names[depth], names[0], "apply")  # a shortcut, not a cycle
        self.consistent()
        file_graph = dep.add_edges(dep.empty_graph(), chain, set(names))
        self.assertEqual(code_of(dep.add_edge, file_graph, names[0], names[depth], "draft", set(names)),
                         errors.REJECTED)

    def test_a_chain_grown_one_edge_at_a_time(self):
        names = self.seed_tasks(400, "s")
        for i in range(399):
            store.add_edge(self.conn, names[i + 1], names[i], "draft")
        self.consistent()
        self.assertEqual(len(store.prerequisite_closure(self.conn, names[399])), 399)
        self.assertEqual(code_of(store.add_edge, self.conn, names[0], names[399], "draft"), errors.REJECTED)


class RemoveEdgeTests(Base):
    def test_remove_and_the_errors(self):
        self.tasks("a", "b", "c")
        store.add_edge(self.conn, "b", "a", "draft")
        store.add_edge(self.conn, "b", "a", "apply")
        store.add_edge(self.conn, "c", "b", "draft")
        self.assertIsNone(store.remove_edge(self.conn, "b", "a", "draft"))
        self.consistent()
        self.assertEqual(store.list_edges(self.conn), [{"task_id": "b", "needs_id": "a", "type": "apply"},
                                                       {"task_id": "c", "needs_id": "b", "type": "draft"}])
        before = store.list_edges(self.conn)
        for name, args, code in (
                ("already gone", ("b", "a", "draft"), errors.PRECONDITION),
                ("wrong type", ("c", "b", "apply"), errors.PRECONDITION),
                ("wrong direction", ("a", "b", "apply"), errors.PRECONDITION),
                ("never existed", ("a", "c", "draft"), errors.PRECONDITION),
                ("unknown task", ("ghost", "a", "draft"), errors.PRECONDITION),
                ("bad id", ("B", "a", "draft"), errors.REJECTED),
                ("NUL", ("b", "a\x00", "draft"), errors.REJECTED),
                ("bad type", ("b", "a", "other"), errors.REJECTED),
                ("type not text", ("b", "a", 7), errors.REJECTED),
                ("not text", (None, "a", "draft"), errors.REJECTED)):
            with self.subTest(name):
                self.assertEqual(code_of(store.remove_edge, self.conn, *args), code)
                self.assertEqual(store.list_edges(self.conn), before)
                self.consistent()

    def test_removal_guard_refuses_while_the_dependent_holds_a_live_lease(self):
        self.tasks("a", "b", "c")
        store.add_edge(self.conn, "b", "a", "draft")
        store.add_edge(self.conn, "c", "b", "draft")
        self.lease("b")
        self.consistent()
        self.assertEqual(code_of(store.remove_edge, self.conn, "b", "a", "draft"), errors.ILLEGAL_TRANSITION)
        self.assertEqual(len(store.list_edges(self.conn)), 2)
        # The prerequisite holding a lease does not matter, only the dependent.
        store.remove_edge(self.conn, "c", "b", "draft")
        self.assertEqual(store.list_edges(self.conn), [{"task_id": "b", "needs_id": "a", "type": "draft"}])
        self.consistent()
        # Missing edge is reported before the lease, as in the file graph.
        self.assertEqual(code_of(store.remove_edge, self.conn, "b", "a", "apply"), errors.PRECONDITION)
        # Once the lease is gone the edge can go (the claim ends with the lease row; verify names the log).
        with store.write_transaction(self.conn):
            self.conn.execute("DELETE FROM leases WHERE task_id = 'b'")
        store.remove_edge(self.conn, "b", "a", "draft")
        self.assertEqual(store.list_edges(self.conn), [])

    def test_removal_is_one_transaction_and_loses_the_lock_race_cleanly(self):
        self.tasks("a", "b")
        store.add_edge(self.conn, "b", "a", "draft")
        other = store.connect(self.proj, busy_timeout_ms=0)
        self.addCleanup(other.close)
        with store.write_transaction(other):
            self.assertEqual(code_of(store.remove_edge, self.conn, "b", "a", "draft"), errors.BUSY)
        self.assertEqual(len(store.list_edges(self.conn)), 1)
        store.remove_edge(self.conn, "b", "a", "draft")
        self.consistent()


class ReadTests(Base):
    def test_reads_refuse_bad_ids_and_return_empty_for_unknown_ones(self):
        self.tasks("a")
        for bad in ("A", "a\x00", "", None, 3, "x" * 65):
            with self.subTest(bad=bad):
                self.assertEqual(code_of(store.needs_of, self.conn, bad), errors.REJECTED)
                self.assertEqual(code_of(store.prerequisite_closure, self.conn, bad), errors.REJECTED)
        self.assertEqual(store.needs_of(self.conn, "ghost"), [])
        self.assertEqual(store.prerequisite_closure(self.conn, "ghost"), [])

    def test_reads_do_not_open_a_transaction(self):
        self.tasks("a", "b")
        store.add_edge(self.conn, "b", "a", "draft")
        store.list_edges(self.conn)
        store.needs_of(self.conn, "b")
        store.prerequisite_closure(self.conn, "b")
        self.assertFalse(self.conn.in_transaction)

    def test_a_closed_connection_raises_the_mapped_error_not_a_raw_one(self):
        self.tasks("a")
        self.conn.close()
        for call in (lambda: store.list_edges(self.conn), lambda: store.needs_of(self.conn, "a"),
                     lambda: store.prerequisite_closure(self.conn, "a")):
            with self.assertRaises(ShitError):
                call()


# -- differential test against the file-based graph --------------------------------------------------------------

def file_closure(graph, task_id):
    needs = {}
    for e in graph["edges"]:
        needs.setdefault(e["task_id"], set()).add(e["needs_id"])
    seen, queue = set(), deque([task_id])
    while queue:
        for nxt in needs.get(queue.popleft(), ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return sorted(seen)


def file_sorted(graph):
    return sorted(graph["edges"], key=lambda e: (e["task_id"], e["needs_id"], e["type"]))


class Differential(Base):
    """Random operation sequences applied to the store and to `shit.dependencies` (with the lease check that
    `shit.queue.remove_edge` adds on top) must accept and reject the same operations with the same exit code,
    and leave the same edges and closures."""

    NODES = ["a", "b", "c", "d", "e", "f", "g", "h"]
    GHOSTS = ["ghost", "z9"]
    HOSTILE = ["A", "a\x00b", "x" * 65, "", "a b", "../a", "a‮b", "a\n", 5, None, b"a", "x" * 5000, "a'--"]
    KINDS = list(dep.TYPES) * 3 + ["bogus", "", None, "DRAFT", 3]
    LEASED = ("c", "f")

    def setUp(self):
        super().setUp()
        self.tasks(*self.NODES)
        for task in self.LEASED:
            self.lease(task)
        self.known = set(self.NODES)

    def pick(self, rng):
        roll = rng.random()
        if roll < 0.80:
            return rng.choice(self.NODES)
        if roll < 0.88:
            return rng.choice(self.GHOSTS)
        return rng.choice(self.HOSTILE)

    def file_remove(self, graph, task_id, needs_id, kind):
        out = dep.remove_edge(graph, task_id, needs_id, kind)
        if task_id in self.LEASED:  # queue.remove_edge: the dependent holds a live lease
            raise ShitError(errors.ILLEGAL_TRANSITION)
        return out

    def run_one(self, graph, op, args):
        if op == "add":
            file_call = lambda: dep.add_edge(graph, *args, self.known)  # noqa: E731
            store_call = lambda: store.add_edge(self.conn, *args)  # noqa: E731
        elif op == "batch":
            file_call = lambda: dep.add_edges(graph, args, self.known)  # noqa: E731
            store_call = lambda: store.add_edges(self.conn, args)  # noqa: E731
        else:
            file_call = lambda: self.file_remove(graph, *args)  # noqa: E731
            store_call = lambda: store.remove_edge(self.conn, *args)  # noqa: E731
        try:
            new_graph, file_code = file_call(), None
        except ShitError as exc:
            new_graph, file_code = graph, exc.code
        return new_graph, file_code, code_of(store_call)

    def test_random_sequences_agree(self):
        rng = random.Random(62)
        accepted = rejected = 0
        codes = set()
        for seed in range(60):
            with store.write_transaction(self.conn):
                self.conn.execute("DELETE FROM edges")
            graph = dep.empty_graph()
            for step in range(70):
                roll = rng.random()
                if roll < 0.55:
                    op, args = "add", (self.pick(rng), self.pick(rng), rng.choice(self.KINDS))
                elif roll < 0.70:
                    op, args = "batch", [(self.pick(rng), self.pick(rng), rng.choice(self.KINDS))
                                         for _ in range(rng.randint(0, 4))]
                elif graph["edges"] and rng.random() < 0.8:
                    e = rng.choice(graph["edges"])
                    op, args = "remove", (e["task_id"], e["needs_id"], e["type"])
                    if rng.random() < 0.15:
                        args = (args[0], args[1], rng.choice(dep.TYPES))
                else:
                    op, args = "remove", (self.pick(rng), self.pick(rng), rng.choice(self.KINDS))
                graph, file_code, store_code = self.run_one(graph, op, args)
                context = (seed, step, op, args)
                self.assertEqual(store_code, file_code, context)
                codes.add(file_code)
                if file_code is None:
                    accepted += 1
                else:
                    rejected += 1
                self.assertEqual(store.list_edges(self.conn), file_sorted(graph), context)
                self.assertEqual(store.verify(self.conn), [], context)
                node = rng.choice(self.NODES)
                self.assertEqual(store.prerequisite_closure(self.conn, node), file_closure(graph, node), context)
                grouped = dep.group_by_task(graph["edges"])
                self.assertEqual(store.needs_of(self.conn, node), grouped.get(node, []), context)
        # The sequences exercised every outcome, so a database that accepts everything or nothing cannot pass.
        self.assertGreater(accepted, 300)
        self.assertGreater(rejected, 300)
        self.assertEqual(codes, {None, errors.REJECTED, errors.PRECONDITION, errors.ILLEGAL_TRANSITION})

    def test_every_node_pair_and_kind_agrees_on_a_dense_graph(self):
        graph = dep.empty_graph()
        for task in self.NODES:
            for needs in self.NODES:
                for kind in dep.TYPES:
                    graph, file_code, store_code = self.run_one(graph, "add", (task, needs, kind))
                    self.assertEqual(store_code, file_code, (task, needs, kind))
        self.assertEqual(store.list_edges(self.conn), file_sorted(graph))
        self.assertEqual(store.verify(self.conn), [])
        self.assertGreater(len(graph["edges"]), 20)
        for node in self.NODES:
            self.assertEqual(store.prerequisite_closure(self.conn, node), file_closure(graph, node))


if __name__ == "__main__":
    unittest.main()
