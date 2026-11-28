"""Shared fixtures for the check, doctor, privacy and hook tests (issue #13)."""

import contextlib
import io
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from shit import commands
from shit import task as task_mod
from shit.project import Project

T0 = "2026-11-26T15:30:00Z"
T1 = "2026-11-26T15:31:00Z"
T2 = "2026-11-26T15:32:00Z"
T3 = "2026-11-26T15:33:00Z"
T4 = "2026-11-26T15:34:00Z"
T5 = "2026-11-26T15:35:00Z"

# Fake secrets are assembled at run time so this file itself holds no scannable secret.
FAKE_GITHUB = "gh" + "p_" + "a1B2c3D4e5" * 4
FAKE_AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
FAKE_KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"
FAKE_SLACK = "xo" + "xb-" + "1234567890-abcdefghij"
FAKE_JWT = "ey" + "Jhbgc1234567890" + "." + "ey" + "Jzdwi1234567890" + "." + "sig1234567890abc"
FAKE_SESSION = "https://claude" + ".ai/code/session_" + "01ABCDEFGHIJKLMN"

BRIEF = """# Work brief
issue: ChiefGyk3D/example#13
role: implementer
packs: none
objective: Do the thing.
scope: one module
acceptance: tests pass
data_class: public
cleanup_duty: remove scratch
evidence: report header
review_destination: reviewer
"""

REPORT = """ROLE: implementer   MODEL: example-model
HEAD: abc1234   BASE: def5678
GATE: python3 -m unittest  exit=0
OTHER GATES: none
PACKS LOADED: none
TREE: clean (git status empty)
NOT VERIFIED: none
LOCAL-LLM: n/a - code and tests

Free text after the header.
"""

CLEANUP = "removed: the scratch folder\nkept: nothing\n"

DECISION = """{
  "schema": "decision",
  "schema_version": 1,
  "id": "d1",
  "task_id": "t1",
  "created_at": "2026-11-26T15:34:00Z",
  "actor": "human",
  "status": "accepted",
  "question": "Approve?",
  "choice": "Yes.",
  "rationale": "Reviewed."
}
"""


def run_main(main, argv):
    """Run a module's main with captured streams; return (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def git(*args, cwd, env=None):
    base = {"PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    base.update(env or {})
    return subprocess.run(["git", *args], cwd=cwd, env=base, capture_output=True, text=True, check=False)


class RepoCase(unittest.TestCase):
    """A real git repository with a .shit store in a temporary directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.assertEqual(git("init", "-q", cwd=self.repo).returncode, 0)
        self.proj = Project.init(self.repo)
        self.addCleanup(self.proj.close)
        self.store = self.repo / ".shit"
        self.outside = self.base / "outside"
        self.outside.mkdir()

    # -- records ---------------------------------------------------------

    def new_task(self, tid="t1", state="open"):
        # The log enforces chronology, so each task continues after the last stamp used by an earlier one.
        base = getattr(self, "_next_minute", 0)
        stamp = lambda n: f"2026-11-26T{15 + (30 + base + n) // 60:02d}:{(30 + base + n) % 60:02d}:00Z"
        task_mod.create_task(self.proj, task_id=tid, title="Title", summary="Summary", owner="human",
                             actor="human", now=stamp(0))
        path = [("running", "wrapper"), ("review", "wrapper"), ("approved", "human"), ("closed", "human")]
        used = 0
        for n, (to, actor) in enumerate(path, start=1):
            if state == "open":
                break
            if to == "approved":
                commands.add_decision(self.proj, task_id=tid, decision_id=f"decision-{tid}", status="accepted",
                                      question="Approve?", choice="Yes.", rationale="Reviewed.", actor="human",
                                      now=stamp(n))
                self._next_minute = base + n + 1
                task_mod.transition(self.proj, tid, to, actor, "step", now=stamp(n + 1))
                used = n + 1
            else:
                task_mod.transition(self.proj, tid, to, actor, "step", now=stamp(n))
                used = n
            if to == state:
                break
        self._next_minute = base + used + 1

    def put(self, rel, text):
        parent = self.store / Path(rel).parent
        parent.mkdir(mode=0o700, exist_ok=True)
        (self.store / rel).write_text(text, encoding="utf-8")

    def full_task(self, tid="t1", state="closed"):
        self.new_task(tid, state)
        self.put(f"briefs/{tid}.md", BRIEF)
        self.put(f"reports/{tid}.md", REPORT)
        self.put(f"cleanup/{tid}.md", CLEANUP)

    def snapshot(self):
        out = {}
        for p in sorted(self.repo.rglob("*")):
            if p.is_symlink() or not p.is_file():
                out[str(p)] = "link-or-dir"
            else:
                out[str(p)] = (p.read_bytes(), p.stat().st_mode)
        return out

    def deny_file(self, text, mode=0o600, name="deny.txt"):
        path = self.outside / name
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def assert_clean(self, *texts, secrets=()):
        for text in texts:
            for secret in secrets:
                self.assertNotIn(secret, text)

    @staticmethod
    def mode(path):
        return stat.S_IMODE(os.stat(path).st_mode)
