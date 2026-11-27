"""Tests for the tool adapters (issue #12). Standard library only.

Run: python3 -m unittest discover -s tests -v

Every guard has an accepted case and a refused case, so a guard that stops biting shows up.
"""

import os
import tempfile
import unittest
from pathlib import Path

import _bootstrap

from shit import adapters, brief
from shit.adapters import base
from shit.errors import ContractError

ROOT = _bootstrap.ROOT

GOOD_BRIEF = {
    "issue": "ChiefGyk3D/Strategic-Herding-of-IT-Tasks#12",
    "role": "implementer",
    "model": "example-model-1",
    "packs": ["review-protocol"],
    "objective": "Write the adapter layer.",
    "scope": ["shit/adapters/base.py"],
    "constraints": ["Standard library only."],
    "acceptance": ["Tests pass."],
    "data_class": "internal",
    "cleanup_duty": "Remove scratch.",
    "evidence": "Paste the test output.",
    "review_destination": "reviewer",
}

GOOD_REPORT = """ROLE: implementer   MODEL: example-model-1
HEAD: abc1234   BASE: def5678
GATE: python3 -m unittest  exit=0
OTHER GATES: lint: pass
PACKS LOADED: none
TREE: clean (git status empty)
NOT VERIFIED: none
LOCAL-LLM: n/a - code work

Free text after the header.
"""


def brief_text(**changes):
    fields = dict(GOOD_BRIEF)
    fields.update(changes)
    return brief.render(fields)


class RegistryTests(unittest.TestCase):
    def test_four_adapters_registered(self):
        self.assertEqual(sorted(adapters.names()), ["claude", "codex", "copilot", "prompt"])

    def test_unknown_name_is_usage_error(self):
        with self.assertRaises(ContractError) as cm:
            adapters.get("nope")
        self.assertEqual(cm.exception.code, 2)

    def test_non_string_name_is_usage_error(self):
        with self.assertRaises(ContractError) as cm:
            adapters.get(["claude"])
        self.assertEqual(cm.exception.code, 2)

    def test_self_description(self):
        infos = adapters.doctor()
        self.assertEqual(len(infos), 4)
        for info in infos:
            self.assertTrue(info["name"])
            self.assertTrue(info["needs"])
            self.assertTrue(info["never"])
            self.assertIn("invoke", " ".join(info["never"]))
            self.assertIn("network", " ".join(info["never"]))


class ArgvTests(unittest.TestCase):
    def test_claude_exact_argv(self):
        plan = adapters.get("claude").prepare(brief_text())
        self.assertEqual(plan.argv, ("claude", "-p", "--permission-mode", "plan", "--output-format", "text",
                                     "--model", "example-model-1"))
        self.assertEqual(plan.stdin, plan.prompt)

    def test_codex_exact_argv(self):
        plan = adapters.get("codex").prepare(brief_text())
        self.assertEqual(plan.argv, ("codex", "exec", "--sandbox", "read-only", "--model", "example-model-1",
                                     "-"))
        self.assertEqual(plan.stdin, plan.prompt)

    def test_copilot_exact_argv(self):
        plan = adapters.get("copilot").prepare(brief_text())
        self.assertEqual(plan.argv[:3], ("copilot", "--model", "example-model-1"))
        self.assertEqual(plan.argv[3], "-p")
        self.assertEqual(plan.argv[4], plan.prompt)
        self.assertEqual(len(plan.argv), 5)
        self.assertEqual(plan.stdin, "")

    def test_no_model_means_no_model_flag(self):
        fields = dict(GOOD_BRIEF)
        del fields["model"]
        text = brief.render(fields)
        self.assertNotIn("--model", adapters.get("claude").prepare(text).argv)
        self.assertNotIn("--model", adapters.get("codex").prepare(text).argv)

    def test_prompt_adapter_has_no_argv(self):
        plan = adapters.get("prompt").prepare(brief_text())
        self.assertIsNone(plan.argv)
        self.assertIn("Write the adapter layer.", plan.prompt)

    def test_default_permissions_are_least_capable(self):
        self.assertIn("plan", adapters.get("claude").prepare(brief_text()).permissions)
        self.assertIn("read-only", adapters.get("codex").prepare(brief_text()).permissions)
        self.assertIn("no tools allowed", adapters.get("copilot").prepare(brief_text()).permissions)

    def test_argv_is_a_tuple_of_strings(self):
        for name in ("claude", "codex", "copilot"):
            plan = adapters.get(name).prepare(brief_text())
            self.assertIsInstance(plan.argv, tuple)
            self.assertTrue(all(isinstance(a, str) for a in plan.argv))


