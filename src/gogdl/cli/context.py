"""Zusammenbau der Bestandteile und gemeinsame Helfer der Kommandos."""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gogdl.constants import DB_FILENAME, LOCK_FILENAME, STATE_DIRNAME, TRASH_DIRNAME
from gogdl.errors import GogdlError, LockBusy
from gogdl.model.types import OsName, Preference, PruneMode, SyncConfig

try:  # pragma: no cover - auf allen Zielplattformen vorhanden
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

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


def lock_path(dest: Path) -> Path:
    return state_dir(dest) / LOCK_FILENAME


# Fehlernummern, mit denen ein Dateisystem sagt "flock kenne ich nicht".
# Dann ist die Sperre nicht belegt, sondern schlicht nicht messbar - der
# Rückfallweg über die Prozessnummer muss allein entscheiden.
_FLOCK_UNBEKANNT = frozenset(
    {
        getattr(errno, name)
        for name in ("ENOTSUP", "EOPNOTSUPP", "EINVAL", "ENOLCK", "ENOSYS")
        if hasattr(errno, name)
    }
)


@dataclass(frozen=True)
class LockInfo:
    """Was in einer vorgefundenen Sperrdatei steht. Beides kann fehlen."""

    pid: int | None = None
    started: str | None = None


def _read_lock(fd: int) -> LockInfo:
    """Prozessnummer und Startzeit aus einer offenen Sperrdatei lesen.

    Unlesbarer oder unvollständiger Inhalt gilt als "keine Angabe" und
    nicht als Fehler: eine kaputte Sperrdatei darf niemanden aussperren.
    """
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        rohdaten = os.read(fd, 4096).decode("utf-8", "replace")
    except OSError:
        return LockInfo()
    zeilen = [z.strip() for z in rohdaten.splitlines() if z.strip()]
    pid: int | None = None
    if zeilen:
        try:
            pid = int(zeilen[0])
        except ValueError:
            pid = None
    started = zeilen[1] if len(zeilen) > 1 else None
    return LockInfo(pid=pid, started=started)


