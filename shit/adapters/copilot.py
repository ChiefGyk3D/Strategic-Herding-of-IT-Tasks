"""Copilot adapter: argument array for the Copilot command-line tool, brief as one prompt argument.

No tool-allow flag is added, so a non-interactive run has no tool permission granted. The prompt is a single
array element that always starts with fixed text, so it cannot be read as a flag. Nothing here starts the
tool. Flags follow the vendor's command-line reference at the time of writing; check them with
`copilot --help`.
"""

from shit.adapters.base import Adapter


class CopilotAdapter(Adapter):
    name = "copilot"
    needs = ("the copilot command installed by the worker", "a brief file that passes the brief contract")
    default_permissions = "no tools allowed (no allow flag is passed, so no tool permission is granted)"
    safe_extra_args = frozenset({"--no-color"})

    def command(self, fields, prompt, extra):
        argv = ["copilot"]
        if "model" in fields:
            argv += ["--model", fields["model"]]
        argv += list(extra)
        argv += ["-p", prompt]
        return tuple(argv), ""
