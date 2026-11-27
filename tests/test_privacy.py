"""Privacy scan tests (issue #13): generic rules, the private deny list, and hostile trees."""

import os
import shutil
import time
import unittest
from unittest import mock

from _helpers import (
    FAKE_AWS,
    FAKE_GITHUB,
    FAKE_JWT,
    FAKE_KEY_HEADER,
    FAKE_SESSION,
    FAKE_SLACK,
    RepoCase,
    run_main,
)

from shit import privacy

SENTINEL = "zq-private-value-9931"


class Scan(RepoCase):
    def scan(self, *extra, env=None):
        with mock.patch.dict(os.environ, {k: v for k, v in (env or {}).items()}, clear=False):
            return run_main(privacy.main, ["--repo", str(self.repo), *extra])

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class GenericRules(Scan):
    def test_clean_tree_exits_zero(self):
        self.write("a.txt", "nothing to see\n")
        self.assertEqual(self.scan()[0], 0)

    def test_each_rule_reports_path_and_line_only(self):
        cases = {
            "gh.txt": FAKE_GITHUB, "aws.txt": FAKE_AWS, "key.txt": FAKE_KEY_HEADER, "slack.txt": FAKE_SLACK,
            "jwt.txt": FAKE_JWT, "sess.txt": "see " + FAKE_SESSION,
            "url.txt": "https://user:" + "hunter2hunter2" + "@example.invalid/x",
            "cfg.txt": "password = " + "correcthorse99",
        }
        for name, secret in cases.items():
            with self.subTest(name=name):
                self.write(name, "line one\nlast: " + secret + "\n")
                code, out, err = self.scan()
                self.assertEqual(code, 6)
                self.assertIn(f"{name}:2", out)
                self.assertNotIn(secret, out + err)
                (self.repo / name).unlink()

    def test_placeholder_assignment_is_not_a_hit(self):
        self.write("a.txt", "password: <your-password-here>\n")
        self.assertEqual(self.scan()[0], 0)

    def test_filename_rules(self):
        for name in (".env", ".env.local", "id_rsa", "server.pem", "credentials.json", ".netrc", "x.p12"):
            with self.subTest(name=name):
                self.write(name, "harmless\n")
                code, out, _ = self.scan()
                self.assertEqual(code, 6)
                self.assertIn(f"{name}:0", out)
                (self.repo / name).unlink()
        self.write(".env.example", "harmless\n")
        self.assertEqual(self.scan()[0], 0)

    def test_hit_never_logged_to_store(self):
        self.write("gh.txt", FAKE_GITHUB + "\n")
        before = self.snapshot()
        self.scan()
        self.assertEqual(self.snapshot(), before)

    def test_zero_width_evasion_still_hits(self):
        self.write("gh.txt", FAKE_GITHUB[:6] + chr(0x200B) + FAKE_GITHUB[6:] + "\n")
        self.assertEqual(self.scan()[0], 6)


