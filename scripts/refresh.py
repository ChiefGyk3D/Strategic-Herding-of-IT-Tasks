"""Regenerate the starter set from a pinned commit of department-of-internal-arguments (DoIA).

    python3 scripts/refresh.py                  # clone DoIA at the pinned commit, regenerate, update the manifest
    python3 scripts/refresh.py --doia-dir PATH  # use an existing DoIA checkout (must be at the pinned commit)
    python3 scripts/refresh.py --check          # regenerate in a scratch folder and compare; write nothing

Nothing here is copied by hand: DoIA's own installer (scripts/install.py) writes the role and pack files
for each adapter and its exporter (scripts/export_prompt.py) writes the local-model prompts. This script
only picks the roles and packs, runs those two scripts in a scratch folder, drops the roles that are not
in the starter set (the installer has no role filter), and records every file's sha256 in manifest.json.

To move to a newer DoIA: read the commit, change DOIA_COMMIT and DOIA_RELEASE below, run this script, read
the diff, commit. Python 3.11 or later, standard library only. No network call except the git clone.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DOIA_URL = "https://github.com/ChiefGyk3D/department-of-internal-arguments"
DOIA_COMMIT = "c20a9bdee5da039f74fa0267759de1bec03dc6a2"
DOIA_RELEASE = "0.4.0"  # the release number the DoIA files carry; the commit is the pin

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "manifest.json"

ROLES = ["implementer", "reviewer", "researcher", "planner"]
PACKS = [
    "review-protocol",
    "implementer-self-check",
    "threat-model",
    "python-untrusted-input",
    "gha-security-dast",
    "org-operating-model",
]
# Adapters run in this order, each with the same pack list. `both` is Claude Code plus Copilot.
ADAPTERS = ["claude", "copilot", "codex"]
# Packs embedded in each role's local-model prompt (pasted into a chat or a system prompt). Small on
# purpose: a local model has a short context. Every entry must be in PACKS.
PROMPT_PACKS = {
    "implementer": ["implementer-self-check", "python-untrusted-input", "gha-security-dast"],
    "reviewer": ["review-protocol", "threat-model", "implementer-self-check"],
    "researcher": ["threat-model"],
    "planner": ["org-operating-model"],
}
# Where the generated files live, relative to the repository root. Only paths under these are ever
# written or removed, and only by exact name from a manifest.
GENERATED_ROOTS = [".claude", ".codex", ".agents", ".github/agents", "prompts"]
ROLE_SUFFIXES = {".claude/agents": ".md", ".codex/agents": ".toml", ".github/agents": ".agent.md"}
ALL_ROLE_DIRS = list(ROLE_SUFFIXES)


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(cmd, env=None, cwd=None):
    r = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=300, check=False)
    if r.returncode != 0:
        sys.exit(f"command failed ({r.returncode}): {' '.join(map(str, cmd))}\n{r.stdout}{r.stderr}")
    return r.stdout.strip()


def clean_env(extra):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL")}
    env.update(extra)
    return env


def doia_checkout(doia_dir, scratch):
    if doia_dir:
        checkout = Path(doia_dir).resolve()
    else:
        checkout = scratch / "doia"
        run(["git", "clone", "--quiet", DOIA_URL, str(checkout)])
        run(["git", "-C", str(checkout), "checkout", "--quiet", "--detach", DOIA_COMMIT])
    head = run(["git", "-C", str(checkout), "rev-parse", "HEAD"])
    if head != DOIA_COMMIT:
        sys.exit(f"DoIA checkout is at {head}, not the pinned {DOIA_COMMIT}")
    if run(["git", "-C", str(checkout), "status", "--porcelain"]):
        sys.exit("DoIA checkout has uncommitted changes; use a clean checkout at the pinned commit")
    return checkout


def generate(checkout, scratch):
    """Run DoIA's installer and exporter into scratch/out; return {relative path: sha256}."""
    out = scratch / "out"
    out.mkdir(mode=0o700)
    state = scratch / "state"
    state.mkdir(mode=0o700)
    env = clean_env({"DOIA_STATE_DIR": str(state)})
    pack_args = [a for p in PACKS for a in ("--pack", p)]
    for adapter in ADAPTERS:
        run(
            [sys.executable, "scripts/install.py", "--target", str(out), "--adapter", adapter, *pack_args, "--apply"],
            env=env,
            cwd=checkout,
        )
    (out / "prompts").mkdir()
    for role, packs in PROMPT_PACKS.items():
        cmd = [sys.executable, "scripts/export_prompt.py", "--role", role, "--output", str(out / "prompts" / f"{role}.md")]
        for p in packs:
            cmd += ["--pack", p]
        run(cmd, env=env, cwd=checkout)
    # The installer writes every role; keep only the starter set.
    keep = {f"{r}{suffix}" for r in ROLES for suffix in ROLE_SUFFIXES.values()}
    for d in ALL_ROLE_DIRS:
        for f in sorted((out / d).iterdir()):
            if f.name not in keep:
                f.unlink()
    files = {}
    for p in sorted(out.rglob("*")):
        if p.is_symlink():
            sys.exit(f"generated a symlink, refusing: {p}")
        if p.is_file():
            files[p.relative_to(out).as_posix()] = sha256_file(p)
    return out, files


def build_manifest(files):
    return {
        "schema": 1,
        "source": {"repository": DOIA_URL, "commit": DOIA_COMMIT, "release": DOIA_RELEASE},
        "adapters": ADAPTERS,
        "roles": ROLES,
        "packs": PACKS,
        "files": dict(sorted(files.items())),
    }


def render(manifest):
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def safe_rel(rel):
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or not any(rel == r or rel.startswith(r + "/") for r in GENERATED_ROOTS):
        sys.exit(f"refusing path outside the generated roots: {rel}")
    return ROOT / p


def sync(out, files):
    """Remove the files the old manifest lists (exact paths), copy the new set, write the manifest."""
    if MANIFEST.exists():
        old = json.loads(MANIFEST.read_text(encoding="utf-8")).get("files", {})
        for rel in old:
            target = safe_rel(rel)
            if target.is_symlink() or target.is_file():
                target.unlink()
                parent = target.parent
                while parent != ROOT and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
    for rel in files:
        target = safe_rel(rel)
        if target.is_symlink() or target.exists():
            sys.exit(f"not a file this script wrote, refusing to overwrite: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out / rel, target)
    MANIFEST.write_text(render(build_manifest(files)), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doia-dir", help="an existing DoIA checkout at the pinned commit (default: clone it)")
    ap.add_argument("--check", action="store_true", help="compare a fresh generation with the repository; write nothing")
    a = ap.parse_args(argv)
    scratch = Path(tempfile.mkdtemp(prefix="shit-refresh-"))  # mode 0700
    try:
        checkout = doia_checkout(a.doia_dir, scratch)
        out, files = generate(checkout, scratch)
        if a.check:
            current = MANIFEST.read_text(encoding="utf-8") if MANIFEST.exists() else ""
            if current != render(build_manifest(files)):
                print("drift: manifest.json differs from a fresh generation")
                return 3
            print(f"current: {len(files)} files match DoIA {DOIA_COMMIT[:12]}")
            return 0
        sync(out, files)
        print(f"refreshed: {len(files)} files from DoIA {DOIA_COMMIT[:12]} (release {DOIA_RELEASE})")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
