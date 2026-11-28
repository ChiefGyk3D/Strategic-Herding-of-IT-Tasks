"""Resolve the configured persistence backend once for command-line callers."""

from dataclasses import dataclass

from . import (
    backend_name,
    config,
    store_queue,
    store_tasks,
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
    """Return the database operation modules selected by validated configuration."""
    path = repo / "config.toml"
    settings = (
        config.read(path, repo)
        if path.exists()
        else {"store": {"backend": backend_name.SQLITE, "busy_timeout_ms": 60000}}
    )
    store_settings = settings["store"]
    return Backend(
        backend_name.SQLITE,
        store_tasks,
        store_tasks,
        store_queue,
        store_queue,
        store_settings["busy_timeout_ms"],
    )