class DenyList(Scan):
    def deny(self, text="# comment\n\nfirst-term\n" + SENTINEL + "\n", **kw):
        return self.deny_file(text, **kw)

    def test_off_by_default(self):
        self.write("a.txt", SENTINEL + "\n")
        self.assertEqual(self.scan()[0], 0)

    def test_hit_prints_entry_number_not_value(self):
        path = self.deny()
        self.write("a.txt", "x\nsome " + SENTINEL.upper() + " here\n")
        code, out, err = self.scan("--deny-file", str(path))
        self.assertEqual(code, 6)
        self.assertIn("a.txt:2 deny-entry 2", out)
        self.assertNotIn(SENTINEL, (out + err).lower())

    def test_path_from_environment(self):
        path = self.deny()
        self.write("a.txt", SENTINEL + "\n")
        code, out, _ = self.scan(env={"SHIT_DENY_FILE": str(path)})
        self.assertEqual((code, "deny-entry 2" in out), (6, True))

    def test_evasions(self):
        path = self.deny("secret-name-xyz\n")
        fullwidth = "".join(chr(ord(c) + 0xFEE0) if c.isalpha() else c for c in "secret") + "-name-xyz"
        for text in ("sec" + chr(0x200B) + "ret-name-xyz", "SECRET-NAME-XYZ", fullwidth,
                     "secret-name-" + chr(0x301) + "xyz"):
            with self.subTest(text=text):
                self.write("a.txt", text + "\n")
                self.assertEqual(self.scan("--deny-file", str(path))[0], 6)

    def test_value_in_file_name_is_withheld(self):
        path = self.deny()
        self.write(f"dir/{SENTINEL}.txt", "plain\n")
        code, out, err = self.scan("--deny-file", str(path))
        self.assertEqual(code, 6)
        self.assertNotIn(SENTINEL, (out + err).lower())
        self.assertIn("deny-entry 2", out)

    def test_directory_named_with_deny_value_is_a_hit(self):
        path = self.deny()
        self.write(f"{SENTINEL}/ok.txt", "plain\n")
        code, out, err = self.scan("--deny-file", str(path))
        self.assertEqual(code, 6)
        self.assertIn("deny-entry 2", out)
        self.assertNotIn(SENTINEL, (out + err).lower())

    def test_directory_with_credential_like_name_is_a_hit(self):
        for name in ("server.pem", ".env", "id_rsa", FAKE_GITHUB):
            with self.subTest(name=name):
                self.write(f"{name}/ok.txt", "plain\n")
                code, out, err = self.scan()
                self.assertEqual(code, 6)
                self.assertNotIn(FAKE_GITHUB, out + err)
                shutil.rmtree(self.repo / name)

    def test_group_readable_file_refused(self):
        path = self.deny(mode=0o640)
        self.assertEqual(self.scan("--deny-file", str(path))[0], 5)

    def test_world_readable_file_refused_and_value_not_printed(self):
        path = self.deny(mode=0o644)
        code, out, err = self.scan("--deny-file", str(path))
        self.assertEqual(code, 5)
        self.assertNotIn(SENTINEL, out + err)

    def test_file_inside_repository_refused(self):
        inside = self.repo / "deny.txt"
        inside.write_text(SENTINEL + "\n")
        os.chmod(inside, 0o600)
        self.assertEqual(self.scan("--deny-file", str(inside))[0], 5)

    def test_symlinked_file_refused(self):
        real = self.deny()
        link = self.outside / "link.txt"
        link.symlink_to(real)
        self.assertEqual(self.scan("--deny-file", str(link))[0], 5)

    def test_missing_file_is_precondition(self):
        self.assertEqual(self.scan("--deny-file", str(self.outside / "nope.txt"))[0], 9)

    def test_fifo_refused_without_blocking(self):
        fifo = self.outside / "fifo"
        os.mkfifo(fifo, 0o600)
        self.assertEqual(self.scan("--deny-file", str(fifo))[0], 5)

    def test_oversize_and_too_short_entries(self):
        self.assertEqual(self.scan("--deny-file", str(self.deny("x" * 100000, name="big")))[0], 7)
        self.assertEqual(self.scan("--deny-file", str(self.deny("ab\n", name="short")))[0], 3)

    def test_invalid_utf8_rejected(self):
        path = self.outside / "bad.txt"
        path.write_bytes(b"\xff\xfe\xfa\n")
        os.chmod(path, 0o600)
        self.assertEqual(self.scan("--deny-file", str(path))[0], 3)


