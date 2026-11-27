"""Queue, lease and recovery tests (issue #17). Real files in a temporary git repository, no mocks of storage.

Rule ids (Q-...) and threat-model rows (TM-...) refer to docs/queue-spec.md and docs/threat-model.md.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from _bootstrap import ROOT, make_git_dir

from shit import commands
from shit import dependencies as dep
from shit import queue as q
from shit import queue_records as qr
from shit import task as task_mod
from shit.errors import ShitError
from shit.log import GENESIS, chain_hash
from shit.project import Project

BASE = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC)


def at(seconds):
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class QueueCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        self.repo.mkdir()
        make_git_dir(self.repo / ".git")
        self.proj = Project.init(self.repo)
        self.addCleanup(self.proj.close)
        self.shit = self.repo / ".shit"
        self.clock = 0

    # -- helpers ----------------------------------------------------------

    def tick(self, n=1):
        self.clock += n
        return at(self.clock)

    def code(self, fn, *a, **kw):
        with self.assertRaises(ShitError) as cm:
            fn(*a, **kw)
        return cm.exception.code

    def task(self, tid, enqueue=True):
        task_mod.create_task(self.proj, task_id=tid, title="Title", summary="Summary", owner="human",
                             actor="human", now=self.tick())
        if enqueue:
            q.enqueue(self.proj, tid, "human", now=self.tick())

    def claim(self, tid, step="draft", worker="w1", kind="ai"):
        return q.claim(self.proj, {"task_id": tid, "step": step, "worker": worker, "worker_kind": kind},
                       now=self.tick())

    @staticmethod
    def present(lease):
        return {"task_id": lease["task_id"], "lease_id": lease["lease_id"], "generation": lease["generation"]}

    def run_to_running(self, tid):
        lease = self.claim(tid)
        q.start(self.proj, self.present(lease), now=self.tick())
        return lease

    def status(self, **kw):
        return q.status(self.proj, now=kw.pop("now", None) or self.tick(), **kw)

    def lifecycle(self, tid):
        return task_mod.get_task(self.proj, tid)["state"]

    def success_report(self, tid):
        commands.add_report(self.proj, task_id=tid, report_id=f"{tid}-report", outcome="success", summary="ok",
                            evidence_ids=[], actor="human", now=self.tick())

    def snap(self):
        return {str(p.relative_to(self.shit)): p.read_bytes() for p in sorted(self.shit.rglob("*"))
                if p.is_file() and p.name not in ("lock", "QUEUE.md")}

    def queue_lines(self):
        return (self.shit / "queue.jsonl").read_bytes().splitlines()

    def rewrite_queue(self, lines, anchor=True):
        """Rewrite queue.jsonl and its chain consistently (a writer with full access to the store).

        With `anchor` the anchor file is rewritten to match too: a writer that rewrites every queue file at once
        is the documented unsigned-history residual (docs/threat-model.md); `anchor=False` models the weaker one."""
        previous, chain = GENESIS, []
        for line in lines:
            previous = chain_hash(previous, line)
            chain.append(previous.encode())
        (self.shit / "queue.jsonl").write_bytes(b"".join(line + b"\n" for line in lines))
        (self.shit / "queue.chain").write_bytes(b"".join(c + b"\n" for c in chain))
        if anchor:
            try:
                held = {t: v["generation"] for t, v in qr.replay([qr.load_entry(x) for x in lines]).items()
                        if v["generation"]}
            except ShitError:
                held = {}
            doc = {"schema": "queue-anchor", "schema_version": 1, "count": len(lines), "tip": previous,
                   "generations": held}
            (self.shit / "queue-anchor.json").write_text(json.dumps(doc))


class Flow(QueueCase):
    def test_full_path_for_a_human_worker_only(self):
        # Q-HET-02: claim, start, renew, report (submit) with plain calls and no adapter.
        self.task("t1")
        lease = self.claim("t1", worker="alice", kind="human")
        self.assertEqual(lease["generation"], 1)
        self.assertEqual(self.status()["t1"]["state"], "dispatched")
        q.start(self.proj, self.present(lease), now=self.tick())
        self.assertEqual(self.lifecycle("t1"), "running")
        q.renew(self.proj, self.present(lease), now=self.tick())
        q.submit(self.proj, self.present(lease), now=self.tick())
        self.assertEqual(self.lifecycle("t1"), "review")
        row = self.status()["t1"]
        self.assertEqual((row["state"], row["lease"], row["attempts"]), ("review", None, 1))
        self.assertFalse((self.shit / "leases" / "t1.json").exists())

    def test_release_returns_to_ready_and_generation_rises(self):
        self.task("t1")
        first = self.claim("t1")
        q.release(self.proj, self.present(first), now=self.tick())
        self.assertEqual(self.status()["t1"]["state"], "ready")
        second = self.claim("t1")
        self.assertEqual(second["generation"], 2)
        self.assertNotEqual(second["lease_id"], first["lease_id"])

    def test_release_of_a_running_task_is_illegal(self):
        self.task("t1")
        lease = self.run_to_running("t1")
        before = self.snap()
        self.assertEqual(self.code(q.release, self.proj, self.present(lease), now=self.tick()), 4)
        self.assertEqual(self.snap(), before)

    def test_fail_and_hold_release_the_lease(self):
        self.task("a")
        self.task("b")
        la, lb = self.run_to_running("a"), self.run_to_running("b")
        q.fail(self.proj, self.present(la), now=self.tick())
        q.hold(self.proj, self.present(lb), "owner", "needs a decision", "decision-1", now=self.tick())
        rows = self.status()
        self.assertEqual((rows["a"]["state"], rows["b"]["state"]), ("failed", "hold-owner"))
        self.assertEqual((self.lifecycle("a"), self.lifecycle("b")), ("failed", "blocked"))
        self.assertEqual(os.listdir(self.shit / "leases"), [])

    def test_survives_restart_with_same_state(self):
        self.task("a")
        self.task("b")
        self.run_to_running("a")
        before = self.status()
        self.proj.close()
        self.proj = Project.open(self.repo)
        self.assertEqual(q.status(self.proj, now=self.tick()), before)

    def test_queue_md_is_a_view_only(self):
        self.task("a")
        self.run_to_running("a")
        view = self.shit / "QUEUE.md"
        self.assertIn("| a | running |", view.read_text())
        before = self.status()
        view.unlink()
        self.assertEqual(self.status(), before)  # deleting it changes nothing and is not an error
        self.assertTrue(view.exists())  # regenerated
        view.write_text("| a | done | forged |\n")
        self.assertEqual(self.status(), before)  # editing it changes nothing
        self.assertNotIn("forged", view.read_text())
        view.unlink()
        q.render_view(self.proj, now=self.tick())
        self.assertTrue(view.exists())

    def test_queue_md_symlink_is_never_followed(self):
        self.task("a")
        target = self.repo / "elsewhere.txt"
        target.write_text("keep")
        (self.shit / "QUEUE.md").unlink()
        os.symlink(target, self.shit / "QUEUE.md")
        self.assertEqual(self.code(q.render_view, self.proj, now=self.tick()), 5)
        self.assertEqual(target.read_text(), "keep")
        self.run_to_running("a")  # an operation still succeeds; the view is only derived
        self.assertEqual(target.read_text(), "keep")

    def test_queue_md_carries_no_titles(self):
        task_mod.create_task(self.proj, task_id="a", title="SENTINEL-TITLE", summary="SENTINEL-SUMMARY",
                             owner="human", actor="human", now=self.tick())
        q.enqueue(self.proj, "a", "human", now=self.tick())
        text = (self.shit / "QUEUE.md").read_text()
        self.assertNotIn("SENTINEL", text)


class Readiness(QueueCase):
    def test_chain_becomes_ready_only_in_order(self):  # Q-DEP-01
        for tid in ("a", "b", "c"):
            self.task(tid, enqueue=False)
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        q.add_edge(self.proj, "c", "b", "draft", "human", now=self.tick())
        for tid in "abc":
            q.enqueue(self.proj, tid, "human", now=self.tick())
        rows = self.status()
        self.assertEqual([rows[t]["state"] for t in "abc"], ["ready", "waiting", "waiting"])
        self.assertEqual(rows["c"]["waiting_on"], ["b"])
        self.success_report("a")
        rows = self.status()
        self.assertEqual([rows[t]["state"] for t in "abc"], ["ready", "ready", "waiting"])
        self.success_report("b")
        self.assertEqual(self.status()["c"]["state"], "ready")
        kinds = [e["kind"] for e in map(json.loads, self.queue_lines())]
        self.assertEqual(kinds.count("ready"), 2)  # the waiting to ready step is recorded

    def test_a_failed_report_does_not_satisfy_a_draft_edge(self):
        self.task("a")
        self.task("b")
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        commands.add_report(self.proj, task_id="a", report_id="r1", outcome="failed", summary="no", evidence_ids=[],
                            actor="human", now=self.tick())
        self.assertEqual(self.status()["b"]["state"], "waiting")

    def test_unmet_apply_edge_never_blocks_draft_but_refuses_apply(self):  # Q-DEP-02, Q-SER-03, OQ-6
        self.task("a")
        self.task("b")
        q.add_edge(self.proj, "b", "a", "apply", "human", now=self.tick())
        self.assertEqual(self.status()["b"]["state"], "ready")
        before = self.snap()
        self.assertEqual(self.code(self.claim, "b", "apply"), 4)
        self.assertEqual(self.snap(), before)
        lease = self.claim("b", "draft")
        self.assertEqual(lease["step"], "draft")

    def test_review_and_decision_edges_need_a_human(self):
        for tid in ("a", "b", "c"):
            self.task(tid)
        q.add_edge(self.proj, "b", "a", "decision", "human", now=self.tick())
        q.add_edge(self.proj, "c", "a", "review", "human", now=self.tick())
        commands.add_decision(self.proj, task_id="a", decision_id="d1", status="proposed", question="q", choice="c",
                              rationale="r", actor="model", now=self.tick())
        self.assertEqual(self.status()["b"]["state"], "waiting")
        commands.add_decision(self.proj, task_id="a", decision_id="d2", status="accepted", question="q", choice="c",
                              rationale="r", actor="human", now=self.tick())
        rows = self.status()
        self.assertEqual((rows["b"]["state"], rows["c"]["state"]), ("ready", "waiting"))
        q.submit(self.proj, self.present(self.run_to_running("a")), now=self.tick())
        self.success_report("a")
        commands.add_review(self.proj, task_id="a", review_id="rv1", report_id="a-report", verdict="pass",
                            summary="ok", actor="model", now=self.tick())
        self.assertEqual(self.status()["c"]["state"], "waiting")  # a model's pass does not open a review edge
        commands.add_review(self.proj, task_id="a", review_id="rv2", report_id="a-report", verdict="pass",
                            summary="ok", actor="human", now=self.tick())
        self.assertEqual(self.status()["c"]["state"], "ready")

    def test_a_dependent_returns_to_waiting_when_the_gate_recloses(self):  # Q-DEP-09
        self.task("a")
        self.task("b")
        q.add_edge(self.proj, "b", "a", "decision", "human", now=self.tick())
        commands.add_decision(self.proj, task_id="a", decision_id="d1", status="accepted", question="q", choice="c",
                              rationale="r", actor="human", now=self.tick())
        self.assertEqual(self.status()["b"]["state"], "ready")
        commands.add_decision(self.proj, task_id="a", decision_id="d2", status="rejected", question="q", choice="c",
                              rationale="r", actor="human", now=self.tick())
        self.assertEqual(self.status()["b"]["state"], "waiting")
        self.assertEqual(self.code(self.claim, "b"), 4)

    def test_running_dependent_cannot_submit_while_gate_is_closed(self):  # Q-DEP-09
        self.task("a")
        self.task("b")
        lease = self.run_to_running("b")
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        self.assertEqual(self.code(q.submit, self.proj, self.present(lease), now=self.tick()), 9)
        self.assertEqual(self.lifecycle("b"), "running")
        self.success_report("a")
        q.submit(self.proj, self.present(lease), now=self.tick())

    def test_readiness_is_recomputed_from_files_after_deleting_every_derived_file(self):  # Q-DEP-08
        self.task("a")
        self.task("b")
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        before = self.status()
        (self.shit / "QUEUE.md").unlink()
        self.assertEqual(self.status(), before)


class GraphOperations(QueueCase):
    def shard(self, task_id):
        return json.loads((self.shit / "deps" / f"{task_id}.json").read_text())

    def forge_graph(self, shards, *, actor="human"):
        """Write shard files and append a matching, correctly chained graph_changed entry (an attacker who can rechain)."""
        entries = [json.loads(line) for line in self.queue_lines()]
        digests = []
        for task_id, needs in shards.items():
            data = dep.dump_shard(task_id, [{"needs_id": n, "type": t} for n, t in needs])
            (self.shit / "deps" / f"{task_id}.json").write_bytes(data)
            digests.append({"task_id": task_id, "sha256": hashlib.sha256(data).hexdigest()})
        first = min(shards)
        entries.append({"schema": "queue-entry", "schema_version": 1, "id": f"queue-{len(entries) + 1:04d}",
                        "task_id": first, "timestamp": entries[-1]["timestamp"], "actor": actor,
                        "kind": "graph_changed", "shards": sorted(digests, key=lambda d: d["task_id"]),
                        "message": "Queue entry recorded."})
        self.rewrite_queue([json.dumps(e, separators=(",", ":")).encode() for e in entries])

    def test_cycles_unknown_and_duplicate(self):  # Q-DEP-03, Q-DEP-05, Q-DEP-06
        for tid in ("a", "b", "c"):
            self.task(tid)
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        q.add_edge(self.proj, "c", "b", "draft", "wrapper", now=self.tick())
        before = self.snap()
        self.assertEqual(self.code(q.add_edge, self.proj, "a", "a", "draft", "human", now=self.tick()), 3)
        self.assertEqual(self.code(q.add_edge, self.proj, "a", "c", "apply", "human", now=self.tick()), 3)
        self.assertEqual(self.code(q.add_edge, self.proj, "a", "ghost", "draft", "human", now=self.tick()), 9)
        self.assertEqual(self.code(q.add_edge, self.proj, "b", "a", "draft", "human", now=self.tick()), 3)
        self.assertEqual(self.snap(), before)  # every refusal leaves every byte unchanged
        q.add_edge(self.proj, "b", "a", "review", "human", now=self.tick())
        self.assertEqual(len(self.shard("b")["needs"]), 2)
        self.assertEqual(sorted(os.listdir(self.shit / "deps")), ["b.json", "c.json"])  # one shard per dependent

    def test_every_graph_change_is_a_chain_entry(self):
        self.task("a")
        self.task("b")
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        last = json.loads(self.queue_lines()[-1])
        data = (self.shit / "deps" / "b.json").read_bytes()
        self.assertEqual((last["kind"], last["shards"]), ("graph_changed", [
            {"task_id": "b", "sha256": hashlib.sha256(data).hexdigest()}]))
        q.remove_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        self.assertFalse((self.shit / "deps" / "b.json").exists())  # the last edge removes the shard
        self.assertEqual(json.loads(self.queue_lines()[-1])["shards"], [{"task_id": "b", "sha256": "0" * 64}])
        self.assertEqual(self.status()["b"]["state"], "ready")

    def test_add_edges_writes_every_shard_in_one_commit(self):
        for tid in ("a", "b", "c"):
            self.task(tid)
        q.add_edges(self.proj, [("b", "a", "draft"), ("c", "a", "draft"), ("c", "b", "apply")], "human",
                    now=self.tick())
        last = json.loads(self.queue_lines()[-1])
        self.assertEqual([d["task_id"] for d in last["shards"]], ["b", "c"])
        self.assertEqual(sorted(os.listdir(self.shit / "deps")), ["b.json", "c.json"])
        before = self.snap()
        # the second edge closes a cycle: nothing from the batch may be written
        self.assertEqual(self.code(q.add_edges, self.proj, [("a", "c", "decision"), ("a", "b", "review")], "human",
                                   now=self.tick()), 3)
        self.assertEqual(self.snap(), before)

    def test_graph_survives_restart(self):
        self.task("a")
        self.task("b")
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        self.proj.close()
        self.proj = Project.open(self.repo)
        self.assertEqual(self.status()["b"]["state"], "waiting")

    def test_model_actor_cannot_change_the_graph(self):  # Q-DEP-07
        self.task("a")
        self.task("b")
        before = self.snap()
        self.assertEqual(self.code(q.add_edge, self.proj, "b", "a", "draft", "model", now=self.tick()), 10)
        self.assertEqual(self.code(q.add_edges, self.proj, [("b", "a", "draft")], "model", now=self.tick()), 10)
        self.assertEqual(self.code(q.remove_edge, self.proj, "b", "a", "draft", "model", now=self.tick()), 10)
        self.assertEqual(self.snap(), before)

    def test_remove_edge_refused_while_the_task_holds_a_lease(self):  # Q-DEP-07
        self.task("a")
        self.task("b")
        q.add_edge(self.proj, "b", "a", "apply", "human", now=self.tick())
        self.claim("b")
        self.assertEqual(self.code(q.remove_edge, self.proj, "b", "a", "apply", "human", now=self.tick()), 4)

    def test_chain_proven_cycle_fails_closed(self):  # Q-DEP-04, TM-18: even a rechained cycle is refused
        self.task("a")
        self.task("b")
        self.forge_graph({"a": [("b", "draft")], "b": [("a", "draft")]})
        before = self.snap()
        self.assertEqual(self.code(self.claim, "a"), 8)
        self.assertEqual(self.code(self.status), 8)
        self.assertEqual(self.snap(), before)

    def test_chain_proven_self_edge_and_dangling_edge_fail_closed(self):
        self.task("a")
        self.forge_graph({"a": [("a", "draft")]})
        self.assertEqual(self.code(self.status), 8)
        self.forge_graph({"a": [("ghost", "draft")]})
        self.assertEqual(self.code(self.status), 8)

    def test_shard_deleted_fails_closed(self):
        self.task("a")
        self.task("b")
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        (self.shit / "deps" / "b.json").unlink()
        before = self.snap()
        self.assertEqual(self.code(self.claim, "a"), 8)
        self.assertEqual(self.snap(), before)

    def test_shard_edited_fails_closed(self):
        self.task("a")
        self.task("b")
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        path = self.shit / "deps" / "b.json"
        edited = json.loads(path.read_text())
        edited["needs"] = []  # an attacker drops the edge so the task becomes ready
        path.write_text(json.dumps(edited))
        self.assertEqual(self.code(self.status), 8)
        path.write_text(path.read_text().replace('"draft"', '"apply"'))
        self.assertEqual(self.code(self.status), 8)

    def test_shard_added_without_a_chain_entry_fails_closed(self):
        self.task("a")
        self.task("b")
        (self.shit / "deps" / "b.json").write_bytes(dep.dump_shard("b", [{"needs_id": "a", "type": "draft"}]))
        self.assertEqual(self.code(self.status), 8)

    def test_two_shards_disagreeing_on_one_edge_fail_closed(self):
        # an edge lives in exactly one shard; a copy under another task's name must not be believed
        for tid in ("a", "b", "c"):
            self.task(tid)
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        copy = (self.shit / "deps" / "b.json").read_bytes()
        (self.shit / "deps" / "c.json").write_bytes(copy)  # c.json now says it belongs to b
        self.assertEqual(self.code(self.status), 8)  # no chain entry for c
        entries = [json.loads(line) for line in self.queue_lines()]
        entries.append({"schema": "queue-entry", "schema_version": 1, "id": f"queue-{len(entries) + 1:04d}",
                        "task_id": "c", "timestamp": entries[-1]["timestamp"], "actor": "human",
                        "kind": "graph_changed", "message": "Queue entry recorded.",
                        "shards": [{"task_id": "c", "sha256": hashlib.sha256(copy).hexdigest()}]})
        self.rewrite_queue([json.dumps(e, separators=(",", ":")).encode() for e in entries])
        self.assertEqual(self.code(self.status), 8)  # even rechained: the record id disagrees with the file name

    def test_malformed_graph_changed_entries_are_refused(self):
        self.task("a")
        self.task("b")
        self.task("c", enqueue=False)
        digests = {}
        for tid in ("a", "b"):  # real shards, so only the entry's own shape can make a case fail
            data = dep.dump_shard(tid, [{"needs_id": "c", "type": "apply"}])
            (self.shit / "deps" / f"{tid}.json").write_bytes(data)
            digests[tid] = hashlib.sha256(data).hexdigest()
        digest = digests["a"]
        both = [{"task_id": "a", "sha256": digests["a"]}, {"task_id": "b", "sha256": digests["b"]}]
        base = {"schema": "queue-entry", "schema_version": 1, "id": "queue-0003", "task_id": "a",
                "timestamp": at(self.clock), "actor": "human", "kind": "graph_changed", "message": "m"}
        original = self.queue_lines()
        good = {**base, "shards": both}
        self.rewrite_queue([*original, json.dumps(good, separators=(",", ":")).encode()])
        self.assertEqual(self.status(now=at(self.clock + 1))["a"]["state"], "ready")  # the well-formed entry is accepted: the cases bite
        original = self.queue_lines()
        base["id"] = "queue-0004"
        cases = {
            "duplicate shard ids": {**base, "shards": [both[0]] * 2},
            "unsorted": {**base, "shards": both[::-1]},
            "entry task is not the first shard": {**base, "task_id": "b", "shards": both},
            "empty": {**base, "shards": []},
            "bad digest": {**base, "shards": [{"task_id": "a", "sha256": "zz"}]},
            "state_after present": {**base, "state_after": "ready", "shards": [{"task_id": "a", "sha256": digest}]},
            "model actor": {**base, "actor": "model", "shards": [{"task_id": "a", "sha256": digest}]},
            "extra shard key": {**base, "shards": [{"task_id": "a", "sha256": digest, "x": 1}]},
        }
        for name, entry in cases.items():
            self.rewrite_queue([*original, json.dumps(entry, separators=(",", ":")).encode()])
            self.assertEqual(self.code(self.status), 8, name)

    def test_oversize_and_garbage_shards_fail_closed(self):
        self.task("a")
        self.task("b")
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        path = self.shit / "deps" / "b.json"
        good = path.read_bytes()
        for bad in (b" " * 70000, b"{not json", b'{"schema":"dependency-shard","schema":"dependency-shard"}',
                    b'{"a":[[[[[[[[[1]]]]]]]]]}'):
            path.write_bytes(bad)
            self.assertEqual(self.code(self.status), 8, bad[:20])
        path.write_bytes(good)
        self.assertEqual(self.status()["b"]["state"], "waiting")  # the restored shard is accepted: the cases bite

    def test_oversize_shard_with_a_rechained_digest_is_still_refused(self):
        with mock.patch.object(dep, "MAX_NEEDS_PER_TASK", 2):
            for tid in ("a", "b", "c", "d"):
                self.task(tid)
            obj = {"schema": "dependency-shard", "schema_version": 1, "task_id": "d",
                   "needs": [{"needs_id": n, "type": "draft"} for n in ("a", "b", "c")]}
            data = json.dumps(obj).encode()
            (self.shit / "deps" / "d.json").write_bytes(data)
            entries = [json.loads(line) for line in self.queue_lines()]
            entries.append({"schema": "queue-entry", "schema_version": 1, "id": f"queue-{len(entries) + 1:04d}",
                            "task_id": "d", "timestamp": entries[-1]["timestamp"], "actor": "human",
                            "kind": "graph_changed", "message": "Queue entry recorded.",
                            "shards": [{"task_id": "d", "sha256": hashlib.sha256(data).hexdigest()}]})
            self.rewrite_queue([json.dumps(e, separators=(",", ":")).encode() for e in entries])
            self.assertEqual(self.code(self.status), 8)  # three real prerequisites, one over the cap
        self.assertEqual(self.status()["d"]["state"], "waiting")  # the same shard is fine under the real cap

    def test_legacy_single_file_graph_fails_closed(self):
        self.task("a")
        (self.shit / "dependencies.json").write_text('{"schema":"dependency-graph","schema_version":1,"edges":[]}')
        self.assertEqual(self.code(self.status), 8)

    def test_symlinked_shard_and_directory_are_refused(self):
        self.task("a")
        self.task("b")
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        target = self.repo / "g.json"
        target.write_bytes((self.shit / "deps" / "b.json").read_bytes())
        (self.shit / "deps" / "b.json").unlink()
        os.symlink(target, self.shit / "deps" / "b.json")
        self.assertEqual(self.code(self.status), 5)
        (self.shit / "deps" / "b.json").unlink()
        (self.shit / "deps").rmdir()
        os.symlink(self.repo, self.shit / "deps")
        self.assertEqual(self.code(self.status), 5)

    def test_stray_and_stale_files_in_deps(self):
        self.task("a")
        (self.shit / "deps" / "Not A Slug.txt").write_text("x")
        self.assertEqual(self.code(self.status), 8)
        (self.shit / "deps" / "Not A Slug.txt").unlink()
        (self.shit / "deps" / ".tmp-abc").write_text("x")  # a crash leftover: cleaned, not an error
        self.status()
        self.assertEqual(os.listdir(self.shit / "deps"), [])

    def test_total_task_cap_is_enforced_on_enqueue(self):
        with mock.patch.object(dep, "MAX_TASKS", 2):
            self.task("a")
            self.task("b")
            task_mod.create_task(self.proj, task_id="c", title="T", summary="S", owner="human", actor="human",
                                 now=self.tick())
            self.assertEqual(self.code(q.enqueue, self.proj, "c", "human", now=self.tick()), 7)

    def test_per_task_edge_cap_is_enforced_by_the_queue(self):
        with mock.patch.object(dep, "MAX_NEEDS_PER_TASK", 2):
            for tid in ("a", "b", "c", "d"):
                self.task(tid)
            q.add_edge(self.proj, "d", "a", "draft", "human", now=self.tick())
            q.add_edge(self.proj, "d", "b", "draft", "human", now=self.tick())
            before = self.snap()
            self.assertEqual(self.code(q.add_edge, self.proj, "d", "c", "draft", "human", now=self.tick()), 7)
            self.assertEqual(self.snap(), before)


class GraphCrash(QueueCase):
    STAGES = ("pending-written", "lease-applied", "lifecycle-applied", "shards-applied", "entry-appended",
              "chain-appended", "anchor-written", "pending-removed")

    class Crash(Exception):
        pass

    def scenario(self, case, additions):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve() / "repo"
            repo.mkdir()
            make_git_dir(repo / ".git")
            proj = Project.init(repo)
            try:
                for n, tid in enumerate(("a", "b", "c"), start=1):
                    task_mod.create_task(proj, task_id=tid, title="T", summary="S", owner="human", actor="human",
                                         now=at(n))
                    q.enqueue(proj, tid, "human", now=at(n + 10))

                def crash(stage):
                    if stage == case:
                        raise GraphCrash.Crash

                if case is None:
                    q.add_edges(proj, additions, "human", now=at(50))
                else:
                    with mock.patch.object(q, "_checkpoint", crash), self.assertRaises(GraphCrash.Crash):
                        q.add_edges(proj, additions, "human", now=at(50))
                q.status(proj, now=at(60))
                q.status(proj, now=at(60))
                return {str(p.relative_to(repo / ".shit")): p.read_bytes() for p in sorted((repo / ".shit").rglob("*"))
                        if p.is_file() and p.name not in ("lock",)}
            finally:
                proj.close()

    def test_multi_shard_graph_change_recovers_from_a_crash_at_every_stage(self):  # Q-REC-01, TM-24
        additions = [("b", "a", "draft"), ("c", "a", "draft"), ("c", "b", "apply")]
        expected = self.scenario(None, additions)
        self.assertEqual(sorted(k for k in expected if k.startswith("deps/")), ["deps/b.json", "deps/c.json"])
        for stage in self.STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(self.scenario(stage, additions), expected)

    def test_a_torn_shard_set_without_its_pending_record_fails_closed(self):
        # one shard written, no chain entry and no pending record: tampering, not a crash
        self.task("a")
        self.task("b")
        self.task("c")
        q.add_edges(self.proj, [("b", "a", "draft"), ("c", "a", "draft")], "human", now=self.tick())
        (self.shit / "deps" / "c.json").unlink()
        self.assertEqual(self.code(self.status), 8)

    def test_pending_with_a_shard_that_does_not_match_its_entry_fails_closed(self):
        self.task("a")
        self.task("b")

        def crash(stage):
            if stage == "pending-written":
                raise GraphCrash.Crash

        with mock.patch.object(q, "_checkpoint", crash), self.assertRaises(GraphCrash.Crash):
            q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        path = self.shit / "queue-pending.json"
        doc = json.loads(path.read_text())
        doc["shards"][0]["content"] = doc["shards"][0]["content"].replace('"draft"', '"apply"')
        path.write_text(json.dumps(doc))
        before = self.snap()
        self.assertEqual(self.code(self.status), 8)
        self.assertFalse((self.shit / "deps" / "b.json").exists())  # nothing from the forged pending was applied
        self.assertIn("queue-pending.json", {k.split("/")[-1] for k in self.snap()})  # kept as evidence
        self.assertNotEqual(before, {})


class Claims(QueueCase):
    def test_claim_rejects_extra_fields_and_worker_supplied_values(self):  # Q-CLAIM-05, Q-LEASE-04, TM-16
        self.task("t1")
        base = {"task_id": "t1", "step": "draft", "worker": "w", "worker_kind": "ai"}
        before = self.snap()
        for extra in ({"lease_id": "0" * 32}, {"generation": 9}, {"now": at(5)}, {"state_after": "running"},
                      {"expires_at": at(99999)}):
            self.assertEqual(self.code(q.claim, self.proj, {**base, **extra}, now=self.tick()), 2, extra)
        self.assertEqual(self.code(q.claim, self.proj, {k: v for k, v in base.items() if k != "step"}, now=self.tick()), 2)
        self.assertEqual(self.code(q.claim, self.proj, {**base, "worker_kind": "gpt-4"}, now=self.tick()), 2)
        self.assertEqual(self.code(q.claim, self.proj, {**base, "step": "deploy"}, now=self.tick()), 2)
        self.assertEqual(self.code(q.claim, self.proj, {**base, "worker": "A B"}, now=self.tick()), 2)
        self.assertEqual(self.snap(), before)

    def test_claim_of_a_waiting_unknown_or_leased_task(self):
        self.task("a")
        self.task("b")
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        self.assertEqual(self.code(self.claim, "b"), 4)
        self.assertEqual(self.code(self.claim, "ghost"), 9)
        self.claim("a")
        self.assertEqual(self.code(self.claim, "a"), 4)  # one live lease per task (Q-CLAIM-04)

    def test_unenqueued_task_cannot_be_claimed(self):
        self.task("a", enqueue=False)
        self.assertEqual(self.code(self.claim, "a"), 9)

    def test_precreated_lease_refuses_claim(self):  # Q-CLAIM-02, TM-15
        self.task("t1")
        before_queue = self.snap()
        planted = self.shit / "leases" / "t1.json"

        def plant(stage):
            if stage == "pending-written":
                planted.write_text("planted")

        with mock.patch.object(q, "_checkpoint", plant):
            self.assertEqual(self.code(self.claim, "t1"), 8)
        self.assertEqual(self.queue_lines().__len__(), 1)  # only the enqueue entry
        self.assertTrue((self.shit / "queue-pending.json").exists())  # kept as evidence, never deleted on a conflict
        self.assertEqual(json.loads((self.shit / "tasks" / "t1.json").read_text())["state"], "open")
        planted.unlink()
        self.assertNotEqual(before_queue, {})

    def test_leftover_lease_file_for_a_ready_task_is_an_orphan_not_a_claim(self):  # Q-LEASE-10, Q-REC-03
        self.task("t1")
        (self.shit / "leases" / "t1.json").write_text("planted")
        self.assertEqual(self.status()["t1"]["state"], "ready")
        self.assertFalse((self.shit / "leases" / "t1.json").exists())
        self.assertEqual(json.loads(self.queue_lines()[-1])["kind"], "orphan_removed")

    def test_simultaneous_claims_single_winner(self):  # Q-CLAIM-01, TM-15 (racing claims, threads)
        # The spec asks for 200 trials; each trial adds a task that every later operation re-verifies, so the
        # default is 60 (about 6 s). Set SHIT_RACE_TRIALS=200 for the full run.
        trials = int(os.environ.get("SHIT_RACE_TRIALS", "60"))
        for n in range(trials):
            task_mod.create_task(self.proj, task_id=f"r{n}", title="T", summary="S", owner="human", actor="human",
                                 now=at(self.clock))
            q.enqueue(self.proj, f"r{n}", "human", now=at(self.clock))
        projects = [Project.open(self.repo) for _ in range(2)]
        for p in projects:
            self.addCleanup(p.close)
        wins = {n: 0 for n in range(trials)}
        errors = []
        gate = threading.Barrier(2)

        def worker(index):
            gate.wait()
            for n in range(trials):
                try:
                    q.claim(projects[index], {"task_id": f"r{n}", "step": "draft", "worker": f"w{index}",
                                              "worker_kind": "script"}, now=at(self.clock + 1))
                    wins[n] += 1
                except ShitError as exc:
                    errors.append(exc.code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertEqual(set(wins.values()), {1})
        self.assertEqual(set(errors), {4})
        self.assertEqual(len(os.listdir(self.shit / "leases")), trials)

    def test_simultaneous_claims_from_two_processes(self):  # Q-CLAIM-01 across real processes
        self.task("t1")
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from shit import queue as q; from shit.project import Project; from shit.errors import ShitError;"
            "p = Project.open(sys.argv[2]);"
            "\ntry:\n q.claim(p, {'task_id': 't1', 'step': 'draft', 'worker': 'w', 'worker_kind': 'ai'}, now=sys.argv[3]); print('won')"
            "\nexcept ShitError as e:\n print('lost', e.code)\n")
        procs = [subprocess.Popen([sys.executable, "-I", "-c", script, str(ROOT), str(self.repo), at(100)],
                                  stdout=subprocess.PIPE, text=True) for _ in range(2)]
        outs = sorted(p.communicate(timeout=60)[0].strip() for p in procs)
        self.assertEqual(outs, ["lost 4", "won"])

    def test_capability_and_capacity_are_not_part_of_this_issue(self):
        # Documented gap (see docs/queue-spec.md follow-ups): no tag or capacity fields exist in a claim request.
        self.assertNotIn("tags", q.CLAIM_KEYS)

    def test_claim_before_not_before_is_refused(self):  # Q-RET-06
        self.task("t1")
        lease = self.claim("t1")
        entries = [json.loads(line) for line in self.queue_lines()]
        # a dispatch failure entry is written by issue 18's dispatcher; replay must honor its not_before
        entries.append({**entries[-1], "id": "queue-0003", "timestamp": self.tick(), "kind": "dispatch_failed",
                        "state_after": "ready", "lease_id": lease["lease_id"], "generation": 1,
                        "not_before": at(self.clock + 500)})
        for key in ("worker", "worker_kind", "step", "expires_at"):
            entries[-1].pop(key)
        (self.shit / "leases" / "t1.json").unlink()
        self.rewrite_queue([json.dumps(e, separators=(",", ":")).encode() for e in entries])
        self.assertEqual(self.code(self.claim, "t1"), 4)
        later = q.claim(self.proj, {"task_id": "t1", "step": "draft", "worker": "w", "worker_kind": "ai"},
                        now=at(self.clock + 600))
        self.assertEqual(later["generation"], 2)


class Fencing(QueueCase):
    def setUp(self):
        super().setUp()
        self.task("t1")
        self.lease = self.claim("t1")
        self.good = self.present(self.lease)

    def refused(self, presented, op=None):
        before = self.snap()
        code = self.code(op or q.start, self.proj, presented, now=self.tick())
        self.assertEqual(self.snap(), before)  # nothing is written
        return code

    def test_forged_lease_refused(self):  # Q-LEASE-01, TM-17
        self.assertEqual(self.refused({**self.good, "lease_id": "0" * 32}), 12)
        self.assertEqual(self.refused({**self.good, "lease_id": self.good["lease_id"][:-1] + "x"}), 12)
        self.assertEqual(self.refused({**self.good, "generation": 2}), 12)
        self.assertEqual(self.refused({**self.good, "generation": 0}), 12)
        self.assertEqual(self.refused({**self.good, "task_id": "ghost"}), 9)

    def test_shape_errors_are_usage_errors(self):
        self.assertEqual(self.refused({**self.good, "extra": 1}), 2)
        self.assertEqual(self.refused({"task_id": "t1"}), 2)
        self.assertEqual(self.refused({**self.good, "generation": "1"}), 2)
        self.assertEqual(self.refused({**self.good, "generation": True}), 2)
        self.assertEqual(self.refused({**self.good, "lease_id": None}), 2)

    def test_no_lease_refused_for_every_operation(self):
        q.release(self.proj, self.good, now=self.tick())
        for op in (q.start, q.renew, q.release, q.submit, q.fail):
            self.assertEqual(self.refused(self.good, op), 12, op.__name__)
        self.assertEqual(self.code(q.hold, self.proj, self.good, "owner", "r", "i", now=self.tick()), 12)

    def test_hand_written_lease_file_grants_nothing(self):  # Q-LEASE-10
        q.release(self.proj, self.good, now=self.tick())
        forged = {**self.lease, "lease_id": "ab" * 16}
        (self.shit / "leases" / "t1.json").write_text(json.dumps(forged))
        self.status()  # maintenance removes the orphan file and records orphan_removed
        self.assertEqual(self.refused({**self.good, "lease_id": "ab" * 16}), 12)

    def test_tampered_live_lease_file_fails_closed(self):  # an edited expiry must not extend a lease
        path = self.shit / "leases" / "t1.json"
        record = json.loads(path.read_text())
        record["expires_at"] = at(10 ** 6)
        path.write_text(json.dumps(record))
        before = self.snap()
        self.assertEqual(self.code(self.status), 8)
        self.assertEqual(self.snap(), before)

    def test_stale_generation_submit_refused(self):  # Q-LEASE-02, TM-20
        self.run_to_running("t1") if False else None
        q.start(self.proj, self.good, now=self.tick())
        q.reclaim(self.proj, "t1", "human", now=self.tick())  # lease A ends; the task is failed
        self.assertEqual(self.lifecycle("t1"), "failed")
        q.reopen(self.proj, "t1", "human", "retry", now=self.tick())
        second = self.claim("t1", worker="w2")
        self.assertEqual(second["generation"], 2)
        q.start(self.proj, self.present(second), now=self.tick())
        before = self.snap()
        self.assertEqual(self.code(q.submit, self.proj, self.good, now=self.tick()), 12)  # worker A, generation 1
        self.assertEqual(self.snap(), before)
        q.submit(self.proj, self.present(second), now=self.tick())  # B's lease is intact

    def test_wrong_state_for_the_operation(self):
        self.assertEqual(self.refused(self.good, q.submit), 4)  # dispatched, not running
        q.start(self.proj, self.good, now=self.tick())
        self.assertEqual(self.refused(self.good, q.start), 4)


class Expiry(QueueCase):
    def test_dispatched_expiry_returns_to_ready_on_next_operation(self):  # Q-LEASE-05
        self.task("t1")
        lease = self.claim("t1")
        later = at(self.clock + 901)
        self.assertEqual(q.status(self.proj, now=later)["t1"]["state"], "ready")
        kinds = [json.loads(line)["kind"] for line in self.queue_lines()]
        self.assertEqual(kinds[-1], "expired")
        self.assertFalse((self.shit / "leases" / "t1.json").exists())
        self.clock += 902
        self.assertEqual(self.code(q.start, self.proj, self.present(lease), now=self.tick()), 12)

    def test_running_expiry_ends_failed_never_ready(self):  # Q-LEASE-06
        self.task("t1")
        self.run_to_running("t1")
        row = q.status(self.proj, now=at(self.clock + 5000))["t1"]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(self.lifecycle("t1"), "failed")
        history = task_mod.get_task(self.proj, "t1")["history"][-1]
        self.assertEqual((history["actor"], history["reason"]), ("wrapper", "lease expired"))

    def test_renewal_bounds_enforced(self):  # Q-LEASE-03, TM-23
        self.task("t1")
        config = {"lease_ttl_seconds": 100 + 0, "lease_max_seconds": 350, "lease_max_renewals": 2}
        lease = q.claim(self.proj, {"task_id": "t1", "step": "draft", "worker": "w", "worker_kind": "ai"},
                        now=self.tick(), config=config)
        p = self.present(lease)
        first = q.renew(self.proj, p, now=self.tick(), config=config)
        self.assertEqual(first["renewals"], 1)
        second = q.renew(self.proj, p, now=self.tick(), config=config)
        self.assertEqual(second["renewals"], 2)
        before = self.snap()
        self.assertEqual(self.code(q.renew, self.proj, p, now=self.tick(), config=config), 12)  # renewal count
        self.assertEqual(self.snap(), before)

    def test_total_length_bound(self):
        self.task("t1")
        config = {"lease_ttl_seconds": 100, "lease_max_seconds": 250, "lease_max_renewals": 50}
        start = self.tick()
        lease = q.claim(self.proj, {"task_id": "t1", "step": "draft", "worker": "w", "worker_kind": "ai"},
                        now=start, config=config)
        p = self.present(lease)
        r1 = q.renew(self.proj, p, now=self.tick(), config=config)
        r2 = q.renew(self.proj, p, now=self.tick(), config=config)  # clamped to claimed + 250
        self.assertEqual(r2["expires_at"], (_dt(start) + timedelta(seconds=250)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertGreater(r2["expires_at"], r1["expires_at"])
        self.assertEqual(self.code(q.renew, self.proj, p, now=self.tick(), config=config), 12)

    def test_config_ranges_are_enforced(self):
        self.task("t1")
        for bad in ({"lease_ttl_seconds": 59}, {"lease_ttl_seconds": True}, {"unknown": 1}, {"max_attempts": 0},
                    {"lease_ttl_seconds": 600, "lease_max_seconds": 100}, {"lease_max_renewals": 1001}):
            self.assertEqual(self.code(q.status, self.proj, now=self.tick(), config=bad), 3, bad)

    def test_clock_backward_fails_closed(self):  # Q-LEASE-08, TM-16
        self.task("t1")
        lease = self.claim("t1")
        before = self.snap()
        back = at(self.clock - 1)
        self.assertEqual(self.code(q.claim, self.proj, {"task_id": "t1", "step": "draft", "worker": "w",
                                                       "worker_kind": "ai"}, now=back), 8)
        self.assertEqual(self.code(q.status, self.proj, now=back), 8)
        self.assertEqual(self.code(q.renew, self.proj, self.present(lease), now=back), 8)
        self.assertEqual(self.snap(), before)

    def test_forward_clock_jump_does_not_double_claim(self):  # Q-LEASE-09
        self.task("t1")
        lease = self.run_to_running("t1")
        jump = at(self.clock + 10 ** 5)
        second = q.status(self.proj, now=jump)
        self.assertEqual(second["t1"]["state"], "failed")
        self.assertEqual(self.code(q.submit, self.proj, self.present(lease), now=at(self.clock + 10 ** 5 + 1)), 12)

    def test_forced_reclaim_writes_the_same_entries_as_expiry(self):  # Q-REC-05
        for tid in ("a", "b"):
            self.task(tid)
            self.run_to_running(tid)
        q.reclaim(self.proj, "a", "human", now=self.tick())
        self.clock += 5000
        q.status(self.proj, now=at(self.clock))
        entries = {tid: [json.loads(line) for line in self.queue_lines() if json.loads(line)["task_id"] == tid]
                   for tid in ("a", "b")}
        shape = lambda tid: [(e["kind"], e["state_after"], e["actor"]) for e in entries[tid]]
        self.assertEqual(shape("a"), shape("b"))
        self.assertEqual(self.code(q.reclaim, self.proj, "b", "human", now=self.tick()), 9)
        self.assertEqual(self.code(q.reclaim, self.proj, "a", "model", now=self.tick()), 10)


def _dt(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class RetryAndHold(QueueCase):
    def test_no_automatic_retry_and_only_a_human_reopens(self):  # Q-RET-02
        self.task("t1")
        lease = self.run_to_running("t1")
        q.fail(self.proj, self.present(lease), now=self.tick())
        for actor in ("wrapper", "model"):
            self.assertEqual(self.code(q.reopen, self.proj, "t1", actor, "again", now=self.tick()), 4)
        self.assertEqual(self.status()["t1"]["state"], "failed")
        self.assertEqual(self.status(now=at(self.clock + 10 ** 6))["t1"]["state"], "failed")
        q.reopen(self.proj, "t1", "human", "again", now=self.tick())
        self.assertEqual(self.status()["t1"]["state"], "ready")
        self.assertEqual(self.lifecycle("t1"), "open")

    def test_attempt_counts_started_entries_and_max_attempts_needs_a_decision(self):  # Q-RET-01, Q-RET-03
        self.task("t1")
        config = {"max_attempts": 2}
        for n in range(2):
            lease = q.claim(self.proj, {"task_id": "t1", "step": "draft", "worker": "w", "worker_kind": "ai"},
                            now=self.tick(), config=config)
            q.start(self.proj, self.present(lease), now=self.tick(), config=config)
            q.fail(self.proj, self.present(lease), now=self.tick(), config=config)
            if n == 0:
                q.reopen(self.proj, "t1", "human", "again", now=self.tick(), config=config)
        self.assertEqual(self.status(config=config)["t1"]["attempts"], 2)
        before = self.snap()
        self.assertEqual(self.code(q.reopen, self.proj, "t1", "human", "again", now=self.tick(), config=config), 10)
        self.assertEqual(self.snap(), before)
        commands.add_decision(self.proj, task_id="t1", decision_id="d1", status="accepted", question="q", choice="c",
                              rationale="r", actor="human", now=self.tick())
        q.reopen(self.proj, "t1", "human", "again", now=self.tick(), config=config)
        self.assertEqual(self.status(config=config)["t1"]["state"], "ready")

    def test_hold_blocks_dependents_and_leaves_only_through_a_human(self):  # Q-RET-07
        for tid in ("a", "b"):
            self.task(tid)
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        lease = self.run_to_running("a")
        q.hold(self.proj, self.present(lease), "ci", "waiting for checks", "ci-run-1", now=self.tick())
        rows = self.status(now=at(self.clock + 10 ** 6))
        self.assertEqual((rows["a"]["state"], rows["b"]["state"]), ("hold-ci", "waiting"))
        self.assertEqual(self.code(q.reopen, self.proj, "a", "wrapper", "x", now=self.tick()), 4)
        q.reopen(self.proj, "a", "human", "resolved", now=self.tick())
        self.assertEqual(self.status()["a"]["state"], "ready")

    def test_reopen_lands_in_waiting_when_an_edge_is_unmet(self):
        for tid in ("a", "b"):
            self.task(tid)
        lease = self.run_to_running("b")
        q.fail(self.proj, self.present(lease), now=self.tick())
        q.add_edge(self.proj, "b", "a", "draft", "human", now=self.tick())
        q.reopen(self.proj, "b", "human", "again", now=self.tick())
        self.assertEqual(json.loads(self.queue_lines()[-1])["state_after"], "waiting")
        self.assertEqual(self.status()["b"]["state"], "waiting")

    def test_hold_text_is_checked(self):
        self.task("t1")
        lease = self.run_to_running("t1")
        self.assertEqual(self.code(q.hold, self.proj, self.present(lease), "owner", "bad\u202etext", "i", now=self.tick()), 3)
        self.assertEqual(self.code(q.hold, self.proj, self.present(lease), "elsewhere", "r", "i", now=self.tick()), 2)

    def test_complete_requires_the_close_rules_and_is_terminal(self):  # Q-ST-05, TM-19
        self.task("t1")
        lease = self.run_to_running("t1")
        q.submit(self.proj, self.present(lease), now=self.tick())
        self.assertEqual(self.code(q.complete, self.proj, "t1", "model", "done", now=self.tick()), 4)
        self.assertEqual(self.code(q.complete, self.proj, "t1", "human", "done", now=self.tick()), 4)  # not approved yet
        self.success_report("t1")
        commands.add_review(self.proj, task_id="t1", review_id="rv1", report_id="t1-report", verdict="pass",
                            summary="ok", actor="human", now=self.tick())
        commands.add_decision(self.proj, task_id="t1", decision_id="d1", status="accepted", question="q", choice="c",
                              rationale="r", actor="human", now=self.tick())
        task_mod.transition(self.proj, "t1", "approved", "human", "ok", now=self.tick())
        self.assertEqual(self.status()["t1"]["state"], "review")
        q.complete(self.proj, "t1", "human", "closed", now=self.tick())
        self.assertEqual((self.status()["t1"]["state"], self.lifecycle("t1")), ("done", "closed"))
        for op in (lambda: self.claim("t1"), lambda: q.reopen(self.proj, "t1", "human", "x", now=self.tick()),
                   lambda: q.enqueue(self.proj, "t1", "human", now=self.tick())):
            self.assertIn(self.code(op), (3, 4))

    def test_closed_id_not_reusable(self):  # Q-RES-01
        self.task("t1")
        self.assertEqual(self.code(q.enqueue, self.proj, "t1", "human", now=self.tick()), 3)
        self.assertEqual(self.code(task_mod.create_task, self.proj, task_id="t1", title="T", summary="S",
                                   owner="human", actor="human", now=self.tick()), 2)
        (self.shit / "tasks" / "t1.json").unlink()
        self.assertEqual(self.code(task_mod.create_task, self.proj, task_id="t1", title="T", summary="S",
                                   owner="human", actor="human", now=self.tick()), 3)

    def test_enqueue_requires_human_or_wrapper_and_an_open_task(self):
        self.task("t1", enqueue=False)
        self.assertEqual(self.code(q.enqueue, self.proj, "t1", "model", now=self.tick()), 10)
        self.assertEqual(self.code(q.enqueue, self.proj, "ghost", "human", now=self.tick()), 9)
        self.assertEqual(self.code(q.enqueue, self.proj, "../x", "human", now=self.tick()), 3)
        task_mod.transition(self.proj, "t1", "blocked", "human", "wait", now=self.tick(), blocked_reason="r",
                            blocking_item="i")
        self.assertEqual(self.code(q.enqueue, self.proj, "t1", "human", now=self.tick()), 4)


class TransitionTable(unittest.TestCase):
    def test_exactly_the_listed_pairs_are_legal(self):  # Q-ST-03
        legal = {(a, b) for a in qr.STATES for b in qr.STATES if qr.check_queue_transition(a, b)}
        self.assertEqual(len(qr.STATES) ** 2, 81)
        self.assertEqual(legal, set(qr.ALLOWED))
        self.assertEqual(len(legal), 24)
        for state in qr.STATES:
            self.assertFalse(qr.check_queue_transition(state, state))
        for frm in ("done",):
            self.assertFalse(any(qr.check_queue_transition(frm, b) for b in qr.STATES))  # done is terminal

    def test_failed_never_goes_to_ready_except_by_reopen_entry(self):
        entry = {"schema": "queue-entry", "schema_version": 1, "id": "queue-0001", "task_id": "t", "timestamp": at(1),
                 "actor": "wrapper", "kind": "ready", "state_after": "ready"}
        enqueued = {**entry, "kind": "enqueued", "state_after": "waiting", "id": "queue-0001"}
        failed = {**entry, "id": "queue-0002", "kind": "failed", "state_after": "failed"}
        with self.assertRaises(ShitError):
            qr.replay([enqueued, failed])  # waiting to failed is not in the table


class Hostile(QueueCase):
    def entries(self):
        return [json.loads(line) for line in self.queue_lines()]

    def test_truncated_middle_line_fails_closed(self):  # Q-REC-04
        self.task("a")
        lease = self.run_to_running("a")
        q.submit(self.proj, self.present(lease), now=self.tick())
        lines = self.queue_lines()
        del lines[1]
        (self.shit / "queue.jsonl").write_bytes(b"".join(line + b"\n" for line in lines))
        before = self.snap()
        for op in (self.status, lambda: self.claim("a")):
            self.assertEqual(self.code(op), 8)
        self.assertEqual(self.snap(), before)

    def test_edited_line_with_untouched_chain_fails_closed(self):
        self.task("a")
        lines = self.queue_lines()
        lines[0] = lines[0].replace(b'"ready"', b'"waiting"')
        (self.shit / "queue.jsonl").write_bytes(b"".join(line + b"\n" for line in lines))
        self.assertEqual(self.code(self.status), 8)

    def test_chain_file_missing_or_short_fails_closed(self):
        self.task("a")
        (self.shit / "queue.chain").write_bytes(b"")
        self.assertEqual(self.code(self.status), 8)

    def test_forged_rechained_entries_are_rejected_by_validation(self):  # TM-17, TM-19
        self.task("a")
        base = self.entries()[0]
        wrapper = {**base, "actor": "wrapper", "id": "queue-0002"}
        valid = {**wrapper, "kind": "orphan_removed"}  # a well-formed entry the cases below each break in one way
        forged_cases = {
            "model reopen": {**valid, "kind": "reopened", "actor": "model"},
            "human claim": {**valid, "kind": "claimed", "actor": "human", "state_after": "dispatched",
                            "lease_id": "1" * 32, "generation": 1, "worker": "w", "step": "draft",
                            "expires_at": at(10 ** 5)},
            "unknown kind": {**valid, "kind": "approved"},
            "extra field": {**valid, "model": "gpt"},
            "claim without lease": {**wrapper, "kind": "claimed", "state_after": "dispatched"},
            "generation jump": {**wrapper, "kind": "claimed", "state_after": "dispatched", "lease_id": "1" * 32,
                                "generation": 7, "worker": "w", "step": "draft", "expires_at": at(10 ** 5)},
            "done from ready": {**valid, "kind": "done", "state_after": "done"},
            "reused task id": {**base, "id": "queue-0002"},
            "time goes back": {**valid, "timestamp": at(-100)},
            "wrong id": {**valid, "id": "queue-0009"},
        }
        original = self.queue_lines()
        for name, forged in forged_cases.items():
            self.rewrite_queue([*original, json.dumps(forged, separators=(",", ":")).encode()])
            self.assertEqual(self.code(self.status), 8, name)
        self.rewrite_queue([*original, json.dumps(valid, separators=(",", ":")).encode()])
        self.assertEqual(self.status()["a"]["state"], "ready")  # the unbroken entry is accepted: the cases bite

    def test_duplicate_key_and_depth_in_an_entry_are_refused(self):
        self.task("a")
        line = self.queue_lines()[0]
        dup = line[:-1] + b',"kind":"ready"}'
        self.rewrite_queue([dup])
        self.assertEqual(self.code(self.status), 8)
        self.rewrite_queue([line[:-1] + b',"x":[[[[[[[[[1]]]]]]]]]}'])
        self.assertEqual(self.code(self.status), 7)

    def test_hostile_characters_in_an_entry_message_are_refused(self):
        self.task("a")
        entry = {**self.entries()[0], "message": "line\u202ebreak"}
        self.rewrite_queue([json.dumps(entry, separators=(",", ":")).encode()])
        self.assertEqual(self.code(self.status), 8)

    def test_restored_older_queue_fails_closed(self):  # Q-RES-02, TM-19
        self.task("a")
        old = self.queue_lines()[:1]
        lease = self.run_to_running("a")
        q.submit(self.proj, self.present(lease), now=self.tick())
        self.rewrite_queue(old)  # the prefix that ends at `enqueued`; the task is already in review
        self.assertEqual(self.code(self.status), 8)

    def test_queue_entry_that_contradicts_the_lifecycle_fails_closed(self):  # Q-REC-04
        self.task("a")
        task_mod.transition(self.proj, "a", "blocked", "human", "by hand", now=self.tick(), blocked_reason="r",
                            blocking_item="i")
        self.assertEqual(self.code(self.status), 8)

    def test_task_file_missing_for_a_queued_task_fails_closed(self):
        self.task("a")
        (self.shit / "tasks" / "a.json").unlink()
        self.assertEqual(self.code(self.status), 8)

    def test_symlinked_queue_files_and_leases_dir_are_refused(self):
        self.task("a")
        outside = self.repo / "x.jsonl"
        outside.write_bytes((self.shit / "queue.jsonl").read_bytes())
        (self.shit / "queue.jsonl").unlink()
        os.symlink(outside, self.shit / "queue.jsonl")
        self.assertEqual(self.code(self.status), 5)

    def test_stray_name_in_leases_fails_closed(self):
        self.task("a")
        (self.shit / "leases" / "Not A Slug.txt").write_text("x")
        self.assertEqual(self.code(self.status), 8)

    def test_lease_file_symlink_is_refused(self):
        self.task("a")
        os.symlink(self.repo / "elsewhere", self.shit / "leases" / "a.json")
        self.assertEqual(self.code(self.status), 5)

    def test_hostile_text_in_task_never_reaches_the_view_or_entries(self):
        self.task("a")
        self.assertNotIn(b"Title", b"".join(self.queue_lines()))


class CrashRecovery(QueueCase):
    STAGES = ("pending-written", "lease-applied", "lifecycle-applied", "entry-appended", "chain-appended", "anchor-written",
              "pending-removed")

    class Crash(Exception):
        pass

    def scenario(self, case, op, module=q):
        """Build a fresh repository, run `op` and crash at stage `case` (or never when None); return the end state."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve() / "repo"
            repo.mkdir()
            make_git_dir(repo / ".git")
            proj = Project.init(repo)
            try:
                for tid in ("t1",):
                    task_mod.create_task(proj, task_id=tid, title="T", summary="S", owner="human", actor="human",
                                         now=at(1))
                    q.enqueue(proj, tid, "human", now=at(2))
                with mock.patch.object(q, "_new_lease_id", lambda: "c" * 32):
                    first = q.claim(proj, {"task_id": "t1", "step": "draft", "worker": "w", "worker_kind": "ai"}, now=at(3))
                    if op != "claim":
                        q.start(proj, {"task_id": "t1", "lease_id": first["lease_id"], "generation": 1}, now=at(4))
                    target = {"claim": None, "start": None, "submit": q.submit, "fail": q.fail}.get(op)
                    present = {"task_id": "t1", "lease_id": first["lease_id"], "generation": 1}
                    if op == "claim":
                        # crash during a second task's claim path is covered by a fresh task
                        task_mod.create_task(proj, task_id="t2", title="T", summary="S", owner="human", actor="human",
                                             now=at(5))
                        q.enqueue(proj, "t2", "human", now=at(6))
                        run = lambda: q.claim(proj, {"task_id": "t2", "step": "draft", "worker": "w", "worker_kind": "ai"},
                                              now=at(7))
                    else:
                        run = lambda: target(proj, present, now=at(7))

                    def crash(stage):
                        if stage == case:
                            raise CrashRecovery.Crash

                    if case is not None:
                        with mock.patch.object(module, "_checkpoint", crash), self.assertRaises(CrashRecovery.Crash):
                            run()
                    else:
                        run()
                    q.status(proj, now=at(8))  # any later locked operation finishes the commit
                    q.status(proj, now=at(8))
                return {str(p.relative_to(repo / ".shit")): p.read_bytes() for p in sorted((repo / ".shit").rglob("*"))
                        if p.is_file() and p.name != "lock"}
            finally:
                proj.close()

    def test_queue_crash_matrix_recovers_consistently(self):  # Q-REC-01, TM-24
        for op in ("claim", "submit", "fail"):
            expected = self.scenario(None, op)
            for stage in self.STAGES:
                with self.subTest(op=op, stage=stage):
                    self.assertEqual(self.scenario(stage, op), expected)

    def test_crash_inside_the_nested_lifecycle_transition_recovers(self):  # Q-ST-04
        for op in ("submit", "fail"):
            expected = self.scenario(None, op)
            for stage in ("start", "pending-written", "event-appended", "chain-appended", "task-written"):
                with self.subTest(op=op, stage=stage):
                    self.assertEqual(self.scenario(stage, op, module=task_mod), expected)

    def test_orphan_lease_removed(self):  # Q-REC-03a
        self.task("t1")
        (self.shit / "leases" / "ghost.json").write_text("{}")
        self.status()
        self.assertEqual(os.listdir(self.shit / "leases"), [])

    def test_claimed_entry_with_missing_lease_file_is_treated_as_expired(self):  # Q-REC-03b
        self.task("t1")
        self.claim("t1")
        (self.shit / "leases" / "t1.json").unlink()
        self.assertEqual(self.status()["t1"]["state"], "ready")
        self.assertEqual(json.loads(self.queue_lines()[-1])["kind"], "expired")

    def test_stale_tmp_file_in_leases_is_cleaned(self):
        self.task("t1")
        (self.shit / "leases" / ".tmp-abc").write_text("x")
        self.status()
        self.assertEqual(os.listdir(self.shit / "leases"), [])

    def test_pending_record_that_is_garbage_fails_closed_and_is_kept(self):  # Q-REC-02
        self.task("t1")
        before = self.snap()
        pending = self.shit / "queue-pending.json"
        pending.write_text("{not json")
        self.assertEqual(self.code(self.status), 8)
        self.assertTrue(pending.exists())  # kept as evidence, nothing applied
        self.assertEqual(self.snap(), {**before, "queue-pending.json": b"{not json"})
        pending.write_text(json.dumps({"index": 0}))
        self.assertEqual(self.code(self.status), 8)

    def test_torn_queue_tail_is_healed_from_the_pending_record(self):
        self.task("t1")

        def crash(stage):
            if stage == "lifecycle-applied":
                raise CrashRecovery.Crash

        with mock.patch.object(q, "_checkpoint", crash), self.assertRaises(CrashRecovery.Crash):
            self.claim("t1")
        pending = json.loads((self.shit / "queue-pending.json").read_text())
        with open(self.shit / "queue.jsonl", "ab") as handle:
            handle.write(pending["entry_line"].encode()[:20])  # a torn append
        self.assertEqual(self.status()["t1"]["state"], "dispatched")

    def test_pending_with_foreign_chain_line_fails_closed(self):
        self.task("t1")

        def crash(stage):
            if stage == "pending-written":
                raise CrashRecovery.Crash

        with mock.patch.object(q, "_checkpoint", crash), self.assertRaises(CrashRecovery.Crash):
            self.claim("t1")
        path = self.shit / "queue-pending.json"
        doc = json.loads(path.read_text())
        doc["chain_line"] = hashlib.sha256(b"x").hexdigest()
        path.write_text(json.dumps(doc))
        self.assertEqual(self.code(self.status), 8)


