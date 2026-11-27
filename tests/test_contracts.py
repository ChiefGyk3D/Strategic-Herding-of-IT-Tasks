"""Tests for the brief and report-header contracts (issue #10). Standard library only.

Run: python3 -m unittest discover -s tests -v

Each rule has a case that is accepted and a case that is refused, so a guard that stops biting shows up.
"""

import os
import re
import tempfile
import unittest
from pathlib import Path

import _bootstrap

from shit import brief, evidence
from shit.errors import ContractError

ROOT = _bootstrap.ROOT
TEMPLATES = ROOT / "templates"

GOOD_BRIEF = {
    "issue": "ChiefGyk3D/Strategic-Herding-of-IT-Tasks#10",
    "role": "implementer",
    "model": "example-model-1",
    "packs": ["review-protocol", "threat-model"],
    "objective": "Import the brief contract.",
    "scope": ["templates/brief.md", "shit/brief.py"],
    "constraints": ["Standard library only."],
    "acceptance": ["The validator rejects a brief with no issue line."],
    "data_class": "internal; no hostnames, serials, callsigns or secrets",
    "cleanup_duty": "Remove only the scratch folder created for this task.",
    "evidence": "Gate exit code, red evidence, revert check.",
    "review_destination": "reviewer role, report to the lead",
}

GOOD_HEADER = """ROLE: implementer   MODEL: example-model-1
HEAD: abc1234   BASE: 0123abc
GATE: python3 -m unittest discover -s tests  exit=0
OTHER GATES: ruff: pass
PACKS LOADED: none
TREE: clean (git status --porcelain is empty)
NOT VERIFIED: none
LOCAL-LLM: n/a - code and tests
"""

# Hostile characters, written as escapes so this file stays plain ASCII.
HOSTILE_CHARS = [
    chr(0x00),  # NUL
    chr(0x1B),  # escape
    chr(0x09),  # tab
    chr(0x0D),  # carriage return
    chr(0x200B),  # zero-width space
    chr(0x202E),  # right-to-left override
    chr(0xFEFF),  # byte order mark
    chr(0x2028),  # line separator
    chr(0xD800),  # lone surrogate
]


def without(d, key):
    return {k: v for k, v in d.items() if k != key}


def code_of(fn, *args):
    try:
        fn(*args)
    except ContractError as e:
        return e.code
    return None


class BriefRenderTests(unittest.TestCase):
    def test_render_then_parse_round_trips(self):
        text = brief.render(GOOD_BRIEF)
        parsed = brief.parse(text)
        self.assertEqual(parsed["issue"], GOOD_BRIEF["issue"])
        self.assertEqual(parsed["scope"], GOOD_BRIEF["scope"])
        self.assertEqual(parsed["packs"], GOOD_BRIEF["packs"])
        self.assertEqual(parsed["constraints"], GOOD_BRIEF["constraints"])

    def test_model_and_constraints_are_optional(self):
        d = without(without(GOOD_BRIEF, "model"), "constraints")
        parsed = brief.parse(brief.render(d))
        self.assertNotIn("model", parsed)
        self.assertEqual(parsed["constraints"], [])

    def test_empty_packs_render_as_none(self):
        text = brief.render({**GOOD_BRIEF, "packs": []})
        self.assertIn("\npacks: none\n", text)
        self.assertEqual(brief.parse(text)["packs"], [])


class BriefRequiredFieldTests(unittest.TestCase):
    def test_each_required_field_missing_is_rejected(self):
        for key in ("issue", "role", "packs", "objective", "scope", "acceptance", "data_class",
                    "cleanup_duty", "evidence", "review_destination"):
            with self.subTest(key=key):
                self.assertEqual(code_of(brief.render, without(GOOD_BRIEF, key)), 3)

    def test_missing_issue_line_in_text_is_rejected(self):
        text = re.sub(r"^issue: .*\n", "", brief.render(GOOD_BRIEF), flags=re.MULTILINE)
        self.assertEqual(code_of(brief.parse, text), 3)

    def test_missing_cleanup_line_in_text_is_rejected(self):
        text = re.sub(r"^cleanup_duty: .*\n", "", brief.render(GOOD_BRIEF), flags=re.MULTILINE)
        self.assertEqual(code_of(brief.parse, text), 3)

    def test_missing_review_destination_in_text_is_rejected(self):
        text = re.sub(r"^review_destination: .*\n", "", brief.render(GOOD_BRIEF), flags=re.MULTILINE)
        self.assertEqual(code_of(brief.parse, text), 3)

    def test_empty_and_blank_values_are_rejected(self):
        for key in ("cleanup_duty", "review_destination", "evidence", "objective", "data_class"):
            for bad in ("", "   "):
                with self.subTest(key=key, bad=bad):
                    self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, key: bad}), 3)

    def test_empty_scope_list_is_rejected(self):
        self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "scope": []}), 3)
        self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "acceptance": []}), 3)

    def test_unfilled_placeholder_is_rejected(self):
        self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "cleanup_duty": "<what to remove>"}), 3)

    def test_unfilled_template_is_rejected(self):
        text = (TEMPLATES / "brief.md").read_text(encoding="utf-8")
        self.assertEqual(code_of(brief.parse, text), 3)


