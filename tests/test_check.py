"""`shit check` tests (issue #13): a negative fixture for each missing element, and hostile records."""

import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path

from _helpers import BRIEF, CLEANUP, REPORT, RepoCase, run_main

from shit import check, store, store_check
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
        ]
        for label, rel, state, want in cases:
            with self.subTest(label=label):
                self.full_task(state=state)
                (self.store / rel).unlink()
                code, out, _ = self.check()
                self.reset_store()
                self.assertEqual(code, want)
                self.assertIn(label, out)

    def test_approved_task_needs_a_store_backed_human_disposition(self):
        self.new_task(state="review")
        from shit import task as task_mod
        task_mod.transition(self.proj, "t1", "approved", "human", "step", now="2026-11-26T16:00:00Z")
        code, out, _ = self.check()
        self.assertEqual(code, 9)
        self.assertIn("human disposition", out)

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

    def test_task_selection(self):
        self.full_task("t1")
        self.new_task("t2", state="running")  # t2 has no brief
        self.assertEqual(self.check("--task", "t1")[0], 0)
        self.assertEqual(self.check("--task", "t2")[0], 9)
        self.assertEqual(self.check("--task", "nope")[0], 9)
        self.assertEqual(self.check("--task", "../x")[0], 2)

    def test_tampered_event_log_fails(self):
        self.full_task()
        raw = sqlite3.connect(self.store / store.DB_NAME, isolation_level=None)
        self.addCleanup(raw.close)
        raw.execute("DROP TRIGGER events_no_update")
        raw.execute("UPDATE events SET message = 'Task record altered.' WHERE seq = 1")
        self.assertEqual(self.check()[0], 8)

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


CONFIG = """schema_version = 1

[project]
name = "example-project"

[tracker]
repository = "example-owner/example-repo"

[roles]
names = ["implementer", "reviewer", "researcher", "planner"]

[review]
destination = "reviewer role, report to the lead"

[storage]
root = ".shit"

[log]
file = ".shit/events.jsonl"

[local_model]
context_tokens = 32768
"""


class SizeCheck(RepoCase):
    """Issue #45: configurable instruction-file size and role-list size limits."""

    def check(self, *extra):
        return run_main(check.main, ["--repo", str(self.repo), *extra])

    def config(self, limits=""):
        (self.repo / "config.toml").write_text(CONFIG + limits, encoding="utf-8")

    def instruction(self, name, size):
        path = self.repo / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("x" * size, encoding="utf-8")

    def test_no_config_means_no_size_check(self):
        self.instruction("CLAUDE.md", 100000)
        self.assertEqual(self.check()[0], 0)

    def test_config_without_limits_passes(self):
        self.config()
        self.instruction("CLAUDE.md", 100000)
        self.assertEqual(self.check()[0], 0)

    def test_within_limits_passes_silently(self):
        self.config("\n[limits]\ninstruction_max_bytes = 100\nlisted_roles_max = 4\non_exceed = \"fail\"\n")
        self.instruction("CLAUDE.md", 100)
        code, out, _ = self.check()
        self.assertEqual(code, 0)
        self.assertNotIn("size", out)

    def test_oversize_instruction_warns_by_default_and_exits_zero(self):
        self.config("\n[limits]\ninstruction_max_bytes = 100\n")
        self.instruction("AGENTS.md", 101)
        code, out, _ = self.check()
        self.assertEqual(code, 0)
        self.assertIn("warning: AGENTS.md: instruction file over size limit", out)
        self.assertIn("check: ok", out)

    def test_oversize_instruction_fails_in_fail_mode_without_printing_content(self):
        self.config("\n[limits]\ninstruction_max_bytes = 100\non_exceed = \"fail\"\n")
        (self.repo / "CLAUDE.md").write_text(SENTINEL * 20, encoding="utf-8")
        code, out, err = self.check()
        self.assertEqual(code, 7)
        self.assertIn("CLAUDE.md: instruction file over size limit", out)
        self.assertNotIn(SENTINEL, out + err)
        self.assertIn("check: failed", out)

    def test_nested_instruction_file_is_measured(self):
        self.config("\n[limits]\ninstruction_max_bytes = 10\non_exceed = \"fail\"\n")
        self.instruction(".github/copilot-instructions.md", 11)
        self.assertEqual(self.check()[0], 7)

    def test_too_many_listed_roles(self):
        self.config("\n[limits]\nlisted_roles_max = 3\non_exceed = \"fail\"\n")
        code, out, _ = self.check()
        self.assertEqual(code, 7)
        self.assertIn("roles: listed roles over limit", out)

    def test_too_many_listed_roles_warns_by_default(self):
        self.config("\n[limits]\nlisted_roles_max = 3\n")
        code, out, _ = self.check()
        self.assertEqual(code, 0)
        self.assertIn("warning: roles: listed roles over limit", out)

    def test_invalid_config_fails_check(self):
        self.config("\n[limits]\ninstruction_max_bytes = 0\n")
        code, out, _ = self.check()
        self.assertEqual(code, 3)
        self.assertIn("config: rejected", out)

    def test_symlinked_instruction_file_is_a_confinement_failure(self):
        self.config("\n[limits]\ninstruction_max_bytes = 100\n")
        (self.outside / "big.md").write_text("x" * 500, encoding="utf-8")
        os.symlink(self.outside / "big.md", self.repo / "CLAUDE.md")
        code, out, _ = self.check()
        self.assertEqual(code, 5)
        self.assertIn("CLAUDE.md: instruction file rejected", out)

    def test_root_replaced_during_check_is_a_confinement_failure(self):
        """Review of PR 49: one invocation must not combine records from one tree and limits from another."""
        self.config("\n[limits]\ninstruction_max_bytes = 1\non_exceed = \"fail\"\n")
        self.instruction("CLAUDE.md", 50)
        moved = self.base / "moved"
        real_statuses = store_check.task_statuses

        def swap(project):
            self.repo.rename(moved)
            self.repo.mkdir()
            (self.repo / "config.toml").write_text(
                CONFIG + "\n[limits]\ninstruction_max_bytes = 1\non_exceed = \"fail\"\n", encoding="utf-8")
            (self.repo / "CLAUDE.md").write_text("x" * 50, encoding="utf-8")
            return real_statuses(project)

        store_check.task_statuses = swap
        try:
            code, out, _ = self.check()
        finally:
            store_check.task_statuses = real_statuses
        self.assertEqual(code, 5)
        self.assertIn("repository: root changed during check", out)
        self.assertNotIn("instruction file over size limit", out)

    def test_limits_are_not_machine_specific_defaults(self):
        from shit import config as cfg
        parsed = cfg.parse(CONFIG)
        self.assertNotIn("instruction_max_bytes", parsed["limits"])
        self.assertEqual(parsed["limits"]["on_exceed"], "warn")


if __name__ == "__main__":
    unittest.main()
