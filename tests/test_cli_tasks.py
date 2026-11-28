"""Command line for the task commands (issue #11)."""

import io
import json
import tempfile
import unittest
from pathlib import Path

from _bootstrap import make_git_dir
from _helpers import BRIEF, CLEANUP, REPORT

from shit import (
    artifacts,
    audit_export,
    cli_tasks,
    commands,
    file_backend_state,
    log,
    project,
    queue,
    store,
    store_queue,
    task,
)


class Cli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        self.repo.mkdir()
        make_git_dir(self.repo / ".git")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli_tasks.main(list(argv), cwd=str(self.repo), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual((code, err), (0, ""), argv)
        return json.loads(out)

    def new_task(self):
        self.run_cli("init")
        self.ok(
            "task",
            "create",
            "t1",
            "--title",
            "T",
            "--summary",
            "S",
            "--owner",
            "human",
            "--actor",
            "human",
        )

    def test_whole_flow_by_hand(self):
        self.new_task()
        self.ok(
            "task", "move", "t1", "running", "--actor", "wrapper", "--reason", "start"
        )
        (self.repo / "n.txt").write_text("x")
        self.ok(
            "evidence",
            "add",
            "t1",
            "ev1",
            "--path",
            "n.txt",
            "--description",
            "d",
            "--actor",
            "wrapper",
        )
        self.ok(
            "report",
            "add",
            "t1",
            "r1",
            "--outcome",
            "success",
            "--summary",
            "s",
            "--evidence",
            "ev1",
            "--actor",
            "wrapper",
        )
        self.ok(
            "task", "move", "t1", "review", "--actor", "wrapper", "--reason", "done"
        )
        self.ok(
            "review",
            "add",
            "t1",
            "v1",
            "--report",
            "r1",
            "--verdict",
            "pass",
            "--summary",
            "s",
            "--actor",
            "human",
        )
        self.assertEqual(
            self.run_cli("close", "t1", "--actor", "human", "--reason", "x")[0], 4
        )
        self.ok(
            "decision",
            "add",
            "t1",
            "d1",
            "--status",
            "accepted",
            "--question",
            "q",
            "--choice",
            "c",
            "--rationale",
            "r",
            "--actor",
            "human",
        )
        self.ok("task", "move", "t1", "approved", "--actor", "human", "--reason", "ok")
        self.assertEqual(
            self.ok("close", "t1", "--actor", "human", "--reason", "x")["state"],
            "closed",
        )
        self.assertEqual(self.ok("decision", "show", "d1")["status"], "accepted")
        self.assertEqual(self.ok("task", "show", "t1")["state"], "closed")

    def test_sqlite_backend_whole_flow_and_unified_store_commands(self):
        (self.repo / "config.toml").write_text("""schema_version = 1
[project]
name = "example-project"
[tracker]
repository = "example-owner/example-repo"
[review]
destination = "reviewer"
[storage]
root = ".shit"
[log]
file = ".shit/events.jsonl"
[local_model]
context_tokens = 32768
[store]
backend = "sqlite"
""")
        self.assertEqual(self.ok("init", "--backend", "sqlite"), {"initialized": True})
        self.ok(
            "task",
            "create",
            "t1",
            "--title",
            "T",
            "--summary",
            "S",
            "--owner",
            "human",
            "--actor",
            "human",
        )
        self.ok(
            "task", "move", "t1", "running", "--actor", "wrapper", "--reason", "start"
        )
        (self.repo / "n.txt").write_text("x")
        self.ok(
            "evidence",
            "add",
            "t1",
            "ev1",
            "--path",
            "n.txt",
            "--description",
            "d",
            "--actor",
            "wrapper",
        )
        self.ok(
            "report",
            "add",
            "t1",
            "r1",
            "--outcome",
            "success",
            "--summary",
            "s",
            "--evidence",
            "ev1",
            "--actor",
            "wrapper",
        )
        self.ok(
            "task", "move", "t1", "review", "--actor", "wrapper", "--reason", "done"
        )
        self.ok(
            "review",
            "add",
            "t1",
            "v1",
            "--report",
            "r1",
            "--verdict",
            "pass",
            "--summary",
            "s",
            "--actor",
            "human",
        )
        self.assertEqual(
            self.run_cli("close", "t1", "--actor", "human", "--reason", "x")[0], 4
        )
        self.ok(
            "decision",
            "add",
            "t1",
            "d1",
            "--status",
            "accepted",
            "--question",
            "q",
            "--choice",
            "c",
            "--rationale",
            "r",
            "--actor",
            "human",
        )
        self.ok("task", "move", "t1", "approved", "--actor", "human", "--reason", "ok")
        self.assertEqual(
            self.ok("close", "t1", "--actor", "human", "--reason", "x")["state"],
            "closed",
        )
        self.assertEqual(self.ok("decision", "show", "d1")["status"], "accepted")
        self.assertEqual(self.ok("task", "show", "t1")["state"], "closed")
        self.ok(
            "task",
            "create",
            "queued",
            "--title",
            "Queued",
            "--summary",
            "Queue flow",
            "--owner",
            "human",
            "--actor",
            "human",
        )
        proj = project.Project.open(self.repo)
        try:
            store_queue.enqueue(proj, "queued", "human")
            lease = store_queue.claim(
                proj,
                {"task_id": "queued", "step": "draft", "worker": "worker", "worker_kind": "human"},
            )
            presented = {
                "task_id": lease["task_id"],
                "lease_id": lease["lease_id"],
                "generation": lease["generation"],
            }
            store_queue.start(proj, presented)
            store_queue.submit(proj, presented)
        finally:
            proj.close()
        for task_id in ("t1", "queued"):
            for directory in ("briefs", "reports", "cleanup"):
                (self.repo / ".shit" / directory).mkdir(exist_ok=True)
            (self.repo / ".shit" / "briefs" / f"{task_id}.md").write_text(BRIEF)
            (self.repo / ".shit" / "reports" / f"{task_id}.md").write_text(REPORT)
            (self.repo / ".shit" / "cleanup" / f"{task_id}.md").write_text(CLEANUP)
        code, out, err = self.run_cli("check")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("store: queue 1 task (review 1)", out)
        proj = project.Project.open(self.repo)
        try:
            self.assertEqual(file_backend_state.present(proj), [])
        finally:
            proj.close()
        self.assertEqual(self.ok("init", "--backend", "sqlite"), {"initialized": True})
        code, out, err = self.run_cli("audit", "verify")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("audit-export: ok", out)

    def test_default_init_creates_sqlite_store(self):
        self.assertEqual(self.ok("init"), {"initialized": True})
        self.assertTrue((self.repo / ".shit" / "store.sqlite3").exists())

    def test_removed_file_backend_option_is_refused(self):
        code, _out, err = self.run_cli("init", "--backend", "files")
        self.assertEqual(code, 2)
        self.assertIn("file protocol was removed", err)
        self.assertIn("start a new store", err)
        self.assertFalse((self.repo / ".shit" / "store.sqlite3").exists())

    def test_sqlite_init_refuses_existing_file_backend_state(self):
        (self.repo / ".shit").mkdir()
        (self.repo / ".shit" / "events.jsonl").write_text("state")
        (self.repo / "config.toml").write_text("""schema_version = 1
[project]
name = "example-project"
[tracker]
repository = "example-owner/example-repo"
[review]
destination = "reviewer"
[storage]
root = ".shit"
[log]
file = ".shit/events.jsonl"
[local_model]
context_tokens = 32768
[store]
backend = "sqlite"
""")
        code, _out, err = self.run_cli("init", "--backend", "sqlite")
        self.assertEqual(
            (code, err),
            (9, "shit: found file-protocol state (events.jsonl); file protocol was removed; start a new store\n"),
        )
        self.assertFalse((self.repo / ".shit" / "store.sqlite3").exists())

    def test_file_backend_artifact_catalog_is_derived_from_its_definitions(self):
        file_only = {
            log.LOG_PATH,
            log.CHAIN_PATH,
            task.PENDING,
            queue.QUEUE,
            queue.CHAIN,
            queue.DEPS,
            queue.LEGACY_GRAPH,
            queue.PENDING,
            queue.ANCHOR,
            queue.VIEW,
            queue.LEASES,
        }
        shared = {
            project.LOCK_NAME,
            *project.SUBDIRS,
            *commands.DIRS.values(),
        }
        sqlite_only = {store.DB_NAME, audit_export.EXPORT_PATH}
        self.assertEqual(set(file_backend_state.FILE_ONLY_ARTIFACTS), file_only)
        self.assertEqual(set(file_backend_state.SHARED_ARTIFACTS), shared)
        self.assertEqual(set(file_backend_state.SQLITE_ONLY_ARTIFACTS), sqlite_only)
        self.assertEqual(set(cli_tasks.FILE_BACKEND_ARTIFACTS), file_only)

    def test_legacy_dependency_file_alone_is_file_backend_state(self):
        self.run_cli("init")
        (self.repo / ".shit" / queue.LEGACY_GRAPH).write_text("{}")
        proj = project.Project.open(self.repo)
        try:
            self.assertEqual(file_backend_state.present(proj), [queue.LEGACY_GRAPH])
        finally:
            proj.close()

    def test_sqlite_init_refuses_every_state_bearing_file_backend_artifact(self):
        (self.repo / "config.toml").write_text("""schema_version = 1
[project]
name = "example-project"
[tracker]
repository = "example-owner/example-repo"
[review]
destination = "reviewer"
[storage]
root = ".shit"
[log]
file = ".shit/events.jsonl"
[local_model]
context_tokens = 32768
[store]
backend = "sqlite"
""")
        state = self.repo / ".shit"
        state.mkdir()
        for name in file_backend_state.FILE_ONLY_ARTIFACTS:
            with self.subTest(name=name):
                path = state / name
                if name in {queue.DEPS, queue.LEASES}:
                    path.mkdir(exist_ok=True)
                    (path / "state.json").write_text("{}")
                else:
                    path.write_text("state")
                code, _out, err = self.run_cli("init", "--backend", "sqlite")
                self.assertEqual(code, 9)
                self.assertIn(f"found file-protocol state ({name})", err)
                self.assertIn("file protocol was removed; start a new store", err)
                self.assertFalse((state / "store.sqlite3").exists())
                if path.is_dir():
                    (path / "state.json").unlink()
                    if name not in project.SUBDIRS:
                        path.rmdir()
                else:
                    path.unlink()

    def test_creation_and_record_writer_paths_refuse_every_file_backend_artifact(self):
        state = self.repo / ".shit"
        state.mkdir()
        cases = (
            ("init",),
            ("task", "create", "t1", "--title", "T", "--summary", "S", "--owner", "human", "--actor", "human"),
            ("decision", "add", "t1", "d1", "--actor", "human", "--status", "accepted",
             "--question", "Q", "--choice", "C", "--rationale", "R"),
        )
        for name in file_backend_state.FILE_ONLY_ARTIFACTS:
            artifact = state / name
            if name in {queue.DEPS, queue.LEASES}:
                artifact.mkdir(exist_ok=True)
                marker = artifact / "state.json"
                marker.write_text("{}")
            else:
                artifact.write_text("state")
                marker = artifact
            for argv in cases:
                with self.subTest(name=name, argv=argv):
                    code, _out, err = self.run_cli(*argv)
                    self.assertEqual(code, 9)
                    self.assertIn(f"found file-protocol state ({name})", err)
                    self.assertFalse((state / store.DB_NAME).exists())
            marker.unlink()
            if artifact.is_dir() and name not in project.SUBDIRS:
                artifact.rmdir()

    def test_every_store_command_class_refuses_mixed_backend_state(self):
        self.new_task()
        cases = (
            ("init",),
            ("task", "show", "t1"),
            ("evidence", "show", "missing"),
            ("report", "show", "missing"),
            ("review", "show", "missing"),
            ("decision", "show", "missing"),
            ("close", "t1", "--actor", "human", "--reason", "x"),
            ("audit", "export"),
            ("audit", "verify", "--database"),
        )
        for name in file_backend_state.FILE_ONLY_ARTIFACTS:
            artifact = self.repo / ".shit" / name
            if name in {queue.DEPS, queue.LEASES}:
                artifact.mkdir(exist_ok=True)
                state = artifact / "state.json"
                state.write_text("{}")
            else:
                artifact.write_text("state")
                state = artifact
            message = (
                f"shit: found file-protocol state ({name}); "
                "file protocol was removed; start a new store\n"
            )
            for argv in cases:
                with self.subTest(name=name, argv=argv):
                    code, out, err = self.run_cli(*argv)
                    self.assertEqual((code, out, err), (9, "", message))
            state.unlink()
            if artifact.is_dir() and name not in project.SUBDIRS:
                artifact.rmdir()

    def test_pure_sqlite_project_is_unaffected_by_mixed_state_guard(self):
        self.new_task()
        code, out, err = self.run_cli("task", "show", "t1")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["id"], "t1")

    def test_model_cannot_approve_or_accept(self):
        self.new_task()
        code, _, err = self.run_cli(
            "decision",
            "add",
            "t1",
            "d1",
            "--status",
            "accepted",
            "--question",
            "q",
            "--choice",
            "c",
            "--rationale",
            "r",
            "--actor",
            "model",
        )
        self.assertEqual((code, err), (10, "shit: human approval required\n"))
        self.ok(
            "task",
            "move",
            "t1",
            "blocked",
            "--actor",
            "human",
            "--reason",
            "r",
            "--blocked-reason",
            "x",
            "--blocking-item",
            "y",
        )

    def test_usage_errors_are_2_and_never_echo_input(self):
        secret = "S3CRET-VALUE"
        cases = (
            ["nope", secret],
            ["task", "create", secret],
            ["task", "move", "t1", secret, "--actor", "human", "--reason", "r"],
            ["task", "show"],
            ["decision", "add", "t1", "d1", "--actor", secret],
            [],
            ["task", "--bogus", secret],
        )
        for argv in cases:
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual((out, err), ("", "shit: usage error\n"))

    def test_abbreviated_option_is_usage_error(self):
        self.run_cli("init")
        code, _, _ = self.run_cli(
            "task",
            "create",
            "t1",
            "--tit",
            "T",
            "--summary",
            "S",
            "--owner",
            "human",
            "--actor",
            "human",
        )
        self.assertEqual(code, 2)

    def test_errors_print_only_fixed_text(self):
        marker = "zz-marker-zz"
        self.run_cli("init")
        cases = (
            (["task", "show", marker], 9),
            (
                [
                    "evidence",
                    "add",
                    "t1",
                    "e",
                    "--path",
                    f"../{marker}",
                    "--description",
                    "d",
                    "--actor",
                    "human",
                ],
                5,
            ),
            (["task", "show", "../" + marker], 3),
        )
        for argv, code in cases:
            c, out, err = self.run_cli(*argv)
            self.assertEqual(c, code, argv)
            self.assertNotIn(marker, out + err)
            self.assertNotIn(str(self.repo), out + err)

    def test_no_store_is_9(self):
        self.assertEqual(self.run_cli("task", "show", "t1")[0], 9)

    def test_output_is_ascii_escaped(self):
        self.run_cli("init")
        out = self.ok(
            "task",
            "create",
            "t1",
            "--title",
            "café",
            "--summary",
            "S",
            "--owner",
            "human",
            "--actor",
            "human",
        )
        self.assertEqual(out["title"], "café")
        _, text, _ = self.run_cli("task", "show", "t1")
        self.assertTrue(text.isascii())

    def test_control_characters_in_arguments_rejected_3(self):
        self.run_cli("init")
        code, _, _ = self.run_cli(
            "task",
            "create",
            "t1",
            "--title",
            "a\x1b[2Jb",
            "--summary",
            "S",
            "--owner",
            "human",
            "--actor",
            "human",
        )
        self.assertEqual(code, 3)

    def test_modules_import_no_process_or_net_modules(self):
        for mod in (cli_tasks, commands, artifacts):
            src = Path(mod.__file__).read_text()
            for word in ("socket", "subprocess", "urllib", "http"):
                self.assertNotIn(f"import {word}", src)


if __name__ == "__main__":
    unittest.main()
