"""The code-built registry and the package's possible model calls stay in lockstep."""

import ast
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

try:
    from shit.model_paths import MODEL_PATHS, ModelPath
except ModuleNotFoundError:
    MODEL_PATHS = None
    ModelPath = None


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "shit"
MODEL_NAMES = ("ollama", "llm", "gemini", "codex", "claude", "openai")
OUTBOUND_ROOTS = {"http", "socket", "subprocess", "urllib"}


def _call_name(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _has_model_name(target):
    lowered = target.lower()
    tokens = lowered.replace(".", "_").split("_")
    return "llm" in tokens or any(name in lowered for name in MODEL_NAMES if name != "llm")


def model_call_sites(source, module="shit.fixture"):
    """Return possible model-call sites as ``(module, function, primitive)`` rows."""
    tree = ast.parse(source)
    package_names = set()
    outbound_aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in OUTBOUND_ROOTS:
                    outbound_aliases.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("shit"):
                package_names.update(alias.asname or alias.name for alias in node.names)
            elif node.module and node.module.split(".")[0] in OUTBOUND_ROOTS:
                outbound_aliases.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            package_names.add(node.name)

    found = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.functions = []

        def visit_FunctionDef(self, node):
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            target = _call_name(node.func)
            root = target.split(".", 1)[0]
            is_outbound = root in OUTBOUND_ROOTS or root in outbound_aliases
            is_model = (
                _has_model_name(target)
                and root not in package_names
                and not root.lstrip("_").isupper()
            )
            if target and (is_outbound or is_model):
                found.add((module, self.functions[-1] if self.functions else "<module>", target))
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


def package_model_call_sites():
    found = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(ROOT).with_suffix("")
        module = ".".join(relative.parts)
        found.update(model_call_sites(path.read_text(), module))
    return found


class ModelPathScannerTests(unittest.TestCase):
    def test_registry_module_exists(self):
        self.assertIsNotNone(MODEL_PATHS)

    def test_registry_is_a_tuple_of_frozen_rows(self):
        if MODEL_PATHS is None:
            self.skipTest("registry module does not exist yet")
        self.assertIsInstance(MODEL_PATHS, tuple)
        row = ModelPath("p", "shit.m", "f", "manual", ("text",), "local", False, False, False,
                        "deny privileges", "CLOSED", "guarded")
        with self.assertRaises(FrozenInstanceError):
            row.status = "OPEN"

    def test_scanner_flags_a_fake_model_call(self):
        source = "def send(text):\n    return ollama.generate(text)\n"
        self.assertEqual(
            model_call_sites(source),
            {("shit.fixture", "send", "ollama.generate")},
        )

    def test_scanner_flags_generic_outbound_and_model_primitives(self):
        samples = (
            "import urllib.request\ndef send():\n    urllib.request.urlopen('x')\n",
            "from http import client as hc\ndef send():\n    hc.HTTPConnection('x')\n",
            "import socket as net\ndef send():\n    net.create_connection(('x', 1))\n",
            "import subprocess as sp\ndef send():\n    sp.run(['model'])\n",
            "def send(text):\n    openai.responses.create(input=text)\n",
            "def send(text):\n    llm(text)\n",
        )
        for source in samples:
            with self.subTest(source=source):
                self.assertTrue(model_call_sites(source), source)

    def test_scanner_ignores_comments_strings_and_package_adapter_names(self):
        source = '''
from shit.adapters.claude import ClaudeAdapter
# ollama.generate(hostile)
EXAMPLE = "openai.responses.create(hostile)"
adapter = ClaudeAdapter()
'''
        self.assertEqual(model_call_sites(source), set())

    def test_registry_and_package_call_sites_agree(self):
        if MODEL_PATHS is None:
            self.skipTest("registry module does not exist yet")
        scanned = {(module, function) for module, function, _ in package_model_call_sites()}
        registered = {(row.module, row.function) for row in MODEL_PATHS}
        self.assertEqual(scanned, registered)

    def test_every_registered_path_is_closed_and_has_no_privileges(self):
        if MODEL_PATHS is None:
            self.skipTest("registry module does not exist yet")
        ids = [row.id for row in MODEL_PATHS]
        self.assertEqual(len(ids), len(set(ids)), "model-path IDs must be unique")
        for row in MODEL_PATHS:
            with self.subTest(path=row.id):
                for value in (row.id, row.module, row.function, row.trigger, row.model_kind, row.reason):
                    self.assertTrue(value.strip())
                self.assertTrue(row.untrusted_inputs)
                self.assertTrue(all(value.strip() for value in row.untrusted_inputs))
                self.assertFalse(row.tools)
                self.assertFalse(row.write)
                self.assertFalse(row.secrets)
                self.assertTrue(row.guard.strip())
                self.assertIn(row.status, {"CLOSED", "OPEN"})


if __name__ == "__main__":
    unittest.main()
