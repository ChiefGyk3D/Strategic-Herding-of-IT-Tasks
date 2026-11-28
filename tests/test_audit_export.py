"""The hash-chained audit export (issue 63, C6): format, offline verifier, restore detection, crashes, files, CLI.

Real databases and real files in temporary repositories; nothing about sqlite is mocked. A crash is a child process
that kills itself with SIGKILL between the commit and the export. A whole-store restore is the database file of an
earlier moment copied back over the current one. No inode numbers are compared.
"""

import hashlib
import io
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from _bootstrap import ROOT, make_git_dir
from test_store_queue_adapter import AdapterCase, at
from test_store_queue_safety import Scenarios

from shit import (
    _cli,
    audit,
    audit_export,
    check,
    errors,
    queue_records,
    records,
    store,
    store_queue,
    store_tasks,
)
from shit.errors import ShitError
from shit.log import GENESIS, chain_hash
from shit.project import Project


def code_of(func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except ShitError as exc:
        return exc.code
    return None


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    code = _cli.run(audit._build, ["audit", *argv], out=out, err=err)
    return code, out.getvalue().splitlines(), err.getvalue()


class Flow(AdapterCase):
    """A small real history: two tasks, an edge, and a lease that is claimed, started, renewed, applied, submitted."""

    def flow(self):
        self.task("a")
        self.task("b")
        store_queue.add_edge(self.proj, "b", "a", "apply", "human", now=self.tick())
        request = {"task_id": "a", "step": "apply", "worker": "w1", "worker_kind": "human"}
        lease = store_queue.claim(self.proj, request, now=self.tick())
        present = self.present(lease)
        store_queue.start(self.proj, present, now=self.tick())
        store_queue.renew(self.proj, present, now=self.tick())
        store_queue.apply_start(self.proj, present, ["x/y"], now=self.tick())
        store_queue.apply_finish(self.proj, present, now=self.tick())
        store_queue.submit(self.proj, present, now=self.tick())
        return lease

    def export_bytes(self):
        return audit_export.read_export(self.proj)

    @property
    def export_path(self):
        return self.repo / ".shit" / audit_export.EXPORT_PATH

    def db_path(self):
        return self.repo / ".shit" / store.DB_NAME

    def save_db(self, name):
        """A copy of the whole database as of now (the log is folded into the main file first)."""
        raw = sqlite3.connect(self.db_path(), isolation_level=None)
        try:
            raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            raw.close()
        target = self.repo / name
        shutil.copyfile(self.db_path(), target)
        return target

    def restore_db(self, saved):
        """A whole-store restore: the saved database file replaces the current one (mode 0600, no sidecars)."""
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(self.db_path()) + suffix).unlink(missing_ok=True)
        os.chmod(saved, 0o600)
        os.replace(saved, self.db_path())

    def status(self):
        conn = store.connect(self.proj)
        try:
            return audit_export.status(self.proj, conn)
        finally:
            conn.close()

    def unexported(self, task_id):
        """A task written with its export skipped, as if the process had died between the commit and the export."""
        with mock.patch.object(audit_export, "sync", lambda *args, **kwargs: False):
            self.task(task_id, enqueue=False)


# -- forged exports -----------------------------------------------------------------------------------------------


def parse_export(data):
    """{tag: [record dict]} of the E and Q lines of an export."""
    out = {"E": [], "Q": []}
    for line in data.split(b"\n")[1:-2]:
        tag, _seq, _hash, text = line.split(b" ", 3)
        out[tag.decode()].append(json.loads(text))
    return out


def rechain(data, edit):
    """`data` with `edit(events, entries)` applied to its records, ids renumbered, hashes and footer recomputed.

    The result has valid chains and a valid footer, so only the replay rules can find what `edit` made wrong."""
    parsed = parse_export(data)
    events, entries = parsed["E"], parsed["Q"]
    edit(events, entries)
    chains = {}
    for name, items, dump, prefix in (("events", events, records.dump_event, "event-"),
                                      ("queue", entries, queue_records.dump_entry, "queue-")):
        previous, rows = GENESIS, []
        for number, item in enumerate(items, start=1):
            item["id"] = prefix + format(number, "04d")
            line = dump(item)
            previous = chain_hash(previous, line)
            rows.append((number, previous, line))
        chains[name] = rows
    return audit_export.render(chains)


def refooter(lines):
    """`lines` (records, then the footer; no trailing empty element) as bytes with the footer's body digest recomputed,
    so that a change to a record cannot be found by the digest alone."""
    body = b"".join(line + b"\n" for line in lines[:-1])
    parts = lines[-1].split(b" ")
    parts[-1] = hashlib.sha256(body).hexdigest().encode("ascii")
    return body + b" ".join(parts) + b"\n"


