"""Dependency graph tests (issue #17): shape, cycles, missing nodes, readiness (Q-DEP-01 to Q-DEP-08)."""

import json
import unittest

from _bootstrap import ROOT  # noqa: F401  (puts the repository root on sys.path)

from shit import dependencies as dep
from shit.errors import ShitError


def edge(task, needs, kind="draft"):
    return {"task_id": task, "needs_id": needs, "type": kind}


def graph(*edges):
    return {"schema": "dependency-graph", "schema_version": 1, "edges": list(edges)}


class Base(unittest.TestCase):
    def code(self, fn, *a, **kw):
        with self.assertRaises(ShitError) as cm:
            fn(*a, **kw)
        return cm.exception.code


class Shape(Base):
    def test_empty_graph_is_valid(self):
        self.assertEqual(dep.validate_graph(dep.empty_graph()), dep.empty_graph())

    def test_unknown_key_and_version_rejected(self):
        g = graph()
        self.assertEqual(self.code(dep.validate_graph, {**g, "extra": 1}), 3)
        self.assertEqual(self.code(dep.validate_graph, {**g, "schema_version": 2}), 3)
        self.assertEqual(self.code(dep.validate_graph, {**g, "schema_version": True}), 3)
        self.assertEqual(self.code(dep.validate_graph, {**g, "schema": "task"}), 3)

    def test_bad_edge_shapes_rejected(self):
        for bad in (edge("a", "b", "other"), {"task_id": "a", "needs_id": "b"}, {**edge("a", "b"), "x": 1},
                    edge("A", "b"), edge("a", "../b"), edge("a", 5), "a->b"):
            self.assertEqual(self.code(dep.validate_graph, graph(bad)), 3, bad)

    def test_duplicate_edge_rejected_but_mixed_types_allowed(self):
        self.assertEqual(self.code(dep.validate_graph, graph(edge("a", "b"), edge("a", "b"))), 3)
        dep.validate_graph(graph(edge("a", "b", "draft"), edge("a", "b", "apply")))

    def test_hostile_characters_in_ids_rejected(self):
        for ident in ("a\u202eb", "a\x00b", "a b", "a\u200bb", "x" * 65):
            self.assertEqual(self.code(dep.validate_graph, graph(edge(ident, "b"))), 3)



def shard_bytes(task, *needs):
    return dep.dump_shard(task, [{"needs_id": n, "type": t} for n, t in needs])


class Shards(Base):
    def test_round_trip_is_deterministic_and_sorted(self):
        data = shard_bytes("a", ("z", "draft"), ("b", "apply"), ("b", "draft"))
        self.assertEqual(data, shard_bytes("a", ("b", "draft"), ("z", "draft"), ("b", "apply")))
        got = dep.load_shard(data, "a")
        self.assertEqual([(n["needs_id"], n["type"]) for n in got["needs"]],
                         [("b", "apply"), ("b", "draft"), ("z", "draft")])

    def test_file_name_and_record_must_agree(self):
        self.assertEqual(self.code(dep.load_shard, shard_bytes("a", ("b", "draft")), "c"), 3)

    def test_unknown_keys_version_duplicates_and_bad_ids_rejected(self):
        good = json.loads(shard_bytes("a", ("b", "draft")))
        for bad in ({**good, "extra": 1}, {**good, "schema_version": 2}, {**good, "schema": "task"},
                    {**good, "needs": "b"}, {**good, "needs": [{"needs_id": "b"}]},
                    {**good, "needs": [{"needs_id": "B", "type": "draft"}]},
                    {**good, "needs": [{"needs_id": "b", "type": "other"}]},
                    {**good, "needs": [{"needs_id": "b", "type": "draft"}] * 2},
                    {**good, "needs": [{"needs_id": "b", "type": "draft", "x": 1}]}):
            self.assertEqual(self.code(dep.load_shard, json.dumps(bad).encode(), "a"), 3, bad)

    def test_duplicate_json_key_depth_and_size(self):
        self.assertEqual(self.code(dep.load_shard, b'{"schema":"x","schema":"dependency-shard"}', "a"), 3)
        self.assertEqual(self.code(dep.load_shard, b'{"a":[[[[[[[[[1]]]]]]]]]}', "a"), 7)
        self.assertEqual(self.code(dep.load_shard, b" " * 70000, "a"), 7)

    def test_per_task_cap(self):
        many = [{"needs_id": f"p{i}", "type": "draft"} for i in range(dep.MAX_NEEDS_PER_TASK + 1)]
        self.assertEqual(self.code(dep.dump_shard, "a", many), 7)
        within = many[:dep.MAX_NEEDS_PER_TASK]
        self.assertLess(len(dep.dump_shard("a", within)), 65536)  # a full shard stays under the record limit

    def test_add_edge_enforces_the_per_task_cap(self):
        known = {"a"} | {f"p{i}" for i in range(dep.MAX_NEEDS_PER_TASK + 1)}
        g = dep.empty_graph()
        for i in range(dep.MAX_NEEDS_PER_TASK):
            g = dep.add_edge(g, "a", f"p{i}", "draft", known)
        self.assertEqual(self.code(dep.add_edge, g, "a", f"p{dep.MAX_NEEDS_PER_TASK}", "draft", known), 7)

    def test_edges_group_into_shards_and_back(self):
        edges = [edge("a", "b"), edge("a", "c", "apply"), edge("d", "a")]
        shards = dep.group_by_task(edges)
        self.assertEqual(sorted(shards), ["a", "d"])
        again = [e for t in sorted(shards) for e in dep.edges_of(t, shards[t])]
        self.assertEqual(sorted(map(json.dumps, again)), sorted(map(json.dumps, edges)))

    def test_add_edges_is_all_or_nothing(self):
        known = {"a", "b", "c"}
        g = dep.add_edge(dep.empty_graph(), "b", "a", "draft", known)
        out = dep.add_edges(g, [("c", "b", "draft"), ("a", "c", "draft")][:1], known)
        self.assertEqual(len(out["edges"]), 2)
        before = json.dumps(g)
        self.assertEqual(self.code(dep.add_edges, g, [("c", "b", "draft"), ("a", "c", "draft")], known), 3)
        self.assertEqual(json.dumps(g), before)


