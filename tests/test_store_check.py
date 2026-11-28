"""`shit check` reports the queue state read-only from the SQLite store (issue 63, C6).

Real databases in temporary repositories. Every scenario runs `check.run_check` and proves it wrote nothing: the rows
of every table, the export's bytes and modification time, and the files under `.shit/` are the same afterwards.
"""

import os
import sqlite3
import unittest

from test_audit_export import Flow, code_of

from shit import audit_export, check, errors, store, store_check, store_queue


def lines_with(lines, prefix):
    return [line for line in lines if line.startswith(prefix)]


class StoreCheckCase(Flow):
    def rows(self):
        conn = sqlite3.connect(self.db_path(), isolation_level=None)  # plain: also works on a refused database
        try:
            out = {}
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE "
                                        "'sqlite_%' ORDER BY name").fetchall():
                out[name] = conn.execute("SELECT * FROM " + name + " ORDER BY 1, 2").fetchall()
            return out
        finally:
            conn.close()

    def files(self):
        shit = self.repo / ".shit"
        listing = sorted(p.name for p in shit.iterdir() if not p.name.startswith(store.DB_NAME + "-"))
        export = self.export_path
        return (listing, export.read_bytes() if export.exists() else None,
                export.stat().st_mtime_ns if export.exists() else None)

    def run_checked(self):
        """(code, lines) of the check, with the proof that it changed nothing."""
        before = (self.rows(), self.files())
        code, lines = check.run_check(self.repo)
        self.assertEqual((self.rows(), self.files()), before)
        return code, lines

    def raw_write(self, sql, *args):
        raw = sqlite3.connect(self.db_path(), isolation_level=None)
        try:
            raw.execute(sql, args)
        finally:
            raw.close()


class ReportTests(StoreCheckCase):
    def test_a_project_without_a_store_has_no_store_section(self):
        code, lines = check.run_check(self.repo)
        self.assertEqual((code, lines), (0, ["check: ok (0 tasks)"]))

    def test_a_consistent_store_reports_its_queue_and_its_export(self):
        self.flow()
        code, lines = self.run_checked()
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines_with(lines, "store: queue"), ["store: queue 2 tasks (ready 1, review 1)"])
        self.assertEqual(lines_with(lines, "store: pending"), ["store: pending 1, leases 0 live, 0 past expiry"])
        self.assertEqual(len(lines_with(lines, "store: audit export verified (events 4, queue 9)")), 1)
        self.assertEqual(lines[-1], "check: ok (2 tasks)")
        self.assertEqual(lines_with(lines, "warning"), [])

    def test_leases_and_expiries_are_counted_from_the_clock_given(self):
        self.task("a")
        self.task("b")
        self.claim("a")
        lease = self.claim("b")
        store_queue.start(self.proj, self.present(lease), now=self.tick())
        found, lines, warnings = store_check.diagnose(self.proj, now=self.tick())
        self.assertEqual((found, warnings), ([], []))
        self.assertEqual(lines_with(lines, "store: pending"), ["store: pending 0, leases 2 live, 0 past expiry"])
        found, lines, _ = store_check.diagnose(self.proj, now="2027-01-01T00:00:00Z")
        self.assertEqual(found, [])
        self.assertEqual(lines_with(lines, "store: pending"), ["store: pending 0, leases 2 live, 2 past expiry"])
        self.assertEqual(lines_with(lines, "store: queue"), ["store: queue 2 tasks (dispatched 1, running 1)"])

    def test_waiting_and_ready_tasks_are_pending(self):
        self.task("a", enqueue=False)
        self.task("b")
        store_queue.enqueue(self.proj, "a", "human", now=self.tick())
        store_queue.add_edge(self.proj, "a", "b", "review", "human", now=self.tick())
        _, lines, _ = store_check.diagnose(self.proj, now=self.tick())
        self.assertEqual(lines_with(lines, "store: pending"), ["store: pending 2, leases 0 live, 0 past expiry"])

    def test_an_empty_store_is_ok_and_says_the_export_is_not_written(self):
        store.init_store(self.proj)
        code, lines = self.run_checked()
        self.assertEqual(code, 0)
        self.assertIn("store: audit export not written yet (no entries)", lines)
        self.assertIn("store: queue 0 tasks", lines)

    def test_the_task_option_does_not_hide_the_store(self):
        self.flow()
        code, lines = check.run_check(self.repo, task=None)
        self.assertEqual(code, 0)
        self.assertEqual(len(lines_with(lines, "store: queue")), 1)


