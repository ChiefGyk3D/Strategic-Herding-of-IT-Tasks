"""`shit check` tests (issue #13): a negative fixture for each missing element, and hostile records."""

import json
import os
import shutil
import unittest
from pathlib import Path

from _helpers import BRIEF, CLEANUP, DECISION, REPORT, RepoCase, run_main

from shit import check
from shit.project import Project

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SENTINEL = "zq-record-content-7741"


class Check(RepoCase):
    def reset_store(self):
        shutil.rmtree(self.store)
        self.proj.close()
        self.proj = Project.init(self.repo)

    def check(self, *extra):
        return run_main(check.main, ["--repo", str(self.repo), *extra])

    def test_empty_store_passes(self):
        self.assertEqual(self.check()[0], 0)

    def test_complete_closed_task_passes_and_check_is_read_only(self):
        self.full_task()
        before = self.snapshot()
        code, out, err = self.check()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("1 task", out)
        self.assertEqual(self.snapshot(), before)

    def test_open_task_needs_nothing_else(self):
        self.new_task(state="open")
        self.assertEqual(self.check()[0], 0)

    def test_each_missing_element_fails(self):
        cases = [
            ("brief", "briefs/t1.md", "running", 9),
            ("review header", "reports/t1.md", "review", 9),
            ("cleanup report", "cleanup/t1.md", "review", 9),
            ("human disposition", "decisions/t1.json", "approved", 9),
        ]
        for label, rel, state, want in cases:
            with self.subTest(label=label):
                self.full_task(state=state)
                (self.store / rel).unlink()
                code, out, _ = self.check()
                self.reset_store()
                self.assertEqual(code, want)
                self.assertIn(label, out)

    def test_brief_without_issue_fails(self):
        self.full_task()
        self.put("briefs/t1.md", BRIEF.replace("issue: ChiefGyk3D/example#13\n", ""))
        code, out, _ = self.check()
        self.assertEqual(code, 3)
        self.assertIn("issue", out)

    def test_brief_with_placeholder_issue_fails(self):
        self.full_task()
        self.put("briefs/t1.md", BRIEF.replace("ChiefGyk3D/example#13", "<owner/repo#N>"))
        self.assertEqual(self.check()[0], 3)

    def test_malformed_review_header_fails(self):
        self.full_task()
        self.put("reports/t1.md", REPORT.replace("GATE: python3 -m unittest  exit=0\n", ""))
        self.assertEqual(self.check()[0], 3)

    def test_cleanup_report_must_name_removed_and_kept(self):
        self.full_task()
        self.put("cleanup/t1.md", "removed: scratch\n")
        self.assertEqual(self.check()[0], 3)

    def test_disposition_must_be_human_and_decided(self):
        for change in ('"actor": "human"', '"status": "accepted"'):
            with self.subTest(change=change):
                self.full_task()
                bad = change.replace("human", "model").replace("accepted", "proposed")
                self.put("decisions/t1.json", DECISION.replace(change, bad))
                code = self.check()[0]
                self.reset_store()
                self.assertEqual(code, 3)

    def test_disposition_for_another_task_is_not_accepted(self):
        self.full_task()
        self.put("decisions/t1.json", DECISION.replace('"task_id": "t1"', '"task_id": "other"'))
        self.assertEqual(self.check()[0], 3)

    def test_disposition_with_unknown_field_or_duplicate_key_is_rejected(self):
        self.full_task()
        self.put("decisions/t1.json", DECISION.replace('"id": "d1",', '"id": "d1", "extra": 1,'))
        self.assertEqual(self.check()[0], 3)
        self.put("decisions/t1.json", DECISION.replace('"id": "d1",', '"id": "d1", "id": "d2",'))
        self.assertEqual(self.check()[0], 3)

    def test_task_selection(self):
        self.full_task("t1")
        self.new_task("t2", state="running")  # t2 has no brief
        self.assertEqual(self.check("--task", "t1")[0], 0)
        self.assertEqual(self.check("--task", "t2")[0], 9)
        self.assertEqual(self.check("--task", "nope")[0], 9)
        self.assertEqual(self.check("--task", "../x")[0], 2)

    def test_invalid_task_records(self):
        for fixture, want in (("unknown-field.json", 3), ("duplicate-key.json", 3), ("unknown-version.json", 3),
                              ("bad-transition.json", 4)):
            with self.subTest(fixture=fixture):
                shutil.copy(FIXTURES / "invalid" / fixture, self.store / "tasks" / "bad.json")
                code, _out, _err = self.check()
                self.assertEqual(code, want)
                (self.store / "tasks" / "bad.json").unlink()

    def test_hostile_task_files(self):
        tasks = self.store / "tasks"
        (tasks / "weird name.json").write_text("{}")
        code, out, err = self.check()
        self.assertEqual(code, 3)
        self.assertNotIn("weird", out + err)
        (tasks / "weird name.json").unlink()
        (self.outside / "x.json").write_text("{}")
        (tasks / "linked.json").symlink_to(self.outside / "x.json")
        self.assertEqual(self.check()[0], 5)
        (tasks / "linked.json").unlink()
        os.mkfifo(tasks / "pipe.json")
        self.assertEqual(self.check()[0], 5)
        (tasks / "pipe.json").unlink()
        (tasks / "big.json").write_bytes(b" " * 70000)
        self.assertEqual(self.check()[0], 7)

    def test_tampered_event_log_fails(self):
        self.full_task()
        log = self.store / "events.jsonl"
        log.write_bytes(log.read_bytes().replace(b"task_created", b"task_created"[::-1], 1))
        self.assertEqual(self.check()[0], 3)

    def test_unfinished_commit_is_reported(self):
        self.new_task()
        (self.store / "pending.json").write_text("{}")
        self.assertEqual(self.check()[0], 3)

    def test_record_content_never_reaches_output(self):
        self.full_task()
        task = json.loads((self.store / "tasks" / "t1.json").read_text())
        task["summary"] = SENTINEL
        task["bogus"] = SENTINEL
        (self.store / "tasks" / "t1.json").write_text(json.dumps(task))
        self.put("briefs/t1.md", BRIEF.replace("Do the thing.", SENTINEL) + "typo_field: " + SENTINEL + "\n")
        code, out, err = self.check()
        self.assertEqual(code, 3)
        self.assertNotIn(SENTINEL, out + err)

    def test_missing_store_is_precondition(self):
        shutil.rmtree(self.store)
        self.assertEqual(self.check()[0], 9)

    def test_symlinked_store_is_confinement(self):
        shutil.rmtree(self.store)
        (self.store).symlink_to(self.outside)
        self.assertEqual(self.check()[0], 5)

    def test_symlinked_brief_is_confinement(self):
        self.full_task()
        (self.store / "briefs" / "t1.md").unlink()
        (self.outside / "b.md").write_text(BRIEF)
        (self.store / "briefs" / "t1.md").symlink_to(self.outside / "b.md")
        self.assertEqual(self.check()[0], 5)

    def test_cleanup_report_over_limit(self):
        self.full_task()
        self.put("cleanup/t1.md", CLEANUP + "x" * (check.MAX_TEXT_BYTES + 1))
        self.assertEqual(self.check()[0], 7)


if __name__ == "__main__":
    unittest.main()
