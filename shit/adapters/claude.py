"""Claude adapter: argument array for Claude Code in print mode, brief on standard input.

Default is the plan permission mode (read and propose, no edits or commands). Nothing here starts the tool.
Flags follow the vendor's command-line reference at the time of writing; check them with `claude --help`.
"""

from shit.adapters.base import Adapter


class ClaudeAdapter(Adapter):
    name = "claude"
    needs = ("the claude command installed by the worker", "a brief file that passes the brief contract")
    default_permissions = "plan mode (read and propose only; no edits, no commands, no unlisted tools)"
    safe_extra_args = frozenset({"--verbose"})

    def command(self, fields, prompt, extra):
        argv = ["claude", "-p", "--permission-mode", "plan", "--output-format", "text"]
        if "model" in fields:
            argv += ["--model", fields["model"]]
        argv += list(extra)
        return tuple(argv), prompt