class VerifierTests(Flow):
    def setUp(self):
        super().setUp()
        self.flow()
        self.good = self.export_bytes()
        self.lines = self.good.split(b"\n")[:-1]  # records and the footer

    def assert_refused(self, data, code=errors.FAILED_CLOSED):
        self.assertEqual(code_of(audit_export.verify_bytes, data), code)

    def test_the_export_of_a_real_history_verifies_and_reports_the_database_tips(self):
        verified = audit_export.verify_bytes(self.good)
        self.assertGreater(verified.events.seq, 3)
        self.assertGreater(verified.queue.seq, 8)
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        tips = store.audit_tips(conn)
        self.assertEqual((verified.events.seq, verified.events.hash), tips["events"])
        self.assertEqual((verified.queue.seq, verified.queue.hash), tips["queue"])

    def test_the_format_is_the_documented_one(self):
        self.assertEqual(self.lines[0], b"SHIT-AUDIT-EXPORT 1")
        self.assertTrue(self.lines[-1].startswith(b"TIP events "))
        self.assertTrue(self.good.endswith(b"\n"))
        tags = [line[:2] for line in self.lines[1:-1]]
        self.assertEqual(set(tags), {b"E ", b"Q "})
        self.assertEqual(tags, sorted(tags))  # every event before every queue entry
        self.good.decode("ascii")

    def test_every_hash_is_the_chain_hash_of_its_line(self):
        rows = {"E": [], "Q": []}
        for line in self.lines[1:-1]:
            tag, seq, digest, text = line.split(b" ", 3)
            rows[tag.decode()].append((int(seq), digest.decode(), text))
        for tag, items in rows.items():
            self.assertGreater(len(items), 3, tag)
            previous = GENESIS
            for number, (seq, digest, text) in enumerate(items, start=1):
                self.assertEqual(seq, number)
                self.assertEqual(chain_hash(previous, text), digest, (tag, seq))
                previous = digest

    def test_rechaining_without_an_edit_gives_the_same_bytes(self):
        self.assertEqual(rechain(self.good, lambda events, entries: None), self.good)
        self.assertEqual(refooter(self.lines), self.good)

    def test_every_change_to_a_record_is_found_by_the_chain_alone(self):
        records_at = [i for i, line in enumerate(self.lines) if line[:2] in (b"E ", b"Q ")]
        self.assertGreater(len(records_at), 10)
        for index in records_at:
            with self.subTest(line=index):
                tail = list(self.lines)
                tail[index] = tail[index][:-3] + b"}x}"
                self.assert_refused(refooter(tail))
                flipped = list(self.lines)
                text = bytearray(flipped[index])
                position = text.index(b'"timestamp":"') + 25  # the hour's second digit
                text[position] = ord("0") if text[position] != ord("0") else ord("1")
                flipped[index] = bytes(text)
                self.assert_refused(refooter(flipped))

    def test_a_removed_swapped_or_duplicated_record_is_found(self):
        first_queue = next(i for i, line in enumerate(self.lines) if line[:2] == b"Q ")
        lines = self.lines
        for name, changed in (
                ("removed", [*lines[:first_queue + 2], *lines[first_queue + 3:]]),
                ("swapped", [*lines[:first_queue], lines[first_queue + 1], lines[first_queue], *lines[first_queue + 2:]]),
                ("duplicated", [*lines[:first_queue + 1], lines[first_queue], *lines[first_queue + 1:]]),
                ("event removed", [*lines[:2], *lines[3:]]),
        ):
            with self.subTest(change=name):
                self.assert_refused(refooter(changed))

    def test_a_truncated_export_is_found_even_with_a_footer_that_matches_its_body(self):
        cut = [*self.lines[:-2], self.lines[-1]]  # the last record is gone; the footer still names it
        self.assert_refused(refooter(cut))
        self.assert_refused(self.good[:-1])  # no final newline
        self.assert_refused(self.good[: len(self.good) // 2])
        self.assert_refused(b"")

    def test_a_footer_that_names_other_tips_or_another_body_is_found(self):
        parts = self.lines[-1].split(b" ")
        self.assertEqual(len(parts), 9)
        mutations = {
            "events seq": [*parts[:2], b"9" + parts[2], *parts[3:]],
            "events hash": [*parts[:3], b"0" * 64, *parts[4:]],
            "queue seq": [*parts[:5], b"999", *parts[6:]],
            "queue hash": [*parts[:6], b"1" * 64, *parts[7:]],
            "body digest": [*parts[:-1], b"2" * 64],
            "empty tip with a hash": [*parts[:2], b"0", *parts[3:]],
        }
        for name, changed in mutations.items():
            with self.subTest(footer=name):
                self.assert_refused(b"\n".join([*self.lines[:-1], b" ".join(changed), b""]))

    def test_the_wrong_magic_extra_lines_and_foreign_bytes_are_found(self):
        lines = self.lines
        cases = {
            "magic": [b"SHIT-AUDIT-EXPORT 2", *lines[1:]],
            "blank line": [*lines[:3], b"", *lines[3:]],
            "unknown tag": [*lines[:3], b"X 1 " + b"0" * 64 + b" {}", *lines[3:]],
            "text after the footer": [*lines, b"extra"],
            "two footers": [*lines, lines[-1]],
        }
        for name, changed in cases.items():
            with self.subTest(case=name):
                self.assert_refused(refooter(changed))
        self.assert_refused(self.good.replace(b"Task record created.", "Tâsk record created.".encode()))
        self.assert_refused(self.good.replace(b"\n", b"\r\n"))
        self.assertEqual(code_of(audit_export.verify_bytes, "text"), errors.USAGE)

    def test_an_event_after_a_queue_entry_is_found_by_the_order_rule_alone(self):
        first_queue = next(i for i, line in enumerate(self.lines) if line[:2] == b"Q ")
        last_event = max(i for i, line in enumerate(self.lines) if line[:2] == b"E ")
        moved = list(self.lines)
        moved.insert(first_queue, moved.pop(last_event))  # now just after the first queue entry
        self.assertLess(next(i for i, line in enumerate(moved) if line[:2] == b"Q "),
                        max(i for i, line in enumerate(moved) if line[:2] == b"E "))
        self.assert_refused(refooter(moved))

    def test_replay_rules_find_what_the_chains_alone_cannot(self):
        def twice(events, entries):
            entries.insert(1, dict(entries[0]))  # the same task enqueued twice

        def lone_claim(events, entries):
            entries[:] = [e for e in entries if e["kind"] != "enqueued" or e["task_id"] != "a"]

        def backwards_time(events, entries):
            entries[-1]["timestamp"] = "2026-11-27T09:00:00Z"

        def claim_before_enqueue(events, entries):
            claimed = next(i for i, e in enumerate(entries) if e["kind"] == "claimed")
            entries.insert(0, entries.pop(claimed))

        def second_creation(events, entries):
            events.append({**events[0], "timestamp": "2026-11-27T12:00:00Z"})  # task_created for a task twice

        def lost_transition(events, entries):
            events.remove(next(e for e in events if e["event"] == "state_transition"))

        def event_time_back(events, entries):
            events[-1]["timestamp"] = "2026-11-27T09:00:00Z"

        edits = (twice, lone_claim, backwards_time, claim_before_enqueue, second_creation, lost_transition,
                 event_time_back)
        for edit in edits:
            with self.subTest(edit=edit.__name__):
                forged = rechain(self.good, edit)  # a valid chain and footer: each record is valid on its own
                self.assert_refused(forged)

    def test_queue_entries_require_a_preceding_task_creation_event(self):
        def remove_creation(events, entries):
            task_id = entries[0]["task_id"]
            events[:] = [
                event
                for event in events
                if not (event["event"] == "task_created" and event["task_id"] == task_id)
            ]

        self.assert_refused(rechain(self.good, remove_creation))

    def test_an_edit_that_replay_accepts_still_verifies(self):
        def later(events, entries):
            entries[-1]["timestamp"] = "2026-11-27T23:00:00Z"
            events[-1]["timestamp"] = "2026-11-27T23:00:00Z"

        audit_export.verify_bytes(rechain(self.good, later))

    def test_over_the_size_limit_is_exit_7_and_a_long_line_is_refused(self):
        with mock.patch.object(audit_export, "MAX_EXPORT_BYTES", len(self.good) - 1):
            self.assertEqual(code_of(audit_export.verify_bytes, self.good), errors.INPUT_LIMIT)
        audit_export.verify_bytes(self.good)
        longer = self.good.replace(b"Task record created.", b"x" * (audit_export.MAX_LINE_BYTES + 10), 1)
        self.assert_refused(longer)

    def test_render_refuses_to_write_past_the_limit(self):
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        with store.read_snapshot(conn):
            chains = store.audit_chains(conn)
        with mock.patch.object(audit_export, "MAX_EXPORT_BYTES", 100):
            self.assertEqual(code_of(audit_export.render, chains), errors.INPUT_LIMIT)


class OfflineFileTests(Flow):
    def setUp(self):
        super().setUp()
        self.flow()
        self.copy = self.repo / "copy.txt"
        self.copy.write_bytes(self.export_bytes())

    def test_a_copy_verifies_by_path_with_no_repository(self):
        code, out, err = run_cli(["verify", "--file", str(self.copy)])
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out[0].startswith("audit-export: ok (events "))
        self.assertEqual([line.split()[1] for line in out[1:]], ["events", "queue"])
        self.assertEqual(len(out), 3)

    def test_a_damaged_copy_is_exit_8_and_prints_nothing_of_it(self):
        data = bytearray(self.copy.read_bytes())
        data[40] ^= 1
        self.copy.write_bytes(bytes(data))
        code, out, err = run_cli(["verify", "--file", str(self.copy)])
        self.assertEqual((code, out), (8, []))
        self.assertIn("exit 8", err)
        self.assertNotIn(str(self.repo), err)

    def test_missing_symlink_and_non_regular_files_use_the_existing_codes(self):
        self.assertEqual(run_cli(["verify", "--file", str(self.repo / "absent")])[0], 9)
        link = self.repo / "link.txt"
        link.symlink_to(self.copy)
        self.assertEqual(run_cli(["verify", "--file", str(link)])[0], 5)
        self.assertEqual(run_cli(["verify", "--file", str(self.repo)])[0], 5)
        fifo = self.repo / "fifo.txt"
        os.mkfifo(fifo)
        self.assertEqual(run_cli(["verify", "--file", str(fifo)])[0], 5)

    def test_an_oversized_copy_is_exit_7(self):
        with mock.patch.object(audit_export, "MAX_EXPORT_BYTES", 10):
            self.assertEqual(run_cli(["verify", "--file", str(self.copy)])[0], 7)

    def test_bad_arguments_are_exit_2(self):
        self.assertEqual(run_cli(["verify", "--nope"])[0], 2)
        self.assertEqual(run_cli([])[0], 2)
        self.assertEqual(run_cli(["verify", "--file"])[0], 2)

    def test_the_project_export_is_verified_when_no_file_is_named(self):
        code, out, _ = run_cli(["verify", "--repo", str(self.repo)])
        self.assertEqual(code, 0)
        self.assertTrue(out[0].startswith("audit-export: ok"))
        self.export_path.unlink()
        self.assertEqual(run_cli(["verify", "--repo", str(self.repo)])[0], 9)

    def test_the_database_option_compares_read_only_and_ahead_is_not_an_error(self):
        code, out, _ = run_cli(["verify", "--repo", str(self.repo), "--database"])
        self.assertEqual((code, out[-1]), (0, "database: database matches the export"))
        self.unexported("c")
        code, out, _ = run_cli(["verify", "--repo", str(self.repo), "--database"])
        self.assertEqual(code, 0)
        self.assertIn("ahead of the export", out[-1])

    def test_the_database_option_fails_for_a_restored_database(self):
        saved = self.save_db("old.sqlite3")
        self.task("later")
        self.restore_db(saved)
        code, out, _ = run_cli(["verify", "--repo", str(self.repo), "--database"])
        self.assertEqual(code, 8)
        self.assertIn("older than the export", out[-1])
        # an offline copy of the newer export still verifies on its own
        self.assertEqual(run_cli(["verify", "--file", str(self.copy)])[0], 0)


class EveryKindTests(Scenarios):
    """Every kind of queue entry the operations write is rebuilt to the exact line its hash was computed over."""

    def test_all_operations_and_expiry_export_and_verify(self):
        self.assertTrue(audit_export.sync(self.proj, self.conn))  # an empty database gets its empty export
        for label, op, args, kwargs in self.prepared():
            getattr(store, op)(self.conn, *args, **kwargs)
        self.task("r-a", enqueue=False)
        self.task("r-b", enqueue=False)
        store.queue_add_edges(self.conn, [("r-b", "r-a", "review")], "human", self.tick())
        store.queue_enqueue(self.conn, "r-b", "human", self.tick())  # waits on an edge nothing meets
        store.queue_remove_edge(self.conn, "r-b", "r-a", "review", "human", self.tick())
        store.queue_maintain(self.conn, at(self.clock + 100000))  # expiries and the promotion of r-b
        self.consistent()
        self.assertTrue(audit_export.sync(self.proj, self.conn))
        verified = audit_export.verify_bytes(audit_export.read_export(self.proj))
        kinds = {entry["kind"] for entry in store.queue_entries(self.conn)}
        self.assertEqual(kinds, {"enqueued", "ready", "claimed", "started", "renewed", "released", "expired",
                                 "apply_started", "apply_finished", "submitted", "failed", "held", "reopened", "done",
                                 "graph_changed"})
        self.assertEqual(verified.queue.seq, self.count("queue_entries"))
        self.assertEqual(verified.events.seq, self.count("events"))

    def test_a_kind_without_a_known_field_order_is_refused_not_guessed(self):
        entry = {"schema": "queue-entry", "schema_version": 1, "id": "queue-0001", "timestamp": at(1), "message": "m",
                 "task_id": "a", "actor": "wrapper", "kind": "orphan_removed", "orphan_sha256": "0" * 64}
        self.assertEqual(code_of(store._queue_line, entry), errors.FAILED_CLOSED)

    def test_an_entry_with_a_column_the_order_does_not_name_is_refused(self):
        entry = {"schema": "queue-entry", "schema_version": 1, "id": "queue-0001", "timestamp": at(1), "message": "m",
                 "task_id": "a", "actor": "human", "kind": "enqueued", "state_after": "ready", "worker": "w"}
        self.assertEqual(code_of(store._queue_line, entry), errors.FAILED_CLOSED)

    def test_a_stored_hash_that_does_not_match_its_line_is_refused_at_export(self):
        self.task("a")
        with store.read_snapshot(self.conn):
            self.assertEqual(len(store.audit_chains(self.conn)["queue"]), 1)
        with mock.patch.object(store, "chain_hash", lambda previous, line: "0" * 64), store.read_snapshot(self.conn):
            self.assertEqual(code_of(store.audit_chains, self.conn), errors.FAILED_CLOSED)


class RestoreTests(Flow):
    """A database older than its export, or different from it, is refused before a write extends it."""

    def setUp(self):
        super().setUp()
        self.task("a")
        self.saved = self.save_db("old.sqlite3")  # the database as of now
        self.task("b")
        lease = self.claim("b")
        store_queue.start(self.proj, self.present(lease), now=self.tick())
        self.current = self.export_bytes()

    def test_before_any_restore_everything_is_current(self):
        self.assertEqual(self.status().state, audit_export.CURRENT)
        self.assertIsNone(code_of(store_tasks.get_task, self.proj, "b"))

    def test_a_restored_older_database_is_refused_by_readers_and_writers(self):
        self.restore_db(self.saved)
        self.assertEqual(self.status().state, audit_export.OLDER)
        self.assertEqual(code_of(store_tasks.get_task, self.proj, "a"), errors.FAILED_CLOSED)
        self.assertEqual(code_of(store_queue.status, self.proj, now=self.tick()), errors.FAILED_CLOSED)
        self.assertEqual(code_of(store_queue.enqueue, self.proj, "a", "human", now=self.tick()), errors.FAILED_CLOSED)
        self.assertEqual(code_of(store_tasks.create_task, self.proj, task_id="z", title="T", summary="S",
                                 owner="human", actor="human", now=self.tick()), errors.FAILED_CLOSED)
        self.assertEqual(self.export_bytes(), self.current)  # nothing was written to the export

    def test_a_restored_database_that_grew_another_history_is_diverged(self):
        self.restore_db(self.saved)
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        for number in range(6):  # direct helper calls write no export: the database now has more entries than it had
            task = {"schema": "task", "schema_version": 1, "id": "n" + str(number), "title": "T", "summary": "S",
                    "state": "open", "owner": "human", "created_at": at(500 + number), "updated_at": at(500 + number),
                    "history": []}
            data = records.dump_task(task)
            store.create_task(conn, task, "human", hashlib.sha256(data).hexdigest(), len(data))
        self.assertEqual(self.status().state, audit_export.DIVERGED)
        self.assertEqual(code_of(store_tasks.get_task, self.proj, "a"), errors.FAILED_CLOSED)
        self.assertEqual(code_of(store_queue.enqueue, self.proj, "a", "human", now=self.tick()), errors.FAILED_CLOSED)
        self.assertEqual(self.export_bytes(), self.current)

    def test_only_the_operator_can_accept_the_database(self):
        self.restore_db(self.saved)
        self.assertEqual(run_cli(["export", "--repo", str(self.repo)])[0], 8)
        self.assertEqual(self.export_bytes(), self.current)
        code, out, _ = run_cli(["export", "--repo", str(self.repo), "--accept-database"])
        self.assertEqual(code, 0)
        self.assertTrue(out[0].startswith("audit-export: written"))
        self.assertNotEqual(self.export_bytes(), self.current)
        self.assertEqual(self.status().state, audit_export.CURRENT)
        self.task("after")  # writes work again

    def test_a_removed_export_stops_writers_but_not_readers(self):
        self.export_path.unlink()
        self.assertEqual(self.status().state, audit_export.MISSING)
        self.assertIsNone(code_of(store_tasks.get_task, self.proj, "a"))
        self.assertEqual(code_of(store_queue.enqueue, self.proj, "b", "human", now=self.tick()), errors.FAILED_CLOSED)
        self.assertFalse(self.export_path.exists())
        self.assertEqual(run_cli(["export", "--repo", str(self.repo)])[0], 8)
        self.assertEqual(run_cli(["export", "--repo", str(self.repo), "--accept-database"])[0], 0)
        self.assertEqual(self.export_bytes(), self.current)

    def test_an_export_that_names_a_longer_or_other_tip_is_refused(self):
        lines = self.current.split(b"\n")
        for name, change in (("longer", lambda p: [*p[:2], str(int(p[2]) + 50).encode(), *p[3:]]),
                             ("other hash", lambda p: [*p[:3], b"a" * 64, *p[4:]])):
            with self.subTest(change=name):
                footer = b" ".join(change(lines[-2].split(b" ")))
                self.export_path.write_bytes(b"\n".join([*lines[:-2], footer, b""]))
                os.chmod(self.export_path, 0o600)
                self.assertIn(self.status().state, (audit_export.OLDER, audit_export.DIVERGED))
                self.assertEqual(code_of(store_tasks.get_task, self.proj, "a"), errors.FAILED_CLOSED)
        self.export_path.write_bytes(self.current)
        self.assertEqual(self.status().state, audit_export.CURRENT)

    def test_a_garbled_export_refuses_and_only_the_operator_replaces_it(self):
        self.export_path.write_bytes(b"not an export\n")
        os.chmod(self.export_path, 0o600)
        self.assertEqual(code_of(store_tasks.get_task, self.proj, "a"), errors.FAILED_CLOSED)
        self.assertEqual(code_of(store_queue.enqueue, self.proj, "b", "human", now=self.tick()), errors.FAILED_CLOSED)
        self.assertEqual(self.export_path.read_bytes(), b"not an export\n")
        self.assertEqual(run_cli(["export", "--repo", str(self.repo), "--accept-database"])[0], 0)
        self.assertEqual(self.export_bytes(), self.current)

    def test_a_new_store_with_no_export_and_no_entries_is_not_a_restore(self):
        other = self.repo.parent / "other"
        other.mkdir()
        make_git_dir(other / ".git")
        project = Project.init(other)
        self.addCleanup(project.close)
        store.init_store(project)
        conn = store.connect(project)
        self.addCleanup(conn.close)
        self.assertEqual(audit_export.status(project, conn).state, audit_export.NONE)
        self.assertEqual(audit_export.guard(project, conn).state, audit_export.NONE)
        self.assertTrue(audit_export.sync(project, conn))
        self.assertEqual(audit_export.verify_bytes(audit_export.read_export(project)).events.seq, 0)


class CrashTests(Flow):
    """SIGKILL between the commit and the export: the database has the write, the export is behind, and the next
    open detects it and the next writer repairs it. A crash inside the file replacement leaves the old export."""

    CHILD = """
import os, signal, sys
sys.path.insert(0, sys.argv[1])
from shit import audit_export, store_queue
from shit.project import Project
stage = sys.argv[3]
if stage == "rename":
    def boom(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGKILL)
    os.rename = boom
else:
    def fault(name):
        if name == stage:
            os.kill(os.getpid(), signal.SIGKILL)
    audit_export._fault = fault
project = Project.open(sys.argv[2])
store_queue.enqueue(project, sys.argv[4], "human", now=sys.argv[5])
print("survived")
"""

    def setUp(self):
        super().setUp()
        self.task("a")
        self.task("b", enqueue=False)  # created and exported; not yet enqueued
        self.before = self.export_bytes()
        self.assertEqual(self.status().state, audit_export.CURRENT)

    def crash(self, stage):
        proc = subprocess.run([sys.executable, "-I", "-c", self.CHILD, str(ROOT), str(self.repo), stage, "b",
                               self.tick()], capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(proc.returncode, -signal.SIGKILL, (stage, proc.stdout, proc.stderr[-300:]))
        self.assertEqual(proc.stdout, "")

    def queue_tip(self):
        conn = store.connect(self.proj)
        try:
            return store.audit_tips(conn)["queue"][0]
        finally:
            conn.close()

    def test_a_kill_between_commit_and_export_is_detected_and_repaired_by_the_next_writer(self):
        self.crash("before-write")
        self.assertEqual(self.export_bytes(), self.before)  # the write is in the database, the export is the old file
        self.assertEqual(self.queue_tip(), 2)
        found = self.status()
        self.assertEqual((found.state, found.events_behind, found.queue_behind), (audit_export.BEHIND, 0, 1))
        self.assertIsNone(code_of(store_tasks.get_task, self.proj, "b"))  # readers are not refused
        lease = self.claim("a")  # the next writer repairs the export before it writes
        self.assertEqual(lease["task_id"], "a")
        self.assertEqual(self.status().state, audit_export.CURRENT)
        audit_export.verify_bytes(self.export_bytes())
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        self.assertEqual([e["kind"] for e in store.queue_entries(conn)], ["enqueued", "enqueued", "claimed"])
        self.assertEqual(store.verify(conn), [])

    def test_a_kill_right_after_the_replacement_leaves_a_current_export(self):
        self.crash("written")
        self.assertEqual(self.status().state, audit_export.CURRENT)
        self.assertEqual(audit_export.verify_bytes(self.export_bytes()).queue.seq, 2)

    def test_a_kill_inside_the_replacement_keeps_the_old_export_and_leaves_only_a_temporary_name(self):
        self.crash("rename")
        self.assertEqual(self.export_bytes(), self.before)
        self.assertEqual(self.status().state, audit_export.BEHIND)
        leftovers = [p.name for p in (self.repo / ".shit").iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(len(leftovers), 1)
        self.task("c")  # the next writer repairs it and is not bothered by the leftover
        self.assertEqual(self.status().state, audit_export.CURRENT)
        audit_export.verify_bytes(self.export_bytes())

    def test_a_missed_export_is_a_warning_in_check_and_check_never_repairs_it(self):
        self.crash("before-write")

        def listing():
            return sorted(p.name for p in (self.repo / ".shit").iterdir() if not p.name.startswith(store.DB_NAME))

        before = (self.export_bytes(), self.export_path.stat().st_mtime_ns, listing())
        code, lines = check.run_check(self.repo)
        self.assertEqual(code, 0)
        self.assertTrue(any(line.startswith("warning: store: audit export is 0 event(s) and 1 queue entry")
                            for line in lines), lines)
        self.assertEqual((self.export_bytes(), self.export_path.stat().st_mtime_ns, listing()), before)

    def test_missed_exports_in_a_row_are_repaired_together(self):
        self.crash("before-write")
        self.unexported("c")
        self.unexported("d")
        found = self.status()
        self.assertEqual((found.state, found.queue_behind, found.events_behind), (audit_export.BEHIND, 1, 2))
        self.task("e")
        self.assertEqual(self.status().state, audit_export.CURRENT)
        audit_export.verify_bytes(self.export_bytes())


class FileTests(Flow):
    def setUp(self):
        super().setUp()
        self.flow()

    def test_the_export_is_private_and_whole(self):
        self.assertEqual(stat.S_IMODE(self.export_path.stat().st_mode), 0o600)
        self.assertEqual(self.export_path.stat().st_nlink, 1)
        self.assertEqual([p.name for p in (self.repo / ".shit").iterdir() if p.name.startswith(".tmp-")], [])

    def test_the_mode_holds_under_a_loose_umask(self):
        old = os.umask(0)
        try:
            self.task("loose")
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(self.export_path.stat().st_mode), 0o600)

    def test_a_loose_mode_a_symlink_and_a_hard_link_are_refused(self):
        data = self.export_bytes()
        os.chmod(self.export_path, 0o640)
        self.assertEqual(code_of(store_tasks.get_task, self.proj, "a"), errors.CONFINEMENT)
        self.assertEqual(code_of(audit_export.read_export, self.proj), errors.CONFINEMENT)
        os.chmod(self.export_path, 0o600)
        self.assertIsNone(code_of(store_tasks.get_task, self.proj, "a"))
        other = self.repo / "elsewhere.txt"
        other.write_bytes(data)
        self.export_path.unlink()
        self.export_path.symlink_to(other)
        self.assertEqual(code_of(store_tasks.get_task, self.proj, "a"), errors.CONFINEMENT)
        self.assertEqual(code_of(store_queue.status, self.proj, now=self.tick()), errors.CONFINEMENT)
        self.export_path.unlink()
        os.link(other, self.export_path)
        os.chmod(self.export_path, 0o600)
        self.assertEqual(code_of(store_tasks.get_task, self.proj, "a"), errors.CONFINEMENT)

    def test_a_fifo_in_place_of_the_export_is_refused_without_blocking(self):
        self.export_path.unlink()
        os.mkfifo(self.export_path)
        self.assertEqual(code_of(store_tasks.get_task, self.proj, "a"), errors.CONFINEMENT)

    def test_the_export_is_deterministic(self):
        first = self.export_bytes()
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        with store.read_snapshot(conn):
            rebuilt = audit_export.render(store.audit_chains(conn))
        self.assertEqual(rebuilt, first)

    def test_the_footer_is_read_without_reading_the_whole_file(self):
        with mock.patch.object(audit_export, "read_export", side_effect=AssertionError("whole file read")):
            self.assertEqual(self.status().state, audit_export.CURRENT)
        self.task("more")  # a writer verifies the complete body before accepting the footer
        self.assertEqual(self.status().state, audit_export.CURRENT)

    def test_a_current_database_writes_nothing_to_the_export(self):
        before = (self.export_bytes(), self.export_path.stat().st_mtime_ns)
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        self.assertFalse(audit_export.sync(self.proj, conn))
        self.assertEqual((self.export_bytes(), self.export_path.stat().st_mtime_ns), before)


class WriteFailureTests(Flow):
    def test_a_failed_export_after_a_commit_does_not_hide_the_write_and_is_repaired_next_time(self):
        self.task("a", enqueue=False)
        with mock.patch.object(audit_export, "render", side_effect=ShitError(errors.INPUT_LIMIT)):
            store_queue.enqueue(self.proj, "a", "human", now=self.tick())
        found = self.status()
        self.assertEqual((found.state, found.queue_behind), (audit_export.BEHIND, 1))
        self.assertEqual(store_queue.status(self.proj, now=self.tick())["a"]["state"], "ready")
        self.assertEqual(self.status().state, audit_export.CURRENT)

    def test_an_interrupt_is_never_swallowed(self):
        with mock.patch.object(audit_export, "sync", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            audit_export.after_commit(self.proj, None)

    def test_a_writer_whose_repair_fails_does_not_write(self):
        self.task("a", enqueue=False)
        self.unexported("b")
        before = self.status()
        self.assertEqual(before.state, audit_export.BEHIND)
        with mock.patch.object(audit_export, "render", side_effect=ShitError(errors.INPUT_LIMIT)):
            self.assertEqual(code_of(store_queue.enqueue, self.proj, "a", "human", now=self.tick()), errors.INPUT_LIMIT)
        self.assertEqual(self.status(), before)  # nothing was written, in the database or the export

    def test_a_refused_operation_leaves_the_export_alone(self):
        self.task("a")
        data = self.export_bytes()
        self.assertEqual(code_of(store_queue.enqueue, self.proj, "a", "human", now=self.tick()),
                         errors.REJECTED)
        self.assertEqual(self.export_bytes(), data)


class LockTests(Flow):
    def test_the_export_is_written_while_the_databases_write_lock_is_held(self):
        self.task("a", enqueue=False)
        self.unexported("b")  # the export is behind: the next write must replace it
        seen = []

        def probe(stage):
            if stage != "before-write":
                return
            other = store.connect(self.proj, busy_timeout_ms=0)
            try:
                seen.append(code_of(lambda: store.write_transaction(other).__enter__()))
            finally:
                other.close()

        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        with mock.patch.object(audit_export, "_fault", probe):
            self.assertTrue(audit_export.sync(self.proj, conn))
        self.assertEqual(seen, [errors.BUSY])
        self.assertEqual(self.status().state, audit_export.CURRENT)


class ConcurrencyTests(Flow):
    def test_concurrent_writers_leave_an_export_that_is_current_and_verifies(self):
        self.task("seed")
        failures = []

        def work(name):
            try:
                for number in range(4):
                    task_id = f"{name}{number}"
                    store_tasks.create_task(self.proj, task_id=task_id, title="T", summary="S", owner="human",
                                            actor="human", now=at(1000))
                    store_queue.enqueue(self.proj, task_id, "human", now=at(1000))
            except (ShitError, OSError) as exc:
                failures.append(repr(exc))

        threads = [threading.Thread(target=work, args=(name,)) for name in ("p", "q", "r")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(failures, [])
        self.assertEqual(self.status().state, audit_export.CURRENT)
        verified = audit_export.verify_bytes(self.export_bytes())
        self.assertEqual(verified.queue.seq, 1 + 3 * 4)
        self.assertEqual(verified.events.seq, 1 + 3 * 4)
        self.consistent()


class CommandTests(Flow):
    def test_sync_refuses_a_current_looking_export_with_a_damaged_body(self):
        self.flow()
        data = bytearray(self.export_bytes())
        data[60] ^= 1
        self.export_path.write_bytes(bytes(data))
        os.chmod(self.export_path, 0o600)
        conn = store.connect(self.proj)
        self.addCleanup(conn.close)
        self.assertEqual(code_of(audit_export.sync, self.proj, conn), errors.FAILED_CLOSED)

    def test_export_replaces_a_lost_export_only_on_request_and_then_reports_current(self):
        self.flow()
        self.export_path.unlink()
        self.assertEqual(run_cli(["export", "--repo", str(self.repo)])[0], 8)
        code, out, err = run_cli(["export", "--repo", str(self.repo), "--accept-database"])
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out[0].startswith("audit-export: written (events "))
        code, out, _ = run_cli(["export", "--repo", str(self.repo)])
        self.assertEqual(code, 0)
        self.assertTrue(out[0].startswith("audit-export: already current"))

    def test_export_repairs_a_missed_export_on_demand(self):
        self.flow()
        self.unexported("late")
        self.assertEqual(self.status().state, audit_export.BEHIND)
        code, out, _ = run_cli(["export", "--repo", str(self.repo)])
        self.assertEqual(code, 0)
        self.assertTrue(out[0].startswith("audit-export: written"))
        self.assertEqual(self.status().state, audit_export.CURRENT)

    def test_export_with_no_store_is_exit_9(self):
        self.assertEqual(run_cli(["export", "--repo", str(self.repo)])[0], 9)

    def test_an_empty_store_exports_an_empty_chain(self):
        store.init_store(self.proj)
        code, out, _ = run_cli(["export", "--repo", str(self.repo)])
        self.assertEqual(code, 0)
        self.assertEqual(out[1:], ["tip events 0 " + GENESIS, "tip queue 0 " + GENESIS])

    def test_output_never_carries_a_record_a_path_or_a_message(self):
        self.flow()
        outputs = 0
        for argv in (["verify", "--repo", str(self.repo), "--database"], ["export", "--repo", str(self.repo)]):
            _, out, err = run_cli(argv)
            outputs += len(out)
            text = "\n".join(out) + err
            self.assertNotIn(str(self.repo), text)
            self.assertNotIn("Task record", text)
            self.assertTrue(text.isascii())
        self.assertGreater(outputs, 4)

    def test_the_module_can_be_run_as_a_script_with_the_fixed_exit_code(self):
        self.flow()
        proc = subprocess.run([sys.executable, "-m", "shit.audit", "verify", "--repo", str(self.repo)],
                              capture_output=True, text=True, timeout=120, check=False, cwd=ROOT)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertTrue(proc.stdout.startswith("audit-export: ok"))


if __name__ == "__main__":
    unittest.main()
