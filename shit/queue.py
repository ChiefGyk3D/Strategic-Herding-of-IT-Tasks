"""Compatibility names for queue operations on the database store."""

from .store_queue import *

# Removed file-protocol names retained only for leftover-state detection and migration diagnostics.
QUEUE = "queue.jsonl"
CHAIN = "queue.chain"
DEPS = "deps"
LEGACY_GRAPH = "dependencies.json"
PENDING = "queue-pending.json"
ANCHOR = "queue-anchor.json"
VIEW = "QUEUE.md"
LEASES = "leases"
