"""Checks on the issue #8 design fixtures and tables. Standard library only.

Run: python3 -m unittest discover -s tests -v

These pin the documents to their stated shape: the transition table covers every ordered pair of
states, the exit-code table is complete, and each fixture behaves as its name says under a strict
loader that rejects duplicate keys.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATES = ["open", "running", "review", "approved", "closed", "blocked", "failed"]
ROW = re.compile(r"^\| (\w+) \| (\w+) \| (legal|illegal) \| [^|]+\| [^|]+\|$", re.MULTILINE)


def strict_load(text):
    """Load JSON and reject any duplicate key in any object."""

    def pairs(items):
        seen = {}
        for k, v in items:
            if k in seen:
                raise ValueError(f"duplicate key: {k}")
            seen[k] = v
        return seen

    return json.loads(text, object_pairs_hook=pairs)


class TransitionTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "docs" / "lifecycle.md").read_text(encoding="utf-8")
        cls.rows = {(a, b): v for a, b, v in ROW.findall(cls.text)}

    def test_every_ordered_pair_has_one_row(self):
        self.assertEqual(len(self.rows), len(STATES) ** 2)
        for a in STATES:
            for b in STATES:
                self.assertIn((a, b), self.rows, f"missing row {a} -> {b}")

    def test_closed_is_terminal(self):
        for b in STATES:
            self.assertEqual(self.rows[("closed", b)], "illegal", f"closed -> {b} must be illegal")

    def test_only_human_or_wrapper_enters_approved_legally(self):
        for a in STATES:
            if self.rows.get((a, "approved")) == "legal":
                self.assertEqual(a, "review", "approval is only legal from review in the table")

    def test_repeating_a_state_is_illegal(self):
        for s in STATES:
            self.assertEqual(self.rows[(s, s)], "illegal")


class ExitCodeTests(unittest.TestCase):
    def test_shared_codes_are_unique_and_contiguous(self):
        text = (ROOT / "docs" / "exit-codes.md").read_text(encoding="utf-8")
        codes = [int(m) for m in re.findall(r"^\| (\d+) \| \w+ \| `", text, re.MULTILINE)]
        self.assertEqual(codes, list(range(len(codes))))
        self.assertGreaterEqual(len(codes), 12)


class FixtureTests(unittest.TestCase):
    def load(self, rel):
        return (ROOT / "fixtures" / rel).read_text(encoding="utf-8")

    def test_valid_records_are_version_one(self):
        for name in ("task", "brief", "report", "decision"):
            data = strict_load(self.load(f"valid/{name}.json"))
            self.assertEqual(data["schema_version"], 1, name)
            self.assertEqual(data["schema"], name, name)

    def test_valid_event_lines_are_version_one(self):
        lines = [ln for ln in self.load("valid/event.jsonl").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        for ln in lines:
            self.assertEqual(strict_load(ln)["schema_version"], 1)

    def test_unknown_version_is_version_ninety_nine(self):
        self.assertEqual(strict_load(self.load("invalid/unknown-version.json"))["schema_version"], 99)

    def test_duplicate_key_is_rejected_by_strict_loader(self):
        with self.assertRaises(ValueError):
            strict_load(self.load("invalid/duplicate-key.json"))

    def test_duplicate_key_fixture_parses_with_plain_json(self):
        # A plain parser keeps the last value silently; this is the case the strict loader exists for.
        json.loads(self.load("invalid/duplicate-key.json"))

    def test_strict_loader_bites_on_clean_input(self):
        self.assertEqual(strict_load('{"a": 1, "b": 2}'), {"a": 1, "b": 2})


if __name__ == "__main__":
    unittest.main()
