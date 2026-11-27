"""The lifecycle module must match the table in docs/lifecycle.md for every ordered pair."""

import re
import unittest
from pathlib import Path

from shit import lifecycle
from shit.errors import ShitError

DOC = Path(__file__).resolve().parent.parent / "docs" / "lifecycle.md"
ROW = re.compile(r"^\| (\w+) \| (\w+) \| (legal|illegal) \| ([^|]+)\| [^|]+\|$", re.MULTILINE)


class LifecycleMatchesDoc(unittest.TestCase):
    def test_every_pair_matches_the_table(self):
        rows = ROW.findall(DOC.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 49)
        for frm, to, verdict, who in rows:
            allowed = set() if verdict == "illegal" else {w.strip() for w in who.split(" or ")}
            for actor in ("human", "wrapper", "model"):
                try:
                    lifecycle.check_transition(frm, to, actor)
                    ok = True
                except ShitError as exc:
                    self.assertEqual(exc.code, 4)
                    ok = False
                self.assertEqual(ok, actor in allowed, f"{frm}->{to} by {actor}")

    def test_only_human_can_approve(self):
        for actor in ("wrapper", "model"):
            with self.assertRaises(ShitError):
                lifecycle.check_transition("review", "approved", actor)
        lifecycle.check_transition("review", "approved", "human")

    def test_unknown_state_or_actor_rejected(self):
        for args in (("open", "weird", "human"), ("weird", "open", "human"), ("failed", "open", "root")):
            with self.assertRaises(ShitError):
                lifecycle.check_transition(*args)


if __name__ == "__main__":
    unittest.main()
