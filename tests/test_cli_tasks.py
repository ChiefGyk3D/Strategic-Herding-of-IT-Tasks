"""Command line for the task commands (issue #11)."""

import io
import json
import tempfile
import unittest
from pathlib import Path

from _bootstrap import make_git_dir

from shit import artifacts, cli_tasks, commands


class Cli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        self.repo.mkdir()
        make_git_dir(self.repo / ".git")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli_tasks.main(list(argv), cwd=str(self.repo), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual((code, err), (0, ""), argv)
        return json.loads(out)

    def new_task(self):
        self.run_cli("init")
        self.ok("task", "create", "t1", "--title", "T", "--summary", "S", "--owner", "human", "--actor", "human")

    def test_whole_flow_by_hand(self):
        self.new_task()
        self.ok("task", "move", "t1", "running", "--actor", "wrapper", "--reason", "start")
        (self.repo / "n.txt").write_text("x")
        self.ok("evidence", "add", "t1", "ev1", "--path", "n.txt", "--description", "d", "--actor", "wrapper")
        self.ok("report", "add", "t1", "r1", "--outcome", "success", "--summary", "s", "--evidence", "ev1",
                "--actor", "wrapper")
        self.ok("task", "move", "t1", "review", "--actor", "wrapper", "--reason", "done")
        self.ok("review", "add", "t1", "v1", "--report", "r1", "--verdict", "pass", "--summary", "s",
                "--actor", "human")
        self.assertEqual(self.run_cli("close", "t1", "--actor", "human", "--reason", "x")[0], 4)
        self.ok("decision", "add", "t1", "d1", "--status", "accepted", "--question", "q", "--choice", "c",
                "--rationale", "r", "--actor", "human")
        self.ok("task", "move", "t1", "approved", "--actor", "human", "--reason", "ok")
        self.assertEqual(self.ok("close", "t1", "--actor", "human", "--reason", "x")["state"], "closed")
        self.assertEqual(self.ok("decision", "show", "d1")["status"], "accepted")
        self.assertEqual(self.ok("task", "show", "t1")["state"], "closed")

    def test_model_cannot_approve_or_accept(self):
        self.new_task()
        code, _, err = self.run_cli("decision", "add", "t1", "d1", "--status", "accepted", "--question", "q",
                                    "--choice", "c", "--rationale", "r", "--actor", "model")
        self.assertEqual((code, err), (10, "shit: human approval required\n"))
        self.ok("task", "move", "t1", "blocked", "--actor", "human", "--reason", "r", "--blocked-reason", "x",
                "--blocking-item", "y")

    def test_usage_errors_are_2_and_never_echo_input(self):
        secret = "S3CRET-VALUE"
        cases = (["nope", secret], ["task", "create", secret],
                 ["task", "move", "t1", secret, "--actor", "human", "--reason", "r"], ["task", "show"],
                 ["decision", "add", "t1", "d1", "--actor", secret], [], ["task", "--bogus", secret])
        for argv in cases:
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual((out, err), ("", "shit: usage error\n"))

    def test_abbreviated_option_is_usage_error(self):
        self.run_cli("init")
        code, _, _ = self.run_cli("task", "create", "t1", "--tit", "T", "--summary", "S", "--owner", "human",
                                  "--actor", "human")
        self.assertEqual(code, 2)

    def test_errors_print_only_fixed_text(self):
        marker = "zz-marker-zz"
        self.run_cli("init")
        cases = ((["task", "show", marker], 9),
                 (["evidence", "add", "t1", "e", "--path", f"../{marker}", "--description", "d",
                   "--actor", "human"], 5),
                 (["task", "show", "../" + marker], 3))
        for argv, code in cases:
            c, out, err = self.run_cli(*argv)
            self.assertEqual(c, code, argv)
            self.assertNotIn(marker, out + err)
            self.assertNotIn(str(self.repo), out + err)

    def test_no_store_is_9(self):
        self.assertEqual(self.run_cli("task", "show", "t1")[0], 9)

    def test_output_is_ascii_escaped(self):
        self.run_cli("init")
        out = self.ok("task", "create", "t1", "--title", "café", "--summary", "S", "--owner", "human",
                      "--actor", "human")
        self.assertEqual(out["title"], "café")
        _, text, _ = self.run_cli("task", "show", "t1")
        self.assertTrue(text.isascii())

    def test_control_characters_in_arguments_rejected_3(self):
        self.run_cli("init")
        code, _, _ = self.run_cli("task", "create", "t1", "--title", "a\x1b[2Jb", "--summary", "S", "--owner",
                                  "human", "--actor", "human")
        self.assertEqual(code, 3)

    def test_modules_import_no_process_or_net_modules(self):
        for mod in (cli_tasks, commands, artifacts):
            src = Path(mod.__file__).read_text()
            for word in ("socket", "subprocess", "urllib", "http"):
                self.assertNotIn(f"import {word}", src)


if __name__ == "__main__":
    unittest.main()
