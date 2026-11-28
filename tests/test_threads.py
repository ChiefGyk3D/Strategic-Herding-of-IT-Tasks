"""Tests for the review-thread plan builder (issue 55): `shit threads list|apply` prints a request plan as data.

Run: python3 -m unittest discover -s tests -v

shit/threads.py never starts a process (H-29d in test_hostile.py); the launcher that does is tested in
test_threads_run.py. Each refusal here has an accepted twin, and "nothing is printed on refusal" is checked.
Exit codes are the shared table in docs/exit-codes.md.
"""

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # puts the repository root on sys.path, and names it as ROOT
from _helpers import run_main

from shit import config, records, threads
from shit.errors import ShitError

REPO = "example-owner/example-repo"


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


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name).resolve()

    def file(self, rows, name="disp.json"):
        (self.dir / name).write_text(rows if isinstance(rows, str) else json.dumps(rows), encoding="utf-8")
        return name

    def conf(self, extra="", name="config.toml"):
        text = (
            "schema_version = 1\n[project]\nname = \"p\"\n[tracker]\nrepository = \"o/r\"\n" + extra +
            "[review]\ndestination = \"reviewer\"\n[storage]\nroot = \".shit\"\n"
            "[log]\nfile = \".shit/events.jsonl\"\n[local_model]\ncontext_tokens = 32768\n"
        )
        (self.dir / name).write_text(text, encoding="utf-8")
        return name

    def go(self, argv):
        return run_main(lambda a: threads.main(a, cwd=self.dir), ["threads", *argv])

    def plan(self, argv):
        code, out, err = self.go(argv)
        self.assertEqual((code, err), (0, ""), argv)
        self.assertEqual(len(out.splitlines()), 1)
        return json.loads(out)