class HostileTitleTests(unittest.TestCase):
    """A malicious task title cannot become shell syntax: it stays inside one prompt text."""

    PAYLOADS = ["$(touch pwned)", "`touch pwned`", "; rm -rf / ;", "x' && touch pwned #", "| cat /etc/passwd",
                "--dangerously-skip-permissions", "-s danger-full-access", "${IFS}pwned", "&& echo hi"]

    def test_payload_never_becomes_an_argument_or_flag(self):
        for payload in self.PAYLOADS:
            text = brief_text(objective="Fix " + payload)
            for name in ("claude", "codex"):
                plan = adapters.get(name).prepare(text)
                self.assertNotIn(payload, " ".join(plan.argv))
                self.assertIn(payload, plan.stdin)
            plan = adapters.get("copilot").prepare(text)
            self.assertEqual(plan.argv[4], plan.prompt)  # the whole prompt is exactly one element
            self.assertIn(payload, plan.prompt)
            self.assertFalse(plan.prompt.startswith("-"))

    def test_payload_in_model_field_is_rejected_by_contract(self):
        for payload in ("$(touch pwned)", "x; y", "--flag", "a`b`"):
            with self.assertRaises(ContractError):
                adapters.get("claude").prepare(brief_text(model=payload))

    def test_no_adapter_creates_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            os.chdir(d)
            try:
                for name in adapters.names():
                    adapters.get(name).prepare(brief_text(objective="Fix $(touch pwned)"))
                self.assertEqual(os.listdir(d), [])
            finally:
                os.chdir(old)

    def test_adapter_sources_have_no_process_or_network_use(self):
        sources = [p.read_text() for p in (ROOT / "shit" / "adapters").glob("*.py")]
        self.assertGreaterEqual(len(sources), 5)
        for src in sources:
            for banned in ("import subprocess", "import socket", "import urllib", "import http", "os.system",
                           "os.popen", "shell=True"):
                self.assertNotIn(banned, src)


class InputGuardTests(unittest.TestCase):
    def test_invalid_brief_refused(self):
        with self.assertRaises(ContractError) as cm:
            adapters.get("claude").prepare("objective: missing the rest\n")
        self.assertEqual(cm.exception.code, 3)

    def test_oversize_brief_refused(self):
        with self.assertRaises(ContractError) as cm:
            adapters.get("claude").prepare("x" * (brief.MAX_BYTES + 1))
        self.assertEqual(cm.exception.code, 7)

    def test_hostile_characters_refused(self):
        for bad in ("\x1b[31m", "‮", "\x00", "​"):
            with self.assertRaises(ContractError):
                adapters.get("prompt").prepare(brief_text() + bad)

    def test_non_text_refused(self):
        with self.assertRaises(ContractError):
            adapters.get("prompt").prepare(b"bytes")

    def test_prompt_size_is_bounded(self):
        self.assertLessEqual(base.MAX_PROMPT_BYTES, 131072)
        plan = adapters.get("prompt").prepare(brief_text())
        self.assertLessEqual(len(plan.prompt.encode()), base.MAX_PROMPT_BYTES)


class ConfigTests(unittest.TestCase):
    def test_unknown_config_key_refused(self):
        with self.assertRaises(ContractError) as cm:
            adapters.get("claude").prepare(brief_text(), config={"shell": True})
        self.assertEqual(cm.exception.code, 2)

    def test_config_must_be_a_mapping(self):
        with self.assertRaises(ContractError):
            adapters.get("claude").prepare(brief_text(), config=["extra_args"])

    def test_safe_extra_arg_allowed(self):
        plan = adapters.get("claude").prepare(brief_text(), config={"extra_args": ["--verbose"]})
        self.assertIn("--verbose", plan.argv)
        self.assertEqual(plan.overrides, ())

    def test_unsafe_extra_args_need_approval(self):
        unsafe = {
            "claude": ["--dangerously-skip-permissions", "--permission-mode", "bypassPermissions",
                       "--allowedTools"],
            "codex": ["--dangerously-bypass-approvals-and-sandbox", "--sandbox", "danger-full-access",
                      "--full-auto"],
            "copilot": ["--allow-all-tools", "--allow-all", "--yolo", "--allow-tool"],
        }
        for name, flags in unsafe.items():
            for flag in flags:
                with self.assertRaises(ContractError) as cm:
                    adapters.get(name).prepare(brief_text(), config={"extra_args": [flag]})
                self.assertEqual(cm.exception.code, 10, (name, flag))

    def test_override_allows_and_is_recorded(self):
        plan = adapters.get("codex").prepare(
            brief_text(), config={"extra_args": ["--full-auto"]}, override=True)
        self.assertIn("--full-auto", plan.argv)
        self.assertEqual(len(plan.overrides), 1)
        self.assertIn("override", plan.describe())

    def test_override_must_be_literal_true(self):
        with self.assertRaises(ContractError):
            adapters.get("codex").prepare(brief_text(), config={"extra_args": ["--full-auto"]}, override="yes")

    def test_override_never_admits_control_characters(self):
        for arg in ("--x\n--y", "--x\x00", "", "a" * 300, 5):
            with self.assertRaises(ContractError):
                adapters.get("claude").prepare(brief_text(), config={"extra_args": [arg]}, override=True)

    def test_extra_args_on_prompt_adapter_refused(self):
        with self.assertRaises(ContractError):
            adapters.get("prompt").prepare(brief_text(), config={"extra_args": ["--verbose"]})

    def test_extra_args_must_be_a_short_list(self):
        for value in ("--verbose", ["--verbose"] * 20, None):
            with self.assertRaises(ContractError):
                adapters.get("claude").prepare(brief_text(), config={"extra_args": value})

    def test_timeout_default_and_bounds(self):
        self.assertEqual(adapters.get("claude").prepare(brief_text()).timeout, base.DEFAULT_TIMEOUT)
        self.assertEqual(adapters.get("claude").prepare(brief_text(), config={"timeout": 30}).timeout, 30)
        for bad in (0, -1, base.MAX_TIMEOUT + 1, True, 1.5, "30"):
            with self.assertRaises(ContractError):
                adapters.get("claude").prepare(brief_text(), config={"timeout": bad})


