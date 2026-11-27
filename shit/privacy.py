"""Privacy scan: generic secret patterns, credential-like file names, and a private deny list.

A hit is reported as a path and a line number, plus the deny-list entry number for a deny hit. The
matched text is never printed, logged or stored (docs/exit-codes.md, TM-08). A file name that itself
matches is shown as `<withheld>`.

The deny list is optional and off by default. It is a UTF-8 file of one term per line (blank lines and
lines starting with `#` are ignored). Its path comes from configuration only: `--deny-file`, else the
SHIT_DENY_FILE environment variable. The file must sit outside the repository, be a regular
single-link file you own, and have mode 0600. Matching ignores case, width, combining marks, format
and control characters, and runs of whitespace, so inserted zero-width characters do not evade it.

The scan walks the repository from a directory descriptor with O_NOFOLLOW. A symlink, hard link, FIFO,
socket or device is exit 5; a file over the size limit, or a tree over the file-count or depth limit,
is exit 7; any hit is exit 6.
"""

import os
import re
import stat
import sys
import unicodedata
from pathlib import Path

from . import _cli
from ._text import decode
from .errors import (
    CONFINEMENT,
    INPUT_LIMIT,
    INTERNAL,
    OK,
    PRECONDITION,
    REJECTED,
    ShitError,
)
from .project import discover_root

ENV_DENY = "SHIT_DENY_FILE"
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILES = 20000
MAX_DEPTH = 32
MAX_DENY_BYTES = 64 * 1024
MAX_DENY_ENTRIES = 1000
MIN_ENTRY_CHARS = 3
WITHHELD = "<withheld>"

_STRIP = {"Mn", "Cf", "Cc"}

# Generic content rules. Each is linear-time on a cleaned line. Placeholders such as <your-token> do
# not match the assignment rule because the value must start with a non-bracket character.
CONTENT_RULES = (
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ ){0,3}PRIVATE KEY(?: BLOCK)?-----")),
    ("token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("token", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("token", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("credential-url", re.compile(r"://[^/\s:@]{1,100}:[^/\s@]{3,100}@")),
    ("credential", re.compile(
        r"(?i)\b(?:password|passwd|passphrase|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)s?"
        r"[ \t]{0,20}[:=][ \t]{0,20}[\"']?[^\s\"'<>{}$()\[\]]{8,}")),
    ("session-url", re.compile(r"(?i)https?://(?:www\.)?claude\.ai/\S{0,200}(?:session|share|chat|code)\S{0,200}")),
    ("session-url", re.compile(r"(?i)https?://(?:www\.)?chatgpt\.com/(?:c|share)/[A-Za-z0-9-]{8,}")),
    ("session-url", re.compile(r"(?i)^claude-session:")),
)

NAME_RULES = (
    re.compile(r"^\.env(?:\..+)?$"),
    re.compile(r"^id_(?:rsa|dsa|ecdsa|ed25519)$"),
    re.compile(r"^.+\.(?:pem|key|p12|pfx|keystore|jks|kdbx)$"),
    re.compile(r"^credentials(?:\..+)?$"),
    re.compile(r"^\.(?:netrc|pgpass|htpasswd)$"),
)
NAME_ALLOWED = re.compile(r"^\.env\.(?:example|sample|template|dist)$")


def _clean(text):
    """Decompose, then drop combining marks and format and control characters (tab becomes a space)."""
    out = []
    for ch in unicodedata.normalize("NFKD", text):
        if ch == "\t":
            out.append(" ")
        elif unicodedata.category(ch) not in _STRIP:
            out.append(ch)
    return "".join(out)


def _fold(text):
    return " ".join(_clean(text).casefold().split())


def name_is_credential(name):
    clean = _clean(name)
    if NAME_ALLOWED.match(clean):
        return False
    return any(rule.match(clean) for rule in NAME_RULES)


def name_embeds_secret(name):
    """True when a content rule matches the name itself, so the name must not be printed."""
    clean = _clean(name)
    return any(pattern.search(clean) for _, pattern in CONTENT_RULES)


# -- the deny list ------------------------------------------------------------------------------