class FailureTests(StoreCheckCase):
    def setUp(self):
        super().setUp()
        self.task("a")
        self.saved = self.save_db("old.sqlite3")
        self.flow_rest()

    def flow_rest(self):
        self.task("b")
        lease = self.claim("b")
        store_queue.start(self.proj, self.present(lease), now=self.tick())

    def test_a_restored_database_fails_the_check_with_exit_8(self):
        self.restore_db(self.saved)
        code, lines = self.run_checked()
        self.assertEqual(code, errors.FAILED_CLOSED)
        self.assertIn("store: database is older than its audit export (restored or truncated)", lines)
        self.assertEqual(lines[-1], "check: failed (2 tasks)")

    def test_a_lost_export_fails_the_check(self):
        self.export_path.unlink()
        code, lines = self.run_checked()
        self.assertEqual(code, errors.FAILED_CLOSED)
        self.assertIn("store: audit export is missing but the database has entries", lines)

    def test_a_damaged_export_fails_the_check_even_when_its_footer_matches(self):
        data = bytearray(self.export_bytes())
        data[60] ^= 1  # inside the first record; the footer is untouched
        self.export_path.write_bytes(bytes(data))
        os.chmod(self.export_path, 0o600)
        self.assertEqual(self.status().state, audit_export.CURRENT)  # the open-time comparison reads only the footer
        code, lines = self.run_checked()
        self.assertEqual(code, errors.FAILED_CLOSED)
        self.assertIn("store: audit export does not verify", lines)

    def test_a_loose_export_mode_is_confinement(self):
        os.chmod(self.export_path, 0o644)
        code, lines = self.run_checked()
        self.assertEqual(code, errors.CONFINEMENT)
        self.assertIn("store: cannot be opened or its audit export cannot be read", lines)

    def test_a_broken_invariant_is_named(self):
        self.raw_write("DELETE FROM leases WHERE task_id = ?", "b")
        code, lines = self.run_checked()
        self.assertEqual(code, errors.FAILED_CLOSED)
        self.assertIn("store: invariant claim_without_lease does not hold", lines)

    def test_a_task_without_its_body_record_is_named(self):
        self.raw_write("DELETE FROM records WHERE kind = 'task' AND record_id = ?", "a")
        code, lines = self.run_checked()
        self.assertEqual(code, errors.FAILED_CLOSED)
        self.assertIn("store: invariant task_without_body_record does not hold", lines)

    def test_a_database_that_cannot_be_opened_fails_with_the_code_of_the_refusal(self):
        self.raw_write("DROP TRIGGER tasks_no_delete")
        code, lines = self.run_checked()
        self.assertEqual(code, errors.FAILED_CLOSED)
        self.assertIn("store: cannot be opened or its audit export cannot be read", lines)

    def test_a_loose_database_mode_is_confinement(self):
        os.chmod(self.db_path(), 0o644)
        code, lines = self.run_checked()
        self.assertEqual(code, errors.CONFINEMENT)
        self.assertEqual(lines_with(lines, "store:")[0], "store: cannot be opened or its audit export cannot be read")

    def test_no_failure_text_carries_a_path_or_a_record(self):
        self.restore_db(self.saved)
        _, lines = self.run_checked()
        text = "\n".join(lines)
        self.assertNotIn(str(self.repo), text)
        self.assertTrue(text.isascii())


class PriorityTests(unittest.TestCase):
    def test_a_busy_store_is_the_code_of_the_check_when_nothing_worse_is_found(self):
        failures = check._Failures()
        failures.add(errors.BUSY, "store", "x")
        self.assertEqual(failures.code(), errors.BUSY)
        failures.add(errors.PRECONDITION, "store", "y")
        self.assertEqual(failures.code(), errors.PRECONDITION)
        failures.add(errors.FAILED_CLOSED, "store", "z")
        self.assertEqual(failures.code(), errors.FAILED_CLOSED)
        self.assertIsNone(code_of(lambda: None))


if __name__ == "__main__":
    unittest.main()