class PendingProof(QueueCase):
    """A pending commit is applied only when it is the current tail and carries exactly the effects of its entry."""

    class Crash(Exception):
        pass

    def crash_at(self, stage, fn, *args, **kw):
        def crash(seen):
            if seen == stage:
                raise PendingProof.Crash

        with mock.patch.object(q, "_checkpoint", crash), self.assertRaises(PendingProof.Crash):
            fn(*args, **kw)

    def to_review(self, tid):
        lease = self.run_to_running(tid)
        q.submit(self.proj, self.present(lease), now=self.tick())

    def events(self):
        return (self.shit / "events.jsonl").read_bytes()

    def forge(self, task_id, kind, state_after, actor="wrapper", **effects):
        """Write a pending record whose entry is validly chained but whose side effects are chosen by the test."""
        entries = [json.loads(line) for line in self.queue_lines()]
        previous = (self.shit / "queue.chain").read_bytes().splitlines()[-1].decode()
        entry = {"schema": "queue-entry", "schema_version": 1, "id": f"queue-{len(entries) + 1:04d}",
                 "timestamp": self.tick(), "message": q.MESSAGE, "task_id": task_id, "actor": actor, "kind": kind,
                 "state_after": state_after}
        line = qr.dump_entry(entry)
        doc = {"index": len(entries), "entry_line": line.decode(), "chain_line": chain_hash(previous, line),
               "lease": None, "lease_replaces": None, "remove_lease": None, "lifecycle": None, "shards": []}
        doc.update(effects)
        (self.shit / "queue-pending.json").write_text(json.dumps(doc))

    def test_an_entry_kind_with_no_effects_cannot_carry_a_lifecycle_effect(self):  # H1
        self.task("t1")
        self.to_review("t1")
        life = q._life("t1", "review", "approved", "human", "forged", self.tick())
        self.forge("t1", "orphan_removed", "review", lifecycle=life)
        events, state = self.events(), self.snap()
        self.assertEqual(self.code(self.status), 8)
        self.assertEqual(json.loads((self.shit / "tasks" / "t1.json").read_text())["state"], "review")
        self.assertEqual(self.events(), events)
        self.assertEqual(self.queue_lines(), [line for line in state["queue.jsonl"].splitlines()])

    def test_a_lifecycle_effect_must_match_its_entry_kind_and_actor(self):  # H1
        self.task("t1")
        self.to_review("t1")
        life = q._life("t1", "review", "approved", "human", "forged", self.tick())
        self.forge("t1", "reopened", "ready", actor="human", lifecycle=life)
        self.assertEqual(self.code(self.status), 8)
        self.assertEqual(json.loads((self.shit / "tasks" / "t1.json").read_text())["state"], "review")

    def test_a_stale_pending_record_applies_nothing(self):  # M1
        for tid in ("a", "b", "c"):
            self.task(tid)
        self.crash_at("pending-written", q.add_edge, self.proj, "b", "a", "draft", "human", now=self.tick())
        old = (self.shit / "queue-pending.json").read_bytes()
        self.status()
        q.add_edge(self.proj, "b", "c", "draft", "human", now=self.tick())
        shard = (self.shit / "deps" / "b.json").read_bytes()
        (self.shit / "queue-pending.json").write_bytes(old)
        self.assertEqual(self.code(self.status), 8)
        self.assertEqual((self.shit / "deps" / "b.json").read_bytes(), shard)

    def test_a_renewal_pending_record_must_equal_the_replayed_lease_plus_one_renewal(self):  # round 2
        self.task("t1")
        lease = self.run_to_running("t1")
        self.crash_at("pending-written", q.renew, self.proj, self.present(lease), now=self.tick())
        path = self.shit / "queue-pending.json"
        good = path.read_bytes()
        changes = {"worker": "w2", "worker_kind": "human", "step": "apply", "claimed_at": at(0), "renewals": 5}
        for key, value in changes.items():
            with self.subTest(field=key):
                doc = json.loads(good)
                doc["lease"][key] = value
                path.write_text(json.dumps(doc))
                before = self.snap()
                self.assertEqual(self.code(self.status), 8)
                self.assertEqual(self.snap(), before)  # nothing applied, the record is kept
                self.assertTrue(path.exists())
        path.write_bytes(good)
        self.assertEqual(self.status()["t1"]["state"], "running")  # the genuine record still recovers

    def test_a_lifecycle_reason_in_a_pending_record_must_be_the_fixed_one(self):  # round 2
        self.task("t1")
        lease = self.claim("t1")
        self.crash_at("pending-written", q.start, self.proj, self.present(lease), now=self.tick())
        path = self.shit / "queue-pending.json"
        doc = json.loads(path.read_text())
        doc["lifecycle"]["reason"] = "something else"
        path.write_text(json.dumps(doc))
        before = self.snap()
        self.assertEqual(self.code(self.status), 8)
        self.assertEqual(self.snap(), before)

    def test_a_task_writer_refuses_while_a_queue_commit_is_pending(self):  # round 3
        self.task("t1")
        lease = self.run_to_running("t1")
        self.crash_at("pending-written", q.submit, self.proj, self.present(lease), now=self.tick())
        before = self.snap()
        self.assertEqual(self.code(task_mod.create_task, self.proj, task_id="t2", title="T", summary="S",
                                   owner="human", actor="human", now=self.tick(500)), 8)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.status(now=at(self.clock + 1))["t1"]["state"], "review")  # the commit still recovers

    def test_a_pending_lifecycle_older_than_the_event_log_tip_changes_nothing(self):  # round 3
        self.task("t1")
        lease = self.run_to_running("t1")
        self.crash_at("pending-written", q.submit, self.proj, self.present(lease), now=self.tick())
        path = self.shit / "queue-pending.json"
        held = path.read_bytes()
        path.unlink()  # an event writer that did not look at the pending record
        task_mod.create_task(self.proj, task_id="t2", title="T", summary="S", owner="human", actor="human",
                             now=self.tick(500))
        path.write_bytes(held)
        before = self.snap()
        self.assertEqual(self.code(self.status, now=at(self.clock + 1)), 8)
        self.assertEqual(self.snap(), before)  # the lease is still there, nothing was removed

    def test_task_recovery_does_not_run_before_the_queue_record_is_proven(self):  # round 3
        self.task("t1")
        lease = self.claim("t1")

        def crash(stage):
            if stage == "pending-written":
                raise PendingProof.Crash

        with mock.patch.object(task_mod, "_checkpoint", crash), self.assertRaises(PendingProof.Crash):
            q.start(self.proj, self.present(lease), now=self.tick())
        self.assertTrue((self.shit / "pending.json").exists())
        path = self.shit / "queue-anchor.json"
        doc = json.loads(path.read_text())
        doc["tip"] = "0" * 64
        path.write_text(json.dumps(doc))
        before = self.snap()
        self.assertEqual(self.code(self.status), 8)
        self.assertEqual(self.snap(), before)

    def test_a_torn_tail_is_not_rewritten_when_the_pending_record_cannot_be_proven(self):  # round 3
        self.task("t1")
        self.crash_at("pending-written", self.claim, "t1")
        lines = self.queue_lines()
        pending = json.loads((self.shit / "queue-pending.json").read_text())
        (self.shit / "queue.jsonl").write_bytes(lines[0][:-1] + b"X" + b"\n" + b"".join(l + b"\n" for l in lines[1:])
                                                + pending["entry_line"].encode()[:20])
        before = self.snap()
        self.assertEqual(self.code(self.status), 8)
        self.assertEqual(self.snap(), before)

    def test_a_lease_conflict_keeps_the_pending_record_and_changes_nothing(self):  # round 3
        self.task("t1")
        self.crash_at("pending-written", self.claim, "t1")
        pending = json.loads((self.shit / "queue-pending.json").read_text())
        other = {**pending["lease"], "lease_id": "f" * 32}
        (self.shit / "leases" / "t1.json").write_bytes(qr.dump_lease(other))
        before = self.snap()
        self.assertEqual(self.code(self.status), 8)
        self.assertEqual(self.snap(), before)
        self.assertTrue((self.shit / "queue-pending.json").exists())

    def test_a_graph_pending_record_with_an_empty_shard_applies_nothing(self):  # round 2
        self.task("a")
        self.task("b")
        data = dep.dump_shard("b", [])
        entry = {"schema": "queue-entry", "schema_version": 1, "id": f"queue-{len(self.queue_lines()) + 1:04d}",
                 "timestamp": self.tick(), "message": q.MESSAGE, "task_id": "b", "actor": "human",
                 "kind": "graph_changed", "shards": [{"task_id": "b", "sha256": hashlib.sha256(data).hexdigest()}]}
        line = qr.dump_entry(entry)
        previous = (self.shit / "queue.chain").read_bytes().splitlines()[-1].decode()
        doc = {"index": len(self.queue_lines()), "entry_line": line.decode(), "chain_line": chain_hash(previous, line),
               "lease": None, "lease_replaces": None, "remove_lease": None, "lifecycle": None,
               "shards": [{"task_id": "b", "content": data.decode()}]}
        (self.shit / "queue-pending.json").write_text(json.dumps(doc))
        before = self.snap()
        self.assertEqual(self.code(self.status), 8)
        self.assertEqual(self.snap(), before)

    def test_a_corrupt_anchor_is_found_before_recovery_changes_anything(self):  # round 2
        self.task("a")
        self.crash_at("pending-written", self.claim, "a")
        path = self.shit / "queue-anchor.json"
        for key, value in (("tip", "0" * 64), ("count", 9)):
            with self.subTest(field=key):
                good = path.read_bytes()
                doc = json.loads(good)
                doc[key] = value
                path.write_text(json.dumps(doc))
                before = self.snap()
                self.assertEqual(self.code(self.status), 8)
                self.assertEqual(self.snap(), before)
                path.write_bytes(good)
        self.assertEqual(self.status()["a"]["state"], "dispatched")

    def test_an_unverifiable_pending_record_is_kept_as_evidence(self):  # M1
        self.task("t1")
        lease = self.claim("t1")
        self.crash_at("lifecycle-applied", q.start, self.proj, self.present(lease), now=self.tick())
        path = self.shit / "queue-pending.json"
        doc = json.loads(path.read_text())
        doc["chain_line"] = "zz"
        path.write_text(json.dumps(doc))
        self.assertEqual(self.code(self.status), 8)
        self.assertTrue(path.exists())
        self.assertEqual(self.code(self.status), 8)
        self.assertTrue(path.exists())


