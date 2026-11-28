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
# Read-only statements are SELECT and these exact PRAGMA reads (no value, no argument list).
READ_PRAGMAS = {"application_id", "user_version", "foreign_keys", "synchronous", "trusted_schema", "busy_timeout",
                "recursive_triggers", "foreign_key_check", "integrity_check"}
PRAGMA_READ = re.compile(r"\s*pragma\s+([a-z_]+)\s*$", re.IGNORECASE)
SELECT_ONLY = re.compile(r"\s*select\b", re.IGNORECASE)
# Modules that open a path around the checks, names that run text as code, and attributes that reach any attribute.
FORBIDDEN_IMPORTS = {"builtins", "importlib", "operator", "ctypes", "inspect", "sqlite3", "_sqlite3"}
FORBIDDEN_NAMES = {"eval", "exec", "compile", "globals", "locals", "vars", "__import__", "__builtins__", "breakpoint"}
FORBIDDEN_DUNDERS = {"__getattribute__", "__getattr__", "__dict__", "__class__", "__import__", "__builtins__",
                     "__globals__", "__subclasses__", "__mro__", "__bases__", "__code__", "__self__", "__func__",
                     "__setattr__", "__delattr__", "__reduce__", "__reduce_ex__", "__call__"}


def is_read_only(sql):
    match = PRAGMA_READ.match(sql)
    if match:
        return match.group(1).lower() in READ_PRAGMAS
    return bool(SELECT_ONLY.match(sql))
EXECUTORS = {"execute", "executemany", "executescript"}
# Connection and cursor methods that reach the database. Outside the store none may be called.
DB_METHODS = EXECUTORS | {"cursor", "backup", "iterdump", "create_function", "create_aggregate", "create_collation",
                          "set_authorizer", "enable_load_extension", "load_extension", "setconfig", "blobopen",
                          "set_progress_handler", "set_trace_callback", "commit", "rollback"}
DB_NAMES = EXECUTORS | {"cursor"}
REFLECTORS = {"getattr", "hasattr", "setattr", "delattr", "attrgetter", "methodcaller"}
STORE = "shit/store.py"
# The exact functions of the store that may call each database method. C3 and later add to this list on purpose.
ALLOWED_METHODS = {
    "_open_once": {"execute", "setconfig"}, "_install_schema": {"execute"}, "_check_header": {"execute"},
    "_check_settings": {"execute"}, "fingerprint": {"execute"}, "expected_fingerprint": {"execute"},
    "write_transaction": {"execute"}, "_fail_transaction": {"execute"}, "verify": {"execute"},
}
ALLOWED_REFLECTION = {("_open_once", "setconfig")}
CONNECTORS = {"_open_once", "expected_fingerprint"}
# The only functions that may assemble SQL text: schema DDL from this code's own vocabulary.
BUILDERS = {(STORE, name) for name in (
    "schema_statements", "_guard", "_no_pair", "_queue_guards", "_lease_guards", "_chain_guard", "_stamp_check",
    "_slug_check", "_hex_check", "_text_check", "_seconds", "_pair_rows", "_kind_rules")}
# Functions of the store that run their own BEGIN IMMEDIATE / pragmas / scratch in-memory database.
TXN_OWNERS = {"_open_once", "_install_schema", "write_transaction", "_fail_transaction", "expected_fingerprint"}
# A variable may be passed to execute only as the loop variable over `schema_statements()` in these two functions.
SCHEMA_CONSUMERS = {"_install_schema", "expected_fingerprint"}


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


def _is_call_func(node):
    parent = getattr(node, "parent", None)
    return isinstance(parent, ast.Call) and parent.func is node


def _is_schema_loop_variable(arg, function):
    """`arg` is a Name that is the target of an enclosing `for ... in schema_statements()` in a schema consumer."""
    if not isinstance(arg, ast.Name) or function not in SCHEMA_CONSUMERS:
        return False
    node = arg
    while node is not None:
        node = getattr(node, "parent", None)
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == arg.id:
            call = node.iter
            return isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "schema_statements"
    return False


def _reflection_findings(node, function, inside):
    """getattr, hasattr, setattr, delattr, attrgetter and methodcaller reaching a database method by name."""
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    if name not in REFLECTORS:
        return []
    index = 0 if name in ("attrgetter", "methodcaller") else 1
    args = node.args
    if len(args) <= index:
        return [(node.lineno, "reflection without a fixed name")]
    target = _text(args[index])
    if target is None:
        return [(node.lineno, "reflection with a computed name")]
    if target in DB_METHODS and not (inside and (function, target) in ALLOWED_REFLECTION):
        return [(node.lineno, "reflection on a database method")]
    return []


