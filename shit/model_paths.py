"""Registry of package code paths that send untrusted text to a model.

The tuple is empty because this edition currently prepares text and command plans but does not invoke a
model, launch a process, or open a network connection. Any future call site must add one row here.
"""

from dataclasses import dataclass

CLOSED = "CLOSED"
OPEN = "OPEN"


@dataclass(frozen=True)
class ModelPath:
    """One exact function where untrusted text can cross a model boundary."""

    id: str
    module: str
    function: str
    trigger: str
    untrusted_inputs: tuple[str, ...]
    model_kind: str
    tools: bool
    write: bool
    secrets: bool
    guard: str
    status: str
    reason: str


MODEL_PATHS: tuple[ModelPath, ...] = ()