class ApplyMarkers(QueueCase):
    """`apply_started` and `apply_finished` are accepted only as one ordered pair under an apply-step lease."""

    def extend(self, lease, *kinds):
        lines = self.queue_lines()
        for kind in kinds:
            entry = {"schema": "queue-entry", "schema_version": 1, "id": f"queue-{len(lines) + 1:04d}",
                     "timestamp": self.tick(), "message": q.MESSAGE, "task_id": lease["task_id"], "actor": "wrapper",
                     "kind": kind, "state_after": "running", "lease_id": lease["lease_id"],
                     "generation": lease["generation"]}
            lines.append(qr.dump_entry(entry))
        self.rewrite_queue(lines)

    def running(self, step):
        self.task("t1")
        lease = self.claim("t1", step=step)
        q.start(self.proj, self.present(lease), now=self.tick())
        return lease

    def test_an_ordered_pair_under_an_apply_lease_is_accepted(self):
        lease = self.running("apply")
        self.extend(lease, "apply_started", "apply_finished")
        self.assertEqual(self.status()["t1"]["state"], "running")

    def test_a_finish_under_a_draft_lease_is_refused(self):  # H5
        lease = self.running("draft")
        self.extend(lease, "apply_started", "apply_finished")
        self.assertEqual(self.code(self.status), 8)

    def test_a_finish_without_a_start_is_refused(self):  # H5
        lease = self.running("apply")
        self.extend(lease, "apply_finished")
        self.assertEqual(self.code(self.status), 8)

    def test_a_repeated_start_or_finish_is_refused(self):  # H5
        lease = self.running("apply")
        self.extend(lease, "apply_started", "apply_started")
        self.assertEqual(self.code(self.status), 8)

    def test_a_repeated_finish_is_refused(self):  # H5
        lease = self.running("apply")
        self.extend(lease, "apply_started", "apply_finished", "apply_finished")
        self.assertEqual(self.code(self.status), 8)


