"""Keepsake — full-entry memory system for Hermes Agent (MemoryProvider implementation).

Retrieves relevant memories automatically and injects them into the conversation
context each turn: BM25 full-text search out of the box, optional vector KNN,
time decay, feedback-based ranking, three storage backends (Redis / PostgreSQL /
SQLite), and multi-agent governance.

Install:  pip install keepsake-memory  (the plugin catalog installs it for you)
Activate: set ``memory.provider: keepsake`` in Hermes config.yaml, restart the gateway.
Config:   ~/.config/keepsake/config.json (or KEEPSAKE_CONFIG path / KEEPSAKE_* env vars)
"""

from keepsake import KeepsakeProvider


def register(ctx) -> None:
    """Plugin entry point: hand the MemoryProvider to Hermes.

    Activation itself is owned by ``memory.provider`` in config; this
    registration makes the provider discoverable through the plugin surfaces
    (dashboard picker, ``hermes plugins list``) and satisfies the catalog
    capability probe. Equivalent to the loader's subclass-instantiation
    fallback, so behaviour is unchanged for existing installs.
    """
    ctx.register_memory_provider(KeepsakeProvider())
