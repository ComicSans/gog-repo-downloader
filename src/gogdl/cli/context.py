"""Zusammenbau der Bestandteile und gemeinsame Helfer der Kommandos."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gogdl.constants import DB_FILENAME, STATE_DIRNAME, TRASH_DIRNAME
from gogdl.model.types import OsName, Preference, PruneMode, SyncConfig

_NOTATION_HINT = "A comma means 'otherwise', a plus means 'and'"
"""Erklärung der ``--os``/``--lang``-Schreibweise. Hilfetexte und Fehler-
meldungen benutzen sie wortgleich, sonst liest sich beides wie zwei
verschiedene Funktionen."""


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


def check_dest(dest: Path) -> str | None:
    """Warnt, wenn das Zielverzeichnis offensichtlich das falsche ist.

    Das Tool legt im Ziel eine Datenbank an und löscht dort alte Versionen.
    Ein Git-Arbeitsverzeichnis ist dafür fast nie gemeint - meist ist der
    Quellbaum erwischt worden, weil ``--dest`` fehlte.
    """
    if (dest / ".git").exists():
        return (
            f"{dest} is a git working tree. The manifest, the trash directory "
            "and every downloaded game would end up in the repository. A "
            "separate destination directory is probably meant, for example "
            "--dest ~/GOG"
        )
    return None


def build_sync_config(args) -> SyncConfig:
    """Übersetzt die geparsten Argumente in eine ``SyncConfig``."""
    dest = Path(args.dest).expanduser().resolve()

    os_pref = _parse_os_preference(getattr(args, "os", None))
    lang_pref = _parse_lang_preference(getattr(args, "lang", None))

    # Die Mengen bleiben als Vorfilter erhalten; die eigentliche Auswahl mit
    # Rückfallebenen trifft sync/ pro Auslieferung.
    os_filter = (
        {OsName(v) for v in os_pref.all_values} if os_pref else set(OsName)
    )
    languages = lang_pref.all_values or frozenset({"en"})

    return SyncConfig(
        dest=dest,
        os_filter=frozenset(os_filter),
        languages=frozenset(languages),
        os_preference=os_pref,
        language_preference=lang_pref,
        include_dlc=getattr(args, "dlc", True),
        include_extras=getattr(args, "extras", False),
        include_patches=getattr(args, "patches", False),
        prune=getattr(args, "prune", True),
        keep_versions=getattr(args, "keep_versions", 1),
        prune_mode=PruneMode(getattr(args, "prune_mode", "delete")),
        keep_old=getattr(args, "keep_old", False),
        strict_md5=getattr(args, "strict", False),
    )


_SPRACHCODES = {
    "en", "de", "fr", "es", "it", "pl", "ru", "pt", "br", "cz", "hu", "jp", "ja",
    "ko", "cn", "zh", "nl", "da", "sv", "no", "fi", "tr", "uk", "ro", "el", "he",
    "ar", "th", "bl", "sk", "es_mx", "pt_br", "zh_hans", "zh_hant",
}


def _parse_lang_preference(value: str | None) -> Preference:
    """``--lang`` auflösen und offensichtliche Tippfehler abfangen.

    Ohne Prüfung bedeutet ein vertippter Sprachcode still "lade gar nichts",
    und das sieht wie eine leere Bibliothek aus statt wie ein Fehler. Die
    Liste ist bewusst großzügig: GOG führt gelegentlich Codes ein, die hier
    nicht stehen, deshalb wird nur gewarnt, wenn ALLE angegebenen Codes
    unbekannt sind.
    """
    if not value or not value.strip():
        return Preference.of("en")
    if value.strip().lower() == "all":
        return Preference()

    pref = Preference.parse(value)
    unbekannt = sorted(pref.all_values - _SPRACHCODES)
    if unbekannt and len(unbekannt) == len(pref.all_values):
        raise ValueError(
            f"None of these language codes is known: {', '.join(unbekannt)}. "
            "Expected are codes like de, en, fr, or 'all' for every language. "
            f"{_NOTATION_HINT}: --lang de,en"
        )
    return pref


def _parse_os_preference(value: str | None) -> Preference:
    """``--os`` auflösen und die Plattformnamen sofort prüfen.

    Ohne Angabe: nur die laufende Plattform. ``all`` hebt die Einschränkung
    auf. Ein Tippfehler soll hier auffliegen und nicht später als leeres
    Ergebnis erscheinen.
    """
    if not value or not value.strip():
        return Preference.of(OsName.current().value)
    if value.strip().lower() == "all":
        return Preference()

    pref = Preference.parse(value)
    erlaubt = {member.value for member in OsName}
    unbekannt = sorted(pref.all_values - erlaubt)
    if unbekannt:
        raise ValueError(
            f"Unknown platform: {', '.join(unbekannt)}. "
            "Allowed are windows, linux, mac or all. "
            f"{_NOTATION_HINT}: --os linux+mac"
        )
    return pref
