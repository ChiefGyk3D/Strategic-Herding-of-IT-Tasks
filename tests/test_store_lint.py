"""Static guards over the whole package for the SQLite store (issue 59).

They read the source of every module under `shit/` (nothing is imported or run) and check:

- no SQL text is built by f-string, `%`, `.format` or `+`, except in the named schema builders;
- only `shit/store.py` imports sqlite3, and no module reaches it by `importlib` or `__import__`;
- no module but the store calls `execute`, `executemany`, `executescript`, `cursor` or other connection methods;
- inside the store, `executescript` and cursors are forbidden, SQL text passed to `execute` is a literal (or a
  named loop variable over the schema), and every statement that changes anything runs inside `write_transaction`
  or in one of the few functions that own their transaction.

Every rule has a negative case that proves it bites.
"""

import ast
import re
import unittest

from _bootstrap import ROOT

SQL_START = re.compile(r"\s*(select|insert|update|delete|create|drop|alter|pragma|begin|commit|rollback|with"
                       r"|replace|vacuum|attach|detach|savepoint|release)\b", re.IGNORECASE)
READ_ONLY = re.compile(r"\s*(select\b|pragma\s+[a-z_]+\s*(\(\s*[a-z_]*\s*\))?\s*$)", re.IGNORECASE)
EXECUTORS = {"execute", "executemany", "executescript"}
# Connection and cursor methods that reach the database. Outside the store none may be called.
DB_METHODS = EXECUTORS | {"cursor", "backup", "iterdump", "create_function", "create_aggregate", "create_collation",
                          "set_authorizer", "enable_load_extension", "load_extension", "setconfig", "blobopen",
                          "set_progress_handler", "set_trace_callback", "commit", "rollback"}
STORE = "shit/store.py"
# The only functions that may assemble SQL text: schema DDL from this code's own vocabulary.
BUILDERS = {(STORE, name) for name in (
    "schema_statements", "_guard", "_no_pair", "_queue_guards", "_lease_guards", "_chain_guard", "_stamp_check",
    "_slug_check", "_hex_check", "_text_check", "_seconds", "_pair_rows", "_kind_rules")}
# Functions of the store that run their own BEGIN IMMEDIATE / pragmas / scratch in-memory database.
TXN_OWNERS = {"_open_once", "_install_schema", "write_transaction", "expected_fingerprint"}
NAMED_SQL_VARIABLES = {("_install_schema", "sql"), ("expected_fingerprint", "sql"), ("_check_settings", "sql")}


def _leaves(node):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _leaves(node.left) + _leaves(node.right)
    return [node]


