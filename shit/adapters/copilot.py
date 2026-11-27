"""Copilot adapter: argument array for the Copilot command-line tool, brief as one prompt argument.

No tool-allow flag is added, so tool use is denied or prompts, and an unattended run denies. The prompt is a
single array element that always starts with fixed text, so it cannot be read as a flag. It is passed with -p
and standard input stays empty, because the tool ignores standard input when -p is given. Nothing here starts the
tool. Flags follow the vendor's command-line reference at the time of writing; check them with
`copilot --help`.
"""

from shit.adapters.base import Adapter


class CopilotAdapter(Adapter):
    name = "copilot"
    needs = ("the copilot command installed by the worker", "a brief file that passes the brief contract")
    default_permissions = "no allow flag passed; tool use is denied or prompts, and unattended runs deny"
    safe_extra_args = frozenset({"--no-color", "--no-ask-user"})  # --no-ask-user: an unattended run cannot stall on a question

    def command(self, fields, prompt, extra):
        argv = ["copilot"]
        if "model" in fields:
            argv += ["--model", fields["model"]]
        argv += list(extra)
        argv += ["-p", prompt]
        return tuple(argv), ""