class BriefFormatTests(unittest.TestCase):
    def test_issue_line_shape(self):
        for ok in ("a/b#1", "ChiefGyk3D/Strategic-Herding-of-IT-Tasks#10", "o.rg/re_po.x#123456789"):
            with self.subTest(ok=ok):
                self.assertIsNone(code_of(brief.render, {**GOOD_BRIEF, "issue": ok}))
        for bad in ("ab#1", "a/b#0", "a/b#01", "a/b", "a/b#x", "a/b#1234567890", "https://x/y#1", "a/b#1 ",
                    "a b/c#1", "../b#1", "a/b/c#1", "a/b#-1"):
            with self.subTest(bad=bad):
                self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "issue": bad}), 3)

    def test_role_is_one_lowercase_slug(self):
        for bad in ("a|b", "a,b", "Implementer", "", "a b", "-a"):
            with self.subTest(bad=bad):
                self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "role": bad}), 3)
        self.assertIsNone(code_of(brief.render, {**GOOD_BRIEF, "role": "human"}))

    def test_pack_names_are_slugs(self):
        self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "packs": ["Bad Pack"]}), 3)
        self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "packs": ["a,b"]}), 3)
        self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "packs": ["a", "a"]}), 3)

    def test_length_limits_match_the_schema(self):
        self.assertIsNone(code_of(brief.render, {**GOOD_BRIEF, "objective": "x" * 4000}))
        self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "objective": "x" * 4001}), 3)
        self.assertIsNone(code_of(brief.render, {**GOOD_BRIEF, "cleanup_duty": "x" * 1000}))
        self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "cleanup_duty": "x" * 1001}), 3)
        self.assertIsNone(code_of(brief.render, {**GOOD_BRIEF, "scope": ["x" * 500]}))
        self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "scope": ["x" * 501]}), 3)
        self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "constraints": ["x" * 501]}), 3)

    def test_unknown_field_is_rejected(self):
        self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "extra": "x"}), 3)
        self.assertEqual(code_of(brief.parse, brief.render(GOOD_BRIEF) + "extra: x\n"), 3)

    def test_non_string_values_are_rejected(self):
        self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "issue": 5}), 3)
        self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "scope": "a"}), 3)
        self.assertEqual(code_of(brief.render, {**GOOD_BRIEF, "scope": [1]}), 3)
        self.assertEqual(code_of(brief.render, [1]), 3)


