"""Schnittstellen zwischen den Paketen.

Diese Signaturen sind verbindlich. Ein Fachmodul implementiert das
jeweilige Protocol — es ändert es nicht.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from .types import (
    Credentials,
    DownloadItem,
    DownloadResult,
    FileChecksum,
    ManifestEntry,
    ProductRef,
    PruneItem,
    PruneResult,
    RemoteFile,
    ResolvedLink,
    SlotKey,
    UserData,
)


@runtime_checkable
class CredentialStore(Protocol):
    """Persistenz der Tokens. Speichert niemals Passwörter."""

    def load(self) -> Credentials | None: ...

    def save(self, credentials: Credentials) -> None: ...

    def clear(self) -> None: ...


@runtime_checkable
class AuthProvider(Protocol):
    """Liefert ein gültiges Access-Token und erneuert es bei Bedarf still."""

    async def access_token(self) -> str: ...

    def is_authenticated(self) -> bool: ...


@runtime_checkable
class GogApi(Protocol):
    """Lesezugriff auf Bibliothek, Produktdetails und Download-Links."""

    async def user_data(self) -> UserData: ...

    async def library(self) -> list[ProductRef]: ...

    async def product_files(
        self, product_id: int, *, include_dlc: bool = True
    ) -> list[RemoteFile]:
        """Alle Dateien eines Produkts, DLCs rekursiv eingeschlossen."""
        ...

    async def resolve_downlink(self, downlink: str) -> ResolvedLink:
        """Signierte CDN-URL frisch auflösen. Ergebnis ist kurzlebig."""
        ...

    async def checksum(self, checksum_url: str) -> FileChecksum | None: ...


@runtime_checkable
class Store(Protocol):
    """Persistentes Manifest."""

    def replace_remote(self, product_id: int, files: Sequence[RemoteFile], seen_utc: str) -> None:
        """Remote-Stand eines Produkts übernehmen, lokale Zustände erhalten."""
        ...

    def entries(self, product_id: int | None = None) -> list[ManifestEntry]: ...

    def entries_for_slot(self, slot: SlotKey) -> list[ManifestEntry]: ...

    def update_entry(self, entry: ManifestEntry) -> None: ...

    def remove_entry(self, slot: SlotKey, file_id: str) -> None: ...

    def products(self) -> list[ProductRef]: ...

    def replace_products(self, products: Sequence[ProductRef]) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class ProgressReporter(Protocol):
    """Zweistufige Fortschrittsanzeige, TTY-abhängig (KONZEPT.md §5.4)."""

    def start_overall(self, total_files: int, total_bytes: int) -> None: ...

    def start_file(self, name: str, total_bytes: int | None, already_done: int = 0) -> None: ...

    def advance(self, n_bytes: int) -> None: ...

    def finish_file(self, name: str, ok: bool, detail: str = "") -> None: ...

    def message(self, text: str) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class Downloader(Protocol):
    """Lädt eine Datei, resume-fähig, mit erzwungener 206-Prüfung."""

    async def fetch(self, item: DownloadItem, reporter: ProgressReporter) -> DownloadResult: ...


@runtime_checkable
class Pruner(Protocol):
    """Führt einen Prune-Plan aus und prüft dessen Vorbedingungen erneut."""

    def execute(self, items: Sequence[PruneItem], *, dry_run: bool = False) -> list[PruneResult]: ...


@runtime_checkable
class FileScanner(Protocol):
    """Beobachtet den echten Zustand auf der Platte."""

    def scan(self, root: Path) -> dict[Path, int]:
        """Pfad -> Größe für alle Dateien unterhalb von ``root``."""
        ...
