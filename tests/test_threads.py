"""Tests for the review-thread tool (issue 55): list, then reply and resolve from a dispositions file.

Run: python3 -m unittest discover -s tests -v

The GitHub CLI is replaced by a fake runner, so no test touches the network. Each refusal has an accepted
twin, and the "nothing written" rule is checked by counting the write calls the fake saw.
Exit codes are the shared table in docs/exit-codes.md.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (puts the repository root on sys.path)
from _helpers import run_main

from shit import config, records, threads

REPO = "example-owner/example-repo"
GH = "/fake/bin/gh"


def node(thread_id="T1", comment_id=101, path="a.py", line=3, body="please fix", resolved=False, outdated=False,
         author="reviewer-one"):
    return {
        "id": thread_id,
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": path,
        "line": line,
        "comments": {"nodes": [{"databaseId": comment_id, "author": {"login": author}, "body": body}]},
    }


def page(nodes, more=False, cursor="c1"):
    block = {"pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes}
    return {"data": {"repository": {"pullRequest": {"reviewThreads": block}}}}


RESOLVED_OK = {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}


class FakeGh:
    """Scripted stand-in for subprocess.run. `pages` answers the thread query in order."""

    def __init__(self, pages=None, reply_rc=0, resolve=None, read_rc=0, raw=None):
        self.pages = list(pages if pages is not None else [page([node()])])
        self.reply_rc = reply_rc
        self.resolve = RESOLVED_OK if resolve is None else resolve
        self.read_rc = read_rc
        self.raw = raw
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        text = " ".join(argv)
        if "-X POST" in text:
            return subprocess.CompletedProcess(argv, self.reply_rc, "{}", "stderr-secret-text")
        if "resolveReviewThread" in text:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.resolve), "")
        if self.raw is not None:
            return subprocess.CompletedProcess(argv, self.read_rc, self.raw, "")
        return subprocess.CompletedProcess(argv, self.read_rc, json.dumps(self.pages.pop(0)), "")

    def writes(self):
        return [c for c in self.calls if "-X POST" in " ".join(c[0]) or "resolveReviewThread" in " ".join(c[0])]


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name).resolve()

    def file(self, rows, name="disp.json"):
        path = self.dir / name
        path.write_text(rows if isinstance(rows, str) else json.dumps(rows), encoding="utf-8")
        return name

    def go(self, argv, fake, gh=GH):
        return run_main(lambda a: threads.main(a, runner=fake, gh_path=gh, cwd=self.dir), argv)


class ListTests(Case):
    def test_lists_unresolved_current_threads_grouped_by_file(self):
        fake = FakeGh([page([
            node("T1", 101, "b.py", 7, "second"),
            node("T2", 102, "a.py", 3, "first"),
            node("T3", 103, "a.py", 4, "done", resolved=True),
            node("T4", 104, "a.py", 5, "old", outdated=True),
        ])])
        code, out, err = self.go(["threads", "list", REPO, "9"], fake)
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "a.py")
        self.assertIn("102 a.py:3 reviewer-one: first", lines[1])
        self.assertEqual(lines[2], "b.py")
        self.assertNotIn("103", out)
        self.assertNotIn("104", out)
        self.assertEqual(lines[-1], f"2 unresolved thread(s) on {REPO}#9")
        self.assertEqual(fake.writes(), [])

    def test_empty_list_is_success(self):
        code, out, _ = self.go(["threads", "list", REPO, "9"], FakeGh([page([])]))
        self.assertEqual(code, 0)
        self.assertIn("no unresolved threads", out)

    def test_follows_pages(self):
        fake = FakeGh([page([node("T1", 101)], more=True, cursor="next"), page([node("T2", 102, "z.py")])])
        code, out, _ = self.go(["threads", "list", REPO, "9"], fake)
        self.assertEqual(code, 0)
        self.assertIn("101", out)
        self.assertIn("102", out)
        self.assertIn("cursor=next", " ".join(fake.calls[1][0]))

    def test_page_cap_is_refused_not_cut(self):
        fake = FakeGh([page([node("T1", 101)], more=True, cursor="n")] * (threads.MAX_PAGES + 1))
        code, out, _ = self.go(["threads", "list", REPO, "9"], fake)
        self.assertEqual(code, 8)
        self.assertEqual(out, "")

    def test_body_is_one_line_cut_and_escaped(self):
        body = "line one\nline two\x1b[31m red " + "x" * 400
        code, out, _ = self.go(["threads", "list", REPO, "9"], FakeGh([page([node(body=body)])]))
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        self.assertEqual(len(out.splitlines()), 3)
        self.assertLessEqual(len(out.splitlines()[1]), 400)

    def test_bidi_and_zero_width_are_escaped(self):
        body = "a" + chr(0x202E) + "b" + chr(0x200B) + "c"
        _, out, _ = self.go(["threads", "list", REPO, "9"], FakeGh([page([node(body=body)])]))
        self.assertNotIn(chr(0x202E), out)
        self.assertNotIn(chr(0x200B), out)

    def test_bad_answer_shape_fails_closed(self):
        for pages in ([{"data": {}}], [{"errors": [{"message": "x"}]}], [page([node(comment_id=True)])],
                      [page([node(comment_id=0)])], [page([node(thread_id="bad id!")])]):
            with self.subTest(pages=str(pages)[:40]):
                code, _, _ = self.go(["threads", "list", REPO, "9"], FakeGh(pages))
                self.assertEqual(code, 8)

    def test_unreadable_json_and_duplicate_keys_fail_closed(self):
        for raw in ("not json", '{"data": 1, "data": 2}', ""):
            with self.subTest(raw=raw):
                code, _, _ = self.go(["threads", "list", REPO, "9"], FakeGh(raw=raw))
                self.assertEqual(code, 8)

    def test_gh_failure_is_closed_and_stderr_is_not_shown(self):
        code, out, err = self.go(["threads", "list", REPO, "9"], FakeGh(read_rc=1, raw="{}"))
        self.assertEqual(code, 8)
        self.assertNotIn("stderr-secret-text", out + err)

    def test_missing_gh_is_tool_unavailable(self):
        code, _, _ = self.go(["threads", "list", REPO, "9"], FakeGh(), gh=None)
        self.assertEqual(code, 11)

    def test_runner_that_cannot_start_is_tool_unavailable(self):
        def boom(argv, **kw):
            raise FileNotFoundError("gh")

        code, _, _ = self.go(["threads", "list", REPO, "9"], boom)
        self.assertEqual(code, 11)

    def test_timeout_is_tool_unavailable(self):
        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 1)

        code, _, _ = self.go(["threads", "list", REPO, "9"], slow)
        self.assertEqual(code, 11)

    def test_call_is_bounded_and_has_no_shell_and_no_stdin(self):
        fake = FakeGh()
        self.go(["threads", "list", REPO, "9"], fake)
        argv, kwargs = fake.calls[0]
        self.assertEqual(argv[0], GH)
        self.assertIsInstance(argv, list)
        self.assertFalse(kwargs.get("shell", False))
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], threads.CALL_TIMEOUT)
        self.assertIs(kwargs["check"], False)

    def test_usage_errors(self):
        for argv in (["threads", "list", "noslash", "9"], ["threads", "list", REPO, "0"],
                     ["threads", "list", REPO, "-3"], ["threads", "list", "../x/y", "9"],
                     ["threads", "list", "a/b;rm", "9"], ["threads", "list", REPO],
                     ["threads", "list", REPO, "x"], ["threads", "apply", REPO, "9"]):
            with self.subTest(argv=argv):
                fake = FakeGh()
                code, out, err = self.go(argv, fake)
                self.assertEqual(code, 2)
                self.assertEqual(fake.calls, [])
                self.assertNotIn("noslash", out + err)


class HostTests(Case):
    def conf(self, extra=""):
        path = self.dir / "config.toml"
        text = (
            "schema_version = 1\n[project]\nname = \"p\"\n[tracker]\nrepository = \"o/r\"\n" + extra +
            "[review]\ndestination = \"reviewer\"\n[storage]\nroot = \".shit\"\n"
            "[log]\nfile = \".shit/events.jsonl\"\n[local_model]\ncontext_tokens = 32768\n"
        )
        path.write_text(text, encoding="utf-8")
        return "config.toml"

    def test_no_config_passes_no_hostname(self):
        fake = FakeGh()
        self.go(["threads", "list", REPO, "9"], fake)
        self.assertNotIn("--hostname", fake.calls[0][0])

    def test_host_comes_from_configuration(self):
        fake = FakeGh()
        code, _, _ = self.go(["threads", "--config", self.conf('host = "git.example.org"\n'), "list", REPO, "9"],
                             fake)
        self.assertEqual(code, 0)
        argv = fake.calls[0][0]
        self.assertEqual(argv[argv.index("--hostname") + 1], "git.example.org")

    def test_config_without_host_passes_no_hostname(self):
        fake = FakeGh()
        self.go(["threads", "--config", self.conf(), "list", REPO, "9"], fake)
        self.assertNotIn("--hostname", fake.calls[0][0])

    def test_bad_host_in_config_is_rejected_by_the_config_contract(self):
        for bad in ("a b", "-x.example", "x/../y", "host:99999999", "", "<host>", "ex ample.org"):
            with self.subTest(bad=bad):
                text = (self.dir / self.conf()).read_text().replace("[review]", f'host = "{bad}"\n[review]')
                with self.assertRaises(Exception) as ctx:
                    config.parse(text)
                self.assertEqual(getattr(ctx.exception, "code", None), 3)

    def test_unreadable_config_is_refused_before_any_call(self):
        fake = FakeGh()
        (self.dir / "bad.toml").write_text("schema_version = ", encoding="utf-8")
        code, _, _ = self.go(["threads", "--config", "bad.toml", "list", REPO, "9"], fake)
        self.assertEqual(code, 3)
        self.assertEqual(fake.calls, [])
        code, _, _ = self.go(["threads", "--config", "missing.toml", "list", REPO, "9"], fake)
        self.assertEqual(code, 9)
        self.assertEqual(fake.calls, [])

    def test_host_applies_to_writes_too(self):
        fake = FakeGh([page([node("T1", 101)])])
        name = self.file([{"comment_id": 101, "action": "reply_resolve", "body": "done"}])
        code, _, _ = self.go(["threads", "--config", self.conf('host = "git.example.org"\n'), "apply", REPO, "9",
                              name], fake)
        self.assertEqual(code, 0)
        for argv, _ in fake.calls:
            self.assertIn("--hostname", argv)


class ApplyTests(Case):
    def apply(self, rows, fake=None, **kw):
        fake = fake or FakeGh([page([node("T1", 101), node("T2", 102, "b.py")])])
        name = self.file(rows, **kw)
        code, out, err = self.go(["threads", "apply", REPO, "9", name], fake)
        return code, out, err, fake

    def test_reply_resolve_and_skip_are_carried_out_in_order(self):
        rows = [
            {"comment_id": 101, "action": "reply_resolve", "body": "Fixed."},
            {"comment_id": 102, "action": "skip"},
        ]
        code, out, err, fake = self.apply(rows)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), ["replied 101", "resolved 101", "skip 102"])
        writes = fake.writes()
        self.assertEqual(len(writes), 2)
        self.assertIn(f"repos/{REPO}/pulls/9/comments/101/replies", writes[0][0])
        self.assertIn("body=Fixed.", writes[0][0])
        self.assertIn("id=T1", writes[1][0])

    def test_reply_only_does_not_resolve(self):
        code, out, _, fake = self.apply([{"comment_id": 101, "action": "reply", "body": "Kept on purpose."}])
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.writes()), 1)
        self.assertNotIn("resolved", out)

    def test_body_cannot_be_read_as_an_option(self):
        _, _, _, fake = self.apply([{"comment_id": 101, "action": "reply", "body": "-X DELETE"}])
        argv = fake.writes()[0][0]
        self.assertEqual(argv[argv.index("-f") + 1], "body=-X DELETE")

    def test_every_bad_file_writes_nothing(self):
        good = {"comment_id": 101, "action": "reply", "body": "ok"}
        cases = {
            "not a list": {"comment_id": 101},
            "empty list": [],
            "not an object": [good, 5],
            "extra key": [good, {"comment_id": 102, "action": "skip", "x": 1}],
            "missing action": [good, {"comment_id": 102}],
            "bool id": [good, {"comment_id": True, "action": "skip"}],
            "zero id": [good, {"comment_id": 0, "action": "skip"}],
            "string id": [good, {"comment_id": "102", "action": "skip"}],
            "unknown action": [good, {"comment_id": 102, "action": "close"}],
            "empty body": [good, {"comment_id": 102, "action": "reply", "body": "  "}],
            "missing body": [good, {"comment_id": 102, "action": "reply"}],
            "body not text": [good, {"comment_id": 102, "action": "reply", "body": 5}],
            "skip with body": [good, {"comment_id": 102, "action": "skip", "body": "x"}],
            "nul body": [good, {"comment_id": 102, "action": "reply", "body": "a\x00b"}],
            "long body": [good, {"comment_id": 102, "action": "reply", "body": "x" * (threads.MAX_BODY + 1)}],
            "repeat id": [good, {"comment_id": 101, "action": "skip"}],
            "unknown thread": [good, {"comment_id": 999, "action": "skip"}],
            "too many": [{"comment_id": n, "action": "skip"} for n in range(1, threads.MAX_ITEMS + 2)],
        }
        for label, rows in cases.items():
            with self.subTest(label):
                code, out, _, fake = self.apply(rows)
                self.assertIn(code, (3, 9), label)
                self.assertEqual(fake.writes(), [], label)
                self.assertEqual(out, "")

    def test_unknown_thread_is_precondition_missing(self):
        code, _, _, fake = self.apply([{"comment_id": 999, "action": "skip"}])
        self.assertEqual(code, 9)
        self.assertEqual(fake.writes(), [])

    def test_resolved_or_outdated_thread_is_not_a_target(self):
        fake = FakeGh([page([node("T1", 101, resolved=True), node("T2", 102, outdated=True)])])
        code, _, _, fake = self.apply([{"comment_id": 101, "action": "reply", "body": "x"}], fake)
        self.assertEqual(code, 9)
        self.assertEqual(fake.writes(), [])

    def test_lone_surrogate_in_body_is_refused(self):
        raw = '[{"comment_id": 101, "action": "reply", "body": "a\\ud800b"}]'
        code, _, _, fake = self.apply(raw)
        self.assertEqual(code, 3)
        self.assertEqual(fake.writes(), [])

    def test_hostile_json_is_refused(self):
        for raw in ('[{"comment_id": 101, "comment_id": 101, "action": "skip"}]', "[NaN]", "﻿[]",
                    "[" * 20 + "]" * 20, "not json", "\xff"):
            with self.subTest(raw=raw[:20]):
                code, _, _, fake = self.apply(raw)
                self.assertIn(code, (3, 7))
                self.assertEqual(fake.writes(), [])

    def test_oversize_file_is_input_limit(self):
        big = json.dumps([{"comment_id": 101, "action": "reply", "body": "x" * 100}] * 2) + " " * threads.FILE_LIMIT
        code, _, _, fake = self.apply(big)
        self.assertEqual(code, 7)
        self.assertEqual(fake.calls, [])

    def test_file_must_be_a_regular_file_below_the_base(self):
        fake = FakeGh()
        outside = self.dir.parent / "elsewhere.json"
        for arg in ("missing.json", "../elsewhere.json", str(outside), "sub"):
            with self.subTest(arg=arg):
                (self.dir / "sub").mkdir(exist_ok=True)
                code, _, _ = self.go(["threads", "apply", REPO, "9", arg], fake)
                self.assertIn(code, (5, 9))
                self.assertEqual(fake.calls, [])
        real = self.dir / "real.json"
        real.write_text("[]", encoding="utf-8")
        (self.dir / "link.json").symlink_to(real)
        code, _, _ = self.go(["threads", "apply", REPO, "9", "link.json"], fake)
        self.assertEqual(code, 5)
        self.assertEqual(fake.calls, [])

    def test_first_failed_reply_stops_and_is_not_retried(self):
        rows = [{"comment_id": 101, "action": "reply", "body": "a"}, {"comment_id": 102, "action": "reply", "body": "b"}]
        code, out, err, fake = self.apply(rows, FakeGh([page([node("T1", 101), node("T2", 102)])], reply_rc=1))
        self.assertEqual(code, 8)
        self.assertEqual(len(fake.writes()), 1)
        self.assertIn("0 of 2 done", out)
        self.assertNotIn("stderr-secret-text", out + err)

    def test_failed_second_reply_keeps_first_and_reports_count(self):
        class Flaky(FakeGh):
            def __call__(self, argv, **kw):
                if "-X POST" in " ".join(argv) and len([c for c in self.calls if "-X POST" in " ".join(c[0])]) == 1:
                    self.calls.append((list(argv), kw))
                    return subprocess.CompletedProcess(argv, 1, "", "")
                return super().__call__(argv, **kw)

        rows = [{"comment_id": 101, "action": "reply", "body": "a"}, {"comment_id": 102, "action": "reply", "body": "b"}]
        code, out, _, _ = self.apply(rows, Flaky([page([node("T1", 101), node("T2", 102)])]))
        self.assertEqual(code, 8)
        self.assertIn("replied 101", out)
        self.assertIn("1 of 2 done", out)

    def test_resolve_must_report_resolved(self):
        for answer in ({"data": {"resolveReviewThread": {"thread": {"isResolved": False}}}},
                       {"errors": [{"message": "x"}], "data": {"resolveReviewThread": {"thread": {"isResolved": True}}}},
                       {"data": None}, {}, {"data": {"resolveReviewThread": {"thread": {"isResolved": "true"}}}}):
            with self.subTest(answer=str(answer)[:50]):
                rows = [{"comment_id": 101, "action": "reply_resolve", "body": "a"},
                        {"comment_id": 102, "action": "reply", "body": "b"}]
                code, out, _, fake = self.apply(rows, FakeGh([page([node("T1", 101), node("T2", 102)])],
                                                             resolve=answer))
                self.assertEqual(code, 8)
                self.assertIn("replied 101", out)
                self.assertEqual(len(fake.writes()), 2)  # reply + resolve attempt; the second row never ran

    def test_resolve_is_only_after_an_accepted_reply(self):
        _, _, _, fake = self.apply([{"comment_id": 101, "action": "reply_resolve", "body": "a"}],
                                   FakeGh([page([node("T1", 101)])], reply_rc=1))
        self.assertEqual(len(fake.writes()), 1)
        self.assertNotIn("resolveReviewThread", " ".join(fake.writes()[0][0]))

    def test_no_token_is_read_or_passed(self):
        _, _, _, fake = self.apply([{"comment_id": 101, "action": "skip"}])
        for argv, _kw in fake.calls:
            self.assertFalse(any("token" in a.lower() for a in argv))


class ParseValueTests(unittest.TestCase):
    """records.parse_value: strict JSON of any top-level type, used for the dispositions file and gh answers."""

    def test_accepts_list_and_respects_depth_and_size(self):
        self.assertEqual(records.parse_value(b'[{"a": 1}]', limit=100, max_depth=2), [{"a": 1}])
        with self.assertRaises(Exception) as ctx:
            records.parse_value(b'[[[1]]]', limit=100, max_depth=2)
        self.assertEqual(ctx.exception.code, 7)
        with self.assertRaises(Exception) as ctx:
            records.parse_value(b'[1, 2, 3]', limit=3, max_depth=2)
        self.assertEqual(ctx.exception.code, 7)

    def test_refuses_duplicates_constants_bom_and_surrogates(self):
        for raw in (b'{"a": 1, "a": 2}', b"[NaN]", b"[Infinity]", b"\xef\xbb\xbf[]", b'["\\ud800"]', b"\xff"):
            with self.subTest(raw=raw):
                with self.assertRaises(Exception) as ctx:
                    records.parse_value(raw, limit=100, max_depth=4)
                self.assertEqual(ctx.exception.code, 3)

    def test_record_parse_still_requires_an_object(self):
        with self.assertRaises(Exception) as ctx:
            records.parse(b"[]")
        self.assertEqual(ctx.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
