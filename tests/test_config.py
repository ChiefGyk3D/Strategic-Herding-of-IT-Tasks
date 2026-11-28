"""Tests for the project configuration contract (templates/config.toml, shit/config.py). Standard library only.

Run: python3 -m unittest discover -s tests -v

Each rule has a case that is accepted and a case that is refused, so a guard that stops biting shows up.
Refusals are checked by exit code (docs/exit-codes.md): 3 record rejected, 7 input limit.
"""

import re
import unittest

import _bootstrap

from shit import config
from shit.errors import ContractError

ROOT = _bootstrap.ROOT
TEMPLATES = ROOT / "templates"

GOOD = """\
schema_version = 1

[project]
name = "example-project"

[tracker]
repository = "example-owner/example-repo"

[roles]
names = ["implementer", "reviewer", "researcher", "planner"]

[review]
destination = "reviewer role, report to the lead"

[storage]
root = ".shit"

[log]
file = ".shit/events.jsonl"

[privacy]
scan = true

[local_model]
context_tokens = 32768
"""


def code_of(fn, *args):
    try:
        fn(*args)
    except ContractError as e:
        return e.code
    return None


class ConfigValidTests(unittest.TestCase):
    def test_valid_file_passes(self):
        parsed = config.parse(GOOD)
        self.assertEqual(parsed["schema_version"], 1)
        self.assertEqual(parsed["project"]["name"], "example-project")
        self.assertEqual(parsed["tracker"]["repository"], "example-owner/example-repo")
        self.assertEqual(
            parsed["roles"]["names"],
            ["implementer", "reviewer", "researcher", "planner"],
        )
        self.assertEqual(parsed["storage"]["root"], ".shit")
        self.assertEqual(parsed["log"]["file"], ".shit/events.jsonl")
        self.assertIs(parsed["privacy"]["scan"], True)
        self.assertEqual(parsed["local_model"]["context_tokens"], 32768)

    def test_64k_is_the_only_opt_in_window(self):
        self.assertEqual(
            config.parse(GOOD.replace("32768", "65536"))["local_model"][
                "context_tokens"
            ],
            65536,
        )
        for bad in ("131072", "16384", "4096"):
            with self.subTest(bad=bad):
                self.assertEqual(code_of(config.parse, GOOD.replace("32768", bad)), 3)

    def test_privacy_scan_missing_fails_closed_to_on(self):
        parsed = config.parse(GOOD.replace("[privacy]\nscan = true\n", ""))
        self.assertIs(parsed["privacy"]["scan"], True)

    def test_privacy_scan_may_be_turned_off_explicitly(self):
        parsed = config.parse(GOOD.replace("scan = true", "scan = false"))
        self.assertIs(parsed["privacy"]["scan"], False)