class DenyPathResolution(Scan):
    """The deny path is resolved once; the open never follows a symlink (no check/open race)."""

    def put(self, directory, text):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "deny.txt"
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def test_race_retarget_to_repo_never_reads_the_repo_file(self):
        self.put(self.outside, "outside-term\n")
        self.put(self.repo / "inner", "repo-term\n")
        race = self.base / "race"
        race.symlink_to(self.outside)
        real_realpath = os.path.realpath
        calls = []

        def retarget(target, *args, **kw):
            result = real_realpath(target, *args, **kw)
            if not calls and str(target).startswith(str(race)):
                calls.append(1)  # the check has resolved the path; now swing the link
                race.unlink()
                race.symlink_to(self.repo / "inner")
            return result

        with mock.patch.object(os.path, "realpath", retarget):
            try:
                terms = privacy.load_deny(str(race / "deny.txt"), str(self.repo))
            except privacy.ShitError:
                terms = ()
        self.assertEqual(calls, [1])
        self.assertNotIn("repo-term", terms)

    def test_component_swapped_for_symlink_at_open_time_is_refused(self):
        path = self.put(self.outside, "outside-term\n")
        moved = self.base / "moved"
        real_open = os.open
        swapped = []

        def swap(name, *args, **kw):
            if not swapped and name == "outside" and kw.get("dir_fd") is not None:
                swapped.append(1)
                os.rename(self.outside, moved)
                self.outside.symlink_to(self.repo)
            return real_open(name, *args, **kw)

        with mock.patch.object(privacy.os, "open", swap):
            with self.assertRaises(privacy.ShitError) as ctx:
                privacy.load_deny(str(path), str(self.repo))
        self.assertEqual(swapped, [1])
        self.assertEqual(ctx.exception.code, privacy.CONFINEMENT)

    def test_static_symlinked_parent_into_repo_refused(self):
        self.put(self.repo / "inner", "repo-term\n")
        link = self.base / "link"
        link.symlink_to(self.repo / "inner")
        with self.assertRaises(privacy.ShitError) as ctx:
            privacy.load_deny(str(link / "deny.txt"), str(self.repo))
        self.assertEqual(ctx.exception.code, privacy.CONFINEMENT)

    def test_stable_symlinked_parent_outside_the_repo_works(self):
        self.put(self.outside, "outside-term\n")
        link = self.base / "link"
        link.symlink_to(self.outside)
        self.assertEqual(privacy.load_deny(str(link / "deny.txt"), str(self.repo)), ["outside-term"])

    def test_relative_path_with_changed_cwd(self):
        self.put(self.outside, "outside-term\n")
        old = os.getcwd()
        os.chdir(self.base)
        self.addCleanup(os.chdir, old)
        terms = privacy.load_deny("outside/deny.txt", str(self.repo))
        os.chdir(self.repo)
        self.assertEqual(terms, ["outside-term"])
        with self.assertRaises(privacy.ShitError):
            privacy.load_deny("../outside/../repo/.shit", str(self.repo))

    def test_final_symlink_refused(self):
        real = self.put(self.outside, "outside-term\n")
        link = self.outside / "alias.txt"
        link.symlink_to(real)
        with self.assertRaises(privacy.ShitError):
            privacy.load_deny(str(link), str(self.repo))


class HostileTrees(Scan):
    def test_symlink_in_tree_is_confinement(self):
        (self.repo / "link").symlink_to(self.outside)
        self.assertEqual(self.scan()[0], 5)

    def test_fifo_in_tree_does_not_block(self):
        os.mkfifo(self.repo / "pipe")
        start = time.monotonic()
        self.assertEqual(self.scan()[0], 5)
        self.assertLess(time.monotonic() - start, 5)

    def test_oversize_file_is_input_limit(self):
        with open(self.repo / "huge.bin", "wb") as fh:
            fh.truncate(privacy.MAX_FILE_BYTES + 1)
        self.assertEqual(self.scan()[0], 7)

    def test_git_dir_is_not_scanned(self):
        (self.repo / ".git" / "note").write_text(FAKE_GITHUB + "\n")
        self.assertEqual(self.scan()[0], 0)

    def test_hostile_file_name_is_escaped(self):
        bidi = chr(0x202E)
        name = "bad\nname\x1b[31m" + bidi + ".txt"
        self.write(name, FAKE_GITHUB + "\n")
        code, out, _ = self.scan()
        self.assertEqual(code, 6)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertNotIn("\x1b", out)
        self.assertNotIn(bidi, out)

    def test_binary_and_long_line_are_bounded(self):
        (self.repo / "blob").write_bytes(os.urandom(200000))
        self.write("long.txt", "a" * 2_000_000 + "\n")
        start = time.monotonic()
        code = self.scan()[0]
        self.assertLess(time.monotonic() - start, 20)
        self.assertIn(code, (0, 6))

    def test_file_count_is_bounded(self):
        with mock.patch.object(privacy, "MAX_FILES", 3):
            for i in range(5):
                self.write(f"f{i}.txt", "x\n")
            self.assertEqual(self.scan()[0], 7)

    def test_depth_is_bounded(self):
        with mock.patch.object(privacy, "MAX_DEPTH", 3):
            self.write("a/b/c/d/e/f.txt", "x\n")
            self.assertEqual(self.scan()[0], 7)

    def test_unknown_option_is_usage_and_not_echoed(self):
        code, out, err = run_main(privacy.main, ["--repo", str(self.repo), "--" + SENTINEL])
        self.assertEqual(code, 2)
        self.assertNotIn(SENTINEL, out + err)

    def test_missing_repo_is_precondition(self):
        self.assertEqual(run_main(privacy.main, ["--repo", str(self.outside / "nope")])[0], 9)


if __name__ == "__main__":
    unittest.main()
