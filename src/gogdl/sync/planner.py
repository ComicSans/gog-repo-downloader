"""Vergleich Remote↔Lokal: Download-Arbeitsliste und Prune-Plan.

Das Herzstück des Tools (KONZEPT.md §4 und §5.5). Hier fallen die beiden
riskantesten Entscheidungen — „ist das veraltet?" und „darf das gelöscht
werden?".

**Dieses Modul ist strikt I/O-frei.** Kein Netzwerk, kein Dateisystem,
keine Datenbank: der beobachtete Plattenzustand kommt ausschließlich als
``on_disk``-Abbildung (Pfad → Größe) herein, die ein ``FileScanner``
außerhalb erhoben hat. Auch ``Path.exists()``/``Path.resolve()`` sind
deshalb tabu — ``resolve()`` würde Symlinks auflösen und damit die Platte
anfassen. Pfadvergleiche laufen rein algebraisch über
``Path.is_relative_to``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from gogdl.constants import STATE_DIRNAME, TRASH_DIRNAME
from gogdl.constants import OLD_SUFFIX
from gogdl.model.types import (
    DownloadItem,
    FileKind,
    LocalState,
    ManifestEntry,
    PruneItem,
    RemoteFile,
    Report,
    SlotKey,
    SyncConfig,
    SyncPlan,
)

__all__ = ["is_stale", "plan_downloads", "plan_prune"]

PART_SUFFIX = ".part"
"""Endung der noch unvollständigen Datei (KONZEPT.md §5.2)."""

MIN_PREFIX_MATCH = 8
"""Mindestlänge des Namensteils **vor** dem Versionstoken.