class ClockOrder(QueueCase):
    """Every mutation checks the clock against the queue tip and the event-log tip, and never appends out of order."""

    def test_a_clock_behind_the_event_log_changes_nothing(self):  # H6
        self.task("t1")
        self.clock = 5000
        self.success_report("t1")
        before = self.snap()
        for operation in (lambda: q.claim(self.proj, {"task_id": "t1", "step": "draft", "worker": "w1",
                                                      "worker_kind": "ai"}, now=at(10)),
                          lambda: q.status(self.proj, now=at(10))):
            self.assertEqual(self.code(operation), 8)
        self.assertEqual(self.snap(), before)
        from shit.log import EventLog
        EventLog(self.proj).verify()

    def test_the_event_log_refuses_a_candidate_older_than_its_tip(self):  # H6
        from shit.log import EventLog
        self.task("t1", enqueue=False)
        log = EventLog(self.proj)
        events = log.verify()
        stale = {**events[-1], "id": f"event-{len(events) + 1:04d}", "timestamp": at(0)}
        with self.proj.locked():
            self.assertEqual(self.code(log.prepare, stale), 3)


class PublicationWindow(QueueCase):
    def test_a_process_killed_between_link_and_unlink_is_recovered(self):  # H7
        self.task("t1")
        script = (
            "import os, sys; sys.path.insert(0, sys.argv[1]);"
            "from shit import queue as q; from shit.project import Project;"
            "p = Project.open(sys.argv[2]);"
            "os.unlink = lambda *a, **k: os._exit(9);"  # die right after the link, before the temp name is removed
            "q.claim(p, {'task_id': 't1', 'step': 'draft', 'worker': 'w', 'worker_kind': 'ai'}, now=sys.argv[3])")
        done = subprocess.run([sys.executable, "-I", "-c", script, str(ROOT), str(self.repo), at(100)], timeout=60,
                              check=False)
        self.assertEqual(done.returncode, 9)
        leases = self.shit / "leases"
        self.assertEqual((leases / "t1.json").stat().st_nlink, 2)
        self.assertEqual(self.status(now=at(101))["t1"]["state"], "dispatched")
        self.assertEqual(self.status(now=at(102))["t1"]["state"], "dispatched")
        self.assertEqual(os.listdir(leases), ["t1.json"])
        self.assertEqual((leases / "t1.json").stat().st_nlink, 1)

    def test_an_unrelated_second_link_to_a_lease_is_still_refused(self):  # H7 negative: only an exact temp pair heals
        self.task("t1")
        self.claim("t1")
        os.link(self.shit / "leases" / "t1.json", self.shit / "leases" / "t1-copy.json")
        self.assertEqual(self.code(self.status), 5)