class BriefHostileTextTests(unittest.TestCase):
    def test_newline_in_a_value_cannot_inject_a_field(self):
        bad = {**GOOD_BRIEF, "objective": "ok\ncleanup_duty: none"}
        self.assertEqual(code_of(brief.render, bad), 3)

    def test_duplicate_single_field_is_rejected(self):
        for key in ("issue", "role", "cleanup_duty", "review_destination"):
            with self.subTest(key=key):
                text = brief.render(GOOD_BRIEF) + f"{key}: second\n"
                self.assertEqual(code_of(brief.parse, text), 3)

    def test_padded_values_are_rejected_on_parse_as_on_render(self):
        base = brief.render(GOOD_BRIEF)
        padded = [
            base.replace("objective: Import the brief contract.",
                         "objective:   Import the brief contract.   "),
            base.replace("objective: Import the brief contract.",
                         "objective: Import the brief contract. "),
            base.replace("scope: templates/brief.md", "scope:  templates/brief.md"),
            base.replace("packs: review-protocol, threat-model",
                         "packs:  review-protocol, threat-model "),
        ]
        for text in padded:
            with self.subTest(text=text.splitlines()[2:6]):
                self.assertEqual(code_of(brief.parse, text), 3)
        self.assertEqual(brief.parse(base)["objective"], "Import the brief contract.")

    def test_repeated_scope_lines_are_a_list(self):
        text = brief.render({**GOOD_BRIEF, "scope": ["a", "b", "c"]})
        self.assertEqual(brief.parse(text)["scope"], ["a", "b", "c"])

    def test_control_zero_width_and_bidi_characters_are_rejected(self):
        for ch in HOSTILE_CHARS:
            with self.subTest(ch=hex(ord(ch))):
                self.assertEqual(code_of(brief.parse, brief.render(GOOD_BRIEF) + f"scope: a{ch}b\n"), 3)

    def test_byte_order_mark_and_crlf_are_rejected(self):
        text = brief.render(GOOD_BRIEF)
        self.assertEqual(code_of(brief.parse, chr(0xFEFF) + text), 3)
        self.assertEqual(code_of(brief.parse, text.replace("\n", "\r\n")), 3)

    def test_indented_or_fenced_key_does_not_count(self):
        text = re.sub(r"^issue: .*\n", "", brief.render(GOOD_BRIEF), flags=re.MULTILINE)
        self.assertEqual(code_of(brief.parse, text + "  issue: a/b#1\n"), 3)
        self.assertEqual(code_of(brief.parse, text + "```\nissue: a/b#1\n```\n"), 3)

    def test_key_inside_comment_does_not_count(self):
        text = re.sub(r"^issue: .*\n", "", brief.render(GOOD_BRIEF), flags=re.MULTILINE)
        self.assertEqual(code_of(brief.parse, text + "<!--\nissue: a/b#1\n-->\n"), 3)

    def test_unterminated_comment_is_rejected(self):
        self.assertEqual(code_of(brief.parse, brief.render(GOOD_BRIEF) + "<!-- open\n"), 3)

    def test_oversize_brief_is_rejected_with_the_limit_code(self):
        text = brief.render(GOOD_BRIEF) + "# " + "x" * 70000 + "\n"
        self.assertEqual(code_of(brief.parse, text), 7)

    def test_error_message_never_echoes_the_value(self):
        secret = "SECRETVALUE-123"
        try:
            brief.render({**GOOD_BRIEF, "issue": secret})
        except ContractError as e:
            self.assertNotIn(secret, str(e))
        else:
            self.fail("expected rejection")

    def test_non_text_input_is_rejected(self):
        self.assertEqual(code_of(brief.parse, b"issue: a/b#1"), 3)


class BriefFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_read_valid_file(self):
        p = self.dir / "b.md"
        p.write_text(brief.render(GOOD_BRIEF), encoding="utf-8")
        self.assertEqual(brief.read(p)["role"], "implementer")

    def test_read_bounds_the_bytes(self):
        p = self.dir / "big.md"
        p.write_bytes(b"#" * 70000)
        self.assertEqual(code_of(brief.read, p), 7)

    def test_read_rejects_invalid_utf8(self):
        p = self.dir / "bad.md"
        p.write_bytes(b"issue: \xff\xfe\n")
        self.assertEqual(code_of(brief.read, p), 3)

    def test_read_missing_file_is_precondition_missing(self):
        self.assertEqual(code_of(brief.read, self.dir / "nope.md"), 9)

    def test_read_refuses_a_symlink(self):
        real = self.dir / "real.md"
        real.write_text(brief.render(GOOD_BRIEF), encoding="utf-8")
        link = self.dir / "link.md"
        os.symlink(real, link)
        self.assertEqual(code_of(brief.read, link), 5)

    def test_read_refuses_a_fifo_without_blocking(self):
        fifo = self.dir / "pipe"
        os.mkfifo(fifo)
        self.assertEqual(code_of(brief.read, fifo), 5)

    def test_read_refuses_a_directory(self):
        self.assertEqual(code_of(brief.read, self.dir), 5)

    def test_read_refuses_a_symlinked_parent_directory(self):
        outside = self.dir / "outside"
        outside.mkdir()
        (outside / "brief.md").write_text(brief.render(GOOD_BRIEF), encoding="utf-8")
        work = self.dir / "work"
        work.mkdir()
        os.symlink(outside, work / "linked-dir")
        self.assertEqual(code_of(brief.read, work / "linked-dir" / "brief.md"), 5)

    def test_read_refuses_a_symlink_in_the_middle_of_the_path(self):
        real = self.dir / "real"
        (real / "deep").mkdir(parents=True)
        (real / "deep" / "brief.md").write_text(brief.render(GOOD_BRIEF), encoding="utf-8")
        os.symlink(real, self.dir / "alias")
        self.assertEqual(code_of(brief.read, self.dir / "alias" / "deep" / "brief.md"), 5)

    def test_read_refuses_a_hard_linked_file(self):
        outside = self.dir / "outside.md"
        outside.write_text(brief.render(GOOD_BRIEF), encoding="utf-8")
        hard = self.dir / "hard.md"
        os.link(outside, hard)
        self.assertEqual(code_of(brief.read, hard), 5)

    def test_header_reader_refuses_a_symlinked_parent_and_a_hard_link(self):
        real = self.dir / "real"
        real.mkdir()
        (real / "r.md").write_text(GOOD_HEADER, encoding="utf-8")
        os.symlink(real, self.dir / "linked")
        self.assertEqual(code_of(evidence.read_header, self.dir / "linked" / "r.md"), 5)
        os.link(real / "r.md", self.dir / "hard.md")
        self.assertEqual(code_of(evidence.read_header, self.dir / "hard.md"), 5)

    def test_read_of_a_plain_nested_path_still_works(self):
        nested = self.dir / "a" / "b"
        nested.mkdir(parents=True)
        (nested / "brief.md").write_text(brief.render(GOOD_BRIEF), encoding="utf-8")
        self.assertEqual(brief.read(nested / "brief.md")["role"], "implementer")