class ListPlanTests(Case):
    def test_list_plan_is_one_read_request(self):
        plan = self.plan(["list", REPO, "9"])
        self.assertEqual(plan["kind"], "list")
        self.assertEqual((plan["repo"], plan["pr"], plan["host"]), (REPO, 9, None))
        (request,) = plan["requests"]
        self.assertEqual(request["op"], "read_threads")
        self.assertEqual(request["fields"], {"owner": "example-owner", "name": "example-repo", "number": 9})
        self.assertEqual(request["query"], threads.QUERY)

    def test_usage_errors_print_nothing_and_name_nothing(self):
        for argv in (["list", "noslash", "9"], ["list", REPO, "0"], ["list", REPO, "-3"], ["list", "../x/y", "9"],
                     ["list", "a/b;rm", "9"], ["list", REPO], ["list", REPO, "x"], ["apply", REPO, "9"], ["bogus"]):
            with self.subTest(argv=argv):
                code, out, err = self.go(argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertNotIn("noslash", err)

    def test_no_token_or_credential_is_in_a_plan(self):
        text = self.go(["list", REPO, "9"])[1].lower()
        for word in ("token", "authorization", "password", "secret"):
            self.assertNotIn(word, text)


class HostTests(Case):
    def test_host_comes_from_configuration(self):
        plan = self.plan(["--config", self.conf('host = "git.example.org"\n'), "list", REPO, "9"])
        self.assertEqual(plan["host"], "git.example.org")

    def test_config_without_host_has_no_host(self):
        self.assertIsNone(self.plan(["--config", self.conf(), "list", REPO, "9"])["host"])

    def test_no_config_has_no_host(self):
        self.assertIsNone(self.plan(["list", REPO, "9"])["host"])

    def test_bad_host_is_rejected_by_the_config_contract(self):
        for bad in ("a b", "-x.example", "x/../y", "host:99999999", "", "<host>", "ex ample.org", "http://x.example"):
            with self.subTest(bad=bad):
                text = (self.dir / self.conf()).read_text().replace("[review]", f'host = "{bad}"\n[review]')
                with self.assertRaises(Exception) as ctx:
                    config.parse(text)
                self.assertEqual(getattr(ctx.exception, "code", None), 3)

    def test_unreadable_config_is_refused(self):
        (self.dir / "bad.toml").write_text("schema_version = ", encoding="utf-8")
        for name, want in (("bad.toml", 3), ("missing.toml", 9), ("../x.toml", 5)):
            with self.subTest(name=name):
                code, out, _ = self.go(["--config", name, "list", REPO, "9"])
                self.assertEqual((code, out), (want, ""))

    def test_host_is_in_every_request_argument_list(self):
        name = self.file([{"comment_id": 101, "action": "reply_resolve", "body": "done"}])
        plan = self.plan(["--config", self.conf('host = "git.example.org"\n'), "apply", REPO, "9", name])
        for request in plan["requests"]:
            args = threads.request_args(plan, request, thread_id="T1")
            self.assertEqual(args[args.index("--hostname") + 1], "git.example.org")


class ApplyPlanTests(Case):
    def apply(self, rows, **kw):
        return self.go(["apply", REPO, "9", self.file(rows, **kw)])

    def test_replies_come_before_resolves_and_skips_are_dropped(self):
        rows = [
            {"comment_id": 101, "action": "reply_resolve", "body": "Fixed."},
            {"comment_id": 102, "action": "skip"},
            {"comment_id": 103, "action": "reply", "body": "Kept on purpose."},
            {"comment_id": 104, "action": "reply_resolve", "body": "Done."},
        ]
        plan = self.plan(["apply", REPO, "9", self.file(rows)])
        ops = [(r["op"], r.get("comment_id")) for r in plan["requests"]]
        self.assertEqual(ops, [("read_threads", None), ("reply", 101), ("reply", 103), ("reply", 104),
                               ("resolve", 101), ("resolve", 104)])
        reply = plan["requests"][1]
        self.assertEqual(reply["endpoint"], f"repos/{REPO}/pulls/9/comments/101/replies")
        self.assertEqual(reply["method"], "POST")
        self.assertEqual(reply["fields"], {"body": "Fixed."})
        self.assertEqual(plan["requests"][4]["query"], threads.MUTATION)

    def test_all_skip_plan_has_only_the_read(self):
        plan = self.plan(["apply", REPO, "9", self.file([{"comment_id": 5, "action": "skip"}])])
        self.assertEqual([r["op"] for r in plan["requests"]], ["read_threads"])

    def test_every_bad_file_prints_nothing(self):
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
            "tab body": [good, {"comment_id": 102, "action": "reply", "body": "a\tb"}],
            "bidi body": [good, {"comment_id": 102, "action": "reply", "body": "a" + chr(0x202E) + "b"}],
            "long body": [good, {"comment_id": 102, "action": "reply", "body": "x" * (threads.MAX_BODY + 1)}],
            "repeat id": [good, {"comment_id": 101, "action": "skip"}],
            "too many": [{"comment_id": n, "action": "skip"} for n in range(1, threads.MAX_ITEMS + 2)],
        }
        for label, rows in cases.items():
            with self.subTest(label):
                code, out, _ = self.apply(rows)
                self.assertEqual((code, out), (3, ""), label)

    def test_a_body_at_the_limit_and_a_newline_are_accepted(self):
        code, _, _ = self.apply([{"comment_id": 1, "action": "reply", "body": "a\n" + "x" * (threads.MAX_BODY - 2)}])
        self.assertEqual(code, 0)

    def test_lone_surrogate_in_body_is_refused(self):
        code, out, _ = self.apply('[{"comment_id": 101, "action": "reply", "body": "a\\ud800b"}]')
        self.assertEqual((code, out), (3, ""))

    def test_hostile_json_is_refused(self):
        for raw in ('[{"comment_id": 101, "comment_id": 101, "action": "skip"}]', "[NaN]", "﻿[]",
                    "[" * 20 + "]" * 20, "not json", "\xff"):
            with self.subTest(raw=raw[:20]):
                code, out, _ = self.apply(raw)
                self.assertIn(code, (3, 7))
                self.assertEqual(out, "")

    def test_oversize_file_is_input_limit(self):
        big = json.dumps([{"comment_id": 1, "action": "skip"}]) + " " * threads.FILE_LIMIT
        self.assertEqual(self.apply(big)[0], 7)

    def test_file_must_be_a_regular_file_below_the_base(self):
        (self.dir / "sub").mkdir()
        real = self.dir / "real.json"
        real.write_text("[]", encoding="utf-8")
        (self.dir / "link.json").symlink_to(real)
        for arg, want in (("missing.json", 9), ("../elsewhere.json", 5), ("/etc/hostname", 5), ("sub", 5),
                          ("link.json", 5)):
            with self.subTest(arg=arg):
                code, out, _ = self.go(["apply", REPO, "9", arg])
                self.assertEqual((code, out), (want, ""))


