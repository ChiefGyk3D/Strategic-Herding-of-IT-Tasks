"""Puts the repository root on sys.path so the tests import `shit` from any working directory."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def make_git_dir(git):
    """Create a minimal real repository marker (HEAD, objects/, refs/) at `git`."""
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "objects").mkdir()
    (git / "refs").mkdir()
