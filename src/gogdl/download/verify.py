"""Prüfung einer heruntergeladenen Datei gegen ihren Manifest-Eintrag.

Getrennt von ``engine.py``, weil ``gogdl verify`` dieselbe Prüfung ohne
Netzwerk braucht. Nur was hier durchkommt, darf später eine Löschung
rechtfertigen (KONZEPT.md §5.5).
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from gogdl.constants import CHUNK_SIZE
from gogdl.model.types import ManifestEntry

_ARCHIVE_SUFFIXES = {".zip"}


def file_md5(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def verify_entry(entry: ManifestEntry, path: Path, *, deep: bool = False) -> tuple[bool, str]:
    """Prüft eine Datei. Rückgabe: (in Ordnung, Begründung).

    Ohne ``deep`` wird nur die Größe geprüft - das ist billig und fängt
    abgebrochene Downloads. Mit ``deep`` kommen MD5 und, bei Archiven, ein
    Strukturtest dazu.

    Fehlt sowohl ``size`` als auch ``md5`` im Manifest, gibt es nichts zu
    prüfen: die Datei gilt dann als *nicht* verifiziert (False), damit sie
    keine Löschung autorisiert.
    """
    if not path.exists():
        return False, "file is missing"
    if not path.is_file():
        return False, "not a regular file"

    actual_size = path.stat().st_size
    if entry.size is not None and actual_size != entry.size:
        return False, f"size {actual_size} instead of {entry.size}"

    if not deep:
        if entry.size is None:
            return False, "no expected size in the manifest - not checkable"
        return True, "size matches"

    if entry.md5:
        actual_md5 = file_md5(path)
        if actual_md5.lower() != entry.md5.lower():
            return False, f"MD5 {actual_md5} instead of {entry.md5}"

    if path.suffix.lower() in _ARCHIVE_SUFFIXES:
        ok, detail = _check_zip(path)
        if not ok:
            return False, detail

    if entry.size is None and not entry.md5:
        return False, "neither size nor MD5 in the manifest - not checkable"

    signals = [name for name, value in (("size", entry.size), ("MD5", entry.md5)) if value]
    return True, "verified: " + " and ".join(signals)


def _check_zip(path: Path) -> tuple[bool, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            broken = archive.testzip()
    except zipfile.BadZipFile as exc:
        return False, f"archive is damaged: {exc}"
    except OSError as exc:
        return False, f"archive is not readable: {exc}"
    if broken is not None:
        return False, f"archive is damaged at {broken}"
    return True, "archive is fine"
