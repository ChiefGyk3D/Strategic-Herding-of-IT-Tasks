"""The store's concurrency and crash harness (issue 65, C8): what it runs, and proof that it bites.

Defaults are small (well under a minute in all); `SHIT_HARNESS_LONG=1` turns on the long mode and the other
`SHIT_HARNESS_*` variables tune it. See docs/store.md, "Concurrency and crash harness". A failure prints its seed.

The pieces live in `tests/store_harness.py` (drivers and checks), `tests/store_worker.py` (one worker process) and
`tests/store_model.py` (the pure lifecycle model). The mutation checks run the same drivers against a copy of the
package with one deliberate breakage in `shit/store.py` and require them to notice it.
"""

import sqlite3
import tempfile
import unittest
from unittest import mock

import store_harness as harness
import store_model
import store_worker

from shit import store


class Quiet(unittest.TestCase):
    settings = harness.Settings.from_env()

    def fail_with(self, problems):
        self.fail("\n".join(str(p) for p in problems[:8]) + "\n" + self.settings.failure_note())


class ConcurrentWorkerTests(Quiet):
    def test_workers_doing_random_operations_leave_a_sound_database(self):
        with harness.Arena(self.settings) as arena:
            report = harness.run_workers(arena, seed=self.settings.seed, check_every=1)
            problems = report.problems + harness.check_invariants(arena.conn, report.claims)
            if problems:
                self.fail_with(problems)
            # The run did something: several kinds of operation were accepted, claims were won, none was busy.
            self.assertGreaterEqual(len(report.ok_kinds()), 5, report.counts)
            self.assertGreaterEqual(len(report.claims), 3, report.counts)
            self.assertEqual(report.total("code13"), 0, report.counts)
            self.assertEqual(report.exit_codes, [0] * self.settings.workers)


