"""Zusammenbau der Bestandteile und gemeinsame Helfer der Kommandos."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gogdl.constants import DB_FILENAME, STATE_DIRNAME, TRASH_DIRNAME
from gogdl.model.types import OsName, PruneMode, SyncConfig


def now_utc() -> str:
    """Zeitstempel für ``last_seen_utc``/``last_verified_utc``."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_dir(dest: Path) -> Path:
    return dest / STATE_DIRNAME


def db_path(dest: Path) -> Path:
    return state_dir(dest) / DB_FILENAME


def scan_disk(root: Path) -> dict[Path, int]:
    """Tatsächlicher Plattenzustand: Pfad -> Größe.

    Die Datei ist die Wahrheit, die Datenbank nur der Cache (KONZEPT.md §5.2).
    Der Zustandsordner und der Papierkorb bleiben außen vor.
    """
    result: dict[Path, int] = {}
    if not root.exists():
        return result
    skip = {STATE_DIRNAME, TRASH_DIRNAME}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        base = Path(dirpath)
        for name in filenames:
            path = base / name
            try:
                result[path] = path.stat().st_size
            except OSError:
                continue
    return result


@dataclass
class AppContext:
    """Alles, was ein Kommando braucht — an einer Stelle gebaut."""

    dest: Path
    config: SyncConfig
    jobs: int
    dry_run: bool
    quiet: bool
    verbose: bool
    json_output: bool
    limit_rate: int | None = None


def build_sync_config(args) -> SyncConfig:
    """Übersetzt die geparsten Argumente in eine ``SyncConfig``."""
    dest = Path(args.dest).expanduser().resolve()

    os_filter = _parse_os(getattr(args, "os", None))
    languages = _parse_list(getattr(args, "lang", None)) or {"en"}

    return SyncConfig(
        dest=dest,
        os_filter=frozenset(os_filter),
        languages=frozenset(languages),
        include_dlc=getattr(args, "dlc", True),
        include_extras=getattr(args, "extras", False),
        include_patches=getattr(args, "patches", False),
        prune=getattr(args, "prune", True),
        keep_versions=getattr(args, "keep_versions", 1),
        prune_mode=PruneMode(getattr(args, "prune_mode", "delete")),
        strict_md5=getattr(args, "strict", False),
    )


def _parse_os(value: str | None) -> set[OsName]:
    """``--os`` auflösen. Ohne Angabe: nur die laufende Plattform (§10)."""
    if not value:
        return {OsName.current()}
    if value.strip().lower() == "all":
        return set(OsName)
    result: set[OsName] = set()
    for item in _parse_list(value) or []:
        try:
            result.add(OsName(item))
        except ValueError as exc:
            raise ValueError(
                f"Unbekannte Plattform {item!r}. Erlaubt: windows, linux, mac, all"
            ) from exc
    return result or {OsName.current()}


def _parse_list(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {part.strip().lower() for part in value.split(",") if part.strip()}
