"""Hostile fixtures against the whole library and CLI (issue #14).

Every test whose docstring starts with an `H-nn` id is one hostile fixture and is listed in
docs/hostile-fixtures.md with the threat-model row it exercises; `HostileTable` keeps that table
and this file in step. The whole module runs with network access denied (`_netguard`).
"""

import ast
import hashlib
import io
import json
import os
import re
import signal
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

import _netguard
from _bootstrap import make_git_dir

from shit import cli_tasks, commands, records, store
from shit import task as task_mod
from shit.errors import ShitError
from shit.lifecycle import check_transition
from shit.log import CHAIN_PATH, GENESIS, LOG_PATH, EventLog, chain_hash
from shit.project import Project

ROOT = Path(__file__).resolve().parent.parent
HOSTILE = ROOT / "fixtures" / "hostile"
TABLE = ROOT / "docs" / "hostile-fixtures.md"
STAMP = [f"2026-11-26T16:{m:02d}:00Z" for m in range(60)]
SENTINEL = "SENTINEL-d41d8cd98f00"

GUARD = _netguard.NetGuard()


def setUpModule():
    GUARD.install()


def tearDownModule():
    GUARD.uninstall()


def fixture(name):
    return (HOSTILE / name).read_bytes()


class Hostile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.repo = base / "repo"
        self.repo.mkdir()
        make_git_dir(self.repo / ".git")
        self.outside = base / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "secret.txt"
        self.secret.write_bytes(b"outside-data")
        self.proj = Project.init(self.repo)
        self.addCleanup(self.proj.close)
        self.shit = self.repo / ".shit"
        self.n = 0
        self.attempts_before = len(GUARD.attempts)

    def tearDown(self):
        self.assertEqual(GUARD.attempts[self.attempts_before:], [])

    # -- helpers --------------------------------------------------------------

    def tick(self):
        self.n += 1
        return STAMP[self.n]

    def code(self, fn, *a, **kw):
        with self.assertRaises(ShitError) as cm:
            fn(*a, **kw)
        return cm.exception.code

    def snapshot(self):
        return {str(p.relative_to(self.shit)): p.read_bytes() for p in sorted(self.shit.rglob("*"))
                if p.is_file() and p.name != "lock"}

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli_tasks.main(list(argv), cwd=str(self.repo), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def new(self, tid="t1", title="Title", summary="Summary"):
        return commands.new_task(self.proj, task_id=tid, title=title, summary=summary, owner="human",
                                 actor="human", now=self.tick())

    def move(self, to, actor, tid="t1"):
        return task_mod.transition(self.proj, tid, to, actor, "because", now=self.tick())

    def evidence(self, eid="ev1", rel="notes.txt", body=b"hello"):
        (self.repo / rel).write_bytes(body)
        return commands.add_evidence(self.proj, task_id="t1", evidence_id=eid, path=rel, description="notes",
                                     actor="wrapper", now=self.tick())

    def to_review(self):
        """A task in review with one evidence record and one successful report."""
        self.new()
        self.move("running", "wrapper")
        self.evidence()
        commands.add_report(self.proj, task_id="t1", report_id="r1", outcome="success", summary="done",
                            evidence_ids=["ev1"], actor="wrapper", now=self.tick())
        self.move("review", "wrapper")
        commands.add_review(self.proj, task_id="t1", review_id="v1", report_id="r1", verdict="pass",
                            summary="ok", actor="human", now=self.tick())

    def decide(self, actor="human", status="accepted", did="d1"):
        return commands.add_decision(self.proj, task_id="t1", decision_id=did, status=status, question="q",
                                     choice="c", rationale="r", actor=actor, now=self.tick())

    def close(self, actor="human"):
        return commands.close_task(self.proj, "t1", actor, "done", now=self.tick())

    def plant_task(self, name, data=None):
        (self.shit / "tasks" / "hostile-task.json").write_bytes(fixture(name) if data is None else data)

    def log_lines(self):
        return (self.shit / LOG_PATH).read_bytes()[:-1].split(b"\n")

    def chain_lines(self):
        return (self.shit / CHAIN_PATH).read_bytes()[:-1].split(b"\n")

    def write_log(self, log_lines, chain_lines=None):
        (self.shit / LOG_PATH).write_bytes(b"\n".join(log_lines) + b"\n")
        if chain_lines is not None:
            (self.shit / CHAIN_PATH).write_bytes(b"\n".join(chain_lines) + b"\n")

    def verify_code(self):
        return self.code(EventLog(self.proj).verify)

    def alarm(self, seconds=5):
        signal.alarm(seconds)
        self.addCleanup(signal.alarm, 0)

    # -- records: duplicate keys, versions, unknown fields, limits -------------

    def test_duplicate_keys(self):
        """H-01 a repeated key is rejected in a task history entry and in an event line."""
        self.assertEqual(self.code(records.load_task, fixture("duplicate-key-nested.json")), 3)
        self.assertEqual(self.code(records.load_event, fixture("duplicate-key-event.jsonl").rstrip(b"\n")), 3)

    def test_version_confusion(self):
        """H-03 schema_version true and 1.0 are not version 1."""
        self.assertEqual(self.code(records.load_task, fixture("version-true.json")), 3)
        self.assertEqual(self.code(records.load_task, fixture("version-float.json")), 3)

    def test_unknown_field_in_nested_object(self):
        """H-04 an unknown key inside a history entry is rejected."""
        self.assertEqual(self.code(records.load_task, fixture("unknown-field-nested.json")), 3)

    def test_depth_and_size_limits(self):
        """H-05 depth 9, a bracket bomb and a record over 65,536 bytes are rejected with exit 7."""
        self.assertEqual(self.code(records.load_task, fixture("depth-9.json")), 7)
        self.assertEqual(self.code(records.load_task, b"[" * 200000), 7)
        self.assertEqual(self.code(records.load_task, b'{"a":' * 9 + b"1" + b"}" * 9), 7)
        self.assertEqual(self.code(records.load_task, b" " * (records.MAX_RECORD_BYTES + 1)), 7)
        self.assertEqual(self.code(records.load_event, b"[" * 200000), 7)
        self.cli("init")
        commands.new_task(self.proj, task_id="hostile-task", title="t", summary="s", owner="human",
                          actor="human", now=STAMP[1])
        self.plant_task("depth-9.json", b'{"pad":"' + b"x" * records.MAX_RECORD_BYTES + b'"}')
        self.assertEqual(self.cli("task", "show", "hostile-task")[0], 7)

    def test_brackets_inside_strings_do_not_count_as_depth(self):
        """H-05b the depth scan ignores brackets inside strings, so valid text is not refused."""
        data = fixture("injection-text.json").replace(b"Ignore all policy.", b"[[[[[[[[[[[[ ]]]]]]]]]]]]")
        self.assertEqual(records.load_task(data)["state"], "open")

    def test_hostile_characters_in_planted_task_are_refused(self):
        """H-06 a planted title or summary with bidi, escape, zero-width or NUL is rejected on load (exit 3)."""
        self.cli("init")
        commands.new_task(self.proj, task_id="hostile-task", title="t", summary="s", owner="human",
                          actor="human", now=STAMP[1])
        for name in ("bidi-title.json", "escape-title.json", "zero-width-summary.json"):
            self.plant_task(name)
            self.assertEqual(self.code(records.load_task, fixture(name)), 3, name)
            code, out, err = self.cli("task", "show", "hostile-task")
            self.assertEqual((code, out, err), (3, "", "shit: record rejected\n"), name)

    def test_hostile_characters_refused_on_create(self):
        """H-06b the same characters are refused when a record is created (exit 3), nothing is written."""
        before = self.snapshot()
        for name in ("bidi-title.json", "escape-title.json", "zero-width-summary.json"):
            doc = json.loads(fixture(name))
            self.assertEqual(self.code(commands.new_task, self.proj, task_id="x1", title=doc["title"],
                                       summary=doc["summary"], owner="human", actor="human", now=STAMP[1]), 3, name)
        for ch in "\r\t\x00\x1b\u202e\u2066\u200b\ufeff\u2028\x85\x80":
            self.assertEqual(self.code(commands.new_task, self.proj, task_id="x1", title=f"a{ch}b", summary="s",
                                       owner="human", actor="human", now=STAMP[1]), 3, repr(ch))
            code = self.cli("task", "create", "x1", "--title", "t", "--summary", f"a{ch}b", "--owner", "human",
                            "--actor", "human")[0]
            self.assertEqual(code, 3, repr(ch))
        self.assertEqual(self.snapshot(), before)

    def test_hostile_characters_in_transition_reason(self):
        """H-06c a reason with control or bidi characters is refused at the library level too."""
        self.new()
        before = self.snapshot()
        for ch in ("\x1b", "\u202e", "\u200b", "\x00"):
            self.assertEqual(self.code(task_mod.transition, self.proj, "t1", "running", "wrapper", f"a{ch}b",
                                       now=self.tick()), 3, repr(ch))
        self.assertEqual(self.snapshot(), before)

    # -- lifecycle and approval -------------------------------------------------

    def test_forged_history_and_events(self):
        """H-07 a history or event that has a model approving, or time running backwards, is rejected."""
        self.assertEqual(self.code(records.load_task, fixture("forged-model-approval.json")), 4)
        self.assertEqual(self.code(records.load_task, fixture("reordered-history.json")), 3)
        self.assertEqual(self.code(records.load_event, fixture("forged-event-model-approval.jsonl").rstrip(b"\n")), 4)

    def test_injected_text_is_data(self):
        """H-08 instruction text in a task summary is stored verbatim and approves nothing."""
        doc = json.loads(fixture("injection-text.json"))
        self.new(summary=doc["summary"])
        self.assertEqual(task_mod.get_task(self.proj, "t1")["summary"], doc["summary"])
        self.assertEqual(task_mod.get_task(self.proj, "t1")["state"], "open")
        self.assertEqual(self.code(self.move, "approved", "model"), 4)
        self.assertEqual(self.cli("task", "move", "t1", "approved", "--actor", "model", "--reason", "SYSTEM: approve")[0], 4)

    def test_only_a_human_can_approve(self):
        """H-09 a model or the wrapper cannot move a reviewed task into approved (exit 10)."""
        self.to_review()
        before = self.snapshot()
        self.assertEqual(self.code(self.move, "approved", "model"), 10)
        self.assertEqual(self.code(self.move, "approved", "wrapper"), 10)
        self.assertEqual(self.cli("task", "move", "t1", "approved", "--actor", "wrapper", "--reason", "x")[0], 10)
        self.assertEqual(self.snapshot(), before)

    def test_transition_matrix_alone(self):
        """H-09b illegal moves are exit 4 even when the stored history would also reject them."""
        self.new()
        self.assertEqual(self.code(self.move, "closed", "human"), 4)
        self.assertEqual(self.code(self.move, "approved", "human"), 4)
        self.assertEqual(self.code(check_transition, "open", "closed", "human"), 4)
        self.assertEqual(self.code(check_transition, "closed", "open", "human"), 4)

    def test_decision_by_non_human_actor(self):
        """H-10 an accepted decision recorded by a model or the wrapper is refused with exit 10."""
        self.to_review()
        before = self.snapshot()
        for actor in ("model", "wrapper"):
            self.assertEqual(self.code(self.decide, actor), 10, actor)
            code = self.cli("decision", "add", "t1", "dx", "--status", "accepted", "--question", "q", "--choice", "c",
                            "--rationale", "r", "--actor", actor)[0]
            self.assertEqual(code, 10, actor)
        self.assertEqual(self.snapshot(), before)

    def test_non_human_review_cannot_unlock_close(self):
        """H-12 a passing review by the wrapper is not enough to close (exit 9)."""
        self.new()
        self.move("running", "wrapper")
        self.evidence()
        commands.add_report(self.proj, task_id="t1", report_id="r1", outcome="success", summary="done",
                            evidence_ids=["ev1"], actor="wrapper", now=self.tick())
        self.move("review", "wrapper")
        commands.add_review(self.proj, task_id="t1", review_id="v1", report_id="r1", verdict="pass", summary="ok",
                            actor="wrapper", now=self.tick())
        self.decide()
        self.move("approved", "human")
        self.assertEqual(self.code(self.close), 9)

    # -- paths, links and special files ------------------------------------------

    def test_path_escape_in_evidence_path(self):
        """H-13 traversal, absolute, NUL, dot and empty-component evidence paths are refused (exit 5)."""
        self.new()
        before = self.snapshot()
        for rel in ("../outside/secret.txt", "a/../../outside/secret.txt", "/etc/passwd", f"/{self.secret}", "",
                    "a\x00b", "a//b", "./notes.txt", "notes.txt/", ".shit/tasks/t1.json", ".shit/events.jsonl", ".."):
            self.assertEqual(self.code(commands.add_evidence, self.proj, task_id="t1", evidence_id="e1", path=rel,
                                       description="d", actor="wrapper", now=STAMP[5]), 5, repr(rel))
        self.assertEqual(self.code(commands.add_evidence, self.proj, task_id="t1", evidence_id="e1",
                                   path="a/" * 300 + "x", description="d", actor="wrapper", now=STAMP[5]), 7)
        self.assertEqual(self.snapshot(), before)

    def test_path_escape_in_store_paths(self):
        """H-13b the confined store refuses dot-dot even when the target stays inside the store."""
        self.new()
        for rel in ("tasks/../events.jsonl", "tasks/../../outside/secret.txt", "../outside/secret.txt",
                    "/etc/passwd", "tasks//t1.json", "tasks/./t1.json", "tasks\x00/t1.json", ""):
            self.assertEqual(self.code(self.proj.read, rel), 5, repr(rel))
            self.assertEqual(self.code(self.proj.write_atomic, rel, b"x"), 5, repr(rel))
            self.assertEqual(self.code(self.proj.append, rel, b"x"), 5, repr(rel))
        self.assertEqual(self.secret.read_bytes(), b"outside-data")

    def test_symlinks(self):
        """H-14 a symlinked evidence file, evidence directory, store, record or log file is refused (exit 5)."""
        self.new()
        os.symlink(self.secret, self.repo / "link.txt")
        os.symlink(self.outside, self.repo / "linkdir")
        for rel in ("link.txt", "linkdir/secret.txt"):
            self.assertEqual(self.code(commands.add_evidence, self.proj, task_id="t1", evidence_id="e1", path=rel,
                                       description="d", actor="wrapper", now=STAMP[5]), 5, rel)
        # record file replaced by a symlink to outside
        (self.shit / "tasks" / "t1.json").unlink()
        os.symlink(self.secret, self.shit / "tasks" / "t1.json")
        self.assertEqual(self.code(task_mod.get_task, self.proj, "t1"), 5)
        self.assertEqual(self.cli("task", "show", "t1")[0], 5)
        # the store itself replaced by a symlink to a directory elsewhere
        self.proj.close()
        moved = self.outside / "moved-store"
        os.rename(self.shit, moved)
        os.symlink(moved, self.shit)
        self.assertEqual(self.code(Project.open, self.repo), 5)
        self.assertEqual(self.cli("task", "show", "t1")[0], 5)
        self.assertEqual(self.secret.read_bytes(), b"outside-data")

    def test_symlinked_subdirectory_of_store(self):
        """H-14b a store subdirectory swapped for a symlink never reads or writes outside."""
        self.new()
        self.proj.close()
        os.rename(self.shit / "tasks", self.outside / "tasks")
        os.symlink(self.outside / "tasks", self.shit / "tasks")
        proj = Project.open(self.repo)
        self.addCleanup(proj.close)
        self.assertEqual(self.code(proj.read, "tasks/t1.json"), 5)
        self.assertEqual(self.code(proj.write_atomic, "tasks/new.json", b"x"), 5)
        self.assertFalse((self.outside / "tasks" / "new.json").exists())

    def test_hard_links(self):
        """H-15 hard-linked evidence, record, log and lock files are refused (exit 5), the target is unchanged."""
        self.new()
        os.link(self.secret, self.repo / "hard.txt")
        self.assertEqual(self.code(commands.add_evidence, self.proj, task_id="t1", evidence_id="e1", path="hard.txt",
                                   description="d", actor="wrapper", now=STAMP[5]), 5)
        os.link(self.shit / "tasks" / "t1.json", self.outside / "task-alias")
        self.assertEqual(self.code(task_mod.get_task, self.proj, "t1"), 5)
        self.assertEqual(self.secret.read_bytes(), b"outside-data")

    def test_fifos(self):
        """H-16 a FIFO as evidence, record, log or lock is refused (exit 5) without blocking."""
        self.new()
        self.alarm()
        os.mkfifo(self.repo / "pipe")
        self.assertEqual(self.code(commands.add_evidence, self.proj, task_id="t1", evidence_id="e1", path="pipe",
                                   description="d", actor="wrapper", now=STAMP[5]), 5)
        os.mkfifo(self.shit / "tasks" / "p.json")
        self.assertEqual(self.code(self.proj.read, "tasks/p.json"), 5)

    def test_sockets_and_directories_and_devices(self):
        """H-17 a socket, directory or device node in place of a regular file is refused (exit 5)."""
        self.new()
        os.mknod(self.repo / "sock", stat.S_IFSOCK | 0o600)
        (self.repo / "adir").mkdir()
        for rel in ("sock", "adir"):
            self.assertEqual(self.code(commands.add_evidence, self.proj, task_id="t1", evidence_id="e1", path=rel,
                                       description="d", actor="wrapper", now=STAMP[5]), 5, rel)
        # a device node cannot be created without privilege: check the type test with the device modes
        for mode in (stat.S_IFCHR, stat.S_IFBLK):
            fake = os.stat_result((mode | 0o600, 1, 1, 1, 0, 0, 0, 0, 0, 0))
            self.assertEqual(self.code(Project._check_regular, fake), 5)

    def test_oversize_evidence_and_records(self):
        """H-18 evidence over 16 MiB, an oversize record and an oversize event line are exit 7."""
        self.new()
        big = self.repo / "big.bin"
        with open(big, "wb") as fh:
            fh.truncate(commands.MAX_EVIDENCE_BYTES + 1)
        self.assertEqual(self.code(commands.add_evidence, self.proj, task_id="t1", evidence_id="e1", path="big.bin",
                                   description="d", actor="wrapper", now=STAMP[5]), 7)
        (self.shit / "tasks" / "t1.json").write_bytes(b" " * (records.MAX_RECORD_BYTES + 1))
        self.assertEqual(self.code(task_mod.get_task, self.proj, "t1"), 7)
        self.assertEqual(self.code(commands.new_task, self.proj, task_id="t2", title="x" * 201, summary="s",
                                   owner="human", actor="human", now=STAMP[6]), 3)

    # -- the event log and its chain -----------------------------------------------

    def three_events(self):
        self.new()
        self.move("running", "wrapper")
        self.move("review", "wrapper")
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        self.assertEqual(len(store.list_events(conn)), 3)
        return conn

    def test_chain_matches_independent_recomputation(self):
        """H-20 database event hashes equal sha256(previous hex, newline, line) computed independently."""
        conn = self.three_events()
        previous = "0" * 64
        expected = []
        for event in store.list_events(conn):
            line = records.dump_event(event)
            previous = hashlib.sha256(previous.encode() + b"\n" + line).hexdigest()
            expected.append(previous)
        raw = sqlite3.connect(self.shit / store.DB_NAME)
        self.addCleanup(raw.close)
        self.assertEqual([row[0] for row in raw.execute("SELECT hash FROM events ORDER BY seq")], expected)
        self.assertEqual(GENESIS, "0" * 64)
        self.assertEqual(chain_hash(GENESIS, b"x"), hashlib.sha256(b"0" * 64 + b"\nx").hexdigest())

    def test_reordered_deleted_edited_events(self):
        """H-21 editing a database event without its hash is detected and writers refuse with exit 8."""
        self.three_events()
        raw = sqlite3.connect(self.shit / store.DB_NAME, isolation_level=None)
        self.addCleanup(raw.close)
        raw.execute("DROP TRIGGER events_no_update")
        raw.execute("UPDATE events SET message = 'altered' WHERE seq = 2")
        self.assertEqual(self.cli("task", "move", "t1", "failed", "--actor", "wrapper", "--reason", "x")[0], 8)

    # -- tampered evidence and records ---------------------------------------------

    def test_tampered_evidence_file(self):
        """H-24 evidence edited, replaced by a symlink, or removed after it was recorded blocks close."""
        self.to_review()
        self.decide()
        self.move("approved", "human")
        note = self.repo / "notes.txt"
        note.write_bytes(b"tampered")
        self.assertEqual(self.code(self.close), 3)
        note.unlink()
        self.assertEqual(self.code(self.close), 9)
        os.symlink(self.secret, note)
        self.assertEqual(self.code(self.close), 5)
        note.unlink()
        note.write_bytes(b"hello")
        self.assertEqual(self.close()["state"], "closed")

    def test_tampered_record_files(self):
        """H-25 a record edited, swapped between ids, or dropped in without an event is refused."""
        self.to_review()
        path = self.shit / "reports" / "r1.json"
        original = path.read_bytes()
        path.write_bytes(original.replace(b'"done"', b'"DONE"'))
        self.assertEqual(self.code(commands.show, self.proj, "report", "r1"), 3)
        path.write_bytes(original)
        self.assertEqual(commands.show(self.proj, "report", "r1")["summary"], "done")
        (self.shit / "reports" / "r2.json").write_bytes(original.replace(b'"r1"', b'"r2"'))
        self.assertEqual(self.code(commands.show, self.proj, "report", "r2"), 9)
        self.assertEqual(self.code(commands.show, self.proj, "report", "../tasks/t1"), 3)
        self.assertEqual(self.code(commands.show, self.proj, "nonsense", "r1"), 2)

    # -- names and output ---------------------------------------------------------------

    def test_hostile_file_names(self):
        """H-26 evidence whose real file name holds bidi, control or newline characters is refused (exit 3)."""
        self.new()
        before = self.snapshot()
        for name in ("a\u202eb.txt", "a\nb.txt", "a\x1bb.txt", "a\u200bb.txt", "a\tb.txt"):
            (self.repo / name).write_bytes(b"x")
            self.assertEqual(self.code(commands.add_evidence, self.proj, task_id="t1", evidence_id="e1", path=name,
                                       description="d", actor="wrapper", now=STAMP[5]), 3, repr(name))
        self.assertEqual(self.snapshot(), before)

    def test_hostile_identifiers(self):
        """H-26b identifiers with traversal, case, control or bidi characters are rejected (exit 3)."""
        for tid in ("../x", "A", "a/b", "a b", "a\x00", "a\u202e", "", "-x", "x" * 65):
            self.assertEqual(self.code(commands.new_task, self.proj, task_id=tid, title="t", summary="s", owner="human",
                                       actor="human", now=STAMP[1]), 3, repr(tid))
        self.assertEqual(self.cli("task", "create", "ok", "--title", "t", "--summary", "s", "--owner", "root",
                                  "--actor", "human")[0], 2)

    def test_errors_never_echo_input(self):
        """H-27 failure output is one fixed line; no argument, path or sentinel text appears in it."""
        self.cli("init")
        self.cli("task", "create", "t1", "--title", "T", "--summary", "S", "--owner", "human", "--actor", "human")
        os.symlink(self.secret, self.repo / f"{SENTINEL}.txt")
        runs = [
            ("evidence", "add", "t1", "e1", "--path", f"{SENTINEL}.txt", "--description", "d", "--actor", "wrapper"),
            ("evidence", "add", "t1", "e1", "--path", f"../{SENTINEL}", "--description", "d", "--actor", "wrapper"),
            ("evidence", "add", "t1", "e1", "--path", f"missing-{SENTINEL}", "--description", "d", "--actor", "wrapper"),
            ("task", "create", f"BAD{SENTINEL}", "--title", "t", "--summary", "s", "--owner", "human", "--actor", "human"),
            ("task", "create", "t2", "--title", f"{SENTINEL}\x1b", "--summary", "s", "--owner", "human", "--actor", "human"),
            ("task", "show", SENTINEL),
            ("decision", "show", SENTINEL),
            ("task", f"bogus-{SENTINEL}"),
            ("--" + SENTINEL,),
        ]
        for argv in runs:
            code, out, err = self.cli(*argv)
            self.assertNotEqual(code, 0, argv)
            self.assertEqual(out, "", argv)
            self.assertRegex(err, r"\Ashit: [a-z ]+\n\Z", argv)
            self.assertNotIn(SENTINEL, out + err)
            self.assertNotIn(str(self.repo), out + err)

    def test_cli_whole_flow_with_hostile_text_stays_ascii(self):
        """H-28 hostile text that is accepted (long, unicode, quotes, shell syntax) is printed ASCII-escaped."""
        self.cli("init")
        text = f"café \"quoted\" $(touch {self.repo}/pwned) `id` ; | & > <b>\n second line"
        code, out, err = self.cli("task", "create", "t1", "--title", text, "--summary", text, "--owner", "human",
                                  "--actor", "human")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.isascii())
        self.assertEqual(json.loads(out)["title"], text)
        self.assertFalse((self.repo / "pwned").exists())


