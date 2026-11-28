"""Tests for the launcher scripts/threads_run.py (issue 55), against a fake `gh` executable on a temporary PATH.

Run: python3 -m unittest discover -s tests -v

The fake `gh` is a small script written into a temporary directory. It logs each call (arguments and the names
of the environment variables it received) and answers from a script file next to it, so no test touches the
network or a real credential. Nothing here is imported by the `shit` package, whose modules never start a process.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import _bootstrap
from _helpers import run_main

from shit import threads
from shit.errors import ShitError

SCRIPT = _bootstrap.ROOT / "scripts" / "threads_run.py"
REPO = "example-owner/example-repo"
SECRET = "stderr-secret-text"
ENV_SENTINEL = "SHIT_TEST_UNLISTED_VARIABLE"

FAKE_GH = '''#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.realpath(__file__))
cfg = json.load(open(os.path.join(here, "cfg.json")))
state_path = os.path.join(here, "state.json")
state = json.load(open(state_path)) if os.path.exists(state_path) else {{"read": 0, "reply": 0, "resolve": 0}}
argv = sys.argv[1:]
def resolved_config():
    # what gh would read: GH_CONFIG_DIR, else XDG_CONFIG_HOME/gh, else HOME/.config/gh
    base = os.environ.get("GH_CONFIG_DIR")
    if not base:
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.environ.get("HOME", ""), ".config")
        base = os.path.join(xdg, "gh")
    path = os.path.join(base, "config.yml")
    return open(path).read() if os.path.isfile(path) else None
names = ("HOME", "GH_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "TMPDIR")
seen = {{n: os.environ.get(n) for n in names}}
modes = {{n: (oct(os.stat(v).st_mode & 0o777) if v and os.path.isdir(v) else None) for n, v in seen.items()}}
with open(os.path.join(here, "calls.jsonl"), "a") as log:
    log.write(json.dumps({{"argv": argv, "env": sorted(os.environ), "stdin_tty": sys.stdin.isatty(), "vals": seen,
                          "modes": modes, "config_seen": resolved_config(), "me": here}}) + "\\n")
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
mode = answer.get("flood")
if mode:
    stream = sys.stdout if mode == "out" else sys.stderr
    try:
        while True:
            stream.write("x" * 65536)
            stream.flush()
    except BrokenPipeError:
        sys.exit(0)
if answer.get("sleep"):
    time.sleep(answer["sleep"])
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
        self.bin.chmod(0o755)  # the user's umask may be group-writable; --gh refuses such a directory
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

    def run_plan(self, name, *opts, **kw):
        environ = kw.pop("environ", None) or self.environ()
        return run_main(lambda a: launcher.main(a, environ=environ, cwd=self.work, **kw), [*opts, name])


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


class HostTrustTests(Case):
    def conf(self, host=None, name="config.toml"):
        extra = f'host = "{host}"\n' if host else ""
        (self.work / name).write_text(
            "schema_version = 1\n[project]\nname = \"p\"\n[tracker]\nrepository = \"o/r\"\n" + extra +
            "[review]\ndestination = \"reviewer\"\n[storage]\nroot = \".shit\"\n"
            "[log]\nfile = \".shit/events.jsonl\"\n[local_model]\ncontext_tokens = 32768\n", encoding="utf-8")
        return name

    def run_with(self, plan_name, *opts, environ=None):
        env = environ or self.environ()
        return run_main(lambda a: launcher.main(a, environ=env, cwd=self.work), [*opts, plan_name])

    def hostname(self):
        argv = self.calls()[0]["argv"]
        return argv[argv.index("--hostname") + 1]

    def test_no_config_uses_github_com_and_always_names_it(self):
        self.fake()
        code, _, _ = self.run_with(self.plan_file(kind="list"))
        self.assertEqual(code, 0)
        self.assertEqual(self.hostname(), "github.com")

    def test_a_plan_naming_another_host_is_refused_with_no_call(self):
        self.fake()
        code, out, err = self.run_with(self.plan_file(kind="list", host="evil.example.net"))
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(self.calls(), [])
        self.assertNotIn("evil.example.net", err)

    def test_configured_host_is_used_when_the_plan_has_none(self):
        self.fake()
        code, _, _ = self.run_with(self.plan_file(kind="list"), "--config", self.conf("git.example.org"))
        self.assertEqual(code, 0)
        self.assertEqual(self.hostname(), "git.example.org")

    def test_matching_host_is_accepted_and_case_does_not_matter(self):
        self.fake()
        code, _, _ = self.run_with(self.plan_file(kind="list", host="Git.Example.org"), "--config",
                                   self.conf("git.example.org"))
        self.assertEqual(code, 0)
        self.assertEqual(self.hostname(), "git.example.org")

    def test_a_plan_host_that_differs_from_the_configured_host_is_refused(self):
        self.fake()
        for plan_host in ("github.com", "other.example.org"):
            with self.subTest(plan_host=plan_host):
                code, out, err = self.run_with(self.plan_file(kind="list", host=plan_host), "--config",
                                               self.conf("git.example.org"))
                self.assertEqual((code, out), (3, ""))
                self.assertNotIn(plan_host, err)
        self.assertEqual(self.calls(), [])

    def test_a_config_in_the_working_directory_is_never_picked_up(self):
        self.fake()
        self.conf("git.example.org")  # planted config.toml: without --config it must have no effect
        self.assertEqual(self.run_with(self.plan_file(kind="list", host="git.example.org"))[0], 3)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.run_with(self.plan_file(kind="list"))[0], 0)
        self.assertEqual(self.hostname(), "github.com")
        self.assertEqual(set(self.calls()[0]["env"]) & {"GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}, set())

    def test_a_bad_or_missing_config_is_refused_before_any_call(self):
        self.fake()
        (self.work / "bad.toml").write_text("schema_version = ", encoding="utf-8")
        for opt, want in (("bad.toml", 3), ("missing.toml", 9), ("../x.toml", 5)):
            with self.subTest(opt=opt):
                self.assertEqual(self.run_with(self.plan_file(kind="list"), "--config", opt)[0], want)
        self.assertEqual(self.calls(), [])

    def test_only_the_tokens_for_the_target_host_reach_gh(self):
        all_tokens = {"GH_TOKEN": "a", "GITHUB_TOKEN": "b", "GH_ENTERPRISE_TOKEN": "c", "GITHUB_ENTERPRISE_TOKEN": "d",
                      "GH_HOST": "evil.example.net"}
        self.fake()
        self.run_with(self.plan_file(kind="list"), environ=self.environ(**all_tokens))
        names = set(self.calls()[0]["env"])
        self.assertEqual(names & set(all_tokens), {"GH_TOKEN", "GITHUB_TOKEN"})
        self.setUp()
        self.fake()
        self.run_with(self.plan_file(kind="list"), "--config", self.conf("git.example.org"),
                      environ=self.environ(**all_tokens))
        names = set(self.calls()[0]["env"])
        self.assertEqual(names & set(all_tokens), {"GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"})

    def test_child_env_is_per_host(self):
        env = {"GH_TOKEN": "a", "GITHUB_TOKEN": "b", "GH_ENTERPRISE_TOKEN": "c", "GITHUB_ENTERPRISE_TOKEN": "d"}
        self.assertEqual(set(launcher.child_env(env, "github.com")) & set(env), {"GH_TOKEN", "GITHUB_TOKEN"})
        self.assertEqual(set(launcher.child_env(env, "GitHub.com")) & set(env), {"GH_TOKEN", "GITHUB_TOKEN"})
        self.assertEqual(set(launcher.child_env(env, "git.example.org")) & set(env),
                         {"GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"})
        self.assertNotIn("GH_HOST", launcher.ENV_ALLOW)


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

    def test_a_command_that_cannot_start_is_tool_unavailable(self):
        self.fake()

        def cannot_start(argv, **kw):
            raise FileNotFoundError("gh")

        self.assertEqual(self.run_plan(self.plan_file(kind="list"), popen=cannot_start)[0], 11)

    def test_a_call_past_the_deadline_is_killed_and_is_tool_unavailable(self):
        from unittest import mock

        self.fake(read=[{"out": {}, "sleep": 30}])
        with mock.patch.object(launcher, "CALL_TIMEOUT", 1):
            started = time.monotonic()
            code, out, _ = self.run_plan(self.plan_file(kind="list"))
        self.assertEqual((code, out), (11, ""))
        self.assertLess(time.monotonic() - started, 20)


class CallRuleTests(Case):
    def test_each_call_is_bounded_without_shell_or_stdin(self):
        seen = []

        def spy(argv, **kw):
            seen.append((argv, kw))
            return subprocess.Popen(argv, **kw)

        self.fake()
        self.run_plan(self.plan_file(kind="list"), popen=spy)
        argv, kw = seen[0]
        self.assertIsInstance(argv, list)
        self.assertFalse(kw.get("shell", False))
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertIs(kw["stdout"], subprocess.PIPE)
        self.assertIs(kw["stderr"], subprocess.PIPE)
        self.assertEqual(launcher.CALL_TIMEOUT, 60)

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
        for name in launcher.RISKY + ("GH_HOST", "HOME", "TMPDIR", "GH_CONFIG_DIR", "XDG_CONFIG_HOME"):
            self.assertNotIn(name, launcher.ENV_ALLOW, name)

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


class HardeningTests(Case):
    def refused(self, *opts, **env):
        self.fake()
        return self.run_plan(self.plan_file(kind="list"), *opts, environ=self.environ(**env))

    def test_each_risky_variable_is_refused_by_name_with_no_call(self):
        for name in launcher.RISKY:
            with self.subTest(name=name):
                self.setUp()
                code, out, err = self.refused(**{name: "value-that-must-not-print"})
                self.assertEqual(code, 3)
                self.assertIn(name, out)
                self.assertNotIn("value-that-must-not-print", out + err)
                self.assertEqual(self.calls(), [])

    def test_opt_in_passes_exactly_that_name(self):
        for name in launcher.RISKY:
            with self.subTest(name=name):
                self.setUp()
                other = next(n for n in launcher.RISKY if n != name)
                self.fake()
                code, _, _ = self.run_plan(self.plan_file(kind="list"), "--allow-env", name,
                                           environ=self.environ(**{name: "v"}))
                self.assertEqual(code, 0)
                names = set(self.calls()[0]["env"])
                self.assertIn(name, names)
                self.assertEqual(names & (set(launcher.RISKY) - {name}), set(), other)

    def test_opting_in_one_name_does_not_cover_another(self):
        code, out, _ = self.refused("--allow-env", "HTTPS_PROXY", HTTPS_PROXY="a", SSL_CERT_FILE="b")
        self.assertEqual(code, 3)
        self.assertIn("SSL_CERT_FILE", out)
        self.assertNotIn("HTTPS_PROXY", out)
        self.assertEqual(self.calls(), [])

    def test_a_token_name_or_anything_else_can_never_be_opted_in(self):
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "LD_PRELOAD",
                     "PYTHONPATH", "HOME", "GH_HOST", "", "a b", "HTTPS_PROXY=x", "https_proxy;x"):
            with self.subTest(name=name):
                self.setUp()
                code, out, err = self.refused("--allow-env", name)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(self.calls(), [])
                self.assertNotIn(name, err) if name else None


class PrivateEnvTests(Case):
    def go(self, *opts, plan=None, environ=None, **kw):
        self.tmp = self.dir / "tmproot"
        self.tmp.mkdir(mode=0o700, exist_ok=True)
        self.tmp.chmod(0o700)
        env = environ or self.environ()
        return run_main(lambda a: launcher.main(a, environ=env, cwd=self.work, bases=[(str(self.tmp), "runtime")], **kw),
                        [*opts, plan or self.plan_file(kind="list")])

    def hostile_home(self, text="api_host: attacker.example\nhttp_unix_socket: /tmp/attacker.sock\n"):
        home = self.dir / "userhome"
        (home / ".config" / "gh").mkdir(parents=True)
        (home / ".config" / "gh" / "config.yml").write_text(text, encoding="utf-8")
        return home

    def test_the_child_gets_private_directories_and_never_the_users_home(self):
        self.fake()
        home = self.hostile_home()
        env = self.environ(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), GH_CONFIG_DIR=str(home / "gh"),
                           TMPDIR=str(home), XDG_DATA_HOME=str(home), XDG_CACHE_HOME=str(home),
                           XDG_STATE_HOME=str(home))
        self.assertEqual(self.go(environ=env)[0], 0)
        call = self.calls()[0]
        for name, value in call["vals"].items():
            self.assertTrue(value.startswith(str(self.tmp) + os.sep), (name, value))
            self.assertNotIn(str(home), value)
            self.assertEqual(call["modes"][name], "0o700", name)
        self.assertEqual(len(set(call["vals"].values())), len(call["vals"]))
        self.assertEqual({"GH_NO_UPDATE_NOTIFIER", "GH_PROMPT_DISABLED", "NO_COLOR"} - set(call["env"]), set())
        self.assertNotIn(str(home), json.dumps(call["env"]))

    def test_a_hostile_config_in_the_users_home_has_no_effect(self):
        self.fake()
        home = self.hostile_home()
        self.assertEqual(self.go(environ=self.environ(HOME=str(home)))[0], 0)
        self.assertIsNone(self.calls()[0]["config_seen"])

    def test_the_private_directory_is_removed_after_success_and_after_failure(self):
        self.fake()
        self.go()
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.setUp()
        self.fake(read=[{"out": "", "rc": 1}])
        self.assertEqual(self.go()[0], 8)
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.setUp()
        self.fake(read=[{"out": {}, "flood": "out"}])
        self.assertEqual(self.go()[0], 7)
        self.assertEqual(list(self.tmp.iterdir()), [])

        def boom(argv, **kw):
            raise RuntimeError("unexpected")

        self.setUp()
        self.fake()
        with self.assertRaises(RuntimeError):
            self.go(popen=boom)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_nothing_is_created_when_the_run_is_refused_early(self):
        self.fake()
        (self.work / "bad.json").write_text("x", encoding="utf-8")
        self.assertEqual(self.go(plan="bad.json")[0], 3)
        self.assertEqual(self.go("--allow-env", "GH_TOKEN")[0], 2)
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertEqual(self.calls(), [])

    def good_config_dir(self, mode=0o700):
        path = self.dir / "ghcfg"
        path.mkdir(exist_ok=True)
        path.chmod(mode)
        (path / "config.yml").write_text("git_protocol: https\n", encoding="utf-8")
        return path

    def test_the_opt_in_config_dir_is_used_and_home_stays_private(self):
        self.fake()
        cfg = self.good_config_dir()
        self.assertEqual(self.go("--gh-config-dir", str(cfg))[0], 0)
        call = self.calls()[0]
        self.assertRegex(call["vals"]["GH_CONFIG_DIR"], r"^/proc/self/fd/\d+$")  # the checked directory, by descriptor
        self.assertIn("git_protocol", call["config_seen"])
        self.assertTrue(call["vals"]["HOME"].startswith(str(self.tmp)))

    def test_a_writable_parent_of_the_config_dir_is_refused_with_no_call(self):
        self.fake()
        shared = self.dir / "shared"
        shared.mkdir()
        cfg = shared / "optcfg"
        cfg.mkdir(mode=0o700)
        (cfg / "config.yml").write_text("git_protocol: https\n", encoding="utf-8")
        for mode in (0o777, 0o775, 0o757, 0o1777):  # sticky but owned by this user is refused too
            with self.subTest(mode=oct(mode)):
                shared.chmod(mode)
                self.assertEqual(self.go("--gh-config-dir", str(cfg))[0], 5)
        shared.chmod(0o755)
        self.assertEqual(self.go("--gh-config-dir", str(cfg))[0], 0)
        self.assertEqual(len(self.calls()), 1)  # only the accepted run called gh

    def test_swapping_the_config_dir_after_the_check_does_not_change_what_gh_reads(self):
        self.fake()
        cfg = self.good_config_dir()

        def swap_then_start(argv, **kw):
            os.rename(cfg, self.dir / "moved")
            cfg.mkdir(mode=0o700)
            (cfg / "config.yml").write_text("http_unix_socket: /attacker/socket\n", encoding="utf-8")
            return subprocess.Popen(argv, **kw)

        code, out, _ = self.go("--gh-config-dir", str(cfg), popen=swap_then_start)
        self.assertEqual(code, 0, out)
        seen = self.calls()[0]["config_seen"]
        self.assertIn("git_protocol", seen)
        self.assertNotIn("attacker", seen)

    def test_the_opt_in_config_dir_is_validated(self):
        self.fake()
        cfg = self.good_config_dir()
        link = self.dir / "link"
        link.symlink_to(cfg)
        sub = cfg / "sub"
        sub.mkdir(mode=0o700)
        (self.dir / "file").write_text("x")
        bad_mode = self.dir / "mode750"
        bad_mode.mkdir()
        bad_mode.chmod(0o750)
        loose = self.dir / "mode777"
        loose.mkdir()
        loose.chmod(0o777)
        cases = (("ghcfg", 2), ("./ghcfg", 2), (str(self.dir / "x" / ".." / "ghcfg"), 2), (str(self.dir / "missing"), 9),
                 (str(link), 5), (str(link / "sub"), 5), (str(self.dir / "file"), 5), (str(bad_mode), 5),
                 (str(loose), 5), ("/", 5), ("/proc/1", 5))
        for arg, want in cases:
            with self.subTest(arg=arg):
                self.assertEqual(self.go("--gh-config-dir", arg)[0], want)
        self.assertEqual(self.calls(), [])

    def test_a_directory_owned_by_someone_else_is_refused(self):
        from unittest import mock

        self.fake()
        cfg = self.good_config_dir()
        with mock.patch.object(launcher.os, "geteuid", return_value=os.geteuid() + 1):
            self.assertEqual(self.go("--gh-config-dir", str(cfg))[0], 5)
        self.assertEqual(self.calls(), [])

    def test_the_old_config_directory_names_can_no_longer_be_opted_in(self):
        for name in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME", "HOME", "TMPDIR"):
            with self.subTest(name=name):
                self.assertEqual(self.go("--allow-env", name)[0], 2)


class TempBaseTests(Case):
    """Where the private directory is made: a fixed ordered list, never the ambient TMPDIR, TMP or TEMP."""

    def make(self, name, mode, parent=None):
        path = (parent or self.dir) / name
        path.mkdir(exist_ok=True)
        path.chmod(mode)
        return path

    def test_ambient_temp_variables_are_never_used(self):
        from unittest import mock

        self.fake()
        attacker = self.make("attacker-parent", 0o777)
        env = {"TMPDIR": str(attacker), "TMP": str(attacker), "TEMP": str(attacker)}
        with mock.patch.dict(os.environ, env):
            code, _, _ = self.run_plan(self.plan_file(kind="list"))
        self.assertEqual(code, 0)
        self.assertEqual(list(attacker.iterdir()), [])
        home = self.calls()[0]["vals"]["HOME"]
        self.assertFalse(home.startswith(str(attacker)))
        self.assertTrue(any(home.startswith(base) for base, _ in launcher.default_bases()), home)

    def test_the_default_order_and_the_documented_names(self):
        bases = launcher.default_bases()
        self.assertEqual(bases[0], (f"/run/user/{os.geteuid()}", "runtime"))
        self.assertEqual([k for _, k in bases[1:]], ["system", "system"])
        self.assertEqual([p for p, _ in bases[1:]], ["/tmp", "/var/tmp"])
        self.assertTrue(all(os.path.isabs(p) for p, _ in bases))

    def test_each_hostile_value_is_not_usable(self):
        from unittest import mock

        open_world = self.make("open", 0o777)  # world writable, not sticky
        own_sticky = self.make("sticky", 0o1777)  # sticky, but owned by this user, not root
        loose = self.make("loose", 0o755)
        link = self.dir / "link"
        link.symlink_to(self.make("real", 0o700))
        wide_parent = self.make("wide", 0o777)
        under_wide = self.make("run", 0o700, parent=wide_parent)
        (self.dir / "file").write_text("x")
        cases = (
            (str(open_world), "system"), (str(open_world), "runtime"), (str(own_sticky), "system"),
            (str(own_sticky), "runtime"), (str(loose), "runtime"), (str(link), "runtime"), (str(link), "system"),
            ("relative/dir", "runtime"), ("", "runtime"), (str(self.dir / "nonexistent"), "runtime"),
            (str(under_wide), "runtime"), (str(self.dir / "file"), "runtime"), (str(self.dir / ".." / "x"), "runtime"),
            (None, "runtime"),
        )
        for path, kind in cases:
            with self.subTest(path=path, kind=kind):
                self.assertFalse(launcher.usable_base(path, kind))
        good = self.make("good", 0o700)
        self.assertTrue(launcher.usable_base(str(good), "runtime"))
        with mock.patch.object(launcher.os, "geteuid", return_value=os.geteuid() + 1):  # someone else's directory
            self.assertFalse(launcher.usable_base(str(good), "runtime"))

    def test_the_system_temp_directory_is_usable_only_when_root_owned_and_sticky(self):
        st = os.lstat("/tmp")
        expected = st.st_uid == 0 and bool(st.st_mode & 0o1000)
        self.assertEqual(launcher.usable_base("/tmp", "system"), expected)
        self.assertFalse(launcher.usable_base("/tmp", "runtime"))  # not owned by us with mode 0700

    def test_the_first_usable_base_wins_and_fallbacks_are_used(self):
        a = self.make("a", 0o700)
        b = self.make("b", 0o700)
        bad = self.make("bad", 0o755)
        self.assertEqual(launcher.pick_base([(str(a), "runtime"), (str(b), "runtime")]), str(a))
        self.assertEqual(launcher.pick_base([(str(bad), "runtime"), (str(b), "runtime")]), str(b))
        self.assertEqual(launcher.pick_base([(str(self.dir / "none"), "runtime"), (str(bad), "runtime"),
                                             (str(b), "runtime")]), str(b))
        self.assertIsNone(launcher.pick_base([(str(bad), "runtime"), ("/nonexistent-shit55", "system")]))
        self.assertIsNone(launcher.pick_base([]))

    def test_when_no_base_is_safe_the_run_is_refused_with_no_call_and_nothing_created(self):
        self.fake()
        bad = self.make("bad", 0o777)
        name = self.plan_file(kind="list")
        code, out, _ = run_main(lambda a: launcher.main(a, environ=self.environ(), cwd=self.work,
                                                        bases=[(str(bad), "runtime"), (str(bad), "system")]), [name])
        self.assertEqual(code, 9)
        self.assertIn("no safe directory", out)
        self.assertEqual(self.calls(), [])
        self.assertEqual(list(bad.iterdir()), [])

    def test_the_root_is_checked_after_creation(self):
        from unittest import mock

        base = self.make("base", 0o700)
        root, fd = launcher.make_private_root(str(base))
        try:
            self.assertEqual(oct(os.stat(root).st_mode & 0o777), "0o700")
            self.assertEqual(os.path.dirname(root), str(base))
        finally:
            os.close(fd)
            shutil.rmtree(root)
        with (mock.patch.object(launcher.os, "geteuid", return_value=os.geteuid() + 1),
              self.assertRaises(ShitError)):  # refused, and the directory must not be left behind
            launcher.make_private_root(str(base))
        self.assertEqual(list(base.iterdir()), [])


class BoundedReadTests(Case):
    def test_a_flood_on_stdout_or_stderr_is_cut_off_with_the_limit_code(self):
        for stream in ("out", "err"):
            with self.subTest(stream=stream):
                self.setUp()
                self.fake(read=[{"out": {}, "flood": stream}])
                started = time.monotonic()
                code, out, _ = self.run_plan(self.plan_file(kind="list"))
                self.assertEqual((code, out), (7, ""))
                self.assertLess(time.monotonic() - started, 30)

    def test_a_flooding_reply_is_cut_off_too_and_reports_the_count(self):
        self.fake(reply=[{"out": {}, "flood": "err"}])
        code, out, _ = self.run_plan(self.plan_file([{"comment_id": 101, "action": "reply", "body": "x"}]))
        self.assertEqual(code, 7)
        self.assertIn("0 of 1 done", out)

    def test_an_answer_just_under_the_limit_is_read_whole(self):
        pad = "y" * (threads.ANSWER_LIMIT // 2)
        self.fake(read=[{"out": page([node("T1", 101, body=pad)])}])
        code, out, _ = self.run_plan(self.plan_file(kind="list"))
        self.assertEqual(code, 0)
        self.assertIn("1 unresolved thread(s)", out)


class AmbiguityTests(Case):
    def test_duplicate_comment_ids_are_refused_before_any_write(self):
        self.fake(read=[{"out": page([node("T_A", 7), node("T_B", 7)])}])
        code, out, _ = self.run_plan(self.plan_file([{"comment_id": 7, "action": "reply_resolve", "body": "x"}]))
        self.assertEqual((code, out), (8, ""))
        self.assertEqual(self.writes(), [])

    def test_duplicate_thread_ids_and_duplicates_across_pages_are_refused(self):
        for read in ([{"out": page([node("T", 1), node("T", 2)])}],
                     [{"out": page([node("A", 7)], more=True, cursor="n")}, {"out": page([node("B", 7)])}]):
            with self.subTest(read=str(read)[:30]):
                self.setUp()
                self.fake(read=read)
                code, _, _ = self.run_plan(self.plan_file(kind="list"))
                self.assertEqual(code, 8)


class OutputTests(Case):
    def test_no_printed_line_can_be_a_workflow_command(self):
        hostile = node("T1", 101, path="::add-mask::SECRET", body="::error::x\n::warning::y")
        hostile["comments"]["nodes"][0]["author"]["login"] = "::set-output::z"
        self.fake(read=[{"out": page([hostile])}])
        code, out, _ = self.run_plan(self.plan_file(kind="list"))
        self.assertEqual(code, 0)
        for line in out.splitlines():
            self.assertFalse(line.lstrip().startswith("::"), line)
        self.assertIn("file: ::add-mask::SECRET", out)


class GhPathTests(Case):
    def gh(self):
        return str(self.bin / "gh")

    def test_explicit_gh_is_used_and_path_is_not_searched(self):
        self.fake()
        code, _, _ = self.run_plan(self.plan_file(kind="list"), "--gh", self.gh(),
                                   environ={"PATH": str(self.dir / "empty"), "HOME": str(self.dir)})
        self.assertEqual(code, 0)
        self.assertEqual(len(self.calls()), 1)

    def test_a_writable_parent_directory_is_refused(self):
        self.fake()
        for mode in (0o775, 0o757, 0o777):
            with self.subTest(mode=oct(mode)):
                self.bin.chmod(mode)
                self.assertEqual(self.run_plan(self.plan_file(kind="list"), "--gh", self.gh())[0], 5)
        self.bin.chmod(0o755)
        sticky = self.dir / "sticky"
        sticky.mkdir()
        (sticky / "inner").mkdir()
        shutil.copy(self.bin / "gh", sticky / "inner" / "gh")
        (sticky / "inner" / "gh").chmod(0o755)
        sticky.chmod(0o1777)  # sticky but owned by this user, not by root: still refused
        self.assertEqual(self.run_plan(self.plan_file(kind="list"), "--gh", str(sticky / "inner" / "gh"))[0], 5)
        self.assertEqual(self.calls(), [])

    def test_a_writable_grandparent_is_refused_but_a_root_sticky_tmp_is_not(self):
        self.fake()
        self.dir.chmod(0o770)
        self.assertEqual(self.run_plan(self.plan_file(kind="list"), "--gh", self.gh())[0], 5)
        self.dir.chmod(0o700)
        self.assertEqual(self.run_plan(self.plan_file(kind="list"), "--gh", self.gh())[0], 0)

    def test_the_checked_file_is_the_one_that_runs_even_if_the_path_is_swapped(self):
        self.fake()
        real = self.bin / "gh"

        def swap_then_start(argv, **kw):
            os.replace(real, self.bin / "moved")
            real.write_text("#!" + sys.executable + "\nimport sys; open(" + repr(str(self.dir / "evil")) +
                            ", 'w').write('ran')\n")
            real.chmod(0o755)
            return subprocess.Popen(argv, **kw)

        code, out, _ = self.run_plan(self.plan_file(kind="list"), "--gh", str(real), popen=swap_then_start)
        self.assertEqual(code, 0, out)
        self.assertFalse((self.dir / "evil").exists())
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0]["me"], str(self.bin))

    def test_a_program_or_directory_owned_by_another_user_is_refused_whatever_its_mode(self):
        from unittest import mock

        self.fake()
        self.assertEqual(self.run_plan(self.plan_file(kind="list"), "--gh", self.gh())[0], 0)
        with mock.patch.object(launcher.os, "geteuid", return_value=os.geteuid() + 1):  # now it is "someone else's"
            self.assertEqual(self.run_plan(self.plan_file(kind="list"), "--gh", self.gh())[0], 5)
            with self.assertRaises(ShitError):
                launcher.open_gh(self.gh())

    def test_the_directory_chain_and_the_file_are_each_checked_for_owner(self):
        from unittest import mock

        self.fake()
        chain_dir = self.bin
        launcher._safe_dir_chain(str(chain_dir))  # ours, mode 0755: fine
        with mock.patch.object(launcher.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(ShitError):  # a directory owned by another non-root user, even at mode 0755
                launcher._safe_dir_chain(str(chain_dir))
            with (mock.patch.object(launcher, "_safe_dir_chain", return_value=str(chain_dir)),
                  self.assertRaises(ShitError)):  # the chain is waved through: the program's own owner is checked
                launcher.open_gh(self.gh())

    def test_a_root_owned_program_in_a_root_owned_chain_is_accepted(self):
        candidate = "/usr/bin/env"
        if not os.path.isfile(candidate) or os.path.islink(candidate) or os.lstat(candidate).st_uid != 0:
            self.skipTest("no root-owned regular /usr/bin/env here")
        path, fd = launcher.open_gh(candidate)
        os.close(fd)
        self.assertEqual(path, candidate)

    def test_bad_explicit_gh_is_refused_with_no_call(self):
        self.fake()
        real = self.bin / "gh"
        (self.dir / "ln").symlink_to(real)
        groupw = self.dir / "gw"
        groupw.write_text("x")
        groupw.chmod(0o775)
        otherw = self.dir / "ow"
        otherw.write_text("x")
        otherw.chmod(0o757)
        noexec = self.dir / "nx"
        noexec.write_text("x")
        noexec.chmod(0o644)
        cases = (("gh", 2), ("./gh", 2), (str(self.bin / ".." / "bin" / "gh"), 2), (str(self.dir / "missing"), 9),
                 (str(self.dir / "ln"), 5), (str(groupw), 5), (str(otherw), 5), (str(noexec), 5), (str(self.bin), 5))
        for arg, want in cases:
            with self.subTest(arg=arg):
                self.assertEqual(self.run_plan(self.plan_file(kind="list"), "--gh", arg)[0], want)
        self.assertEqual(self.calls(), [])


class PlacementTests(unittest.TestCase):
    def test_the_package_does_not_import_the_process_module_and_the_launcher_is_outside_it(self):
        package = _bootstrap.ROOT / "shit"
        self.assertFalse(SCRIPT.is_relative_to(package))
        self.assertIn("import subprocess", SCRIPT.read_text())
        self.assertNotIn("import subprocess", (package / "threads.py").read_text())
        self.assertNotIn("subprocess", (package / "threads.py").read_text().replace("subprocess launch", ""))


if __name__ == "__main__":
    unittest.main()
