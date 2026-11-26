"""Checks on the generated starter set. Standard library only; run: python3 -m unittest discover -s tests -v

Guards collect loosely and assert strictly: each one asserts it found something to check, and the
negative cases below prove the checks fail on bad input.
"""

import hashlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "manifest.json"
TERMS_FILE = Path(__file__).resolve().parent / "privacy-terms.txt"
GENERATED_ROOTS = [".claude", ".codex", ".agents", ".github/agents", "prompts"]
SELF_FILES = {TERMS_FILE.resolve(), Path(__file__).resolve()}
SKIP_DIRS = {".git", "__pycache__"}
SESSION_URL = re.compile(r"claude\.ai/(?:code|chat)/", re.IGNORECASE)


def load_terms(*paths):
    terms = []
    for p in paths:
        if p and Path(p).is_file():
            for line in Path(p).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    terms.append(line.lower())
    return terms


def scan_text(text, terms):
    """Return the 1-based line numbers that hit a term or a session URL. Never returns the matched text."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        if any(t in low for t in terms) or SESSION_URL.search(line):
            hits.append(n)
    return hits


def repo_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def front_matter(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    return m.group(1) if m else ""


def description_of(path):
    """The description value from a role or pack file, in any of the three formats."""
    text = path.read_text(encoding="utf-8")
    source = text if path.suffix == ".toml" else front_matter(text)
    m = re.search(r'^description\s*[:=]\s*"(.*)"\s*$', source, re.MULTILINE)
    return m.group(1) if m else ""


def disk_generated():
    found = set()
    for root in GENERATED_ROOTS:
        base = ROOT / root
        if base.is_dir():
            found.update(p.relative_to(ROOT).as_posix() for p in base.rglob("*") if p.is_file())
    return found


class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_pin_is_a_full_commit(self):
        src = self.manifest["source"]
        self.assertRegex(src["commit"], r"^[0-9a-f]{40}$")
        self.assertTrue(src["release"])
        self.assertIn("department-of-internal-arguments", src["repository"])

    def test_files_match_manifest_hashes(self):
        files = self.manifest["files"]
        self.assertGreaterEqual(len(files), 28)
        for rel, digest in files.items():
            p = ROOT / rel
            self.assertTrue(p.is_file() and not p.is_symlink(), rel)
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), digest, rel)

    def test_no_unlisted_generated_files(self):
        self.assertEqual(disk_generated(), set(self.manifest["files"]))

    def test_every_role_and_pack_has_every_adapter_file(self):
        files = set(self.manifest["files"])
        roles, packs = self.manifest["roles"], self.manifest["packs"]
        self.assertEqual(len(roles), 4)
        self.assertEqual(len(packs), 6)
        for r in roles:
            for rel in (f".claude/agents/{r}.md", f".codex/agents/{r}.toml", f".github/agents/{r}.agent.md", f"prompts/{r}.md"):
                self.assertIn(rel, files)
        for p in packs:
            for rel in (f".claude/skills/pack-{p}/SKILL.md", f".agents/skills/pack-{p}/SKILL.md"):
                self.assertIn(rel, files)
        self.assertEqual(len(files), 4 * 4 + 6 * 2)

    def test_hash_check_bites(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.md"
            f.write_text("original", encoding="utf-8")
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            f.write_text("edited", encoding="utf-8")
            self.assertNotEqual(hashlib.sha256(f.read_bytes()).hexdigest(), digest)


class TriggerTests(unittest.TestCase):
    """Every role and pack file carries a trigger description, so the assistant knows when to use it."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def descriptions(self):
        return {
            rel: description_of(ROOT / rel) for rel in self.manifest["files"] if not rel.startswith("prompts/")
        }

    def test_every_description_present(self):
        d = self.descriptions()
        self.assertEqual(len(d), 4 * 3 + 6 * 2)
        for rel, text in d.items():
            self.assertGreaterEqual(len(text), 40, f"{rel}: description missing or too short")

    def test_role_descriptions_say_what_the_brief_names(self):
        checked = 0
        for rel, text in self.descriptions().items():
            if "/agents/" in rel:
                checked += 1
                self.assertRegex(text, r"(?i)brief", f"{rel}: role description should say what the brief supplies")
        self.assertEqual(checked, 12)

    def test_pack_descriptions_say_when_to_load(self):
        checked = 0
        for rel, text in self.descriptions().items():
            if "/skills/" in rel:
                checked += 1
                self.assertRegex(text, r"(?i)\b(load|use)\b", f"{rel}: pack description should say when to load it")
        self.assertEqual(checked, 12)

    def test_description_reader_bites(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.md"
            f.write_text("---\nname: a\n---\nbody\n", encoding="utf-8")
            self.assertEqual(description_of(f), "")


class PrivacyTests(unittest.TestCase):
    def test_terms_file_has_placeholders(self):
        self.assertGreaterEqual(len(load_terms(TERMS_FILE)), 3)

    def test_no_private_term_in_any_file(self):
        terms = load_terms(TERMS_FILE, os.environ.get("SHIT_EXTRA_TERMS"))
        scanned = 0
        for p in repo_files():
            if p.resolve() in SELF_FILES:  # they hold the placeholder terms and the negative-case strings
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            scanned += 1
            self.assertEqual(scan_text(text, terms), [], f"private term or session URL in {p.relative_to(ROOT)}")
        self.assertGreater(scanned, 30)

    def test_scan_bites(self):
        terms = load_terms(TERMS_FILE)
        self.assertEqual(scan_text("a clean line\nanother", terms), [])
        self.assertEqual(scan_text("ok\nworks at Example-Employer-Placeholder inc", terms), [2])
        self.assertEqual(scan_text("see https://claude.ai/code/session_x", terms), [1])

    def test_extra_terms_file_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            extra = Path(d) / "extra.txt"
            extra.write_text("# local\nlocal-only-test-word\n", encoding="utf-8")
            terms = load_terms(TERMS_FILE, extra)
            self.assertEqual(scan_text("has Local-Only-Test-Word here", terms), [1])


if __name__ == "__main__":
    unittest.main()
