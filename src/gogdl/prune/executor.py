"""Stub — wird von prune-Agent implementiert."""

from __future__ import annotations


class PruneExecutor:
    """Implementiert model.protocols.Pruner."""

    def __init__(self, dest, store, mode=None) -> None:
        raise NotImplementedError

    def execute(self, items, *, dry_run: bool = False):
        raise NotImplementedError