class KillTests(Quiet):
    def test_workers_killed_at_random_times_leave_a_sound_database(self):
        problems, summary = harness.kill_rounds(self.settings)
        if problems:
            self.fail_with(problems)
        self.assertEqual(summary["rounds"], self.settings.rounds, summary)
        self.assertGreaterEqual(summary["killed"], self.settings.rounds // 2, summary)
        self.assertGreater(summary["selfkilled"], 0, summary)
        self.assertGreater(summary["ops_ok"], 20, summary)
        self.assertGreater(summary["claims"], 0, summary)

    def test_an_operation_killed_at_every_fault_point_leaves_a_sound_database(self):
        problems, stages = harness.stage_sweep(self.settings)
        if problems:
            self.fail_with(problems)
        self.assertEqual(sorted(stages), ["claim", "renew", "submit"])
        self.assertEqual(stages["claim"], ["checked", "entry", "lease-row"])  # the entry first, the lease row after
        self.assertIn("lease-dropped", stages["submit"])


class LifecycleModelTests(Quiet):
    def test_generated_sequences_agree_with_the_model_and_the_replay(self):
        seen = {}
        for seed in range(1, self.settings.model_seeds + 1):
            problems, counts = store_model.run_sequence(harness.ROOT, seed, self.settings.model_length)
            if problems:
                self.fail("\n".join(problems[:5]) + "\n(model seeds are 1..SHIT_HARNESS_MODEL_SEEDS)")
            for key, n in counts.items():
                seen[key] = seen.get(key, 0) + n
        # Collected loosely, asserted strictly: expiry, reclaim, refusals and the apply step were all exercised.
        for key in ("ok:claim", "ok:start", "ok:renew", "ok:reclaim", "ok:reopen", "ok:hold", "ok:fail", "refused",
                    "expired"):
            self.assertGreater(seen.get(key, 0), 0, (key, seen))


class MutationTests(Quiet):
    """A deliberate breakage of a copy of the store must be noticed by the part of the harness named for it."""

    def test_every_mutation_is_detected(self):
        with tempfile.TemporaryDirectory(prefix="shit-mutation-") as directory:
            self.assertGreaterEqual(len(harness.MUTATIONS), 5)
            for mutation in harness.MUTATIONS:
                with self.subTest(mutation=mutation.name):
                    self.assertTrue(mutation.expected, mutation.name)
                    found = harness.detect(self.settings, mutation, directory)
                    self.assertTrue(found, f"{mutation.name} was not detected by {mutation.detector}")
                    self.assertTrue(
                        any(mutation.expected in problem for problem in found),
                        f"{mutation.name} was not detected by its named guard: {found[:3]}",
                    )

    def test_default_mutation_seed_is_fixed_and_random_seed_is_long_run_only(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            first = harness.Settings.from_env()
            second = harness.Settings.from_env()
        self.assertEqual(first.seed, second.seed)
        with mock.patch.dict("os.environ", {"SHIT_HARNESS_LONG": "1"}, clear=True), \
                mock.patch.object(harness.secrets, "randbelow", side_effect=(11, 12)):
            self.assertEqual((harness.Settings.from_env().seed, harness.Settings.from_env().seed), (11, 12))

    def test_an_unmutated_copy_is_not_flagged(self):
        """The negative control: the same detectors say nothing about a copy with no breakage."""
        with tempfile.TemporaryDirectory(prefix="shit-mutation-") as directory:
            for detector in ("barrier", "processes", "sweep", "model"):
                control = harness.Mutation("control-" + detector, "def _fault(stage):", "def _fault(stage):", detector)
                with self.subTest(detector=detector):
                    self.assertEqual(harness.detect(self.settings, control, directory), [])

    def test_a_mutation_that_matches_nothing_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="shit-mutation-") as directory:
            for old in ("this text is not in the store", "conn.execute("):  # none, and many
                with self.assertRaises(AssertionError):
                    harness.mutated_root(directory, harness.Mutation("bad", old, "x", "processes"))


class InvariantCheckTests(Quiet):
    """The invariant check itself bites: it reports each kind of damage it claims to find."""

    def claimed(self, arena):
        request = {"task_id": "t0", "step": "draft", "worker": "w1", "worker_kind": "ai"}
        return store.queue_claim(arena.conn, request, arena.now(), config=store_worker.CONFIG)

    def raw(self, arena):
        conn = sqlite3.connect(arena.repo / ".shit" / store.DB_NAME, isolation_level=None)
        self.addCleanup(conn.close)
        return conn

    def test_a_sound_database_reports_nothing(self):
        with harness.Arena(self.settings) as arena:
            lease = self.claimed(arena)
            claims = [{"task": "t0", "lease_id": lease["lease_id"], "generation": lease["generation"]}]
            self.assertEqual(harness.check_invariants(arena.conn, claims), [])

    def test_a_lost_lease_row_is_reported(self):
        with harness.Arena(self.settings) as arena:
            self.claimed(arena)
            self.raw(arena).execute("DELETE FROM leases WHERE task_id = 't0'")
            found = harness.check_invariants(arena.conn)
            self.assertIn("verify: claim_without_lease", found)
            self.assertIn("lease row of t0 differs from the log", found)

    def test_a_lease_row_with_no_live_claim_is_reported(self):
        with harness.Arena(self.settings) as arena:
            lease = self.claimed(arena)
            presented = {"task_id": "t0", "lease_id": lease["lease_id"], "generation": lease["generation"]}
            store.queue_release(arena.conn, presented, arena.now(), config=store_worker.CONFIG)
            raw = self.raw(arena)
            with self.assertRaises(sqlite3.Error):  # the schema refuses this row; a hostile process could drop that guard
                self.insert_lease(raw, lease)
            for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'leases'").fetchall():
                raw.execute('DROP TRIGGER "' + row[0] + '"')
            self.insert_lease(raw, lease)
            self.assertIn("verify: lease_without_live_claim", harness.check_invariants(arena.conn))

    @staticmethod
    def insert_lease(raw, lease):
        raw.execute(
            "INSERT INTO leases (task_id, lease_id, generation, step, worker, worker_kind, claimed_at, "
            "expires_at, renewals) VALUES ('t0', ?, 1, 'draft', 'w1', 'ai', ?, ?, 0)",
            (lease["lease_id"], lease["claimed_at"], lease["expires_at"]))

    def test_two_workers_told_they_won_the_same_claim_are_reported(self):
        with harness.Arena(self.settings) as arena:
            lease = self.claimed(arena)
            first = {"task": "t0", "lease_id": lease["lease_id"], "generation": lease["generation"]}
            second = {**first, "lease_id": "f" * 32}
            found = harness.check_invariants(arena.conn, [first, second])
            self.assertTrue(any("two winners" in line for line in found), found)
            self.assertTrue(any("in the log 0 times" in line for line in found), found)

    def test_a_broken_hash_chain_is_reported(self):
        with harness.Arena(self.settings) as arena:
            self.claimed(arena)
            raw = self.raw(arena)
            with self.assertRaises(sqlite3.Error):  # the schema refuses a hand edit ...
                raw.execute("UPDATE queue_entries SET prev_hash = ? WHERE seq = 2", ("0" * 64,))
            names = [row[0] for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'queue_entries'")]
            for name in names:  # ... so the test removes the guards first, as a hostile process of the user could
                raw.execute('DROP TRIGGER "' + name + '"')
            raw.execute("UPDATE queue_entries SET prev_hash = ? WHERE seq = 2", ("0" * 64,))
            found = harness.check_invariants(arena.conn)
            self.assertIn("queue hash chain broken at entry 2", found)
            self.assertIn("verify: queue_chain_broken", found)

    def test_a_worker_that_cannot_start_is_reported(self):
        with harness.Arena(self.settings) as arena:
            report = harness.run_workers(arena, seed=1, workers=1, ops=5, seconds=10.0, root=arena.dir / "nowhere")
            self.assertTrue(any("exited" in p for p in report.problems), report.problems)


if __name__ == "__main__":
    unittest.main()
