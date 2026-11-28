"""Yardstick report from SHIT's own task log (issue #44)."""

import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from _bootstrap import make_git_dir
from test_config import GOOD

from shit import commands, config, store, yardstick
from shit import task as task_mod
from shit.errors import ContractError, ShitError
from shit.project import Project

T = [f"2026-11-26T15:3{i}:00Z" for i in range(10)]


class Yardstick(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        self.repo.mkdir()
        make_git_dir(self.repo / ".git")
        self.p = Project.init(self.repo)
        self.addCleanup(self.p.close)

    def seed(self):
        p = self.p
        commands.new_task(p, task_id="t1", title="T", summary="S", owner="human", actor="human", now=T[0])
        task_mod.transition(p, "t1", "running", "wrapper", "go", now=T[1])
        commands.add_report(p, task_id="t1", report_id="r1", outcome="success", summary="s", evidence_ids=[],
                            actor="wrapper", now=T[2])
        task_mod.transition(p, "t1", "review", "wrapper", "done", now=T[3])
        commands.add_review(p, task_id="t1", review_id="v1", report_id="r1", verdict="pass", summary="s",
                            actor="human", now=T[4])
        commands.new_task(p, task_id="t2", title="T", summary="S", owner="human", actor="human", now=T[5])

    def test_baseline_counts_are_measured_and_labeled(self):
        self.seed()
        out = yardstick.compute(self.p)
        self.assertEqual(out["tasks"]["total"], 2)
        self.assertEqual(out["tasks"]["by_state"], {"open": 1, "review": 1})
        self.assertEqual(out["events"]["task_created"], 2)
        self.assertEqual(out["events"]["state_transition"], 2)
        self.assertEqual(out["reports_by_outcome"], {"label": "measured", "success": 1})
        self.assertEqual(out["reviews_by_verdict"], {"label": "measured", "pass": 1})
        self.assertEqual(out["tasks"]["label"], "measured")
        self.assertEqual(out["events"]["label"], "measured")
        self.assertEqual(out["reports_by_outcome"]["label"], "measured")
        self.assertEqual(out["reviews_by_verdict"]["label"], "measured")

    def test_points_are_estimates_and_weights_are_config(self):
        self.seed()
        default = yardstick.compute(self.p)["effort_points"]
        self.assertEqual((default["value"], default["label"]), (6, "estimate"))
        heavy = dict(yardstick.DEFAULT_WEIGHTS, review_recorded=10)
        self.assertEqual(yardstick.compute(self.p, heavy)["effort_points"]["value"], 15)

    def test_no_tokens_unless_supplied_and_no_other_project_figures(self):
        self.seed()
        out = yardstick.compute(self.p)
        self.assertEqual(out["tokens"], {"value": None, "label": "not supplied"})
        self.assertEqual(out["basis"], "this project's own task log")

    def test_record_event_without_record_id_is_refused_not_a_crash(self):
        self.seed()
        raw = sqlite3.connect(self.repo / ".shit" / store.DB_NAME, isolation_level=None)
        self.addCleanup(raw.close)
        raw.execute("DROP TRIGGER events_no_update")
        raw.execute("UPDATE events SET record_id = NULL WHERE event = 'report_recorded'")
        with self.assertRaises(ShitError) as ctx:
            yardstick.compute(self.p)
        self.assertEqual(ctx.exception.code, 8)

    def test_loading_a_record_does_not_rescan_the_whole_log(self):
        self.seed()
        seen = []
        real = store.list_events

        def spy(conn):
            seen.append(1)
            return real(conn)

        store.list_events = spy
        self.addCleanup(setattr, store, "list_events", real)
        yardstick.compute(self.p)
        self.assertEqual(seen, [1])

    def test_empty_store(self):
        store.init_store(self.p)
        out = yardstick.compute(self.p)
        self.assertEqual((out["tasks"]["total"], out["effort_points"]["value"]), (0, 0))

    def test_per_task_rows_are_capped(self):
        for i in range(yardstick.MAX_ROWS + 3):
            commands.new_task(self.p, task_id=f"k{i}", title="T", summary="S", owner="human", actor="human",
                              now=T[0])
        out = yardstick.compute(self.p)
        self.assertEqual(len(out["per_task"]), yardstick.MAX_ROWS)
        self.assertIs(out["per_task_truncated"], True)
        self.assertEqual(out["tasks"]["total"], yardstick.MAX_ROWS + 3)

    def test_tampered_log_is_refused(self):
        self.seed()
        raw = sqlite3.connect(self.repo / ".shit" / store.DB_NAME, isolation_level=None)
        self.addCleanup(raw.close)
        raw.execute("DROP TRIGGER events_no_update")
        raw.execute("UPDATE events SET message = 'Task record altered.' WHERE seq = 1")
        with self.assertRaises(ShitError) as ctx:
            yardstick.compute(self.p)
        self.assertEqual(ctx.exception.code, 8)

    def test_cli_prints_json(self):
        self.seed()
        self.p.close()
        out, err = io.StringIO(), io.StringIO()
        code = yardstick.main([], cwd=str(self.repo), stdout=out, stderr=err)
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertEqual(json.loads(out.getvalue())["tasks"]["total"], 2)

    def test_cli_reads_weights_from_config(self):
        self.seed()
        self.p.close()
        cfg = self.repo / "config.toml"
        cfg.write_text(GOOD + "\n[yardstick]\nreview_recorded = 10\n")
        out = io.StringIO()
        code = yardstick.main(["--config", "config.toml"], cwd=str(self.repo), stdout=out, stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["effort_points"]["value"], 15)

    def test_cli_bad_args_fixed_error(self):
        err = io.StringIO()
        code = yardstick.main(["--nope"], cwd=str(self.repo), stdout=io.StringIO(), stderr=err)
        self.assertEqual((code, err.getvalue()), (2, "shit: usage error\n"))


class YardstickConfig(unittest.TestCase):
    def test_defaults_to_one(self):
        w = config.parse(GOOD)["yardstick"]
        self.assertEqual(w, yardstick.DEFAULT_WEIGHTS)
        self.assertTrue(all(v == 1 for v in w.values()))

    def test_override_accepted(self):
        self.assertEqual(config.parse(GOOD + "\n[yardstick]\ncommand_failed = 5\n")["yardstick"]["command_failed"], 5)

    def test_bad_values_refused(self):
        for bad in ("-1", "1001", "true", "1.5", '"x"'):
            with self.subTest(bad=bad), self.assertRaises(ContractError) as ctx:
                config.parse(GOOD + f"\n[yardstick]\ncommand_failed = {bad}\n")
            self.assertEqual(ctx.exception.code, 3)

    def test_unknown_weight_refused(self):
        with self.assertRaises(ContractError):
            config.parse(GOOD + "\n[yardstick]\nsurprise = 1\n")


if __name__ == "__main__":
    unittest.main()