class HeaderTests(unittest.TestCase):
    def test_good_header_parses(self):
        h = evidence.validate_header(GOOD_HEADER)
        self.assertEqual(h["role"], "implementer")
        self.assertEqual(h["gate_exit"], 0)
        self.assertEqual(h["head"], "abc1234")

    def test_hand_filled_header_without_any_ai_is_accepted(self):
        text = GOOD_HEADER.replace("MODEL: example-model-1", "MODEL: n/a - done by hand")
        text = text.replace("ROLE: implementer", "ROLE: human")
        self.assertEqual(evidence.validate_header(text)["role"], "human")

    def test_each_missing_field_is_rejected(self):
        for label in ("ROLE", "HEAD", "GATE", "OTHER GATES", "PACKS LOADED", "TREE", "NOT VERIFIED", "LOCAL-LLM"):
            with self.subTest(label=label):
                text = "".join(ln for ln in GOOD_HEADER.splitlines(True) if not ln.startswith(label + ":"))
                self.assertEqual(code_of(evidence.validate_header, text), 3)

    def test_missing_model_and_base_on_a_shared_line_are_rejected(self):
        self.assertEqual(code_of(evidence.validate_header, GOOD_HEADER.replace("   MODEL: example-model-1", "")), 3)
        self.assertEqual(code_of(evidence.validate_header, GOOD_HEADER.replace("   BASE: 0123abc", "")), 3)

    def test_duplicate_field_is_rejected(self):
        for label, extra in (("GATE", "GATE: x  exit=0\n"), ("TREE", "TREE: n-a - dup\n"),
                             ("LOCAL-LLM", "LOCAL-LLM: n/a - dup\n"), ("PACKS LOADED", "PACKS LOADED: none\n")):
            with self.subTest(label=label):
                self.assertEqual(code_of(evidence.validate_header, GOOD_HEADER + extra), 3)

    def test_repeatable_fields_may_repeat(self):
        text = GOOD_HEADER.replace(
            "OTHER GATES: ruff: pass\n", "OTHER GATES: ruff: pass\nOTHER GATES: build: n-a - no build\n"
        )
        h = evidence.validate_header(text)
        self.assertEqual(len(h["other_gates"]), 2)

    def test_unknown_field_is_rejected(self):
        self.assertEqual(code_of(evidence.validate_header, GOOD_HEADER + "EXTRA: x\n"), 3)

    def test_malformed_values_are_rejected(self):
        cases = {
            "HEAD: abc1234": "HEAD: xyz",
            "BASE: 0123abc": "BASE: 12",
            "GATE: python3 -m unittest discover -s tests  exit=0": "GATE: python3 tests  exit=abc",
            "OTHER GATES: ruff: pass": "OTHER GATES: ruff: maybe",
            "TREE: clean (git status --porcelain is empty)": "TREE: tidy",
            "LOCAL-LLM: n/a - code and tests": "LOCAL-LLM: used",
            "PACKS LOADED: none": "PACKS LOADED: Bad Pack",
        }
        for good, bad in cases.items():
            with self.subTest(bad=bad):
                self.assertEqual(code_of(evidence.validate_header, GOOD_HEADER.replace(good, bad)), 3)

    def test_gate_forms(self):
        for ok in ("GATE: make test  exit=0", "GATE: make test  exit=1", "GATE: not-run - no runner",
                   "GATE: n-a - docs only"):
            with self.subTest(ok=ok):
                text = re.sub(r"^GATE: .*$", ok, GOOD_HEADER, flags=re.MULTILINE)
                self.assertIsNone(code_of(evidence.validate_header, text))
        for bad in ("GATE: make test  exit=256", "GATE: make test  exit=-1", "GATE: not-run", "GATE: n-a",
                    "GATE: not-run -", "GATE: exit=0", "GATE: make test exit=0", "GATE: "):
            with self.subTest(bad=bad):
                text = re.sub(r"^GATE: .*$", bad, GOOD_HEADER, flags=re.MULTILINE)
                self.assertEqual(code_of(evidence.validate_header, text), 3)

    def test_other_gate_not_run_needs_a_reason(self):
        text = GOOD_HEADER.replace("ruff: pass", "ruff: not-run")
        self.assertEqual(code_of(evidence.validate_header, text), 3)
        ok = GOOD_HEADER.replace("ruff: pass", "ruff: not-run - no tool")
        self.assertIsNone(code_of(evidence.validate_header, ok))

    def test_local_llm_used_form(self):
        ok = "LOCAL-LLM: used - model:7b; VERIFIED-AGAINST: the diff; DISCREPANCIES: 0"
        text = GOOD_HEADER.replace("LOCAL-LLM: n/a - code and tests", ok)
        self.assertIsNone(code_of(evidence.validate_header, text))
        for bad in ("LOCAL-LLM: used - model:7b", "LOCAL-LLM: used - m; VERIFIED-AGAINST: x; DISCREPANCIES: many"):
            with self.subTest(bad=bad):
                text = GOOD_HEADER.replace("LOCAL-LLM: n/a - code and tests", bad)
                self.assertEqual(code_of(evidence.validate_header, text), 3)

    def test_unfilled_placeholder_is_rejected(self):
        text = GOOD_HEADER.replace("example-model-1", "<model id>")
        self.assertEqual(code_of(evidence.validate_header, text), 3)

    def test_over_length_field_is_rejected(self):
        long_ok = GOOD_HEADER.replace("NOT VERIFIED: none", "NOT VERIFIED: " + "x" * 500)
        self.assertIsNone(code_of(evidence.validate_header, long_ok))
        long_bad = GOOD_HEADER.replace("NOT VERIFIED: none", "NOT VERIFIED: " + "x" * 501)
        self.assertEqual(code_of(evidence.validate_header, long_bad), 3)
        long_sha = GOOD_HEADER.replace("HEAD: abc1234", "HEAD: " + "a" * 41)
        self.assertEqual(code_of(evidence.validate_header, long_sha), 3)

    def test_too_many_lines_is_rejected(self):
        text = GOOD_HEADER + "NOT VERIFIED: x\n" * 80
        self.assertEqual(code_of(evidence.validate_header, text), 3)

    def test_oversize_header_is_rejected_with_the_limit_code(self):
        self.assertEqual(code_of(evidence.validate_header, GOOD_HEADER + "x" * 20000), 7)

    def test_hostile_characters_are_rejected(self):
        for ch in HOSTILE_CHARS:
            with self.subTest(ch=hex(ord(ch))):
                text = GOOD_HEADER.replace("none", "no" + ch + "ne", 1)
                self.assertEqual(code_of(evidence.validate_header, text), 3)

    def test_blank_line_ends_the_header_and_the_body_is_ignored(self):
        h = evidence.validate_header(GOOD_HEADER + "\nFindings: free text, EXTRA: not parsed\n")
        self.assertEqual(h["role"], "implementer")

    def test_non_text_input_is_rejected(self):
        self.assertEqual(code_of(evidence.validate_header, b"ROLE: x"), 3)

    def test_error_message_never_echoes_the_value(self):
        secret = "SECRETVALUE-456"
        try:
            evidence.validate_header(GOOD_HEADER.replace("abc1234", secret))
        except ContractError as e:
            self.assertNotIn(secret, str(e))
        else:
            self.fail("expected rejection")


class HeaderFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_read_header_from_a_report_with_a_long_body(self):
        p = self.dir / "r.md"
        p.write_text(GOOD_HEADER + "\n" + "body line\n" * 100000, encoding="utf-8")
        self.assertEqual(evidence.read_header(p)["role"], "implementer")

    def test_read_header_bounds_the_bytes_when_there_is_no_blank_line(self):
        p = self.dir / "r.md"
        p.write_text(GOOD_HEADER + "NOT VERIFIED: x\n" * 5000, encoding="utf-8")
        self.assertEqual(code_of(evidence.read_header, p), 7)

    def test_read_header_missing_symlink_and_fifo(self):
        self.assertEqual(code_of(evidence.read_header, self.dir / "none.md"), 9)
        real = self.dir / "real.md"
        real.write_text(GOOD_HEADER, encoding="utf-8")
        os.symlink(real, self.dir / "link.md")
        self.assertEqual(code_of(evidence.read_header, self.dir / "link.md"), 5)
        os.mkfifo(self.dir / "pipe")
        self.assertEqual(code_of(evidence.read_header, self.dir / "pipe"), 5)

    def test_read_header_rejects_invalid_utf8(self):
        p = self.dir / "r.md"
        p.write_bytes(GOOD_HEADER.encode() + b"\xff\n")
        self.assertEqual(code_of(evidence.read_header, p), 3)

    def test_multibyte_character_cut_at_the_read_limit_is_not_a_false_reject(self):
        p = self.dir / "r.md"
        p.write_bytes(GOOD_HEADER.encode() + b"\n" + chr(0xE9).encode() * 20000)
        self.assertEqual(evidence.read_header(p)["role"], "implementer")


