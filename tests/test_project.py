"""Confined storage tests (issue #9): traversal, symlinks, special files, limits, atomicity, locks."""

import os
import signal
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _bootstrap import make_git_dir

from shit import project as project_mod
from shit.errors import ShitError
from shit.project import Project, discover_root


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.repo = base / "repo"
        self.repo.mkdir()
        make_git_dir(self.repo / ".git")
        self.outside = base / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "secret.txt"
        self.secret.write_bytes(b"outside-data")
        self.proj = Project.init(self.repo)
        self.addCleanup(self.proj.close)
        self.shit = self.repo / ".shit"

    def code(self, fn, *a, **kw):
        with self.assertRaises(ShitError) as cm:
            fn(*a, **kw)
        return cm.exception.code


class InitAndDiscovery(Base):
    def test_init_creates_private_layout(self):
        self.assertEqual(stat.S_IMODE(self.shit.stat().st_mode), 0o700)
        for sub in ("tasks",):
            self.assertTrue((self.shit / sub).is_dir())
            self.assertEqual(stat.S_IMODE((self.shit / sub).stat().st_mode), 0o700)

    def test_discover_walks_up_to_git_root(self):
        deep = self.repo / "a" / "b"
        deep.mkdir(parents=True)
        self.assertEqual(discover_root(deep), self.repo)

    def test_discover_without_repository_is_precondition_missing(self):
        self.assertEqual(self.code(discover_root, self.outside, ceiling=self.outside), 9)

    def test_init_forces_0700_on_precreated_tasks_directory(self):
        other = self.outside / "r3"
        other.mkdir()
        make_git_dir(other / ".git")
        os.mkdir(other / ".shit", 0o700)
        os.mkdir(other / ".shit" / "tasks", 0o777)
        os.chmod(other / ".shit" / "tasks", 0o777)
        proj = Project.init(other)
        self.addCleanup(proj.close)
        self.assertEqual(stat.S_IMODE((other / ".shit" / "tasks").stat().st_mode), 0o700)

    def test_planted_git_file_is_not_a_repository(self):
        other = self.outside / "planted"
        other.mkdir()
        (other / ".git").write_text("gitdir: /nowhere\n")
        self.assertEqual(self.code(discover_root, other, ceiling=self.outside), 5)

    def test_planted_empty_git_directory_is_not_a_repository(self):
        other = self.outside / "empty"
        other.mkdir()
        (other / ".git").mkdir()
        self.assertEqual(self.code(discover_root, other, ceiling=self.outside), 5)

    def test_symlinked_git_entry_is_not_a_repository(self):
        other = self.outside / "linked"
        other.mkdir()
        os.symlink(self.repo / ".git", other / ".git")
        self.assertEqual(self.code(discover_root, other, ceiling=self.outside), 5)

    def test_gitdir_pointer_to_real_repository_is_accepted(self):
        other = self.outside / "worktree"
        other.mkdir()
        (other / ".git").write_text(f"gitdir: {self.repo / '.git'}\n")
        self.assertEqual(discover_root(other, ceiling=self.outside), other)

    def test_gitdir_pointer_to_non_repository_is_refused(self):
        other = self.outside / "pointer"
        other.mkdir()
        (self.outside / "notrepo").mkdir()
        (other / ".git").write_text(f"gitdir: {self.outside / 'notrepo'}\n")
        self.assertEqual(self.code(discover_root, other, ceiling=self.outside), 5)

    def test_foreign_owned_git_entry_is_refused(self):
        with mock.patch.object(project_mod.os, "getuid", return_value=os.getuid() + 1):
            self.assertEqual(self.code(discover_root, self.repo, ceiling=self.outside), 5)

    def test_open_missing_store_is_precondition_missing(self):
        other = self.outside / "r"
        other.mkdir()
        self.assertEqual(self.code(Project.open, other), 9)

    def test_symlinked_store_is_rejected(self):
        other = self.outside / "r2"
        other.mkdir()
        (self.outside / "real-store").mkdir()
        os.symlink(self.outside / "real-store", other / ".shit")
        self.assertEqual(self.code(Project.open, other), 5)
        self.assertEqual(self.code(Project.init, other), 5)


