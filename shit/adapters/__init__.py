"""Optional adapters between a SHIT brief and a worker's tool (issue #12).

Adapters are chosen by configuration, one per worker, and never required: a brief and a report header
work by hand. `get(name)` finds one, `names()` lists them, `doctor()` describes each (what it needs and
what it never does).
"""

from shit.adapters.base import Adapter, Plan, clean_env
from shit.adapters.claude import ClaudeAdapter
from shit.adapters.codex import CodexAdapter
from shit.adapters.copilot import CopilotAdapter
from shit.adapters.prompt import PromptAdapter
from shit.errors import USAGE, ContractError

_REGISTRY = {a.name: a for a in (ClaudeAdapter(), CodexAdapter(), CopilotAdapter(), PromptAdapter())}

__all__ = ["Adapter", "Plan", "clean_env", "doctor", "get", "names"]


def names():
    return sorted(_REGISTRY)


def get(name):
    if not isinstance(name, str) or name not in _REGISTRY:
        raise ContractError(USAGE, "unknown adapter")
    return _REGISTRY[name]


def doctor():
    """Self-description of every adapter, in name order."""
    return [_REGISTRY[n].info() for n in names()]