class TemplateTests(unittest.TestCase):
    def test_provenance_records_source_and_commit(self):
        text = (TEMPLATES / "PROVENANCE.md").read_text(encoding="utf-8")
        self.assertIn("department-of-internal-arguments", text)
        self.assertRegex(text, r"\b[0-9a-f]{40}\b")
        for name in ("brief.md", "evidence-header.md", "handoff.md", "decision.md"):
            self.assertIn(name, text)

    def test_brief_template_names_every_field_the_validator_reads(self):
        text = (TEMPLATES / "brief.md").read_text(encoding="utf-8")
        keys = list(brief.SINGLE) + list(brief.MULTI)
        self.assertGreaterEqual(len(keys), 10)
        for key in keys:
            self.assertRegex(text, rf"(?m)^{key}: ", key)

    def test_filled_brief_template_validates(self):
        text = (TEMPLATES / "brief.md").read_text(encoding="utf-8")
        for key, value in GOOD_BRIEF.items():
            if isinstance(value, str):
                text = re.sub(rf"(?m)^{key}: .*$", lambda _m, k=key, v=value: f"{k}: {v}", text)
        text = re.sub(r"(?m)^packs: .*$", "packs: none", text)
        text = re.sub(r"(?m)^(scope|acceptance|constraints): .*$", r"\1: filled", text)
        self.assertEqual(brief.parse(text)["role"], "implementer")

    def test_report_template_header_has_every_label_and_fails_until_filled(self):
        text = (TEMPLATES / "evidence-header.md").read_text(encoding="utf-8")
        self.assertGreaterEqual(len(evidence.LABELS), 10)
        for label in evidence.LABELS:
            self.assertRegex(text, rf"(?m)^(.*   )?{re.escape(label)}: ", label)
        self.assertEqual(code_of(evidence.validate_header, text), 3)

    def test_handoff_and_decision_templates_carry_the_schema_vocabulary(self):
        handoff = (TEMPLATES / "handoff.md").read_text(encoding="utf-8")
        self.assertIn("Approval status", handoff)
        decision = (TEMPLATES / "decision.md").read_text(encoding="utf-8")
        for word in ("proposed", "accepted", "rejected", "superseded", "actor", "question", "choice", "rationale"):
            self.assertIn(word, decision.lower())

    def test_templates_need_no_vendor_specific_field(self):
        checked = 0
        for name in ("brief.md", "evidence-header.md", "handoff.md", "decision.md"):
            text = (TEMPLATES / name).read_text(encoding="utf-8").lower()
            checked += 1
            for term in ("claude", "sonnet", "opus", "haiku", "codex", "copilot"):
                self.assertNotIn(term, text, f"{name} mentions {term}")
        self.assertEqual(checked, 4)


if __name__ == "__main__":
    unittest.main()