def _walk_open(real):
    """Open the canonical absolute path from the filesystem root, O_NOFOLLOW on every component.

    Any symlink met at open time is refused as confinement; nothing is resolved a second time.
    """
    names = [n for n in real.split(os.sep) if n]
    if not names:
        raise ShitError(CONFINEMENT)
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    dfd = os.open(os.sep, dir_flags)
    try:
        for name in names[:-1]:
            nfd = os.open(name, dir_flags, dir_fd=dfd)
            os.close(dfd)
            dfd = nfd
        return os.open(names[-1], file_flags, dir_fd=dfd)
    except FileNotFoundError:
        raise ShitError(PRECONDITION) from None
    except OSError:
        raise ShitError(CONFINEMENT) from None
    finally:
        os.close(dfd)


def load_deny(path, repo):
    """Validate and read the deny file; return the folded terms in entry order (entry 1 first).

    The path is resolved once (realpath) to a canonical path. The repository exclusion is checked
    on that canonical path, and the file is then opened from the root by that same path with no
    symlink followed, so a link retargeted after the check cannot change what is read.
    """
    try:
        path, repo = os.fspath(path), os.fspath(repo)
        if not isinstance(path, str) or not isinstance(repo, str):
            raise TypeError
        try:
            final_is_link = stat.S_ISLNK(os.lstat(path).st_mode)
        except OSError:
            final_is_link = False  # a missing file is reported by the open below
        real = os.path.realpath(path)
        root = os.path.realpath(repo)
        inside = os.path.commonpath([root, real]) == root
    except (TypeError, ValueError):
        raise ShitError(CONFINEMENT) from None
    if final_is_link or inside:
        raise ShitError(CONFINEMENT)
    fd = _walk_open(real)
    try:
        again = _walk_open(real)
        try:
            first, second = os.fstat(fd), os.fstat(again)
        finally:
            os.close(again)
        if (first.st_dev, first.st_ino) != (second.st_dev, second.st_ino) or not stat.S_ISREG(first.st_mode):
            raise ShitError(CONFINEMENT)
    except BaseException:
        os.close(fd)
        raise
    try:
        st = os.fstat(fd)
        if st.st_nlink != 1 or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise ShitError(CONFINEMENT)
        data = os.read(fd, MAX_DENY_BYTES + 1)
    except OSError:
        raise ShitError(INTERNAL) from None
    finally:
        os.close(fd)
    if len(data) > MAX_DENY_BYTES:
        raise ShitError(INPUT_LIMIT)
    try:
        text = decode(data)
    except _cli.EXPECTED:
        raise ShitError(REJECTED) from None
    terms = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        term = _fold(stripped)
        if len(term) < MIN_ENTRY_CHARS:
            raise ShitError(REJECTED)
        terms.append(term)
    if len(terms) > MAX_DENY_ENTRIES:
        raise ShitError(INPUT_LIMIT)
    return terms


def resolve_deny_path(option):
    """The configured deny-file path: the option, else the environment, else None (off)."""
    return option or os.environ.get(ENV_DENY) or None


def _deny_entry(folded, terms):
    for number, term in enumerate(terms, start=1):
        if term in folded:
            return number
    return 0


# -- scanning -----------------------------------------------------------------------------------


def scan_text(text, terms=()):
    """Return sorted (line, rule) pairs for one decoded file. `rule` is a rule name or deny-entry N."""
    found = set()
    for number, raw in enumerate(text.split("\n"), start=1):
        line = _clean(raw)
        for name, pattern in CONTENT_RULES:
            if pattern.search(line):
                found.add((number, name))
        if terms:
            entry = _deny_entry(" ".join(line.casefold().split()), terms)
            if entry:
                found.add((number, f"deny-entry {entry}"))
    return sorted(found)


def _printable(name):
    out = []
    for ch in name.encode("ascii", "backslashreplace").decode("ascii"):
        out.append(ch if " " <= ch <= "~" else f"\\x{ord(ch):02x}")
    return "".join(out)