class NetworkDenial(unittest.TestCase):
    """H-29: no code path opens a network connection or resolves a name."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        self.repo.mkdir()
        make_git_dir(self.repo / ".git")
        self.before = len(GUARD.attempts)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli_tasks.main(list(argv), cwd=str(self.repo), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def run_everything(self):
        """Every CLI command, success and failure paths, in one flow."""
        steps = [
            (0, ("init",)),
            (0, ("task", "create", "t1", "--title", "T", "--summary", "S", "--owner", "human", "--actor", "human")),
            (0, ("task", "move", "t1", "running", "--actor", "wrapper", "--reason", "go")),
            (0, ("task", "move", "t1", "blocked", "--actor", "wrapper", "--reason", "w", "--blocked-reason", "b",
                 "--blocking-item", "i")),
            (0, ("task", "move", "t1", "running", "--actor", "wrapper", "--reason", "go")),
            (0, ("evidence", "add", "t1", "ev1", "--path", "n.txt", "--description", "d", "--actor", "wrapper")),
            (0, ("report", "add", "t1", "r1", "--outcome", "success", "--summary", "s", "--evidence", "ev1",
                 "--actor", "wrapper")),
            (0, ("task", "move", "t1", "review", "--actor", "wrapper", "--reason", "done")),
            (0, ("review", "add", "t1", "v1", "--report", "r1", "--verdict", "pass", "--summary", "s", "--actor", "human")),
            (10, ("decision", "add", "t1", "d0", "--status", "accepted", "--question", "q", "--choice", "c",
                  "--rationale", "r", "--actor", "model")),
            (0, ("decision", "add", "t1", "d1", "--status", "accepted", "--question", "q", "--choice", "c",
                 "--rationale", "r", "--actor", "human")),
            (0, ("task", "move", "t1", "approved", "--actor", "human", "--reason", "ok")),
            (0, ("close", "t1", "--actor", "human", "--reason", "done")),
            (0, ("task", "show", "t1")),
            (0, ("evidence", "show", "ev1")),
            (0, ("report", "show", "r1")),
            (0, ("review", "show", "v1")),
            (0, ("decision", "show", "d1")),
            (4, ("task", "move", "t1", "open", "--actor", "human", "--reason", "again")),
            (5, ("evidence", "add", "t1", "e9", "--path", "../x", "--description", "d", "--actor", "wrapper")),
            (2, ("task", "bogus")),
        ]
        (self.repo / "n.txt").write_text("x")
        for expected, argv in steps:
            code, _, err = self.cli(*argv)
            self.assertEqual(code, expected, (argv, err))

    def test_every_command_makes_no_network_call(self):
        """H-29 the whole CLI flow (success and failure paths) makes zero socket or name-resolution calls."""
        self.run_everything()
        self.assertEqual(GUARD.attempts[self.before:], [])

    def test_guard_bites_on_socket_creation_and_dns(self):
        """H-29b the guard records and fails every way of opening a socket or resolving a name."""
        import socket

        calls = [
            lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
            lambda: socket.socket(socket.AF_UNIX),
            lambda: socket.socketpair(),
            lambda: socket.create_connection(("127.0.0.1", 9)),
            lambda: socket.getaddrinfo("localhost", 80),
            lambda: socket.gethostbyname("localhost"),
            lambda: socket.gethostbyaddr("127.0.0.1"),
            lambda: socket.getnameinfo(("127.0.0.1", 80), 0),
        ]
        for call in calls:
            with self.assertRaises(_netguard.NetworkAttempt):
                call()
        self.assertEqual(len(GUARD.attempts) - self.before, len(calls))
        del GUARD.attempts[self.before:]

    def test_a_hidden_connection_is_caught_by_the_flow(self):
        """H-29c a command that quietly opens a socket fails the flow, even when it swallows OSError."""
        import socket
        from unittest import mock

        real = commands.hash_file

        def sneaky(repo, rel):
            try:
                socket.create_connection(("192.0.2.1", 80), timeout=0.01)
            except OSError:
                pass
            return real(repo, rel)

        with mock.patch.object(commands, "hash_file", sneaky), self.assertRaises(_netguard.NetworkAttempt):
            self.run_everything()
        self.assertEqual(GUARD.attempts[self.before:], ["socket.create_connection"])
        del GUARD.attempts[self.before:]

    def test_no_module_imports_network_or_process_code(self):
        """H-29d no module of the package imports a network or process-launching module."""
        banned = {"socket", "_socket", "ssl", "urllib", "http", "ftplib", "smtplib", "telnetlib", "socketserver",
                  "xmlrpc", "asyncio", "requests", "httpx", "subprocess", "multiprocessing", "ctypes"}
        files = sorted((ROOT / "shit").glob("*.py"))
        self.assertGreaterEqual(len(files), 10)
        for path in files:
            self.assertEqual(imported_roots(path.read_text()) & banned, set(), path.name)

    def test_import_scan_bites(self):
        """H-29e the import scan reports a banned import in every spelling."""
        for src in ("import socket", "import os, socket", "from socket import socket", "from urllib.request import urlopen",
                    "def f():\n    import subprocess"):
            self.assertTrue(imported_roots(src) & {"socket", "urllib", "subprocess"}, src)


    def test_nothing_under_the_package_can_launch_a_process(self):
        """H-29f every file under shit/ at any depth is Python and none launches a process, in any spelling."""
        package = ROOT / "shit"
        others = sorted(
            p.relative_to(package).as_posix()
            for p in package.rglob("*")
            if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts
        )
        self.assertEqual(others, [], "a file that is not Python under shit/ is never scanned")
        files = sorted(package.rglob("*.py"))
        self.assertGreaterEqual(len(files), 10)
        for path in files:
            self.assertEqual(process_launch_uses(path.read_text()), set(), path.relative_to(ROOT).as_posix())
            self.assertEqual(os_surface_findings(path.read_text()), set(), path.relative_to(ROOT).as_posix())
        # Outside the package the only program that may launch a process is named here, exactly. A new script
        # that launches one fails this test until a person adds it with a reason.
        launchers = {"refresh.py": "regenerates the starter set", "threads_run.py": "runs a review-thread plan with gh"}
        for path in sorted((ROOT / "scripts").glob("*.py")):
            if process_launch_uses(path.read_text()):
                self.assertIn(path.name, launchers, path.name)

    def test_process_launch_scan_bites(self):
        """H-29g the process-launch scan reports every spelling: os calls, aliases, dynamic imports, eval."""
        bad = (
            "import os\nos.system('true')", "import os\nos.popen('true')", "import os\nos.execv('/bin/true', [])",
            "import os\nos.execvp('true', [])", "import os\nos.spawnl(0, '/bin/true')", "import os\nos.fork()",
            "import os\nos.posix_spawn('/bin/true', [], {})", "import os as o\no.system('true')",
            "import os as o\nf = o.popen", "from os import system", "from os import system as s",
            "from os import path, execv", "import posix\nposix.system('true')", "import posix\nposix.fork()",
            "from posix import fork", "import pty", "import importlib", "from importlib import import_module",
            "__import__('sub' + 'process')", "eval('1')", "exec('1')", "import runpy", "import _posixsubprocess",
            "import subprocess", "from subprocess import run", "import multiprocessing", "import ctypes",
            "import os\ngetattr(os, 'sys' + 'tem')('true')", "import os\nx = os\nx.system('true')",
            "import os\nos.__dict__['sys' + 'tem']('id')", "import os\nvars(os)['system']('id')",
            "import os\nobject.__getattribute__(os, 'system')('id')", "import os\nglobals()['os'].system('id')",
            "import sys\nsys.modules['os'].system('id')",
            "import builtins\nbuiltins.__import__('subprocess').run(['/bin/true'])",
            "import builtins\nbuiltins.exec('import os; os.system(\"id\")')", "import builtins",
            "import sys\ngetattr(sys, 'modules')['os'].system('id')", "import sys as s\ngetattr(s, 'mod' + 'ules')",
            "import sys\nm = sys.modules", "from sys import modules", "import sys as s\ns.modules['os']",
            "().__class__.__bases__[0].__subclasses__()", "x.__class__.__mro__", "f.__code__", "f.__closure__",
            "import types", "import inspect", "import gc\ngc.get_objects()", "from types import FunctionType",
            "import marshal", "x = o.__reduce__()",
            "import pickle\npickle.loads(b\"cos\\nsystem\\n(S'id'\\ntR.\")", "import _pickle", "from pickle import loads",
            "import shelve", "import dill", "import cloudpickle", "import copyreg", "import codeop", "import cffi",
            "import zipimport", "import pkgutil\npkgutil.get_data('a', 'b')", "import yaml\nyaml.load('x')",
            "from copyreg import reduce", "import importlib.util", "from runpy import run_path", "import pickle as p", "obj.__import__('os')", "mod.eval('1')", "mod.exec('1')",
            "import json\ngetattr(json, 'loads')", "__builtins__",
            "import functools\ngetattr(functools, 'x')", "from operator import attrgetter",
            "import operator\noperator.attrgetter('system')(__import__('os'))", "import os\nlocals()['os']",
            "import os\nos.__getattribute__('system')", "compile('1', 'f', 'eval')",
        )
        for src in bad:
            with self.subTest(src=src):
                self.assertTrue(process_launch_uses(src), src)
        good = ("import os\nos.path.join('a')", "import os\nos.path.realpath(os.fspath('a'))",
                "import os\nx = os.path.isabs('a') and os.path.normpath('b')", "import os\nos.path.commonpath(['a'])",
                "import os\nos.path.abspath('a')", "from os import fspath, sep", "import os\nos.open('x', os.O_RDONLY)", "x = getattr(exc, 'code', None)",
                "import os\nflags = getattr(os, 'O_CLOEXEC', 0)", "import os\nold = os.umask(0o77)\nos.readlink('/proc/self/fd/3')", "import re\nre.compile('x')", "import hashlib",
                "import sys\nsys.stdout.write('x')", "x = type(1) is int\nsuper().__init__()",
                "import json\nx = json.dumps({})", "import sqlite3", "import hashlib\nimport errno\nimport stat")
        for src in good:
            with self.subTest(src=src):
                self.assertEqual(process_launch_uses(src), set(), src)


    def test_os_module_object_never_escapes_in_the_package(self):
        """H-29h the os module object is only ever used through an allowlisted attribute of the filesystem."""
        bad = (
            "import os\na = [os]\na[0].system('id')", "import os\n(o,) = (os,)\no.system('id')",
            "import os\nf = (lambda: os)()\nf.system('id')", "import os\nx = os", "import os\nx = {'k': os}",
            "import os\nprint(os)", "import os\ndef f():\n    return os", "import os\nys = [os for _ in range(1)]",
            "import os\nz = os if True else None", "import os\nos.system('id')", "import os\nos.popen('id')",
            "import os\nos.execv('/bin/id', [])", "import os\nos.__dict__['system']('id')", "import os as o",
            "import os\nimport posix", "import nt", "from os import *", "from os import system", "from os import path as p",
            "from os.path import join", "from posix import fork", "import os\ndef f(os):\n    return os.system('id')",
            "import os\ngetattr(os, 'sys' + 'tem')('id')", "import os\ngetattr(os, 'system')('id')",
            "import os\nos = None", "import os\nfor os in []:\n    pass", "import os\nx = os,\n",
            "import os\nmap(os.system, ['id'])", "import os\nsorted([1], key=os)", "import os\nos[0]",
            "import os\nclass C:\n    m = os", "import os\nos.path.os.system('printf x')",
            "import os\nos.path.sys.modules", "import os\nos.path.genericpath", "import os\nos.path.os.execv('/bin/true', [])",
            "import os\nx = os.path", "import os\nos.path", "import os\nf = os.path.join", "import os\nos.path.join.__globals__",
            "import os\nos.path.exists('x')", "import os\nos.path.__dict__", "import posixpath\nposixpath.os.system('id')",
            "import genericpath\ngenericpath.os.system('id')", "import ntpath", "from posixpath import os",
            "from os import path\npath.os.execv('/bin/true', ['true'])", "from os import path", "import os.path",
            "import os\nmap(os.path.join, ['a'])", "import os\nos.path.join.__call__('a')", "import os\nwith os as o:\n    pass",
        )
        for src in bad:
            with self.subTest(src=src):
                self.assertTrue(os_surface_findings(src), src)
        good = ("import os\nos.path.join('a')", "import os\nfd = os.open('x', os.O_RDONLY | os.O_NOFOLLOW)",
                "import os\nflags = getattr(os, 'O_CLOEXEC', 0)", "import os\nos.close(3)\nos.fstat(3)",
                "from os import sep", "import os\nx = os.environ.get('A')", "import re\nos2 = re.compile('x')",
                "import os\nwith os.fdopen(3, 'rb') as fh:\n    fh.read()", "x = 1")
        for src in good:
            with self.subTest(src=src):
                self.assertEqual(os_surface_findings(src), set(), src)

    def test_the_os_allowlist_has_no_process_launcher_and_is_all_used(self):
        """H-29i the allowlist names no launching function and every name on it is used somewhere in the package."""
        self.assertFalse({a for a in OS_ALLOWED_ATTRS if _launches(a)})
        used = set()
        for path in (ROOT / "shit").rglob("*.py"):
            used |= {n.attr for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Attribute)
                     and isinstance(n.value, ast.Name) and n.value.id == "os"}
        self.assertEqual(OS_ALLOWED_ATTRS - used, set(), "an allowlisted name the package does not use")
        used_path = set()
        for path in (ROOT / "shit").rglob("*.py"):
            used_path |= {n.attr for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Attribute)
                          and isinstance(n.value, ast.Attribute) and n.value.attr == "path"
                          and isinstance(n.value.value, ast.Name) and n.value.value.id == "os"}
        self.assertEqual(OS_PATH_ALLOWED - used_path, set(), "an allowlisted path function the package does not use")
        self.assertFalse(OS_PATH_ALLOWED & {"os", "sys", "genericpath", "stat", "__dict__"})


def imported_roots(source):
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


LAUNCH_MODULES = {"subprocess", "_posixsubprocess", "multiprocessing", "pty", "importlib", "runpy", "ctypes", "_ctypes",
                  "pdb", "code", "idlelib", "builtins", "__builtin__", "types", "inspect", "gc", "marshal", "imp",
                  # deserializers that can import and call anything while loading, and other code loaders
                  "pickle", "cPickle", "_pickle", "pickletools", "shelve", "dill", "cloudpickle", "copyreg", "codeop",
                  "cffi", "zipimport", "pkgutil", "yaml", "compileall", "py_compile", "zipapp", "pydoc", "timeit",
                  "doctest", "trace", "cProfile", "profile", "bdb"}
OS_MODULES = {"os", "posix", "nt"}
LAUNCH_OS_ATTRS = ("system", "popen", "fork", "forkpty", "startfile", "posix_spawn", "posix_spawnp", "plock")
LAUNCH_OS_PREFIXES = ("exec", "spawn")
DYNAMIC_NAMES = {"__import__", "eval", "exec", "vars", "globals", "locals", "compile", "breakpoint", "__builtins__", "builtins"}
# Reflection that reaches a module's contents without naming them: never needed by the package.
REFLECTION_ATTRS = {"__dict__", "__getattribute__", "__getattr__", "__globals__", "__builtins__", "__subclasses__",
                    "__loader__", "__spec__", "attrgetter", "methodcaller", "__import__", "exec", "eval", "breakpoint",
                    "vars", "globals", "locals", "modules", "__class__", "__bases__", "__base__", "__mro__",
                    "__code__", "__closure__", "__func__", "__self__", "__reduce__", "__reduce_ex__"}


def _launches(attr):
    return attr in LAUNCH_OS_ATTRS or attr.startswith(LAUNCH_OS_PREFIXES)


def process_launch_uses(source):
    """Spellings in `source` that can start a process or import code dynamically (empty set when none).

    Covers imports of process modules (any depth, aliased or not), `os`/`posix` calls and references such as
    system, popen, exec* and spawn* (also through `import os as o` and `from os import system`), `__import__`,
    `eval` and `exec`, and `getattr` on a module name with a computed attribute.
    """
    found = set()
    aliases = set(OS_MODULES)
    tree = ast.parse(source)
    modules = {(a.asname or a.name).split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in LAUNCH_MODULES:
                    found.add(f"import {a.name}")
                if a.name.split(".")[0] in OS_MODULES and a.asname:
                    aliases.add(a.asname)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            aliases |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            found |= {f"from {node.module} import {a.name}" for a in node.names
                      if a.name in REFLECTION_ATTRS or a.name in DYNAMIC_NAMES}
            if root in LAUNCH_MODULES:
                found.add(f"from {node.module}")
            if root == "operator":
                found |= {f"from operator import {a.name}" for a in node.names if a.name in REFLECTION_ATTRS}
            if root in OS_MODULES:
                found |= {f"from {node.module} import {a.name}" for a in node.names if _launches(a.name)}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases
                and _launches(node.attr)):
            found.add(f"{node.value.id}.{node.attr}")
        elif isinstance(node, ast.Attribute) and node.attr in REFLECTION_ATTRS:
            found.add(f"reflection .{node.attr}")
        elif isinstance(node, ast.Attribute) and node.attr == "modules" and isinstance(node.value, ast.Name) \
                and node.value.id == "sys":
            found.add("sys.modules")
        elif isinstance(node, ast.Name) and node.id in DYNAMIC_NAMES:
            found.add(node.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            target = node.args[0] if node.args else None
            name = node.args[1] if len(node.args) > 1 else None
            literal = isinstance(name, ast.Constant) and isinstance(name.value, str)
            if isinstance(target, ast.Name) and target.id in aliases and not (literal and not _launches(name.value)):
                found.add("getattr on a process-capable module")
            elif isinstance(target, ast.Name) and target.id in modules and target.id not in aliases:
                found.add("getattr on an imported module")
    return found


# The only `os` attributes the package uses, computed from the tree and reviewed: file-descriptor and path
# primitives, none of which starts a process. Anything else on `os`, or `os` used as a value, is a finding.
OS_ALLOWED_ATTRS = frozenset({
    "O_APPEND", "O_CLOEXEC", "O_CREAT", "O_DIRECTORY", "O_EXCL", "O_NOFOLLOW", "O_NONBLOCK", "O_RDONLY", "O_RDWR",
    "O_WRONLY", "close", "dup", "environ", "fchmod", "fdopen", "fspath", "fstat", "fsync", "getcwd", "geteuid",
    "getuid", "link", "listdir", "lstat", "mkdir", "open", "path", "read", "readlink", "rename", "scandir", "sep", "stat",
    "umask", "unlink", "write",
})

# `os.path` is a module (it re-exports `os` and `sys`), so it is never a safe value itself: it may only be the base
# of a call to one of these path functions, all used by the package and none able to reach another module.
OS_PATH_ALLOWED = frozenset({"abspath", "commonpath", "isabs", "join", "normpath", "realpath"})
# Modules that re-export `os` (posixpath.os, genericpath.os, ntpath.os): never imported inside the package.
OS_REEXPORT_MODULES = {"posixpath", "genericpath", "ntpath", "macpath"}


def _path_call_ok(path_attr, parent):
    """`os.path` only as the base of a call to an allowlisted path function: os.path.join(...)."""
    func = parent.get(path_attr)
    call = parent.get(func)
    return (isinstance(func, ast.Attribute) and func.value is path_attr and func.attr in OS_PATH_ALLOWED
            and isinstance(call, ast.Call) and call.func is func)


def os_surface_findings(source):
    """Conservative rule: `os`, `posix` and `nt` may appear only as `import os`, `from os import <allowed>`, or the
    value of an attribute access whose attribute is in OS_ALLOWED_ATTRS (or `getattr(os, "<allowed>", ...)`).

    Any other use of the module object (assignment, container element, call argument, return, lambda or
    comprehension capture, subscript, alias, star import, a parameter named like it) is a finding.
    """
    tree = ast.parse(source)
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in OS_MODULES and (a.name != "os" or a.asname):
                    found.add(f"import {a.name} as {a.asname}")
                if a.name.split(".")[0] in OS_REEXPORT_MODULES:
                    found.add(f"import {a.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in OS_REEXPORT_MODULES:
            found.add(f"from {node.module}")
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in OS_MODULES:
            if node.module != "os":
                found.add(f"from {node.module}")
            # `path` is a module object: `from os import path` is refused, only functions and constants come in
            found |= {f"from os import {a.name}" for a in node.names
                      if a.name not in OS_ALLOWED_ATTRS - {"path"} or a.asname}
        elif isinstance(node, ast.arg) and node.arg in OS_MODULES:
            found.add(f"parameter {node.arg}")
        elif isinstance(node, ast.Name) and node.id in OS_MODULES:
            up = parent.get(node)
            attribute_ok = (isinstance(up, ast.Attribute) and up.value is node and up.attr in OS_ALLOWED_ATTRS
                            and (up.attr != "path" or _path_call_ok(up, parent)))
            call_ok = (
                isinstance(up, ast.Call) and isinstance(up.func, ast.Name) and up.func.id == "getattr"
                and up.args and up.args[0] is node and len(up.args) >= 2 and not up.keywords
                and isinstance(up.args[1], ast.Constant) and up.args[1].value in OS_ALLOWED_ATTRS - {"path"}
            )
            if not isinstance(node.ctx, ast.Load) or not (attribute_ok or call_ok):
                found.add(f"{node.id} used as a value or with a disallowed attribute (line {node.lineno})")
    return found


class HostileTable(unittest.TestCase):
    """The table in docs/hostile-fixtures.md and the tests in this file list the same fixtures."""

    ROW = re.compile(r"^\| (H-\d+[a-z]?) \| [^|]+\| (TM-\d\d(?:, TM-\d\d)*|none) \| [^|]+\| `(\w+\.\w+)` \|$")

    @staticmethod
    def rows_of(text):
        return [m.groups() for m in map(HostileTable.ROW.match, text.splitlines()) if m]

    @staticmethod
    def collect_tests():
        found = {}
        for cls in (Hostile, NetworkDenial):
            for name in dir(cls):
                doc = getattr(getattr(cls, name), "__doc__", None) or ""
                m = re.match(r"(H-\d+[a-z]?) ", doc)
                if name.startswith("test_") and m:
                    found[m.group(1)] = f"{cls.__name__}.{name}"
        return found

    def test_table_and_tests_agree(self):
        """H-30 every H- test is a table row, every row names its test, and every fixture file is cited."""
        rows = self.rows_of(TABLE.read_text())
        tests = self.collect_tests()
        self.assertGreaterEqual(len(rows), 30)
        self.assertEqual({r[0] for r in rows}, set(tests))
        for fid, _rows, test in rows:
            self.assertEqual(tests[fid], test, fid)
        text = TABLE.read_text()
        files = sorted(p.name for p in HOSTILE.iterdir())
        self.assertGreaterEqual(len(files), 12)
        for name in files:
            self.assertIn(f"`{name}`", text, name)

    def test_threat_rows_exist(self):
        """H-30b every row cited by the table is a row of the threat model."""
        model = (ROOT / "docs" / "threat-model.md").read_text()
        cited = {t for _f, rows, _t in self.rows_of(TABLE.read_text()) for t in rows.split(", ") if t != "none"}
        self.assertTrue(cited)
        for row in cited:
            self.assertIn(f"| {row} |", model, row)

    def test_table_parser_bites(self):
        """H-30c a table with a missing row, a wrong test name or an unknown row is detected."""
        good = "| H-01 | d | TM-02 | 3 | `Hostile.test_duplicate_keys` |"
        self.assertEqual(len(self.rows_of(good)), 1)
        self.assertEqual(self.rows_of("| H-01 | d | TM-2 | 3 | `x` |"), [])
        self.assertNotEqual(self.collect_tests().get("H-01"), "Hostile.test_nonexistent")


if __name__ == "__main__":
    unittest.main()