class EnvTests(unittest.TestCase):
    def test_only_allowlisted_variables_pass(self):
        parent = {"PATH": "/usr/bin", "HOME": "/home/x", "LANG": "C", "SECRET_TOKEN": "s3cret",
                  "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "GH_TOKEN": "k"}
        env = base.clean_env(parent)
        self.assertEqual(env, {"PATH": "/usr/bin", "HOME": "/home/x", "LANG": "C"})

    def test_plan_declares_allowlist_only(self):
        plan = adapters.get("claude").prepare(brief_text())
        self.assertEqual(set(plan.env_allow), set(base.ENV_ALLOW))
        self.assertNotIn("GH_TOKEN", plan.env_allow)

    def test_control_characters_in_values_dropped(self):
        env = base.clean_env({"PATH": "/usr/bin\nINJECT=1", "HOME": "/h"})
        self.assertEqual(env, {"HOME": "/h"})


class OutputTests(unittest.TestCase):
    def test_header_parsed_for_every_adapter(self):
        for name in adapters.names():
            header = adapters.get(name).parse_output(GOOD_REPORT)
            self.assertEqual(header["role"], "implementer")
            self.assertEqual(header["gate_exit"], 0)

    def test_header_found_after_prose_and_fence(self):
        text = "Here is my answer.\n\n```\n" + GOOD_REPORT.split("\n\n")[0] + "\n```\n"
        header = adapters.get("prompt").parse_output(text)
        self.assertEqual(header["head"], "abc1234")

    def test_crlf_from_paste_accepted(self):
        header = adapters.get("prompt").parse_output(GOOD_REPORT.replace("\n", "\r\n"))
        self.assertEqual(header["role"], "implementer")

    def test_no_header_is_rejected(self):
        with self.assertRaises(ContractError) as cm:
            adapters.get("prompt").parse_output("I did the work, trust me.\n")
        self.assertEqual(cm.exception.code, 3)

    def test_bad_header_rejected_by_contract(self):
        with self.assertRaises(ContractError) as cm:
            adapters.get("claude").parse_output(GOOD_REPORT.replace("exit=0", "exit=999"))
        self.assertEqual(cm.exception.code, 3)

    def test_oversize_output_refused(self):
        with self.assertRaises(ContractError) as cm:
            adapters.get("codex").parse_output(GOOD_REPORT + "x" * base.MAX_OUTPUT_BYTES)
        self.assertEqual(cm.exception.code, 7)

    def test_hostile_output_refused(self):
        for bad in ("\x1b]0;title\x07", "‮", "\x00", "​"):
            with self.assertRaises(ContractError):
                adapters.get("copilot").parse_output(GOOD_REPORT + bad)

    def test_hostile_text_before_header_refused(self):
        with self.assertRaises(ContractError):
            adapters.get("prompt").parse_output("\x1b[2J\n" + GOOD_REPORT)

    def test_non_text_output_refused(self):
        with self.assertRaises(ContractError):
            adapters.get("prompt").parse_output(b"ROLE: x")

    def test_error_never_echoes_value(self):
        marker = "SENTINEL-VALUE-123"
        try:
            adapters.get("prompt").parse_output(GOOD_REPORT.replace("abc1234", marker))
        except ContractError as exc:
            self.assertNotIn(marker, str(exc))
        else:
            self.fail("expected rejection")

    def test_second_header_in_answer_is_not_read(self):
        text = GOOD_REPORT + "\nROLE: reviewer   MODEL: other\n"
        self.assertEqual(adapters.get("prompt").parse_output(text)["role"], "implementer")


class DescribeTests(unittest.TestCase):
    def test_dry_run_text_shows_exact_argv_permissions_and_files(self):
        plan = adapters.get("codex").prepare(brief_text())
        text = plan.describe()
        self.assertIn("codex exec --sandbox read-only", text)
        self.assertIn("permissions:", text)
        self.assertIn("files:", text)
        self.assertIn("timeout:", text)
        self.assertIn("environment:", text)

    def test_paste_in_describe_says_nothing_is_run(self):
        self.assertIn("nothing is run", adapters.get("prompt").prepare(brief_text()).describe())


if __name__ == "__main__":
    unittest.main()