class _Walk:
    def __init__(self, terms):
        self.terms = terms
        self.files = 0
        self.findings = []  # (display path, line, label)
        self.codes = set()

    def part(self, name):
        if name_embeds_secret(name) or (self.terms and _deny_entry(_fold(name), self.terms)):
            return WITHHELD
        return _printable(name)

    def report(self, parts, line, label):
        self.findings.append(("/".join(parts), line, label))

    def walk(self, dirfd, parts, depth):
        if depth > MAX_DEPTH:
            self.codes.add(INPUT_LIMIT)
            return
        try:
            names = sorted(os.listdir(dirfd))
        except OSError:
            self.codes.add(INTERNAL)
            return
        for name in names:
            if depth == 0 and name == ".git":
                continue
            shown = [*parts, self.part(name)]
            try:
                st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
            except OSError:
                self.codes.add(INTERNAL)
                continue
            if stat.S_ISDIR(st.st_mode):
                entry = _deny_entry(_fold(name), self.terms) if self.terms else 0
                if entry:
                    self.report(shown, 0, f"deny-entry {entry}")
                elif name_is_credential(name) or name_embeds_secret(name):
                    self.report(shown, 0, "filename")
                self.descend(dirfd, name, shown, depth)
            elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                self.file(dirfd, name, shown, st)
            else:
                self.codes.add(CONFINEMENT)
                self.report(shown, 0, "confinement")

    def descend(self, dirfd, name, shown, depth):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
        except OSError:
            self.codes.add(CONFINEMENT)
            self.report(shown, 0, "confinement")
            return
        try:
            self.walk(fd, shown, depth + 1)
        finally:
            os.close(fd)

    def file(self, dirfd, name, shown, st):
        self.files += 1
        if self.files > MAX_FILES:
            if self.files == MAX_FILES + 1:
                self.codes.add(INPUT_LIMIT)
            return
        entry = _deny_entry(_fold(name), self.terms) if self.terms else 0
        if entry:
            self.report(shown, 0, f"deny-entry {entry}")
        elif name_is_credential(name) or name_embeds_secret(name):
            self.report(shown, 0, "filename")
        if st.st_size > MAX_FILE_BYTES:
            self.codes.add(INPUT_LIMIT)
            return
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dirfd)
        except OSError:
            self.codes.add(CONFINEMENT)
            self.report(shown, 0, "confinement")
            return
        try:
            after = os.fstat(fd)
            if not stat.S_ISREG(after.st_mode) or after.st_nlink != 1 or (after.st_dev, after.st_ino) != (st.st_dev, st.st_ino):
                self.codes.add(CONFINEMENT)
                self.report(shown, 0, "confinement")
                return
            data = os.read(fd, MAX_FILE_BYTES + 1)
            while len(data) <= MAX_FILE_BYTES:
                more = os.read(fd, MAX_FILE_BYTES + 1 - len(data))
                if not more:
                    break
                data += more
        except OSError:
            self.codes.add(INTERNAL)
            return
        finally:
            os.close(fd)
        if len(data) > MAX_FILE_BYTES:
            self.codes.add(INPUT_LIMIT)
            return
        for line, label in scan_text(data.decode("utf-8", "replace"), self.terms):
            self.report(shown, line, label)


def scan_repo(repo, terms=()):
    """Scan the working tree. Return (exit code, output lines). No matched text is ever included."""
    try:
        root_fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except FileNotFoundError:
        raise ShitError(PRECONDITION) from None
    except OSError:
        raise ShitError(CONFINEMENT) from None
    walker = _Walk(terms)
    try:
        walker.walk(root_fd, [], 0)
    finally:
        os.close(root_fd)
    lines = []
    for path, line, label in sorted(walker.findings):
        if label.startswith("deny-entry"):
            lines.append(f"{path}:{line} {label}")
        else:
            lines.append(f"{path}:{line}")
    codes = set(walker.codes)
    if any(label != "confinement" for _, _, label in walker.findings):
        codes.add(_cli.PRIVACY_HIT)
    for code in (CONFINEMENT, INPUT_LIMIT, INTERNAL, _cli.PRIVACY_HIT):
        if code in codes:
            return code, lines
    return OK, lines


# -- command ------------------------------------------------------------------------------------


def repo_root(option):
    start = Path(option) if option else Path.cwd()
    if not start.is_dir():
        raise ShitError(PRECONDITION)
    try:
        return discover_root(start)
    except ShitError:
        raise
    except OSError:
        raise ShitError(PRECONDITION) from None


def handler(args):
    root = repo_root(args.repo)
    path = resolve_deny_path(args.deny_file)
    terms = load_deny(path, root) if path else ()
    return scan_repo(root, terms)


def add_arguments(parser):
    parser.add_argument("--repo", default=None, help="repository path (default: current directory)")
    parser.add_argument("--deny-file", default=None, help="private deny list, outside the repository, mode 600")


def register(subparsers):
    parser = subparsers.add_parser("privacy-scan", help="scan the working tree for secrets and private terms")
    add_arguments(parser)
    parser.set_defaults(func=handler)
    return parser


def _build():
    parser = _cli.SafeParser(prog="shit")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_cli.SafeParser)
    register(sub)
    return parser


def main(argv=None):
    return _cli.run(_build, ["privacy-scan", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
