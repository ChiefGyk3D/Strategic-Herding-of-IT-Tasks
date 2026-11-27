"""Append-only hash-chained event log tests (issue #9)."""

import tempfile
import unittest
from pathlib import Path

from _bootstrap import make_git_dir

from shit import records
from shit.errors import ShitError
from shit.log import GENESIS, EventLog, chain_hash
from shit.project import Project


def ev(i, **over):
    base = {
        "schema": "event-log", "schema_version": 1, "id": f"event-{i:04d}", "task_id": f"t{i}",
        "timestamp": "2026-11-26T15:30:00Z", "actor": "human", "event": "task_created",
        "message": "Task record created.", "record_id": f"t{i}",
    }
    base.update(over)
    return base


class LogBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        self.repo.mkdir()
        make_git_dir(self.repo / ".git")
        self.proj = Project.init(self.repo)
        self.addCleanup(self.proj.close)
        self.log = EventLog(self.proj)
        self.shit = self.repo / ".shit"

    def code(self, fn, *a, **kw):
        with self.assertRaises(ShitError) as cm:
            fn(*a, **kw)
        return cm.exception.code

    def fill(self, n=3):
        with self.proj.locked(timeout=1):
            for i in range(1, n + 1):
                self.log.append(ev(i))


class AppendAndVerify(LogBase):
    def test_append_and_read_back(self):
        self.fill(3)
        events = self.log.verify()
        self.assertEqual([e["id"] for e in events], ["event-0001", "event-0002", "event-0003"])

    def test_empty_log_verifies(self):
        self.assertEqual(self.log.verify(), [])

    def test_append_is_append_only_and_chain_has_one_hash_per_event(self):
        self.fill(2)
        first = (self.shit / "events.jsonl").read_bytes()
        with self.proj.locked(timeout=1):
            self.log.append(ev(3))
        after = (self.shit / "events.jsonl").read_bytes()
        self.assertTrue(after.startswith(first))
        self.assertEqual(len((self.shit / "events.chain").read_text().split()), 3)

    def test_invalid_event_not_appended(self):
        with self.proj.locked(timeout=1):
            self.assertEqual(self.code(self.log.append, ev(1, event="nope")), 3)
            self.assertEqual(self.code(self.log.append, ev(1, event="state_transition", from_state="closed", to_state="open")), 4)
        self.assertFalse((self.shit / "events.jsonl").exists())

    def test_duplicate_event_id_rejected(self):
        self.fill(1)
        with self.proj.locked(timeout=1):
            self.assertEqual(self.code(self.log.append, ev(1)), 3)

    def test_append_requires_lock(self):
        self.assertEqual(self.code(self.log.append, ev(1)), 1)


def tr(i, task, frm, to, stamp, actor="wrapper"):
    return ev(i, task_id=task, record_id=task, event="state_transition", message="State transition recorded.",
              actor=actor, timestamp=stamp, from_state=frm, to_state=to)