Der Teil vor der Version muss zwischen Kandidat und aktuellem Slot-Namen
*exakt* übereinstimmen (siehe :func:`_attribute_slot`); diese Konstante
verlangt zusätzlich, dass er lang genug ist, um überhaupt etwas
auszusagen. Ein kurzer Rest wie ``gog_`` ist kein Namensschema, sondern
ein Zufallstreffer - dann bleibt die Datei liegen und wird nicht
gelöscht.
"""

_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
"""Versionsähnliches Token im Dateinamen, z. B. ``2.1.0`` in
``setup_spiel_2.1.0_(1).bin``. Mehrteiligkeit (``_(1)``) matcht bewusst
nicht, weil mindestens ein Punkt gefordert ist."""

_DIGITS_RE = re.compile(r"\d+")
"""Jede Ziffernfolge - für Namensschema und Generationsvergleich."""

_OLD_RE = re.compile(re.escape(OLD_SUFFIX) + r"(?:\.(\d+))?$")
"""``<name>.old`` und ``<name>.old.<n>`` - vom Downloader beiseitegelegte
Vorgängerfassungen (:data:`gogdl.download.OLD_SUFFIX`). Der Downloader
zählt aufwärts, die höhere Nummer ist also die **jüngere** Fassung."""


# ---------------------------------------------------------------------------
# §4.2 — Aktualitätsprüfung
# ---------------------------------------------------------------------------


def is_stale(entry: ManifestEntry, remote: RemoteFile, *, strict_md5: bool = False) -> bool:
    """Ist der lokale Stand gegenüber dem Remote-Angebot veraltet?

    Präzedenz der Signale nach KONZEPT.md §4.2: ``version`` > ``size`` >
    ``md5``. Die Präzedenz bestimmt, *welche* Signale überhaupt gelten —
    nicht, dass nach dem ersten passenden Signal abgebrochen wird:
    **jede** Abweichung in einem geltenden Signal bedeutet veraltet.

    Regeln im Einzelnen:

    * Der **Dateiname ist kein Signal**. GOG lädt Installer still unter
      identischem Namen neu (§4.1); ein Namensvergleich sieht im
      Selbsttest korrekt aus und übersieht genau diesen Fall dauerhaft.
    * Ein **fehlender Wert auf einer Seite ist keine Abweichung**. Kein
      Signal ist nicht dasselbe wie ein negatives Signal — sonst würde
      jede Datei ohne Checksum-XML dauerhaft neu geladen.
    * Fehlt ``version`` auf **beiden** Seiten (so kommen Extras/Bonus
      herein), rückt ``size`` auf Rang 1 und ``md5`` wird zum Tiebreaker.
      ``md5`` gilt dann auch ohne ``strict_md5``, sofern beide Seiten
      einen Wert haben. Genau das meint §4.2 mit „bei Extras ist md5
      standardmäßig aktiv": maßgeblich ist das Fehlen der Version, nicht
      die ``FileKind``-Kennzeichnung.
    * Bei allem, was eine Version trägt (Installer, Patches), zählt
      ``md5`` nur bei ``strict_md5=True`` — ein MD5 kostet einen
      zusätzlichen Request pro Datei.
    """
    stale = False

    if entry.version is not None and remote.version is not None:
        if entry.version != remote.version:
            stale = True

    if entry.size is not None and remote.size is not None:
        if entry.size != remote.size:
            stale = True

    md5_applies = strict_md5 or (entry.version is None and remote.version is None)
    if md5_applies and entry.md5 and remote.md5:
        if entry.md5.strip().lower() != remote.md5.strip().lower():
            stale = True

    return stale


# ---------------------------------------------------------------------------
# Pfad- und Namenshilfen (rein algebraisch, ohne Plattenzugriff)
# ---------------------------------------------------------------------------


def _slug_dir(product_id: int, slugs: Mapping[int, str] | None) -> str:
    """Verzeichnisname eines Produkts unterhalb von ``dest``.

    ``RemoteFile`` trägt keinen ``slug`` (siehe Modul-Docstring von
    ``model.types``), deshalb kommt die Zuordnung als Parameter herein.
    Ohne Angabe dient die Produkt-ID als Verzeichnisname — eindeutig und
    stabil, wenn auch unschön.
    """
    if slugs:
        slug = slugs.get(product_id)
        if slug:
            return slug
    return str(product_id)


def _name_of(remote: RemoteFile, entry: ManifestEntry | None) -> str:
    """Dateiname für den Zielpfad; letzte Rückfallebene ist die File-ID."""
    if remote.filename:
        return remote.filename
    if entry is not None and entry.filename:
        return entry.filename
    return remote.file_id


def _entry_target(entry: ManifestEntry, dest: Path, slugs: Mapping[int, str] | None) -> Path:
    """Zielpfad eines Manifest-Eintrags — ``relative_path`` schlägt ``slug``."""
    if entry.relative_path:
        return dest / Path(entry.relative_path)
    return dest / _slug_dir(entry.product_id, slugs) / entry.filename


def _remote_target(
    remote: RemoteFile,
    entry: ManifestEntry | None,
    dest: Path,
    slugs: Mapping[int, str] | None,
) -> Path:
    """Zielpfad einer Remote-Datei: flaches Layout ``<dest>/<slug>/<datei>``.

    Das **Verzeichnis** stammt aus dem Manifest-Eintrag, sofern es einen
    gibt — so bleibt ein importierter gogrepo-Bestand an seinem Ort. Der
    **Dateiname** kommt dagegen immer vom Remote-Stand: benennt GOG einen
    Installer um (§4.1, Fall 1), kommt die neue Datei **neben** die alte
    (§5.2) und überschreibt sie nicht. Die Altversion bleibt damit als
    Prune-Kandidat erhalten, statt still zu verschwinden.
    """
    name = _name_of(remote, entry)
    if entry is not None and entry.relative_path:
        return (dest / Path(entry.relative_path)).parent / name
    return dest / _slug_dir(remote.product_id, slugs) / name


def _part_of(target: Path) -> Path:
    return target.with_name(target.name + PART_SUFFIX)


def _is_inside(path: Path, dest: Path) -> bool:
    """Liegt ``path`` unterhalb von ``dest``? Reine Pfadalgebra, kein I/O."""
    return path != dest and path.is_relative_to(dest)


def _is_tool_internal(path: Path, dest: Path) -> bool:
    """Papierkorb und Zustandsverzeichnis sind kein Fremdbestand."""
    try:
        first = path.relative_to(dest).parts[0]
    except (ValueError, IndexError):
        return False
    return first in (TRASH_DIRNAME, STATE_DIRNAME)


def _relevant_disk(
    on_disk: Mapping[Path, int] | None, dest: Path
) -> dict[Path, int]:
    """Plattenzustand auf ``dest`` beschränken — hartes Sicherheitsnetz (§5.5)."""
    if not on_disk:
        return {}
    return {
        path: size
        for path, size in on_disk.items()
        if _is_inside(path, dest) and not _is_tool_internal(path, dest)
    }


def _strip_part(name: str) -> str:
    return name[: -len(PART_SUFFIX)] if name.endswith(PART_SUFFIX) else name


def _split_old(name: str) -> tuple[str, int] | None:
    """``<basis>.old`` / ``<basis>.old.<n>`` zerlegen.

    Rückgabe ist ``(basisname, generation)`` oder ``None``, wenn der Name
    gar nicht beiseitegelegt wurde. ``<name>.old`` zählt als Generation
    ``0``, jede durchnummerierte Fassung entsprechend höher - der
    Downloader legt ``.old`` zuerst an und zählt bei Kollision aufwärts,
    die höhere Nummer ist also die jüngere Fassung.

    Diese Endung vergibt **nur** der Downloader. Sie ist damit der einzige
    Fall, in dem eine Datei ohne eigenes Versionstoken trotzdem als
    Vorgängerfassung gilt: ``manual.pdf.old`` neben ``manual.pdf`` sieht
    der Namensheuristik zwar aus wie handverlesener Fremdbestand, ist aber
    nachweislich vom Werkzeug selbst angelegt.
    """
    treffer = _OLD_RE.search(name)
    if treffer is None:
        return None
    basis = name[: treffer.start()]
    if not basis:
        return None
    return basis, int(treffer.group(1) or 0)


def _casefold(path: Path) -> str:
    """Pfad für den Vergleich auf einem case-insensitiven Volume normieren.

    macOS (und jedes andere case-insensitive Dateisystem) liefert im
    Plattenzustand die Schreibweise, die auf der Platte steht. Weicht sie
    von ``relative_path`` ab - weil der Nutzer umbenannt hat oder ein
    rsync von einem case-sensitiven Volume kam -, würde ein
    case-sensitiver Vergleich die **aktuelle** Datei für unbekannt halten
    und sie zum Löschkandidaten machen. Deshalb wird casefold verglichen.
    """
    return str(path).casefold()


def _split_version(name: str) -> tuple[str, str, tuple[int, ...], str] | None:
    """Dateinamen am letzten Versionstoken zerlegen.

    Rückgabe ist ``(präfix, token, sortierschlüssel, suffix)`` oder
    ``None``, wenn der Name gar kein Versionstoken trägt.

    Genommen wird das **letzte** versionsähnliche Token: GOG setzt die
    Version ans Ende, direkt vor einen etwaigen Teilindex
    (``setup_spiel_2.1.0_(1).bin``), während im Spieltitel selbst
    Punkt-Ziffern-Folgen vorkommen können.
    """
    treffer = list(_VERSION_RE.finditer(name))
    if not treffer:
        return None
    letzter = treffer[-1]
    token = letzter.group(0)
    schluessel = tuple(int(part) for part in token.split("."))
    return name[: letzter.start()], token, schluessel, name[letzter.end() :]


def _parse_version(name: str) -> tuple[str, tuple[int, ...]] | None:
    """Nur Token und Sortierschlüssel - Bequemlichkeitshülle."""
    zerlegt = _split_version(name)
    if zerlegt is None:
        return None
    return zerlegt[1], zerlegt[2]


def _schema(name: str) -> str:
    """Namensschema: jede Ziffernfolge durch ``#`` ersetzt, casefold.

    Zwei Fassungen derselben Auslieferung unterscheiden sich nur in ihren
    Zahlen - Version, Update-Nummer, Build-Nummer. Alles andere ist das
    Schema, und das muss übereinstimmen. Genau daran scheitert
    ``soundtrack_flac.zip`` neben ``soundtrack.zip``, und genau daran
    kommt der echte GOG-Fall vorbei::

        setup_the_witcher_3_wild_hunt_4.04a_redkit_update_1_(73519).exe
        setup_the_witcher_3_wild_hunt_4.04a_redkit_update_2_(73883).exe

    Beide tragen dasselbe Schema und unterscheiden sich nur in Zahlen -
    hier zählt die Update- und Build-Nummer, nicht das Versionstoken, das
    in beiden Namen identisch ist.
    """
    return _DIGITS_RE.sub("#", name).casefold()


def _digits(name: str) -> tuple[int, ...]:
    """Alle Ziffernfolgen eines Namens der Reihe nach.

    Bei gleichem Schema ist das der vollständige Generationsvergleich:
    ``(3, 4, 4, 1, 73519) < (3, 4, 4, 2, 73883)``. Das Versionstoken
    allein genügt dafür nicht, weil GOG die Generation auch in einer
    Update- oder Build-Nummer hinter der Version führt.
    """
    return tuple(int(treffer) for treffer in _DIGITS_RE.findall(name))


# ---------------------------------------------------------------------------
# Filter (§6)
# ---------------------------------------------------------------------------


_OsKey = tuple[int, FileKind]
"""Schlüssel der Plattformwahl: Produkt + Dateiart."""

_LangKey = tuple[int, FileKind, str | None]
"""Schlüssel der Sprachwahl: Produkt + Dateiart + Plattform."""


def _select_axes(
    remote: Sequence[RemoteFile], config: SyncConfig
) -> tuple[dict[_OsKey, frozenset[str]], dict[_LangKey, frozenset[str]]]:
    """Plattform- und Sprachwahl pro Produkt treffen (§6).

    Beide Achsen werden aus dem **tatsächlich angebotenen** Bestand
    entschieden, denn nur so greifen die Rückfallebenen von
    :class:`~gogdl.model.types.Preference`: „deutsch, sonst englisch" kann
    man erst beantworten, wenn man weiß, was es gibt.

    Drei Feinheiten, an denen die Auswahl sonst falsch wird:

    * Die Wahl fällt **pro FileKind getrennt**. Patches folgen derselben
      Logik wie Installer, aber ein Spiel kann Installer für drei
      Plattformen und Patches nur für eine anbieten - eine gemeinsame
      Entscheidung würde die Patches der übrigen Plattformen mitnehmen
      oder verwerfen.
    * Die Sprache wird **je gewählter Plattform getrennt** entschieden.
      Ein Spiel kann auf Windows deutsch anbieten und auf Mac nur
      englisch; eine global über alle Plattformen getroffene Sprachwahl
      würde bei ``--lang de,en`` die Mac-Fassung stumm verschlucken.
    * Beide Achsen kaskadieren **gemeinsam**: Eine Plattform-Ebene gilt
      nur als getroffen, wenn mindestens eine ihrer Plattformen auch eine
      akzeptable Sprache anbietet. „Mac, sonst Windows" plus „deutsch"
      heißt nicht „Mac um jeden Preis" - eine Mac-Fassung, die es nur auf
      Chinesisch gibt, erfüllt den Wunsch nicht und darf die Rückfallebene
      auf Windows nicht blockieren. Ohne diese Kopplung käme in dem Fall
      gar nichts, obwohl der Nutzer das Spiel auf Deutsch besitzt.

    Innerhalb der getroffenen Ebene gibt es **keinen** weiteren Rückfall:
    Plattformen ohne akzeptable Sprache fallen einfach heraus. Bei
    ``--os linux+mac --lang de,en`` mit Linux auf Deutsch und Mac nur auf
    Englisch bleiben beide, jede in ihrer Sprache.

    Die Reihenfolge der beiden Rückfälle ist damit festgelegt: erst
    erschöpft die Sprache ihre Ebenen **innerhalb** einer Plattform, dann
    erst rückt die Plattform-Ebene weiter. ``--os mac,windows --lang
    de,en`` mit Mac auf Englisch und Windows auf Deutsch liefert deshalb
    Mac/en - nicht Windows/de.

    ``variant`` geht bewusst in keinen Schlüssel ein: die verschiedenen
    Versionsspannen eines Patches sind dieselbe Auslieferung in
    Plattform/Sprache und teilen sich deshalb die Auswahl.

    Rückwärtskompatibilität: Ist die jeweilige ``Preference`` leer, gilt
    weiterhin die flache Menge (``os_filter``/``languages``). Ist auch die
    leer, gibt es auf dieser Achse keine Einschränkung. Eine gesetzte
    ``Preference`` **ersetzt** die Menge, sie schneidet sich nicht mit ihr
    - sonst könnte eine Rückfallebene nie greifen, die außerhalb der Menge
    liegt. Ohne Sprach-Preference gibt es auch keine Kopplung: dann gilt
    jede Plattform-Ebene als getroffen, sobald sie überhaupt angeboten
    wird.
    """
    angeboten_os: dict[_OsKey, set[str]] = {}
    angeboten_lang: dict[_LangKey, set[str]] = {}
    for file in remote:
        slot = file.slot
        os_value = slot.os.value if slot.os is not None else None
        if os_value is not None:
            angeboten_os.setdefault((slot.product_id, slot.kind), set()).add(os_value)
        if slot.language is not None:
            key = (slot.product_id, slot.kind, os_value)
            angeboten_lang.setdefault(key, set()).add(slot.language.lower())

    gewaehlt_lang: dict[_LangKey, frozenset[str]] = {
        lang_key: _sprachwahl(values, config) for lang_key, values in angeboten_lang.items()
    }

    def traegt(os_key: _OsKey, os_value: str) -> bool:
        """Bietet diese Plattform eine Sprache an, die der Wunsch akzeptiert?

        Ohne Sprach-Preference wird nicht gekoppelt (siehe Docstring).
        Trägt die Plattform gar keine Sprachdimension, gibt es nichts zu
        erfüllen und sie zählt als tragfähig.
        """
        if not config.language_preference:
            return True
        key = (os_key[0], os_key[1], os_value)
        if not angeboten_lang.get(key):
            return True
        return bool(gewaehlt_lang.get(key))

    gewaehlt_os: dict[_OsKey, frozenset[str]] = {}
    for os_key, values in angeboten_os.items():
        if config.os_preference:
            treffer: frozenset[str] = frozenset()
            for level in config.os_preference.levels:
                kandidaten = {v for v in values if v in level}
                treffer = frozenset(v for v in kandidaten if traegt(os_key, v))
                if treffer:
                    break
            gewaehlt_os[os_key] = treffer
        elif config.os_filter:
            erlaubt = {member.value for member in config.os_filter}
            gewaehlt_os[os_key] = frozenset(v for v in values if v in erlaubt)
        else:
            gewaehlt_os[os_key] = frozenset(values)

    return gewaehlt_os, gewaehlt_lang


def _sprachwahl(angeboten: set[str], config: SyncConfig) -> frozenset[str]:
    """Sprachauswahl für **eine** Plattform eines Produkts.

    Die Preference schlägt die flache Menge; ohne beides gibt es keine
    Einschränkung.
    """
    if config.language_preference:
        return config.language_preference.select(angeboten)
    if config.languages:
        erlaubt = {lang.lower() for lang in config.languages}
        return frozenset(v for v in angeboten if v in erlaubt)
    return frozenset(angeboten)


def _passes_filters(
    remote: RemoteFile,
    config: SyncConfig,
    dlc_of: Mapping[int, int] | None,
    gewaehlt_os: Mapping[_OsKey, frozenset[str]],
    gewaehlt_lang: Mapping[_LangKey, frozenset[str]],
) -> bool:
    """Greifen die konfigurierten Filter für diese Datei?

    OS- und Sprachfilter wirken nur, wenn die Datei das jeweilige Signal
    überhaupt trägt. **Extras haben ``os``/``language`` gleich ``None``
    und dürfen davon nicht herausgefiltert werden** — sie werden allein
    über ``include_extras`` gesteuert.

    Die Auswahl selbst ist in :func:`_select_axes` gefallen; hier wird nur
    noch nachgeschlagen.
    """
    slot = remote.slot
    kind = slot.kind

    if kind is FileKind.EXTRA and not config.include_extras:
        return False
    if kind is FileKind.PATCH and not config.include_patches:
        return False

    if not config.include_dlc:
        parent = dlc_of.get(remote.product_id) if dlc_of else remote.dlc_of
        if parent is not None and parent != remote.product_id:
            return False

    os_value = slot.os.value if slot.os is not None else None
    if os_value is not None:
        erlaubte_os = gewaehlt_os.get((slot.product_id, kind))
        if erlaubte_os is None or os_value not in erlaubte_os:
            return False

    if slot.language is not None:
        erlaubte_lang = gewaehlt_lang.get((slot.product_id, kind, os_value))
        if erlaubte_lang is None or slot.language.lower() not in erlaubte_lang:
            return False

    return True


# ---------------------------------------------------------------------------
# §4/§5 — Download-Planung
# ---------------------------------------------------------------------------


def _download_entry(
    remote: RemoteFile,
    existing: ManifestEntry | None,
    target: Path,
    dest: Path,
    resume_from: int,
    stale: bool,
) -> ManifestEntry:
    """Manifest-Eintrag für den Download — mit den **Remote**-Werten.

    Der Downloader verifiziert gegen ``entry.size``/``entry.md5``. Würde
    hier der alte lokale Stand durchgereicht, schlüge die Verifikation
    jeder veralteten Datei fehl.
    """
    relative = target.relative_to(dest) if _is_inside(target, dest) else Path(target.name)
    if stale:
        state = LocalState.STALE
    elif resume_from > 0:
        state = LocalState.PARTIAL
    else:
        state = LocalState.MISSING
    return ManifestEntry(
        slot=remote.slot,
        file_id=remote.file_id,
        filename=_name_of(remote, existing),
        version=remote.version,
        size=remote.size,
        md5=remote.md5,
        downlink=remote.downlink,
        part_index=remote.part_index,
        total_parts=remote.total_parts,
        relative_path=str(relative),
        state=state,
        bytes_done=resume_from,
        last_seen_utc=existing.last_seen_utc if existing is not None else None,
        last_verified_utc=None,
        dlc_of=remote.dlc_of if remote.dlc_of is not None else (
            existing.dlc_of if existing is not None else None
        ),
    )


def plan_downloads(
    remote: Sequence[RemoteFile],
    local: Sequence[ManifestEntry],
    config: SyncConfig,
    on_disk: Mapping[Path, int] | None = None,
    *,
    slugs: Mapping[int, str] | None = None,
    dlc_of: Mapping[int, int] | None = None,
) -> SyncPlan:
    """Arbeitsliste der zu ladenden Dateien plus Meldungen.

    Erzeugt ``downloads`` und ``reports``; ``prunes`` bleibt leer, dafür
    ist :func:`plan_prune` zuständig.

    Zielpfad ist das flache Layout ``<dest>/<slug>/<datei>`` (§10.3).
    ``RemoteFile`` kennt keinen ``slug``, deshalb der Zusatzparameter
    ``slugs`` (Produkt-ID → Verzeichnisname); ohne ihn dient
    ``str(product_id)`` als Verzeichnis.

    ``dlc_of`` (Produkt-ID → Produkt-ID des Hauptspiels, §4.3) steuert
    ``include_dlc`` und **übersteuert** die Angabe an der Datei: ist die
    Abbildung gesetzt, gilt allein sie. Fehlt sie, kommt die Zugehörigkeit
    aus ``RemoteFile.dlc_of`` (die api-Schicht füllt das Feld). Trägt auch
    die Datei nichts, wird nicht nach DLC gefiltert - geraten wird hier
    nichts.

    Plattform- und Sprachwahl fallen pro Produkt in :func:`_select_axes`,
    inklusive der Rückfallebenen aus ``os_preference``/
    ``language_preference``. Dateien, deren Manifest-Eintrag ``ORPHANED``
    ist, gehen dort nicht in den Bestand ein: sie werden ohnehin nicht
    geladen und dürfen deshalb keine Plattform „belegen", für die es dann
    nichts zu tun gibt.

    Der lokale Zustand wird ausschließlich aus ``on_disk`` abgeleitet,
    nie aus dem Manifest — die Datei ist die Wahrheit, die DB nur der
    Cache (§5.2):

    * Ziel liegt mit erwarteter Größe da → nichts zu tun.
    * ``<ziel>.part`` liegt da → ``resume_from`` ist dessen Größe.
    * Sonst ``resume_from=0``.

    Veraltete Dateien kommen in die Liste, auch wenn lokal vollständig —
    dann allerdings mit ``resume_from=0``: in eine ``.part``-Datei der
    alten Version weiterzuschreiben wäre stille Korruption (§5.1).

    Einträge mit ``state == ORPHANED`` werden nie geladen, sondern als
    ``Report(kind="orphaned")`` gemeldet (§5.5). Dateien unterhalb von
    ``dest``, die zu keinem Manifest-/Remote-Eintrag gehören, erscheinen
    als ``Report(kind="foreign")``. Die Menge der bekannten Pfade wird
    dabei aus den **ungefilterten** Remote- und Lokaldaten gebildet — ein
    ``--os``-Filter darf keine Fremdmeldungen erfinden. ``.part``-Dateien
    zu bekannten Einträgen sind ebenfalls nicht fremd.

    **Ziel-Kollisionen** (``Report(kind="collision")``): Das Layout ist
    flach, zwei Slots desselben Produkts können denselben Dateinamen
    tragen (Installer und Extra heißen beide ``doku.pdf``). Beide würden
    in dieselbe Datei und bei ``--jobs 2`` gleichzeitig in dieselbe
    ``.part`` schreiben - stille Korruption. Der erste Eintrag behält den
    Pfad, jeder weitere wird abgelehnt und gemeldet.
    """
    plan = SyncPlan()
    dest = config.dest
    disk = _relevant_disk(on_disk, dest)

    by_key: dict[tuple[SlotKey, str], ManifestEntry] = {
        (entry.slot, entry.file_id): entry for entry in local
    }

    # Bekannte Pfade aus ungefilterten Daten — Filter dürfen keine
    # Fremdmeldungen erzeugen.
    known: set[Path] = set()
    for entry in local:
        target = _entry_target(entry, dest, slugs)
        known.add(target)
        known.add(_part_of(target))
    for remote_file in remote:
        target = _remote_target(
            remote_file, by_key.get((remote_file.slot, remote_file.file_id)), dest, slugs
        )
        known.add(target)
        known.add(_part_of(target))

    for entry in local:
        if entry.state is LocalState.ORPHANED:
            plan.reports.append(
                Report(
                    path=_entry_target(entry, dest, slugs),
                    kind="orphaned",
                    detail=(
                        f"{entry.filename}: no longer offered by GOG "
                        f"(slot {entry.slot.as_str()}) - left in place"
                    ),
                )
            )

    planbar = [
        remote_file
        for remote_file in remote
        if (entry := by_key.get((remote_file.slot, remote_file.file_id))) is None
        or entry.state is not LocalState.ORPHANED
    ]
    gewaehlt_os, gewaehlt_lang = _select_axes(planbar, config)

    belegt: dict[Path, RemoteFile] = {}
    for remote_file in planbar:
        entry = by_key.get((remote_file.slot, remote_file.file_id))
        if not _passes_filters(remote_file, config, dlc_of, gewaehlt_os, gewaehlt_lang):
            continue

        target = _remote_target(remote_file, entry, dest, slugs)

        vorbesitzer = belegt.get(target)
        if vorbesitzer is not None:
            plan.reports.append(
                Report(
                    path=target,
                    kind="collision",
                    detail=(
                        f"Target path already taken by slot "
                        f"{vorbesitzer.slot.as_str()} / {vorbesitzer.file_id} - "
                        f"slot {remote_file.slot.as_str()} / {remote_file.file_id} "
                        f"will not be downloaded"
                    ),
                )
            )
            continue
        belegt[target] = remote_file

        stale = entry is not None and is_stale(
            entry, remote_file, strict_md5=config.strict_md5
        )

        resume_from = 0
        if not stale:
            present = disk.get(target)
            # Ohne bekannte Remote-Größe gibt es kein Maß für „vollständig";
            # dann gilt die vorhandene Datei als erledigt, statt sie bei
            # jedem Lauf erneut zu ziehen.
            if present is not None and (remote_file.size is None or present == remote_file.size):
                continue
            resume_from = disk.get(_part_of(target), 0)
            if remote_file.size is not None and resume_from > remote_file.size:
                # Größer als erwartet: die Teildatei gehört nicht zu diesem
                # Stand. Anhängen wäre Korruption, also von vorn.
                resume_from = 0

        plan.downloads.append(
            DownloadItem(
                entry=_download_entry(remote_file, entry, target, dest, resume_from, stale),
                target=target,
                resume_from=resume_from,
            )
        )

    for path in disk:
        if path in known:
            continue
        # Vom Downloader beiseitegelegte Vorgängerfassungen sind kein
        # Fremdbestand: eine Warnung über Dateien, die das Werkzeug gerade
        # selbst angelegt hat, wäre irreführend. Aufgeräumt werden sie von
        # ``plan_prune``, sobald der Slot verifiziert vollständig vorliegt.
        beiseite = _split_old(path.name)
        if beiseite is not None and path.with_name(beiseite[0]) in known:
            continue
        plan.reports.append(
            Report(
                path=path,
                kind="foreign",
                detail="not created by this tool - left untouched",
            )
        )

    plan.downloads.sort(key=lambda item: (item.entry.slot.as_str(), item.entry.file_id))
    plan.reports.sort(key=lambda report: (report.kind, str(report.path)))
    return plan


# ---------------------------------------------------------------------------
# §5.5 — Prune-Planung
# ---------------------------------------------------------------------------


def _slot_is_replaceable(
    entries: Sequence[ManifestEntry],
    dest: Path,
    slugs: Mapping[int, str] | None,
    disk_norm: Mapping[str, int],
) -> bool:
    """Darf dieser Slot eine Vorgängerversion ersetzen?

    Zwei Vorbedingungen, und beide müssen erfüllt sein:

    1. **Alle** Dateien des Slots sind ``is_verified_complete`` (Zustand
       ``COMPLETE`` *und* ``last_verified_utc`` gesetzt). Große Installer
       sind mehrteilig; wird nach jeder fertigen Einzeldatei aufgeräumt,
       verschwindet Teil 1 der alten Version, während Teil 2 der neuen
       noch fehlt - und es gibt gar keine vollständige Version mehr
       (§5.5).
    2. Jede dieser Dateien liegt **tatsächlich auf der Platte**, mit der
       erwarteten Größe (soweit bekannt). §5.5 verlangt einen Ersatz auf
       der Platte, nicht einen Ersatz im Manifest: das Manifest kann
       ``COMPLETE`` behaupten, während die Datei längst verschoben,
       gelöscht oder auf einem gerade nicht eingehängten Volume ist.
       Ohne diese Prüfung würde die Altversion gelöscht und es bliebe gar
       nichts.

    Der Plattenvergleich läuft casefold (siehe :func:`_casefold`).
    """
    if not entries:
        return False
    if any(entry.state is LocalState.ORPHANED for entry in entries):
        return False
    if not all(entry.is_verified_complete for entry in entries):
        return False
    for entry in entries:
        vorhanden = disk_norm.get(_casefold(_entry_target(entry, dest, slugs)))
        if vorhanden is None:
            return False
        if entry.size is not None and vorhanden != entry.size:
            return False
    return True


def _attribute_slot(
    candidate: str, slot_names: Mapping[SlotKey, tuple[str, ...]]
) -> SlotKey | None:
    """Ordne eine unbekannte Datei genau dann einem Slot zu, wenn sie
    nachweislich eine ältere Fassung *dieses* Namensschemas ist.

    Die Zuordnung ist bewusst eng, denn sie autorisiert eine Löschung.
    Fünf Bedingungen, alle zwingend:

    1. Der Kandidat trägt selbst ein Versionstoken
       (:func:`_split_version`). Ohne Token ist er keine „Altversion",
       sondern Fremdbestand - ``manual_v1_scan.pdf`` neben ``manual.pdf``
       ist handverlesenes Material des Nutzers, kein GOG-Rest.
    2. Der aktuelle Slot-Dateiname trägt **ebenfalls** ein Versionstoken.
       Ein unversionierter Slot (typisch für Extras) kann gar keine
       Vorgängerversion haben.
    3. Der Namensteil **vor** dem Token stimmt exakt überein
       (Groß-/Kleinschreibung ignoriert) und ist mindestens
       ``MIN_PREFIX_MATCH`` Zeichen lang. Das trennt ``setup_spiel1_…``
       von ``setup_spiel2_…``, was ein reiner Schemavergleich nicht
       könnte.
    4. Beide Namen tragen dasselbe :func:`_schema`, unterscheiden sich
       also **nur in Zahlen**. Reine Präfixlänge genügt nicht:
       ``soundtrack_flac.zip`` teilt zehn Zeichen mit ``soundtrack.zip``
       und ist trotzdem etwas völlig anderes.
    5. Der Kandidat ist über :func:`_digits` **echt älter**. Verglichen
       werden alle Ziffernfolgen der Reihe nach, nicht nur das
       Versionstoken: GOG führt die Generation auch in einer Update- oder
       Build-Nummer hinter einer gleichbleibenden Version. Was neuer
       aussieht als der aktuelle Stand, gilt nie als Altversion.

    Passt mehr als ein Slot, ist die Zuordnung mehrdeutig und es wird
    nichts gelöscht.

    Bleibende Lücke, bewusst in Kauf genommen: Fehlt ein Teil eines
    mehrteiligen Installers im Manifest, während ein anderer Teil
    derselben Version dort steht, sieht der fehlende Teil wie eine
    ältere Generation aus (kleinerer Teilindex). Das war vorher nicht
    anders; die Slot-Regel in :func:`_slot_is_replaceable` fängt es nur
    ab, solange das Manifest den Slot vollständig kennt.
    """
    stamm = _strip_part(candidate)
    zerlegt = _split_version(stamm)
    if zerlegt is None:
        return None
    praefix = zerlegt[0]
    if len(praefix) < MIN_PREFIX_MATCH:
        return None
    schema, ziffern = _schema(stamm), _digits(stamm)

    treffer: set[SlotKey] = set()
    for slot, names in slot_names.items():
        for name in names:
            aktuell = _split_version(name)
            if aktuell is None:
                continue
            if praefix.casefold() != aktuell[0].casefold():
                continue
            if schema != _schema(name):
                continue
            if ziffern >= _digits(name):
                continue
            treffer.add(slot)
            break

    if len(treffer) != 1:
        return None
    return next(iter(treffer))


def _keep_newest_generations(
    candidates: Sequence[tuple[Path, int, str]], keep_old: int
) -> list[tuple[Path, int, str]]:
    """Wähle aus den Altdateien eines Slots die löschbaren aus.

    ``keep_old`` ist ``keep_versions - 1``, also die Zahl der zusätzlich
    aufzubewahrenden Generationen (``--keep-versions 1`` = nur die
    aktuelle, ``2`` = eine Rückfallebene, §5.5).

    Dateien ohne erkennbares Versionstoken fallen **bei jeder**
    Einstellung heraus, nicht erst ab ``keep_old >= 1``. Sonst wäre
    ausgerechnet der Default ``keep_versions=1`` die gefährlichste
    Einstellung: er löschte Dateien, die eine höhere Einstellung
    verschont - das erwartet niemand. Ohne Token lässt sich eine Datei
    weder einsortieren noch als Vorgängerversion belegen.

    Damit gilt: die Löschmenge von ``keep_versions=n+1`` ist stets eine
    Teilmenge der von ``keep_versions=n``.
    """
    versioned: dict[tuple[int, ...], list[tuple[Path, int, str]]] = {}
    for path, size, name in candidates:
        parsed = _parse_version(name)
        if parsed is None:
            continue
        token, key = parsed
        versioned.setdefault(key, []).append((path, size, token))

    doomed: list[tuple[Path, int, str]] = []
    for key in sorted(versioned, reverse=True)[max(keep_old, 0) :]:
        doomed.extend(versioned[key])
    return doomed


def _slot_of_basis(
    basis: str, slot_names: Mapping[SlotKey, tuple[str, ...]]
) -> SlotKey | None:
    """Slot einer beiseitegelegten Datei über ihren Basisnamen.

    Der Regelfall des stillen Neu-Uploads (§4.1): die neue Fassung trägt
    denselben Namen, der Basisname ist deshalb **wörtlich** der aktuelle
    Slot-Dateiname. Nur wenn das nicht zutrifft - GOG hat zwischenzeitlich
    auch noch umbenannt -, greift ersatzweise die Namensheuristik.
    """
    treffer = {
        slot
        for slot, names in slot_names.items()
        if any(basis.casefold() == name.casefold() for name in names)
    }
    if len(treffer) == 1:
        return next(iter(treffer))
    if treffer:
        return None  # mehrdeutig - dann lieber gar nichts
    return _attribute_slot(basis, slot_names)


def _keep_newest_old_generations(
    candidates: Sequence[tuple[Path, int, int]], keep_old: int
) -> list[tuple[Path, int, int]]:
    """Beiseitegelegte Fassungen eines Basisnamens staffeln.

    Das Versionstoken taugt hier nicht als Reihenfolge: beim stillen
    Neu-Upload heißen alle Generationen gleich und trügen dasselbe Token.
    Alle landeten in einem Bucket - bei ``--keep-versions 2`` wäre dann
    nie etwas löschbar und die Dateien häuften sich unbegrenzt an, bei
    ``--keep-versions 1`` fielen umgekehrt alle auf einmal weg. Maßgeblich
    ist deshalb die laufende Nummer im Suffix; die höchste ist die
    jüngste Fassung und wird als erste verschont.
    """
    geordnet = sorted(candidates, key=lambda eintrag: eintrag[2], reverse=True)
    return geordnet[max(keep_old, 0) :]


def _verwaltet_die_konfiguration(entries: Sequence[ManifestEntry], config: SyncConfig) -> bool:
    """Räumt die aktuelle Konfiguration in diesem Slot überhaupt auf?

    ``plan_prune`` bekommt den **gesamten** Manifestbestand, nicht die
    gefilterte Auswahl. Ohne diese Prüfung räumt ``--no-extras`` das
    Extras-Umfeld trotzdem auf: der Nutzer hat gesagt, dass er sich um
    Extras nicht kümmern will, und bekommt dort trotzdem Löschungen.

    Plattform und Sprache gehen bewusst **nicht** ein: sie steuern, was
    geladen wird, nicht was bereits im Bestand liegt. Eine schon
    vorhandene Mac-Fassung soll auch dann noch ihre Altversionen los
    werden, wenn der Nutzer heute mit ``--os windows`` läuft.
    """
    kind = entries[0].slot.kind
    if kind is FileKind.EXTRA and not config.include_extras:
        return False
    if kind is FileKind.PATCH and not config.include_patches:
        return False
    if not config.include_dlc:
        parent = next((e.dlc_of for e in entries if e.dlc_of is not None), None)
        if parent is not None and parent != entries[0].product_id:
            return False
    return True


def plan_prune(
    local: Sequence[ManifestEntry],
    config: SyncConfig,
    on_disk: Mapping[Path, int] | None = None,
    *,
    slugs: Mapping[int, str] | None = None,
) -> SyncPlan:
    """Plan der löschbaren Altversionen — und **nur** dieser (§5.5).

    Prune-Einheit ist der **Slot** (``product_id`` + ``kind`` + ``os`` +
    ``language``), nicht die Einzeldatei. Ein Eintrag entsteht nur, wenn
    *alle* Dateien der aktuellen Version des Slots
    ``is_verified_complete`` sind; ist auch nur ein Teil eines
    mehrteiligen Installers unvollständig oder unverifiziert, bleibt der
    ganze Slot unangetastet.

    Nie im Plan: ``ORPHANED``-Einträge, Fremdbestand und alles außerhalb
    von ``config.dest``. ``config.prune=False`` liefert einen leeren Plan.

    **Heuristik „alte Version"** — ``ManifestEntry`` beschreibt nur den
    *aktuellen* Remote-Stand; die Vorgängerversion steht in keiner
    Datenstruktur mehr. Sie wird deshalb aus ``on_disk`` erschlossen:

    1. Kandidat ist eine Datei **direkt** in ``<dest>/<slug>/`` eines
       Produkts, das Manifest-Einträge hat, und deren Pfad zu keinem
       aktuellen Eintrag (und zu keiner ``.part``-Datei eines aktuellen
       Eintrags) gehört. Der Vergleich läuft casefold, sonst wird auf
       einem case-insensitiven Volume die **aktuelle** Datei zum
       Kandidaten, nur weil ihre Schreibweise von ``relative_path``
       abweicht. Das Verzeichnis wird aus ``dest`` und dem Slug gebildet,
       nicht aus ``relative_path``; Unterverzeichnisse wie
       ``<slug>/extras/`` bleiben vollständig außen vor - auch für Slots,
       die selbst dort liegen.
    2. Der Kandidat trägt nachweislich dasselbe Namensschema wie der Slot
       und ein **älteres** Versionstoken (:func:`_attribute_slot`). Ohne
       eindeutigen Treffer bleibt er liegen.
    3. Gelöscht wird nur, wenn dieser Slot vollständig, verifiziert
       **und auf der Platte vorhanden** ist (:func:`_slot_is_replaceable`).
    4. Der Slot muss von der aktuellen Konfiguration überhaupt verwaltet
       werden (:func:`_verwaltet_die_konfiguration`).

    Warum das nur bei nachweislich vollständigem Ersatz greift: Schritt 1
    kann eine Datei nicht von echtem Fremdbestand unterscheiden — der
    Name allein sagt nichts darüber, ob das Tool sie angelegt hat. Erst
    die Kombination „liegt im Produktverzeichnis + trägt das Namensschema
    des Slots samt älterer Version + der Slot liegt vollständig
    verifiziert daneben" macht die Annahme *Vorgängerversion* belastbar.
    Fällt eine dieser Bedingungen weg, ist die Datei nach §5.5
    Fremdbestand: sie bleibt und wird allenfalls gemeldet (durch
    :func:`plan_downloads`), nie gelöscht.

    ``.part``-Reste zu einer nicht mehr angebotenen Version dürfen
    ebenfalls in den Plan; sie sind nie eine Rückfallebene und deshalb
    von ``keep_versions`` ausgenommen. Auch sie müssen dafür das
    Namensschema samt älterer Version tragen.

    **Beiseitegelegte Fassungen** (``<name>.old``, ``<name>.old.<n>``,
    siehe :data:`gogdl.download.OLD_SUFFIX`) laufen auf einer eigenen
    Spur. Der Downloader legt sie an, wenn GOG eine neue Fassung unter
    identischem Dateinamen ausliefert (§4.1); sie sind ausdrücklich kein
    Fremdbestand, obwohl ihr Name zu keinem Manifest-Eintrag passt, und
    brauchen deshalb auch kein eigenes Versionstoken. Die Vorbedingung
    ist dieselbe wie sonst: der Slot muss vollständig, verifiziert und auf
    der Platte vorhanden sein. Gestaffelt werden sie über die laufende
    Nummer im Suffix, nicht über das Versionstoken - beim stillen
    Neu-Upload heißen alle Generationen gleich
    (:func:`_keep_newest_old_generations`). Beide Spuren haben ihr eigenes
    ``keep_versions``-Budget: liegt neben einer echten Altversion auch
    noch eine beiseitegelegte Fassung, bleiben bei ``keep_versions=2``
    zwei Dateien liegen statt einer. Im Zweifel bleibt mehr liegen, nie
    weniger - die beiden Spuren sind nicht vergleichbar, und ein
    gemeinsamer Rang wäre geraten.
    """
    plan = SyncPlan()
    if not config.prune:
        return plan

    dest = config.dest
    disk = _relevant_disk(on_disk, dest)
    if not disk:
        return plan
    disk_norm = {_casefold(path): size for path, size in disk.items()}

    by_slot: dict[SlotKey, list[ManifestEntry]] = {}
    known: set[str] = set()
    slot_dirs: dict[SlotKey, Path] = {}
    verschachtelt: set[SlotKey] = set()
    for entry in local:
        target = _entry_target(entry, dest, slugs)
        if not _is_inside(target, dest):
            # Hartes Sicherheitsnetz: nichts außerhalb von dest.
            continue
        by_slot.setdefault(entry.slot, []).append(entry)
        known.add(_casefold(target))
        known.add(_casefold(_part_of(target)))
        # Das Kandidatenverzeichnis kommt aus dest und dem Slug, **nicht**
        # aus relative_path: sonst macht ein Eintrag in <slug>/extras/
        # diesen Unterordner zum Suchbereich.
        if target.parent == dest / _slug_dir(entry.product_id, slugs):
            slot_dirs.setdefault(entry.slot, target.parent)
        else:
            verschachtelt.add(entry.slot)

    # Produktverzeichnis → Slots, die **direkt** dort liegen.
    dirs: dict[Path, dict[SlotKey, tuple[str, ...]]] = {}
    for slot, entries in by_slot.items():
        # Liegt auch nur eine Datei des Slots in einem Unterverzeichnis,
        # räumt dieser Slot gar nichts auf. Gewollt: in <slug>/extras/ liegt
        # bei einem gewachsenen Bestand handverlesenes Material, das GOG
        # teils nicht mehr anbietet, und die Namensheuristik kann es nicht
        # von einer Altversion unterscheiden (handbuch_alt.pdf neben
        # handbuch.pdf). Extras sind klein, das Platzproblem sind die
        # Installer - lieber räumen wir dort nichts auf, als einmal das
        # Falsche zu löschen. Keine vergessene Ecke, sondern die Abwägung.
        if slot in verschachtelt or slot not in slot_dirs:
            continue
        # Ein Slot, um den sich die Konfiguration nicht kümmert, räumt auch
        # nicht auf. Seine Dateien bleiben trotzdem in ``known`` und sind
        # damit vor der Zuordnung zu einem anderen Slot geschützt.
        if not _verwaltet_die_konfiguration(entries, config):
            continue
        names = tuple(entry.filename for entry in entries if entry.filename)
        if not names:
            continue
        dirs.setdefault(slot_dirs[slot], {})[slot] = names

    per_slot: dict[SlotKey, list[tuple[Path, int, str]]] = {}
    part_leftovers: dict[SlotKey, list[tuple[Path, int]]] = {}
    old_leftovers: dict[SlotKey, dict[str, list[tuple[Path, int, int]]]] = {}
    for path, size in disk.items():
        if _casefold(path) in known:
            continue
        slot_names = dirs.get(path.parent)
        if not slot_names:
            continue  # Fremdverzeichnis oder dest-Wurzel — nie anfassen.

        beiseite = _split_old(path.name)
        if beiseite is not None:
            basis, generation = beiseite
            slot = _slot_of_basis(basis, slot_names)
            if slot is None:
                continue
            if not _slot_is_replaceable(by_slot[slot], dest, slugs, disk_norm):
                continue
            gruppen = old_leftovers.setdefault(slot, {})
            gruppen.setdefault(basis.casefold(), []).append((path, size, generation))
            continue

        slot = _attribute_slot(path.name, slot_names)
        if slot is None:
            continue
        if not _slot_is_replaceable(by_slot[slot], dest, slugs, disk_norm):
            continue
        if path.name.endswith(PART_SUFFIX):
            part_leftovers.setdefault(slot, []).append((path, size))
        else:
            per_slot.setdefault(slot, []).append((path, size, path.name))

    keep_old = max(config.keep_versions, 1) - 1

    for slot in set(per_slot) | set(part_leftovers) | set(old_leftovers):
        entries = by_slot[slot]
        new_version = next((e.version for e in entries if e.version is not None), None)
        replaced_by = tuple(sorted(entry.file_id for entry in entries))
        ersatz = f"version {new_version}" if new_version else "the current version"

        for path, size, old_version in _keep_newest_generations(
            per_slot.get(slot, ()), keep_old
        ):
            plan.prunes.append(
                PruneItem(
                    path=path,
                    slot=slot,
                    reason=(
                        f"old version {old_version or '(unknown)'} - replaced by "
                        f"{ersatz}; slot {slot.as_str()} is complete and verified"
                    ),
                    size=size,
                    old_version=old_version,
                    new_version=new_version,
                    replaced_by=replaced_by,
                )
            )

        for path, size in part_leftovers.get(slot, ()):
            parsed = _parse_version(_strip_part(path.name))
            plan.prunes.append(
                PruneItem(
                    path=path,
                    slot=slot,
                    reason=(
                        f"incomplete leftover (.part) of a version that is no longer "
                        f"offered - replaced by {ersatz}"
                    ),
                    size=size,
                    old_version=parsed[0] if parsed else None,
                    new_version=new_version,
                    replaced_by=replaced_by,
                )
            )

        # Mit --keep-old bleiben beiseitegelegte Fassungen als
        # Rueckfallebene liegen; die uebrigen Spuren sind davon unberuehrt.
        for basis, gruppe in (
            () if config.keep_old else sorted(old_leftovers.get(slot, {}).items())
        ):
            parsed = _parse_version(basis)
            for path, size, generation in _keep_newest_old_generations(gruppe, keep_old):
                plan.prunes.append(
                    PruneItem(
                        path=path,
                        slot=slot,
                        reason=(
                            f"set-aside previous version (generation "
                            f"{generation}) of {basis} - replaced by {ersatz}; "
                            f"slot {slot.as_str()} is complete and verified"
                        ),
                        size=size,
                        old_version=parsed[0] if parsed else None,
                        new_version=new_version,
                        replaced_by=replaced_by,
                    )
                )

    plan.prunes.sort(key=lambda item: (item.slot.as_str(), str(item.path)))
    return plan