class PageTests(unittest.TestCase):
    def test_unresolved_current_threads_only(self):
        found, more, cursor = threads.parse_page(page([
            node("T1", 101), node("T2", 102, resolved=True), node("T3", 103, outdated=True),
        ], more=True, cursor="next"))
        self.assertEqual([t["comment_id"] for t in found], [101])
        self.assertEqual((more, cursor), (True, "next"))

    def test_bad_answer_shape_fails_closed(self):
        for data in ({"data": {}}, {"errors": [{"message": "x"}]}, page([node(comment_id=True)]),
                     page([node(comment_id=0)]), page([node(thread_id="bad id!")]),
                     page([], more="yes"), page([], more=True, cursor=""), [], "x"):
            with self.subTest(data=str(data)[:40]):
                with self.assertRaises(Exception) as ctx:
                    threads.parse_page(data)
                self.assertEqual(ctx.exception.code, 8)

    def test_read_answer_is_strict(self):
        self.assertEqual(threads.read_answer('{"a": 1}'), {"a": 1})
        for raw in ("not json", '{"a": 1, "a": 2}', "", "[NaN]"):
            with self.subTest(raw=raw):
                with self.assertRaises(Exception) as ctx:
                    threads.read_answer(raw)
                self.assertEqual(ctx.exception.code, 8)

    def refused_page(self, data):
        with self.assertRaises(Exception) as ctx:
            threads.parse_page(data)
        self.assertEqual(ctx.exception.code, 8)

    def test_a_cursor_must_be_text_or_null(self):
        # review thread 5: str() used to turn a number or an object into a valid-looking cursor
        for bad in (123, 1.5, ["abc"], {"a": 1}, True):
            with self.subTest(cursor=bad):
                self.refused_page(page([node()], more=True, cursor=bad))
                self.refused_page(page([node()], more=False, cursor=bad))
        found, more, cursor = threads.parse_page(page([node()], more=False, cursor=None))
        self.assertEqual((len(found), more, cursor), (1, False, ""))
        self.assertEqual(threads.parse_page(page([node()], more=True, cursor="abc"))[2], "abc")
        self.refused_page(page([node()], more=True, cursor=None))  # more pages but no cursor: still refused

    def test_every_field_must_have_its_declared_type(self):
        # review thread 6: isResolved 0 and isOutdated 0 used to pass as a valid unresolved thread
        def changed(**fields):
            item = node()
            for key, value in fields.items():
                if key in ("databaseId", "author", "body"):
                    item["comments"]["nodes"][0][key] = value
                else:
                    item[key] = value
            return page([item])

        for label, data in {
            "isResolved 0": changed(isResolved=0), "isResolved 1": changed(isResolved=1),
            "isResolved null": changed(isResolved=None), "isResolved text": changed(isResolved="false"),
            "isOutdated 0": changed(isOutdated=0), "isOutdated null": changed(isOutdated=None),
            "isOutdated text": changed(isOutdated="no"),
            "resolved thread with a bad outdated flag": changed(isResolved=True, isOutdated=0),
            "id number": changed(id=5), "id null": changed(id=None), "path number": changed(path=5),
            "path null": changed(path=None), "line text": changed(line="3"), "line bool": changed(line=True),
            "line float": changed(line=3.5), "author text": changed(author="rev"), "author list": changed(author=[]),
            "login number": changed(author={"login": 5}), "body number": changed(body=5), "body null": changed(body=None),
            "databaseId text": changed(databaseId="101"), "databaseId float": changed(databaseId=101.0),
            "nodes not a list": {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": {"a": 1}}}}}},
            "comments not a list": changed(comments={"nodes": {"a": 1}}),
            "data not a dict": [],
        }.items():
            with self.subTest(label):
                self.refused_page(data)
        # the accepted twins: real booleans, a null line, a null author
        ok = changed(line=None, author=None)
        found = threads.parse_page(ok)[0]
        self.assertEqual((found[0]["line"], found[0]["author"]), ("?", "?"))
        self.assertEqual(threads.parse_page(page([node(resolved=True), node(outdated=True)]))[0], [])

    def test_check_targets(self):
        found = threads.parse_page(page([node("T1", 101)]))[0]
        threads.check_targets(found, [101])
        with self.assertRaises(Exception) as ctx:
            threads.check_targets(found, [101, 999])
        self.assertEqual(ctx.exception.code, 9)

    def test_list_lines_are_grouped_one_line_cut_and_escaped(self):
        body = "line one\nline two\x1b[31m red " + chr(0x202E) + chr(0x200B) + "x" * 400
        found = threads.parse_page(page([node("T1", 1, "b.py", 7, body), node("T2", 2, "a.py", 3, "first")]))[0]
        lines = threads.format_list(found, REPO, 9)
        self.assertEqual(lines[0], "file: a.py")
        self.assertEqual(lines[2], "file: b.py")
        self.assertEqual(lines[-1], f"2 unresolved thread(s) on {REPO}#9")
        text = "\n".join(lines)
        for bad in ("\x1b", chr(0x202E), chr(0x200B)):
            self.assertNotIn(bad, text)
        self.assertEqual(len(lines), 5)
        self.assertLessEqual(len(lines[3]), 400)

    def test_a_hostile_path_cannot_start_a_workflow_command(self):
        found = threads.parse_page(page([node("T1", 1, "::add-mask::SECRET", 7, "x")]))[0]
        lines = threads.format_list(found, REPO, 9)
        self.assertEqual(lines[0], "file: ::add-mask::SECRET")
        self.assertFalse(any(line.startswith("::") for line in lines))

    def test_check_unique(self):
        a, b = threads.parse_page(page([node("A", 7), node("B", 8)]))[0]
        threads.check_unique([a, b])
        for pair in ([a, {**b, "comment_id": 7}], [a, {**b, "thread_id": "A"}]):
            with self.assertRaises(Exception) as ctx:
                threads.check_unique(pair)
            self.assertEqual(ctx.exception.code, 8)

    OUTPUT_SENTENCE = ("Every line that carries text from the API (a path, an author or a comment body) starts with "
                       "`file: ` or with two spaces; the summary line and the empty-list line hold only the "
                       "repository, the pull request number and a count.")

    def test_the_documented_output_rule_matches_the_real_output(self):
        docs = (_bootstrap.ROOT / "docs" / "threads.md").read_text()
        self.assertIn(self.OUTPUT_SENTENCE, docs)
        hostile = ["::add-mask::x", "\x1b[31m", "a" + chr(0x202E), "line\nbreak", "::error::z"]
        nodes = [node(f"T{i}", i + 1, path=text, body=text) for i, text in enumerate(hostile)]
        found = threads.parse_page(page(nodes))[0]
        fixed_line = re.compile(rf"(\d+ unresolved thread\(s\)|no unresolved threads) on {re.escape(REPO)}#9")
        for lines in (threads.format_list(found, REPO, 9), threads.format_list([], REPO, 9)):
            for line in lines:
                self.assertTrue(line.startswith(("file: ", "  ")) or fixed_line.fullmatch(line), line)
                self.assertFalse(line.startswith("::"), line)
        self.assertIsNone(fixed_line.fullmatch("::add-mask::x"))  # the check bites on an unprefixed API line

    def test_empty_list_line(self):
        self.assertEqual(threads.format_list([], REPO, 9), [f"no unresolved threads on {REPO}#9"])


class PlanValidationTests(Case):
    def good(self):
        rows = [{"comment_id": 101, "action": "reply_resolve", "body": "Fixed."},
                {"comment_id": 102, "action": "reply", "body": "Kept."}]
        return self.plan(["apply", REPO, "9", self.file(rows)])

    def check(self, plan):
        return threads.parse_plan(json.dumps(plan).encode())

    def refused(self, plan, code=3):
        with self.assertRaises(Exception) as ctx:
            self.check(plan)
        self.assertEqual(ctx.exception.code, code)

    def test_a_printed_plan_is_accepted(self):
        plan = self.good()
        self.assertEqual(self.check(plan), plan)
        self.assertEqual(self.check(self.plan(["list", REPO, "9"]))["kind"], "list")

    def test_every_tamper_is_refused(self):
        def mut(fn):
            plan = copy.deepcopy(self.good())
            fn(plan)
            return plan

        tampers = {
            "extra top key": lambda p: p.update(extra=1),
            "missing key": lambda p: p.pop("host"),
            "schema": lambda p: p.update(schema="other"),
            "version": lambda p: p.update(schema_version=2),
            "kind": lambda p: p.update(kind="delete"),
            "list kind with replies": lambda p: p.update(kind="list"),
            "repo": lambda p: p.update(repo="a/b;rm"),
            "repo dots": lambda p: p.update(repo="../x"),
            "pr zero": lambda p: p.update(pr=0),
            "pr bool": lambda p: p.update(pr=True),
            "host bad": lambda p: p.update(host="a b"),
            "host not text": lambda p: p.update(host=5),
            "endpoint": lambda p: p["requests"][1].update(endpoint="repos/other/x/pulls/1/comments/101/replies"),
            "endpoint mismatch id": lambda p: p["requests"][1].update(comment_id=999),
            "method": lambda p: p["requests"][1].update(method="DELETE"),
            "query text": lambda p: p["requests"][0].update(query=threads.QUERY + " "),
            "mutation text": lambda p: p["requests"][3].update(query="mutation{deleteRepository}"),
            "read fields": lambda p: p["requests"][0]["fields"].update(owner="someone"),
            "extra request key": lambda p: p["requests"][1].update(extra=1),
            "reply extra field": lambda p: p["requests"][1]["fields"].update(label="x"),
            "unknown op": lambda p: p["requests"][1].update(op="delete"),
            "resolve without reply": lambda p: p["requests"].append(
                {"op": "resolve", "method": "POST", "endpoint": "graphql", "query": threads.MUTATION,
                 "comment_id": 555, "fields": {}}),
            "resolve before reply": lambda p: p["requests"].insert(1, p["requests"].pop(3)),
            "body tab": lambda p: p["requests"][1]["fields"].update(body="a\tb"),
            "empty body": lambda p: p["requests"][1]["fields"].update(body=" "),
            "no requests": lambda p: p.update(requests=[]),
            "requests not list": lambda p: p.update(requests="x"),
            "first not read": lambda p: p["requests"].pop(0),
        }
        for label, fn in tampers.items():
            with self.subTest(label):
                self.refused(mut(fn))

    def test_a_trusted_host_stands_for_a_null_host_and_nothing_else(self):
        null_plan = self.good()
        self.assertIsNone(null_plan["host"])
        trusted = "git.example.org"
        out = threads.parse_plan(json.dumps(null_plan).encode(), trusted_host=trusted)
        self.assertEqual(out, {**null_plan, "host": trusted})  # equals the plan built for the trusted host
        explicit = {**null_plan, "host": trusted}
        self.assertEqual(threads.parse_plan(json.dumps(explicit).encode(), trusted_host=trusted), explicit)
        upper = {**null_plan, "host": "Git.Example.org"}
        self.assertEqual(threads.parse_plan(json.dumps(upper).encode(), trusted_host=trusted)["host"], trusted)
        for other in ("other.example.org", "github.com", "git.example.org.evil.net"):
            with self.subTest(other=other), self.assertRaises(ShitError) as ctx:
                threads.parse_plan(json.dumps({**null_plan, "host": other}).encode(), trusted_host=trusted)
            self.assertEqual(ctx.exception.code, 3)
        # without a trusted host nothing changes: a null stays null and a host must still be a valid host name
        self.assertIsNone(threads.parse_plan(json.dumps(null_plan).encode())["host"])

    def test_tampering_is_still_refused_when_a_trusted_host_is_given(self):
        plan = copy.deepcopy(self.good())
        for fn in (lambda p: p["requests"][1].update(endpoint="repos/x/y/pulls/1/comments/101/replies"),
                   lambda p: p.update(extra=1), lambda p: p["requests"][0].update(query="q"),
                   lambda p: p["requests"].pop(0), lambda p: p.update(repo="a/b;c")):
            bad = copy.deepcopy(plan)
            fn(bad)
            with self.assertRaises(ShitError) as ctx:
                threads.parse_plan(json.dumps(bad).encode(), trusted_host="git.example.org")
            self.assertEqual(ctx.exception.code, 3)

    def test_hostile_json_and_size(self):
        for raw in (b"[]", b"not json", b'{"a":1,"a":2}', b"\xef\xbb\xbf{}", b"[" * 30 + b"]" * 30):
            with self.subTest(raw=raw[:12]):
                with self.assertRaises(Exception) as ctx:
                    threads.parse_plan(raw)
                self.assertIn(ctx.exception.code, (3, 7))
        with self.assertRaises(Exception) as ctx:
            threads.parse_plan(b" " * (threads.PLAN_LIMIT + 1))
        self.assertEqual(ctx.exception.code, 7)


class RequestArgsTests(Case):
    def test_body_is_a_field_value_never_an_option(self):
        plan = self.plan(["apply", REPO, "9", self.file([{"comment_id": 1, "action": "reply", "body": "-X DELETE"}])])
        args = threads.request_args(plan, plan["requests"][1])
        self.assertEqual(args[args.index("-f") + 1], "body=-X DELETE")
        self.assertEqual(args[:2], ["api", "-X"])

    def test_resolve_needs_a_clean_thread_id(self):
        plan = self.plan(["apply", REPO, "9", self.file([{"comment_id": 1, "action": "reply_resolve", "body": "x"}])])
        resolve = plan["requests"][2]
        self.assertIn("id=T1", threads.request_args(plan, resolve, thread_id="T1"))
        for bad in (None, "", "a b", "x;y", 5):
            with self.subTest(bad=bad), self.assertRaises(ShitError):
                threads.request_args(plan, resolve, thread_id=bad)

    def test_cursor_is_added_only_after_the_first_page(self):
        plan = self.plan(["list", REPO, "9"])
        self.assertNotIn("cursor=n1", threads.request_args(plan, plan["requests"][0]))
        self.assertIn("cursor=n1", threads.request_args(plan, plan["requests"][0], cursor="n1"))

    def test_resolved_ok_requires_true_and_no_errors(self):
        ok = {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}
        self.assertTrue(threads.resolved_ok(ok))
        for bad in ({"data": {"resolveReviewThread": {"thread": {"isResolved": False}}}},
                    {**ok, "errors": [{"message": "x"}]}, {"data": None}, {},
                    {"data": {"resolveReviewThread": {"thread": {"isResolved": "true"}}}}, []):
            with self.subTest(bad=str(bad)[:40]):
                self.assertFalse(threads.resolved_ok(bad))


class ParseValueTests(unittest.TestCase):
    """records.parse_value: strict JSON of any top-level type, used for the dispositions file and plans."""

    def test_accepts_list_and_respects_depth_and_size(self):
        self.assertEqual(records.parse_value(b'[{"a": 1}]', limit=100, max_depth=2), [{"a": 1}])
        with self.assertRaises(Exception) as ctx:
            records.parse_value(b"[[[1]]]", limit=100, max_depth=2)
        self.assertEqual(ctx.exception.code, 7)
        with self.assertRaises(Exception) as ctx:
            records.parse_value(b"[1, 2, 3]", limit=3, max_depth=2)
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
