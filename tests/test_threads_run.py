"""Tests for the launcher scripts/threads_run.py (issue 55), against a fake `gh` executable on a temporary PATH.

Run: python3 -m unittest discover -s tests -v

The fake `gh` is a small script written into a temporary directory. It logs each call (arguments and the names
of the environment variables it received) and answers from a script file next to it, so no test touches the
network or a real credential. Nothing here is imported by the `shit` package, whose modules never start a process.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _bootstrap
from _helpers import run_main

from shit import threads

SCRIPT = _bootstrap.ROOT / "scripts" / "threads_run.py"
REPO = "example-owner/example-repo"
SECRET = "stderr-secret-text"
ENV_SENTINEL = "SHIT_TEST_UNLISTED_VARIABLE"

FAKE_GH = '''#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(here, "cfg.json")))
state_path = os.path.join(here, "state.json")
state = json.load(open(state_path)) if os.path.exists(state_path) else {{"read": 0, "reply": 0, "resolve": 0}}
argv = sys.argv[1:]
with open(os.path.join(here, "calls.jsonl"), "a") as log:
    log.write(json.dumps({{"argv": argv, "env": sorted(os.environ), "stdin_tty": sys.stdin.isatty()}}) + "\\n")
text = " ".join(argv)
if "-X POST" in text:
    kind = "reply"
elif "resolveReviewThread" in text:
    kind = "resolve"
else:
    kind = "read"
index = state[kind]
state[kind] += 1
json.dump(state, open(state_path, "w"))
answers = cfg[kind]
answer = answers[min(index, len(answers) - 1)]
sys.stderr.write(cfg.get("stderr", ""))
sys.stdout.write(answer["out"] if isinstance(answer["out"], str) else json.dumps(answer["out"]))
sys.exit(answer.get("rc", 0))
'''

OK_RESOLVE = {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}


def node(thread_id, comment_id, path="a.py", body="please fix"):
    return {"id": thread_id, "isResolved": False, "isOutdated": False, "path": path, "line": 3,
            "comments": {"nodes": [{"databaseId": comment_id, "author": {"login": "rev"}, "body": body}]}}


def page(nodes, more=False, cursor="c1"):
    block = {"pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes}
    return {"data": {"repository": {"pullRequest": {"reviewThreads": block}}}}


def load_launcher():
    spec = importlib.util.spec_from_file_location("threads_run_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = load_launcher()


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name).resolve()
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.work = self.dir / "work"
        self.work.mkdir()

    def fake(self, read=None, reply=None, resolve=None, stderr=SECRET):
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH.format(python=sys.executable), encoding="utf-8")
        gh.chmod(0o755)
        cfg = {
            "read": read if read is not None else [{"out": page([node("T1", 101), node("T2", 102)])}],
            "reply": reply if reply is not None else [{"out": {}}],
            "resolve": resolve if resolve is not None else [{"out": OK_RESOLVE}],
            "stderr": stderr,
        }
        (self.bin / "cfg.json").write_text(json.dumps(cfg), encoding="utf-8")

    def calls(self):
        path = self.bin / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def writes(self):
        return [c for c in self.calls() if "-X" in c["argv"] or any("resolveReviewThread" in a for a in c["argv"])]

    def environ(self, **extra):
        return {"PATH": str(self.bin), "HOME": str(self.dir), **extra}

    def plan_file(self, rows=None, kind="apply", host=None, name="plan.json"):
        if kind == "list":
            plan = threads.build_plan("list", host, REPO, 9)
        else:
            plan = threads.build_plan("apply", host, REPO, 9, threads._check_rows(rows))
        (self.work / name).write_text(threads.render_plan(plan), encoding="utf-8")
        return name

    def run_plan(self, name, **kw):
        environ = kw.pop("environ", None) or self.environ()
        return run_main(lambda a: launcher.main(a, environ=environ, cwd=self.work, **kw), [name])


class ListTests(Case):
    def test_list_prints_the_threads_and_writes_nothing(self):
        self.fake()
        code, out, err = self.run_plan(self.plan_file(kind="list"))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("101 a.py:3 rev: please fix", out)
        self.assertIn("2 unresolved thread(s)", out)
        self.assertEqual(self.writes(), [])

    def test_pages_are_followed_and_capped(self):
        self.fake(read=[{"out": page([node("T1", 101)], more=True, cursor="n1")}, {"out": page([node("T2", 102)])}])
        code, out, _ = self.run_plan(self.plan_file(kind="list"))
        self.assertEqual(code, 0)
        self.assertIn("102", out)
        self.assertIn("cursor=n1", " ".join(self.calls()[1]["argv"]))
        self.setUp()
        self.fake(read=[{"out": page([node("T1", 101)], more=True, cursor="n")}])
        code, out, _ = self.run_plan(self.plan_file(kind="list"))
        self.assertEqual(code, 8)
        self.assertEqual(len(self.calls()), threads.MAX_PAGES)

    def test_read_failures_fail_closed_and_stderr_is_never_shown(self):
        for read in ([{"out": "not json"}], [{"out": {"errors": [{"message": "x"}]}}], [{"out": "", "rc": 1}],
                     [{"out": {"data": {}}}]):
            with self.subTest(read=str(read)[:40]):
                self.setUp()
                self.fake(read=read)
                code, out, err = self.run_plan(self.plan_file(kind="list"))
                self.assertEqual(code, 8)
                self.assertNotIn(SECRET, out + err)

    def test_host_reaches_gh(self):
        self.fake()
        self.run_plan(self.plan_file(kind="list", host="git.example.org"))
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--hostname") + 1], "git.example.org")


class ApplyTests(Case):
    ROWS = ({"comment_id": 101, "action": "reply_resolve", "body": "Fixed."},
            {"comment_id": 102, "action": "reply", "body": "Kept."},
            {"comment_id": 103, "action": "skip"})

    def test_replies_then_resolves_in_order(self):
        self.fake()
        code, out, err = self.run_plan(self.plan_file(list(self.ROWS)))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), ["replied 101", "replied 102", "resolved 101"])
        kinds = [("reply" if "-X" in c["argv"] else "resolve") for c in self.writes()]
        self.assertEqual(kinds, ["reply", "reply", "resolve"])
        self.assertIn(f"repos/{REPO}/pulls/9/comments/101/replies", self.writes()[0]["argv"])
        self.assertIn("id=T1", self.writes()[2]["argv"])

    def test_unknown_thread_writes_nothing(self):
        self.fake(read=[{"out": page([node("T1", 101)])}])
        code, out, _ = self.run_plan(self.plan_file(list(self.ROWS)))
        self.assertEqual((code, out), (9, ""))
        self.assertEqual(self.writes(), [])

    def test_a_resolve_target_the_read_did_not_return_writes_nothing(self):
        rows = [{"comment_id": 102, "action": "reply", "body": "a"},
                {"comment_id": 101, "action": "reply_resolve", "body": "b"}]
        self.fake(read=[{"out": page([node("T2", 102)])}])
        code, out, _ = self.run_plan(self.plan_file(rows))
        self.assertEqual((code, out), (9, ""))
        self.assertEqual(self.writes(), [])

    def test_a_missing_thread_id_is_an_error_not_an_index_error(self):
        from unittest import mock

        rows = [{"comment_id": 101, "action": "reply_resolve", "body": "b"}]
        self.fake(read=[{"out": page([node("T2", 102)])}])
        with mock.patch.object(launcher.threads, "check_targets"):  # simulate the precondition being bypassed
            code, out, _ = self.run_plan(self.plan_file(rows))
        self.assertEqual(code, 8)
        self.assertIn("resolve 101 failed", out)
        self.assertEqual([("-X" in c["argv"]) for c in self.writes()], [True])

    def test_a_resolved_or_outdated_thread_is_not_a_target(self):
        gone = node("T1", 101)
        gone["isResolved"] = True
        self.fake(read=[{"out": page([gone, node("T2", 102)])}])
        code, _, _ = self.run_plan(self.plan_file(list(self.ROWS)))
        self.assertEqual(code, 9)
        self.assertEqual(self.writes(), [])

    def test_a_failed_reply_stops_and_is_not_retried(self):
        self.fake(reply=[{"out": {}, "rc": 1}])
        code, out, err = self.run_plan(self.plan_file(list(self.ROWS)))
        self.assertEqual(code, 8)
        self.assertEqual(len(self.writes()), 1)
        self.assertIn("0 of 3 done", out)
        self.assertNotIn(SECRET, out + err)

    def test_second_reply_failing_keeps_the_first_and_runs_no_resolve(self):
        self.fake(reply=[{"out": {}}, {"out": {}, "rc": 1}])
        code, out, _ = self.run_plan(self.plan_file(list(self.ROWS)))
        self.assertEqual(code, 8)
        self.assertIn("replied 101", out)
        self.assertIn("1 of 3 done", out)
        self.assertEqual(len(self.writes()), 2)

    def test_resolve_must_say_resolved(self):
        for answer in ({"data": {"resolveReviewThread": {"thread": {"isResolved": False}}}},
                       {"errors": [{"message": "x"}], **OK_RESOLVE}, {"data": None}, {}, {"out": 1}):
            with self.subTest(answer=str(answer)[:50]):
                self.setUp()
                self.fake(resolve=[{"out": answer}])
                code, out, _ = self.run_plan(self.plan_file(list(self.ROWS)))
                self.assertEqual(code, 8)
                self.assertIn("2 of 3 done", out)

    def test_resolve_command_failure_is_reported_with_the_count(self):
        self.fake(resolve=[{"out": "", "rc": 1}])
        code, out, err = self.run_plan(self.plan_file(list(self.ROWS)))
        self.assertEqual(code, 8)
        self.assertIn("2 of 3 done", out)
        self.assertNotIn(SECRET, out + err)

    def test_no_resolve_runs_when_any_reply_failed(self):
        self.fake(reply=[{"out": {}}, {"out": {}, "rc": 1}])
        self.run_plan(self.plan_file(list(self.ROWS)))
        self.assertTrue(all("-X" in c["argv"] for c in self.writes()))

    def test_all_skip_plan_makes_no_write(self):
        self.fake()
        code, out, _ = self.run_plan(self.plan_file([{"comment_id": 5, "action": "skip"}]))
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(self.writes(), [])


class RefusalTests(Case):
    def test_a_bad_plan_makes_no_call(self):
        self.fake()
        good = threads.render_plan(threads.build_plan("apply", None, REPO, 9,
                                                      threads._check_rows([{"comment_id": 101, "action": "reply",
                                                                            "body": "ok"}])))
        plan = json.loads(good)
        plan["requests"][1]["endpoint"] = "repos/other/thing/pulls/1/comments/101/replies"
        for label, text in (("tampered", json.dumps(plan)), ("not json", "x"), ("list", "[]"),
                            ("duplicate", '{"a": 1, "a": 2}')):
            with self.subTest(label):
                (self.work / "bad.json").write_text(text, encoding="utf-8")
                code, out, _ = self.run_plan("bad.json")
                self.assertEqual((code, out), (3, ""))
                self.assertEqual(self.calls(), [])

    def test_plan_path_rules(self):
        self.fake()
        (self.work / "sub").mkdir()
        real = self.work / "real.json"
        real.write_text("{}", encoding="utf-8")
        (self.work / "link.json").symlink_to(real)
        for arg, want in (("missing.json", 9), ("../x.json", 5), ("sub", 5), ("link.json", 5)):
            with self.subTest(arg=arg):
                code, out, _ = self.run_plan(arg)
                self.assertEqual((code, out), (want, ""))
        self.assertEqual(self.calls(), [])

    def test_usage(self):
        for argv in ([], ["a", "b"]):
            code, out, _ = run_main(lambda a: launcher.main(a, environ=self.environ(), cwd=self.work), argv)
            self.assertEqual((code, out), (2, ""))

    def test_missing_gh_is_tool_unavailable(self):
        name = self.plan_file(kind="list")
        code, _, _ = self.run_plan(name, environ={"PATH": str(self.dir / "empty")})
        self.assertEqual(code, 11)

    def test_a_command_that_cannot_start_or_times_out_is_tool_unavailable(self):
        self.fake()
        name = self.plan_file(kind="list")

        def cannot_start(argv, **kw):
            raise FileNotFoundError("gh")

        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 1)

        for runner in (cannot_start, slow):
            code, _, _ = self.run_plan(name, runner=runner)
            self.assertEqual(code, 11)


class CallRuleTests(Case):
    def test_each_call_is_bounded_without_shell_or_stdin(self):
        seen = []

        def spy(argv, **kw):
            seen.append((argv, kw))
            return subprocess.run(argv, check=False, **{k: v for k, v in kw.items() if k != "check"})

        self.fake()
        self.run_plan(self.plan_file(kind="list"), runner=spy)
        argv, kw = seen[0]
        self.assertIsInstance(argv, list)
        self.assertFalse(kw.get("shell", False))
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["timeout"], 60)
        self.assertIs(kw["check"], False)
        self.assertTrue(kw["capture_output"])

    def test_the_child_gets_only_the_allowed_environment(self):
        self.fake()
        env = self.environ(GH_TOKEN="fake-credential-value", **{ENV_SENTINEL: "x", "PYTHONPATH": "/nowhere"})
        self.run_plan(self.plan_file(kind="list"), environ=env)
        names = set(self.calls()[0]["env"])
        self.assertIn("PATH", names)
        self.assertIn("GH_TOKEN", names)
        self.assertIn("GH_PROMPT_DISABLED", names)
        self.assertNotIn(ENV_SENTINEL, names)
        self.assertNotIn("PYTHONPATH", names)
        self.assertEqual(names & {"GITHUB_TOKEN", "SHIT_TEST_UNLISTED_VARIABLE"}, set())

    def test_child_env_drops_values_with_control_characters_and_forces_flags(self):
        env = launcher.child_env({"PATH": "/bin", "HOME": "/h\nx", "GH_TOKEN": "t", "LANG": "C" + chr(0)})
        self.assertEqual(env["PATH"], "/bin")
        self.assertNotIn("HOME", env)
        self.assertNotIn("LANG", env)
        self.assertEqual((env["GH_PROMPT_DISABLED"], env["NO_COLOR"]), ("1", "1"))

    def test_the_allow_list_has_no_unneeded_credential_names(self):
        self.assertFalse(any(name.endswith(("_SECRET", "_PASSWORD")) for name in launcher.ENV_ALLOW))

    def test_the_token_never_reaches_the_output(self):
        self.fake(stderr="fake-credential-value")
        env = self.environ(GH_TOKEN="fake-credential-value")
        code, out, err = self.run_plan(self.plan_file([{"comment_id": 101, "action": "reply", "body": "x"}]),
                                       environ=env)
        self.assertEqual(code, 0)
        self.assertNotIn("fake-credential-value", out + err)

    def test_the_script_runs_as_a_program(self):
        self.fake()
        name = self.plan_file(kind="list")
        done = subprocess.run([sys.executable, "-I", str(SCRIPT), name], cwd=self.work, env=self.environ(),
                              capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("unresolved thread(s)", done.stdout)
        done = subprocess.run([sys.executable, "-I", str(SCRIPT)], cwd=self.work, env=self.environ(),
                              capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertNotIn(os.fspath(self.work), done.stderr)


class PlacementTests(unittest.TestCase):
    def test_the_package_does_not_import_the_process_module_and_the_launcher_is_outside_it(self):
        package = _bootstrap.ROOT / "shit"
        self.assertFalse(SCRIPT.is_relative_to(package))
        self.assertIn("import subprocess", SCRIPT.read_text())
        self.assertNotIn("import subprocess", (package / "threads.py").read_text())
        self.assertNotIn("subprocess", (package / "threads.py").read_text().replace("subprocess launch", ""))


if __name__ == "__main__":
    unittest.main()