class ConfigRejectionTests(unittest.TestCase):
    def test_unknown_top_level_key_is_rejected(self):
        self.assertEqual(
            code_of(
                config.parse,
                GOOD.replace("schema_version = 1\n", "schema_version = 1\nextra = 1\n"),
            ),
            3,
        )

    def test_unknown_key_inside_a_table_is_rejected(self):
        self.assertEqual(
            code_of(
                config.parse, GOOD.replace("scan = true", "scan = true\nextra = 1")
            ),
            3,
        )

    def test_unknown_table_is_rejected(self):
        self.assertEqual(code_of(config.parse, GOOD + "\n[surprise]\nx = 1\n"), 3)

    def test_wrong_type_is_rejected(self):
        cases = {
            "schema_version as string": GOOD.replace(
                "schema_version = 1", 'schema_version = "1"'
            ),
            "schema_version as boolean": GOOD.replace(
                "schema_version = 1", "schema_version = true"
            ),
            "scan as string": GOOD.replace("scan = true", 'scan = "true"'),
            "context_tokens as string": GOOD.replace(
                "context_tokens = 32768", 'context_tokens = "32768"'
            ),
            "roles as string": GOOD.replace(
                'names = ["implementer", "reviewer", "researcher", "planner"]',
                'names = "implementer"',
            ),
            "name as integer": GOOD.replace('name = "example-project"', "name = 7"),
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                self.assertEqual(code_of(config.parse, text), 3)

    def test_missing_schema_version_is_rejected(self):
        self.assertEqual(
            code_of(config.parse, GOOD.replace("schema_version = 1\n", "")), 3
        )

    def test_unknown_schema_version_is_rejected(self):
        self.assertEqual(
            code_of(
                config.parse, GOOD.replace("schema_version = 1", "schema_version = 2")
            ),
            3,
        )

    def test_missing_required_key_is_rejected(self):
        self.assertEqual(
            code_of(
                config.parse,
                GOOD.replace('destination = "reviewer role, report to the lead"\n', ""),
            ),
            3,
        )

    def test_duplicate_key_is_rejected(self):
        self.assertEqual(
            code_of(
                config.parse,
                GOOD.replace('name = "example-project"', 'name = "a"\nname = "b"'),
            ),
            3,
        )

    def test_invalid_toml_is_rejected(self):
        self.assertEqual(code_of(config.parse, "schema_version = "), 3)

    def test_storage_must_be_the_fixed_root(self):
        for bad in ("/etc", "../outside", "~/.shit", ".shit/../x", "other"):
            with self.subTest(bad=bad):
                self.assertEqual(
                    code_of(
                        config.parse, GOOD.replace('root = ".shit"', f'root = "{bad}"')
                    ),
                    3,
                )

    def test_log_must_stay_under_the_fixed_root(self):
        for bad in (
            "/var/log/events.jsonl",
            ".shit/../events.jsonl",
            "events.jsonl",
            ".shit/a b.jsonl",
        ):
            with self.subTest(bad=bad):
                self.assertEqual(
                    code_of(
                        config.parse,
                        GOOD.replace('file = ".shit/events.jsonl"', f'file = "{bad}"'),
                    ),
                    3,
                )

    def test_tracker_reference_must_name_owner_and_repo(self):
        for bad in ("example-repo", "owner/repo#10", "owner/repo/extra"):
            with self.subTest(bad=bad):
                self.assertEqual(
                    code_of(
                        config.parse,
                        GOOD.replace('"example-owner/example-repo"', f'"{bad}"'),
                    ),
                    3,
                )

    def test_role_names_must_be_slugs(self):
        self.assertEqual(
            code_of(config.parse, GOOD.replace('"planner"', '"Planner"')), 3
        )

    def test_unfilled_placeholder_is_rejected(self):
        self.assertEqual(
            code_of(
                config.parse, GOOD.replace('"example-project"', '"<project name>"')
            ),
            3,
        )

    def test_hostile_character_is_rejected(self):
        zero_width_space = chr(0x200B)
        self.assertEqual(
            code_of(
                config.parse,
                GOOD.replace(
                    "example-project", "example" + zero_width_space + "-project"
                ),
            ),
            3,
        )

    def test_non_text_input_is_rejected(self):
        self.assertEqual(code_of(config.parse, b"schema_version = 1"), 3)

    def test_error_message_never_echoes_the_value(self):
        secret = "SECRET-VALUE-123"
        try:
            config.parse(
                GOOD.replace('"example-project"', f'"{secret}"').replace(
                    "scan = true", f'scan = "{secret}"'
                )
            )
        except ContractError as e:
            self.assertNotIn(secret, str(e))
        else:
            self.fail("expected a rejection")


class ConfigSizeTests(unittest.TestCase):
    def test_oversize_file_is_rejected_with_the_limit_code(self):
        text = GOOD + "#" + "x" * config.MAX_BYTES
        self.assertEqual(code_of(config.parse, text), 7)

    def test_file_at_the_limit_is_not_rejected_for_size(self):
        text = GOOD + "#" + "x" * (config.MAX_BYTES - len(GOOD.encode("utf-8")) - 1)
        self.assertEqual(len(text.encode("utf-8")), config.MAX_BYTES)
        self.assertEqual(config.parse(text)["schema_version"], 1)


class ConfigLimitsTests(unittest.TestCase):
    LIMITS = '\n[limits]\ninstruction_max_bytes = 8000\nlisted_roles_max = 6\non_exceed = "fail"\n'

    def test_limits_parse(self):
        self.assertEqual(
            config.parse(GOOD + self.LIMITS)["limits"],
            {"instruction_max_bytes": 8000, "listed_roles_max": 6, "on_exceed": "fail"},
        )

    def test_absent_limits_leave_sizes_unset_and_mode_warn(self):
        self.assertEqual(config.parse(GOOD)["limits"], {"on_exceed": "warn"})

    def test_bad_limit_values_are_rejected(self):
        for bad in ("0", "-1", "true", "1.5", '"9"', "10000001"):
            with self.subTest(bad=bad):
                text = GOOD + f"\n[limits]\ninstruction_max_bytes = {bad}\n"
                self.assertEqual(code_of(config.parse, text), 3)

    def test_bad_mode_and_unknown_limit_key_are_rejected(self):
        self.assertEqual(
            code_of(config.parse, GOOD + '\n[limits]\non_exceed = "ignore"\n'), 3
        )
        self.assertEqual(code_of(config.parse, GOOD + "\n[limits]\nsurprise = 1\n"), 3)

    def test_template_documents_limits_as_comments(self):
        text = (TEMPLATES / "config.toml").read_text(encoding="utf-8")
        for key in ("instruction_max_bytes", "listed_roles_max", "on_exceed"):
            self.assertRegex(text, rf"(?m)^# {key} = ")


class ConfigStoreTests(unittest.TestCase):
    def test_backend_defaults_to_files_and_accepts_only_named_backends(self):
        self.assertEqual(config.parse(GOOD)["store"]["backend"], "files")
        for backend in ("files", "sqlite"):
            with self.subTest(backend=backend):
                parsed = config.parse(GOOD + f'\n[store]\nbackend = "{backend}"\n')
                self.assertEqual(parsed["store"]["backend"], backend)
        for bad in ('"other"', "true", "1"):
            with self.subTest(bad=bad):
                self.assertEqual(
                    code_of(config.parse, GOOD + f"\n[store]\nbackend = {bad}\n"), 3
                )

    def test_default_wait_matches_the_store(self):
        from shit import store

        self.assertEqual(
            config.parse(GOOD)["store"],
            {"backend": "files", "busy_timeout_ms": store.DEFAULT_BUSY_TIMEOUT_MS},
        )
        self.assertEqual(
            config.parse(GOOD + "\n[store]\nbusy_timeout_ms = 0\n")["store"][
                "busy_timeout_ms"
            ],
            0,
        )
        self.assertEqual(
            config.parse(GOOD + "\n[store]\nbusy_timeout_ms = 600000\n")["store"][
                "busy_timeout_ms"
            ],
            store.MAX_BUSY_TIMEOUT_MS,
        )

    def test_bad_wait_values_are_rejected(self):
        for bad in ("-1", "600001", "true", "1.5", '"9"'):
            with self.subTest(bad=bad):
                self.assertEqual(
                    code_of(
                        config.parse, GOOD + f"\n[store]\nbusy_timeout_ms = {bad}\n"
                    ),
                    3,
                )
        self.assertEqual(code_of(config.parse, GOOD + "\n[store]\nsurprise = 1\n"), 3)

    def test_template_documents_the_store_wait_as_a_comment(self):
        text = (TEMPLATES / "config.toml").read_text(encoding="utf-8")
        self.assertRegex(text, r'(?m)^# backend = "files"')
        self.assertRegex(text, r"(?m)^# busy_timeout_ms = 60000")


class ConfigTemplateTests(unittest.TestCase):
    def setUp(self):
        self.text = (TEMPLATES / "config.toml").read_text(encoding="utf-8")

    def test_template_fails_until_every_placeholder_is_filled(self):
        self.assertEqual(code_of(config.parse, self.text), 3)

    def test_template_names_every_key_the_validator_reads(self):
        for key in (
            "schema_version",
            "name",
            "repository",
            "names",
            "destination",
            "root",
            "file",
            "scan",
            "context_tokens",
        ):
            self.assertRegex(self.text, rf"(?m)^{key} = ", key)

    def test_every_key_line_has_a_comment_above_it(self):
        lines = self.text.split("\n")
        for i, line in enumerate(lines):
            if re.match(r"[a-z_]+ = ", line):
                with self.subTest(line=line):
                    self.assertTrue(lines[i - 1].startswith("#"), line)

    def test_template_has_no_vendor_or_person_specific_term(self):
        lower = self.text.lower()
        for term in (
            "claude",
            "sonnet",
            "opus",
            "haiku",
            "codex",
            "copilot",
            "doia",
            "department-of",
            "callsign",
            "hostname",
        ):
            self.assertNotIn(term, lower)

    def test_provenance_says_config_is_new_content_not_imported(self):
        text = (TEMPLATES / "PROVENANCE.md").read_text(encoding="utf-8")
        self.assertRegex(text, r"`config\.toml`[^\n]*NEW content")
        self.assertRegex(text, r"Stage 1 docs")
        self.assertNotRegex(text, r"Not imported: `config\.toml`")


if __name__ == "__main__":
    unittest.main()