class Chronology(LogBase):
    """Forged logs with a valid chain, so only the lifecycle rules can reject them."""

    def forge(self, events):
        prev, chain, lines = GENESIS, [], []
        for event in events:
            line = records.dump_event(event)
            prev = chain_hash(prev, line)
            lines.append(line)
            chain.append(prev)
        (self.shit / "events.jsonl").write_bytes(b"".join(x + b"\n" for x in lines))
        (self.shit / "events.chain").write_bytes(b"".join(c.encode("ascii") + b"\n" for c in chain))

    def created(self, i, task, stamp):
        return ev(i, task_id=task, record_id=task, timestamp=stamp)

    def test_forged_valid_sequence_verifies(self):
        self.forge([self.created(1, "t1", "2026-11-26T15:30:00Z"), tr(2, "t1", "open", "running", "2026-11-26T15:31:00Z"),
                    tr(3, "t1", "running", "review", "2026-11-26T15:32:00Z")])
        self.assertEqual(len(self.log.verify()), 3)

    def test_backwards_timestamp_rejected(self):
        self.forge([self.created(1, "t1", "2026-11-26T15:31:00Z"), self.created(2, "t2", "2026-11-26T15:30:00Z")])
        self.assertEqual(self.code(self.log.verify), 3)

    def test_transition_must_start_from_previous_state(self):
        self.forge([self.created(1, "t1", "2026-11-26T15:30:00Z"),
                    tr(2, "t1", "running", "review", "2026-11-26T15:31:00Z")])
        self.assertEqual(self.code(self.log.verify), 3)

    def test_first_transition_must_start_from_open(self):
        self.forge([tr(1, "t1", "running", "review", "2026-11-26T15:31:00Z")])
        self.assertEqual(self.code(self.log.verify), 3)

    def test_replayed_old_transition_with_new_id_rejected(self):
        self.forge([self.created(1, "t1", "2026-11-26T15:30:00Z"),
                    tr(2, "t1", "open", "running", "2026-11-26T15:31:00Z"),
                    tr(3, "t1", "running", "review", "2026-11-26T15:32:00Z"),
                    tr(4, "t1", "open", "running", "2026-11-26T15:33:00Z")])
        self.assertEqual(self.code(self.log.verify), 3)

    def test_second_creation_of_same_task_rejected(self):
        self.forge([self.created(1, "t1", "2026-11-26T15:30:00Z"), self.created(2, "t1", "2026-11-26T15:31:00Z")])
        self.assertEqual(self.code(self.log.verify), 3)


class Tamper(LogBase):
    def test_modified_line_detected(self):
        self.fill(3)
        path = self.shit / "events.jsonl"
        path.write_bytes(path.read_bytes().replace(b"Task record created.", b"Task record forged!!", 1))
        self.assertEqual(self.code(self.log.verify), 3)

    def test_deleted_line_detected(self):
        self.fill(3)
        path = self.shit / "events.jsonl"
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(lines[0] + lines[2])
        self.assertEqual(self.code(self.log.verify), 3)

    def test_reordered_lines_detected(self):
        self.fill(3)
        path = self.shit / "events.jsonl"
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(lines[1] + lines[0] + lines[2])
        self.assertEqual(self.code(self.log.verify), 3)

    def test_truncated_tail_detected(self):
        self.fill(3)
        path = self.shit / "events.jsonl"
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(lines[:2]))
        self.assertEqual(self.code(self.log.verify), 3)

    def test_missing_chain_file_detected(self):
        self.fill(2)
        (self.shit / "events.chain").unlink()
        self.assertEqual(self.code(self.log.verify), 3)

    def test_chain_file_altered_detected(self):
        self.fill(2)
        path = self.shit / "events.chain"
        text = path.read_text()
        path.write_text(("0" if text[0] != "0" else "1") + text[1:])
        self.assertEqual(self.code(self.log.verify), 3)

    def test_blank_line_and_missing_final_newline_rejected(self):
        self.fill(2)
        path = self.shit / "events.jsonl"
        data = path.read_bytes()
        path.write_bytes(data + b"\n")
        self.assertEqual(self.code(self.log.verify), 3)
        path.write_bytes(data[:-1])
        self.assertEqual(self.code(self.log.verify), 3)

    def test_append_refuses_to_extend_a_tampered_log(self):
        self.fill(2)
        path = self.shit / "events.jsonl"
        path.write_bytes(path.read_bytes().replace(b"Task record created.", b"Task record forged!!", 1))
        with self.proj.locked(timeout=1):
            self.assertEqual(self.code(self.log.append, ev(3)), 3)

    def test_oversized_line_is_input_limit(self):
        self.fill(1)
        path = self.shit / "events.jsonl"
        path.write_bytes(path.read_bytes() + b"x" * 70000 + b"\n")
        self.assertEqual(self.code(self.log.verify), 7)


if __name__ == "__main__":
    unittest.main()
