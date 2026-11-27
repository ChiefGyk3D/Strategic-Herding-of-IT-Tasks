"""Codex adapter: argument array for `codex exec`, brief on standard input (the `-` argument).

Default is the read-only sandbox. Nothing here starts the tool. Flags follow the vendor's command-line
reference at the time of writing; check them with `codex exec --help`.
"""

from shit.adapters.base import Adapter


class CodexAdapter(Adapter):
    name = "codex"
    needs = ("the codex command installed by the worker", "a brief file that passes the brief contract")
    default_permissions = "read-only sandbox (no writes, no network from the tool's sandbox)"
    safe_extra_args = frozenset({"--skip-git-repo-check"})

    def command(self, fields, prompt, extra):
        argv = ["codex", "exec", "--sandbox", "read-only"]
        if "model" in fields:
            argv += ["--model", fields["model"]]
        argv += list(extra)
        argv.append("-")
        return tuple(argv), prompt