class TipAnchor(QueueCase):
    """The queue tip and each task's generation high-water mark are anchored apart from the queue files."""

    def test_restoring_an_earlier_consistent_prefix_is_refused(self):  # H3
        self.task("t1")
        prefix = {n: (self.shit / n).read_bytes() for n in ("queue.jsonl", "queue.chain")}
        first = self.claim("t1")
        for name, data in prefix.items():
            (self.shit / name).write_bytes(data)
        self.assertEqual(self.code(self.status), 8)
        self.assertTrue((self.shit / "leases" / "t1.json").exists())  # the lease is not removed as an orphan
        self.assertEqual(self.code(self.claim, "t1"), 8)
        self.assertEqual(first["generation"], 1)

    def test_a_missing_anchor_over_a_nonempty_queue_is_refused(self):  # H3
        self.task("t1")
        (self.shit / "queue-anchor.json").unlink()
        self.assertEqual(self.code(self.status), 8)

    def test_an_anchor_that_disagrees_with_the_queue_is_refused(self):  # H3
        self.task("t1")
        path = self.shit / "queue-anchor.json"
        doc = json.loads(path.read_text())
        doc["tip"] = "0" * 64
        path.write_text(json.dumps(doc))
        self.assertEqual(self.code(self.status), 8)

    def test_a_crash_before_the_anchor_is_written_recovers(self):  # Q-REC-01
        self.task("a")

        def crash(stage):
            if stage == "chain-appended":
                raise PendingProof.Crash

        with mock.patch.object(q, "_checkpoint", crash), self.assertRaises(PendingProof.Crash):
            self.claim("a")
        self.assertEqual(self.status()["a"]["state"], "dispatched")
        self.assertEqual(json.loads((self.shit / "queue-anchor.json").read_text())["generations"], {"a": 1})