class Cycles(Base):
    def test_find_cycle(self):
        self.assertFalse(dep.find_cycle([]))
        self.assertFalse(dep.find_cycle([edge("a", "b"), edge("b", "c"), edge("a", "c", "apply")]))
        self.assertTrue(dep.find_cycle([edge("a", "a")]))
        self.assertTrue(dep.find_cycle([edge("a", "b"), edge("b", "a", "apply")]))  # all edge types combined

    def test_long_chain_cycle_without_recursion(self):
        n = 400
        chain = [edge(f"t{i}", f"t{i + 1}") for i in range(n)]
        self.assertFalse(dep.find_cycle(chain))
        self.assertTrue(dep.find_cycle([*chain, edge(f"t{n}", "t0")]))

    def test_add_refuses_self_two_node_and_long_cycles_without_change(self):
        known = {"a", "b", "c", "d"}
        base = graph(edge("a", "b"), edge("b", "c"), edge("c", "d"))
        before = json.dumps(base)
        for task, needs in (("a", "a"), ("b", "a"), ("d", "a")):
            self.assertEqual(self.code(dep.add_edge, base, task, needs, "apply", known), 3, (task, needs))
        self.assertEqual(json.dumps(base), before)

    def test_add_unknown_node_is_precondition(self):
        self.assertEqual(self.code(dep.add_edge, graph(), "a", "ghost", "draft", {"a"}), 9)
        self.assertEqual(self.code(dep.add_edge, graph(), "ghost", "a", "draft", {"a"}), 9)

    def test_add_duplicate_rejected_and_mixed_types_accepted(self):
        known = {"a", "b"}
        g = dep.add_edge(graph(), "a", "b", "draft", known)
        self.assertEqual(self.code(dep.add_edge, g, "a", "b", "draft", known), 3)
        g2 = dep.add_edge(g, "a", "b", "review", known)
        self.assertEqual(len(g2["edges"]), 2)
        self.assertEqual(len(g["edges"]), 1)  # the input is never mutated

    def test_add_bad_endpoint_is_rejected_before_lookup(self):
        self.assertEqual(self.code(dep.add_edge, graph(), "../x", "a", "draft", {"a"}), 3)

    def test_remove_edge(self):
        g = graph(edge("a", "b"), edge("a", "b", "apply"))
        out = dep.remove_edge(g, "a", "b", "draft")
        self.assertEqual(out["edges"], [edge("a", "b", "apply")])
        self.assertEqual(self.code(dep.remove_edge, g, "a", "c", "draft"), 9)


class Nodes(Base):
    def test_missing_nodes(self):
        edges = [edge("a", "b"), edge("c", "a")]
        self.assertEqual(dep.missing_nodes(edges, {"a", "b", "c"}), set())
        self.assertEqual(dep.missing_nodes(edges, {"a", "b"}), {"c"})


class Readiness(Base):
    def test_only_blocking_types_hold_a_task(self):
        edges = [edge("c", "b"), edge("b", "a"), edge("c", "x", "apply")]
        met = {("b", "a", "draft")}
        unmet = lambda t: [(e["needs_id"], e["type"]) for e in
                           dep.unmet_blocking(edges, t, lambda e: (e["task_id"], e["needs_id"], e["type"]) in met)]
        self.assertEqual(unmet("b"), [])
        self.assertEqual(unmet("c"), [("b", "draft")])  # the apply edge to x never blocks (Q-DEP-02)
        self.assertEqual(unmet("a"), [])

    def test_chain_becomes_ready_only_in_order(self):
        edges = [edge("c", "b"), edge("b", "a")]
        done = set()
        met = lambda e: e["needs_id"] in done
        self.assertEqual([t for t in "abc" if not dep.unmet_blocking(edges, t, met)], ["a"])
        done.add("a")
        self.assertEqual([t for t in "abc" if not dep.unmet_blocking(edges, t, met)], ["a", "b"])
        done.add("b")
        self.assertEqual([t for t in "abc" if not dep.unmet_blocking(edges, t, met)], ["a", "b", "c"])

    def test_apply_prerequisites(self):
        edges = [edge("b", "a", "apply"), edge("b", "c", "draft")]
        self.assertEqual(dep.apply_prerequisites(edges, "b"), ["a"])


if __name__ == "__main__":
    unittest.main()