def _prozess_lebt(pid: int) -> bool:
    """Läuft der eingetragene Prozess noch?

    ``os.kill(pid, 0)`` stellt kein Signal zu, sondern prüft nur die
    Zustellbarkeit. ``PermissionError`` heißt: es gibt ihn, er gehört einem
    anderen Nutzer - das zählt als lebendig. ``pid <= 0`` wird abgefangen,
    weil 0 die gesamte Prozessgruppe meinte.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _flock_frei(fd: int) -> bool:
    """``flock`` versuchen. True heißt "gehört jetzt uns oder ist nicht messbar"."""
    if fcntl is None or not hasattr(fcntl, "flock"):
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        return exc.errno in _FLOCK_UNBEKANNT
    return True


def _belegt_meldung(path: Path, info: LockInfo) -> str:
    wer = f"process {info.pid}" if info.pid is not None else "an unknown process"
    wann = f", started {info.started}" if info.started else ""
    return (
        f"Another gogdl run is already working on this destination: {path} "
        f"is held by {wer}{wann}. Wait for that run to finish or stop it. "
        "If it is gone for good, delete the lock file. Read-only commands "
        "such as `gogdl status` keep working meanwhile."
    )


@contextmanager
def dest_lock(dest: Path) -> Iterator[Path]:
    """Schreibenden Zugriff auf ``dest`` auf einen Lauf begrenzen.

    Zwei gleichzeitige Läufe planen dieselbe Datei, öffnen dieselbe
    ``.part`` und verschränken ihre Bytes; das Ergebnis besteht die
    Größenprüfung und ist trotzdem falsch. Ebenso räumt das Aufräumen des
    einen die Teildatei des anderen ab. Die Sperre liegt unter
    ``<dest>/.gogdl/lock`` und wird beim Verlassen des Blocks entfernt -
    auch bei Strg-C, Ausnahme oder Fehler im Store.

    Zwei Wege, in dieser Reihenfolge:

    1. **Prozessnummer.** Die Sperrdatei enthält Prozessnummer und
       Startzeitpunkt. Steht dort ein noch laufender Prozess, wird
       abgelehnt. Steht dort ein toter, wird die Datei übernommen - das ist
       der häufigste Fall nach einem harten Abbruch und darf niemanden
       dauerhaft aussperren.
    2. **``fcntl.flock`` mit ``LOCK_EX | LOCK_NB``.** Der Kernel gibt die
       Sperre frei, sobald der Prozess endet, egal wie er endet.

    Der Prozesscheck steht bewusst vorn, weil er auch dort trägt, wo
    ``flock`` nichts sagt: Auf exFAT und auf Netzlaufwerken - und der
    Nutzer arbeitet auf genau so einer externen Platte, erkennbar daran,
    dass SQLite dort von WAL auf das alte Journal zurückfällt - ist
    ``flock`` wirkungslos oder meldet nur "nicht unterstützt". Dann
    entscheidet der Prozesscheck allein.

    Was der Rückfallweg NICHT leistet, offen gesagt:

    * Er erkennt keinen **zweiten Rechner**, der über ein Netzlaufwerk auf
      dasselbe Ziel zugreift. ``os.kill`` fragt die lokale Prozesstabelle;
      eine fremde Prozessnummer ist dort bedeutungslos und wird je nach
      Zufall als tot (Sperre wird übernommen) oder als lebend (fremder
      Lauf wird ausgesperrt) gelesen.
    * Er erkennt keine **wiederverwendete Prozessnummer**. Vergibt das
      System die Nummer eines hart abgebrochenen Laufs neu, gilt die
      liegengebliebene Sperre als gehalten, bis die Datei gelöscht wird.
    * Er schützt nicht gegen ein **Wettrennen** zwischen Prüfen und
      Schreiben: Starten zwei Läufe in derselben Millisekunde auf einem
      Dateisystem ohne ``flock``, können beide durchkommen. Auf allem, was
      ``flock`` beherrscht, schließt der Kernel diese Lücke.
    """
    path = lock_path(dest)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Ausdrücklich ohne O_TRUNC: würde der Inhalt hier gelöscht, wäre die
        # Prozessnummer des anderen Laufs weg, bevor sie geprüft ist.
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        # Ein unbeschreibbares Ziel als Traceback zu zeigen wäre unhöflich;
        # die Ursache steht ohnehin in der Meldung des Betriebssystems.
        raise GogdlError(f"Cannot create the lock file {path}: {exc}") from exc
    try:
        vorhanden = _read_lock(fd)
        if vorhanden.pid is not None and _prozess_lebt(vorhanden.pid):
            raise LockBusy(_belegt_meldung(path, vorhanden))
        if not _flock_frei(fd):
            raise LockBusy(_belegt_meldung(path, vorhanden))
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n{now_utc()}\n".encode())
    except BaseException:
        # Abgelehnt: schließen, aber nicht löschen - die Datei gehört dem
        # anderen Lauf.
        os.close(fd)
        raise

    try:
        yield path
    finally:
        # Erst löschen, dann schließen: nach dem Schließen fällt die
        # flock-Sperre, und ein wartender Lauf soll die Datei nicht mehr
        # vorfinden. Beide Schritte einzeln abgesichert, damit ein
        # fehlgeschlagenes Aufräumen keine durchgereichte Ausnahme verdeckt.
        try:
            path.unlink()
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


SAVE_DIRNAMES = frozenset({"savefiles"})
"""Verzeichnisse, die Spielstände statt Auslieferungen enthalten (casefold).

Bewusst kurz gehalten: Was hier steht, sieht das Werkzeug nicht mehr - ein
zu breiter Filter versteckt echten Bestand. Belegt ist ``SaveFiles/``; in
einer gewachsenen Sammlung liegen darunter die eigentlichen ``saves/``, die
deshalb keinen eigenen Eintrag brauchen. ``extras/`` gehört ausdrücklich
nicht dazu, dort liegen Goodies.
"""


def scan_disk(root: Path, *, include_saves: bool = True) -> dict[Path, int]:
    """Tatsächlicher Plattenzustand: Pfad -> Größe.

    Die Datei ist die Wahrheit, die Datenbank nur der Cache (KONZEPT.md §5.2).
    Der Zustandsordner und der Papierkorb bleiben außen vor.

    ``include_saves=False`` blendet zusätzlich die Spielstandsordner aus
    (:data:`SAVE_DIRNAMES`). Der Vorgabewert ist hier bewusst *nicht* die
    Voreinstellung der Befehlszeile: Wer den Parameter vergisst, bekommt zu
    viele Dateien zu sehen, nie zu wenige. Die Kommandos reichen die
    Entscheidung des Nutzers (``--include-saves``) ausdrücklich durch.
    """
    result: dict[Path, int] = {}
    if not root.exists():
        return result
    skip = {STATE_DIRNAME, TRASH_DIRNAME}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in skip and (include_saves or d.casefold() not in SAVE_DIRNAMES)
        ]
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
        include_extras=getattr(args, "extras", True),
        include_patches=getattr(args, "patches", False),
        include_saves=getattr(args, "include_saves", False),
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