def access_findings(source, rel, allowed=None):
    """Database access rules (see the module docstring). Every database method, however reached, is a finding
    outside the store; inside the store it must be a direct call in one of the functions listed for it."""
    tree = _with_parents(ast.parse(source))
    inside = rel == STORE
    found = []
    for node in ast.walk(tree):
        function = _enclosing_function(node)
        if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            found.append((node.lineno, "from-import of sqlite3"))
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for module in modules:
                root = module.split(".")[0]
                if root in FORBIDDEN_IMPORTS and not (inside and isinstance(node, ast.Import) and root == "sqlite3"):
                    found.append((node.lineno, "import of " + root))
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append((node.lineno, "name that runs text as code or reaches builtins"))
        if isinstance(node, ast.Name) and node.id == "sqlite3" and not inside:
            found.append((node.lineno, "sqlite3 outside the store"))
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_DUNDERS:
            found.append((node.lineno, "attribute that reaches any attribute"))
        if isinstance(node, ast.Attribute) and node.attr == "sqlite3" and not inside:
            found.append((node.lineno, "the store's sqlite3 module reached from outside"))
        if inside and isinstance(node, ast.Name) and node.id == "schema_statements" and function is not None \
                and function != "schema_statements":
            if function not in SCHEMA_CONSUMERS:
                found.append((node.lineno, "schema statements used outside the two schema consumers"))
            elif not _is_call_func(node):
                found.append((node.lineno, "schema statements referenced without a direct call"))
        if isinstance(node, ast.ImportFrom) and any(a.name in DB_METHODS for a in node.names):
            found.append((node.lineno, "import of a database method"))
        if isinstance(node, ast.Call):
            found.extend(_reflection_findings(node, function, inside))
        if isinstance(node, ast.Subscript) and _text(node.slice) in DB_NAMES:
            found.append((node.lineno, "subscript dispatch to a database method"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in DB_NAMES \
                and not isinstance(getattr(node, "parent", None), ast.Call):
            found.append((node.lineno, "database method name used as data"))
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr == "connect" and isinstance(node.value, ast.Name) and node.value.id == "sqlite3":
            if not (inside and _is_call_func(node) and function in CONNECTORS):
                found.append((node.lineno, "sqlite3.connect outside its functions, or aliased"))
            continue
        if node.attr not in DB_METHODS:
            continue
        if not inside:
            found.append((node.lineno, "database method outside the store"))
            continue
        if not _is_call_func(node):
            found.append((node.lineno, "database method used without being called directly"))
            continue
        if node.attr not in (ALLOWED_METHODS if allowed is None else allowed).get(function, ()):
            found.append((node.lineno, "database method in a function not on the list"))
            continue
        call = node.parent
        if node.attr not in EXECUTORS or not call.args:
            continue
        arg = call.args[0]
        literal = _text(arg)
        if literal is None:
            if not _is_schema_loop_variable(arg, function):
                found.append((node.lineno, "executor argument is not a literal"))
            continue
        if not is_read_only(literal) and function not in TXN_OWNERS and not _in_write_transaction(node):
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
        for name in TXN_OWNERS | SCHEMA_CONSUMERS:
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
        used = {f for f in ALLOWED_METHODS}
        defined = {n.name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)}
        self.assertLessEqual(used, defined)  # the list names real functions only


# A store with later operations added on purpose: functions `f` and the owners may call `execute`.
FUTURE = {**ALLOWED_METHODS, "f": {"execute"}}


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
                self.assertTrue(access_findings(source, STORE, FUTURE), source)

    def test_every_bypass_from_the_review_is_caught(self):
        outside = (
            'getattr(conn, "execute")(user_sql)',
            "run = conn.execute\nrun(text)",
            "list(map(conn.execute, statements))",
            'handlers = {"execute": 1}\nhandlers["execute"]',
            'name = "exec" + "ute"\ngetattr(conn, name)(text)',
            'import operator\noperator.attrgetter("execute")(conn)(text)',
            "from sqlite3 import connect",
            "def go(c, t):\n    return c.execute(t)",
            "from somewhere import executescript",
        )
        for source in outside:
            with self.subTest(source=source):
                self.assertTrue(access_findings(source, "shit/other.py"), source)
        inside = (
            "def unsafe(conn, text):\n    run = conn.execute\n    run(text)",
            'def unsafe(conn, text):\n    getattr(conn, "execute")(text)',
            "def unsafe(path):\n    c = sqlite3.connect\n    return c(path)",
            "def unsafe(path):\n    return sqlite3.connect(path)",
            'def unsafe(conn):\n    return conn.execute("SELECT 1")',
            'def unsafe(conn):\n    return conn["execute"]("SELECT 1")',
            "from sqlite3 import connect",
            'def _open_once(conn):\n    return conn.execute(sql_from_somewhere)',
            'def _open_once(conn, name):\n    return getattr(conn, name)',
        )
        for source in inside:
            with self.subTest(source=source):
                self.assertTrue(access_findings(source, STORE), source)

    def test_round_three_bypasses_are_caught(self):
        outside = (
            "from . import store\nc = store.sqlite3.connect(path)",
            "import sqlite3",
            "from builtins import __import__ as load\ndb = load('sql' + 'ite3')",
            "from builtins import getattr as pick\nrun = pick(conn, 'execute')",
            "run = conn.__getattribute__('exec' + 'ute')",
            "import importlib",
            "import operator",
            "from operator import attrgetter as grab",
            "eval(text)",
            "exec(text)",
            "x = vars(conn)",
            "y = conn.__dict__",
            "z = type(conn).__mro__",
        )
        for source in outside:
            with self.subTest(source=source):
                self.assertTrue(access_findings(source, "shit/other.py"), source)

    def test_function_form_pragmas_are_not_read_only(self):
        for sql in ("PRAGMA foreign_keys(OFF)", "PRAGMA writable_schema(ON)", "PRAGMA foreign_keys = OFF",
                    "PRAGMA user_version = 9", "PRAGMA journal_mode", "PRAGMA writable_schema", "pragma wal_checkpoint",
                    "DELETE FROM tasks", "WITH x AS (SELECT 1) DELETE FROM tasks"):
            with self.subTest(sql=sql):
                self.assertFalse(is_read_only(sql), sql)
        for sql in ("SELECT 1", "select count(*) from tasks", "PRAGMA user_version", "pragma foreign_key_check"):
            with self.subTest(sql=sql):
                self.assertTrue(is_read_only(sql), sql)
        source = 'def fingerprint(conn):\n    return conn.execute("PRAGMA foreign_keys(OFF)")'
        self.assertTrue(access_findings(source, STORE))

    def test_a_variable_is_only_allowed_as_the_schema_loop_variable(self):
        bad = (
            "def _install_schema(conn, rows):\n    for sql in rows:\n        conn.execute(sql)",
            "def _install_schema(conn):\n    sql = make()\n    conn.execute(sql)",
            "def _install_schema(conn):\n    for other in schema_statements():\n        conn.execute(sql)",
            "def verify(conn):\n    for sql in schema_statements():\n        conn.execute(sql)",
            "def _install_schema(conn):\n    run = schema_statements\n    for sql in run():\n        conn.execute(sql)",
        )
        for source in bad:
            with self.subTest(source=source):
                self.assertTrue(access_findings(source, STORE), source)

    def test_install_schema_keeps_its_own_transaction(self):
        tree = ast.parse((ROOT / STORE).read_text(encoding="utf-8"))
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_install_schema")
        literals = {n.value for n in ast.walk(function) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertTrue({"BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"} <= literals)

    def test_inside_the_store_allowed_shapes_pass(self):
        good = (
            'def f(conn):\n    return conn.execute("SELECT count(*) FROM tasks").fetchone()',
            'def f(conn):\n    return conn.execute("PRAGMA user_version").fetchone()',
            'def f(conn):\n    with write_transaction(conn):\n        conn.execute("INSERT INTO tasks VALUES (?)", row)',
            'def f(conn):\n    with store.write_transaction(conn) as c:\n        c.execute("DELETE FROM edges")',
            'def _install_schema(conn):\n    for sql in schema_statements():\n        conn.execute(sql)',
            'def _open_once(conn):\n    conn.execute("PRAGMA foreign_keys = ON")',
        )
        for source in good:
            with self.subTest(source=source):
                self.assertEqual(access_findings(source, STORE, FUTURE), [], source)


if __name__ == "__main__":
    unittest.main()