class Confinement(Base):
    def test_traversal_and_absolute_paths_rejected(self):
        for bad in ("../outside/secret.txt", "tasks/../../x", "/etc/passwd", "", "a\x00b", "tasks//../.."):
            with self.subTest(path=bad):
                self.assertEqual(self.code(self.proj.read, bad), 5)
                self.assertEqual(self.code(self.proj.write_atomic, bad, b"x"), 5)
                self.assertEqual(self.code(self.proj.append, bad, b"x"), 5)
        self.assertEqual(self.secret.read_bytes(), b"outside-data")

    def test_valid_relative_path_works(self):
        self.proj.write_atomic("tasks/ok.json", b"{}")
        self.assertEqual(self.proj.read("tasks/ok.json"), b"{}")

    def test_symlink_leaf_rejected_for_read_write_append(self):
        os.symlink(self.secret, self.shit / "tasks" / "link.json")
        self.assertEqual(self.code(self.proj.read, "tasks/link.json"), 5)
        self.assertEqual(self.code(self.proj.write_atomic, "tasks/link.json", b"pwn"), 5)
        self.assertEqual(self.code(self.proj.append, "tasks/link.json", b"pwn"), 5)
        self.assertEqual(self.secret.read_bytes(), b"outside-data")

    def test_symlinked_directory_component_rejected(self):
        os.symlink(self.outside, self.shit / "tasks" / "dirlink")
        self.assertEqual(self.code(self.proj.read, "tasks/dirlink/secret.txt"), 5)
        self.assertEqual(self.code(self.proj.write_atomic, "tasks/dirlink/new.json", b"pwn"), 5)
        self.assertFalse((self.outside / "new.json").exists())

    def test_symlink_swapped_in_after_precheck_is_rejected_by_open(self):
        target = self.shit / "tasks" / "race.json"
        target.write_bytes(b"{}")
        real = Project._stat_leaf

        def swap(self_, dirfd, name):
            result = real(self_, dirfd, name)
            target.unlink()
            os.symlink(self.secret, target)
            return result

        with mock.patch.object(Project, "_stat_leaf", swap):
            self.assertEqual(self.code(self.proj.read, "tasks/race.json"), 5)

    def test_fifo_rejected_without_blocking(self):
        os.mkfifo(self.shit / "tasks" / "pipe")
        signal.alarm(5)
        try:
            self.assertEqual(self.code(self.proj.read, "tasks/pipe"), 5)
            self.assertEqual(self.code(self.proj.append, "tasks/pipe", b"x"), 5)
            self.assertEqual(self.code(self.proj.write_atomic, "tasks/pipe", b"x"), 5)
        finally:
            signal.alarm(0)

    def test_socket_rejected(self):
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(self.shit / "tasks" / "s"))
        self.assertEqual(self.code(self.proj.read, "tasks/s"), 5)

    def test_directory_where_file_expected_rejected(self):
        (self.shit / "tasks" / "d.json").mkdir()
        self.assertEqual(self.code(self.proj.read, "tasks/d.json"), 5)
        self.assertEqual(self.code(self.proj.write_atomic, "tasks/d.json", b"x"), 5)

    def test_hard_linked_file_rejected(self):
        os.link(self.secret, self.shit / "tasks" / "hard.json")
        self.assertEqual(self.code(self.proj.read, "tasks/hard.json"), 5)
        self.assertEqual(self.code(self.proj.append, "tasks/hard.json", b"pwn"), 5)
        self.assertEqual(self.code(self.proj.write_atomic, "tasks/hard.json", b"pwn"), 5)
        self.assertEqual(self.secret.read_bytes(), b"outside-data")

    def test_missing_file_is_precondition_missing(self):
        self.assertEqual(self.code(self.proj.read, "tasks/none.json"), 9)


class Limits(Base):
    def test_oversized_read_is_input_limit(self):
        (self.shit / "tasks" / "big.json").write_bytes(b"x" * 101)
        self.assertEqual(self.code(self.proj.read, "tasks/big.json", limit=100), 7)

    def test_read_at_limit_ok(self):
        (self.shit / "tasks" / "edge.json").write_bytes(b"x" * 100)
        self.assertEqual(len(self.proj.read("tasks/edge.json", limit=100)), 100)

    def test_oversized_write_is_input_limit(self):
        self.assertEqual(self.code(self.proj.write_atomic, "tasks/w.json", b"x" * 101, limit=100), 7)
        self.assertFalse((self.shit / "tasks" / "w.json").exists())


class Atomicity(Base):
    def test_files_created_private(self):
        self.proj.write_atomic("tasks/p.json", b"{}")
        self.proj.append("tasks/log", b"x\n")
        for rel in ("tasks/p.json", "tasks/log"):
            self.assertEqual(stat.S_IMODE((self.shit / rel).stat().st_mode), 0o600)

    def test_failed_replace_keeps_old_content_and_leaves_no_temp(self):
        self.proj.write_atomic("tasks/a.json", b"old")
        with mock.patch.object(project_mod.os, "rename", side_effect=OSError("boom")):
            self.assertEqual(self.code(self.proj.write_atomic, "tasks/a.json", b"new"), 1)
        self.assertEqual(self.proj.read("tasks/a.json"), b"old")
        self.assertEqual(sorted(p.name for p in (self.shit / "tasks").iterdir()), ["a.json"])

    def test_failed_write_keeps_old_content_and_leaves_no_temp(self):
        self.proj.write_atomic("tasks/a.json", b"old")
        with mock.patch.object(project_mod.os, "fsync", side_effect=OSError("boom")):
            self.assertEqual(self.code(self.proj.write_atomic, "tasks/a.json", b"new"), 1)
        self.assertEqual((self.shit / "tasks" / "a.json").read_bytes(), b"old")
        self.assertEqual(sorted(p.name for p in (self.shit / "tasks").iterdir()), ["a.json"])


class Locking(Base):
    def test_second_holder_times_out(self):
        other = Project.open(self.repo)
        self.addCleanup(other.close)
        with self.proj.locked(timeout=1):
            self.assertEqual(self.code(other.locked(timeout=0.2).__enter__), 1)

    def test_lock_released_after_exit_and_after_error(self):
        other = Project.open(self.repo)
        self.addCleanup(other.close)
        with self.assertRaises(RuntimeError), self.proj.locked(timeout=1):
            raise RuntimeError("x")
        with other.locked(timeout=0.5):
            pass


if __name__ == "__main__":
    unittest.main()
