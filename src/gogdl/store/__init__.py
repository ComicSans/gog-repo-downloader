"""Persistenz des Manifests (KONZEPT.md §4.4)."""

from __future__ import annotations

from .sqlite_store import SCHEMA_VERSION, SqliteStore

__all__ = ["SqliteStore", "SCHEMA_VERSION"]