def _text(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _with_parents(tree):
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node
    return tree


def _enclosing_function(node):
    while node is not None:
        node = getattr(node, "parent", None)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return None


def _in_write_transaction(node):
    while node is not None:
        node = getattr(node, "parent", None)
        if isinstance(node, ast.With):
            for item in node.items:
                call = item.context_expr
                if isinstance(call, ast.Call):
                    func = call.func
                    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                    if name == "write_transaction":
                        return True
    return False


def sql_findings(source, rel="x.py", builders=frozenset()):
    """Lines that build SQL text by f-string, %, .format or +."""
    tree = _with_parents(ast.parse(source))
    found = []
    for node in ast.walk(tree):
        if (rel, _enclosing_function(node)) in builders:
            continue
        if isinstance(node, ast.JoinedStr):
            head = _text(node.values[0]) if node.values else None
            if head is not None and SQL_START.match(head):
                found.append((node.lineno, "f-string"))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            head = _text(node.left)
            if head is not None and SQL_START.match(head):
                found.append((node.lineno, "percent"))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            leaves = _leaves(node)
            head = _text(leaves[0])
            if len(leaves) > 1 and head is not None and SQL_START.match(head):
                found.append((node.lineno, "plus"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            head = _text(node.func.value)
            if head is not None and SQL_START.match(head):
                found.append((node.lineno, "format"))
    return found


def import_findings(source):
    """Any way of reaching sqlite3 other than a plain import, and the plain import itself."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in ("sqlite3", "_sqlite3"):
                    found.append((node.lineno, "import"))
                if root == "importlib":
                    found.append((node.lineno, "importlib"))
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in ("sqlite3", "_sqlite3"):
                found.append((node.lineno, "import"))
            if root == "importlib":
                found.append((node.lineno, "importlib"))
        elif (isinstance(node, ast.Name) and node.id in ("__import__", "import_module")) or (
                isinstance(node, ast.Attribute) and node.attr in ("import_module", "__import__")):
            found.append((node.lineno, "dynamic import"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "sqlite" in node.value.lower():
            found.append((node.lineno, "name"))
    return found


def access_findings(source, rel):
    """Database access rules. Outside the store: no database method at all. Inside: see the module docstring."""
    tree = _with_parents(ast.parse(source))
    inside = rel == STORE
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        method = node.func.attr
        if not inside:
            if method in DB_METHODS:
                found.append((node.lineno, "database method outside the store"))
            continue
        if method in ("executescript", "cursor"):
            found.append((node.lineno, method))
        if method not in EXECUTORS or not node.args:
            continue
        function = _enclosing_function(node)
        arg = node.args[0]
        literal = _text(arg)
        if literal is None:
            if not (isinstance(arg, ast.Name) and (function, arg.id) in NAMED_SQL_VARIABLES):
                found.append((node.lineno, "executor argument is not a literal"))
            continue
        if not READ_ONLY.match(literal) and function not in TXN_OWNERS and not _in_write_transaction(node):
            found.append((node.lineno, "statement that changes state outside write_transaction"))
    return found


def package_sources():
    files = sorted((ROOT / "shit").rglob("*.py"))
    return [(f.relative_to(ROOT).as_posix(), f.read_text(encoding="utf-8")) for f in files]


class PackageTests(unittest.TestCase):
    def test_lint_collects_the_package(self):
        sources = package_sources()
        self.assertGreater(len(sources), 15)
        self.assertIn(STORE, {rel for rel, _ in sources})
        self.assertIn("shit/adapters/claude.py", {rel for rel, _ in sources})

    def test_no_module_builds_sql_from_text(self):
        for rel, source in package_sources():
            with self.subTest(module=rel):
                self.assertEqual(sql_findings(source, rel, BUILDERS), [])

    def test_builders_exist_so_the_allowlists_cannot_rot(self):
        tree = ast.parse((ROOT / STORE).read_text(encoding="utf-8"))
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for rel, name in BUILDERS:
            self.assertIn(name, defined)
        for name in TXN_OWNERS | {fn for fn, _ in NAMED_SQL_VARIABLES}:
            self.assertIn(name, defined)

    def test_only_the_store_imports_sqlite_and_nobody_imports_dynamically(self):
        for rel, source in package_sources():
            with self.subTest(module=rel):
                found = import_findings(source)
                if rel == STORE:
                    self.assertIn("import", {kind for _, kind in found})  # the one allowed importer
                    self.assertNotIn("importlib", {kind for _, kind in found})
                    self.assertNotIn("dynamic import", {kind for _, kind in found})
                else:
                    self.assertEqual(found, [])

    def test_no_module_but_the_store_touches_the_database(self):
        for rel, source in package_sources():
            if rel != STORE:
                with self.subTest(module=rel):
                    self.assertEqual(access_findings(source, rel), [])

    def test_store_executes_literals_and_writes_only_in_transactions(self):
        source = (ROOT / STORE).read_text(encoding="utf-8")
        self.assertEqual(access_findings(source, STORE), [])
        # Something was collected: the rule saw executor calls.
        self.assertGreater(source.count(".execute("), 10)


class BiteTests(unittest.TestCase):
    def test_sql_text_building_is_caught(self):
        bad = (
            'conn.execute(f"SELECT * FROM t WHERE a = {x}")', 'q = f"DELETE FROM t WHERE a = {x}"',
            'q = "SELECT * FROM t WHERE a = %s" % x', 'q = "UPDATE t SET a = {}".format(x)',
            'q = "SELECT * FROM t WHERE a = " + x', 'q = "SELECT a " + "FROM t " + x',
        )
        for source in bad:
            with self.subTest(source=source):
                self.assertTrue(sql_findings(source), source)

    def test_clean_sql_is_allowed(self):
        for source in ('conn.execute("SELECT * FROM t WHERE a = ?", (x,))', 'msg = "released %s" % name',
                       'msg = f"hello {name}"', "conn.execute(STATEMENT)"):
            with self.subTest(source=source):
                self.assertEqual(sql_findings(source), [], source)

    def test_builder_allowlist_is_scoped_to_its_function(self):
        source = 'def schema_statements():\n    return "CREATE TABLE " + name\n\ndef other():\n    return "SELECT " + x\n'
        found = sql_findings(source, STORE, BUILDERS)
        self.assertEqual([line for line, _ in found], [5])

    def test_every_import_route_is_caught(self):
        for source in ("import sqlite3", "import sqlite3 as db", "from sqlite3 import connect", "import _sqlite3",
                       "import importlib", "from importlib import import_module",
                       'importlib.import_module("sqlite" + "3")', '__import__("sqlite" + "3")',
                       'x = "sqlite3"', "f = import_module"):
            with self.subTest(source=source):
                self.assertTrue(import_findings(source), source)
        self.assertEqual(import_findings("import json\nfrom os import path"), [])

    def test_database_methods_outside_the_store_are_caught(self):
        for source in ("sql = user_input\nconn.execute(sql)", 'conn.execute("SELECT 1")', "conn.cursor()",
                       'def write(conn):\n    conn.execute("INSERT INTO tasks VALUES (?)", row)',
                       "conn.executescript(text)", "conn.commit()", "conn.backup(other)"):
            with self.subTest(source=source):
                self.assertTrue(access_findings(source, "shit/other.py"), source)
        self.assertEqual(access_findings("rows = [a.append(1) for a in x]", "shit/other.py"), [])

    def test_inside_the_store_each_rule_bites(self):
        bad = (
            "conn.executescript(text)",
            "cur = conn.cursor()",
            "def f(conn, text):\n    conn.execute(text)",
            "def f(conn, text):\n    conn.execute(make(text))",
            'def f(conn):\n    conn.execute("INSERT INTO tasks VALUES (?)", row)',
            'def f(conn):\n    conn.execute("UPDATE tasks SET state = ?", row)',
            'def f(conn):\n    conn.execute("DELETE FROM tasks")',
            'def f(conn):\n    conn.execute("PRAGMA user_version = 3")',
            'def f(conn):\n    conn.execute("BEGIN")',
            'def f(conn):\n    with other(conn):\n        conn.execute("INSERT INTO tasks VALUES (?)", row)',
        )
        for source in bad:
            with self.subTest(source=source):
                self.assertTrue(access_findings(source, STORE), source)

    def test_inside_the_store_allowed_shapes_pass(self):
        good = (
            'def f(conn):\n    return conn.execute("SELECT count(*) FROM tasks").fetchone()',
            'def f(conn):\n    return conn.execute("PRAGMA user_version").fetchone()',
            'def f(conn):\n    with write_transaction(conn):\n        conn.execute("INSERT INTO tasks VALUES (?)", row)',
            'def f(conn):\n    with store.write_transaction(conn) as c:\n        c.execute("DELETE FROM edges")',
            'def _install_schema(conn):\n    for sql in statements():\n        conn.execute(sql)',
            'def _open_once(conn):\n    conn.execute("PRAGMA foreign_keys = ON")',
        )
        for source in good:
            with self.subTest(source=source):
                self.assertEqual(access_findings(source, STORE), [], source)


if __name__ == "__main__":
    unittest.main()
