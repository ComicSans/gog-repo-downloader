"""Gemeinsame Datentypen.

Dieses Modul ist der Vertrag zwischen allen Paketen und wird von keinem
Fachmodul verändert. Es hat bewusst keine Abhängigkeiten außer der Stdlib.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class FileKind(str, Enum):
    """Art einer herunterladbaren Datei."""

    INSTALLER = "installer"
    PATCH = "patch"
    EXTRA = "extra"


class OsName(str, Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    MAC = "mac"

    @staticmethod
    def current() -> "OsName":
        system = platform.system()
        if system == "Darwin":
            return OsName.MAC
        if system == "Windows":
            return OsName.WINDOWS
        return OsName.LINUX


class LocalState(str, Enum):
    """Zustand einer Datei auf der Platte, relativ zum Manifest."""

    MISSING = "missing"
    PARTIAL = "partial"
    COMPLETE = "complete"
    STALE = "stale"
    ORPHANED = "orphaned"


class PruneMode(str, Enum):
    DELETE = "delete"
    TRASH = "trash"


@dataclass(frozen=True, order=True)
class SlotKey:
    """Kleinste Einheit, die als Ganzes aktuell oder veraltet ist.

    Ein Slot bündelt alle Teildateien einer Auslieferung (mehrteilige
    Installer!). Prune arbeitet ausschließlich auf Slot-Ebene — siehe
    KONZEPT.md §5.5.
    """

    product_id: int
    kind: FileKind
    os: OsName | None = None
    language: str | None = None

    def as_str(self) -> str:
        parts = [str(self.product_id), self.kind.value]
        if self.os is not None:
            parts.append(self.os.value)
        if self.language is not None:
            parts.append(self.language)
        return "/".join(parts)


@dataclass(frozen=True)
class ProductRef:
    """Eintrag aus der Bibliotheksliste, noch ohne Dateidetails."""

    product_id: int
    title: str
    slug: str
    has_updates: bool = False
    is_new: bool = False


@dataclass(frozen=True)
class RemoteFile:
    """Eine von GOG angebotene Datei.

    ``size``/``md5``/``version`` sind die Aktualitätssignale in der
    Präzedenz aus KONZEPT.md §4.2. ``md5`` ist ``None``, solange das
    Checksum-XML nicht abgerufen wurde oder GOG keines anbietet.
    """

    slot: SlotKey
    file_id: str
    downlink: str
    filename: str | None = None
    size: int | None = None
    md5: str | None = None
    version: str | None = None
    part_index: int = 1
    total_parts: int = 1

    @property
    def product_id(self) -> int:
        return self.slot.product_id


@dataclass(frozen=True)
class ResolvedLink:
    """Ergebnis der Downlink-Auflösung.

    Die URL ist zeitlich signiert und darf niemals persistiert und später
    wiederverwendet werden (KONZEPT.md §5.1).
    """

    url: str
    filename: str
    checksum_url: str | None = None


@dataclass(frozen=True)
class FileChecksum:
    """Inhalt des Checksum-XML einer Datei."""

    filename: str
    md5: str
    total_size: int | None = None


@dataclass
class ManifestEntry:
    """Persistierter Zustand einer Datei: was GOG anbietet + was lokal liegt."""

    slot: SlotKey
    file_id: str
    filename: str
    version: str | None
    size: int | None
    md5: str | None
    downlink: str
    part_index: int = 1
    total_parts: int = 1
    relative_path: str = ""
    state: LocalState = LocalState.MISSING
    bytes_done: int = 0
    last_seen_utc: str | None = None
    last_verified_utc: str | None = None

    @property
    def product_id(self) -> int:
        return self.slot.product_id

    @property
    def is_verified_complete(self) -> bool:
        """Vorbedingung jeder Löschung: vollständig UND geprüft.

        ``last_verified_utc`` wird nur gesetzt, wenn Größe (und, sofern
        vorhanden, MD5) nach dem Download tatsächlich geprüft wurden.
        """
        return self.state is LocalState.COMPLETE and self.last_verified_utc is not None


@dataclass(frozen=True)
class LocalFile:
    """Beobachteter Zustand auf der Platte — die Wahrheit, nicht die DB."""

    path: Path
    size: int
    is_partial: bool = False


@dataclass(frozen=True)
class DownloadItem:
    """Eine zu ladende Datei inklusive Zielpfad und bereits geladener Bytes."""

    entry: ManifestEntry
    target: Path
    resume_from: int = 0

    @property
    def part_path(self) -> Path:
        return self.target.with_name(self.target.name + ".part")

    @property
    def expected_size(self) -> int | None:
        return self.entry.size


@dataclass(frozen=True)
class PruneItem:
    """Eine geplante Löschung.

    ``replaced_by`` nennt die Dateien, deren verifizierte Vollständigkeit
    Vorbedingung ist. ``prune/`` prüft diese Bedingung bei der Ausführung
    ein zweites Mal und lehnt den Eintrag sonst ab.
    """

    path: Path
    slot: SlotKey
    reason: str
    size: int
    old_version: str | None
    new_version: str | None
    replaced_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class Report:
    """Nicht-löschbarer Fremd- oder Altbestand — wird gemeldet, nie angefasst."""

    path: Path
    kind: str  # "orphaned" | "foreign"
    detail: str = ""


@dataclass(frozen=True)
class SyncConfig:
    """Filter und Verhaltensschalter für die Planung."""

    dest: Path
    os_filter: frozenset[OsName] = field(default_factory=lambda: frozenset({OsName.current()}))
    languages: frozenset[str] = field(default_factory=lambda: frozenset({"en"}))
    include_dlc: bool = True
    include_extras: bool = False
    include_patches: bool = False
    prune: bool = True
    keep_versions: int = 1
    prune_mode: PruneMode = PruneMode.DELETE
    strict_md5: bool = False


@dataclass
class SyncPlan:
    """Ergebnis der Planung: was zu laden ist, was gelöscht werden darf."""

    downloads: list[DownloadItem] = field(default_factory=list)
    prunes: list[PruneItem] = field(default_factory=list)
    reports: list[Report] = field(default_factory=list)

    @property
    def download_bytes(self) -> int:
        return sum(
            max((item.expected_size or 0) - item.resume_from, 0) for item in self.downloads
        )

    @property
    def prune_bytes(self) -> int:
        return sum(item.size for item in self.prunes)


@dataclass(frozen=True)
class DownloadResult:
    item: DownloadItem
    ok: bool
    bytes_written: int = 0
    verified: bool = False
    error: str | None = None
    restarted: bool = False


@dataclass(frozen=True)
class PruneResult:
    item: PruneItem
    removed: bool
    reason: str = ""


@dataclass(frozen=True)
class Credentials:
    access_token: str
    refresh_token: str
    expires_at: float
    user_id: str | None = None
    session_id: str | None = None

    def is_expired(self, now: float, margin: float = 120.0) -> bool:
        return now >= self.expires_at - margin


@dataclass(frozen=True)
class UserData:
    username: str
    user_id: str
    is_logged_in: bool = True
