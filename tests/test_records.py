"""Record validation tests (issue #9) against docs/schemas.md and the design fixtures."""

import json
import unittest
from pathlib import Path

from shit import records
from shit.errors import ShitError

FIX = Path(__file__).resolve().parent.parent / "fixtures"


def task(**over):
    base = json.loads((FIX / "valid" / "task.json").read_text(encoding="utf-8"))
    base.update(over)
    return base


def enc(obj):
    return json.dumps(obj).encode("utf-8", "surrogatepass")


class Records(unittest.TestCase):
    def code(self, fn, *a):
        with self.assertRaises(ShitError) as cm:
            fn(*a)
        return cm.exception.code

    def test_valid_fixtures_accepted(self):
        records.load_task((FIX / "valid" / "task.json").read_bytes())
        lines = (FIX / "valid" / "event.jsonl").read_bytes().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            records.load_event(line)

    def test_invalid_fixtures_use_documented_codes(self):
        for name, code in (("duplicate-key", 3), ("unknown-field", 3), ("unknown-version", 3), ("bad-transition", 4)):
            with self.subTest(name):
                self.assertEqual(self.code(records.load_task, (FIX / "invalid" / f"{name}.json").read_bytes()), code)

    def test_bom_rejected(self):
        self.assertEqual(self.code(records.load_task, b"\xef\xbb\xbf" + enc(task())), 3)

    def test_bad_utf8_and_malformed_json_rejected(self):
        self.assertEqual(self.code(records.load_task, b"\xff\xfe{}"), 3)
        self.assertEqual(self.code(records.load_task, b"{not json"), 3)
        self.assertEqual(self.code(records.load_task, b"[]"), 3)
        self.assertEqual(self.code(records.load_task, b'{"a": NaN}'), 3)

    def test_size_limit_boundary(self):
        exact = enc(task(summary="x"))
        pad = b" " * (65536 - len(exact))
        records.load_task(exact + pad)
        self.assertEqual(self.code(records.load_task, exact + pad + b" "), 7)

    def test_depth_limit(self):
        self.assertEqual(records.scan_depth(b'{"a":' * 8 + b"1" + b"}" * 8), 8)
        self.assertEqual(self.code(records.load_task, b'{"a":' * 9 + b"1" + b"}" * 9), 7)
        self.assertEqual(self.code(records.load_task, b"[" * 30000 + b"]" * 30000), 7)

    def test_depth_scan_ignores_brackets_in_strings(self):
        self.assertEqual(records.scan_depth(b'{"a": "[[[[[[[[[[[[\\"[[[["}'), 1)

    def test_version_must_be_integer_one(self):
        for v in (2, True, 1.0, "1", None):
            with self.subTest(v=v):
                self.assertEqual(self.code(records.load_task, enc(task(schema_version=v))), 3)

    def test_field_validation(self):
        bad = [
            {"id": "Bad_ID"}, {"id": "-x"}, {"id": "a" * 65}, {"id": "a\n"}, {"id": "١"},
            {"title": ""}, {"title": "t" * 201}, {"summary": ""}, {"summary": "s" * 4001},
            {"state": "done"}, {"owner": "root"}, {"created_at": "2026-11-26T15:30:00+00:00"},
            {"created_at": "2026-11-26T15:30:00.5Z"}, {"created_at": "2026-13-26T15:30:00Z"},
            {"updated_at": "2026-11-26T15:29:59Z"}, {"history": "x"}, {"history": 5}, {"history": {"a": 1}}, {"history": [5]}, {"title": "a\ud800"},
            {"blocked_reason": "why"}, {"extra": 1}, {"schema": "brief"},
        ]
        for over in bad:
            with self.subTest(over=over):
                self.assertEqual(self.code(records.load_task, enc(task(**over))), 3)

    def test_blocked_requires_both_fields(self):
        base = task(state="blocked", history=[{"from": "open", "to": "blocked", "actor": "human",
                                              "at": "2026-11-26T15:30:00Z", "reason": "r"}])
        self.assertEqual(self.code(records.load_task, enc(base)), 3)
        base["blocked_reason"] = "waiting"
        self.assertEqual(self.code(records.load_task, enc(base)), 3)
        base["blocking_item"] = "issue-1"
        records.load_task(enc(base))

    def test_history_checks(self):
        step = {"from": "open", "to": "running", "actor": "wrapper", "at": "2026-11-26T15:30:00Z", "reason": "go"}
        records.load_task(enc(task(state="running", history=[step])))
        self.assertEqual(self.code(records.load_task, enc(task(state="open", history=[step]))), 3)
        appr = {"from": "review", "to": "approved", "actor": "model", "at": "2026-11-26T15:30:00Z", "reason": "r"}
        self.assertEqual(self.code(records.load_task, enc(task(state="approved", history=[appr]))), 4)
        two = [step, {"from": "review", "to": "approved", "actor": "human", "at": "2026-11-26T15:31:00Z", "reason": "r"}]
        self.assertEqual(self.code(records.load_task, enc(task(state="approved", history=two))), 3)
        back = [step, {"from": "running", "to": "review", "actor": "wrapper", "at": "2026-11-26T15:29:00Z", "reason": "r"}]
        self.assertEqual(self.code(records.load_task, enc(task(state="review", history=back))), 3)

    def test_event_rules(self):
        ev = json.loads((FIX / "valid" / "event.jsonl").read_text(encoding="utf-8").splitlines()[0])
        cases = [
            {"event": "state_transition"},
            {"from_state": "open", "to_state": "running"},
            {"event": "command_failed"},
            {"event": "command_failed", "exit_code": 0},
            {"exit_code": 3},
            {"event": "nope"}, {"message": "m" * 501}, {"timestamp": "yesterday"}, {"zzz": 1},
        ]
        for over in cases:
            with self.subTest(over=over):
                self.assertEqual(self.code(records.load_event, enc({**ev, **over})), 3)
        good = {**ev, "event": "state_transition", "from_state": "open", "to_state": "running", "actor": "wrapper"}
        records.load_event(enc(good))
        bad = {**good, "from_state": "closed", "to_state": "open"}
        self.assertEqual(self.code(records.load_event, enc(bad)), 4)
        failed = {**ev, "event": "command_failed", "exit_code": 3}
        records.load_event(enc(failed))
        self.assertEqual(self.code(records.load_event, enc({**failed, "exit_code": 99})), 3)


if __name__ == "__main__":
    unittest.main()
