"""Stub — wird von sync-Agent implementiert. I/O-frei."""

from __future__ import annotations

from collections.abc import Sequence

from gogdl.model.types import ManifestEntry, RemoteFile, SyncConfig, SyncPlan


def plan_downloads(
    remote: Sequence[RemoteFile],
    local: Sequence[ManifestEntry],
    config: SyncConfig,
    on_disk: dict | None = None,
) -> SyncPlan:
    raise NotImplementedError


def plan_prune(
    local: Sequence[ManifestEntry],
    config: SyncConfig,
    on_disk: dict | None = None,
) -> SyncPlan:
    raise NotImplementedError


def is_stale(entry: ManifestEntry, remote: RemoteFile, *, strict_md5: bool = False) -> bool:
    raise NotImplementedError