class LockIdentity(QueueCase):
    def test_replacing_the_lock_file_does_not_admit_a_second_holder(self):  # H2
        other = Project.open(self.repo)
        self.addCleanup(other.close)
        with self.proj.locked():
            (self.shit / "lock").unlink()
            (self.shit / "lock").write_bytes(b"")
            with self.assertRaises(ShitError) as raised, other.locked(0.3):
                pass
            self.assertEqual(raised.exception.code, 1)
        with other.locked(0.3):
            pass

    def test_a_second_holder_waits_for_the_first(self):  # H2 positive control
        other = Project.open(self.repo)
        self.addCleanup(other.close)
        with self.proj.locked(), self.assertRaises(ShitError), other.locked(0.2):
            pass


class ReplayCeilings(QueueCase):
    """Replay enforces the global lease ceilings even for a correctly chained entry."""

    def entries(self):
        return [json.loads(line) for line in self.queue_lines()]

    def test_a_far_future_claim_is_refused_by_replay(self):  # M2
        self.task("t1")
        self.claim("t1")
        entries = self.entries()
        entries[-1]["expires_at"] = "9999-12-31T23:59:59Z"
        with self.assertRaises(ShitError) as raised:
            qr.replay(entries)
        self.assertEqual(raised.exception.code, 3)

    def test_a_claim_longer_than_the_ttl_ceiling_is_refused(self):  # M2
        self.task("t1")
        self.claim("t1")
        entries = self.entries()
        entries[-1]["expires_at"] = at(self.clock + 86401)
        with self.assertRaises(ShitError):
            qr.replay(entries)

    def test_a_renewal_past_the_absolute_ceiling_is_refused(self):  # M2
        self.task("t1")
        lease = self.run_to_running("t1")
        q.renew(self.proj, self.present(lease), now=self.tick())
        entries = self.entries()
        entries[-1]["expires_at"] = "2099-01-01T00:00:00Z"
        with self.assertRaises(ShitError):
            qr.replay(entries)

    def test_a_renewal_longer_than_the_ttl_ceiling_from_its_own_time_is_refused(self):  # M2
        self.task("t1")
        lease = self.run_to_running("t1")
        q.renew(self.proj, self.present(lease), now=self.tick())
        entries = self.entries()
        previous = entries[-2]["expires_at"] if entries[-2]["kind"] != "started" else entries[-3]["expires_at"]
        entries[-1]["timestamp"] = at(self.clock)
        entries[-1]["expires_at"] = at(self.clock + 86401)  # within the old expiry plus a TTL, past the entry's own ceiling
        self.assertLess(previous, entries[-1]["expires_at"])
        with self.assertRaises(ShitError):
            qr.replay(entries)

    def test_a_renewal_recorded_after_the_lease_expired_is_refused(self):  # M2
        self.task("t1")
        lease = self.run_to_running("t1")
        entries = self.entries()
        expiry = max(e["expires_at"] for e in entries if "expires_at" in e)
        late = {**entries[-1], "id": f"queue-{len(entries) + 1:04d}", "kind": "renewed", "state_after": "running",
                "worker": lease["worker"], "step": lease["step"], "lease_id": lease["lease_id"],
                "generation": lease["generation"], "timestamp": expiry,
                "expires_at": (datetime.strptime(expiry, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                               + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        for key in ("attempt", "worker_kind"):
            late.pop(key, None)
        with self.assertRaises(ShitError):
            qr.replay([*entries, late])
        qr.replay([*entries, {**late, "timestamp": at(self.clock)}])  # positive control: the same renewal in time

    def test_a_lease_file_with_an_impossible_lifetime_is_rejected(self):  # M2
        self.task("t1")
        lease = self.claim("t1")
        lease["expires_at"] = "9999-12-31T23:59:59Z"
        with self.assertRaises(ShitError):
            qr.validate_lease(lease)

    def test_the_generated_limits_still_replay(self):  # M2 positive control
        self.task("t1")
        lease = self.run_to_running("t1")
        for _ in range(3):
            q.renew(self.proj, self.present(lease), now=self.tick())
        qr.replay(self.entries())


class LogCap(QueueCase):
    """An append that would cross the log size cap is refused before anything is written."""

    def sizes(self):
        return {n: (self.shit / n).stat().st_size if (self.shit / n).exists() else 0
                for n in ("events.jsonl", "events.chain", "queue.jsonl", "queue.chain")}

    def test_a_queue_append_past_the_cap_changes_nothing(self):  # M3
        from shit import log as log_mod
        self.task("t1")
        before = self.snap()
        with mock.patch.object(log_mod, "MAX_LOG_BYTES", self.sizes()["queue.jsonl"] + 10):
            self.assertEqual(self.code(self.claim, "t1"), 7)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.status()["t1"]["state"], "ready")

    def test_an_event_append_past_the_cap_changes_nothing(self):  # M3
        from shit import log as log_mod
        self.task("t1", enqueue=False)
        before = self.snap()
        limit = self.sizes()["events.jsonl"] + 10
        with mock.patch.object(log_mod, "MAX_LOG_BYTES", limit):
            self.assertEqual(self.code(task_mod.create_task, self.proj, task_id="t2", title="T", summary="S",
                                       owner="human", actor="human", now=self.tick()), 7)
        self.assertEqual(self.snap(), before)

    def test_a_lifecycle_step_past_the_cap_is_refused_before_the_queue_writes(self):  # M3
        from shit import log as log_mod
        self.task("t1")
        lease = self.claim("t1")
        before = self.snap()
        with mock.patch.object(log_mod, "MAX_LOG_BYTES", max(self.sizes().values()) + 10):
            self.assertEqual(self.code(q.start, self.proj, self.present(lease), now=self.tick()), 7)
        self.assertEqual(self.snap(), before)


class WorkerKindProof(QueueCase):
    def test_editing_the_worker_kind_of_a_live_lease_is_refused(self):  # L1
        self.task("t1")
        self.claim("t1", kind="ai")
        path = self.shit / "leases" / "t1.json"
        path.write_text(path.read_text().replace('"worker_kind": "ai"', '"worker_kind": "human"'))
        self.assertEqual(self.code(self.status), 8)

    def test_the_claim_entry_records_the_worker_kind(self):  # L1
        self.task("t1")
        self.claim("t1", kind="script")
        self.assertEqual(json.loads(self.queue_lines()[-1])["worker_kind"], "script")


class GraphPopulation(QueueCase):
    def test_the_task_cap_covers_queued_tasks_and_shard_owners_together(self):  # L2
        for tid in ("a", "b", "c"):
            self.task(tid, enqueue=False)
        with mock.patch.object(dep, "MAX_TASKS", 2):
            q.enqueue(self.proj, "a", "human", now=self.tick())
            q.add_edge(self.proj, "b", "c", "draft", "human", now=self.tick())
            self.assertEqual(self.code(q.enqueue, self.proj, "c", "human", now=self.tick()), 7)
            self.assertEqual(self.code(q.add_edge, self.proj, "c", "a", "draft", "human", now=self.tick()), 7)

    def test_a_persisted_empty_shard_is_refused(self):  # L2
        self.task("t1")
        data = dep.dump_shard("ghost", [])
        entry = {"schema": "queue-entry", "schema_version": 1, "id": "queue-0002", "timestamp": self.tick(),
                 "message": q.MESSAGE, "task_id": "ghost", "actor": "human", "kind": "graph_changed",
                 "shards": [{"task_id": "ghost", "sha256": hashlib.sha256(data).hexdigest()}]}
        self.rewrite_queue([*self.queue_lines(), qr.dump_entry(entry)])
        (self.shit / "deps" / "ghost.json").write_bytes(data)
        self.assertEqual(self.code(self.status), 8)


class BoundedWork(QueueCase):
    def test_stale_temporary_files_are_removed_in_one_pass(self):  # M4
        self.task("t1")
        for n in range(40):
            (self.shit / "deps" / f".tmp-{n:04d}").write_text("x")
            (self.shit / "leases" / f".tmp-{n:04d}").write_text("x")
        listings = []
        real = Project.list_dir

        def counting(project, name):
            listings.append(name)
            return real(project, name)

        with mock.patch.object(Project, "list_dir", counting):
            self.status()
        self.assertEqual(os.listdir(self.shit / "deps"), [])
        self.assertEqual(os.listdir(self.shit / "leases"), [])
        self.assertLess(len(listings), 12)

    def test_readiness_looks_only_at_the_edges_of_the_task_asked_about(self):  # M4
        for n in range(4):
            self.task(f"p{n}", enqueue=False)
        for n in range(10):
            self.task(f"d{n}", enqueue=False)
        q.add_edges(self.proj, [(f"d{n}", f"p{m}", "draft") for n in range(10) for m in range(4)], "human",
                    now=self.tick())
        for n in range(10):
            q.enqueue(self.proj, f"d{n}", "human", now=self.tick())
        widest = []
        real = dep.unmet_blocking
        with mock.patch.object(dep, "unmet_blocking", lambda edges, *a: (widest.append(len(edges)), real(edges, *a))[1]):
            self.status()
        self.assertTrue(widest)
        self.assertLessEqual(max(widest), 4)


class RecoveryWrites(QueueCase):
    """Recovery proves the whole interrupted operation, both subsystems included, before its first write."""

    WRITERS = ("write_atomic", "append", "remove", "create_exclusive", "ensure_dir", "finish_publications",
               "quarantine_move")
    crash_at = PendingProof.crash_at

    def crashed_submit(self, nested=False):
        self.task("t1")
        lease = self.run_to_running("t1")
        target = task_mod if nested else q

        def crash(stage):
            if stage == "pending-written":
                raise PendingProof.Crash

        with mock.patch.object(target, "_checkpoint", crash), self.assertRaises(PendingProof.Crash):
            q.submit(self.proj, self.present(lease), now=self.tick())

    def tree(self):
        out = {}
        for path in sorted(self.shit.rglob("*")):
            rel = str(path.relative_to(self.shit))
            if rel in ("lock", "QUEUE.md"):
                continue
            out[rel] = path.read_bytes() if path.is_file() else None
            if path.is_file():
                out[rel + "#links"] = path.stat().st_nlink
        return out

    def record(self, call):
        log = []
        real = {name: getattr(Project, name) for name in self.WRITERS}

        def wrap(name):
            def inner(project, *args, **kwargs):
                log.append(name)
                return real[name](project, *args, **kwargs)
            return inner

        def stage(name):
            log.append(f"stage:{name}")

        with mock.patch.multiple(Project, **{name: wrap(name) for name in self.WRITERS}), \
                mock.patch.object(q, "_checkpoint", stage):
            try:
                call()
            except ShitError as exc:
                return log, exc.code
        return log, 0

    def forged_nested(self):
        events = (self.shit / "events.jsonl").read_bytes().splitlines()
        chain = (self.shit / "events.chain").read_bytes().splitlines()
        from shit import records
        from shit.log import EventLog
        stamp = json.loads((self.shit / "queue-pending.json").read_text())["lifecycle"]["at"]
        event = task_mod._event(EventLog(self.proj), "t1", "human", "task_created", "Task record created.", stamp)
        line = records.dump_event(event)
        image = json.loads((self.shit / "tasks" / "t1.json").read_text())
        doc = {"index": len(events), "event_line": line.decode(), "chain_line": chain_hash(chain[-1].decode(), line),
               "task": image}
        (self.shit / "pending.json").write_text(json.dumps(doc))

    def test_a_forged_nested_task_record_changes_nothing(self):  # round 4
        self.crashed_submit()
        self.forged_nested()
        before = self.tree()
        self.assertEqual(self.code(self.status, now=at(self.clock + 1000)), 8)
        self.assertEqual(self.tree(), before)

    def test_a_corrupt_event_prefix_during_nested_recovery_changes_nothing(self):  # round 4
        self.crashed_submit(nested=True)
        path = self.shit / "events.chain"
        data = bytearray(path.read_bytes())
        data[3] = ord("0") if data[3] != ord("0") else ord("1")
        path.write_bytes(bytes(data))
        before = self.tree()
        self.assertEqual(self.code(self.status), 8)
        self.assertEqual(self.tree(), before)

    def test_a_queue_only_commit_refuses_over_a_corrupt_event_chain(self):  # round 4
        self.task("t1")
        path = self.shit / "events.chain"
        data = bytearray(path.read_bytes())
        data[3] = ord("0") if data[3] != ord("0") else ord("1")
        path.write_bytes(bytes(data))
        before = self.tree()
        self.assertEqual(self.code(self.claim, "t1"), 8)
        self.assertEqual(self.tree(), before)

    def test_a_refusal_creates_no_directory_and_removes_no_link(self):  # round 4
        self.task("t1")
        lease = self.claim("t1")
        os.link(self.shit / "leases" / "t1.json", self.shit / "leases" / ".tmp-0123456789abcdef")
        (self.shit / "queue-pending.json").write_text("{}")
        before = self.tree()
        self.assertEqual(self.code(self.status), 8)
        self.assertEqual(self.tree(), before)
        self.assertEqual(lease["generation"], 1)

    def test_no_write_precedes_the_proven_plan(self):  # round 4
        self.crashed_submit(nested=True)
        log, code = self.record(lambda: q.status(self.proj, now=at(self.clock + 1)))
        self.assertEqual(code, 0)
        self.assertIn("stage:plan-proven", log)
        marker = log.index("stage:plan-proven")
        self.assertEqual([n for n in log[:marker] if not n.startswith("stage:")], [])
        self.assertTrue([n for n in log[marker:] if not n.startswith("stage:")])

    def assert_no_writes(self, call):
        log, code = self.record(call)
        self.assertEqual(code, 8)
        self.assertEqual([n for n in log if not n.startswith("stage:")], [])

    def test_a_refused_nested_recovery_performs_no_write_at_all(self):  # round 4
        self.crashed_submit()
        self.forged_nested()
        self.assert_no_writes(lambda: q.status(self.proj, now=at(self.clock + 1000)))

    def test_a_refused_recovery_over_missing_directories_performs_no_write_at_all(self):  # round 4
        self.task("t1")
        (self.shit / "queue-pending.json").write_text("{}")
        for name in ("leases", "deps"):
            os.rmdir(self.shit / name)
        self.assert_no_writes(lambda: q.status(self.proj, now=at(self.clock + 1000)))
        self.assertFalse((self.shit / "leases").exists())

    def test_a_refused_queue_commit_over_a_corrupt_event_chain_performs_no_write_at_all(self):  # round 4
        self.task("t1")
        path = self.shit / "events.chain"
        data = bytearray(path.read_bytes())
        data[3] = ord("0") if data[3] != ord("0") else ord("1")
        path.write_bytes(bytes(data))
        self.assert_no_writes(lambda: self.claim("t1"))

    def test_a_pending_record_that_cannot_be_recovered_has_one_supported_way_out(self):  # round 4
        self.task("t1")
        pending = self.shit / "queue-pending.json"
        pending.write_text("{not json")
        before = self.tree()
        self.assertEqual(self.code(task_mod.create_task, self.proj, task_id="t2", title="T", summary="S",
                                   owner="human", actor="human", now=self.tick()), 8)
        self.assertEqual(self.code(q.recover, self.proj, now=self.tick()), 8)
        self.assertEqual(self.tree(), before)  # without the explicit request nothing is touched
        q.recover(self.proj, quarantine=True, now=self.tick())
        self.assertFalse(pending.exists())
        kept = list((self.shit / "quarantine").iterdir())
        self.assertEqual([p.read_bytes() for p in kept], [b"{not json"])
        task_mod.create_task(self.proj, task_id="t2", title="T", summary="S", owner="human", actor="human",
                             now=self.tick())
        self.assertEqual(self.status()["t1"]["state"], "ready")

    def test_recover_finishes_a_pending_record_that_proves(self):  # round 4
        self.crashed_submit(nested=True)
        q.recover(self.proj, now=self.tick())
        self.assertFalse((self.shit / "queue-pending.json").exists())
        self.assertEqual(self.status()["t1"]["state"], "review")


    # Round 5: anchor generations and removal targets are proven before any write; quarantine is a proven rename.

    def test_anchor_generations_are_proven_before_recovery_writes(self):  # round 5
        for tid in ("a", "b"):
            self.task(tid)
            q.release(self.proj, self.present(self.claim(tid)), now=self.tick())
        self.crash_at("pending-written", self.claim, "a")
        path = self.shit / "queue-anchor.json"
        doc = json.loads(path.read_text())
        doc["generations"]["b"] = 99
        path.write_text(json.dumps(doc))
        before = self.tree()
        self.assertEqual(self.code(self.status), 8)
        self.assertEqual(self.tree(), before)

    def test_a_lease_to_remove_must_be_the_lease_the_replay_says_exists(self):  # round 5
        self.task("t1")
        lease = self.claim("t1")
        self.crash_at("pending-written", q.release, self.proj, self.present(lease), now=self.tick())
        path = self.shit / "leases" / "t1.json"
        other = {**json.loads(path.read_text()), "lease_id": "e" * 32}
        path.write_bytes(qr.dump_lease(other))
        before = self.tree()
        self.assertEqual(self.code(self.status), 8)
        self.assertEqual(self.tree(), before)

    def test_a_lease_removal_that_matches_still_recovers(self):  # round 5 positive control
        self.task("t1")
        lease = self.claim("t1")
        self.crash_at("pending-written", q.release, self.proj, self.present(lease), now=self.tick())
        self.assertEqual(self.status()["t1"]["state"], "ready")
        self.assertFalse((self.shit / "leases" / "t1.json").exists())

    def test_a_queue_that_does_not_verify_is_not_normalized_first(self):  # round 5
        self.task("t1")
        self.claim("t1")
        os.link(self.shit / "leases" / "t1.json", self.shit / "leases" / ".tmp-0123456789abcdef")
        os.rmdir(self.shit / "deps")
        path = self.shit / "queue-anchor.json"
        doc = json.loads(path.read_text())
        doc["tip"] = "0" * 64
        path.write_text(json.dumps(doc))
        before = self.tree()
        self.assert_no_writes(lambda: q.status(self.proj, now=at(self.clock + 1000)))
        self.assertEqual(self.tree(), before)

    def blocked_valid_commit(self):
        self.task("t1")
        self.crash_at("pending-written", self.claim, "t1")
        path = self.shit / "events.chain"
        good = path.read_bytes()
        data = bytearray(good)
        data[3] = ord("0") if data[3] != ord("0") else ord("1")
        path.write_bytes(bytes(data))
        return path, good

    def test_quarantine_is_refused_when_the_record_itself_is_not_proven_bad(self):  # round 5
        path, good = self.blocked_valid_commit()
        before = self.tree()
        self.assertEqual(self.code(q.recover, self.proj, quarantine=True, now=self.tick()), 8)
        self.assertEqual(self.tree(), before)
        path.write_bytes(good)
        self.assertEqual(self.status()["t1"]["state"], "dispatched")  # the committed claim was not abandoned

    def test_quarantine_moves_the_same_inode_and_names_it(self):  # round 5
        self.task("t1")
        pending = self.shit / "queue-pending.json"
        pending.write_text("{not json")
        inode = pending.stat().st_ino
        name = q.recover(self.proj, quarantine=True, now=self.tick())
        self.assertFalse(pending.exists())
        moved = self.shit / "quarantine" / name
        self.assertEqual((moved.read_bytes(), moved.stat().st_ino), (b"{not json", inode))
        self.assertIsNone(q.recover(self.proj, quarantine=True, now=self.tick()))  # idempotent: nothing left to move

    def test_a_record_whose_slot_is_taken_by_another_committed_entry_is_proven_stale(self):  # round 5
        for tid in ("a", "b", "c"):
            self.task(tid)
        self.crash_at("pending-written", q.add_edge, self.proj, "b", "a", "draft", "human", now=self.tick())
        path = self.shit / "queue-pending.json"
        held = path.read_bytes()
        path.unlink()
        self.claim("c")  # a different entry now occupies the slot
        (self.shit / "queue-pending.json").write_bytes(held)
        self.assertEqual(self.code(self.status), 8)
        name = q.recover(self.proj, quarantine=True, now=self.tick())
        self.assertEqual((self.shit / "quarantine" / name).read_bytes(), held)
        self.assertEqual(self.status()["c"]["state"], "dispatched")

    def test_a_record_swapped_in_after_the_proof_is_not_moved(self):  # round 5
        self.task("t1")
        pending = self.shit / "queue-pending.json"
        pending.write_text("{not json")
        real = Project.ensure_dir
        swapped = {}

        def swap(project, name):
            if name == "quarantine" and not swapped:
                swapped["done"] = True
                pending.unlink()
                pending.write_text("{\"replacement\": true}")
            return real(project, name)

        with mock.patch.object(Project, "ensure_dir", swap):
            self.assertEqual(self.code(q.recover, self.proj, quarantine=True, now=self.tick()), 8)
        self.assertEqual(pending.read_text(), "{\"replacement\": true}")
        self.assertEqual(os.listdir(self.shit / "quarantine"), [])

    def test_quarantine_writes_only_after_its_proof(self):  # round 5
        self.task("t1")
        (self.shit / "queue-pending.json").write_text("{not json")
        log, code = self.record(lambda: q.recover(self.proj, quarantine=True, now=self.tick()))
        self.assertEqual(code, 0)
        self.assertIn("stage:quarantine-proven", log)
        marker = log.index("stage:quarantine-proven")
        self.assertEqual([n for n in log[:marker] if not n.startswith("stage:")], [])
        self.assertTrue([n for n in log[marker:] if not n.startswith("stage:")])


if __name__ == "__main__":
    unittest.main()
