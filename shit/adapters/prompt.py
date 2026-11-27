"""Paste-in adapter: for a worker with no AI tool, or a managed computer where none may be installed.

It renders the brief as one bounded block of plain text to copy, and parses a pasted answer. It has no
command at all, so there is nothing to run and nothing to send.
"""

from shit.adapters.base import Adapter


class PromptAdapter(Adapter):
    name = "prompt"
    needs = ("a brief file that passes the brief contract", "a person to copy the text and paste the answer back")
    default_permissions = "none (text only; the adapter has no command)"
    takes_extra_args = False

    def command(self, fields, prompt, extra):
        return None, ""
