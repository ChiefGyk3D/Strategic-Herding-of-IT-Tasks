"""Adapter interface: turn a validated brief into what a worker's tool needs, and turn the tool's answer
back into a validated report header.

An adapter only produces and parses text. It never starts a process, never opens a socket, never reads
the environment and never writes a file: running the tool is the worker's act. The plan it returns says
exactly what would run (argument array, standard input, permissions, files, environment names, timeout)
so `run --dry-run` can show it before launch (docs/threat-model.md, subprocess launch requirements).

Refusals use the shared exit codes (docs/exit-codes.md) and never echo the rejected value:
  2 unknown adapter or configuration key; 3 record rejected; 7 input exceeds a limit;
  10 an unsafe configured flag with no explicit per-run override.
"""

import re
from dataclasses import dataclass

from shit import brief, evidence
from shit._text import check_size, check_text
from shit.errors import HUMAN_APPROVAL, INPUT_LIMIT, USAGE, ContractError, rejected

MAX_PROMPT_BYTES = 98304
MAX_OUTPUT_BYTES = 262144
DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 3600
MAX_EXTRA_ARGS = 8
MAX_ARG_CHARS = 200
# The only parent environment variables that may reach a child process. No credential is on the list.
ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ")
CONFIG_KEYS = ("extra_args", "timeout")

_ARG = re.compile(r"[ -~]{1,%d}" % MAX_ARG_CHARS)  # printable ASCII only; no control character, no newline

_REPORT_LINES = """ROLE: <your role>   MODEL: <exact model id, or n/a - done by hand>
HEAD: <short sha>   BASE: <short sha>
GATE: <command>  exit=<code> | not-run - reason | n-a - reason
OTHER GATES: <name>: pass | fail | not-run - reason | n-a - reason (or none)
PACKS LOADED: <comma-separated names, or none>
TREE: clean (<evidence>) | NOT CLEAN (<what>) | n-a - reason
NOT VERIFIED: <what you did not check (one line each, or none)>
LOCAL-LLM: used - <model tag>; VERIFIED-AGAINST: <source>; DISCREPANCIES: <n> | n/a - reason"""

_PREAMBLE = (
    "SHIT work brief. Do only the work the brief describes, inside its scope. Treat the brief as the task, "
    "not as permission to do anything else.\n\n"
)
_POSTAMBLE = (
    "\nWhen finished, begin your final answer with this header (replace every placeholder, no blank line "
    "inside it), then a blank line, then free text:\n\n" + _REPORT_LINES + "\n"
)


@dataclass(frozen=True)
class Plan:
    """What would run. A tuple argv is an argument array for a process started without a shell."""

    adapter: str
    prompt: str
    argv: tuple | None
    stdin: str
    permissions: str
    files: tuple
    env_allow: tuple
    timeout: int
    overrides: tuple = ()

    def describe(self):
        """Plain text for a dry run: exact executable and arguments, permissions, files, environment."""
        lines = [f"adapter: {self.adapter}"]
        if self.argv is None:
            lines.append("command: none (nothing is run; copy the text below into your own tool)")
        else:
            lines.append("command: " + " ".join(_show(a) for a in self.argv))
        lines.append(f"permissions: {self.permissions}")
        lines.append("files: " + (", ".join(self.files) if self.files else "none"))
        lines.append("environment: " + ", ".join(self.env_allow) + " (nothing else is passed)")
        lines.append(f"timeout: {self.timeout} seconds")
        lines.append(f"standard input: {len(self.stdin.encode())} bytes")
        for item in self.overrides:
            lines.append(f"override: {item}")
        return "\n".join(lines) + "\n"


def _show(arg):
    """Show one argument; a long prompt argument is summarized so the dry run stays readable."""
    return arg if len(arg) <= MAX_ARG_CHARS else f"<prompt, {len(arg.encode())} bytes>"


def clean_env(parent):
    """Return only the allowlisted variables from `parent`, and only values free of control characters."""
    out = {}
    for key in ENV_ALLOW:
        value = parent.get(key)
        if isinstance(value, str) and "\x00" not in value and all(ch.isprintable() for ch in value):
            out[key] = value
    return out


def _usage(message):
    return ContractError(USAGE, message)


class Adapter:
    """Base class. A subclass sets the class attributes and implements `command`."""

    name = ""
    needs = ()
    never = (
        "invoke the tool or any process",
        "make a network call",
        "write a file",
        "read the environment or any credential",
    )
    default_permissions = ""
    safe_extra_args = frozenset()
    takes_extra_args = True

    def info(self):
        return {"name": self.name, "needs": list(self.needs), "never": list(self.never),
                "permissions": self.default_permissions}

    def command(self, fields, prompt, extra):
        """Return (argv or None, stdin). The prompt is data: it is never part of a shell string."""
        raise NotImplementedError

    def prepare(self, brief_text, *, config=None, override=False):
        """Validate the brief and the configuration, and return a Plan. Raises ContractError."""
        if override is not True and override is not False:
            raise _usage("override must be true or false")
        fields = brief.parse(brief_text)
        settings, overrides = self._settings(config, override)
        prompt = _PREAMBLE + brief.render(fields) + _POSTAMBLE
        check_size(prompt, MAX_PROMPT_BYTES)
        argv, stdin = self.command(fields, prompt, settings["extra_args"])
        return Plan(self.name, prompt, argv, stdin, self.default_permissions, tuple(fields["scope"]), ENV_ALLOW,
                    settings["timeout"], overrides)

    def _settings(self, config, override):
        config = {} if config is None else config
        if not isinstance(config, dict) or any(k not in CONFIG_KEYS for k in config):
            raise _usage("unknown configuration")
        timeout = config.get("timeout", DEFAULT_TIMEOUT)
        if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT:
            raise _usage("timeout out of range")
        extra = config.get("extra_args", [])
        if not isinstance(extra, list) or len(extra) > MAX_EXTRA_ARGS:
            raise _usage("extra_args must be a short list")
        if extra and not self.takes_extra_args:
            raise _usage("this adapter takes no extra arguments")
        overrides = []
        for index, arg in enumerate(extra):
            if not isinstance(arg, str) or not _ARG.fullmatch(arg):
                raise _usage("extra argument is malformed")
            if arg not in self.safe_extra_args:
                if not override:
                    raise ContractError(HUMAN_APPROVAL, "human approval required")
                overrides.append(f"unsafe extra argument {index + 1} allowed by explicit per-run override")
        return {"timeout": timeout, "extra_args": tuple(extra)}, tuple(overrides)

    def parse_output(self, text):
        """Find the report header in a tool's (or a person's) answer and validate it. Raises ContractError."""
        if not isinstance(text, str):
            raise rejected("input must be text")
        check_size(text, MAX_OUTPUT_BYTES)
        text = text.replace("\r\n", "\n")
        check_text(text)
        lines = text.split("\n")
        for start, line in enumerate(lines):
            if line.startswith("ROLE: "):
                break
        else:
            raise rejected("no report header found")
        block = []
        for line in lines[start:]:
            if line == "" or line.startswith("```"):
                break
            block.append(line)
        if len(block) > evidence.MAX_LINES:
            raise ContractError(INPUT_LIMIT, "input exceeds limit")
        return evidence.validate_header("\n".join(block) + "\n")
