"""Resolve the configured persistence backend once for command-line callers."""

from dataclasses import dataclass

from . import (
    backend_name,
    commands,
    config,
    dependencies,
    queue,
    store_queue,
    store_tasks,
    task,
)


@dataclass(frozen=True)
class Backend:
    name: str
    tasks: object
    records: object
    queue: object
    dependencies: object
    busy_timeout_ms: int | None


def resolve(repo):
    """Return the operation modules selected by config.toml; absence keeps the file backend."""
    path = repo / "config.toml"
    settings = (
        config.read(path, repo)
        if path.exists()
        else {"store": {"backend": "files", "busy_timeout_ms": 60000}}
    )
    store_settings = settings["store"]
    if store_settings["backend"] == backend_name.SQLITE:
        return Backend(
            backend_name.SQLITE,
            store_tasks,
            store_tasks,
            store_queue,
            store_queue,
            store_settings["busy_timeout_ms"],
        )
    return Backend("files", task, commands, queue, dependencies, None)
