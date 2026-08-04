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

MIN_PREFIX_MATCH = 4
"""Mindestlänge des gemeinsamen Namenspräfixes für die Slot-Zuordnung.

Kürzere Übereinstimmungen (``setup_``-Rauschen gibt es nicht, aber
zweistellige Zufallstreffer schon) gelten als *keine* Zuordnung: dann
bleibt die Datei liegen und wird nicht gelöscht.
"""

_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
"""Versionsähnliches Token im Dateinamen, z. B. ``2.1.0`` in
``setup_spiel_2.1.0_(1).bin``. Mehrteiligkeit (``_(1)``) matcht bewusst
nicht, weil mindestens ein Punkt gefordert ist."""


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


def _common_prefix_len(a: str, b: str) -> int:
    a, b = a.lower(), b.lower()
    n = 0
    for left, right in zip(a, b):
        if left != right:
            break
        n += 1
    return n


def _parse_version(name: str) -> tuple[str, tuple[int, ...]] | None:
    """Versionstoken aus einem Dateinamen ziehen.

    Genommen wird das **letzte** versionsähnliche Token: GOG setzt die
    Version ans Ende, direkt vor einen etwaigen Teilindex
    (``setup_spiel_2.1.0_(1).bin``), während im Spieltitel selbst
    Punkt-Ziffern-Folgen vorkommen können.
    """
    matches = _VERSION_RE.findall(name)
    if not matches:
        return None
    token = matches[-1]
    return token, tuple(int(part) for part in token.split("."))


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
                        f"{entry.filename}: von GOG nicht mehr angeboten "
                        f"(Slot {entry.slot.as_str()}) — bleibt liegen"
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

    for remote_file in planbar:
        entry = by_key.get((remote_file.slot, remote_file.file_id))
        if not _passes_filters(remote_file, config, dlc_of, gewaehlt_os, gewaehlt_lang):
            continue

        target = _remote_target(remote_file, entry, dest, slugs)
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
        if path not in known:
            plan.reports.append(
                Report(
                    path=path,
                    kind="foreign",
                    detail="nicht vom Tool angelegt — bleibt unangetastet",
                )
            )

    plan.downloads.sort(key=lambda item: (item.entry.slot.as_str(), item.entry.file_id))
    plan.reports.sort(key=lambda report: (report.kind, str(report.path)))
    return plan


# ---------------------------------------------------------------------------
# §5.5 — Prune-Planung
# ---------------------------------------------------------------------------


def _slot_is_replaceable(entries: Sequence[ManifestEntry]) -> bool:
    """Darf dieser Slot eine Vorgängerversion ersetzen?

    Vorbedingung jeder Löschung: **alle** Dateien des Slots sind
    ``is_verified_complete`` (Zustand ``COMPLETE`` *und*
    ``last_verified_utc`` gesetzt). Große Installer sind mehrteilig; wird
    nach jeder fertigen Einzeldatei aufgeräumt, verschwindet Teil 1 der
    alten Version, während Teil 2 der neuen noch fehlt — und es gibt gar
    keine vollständige Version mehr (§5.5).
    """
    if not entries:
        return False
    if any(entry.state is LocalState.ORPHANED for entry in entries):
        return False
    return all(entry.is_verified_complete for entry in entries)


def _attribute_slot(
    candidate: str, slot_names: Mapping[SlotKey, tuple[str, ...]]
) -> SlotKey | None:
    """Ordne eine unbekannte Datei dem Slot mit der größten Namensnähe zu.

    Gemessen wird die Länge des gemeinsamen Präfixes zum längsten
    passenden aktuellen Dateinamen. Zwei Slots desselben Produkts
    unterscheiden sich im GOG-Namensschema früh (``setup_spiel_de_…``
    gegen ``setup_spiel_en_…``), deshalb trägt das Präfix die Zuordnung.
    Ohne klaren Sieger — zu kurzes Präfix oder Gleichstand zwischen zwei
    Slots — wird **nicht** zugeordnet und damit auch nicht gelöscht.
    """
    stem = _strip_part(candidate)
    best: SlotKey | None = None
    best_score = 0
    tied = False
    for slot, names in slot_names.items():
        score = max((_common_prefix_len(stem, name) for name in names), default=0)
        if score > best_score:
            best, best_score, tied = slot, score, False
        elif score == best_score and score > 0 and slot != best:
            tied = True
    if best is None or best_score < MIN_PREFIX_MATCH or tied:
        return None
    return best


def _keep_newest_generations(
    candidates: Sequence[tuple[Path, int, str]], keep_old: int
) -> list[tuple[Path, int, str | None]]:
    """Wähle aus den Altdateien eines Slots die löschbaren aus.

    ``keep_old`` ist ``keep_versions - 1``, also die Zahl der zusätzlich
    aufzubewahrenden Generationen (``--keep-versions 1`` = nur die
    aktuelle, ``2`` = eine Rückfallebene, §5.5).

    Bei ``keep_old == 0`` fällt alles weg. Sobald eine Generation
    aufbewahrt werden soll, brauchen wir eine Reihenfolge — Dateien ohne
    erkennbares Versionstoken lassen sich nicht einsortieren und könnten
    genau die aufzubewahrende Generation sein. Sie bleiben deshalb
    liegen.
    """
    if keep_old <= 0:
        return [
            (path, size, (parsed[0] if (parsed := _parse_version(name)) else None))
            for path, size, name in candidates
        ]

    versioned: dict[tuple[int, ...], list[tuple[Path, int, str]]] = {}
    for path, size, name in candidates:
        parsed = _parse_version(name)
        if parsed is None:
            continue
        token, key = parsed
        versioned.setdefault(key, []).append((path, size, token))

    doomed: list[tuple[Path, int, str | None]] = []
    for key in sorted(versioned, reverse=True)[keep_old:]:
        doomed.extend((path, size, token) for path, size, token in versioned[key])
    return doomed


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
       Eintrags) gehört. Das Verzeichnis wird aus ``dest`` und dem Slug
       gebildet, nicht aus ``relative_path``; Unterverzeichnisse wie
       ``<slug>/extras/`` bleiben vollständig außen vor - auch für Slots,
       die selbst dort liegen.
    2. Der Kandidat wird über die Länge des gemeinsamen Namenspräfixes
       genau einem Slot zugeordnet (:func:`_attribute_slot`). Ohne klaren
       Sieger bleibt er liegen.
    3. Gelöscht wird nur, wenn dieser Slot vollständig **und** verifiziert
       vorliegt.

    Warum das nur bei nachweislich vollständigem Ersatz greift: Schritt 1
    kann eine Datei nicht von echtem Fremdbestand unterscheiden — der
    Name allein sagt nichts darüber, ob das Tool sie angelegt hat. Erst
    die Kombination „liegt im Produktverzeichnis + trägt das Namensschema
    des Slots + der Slot liegt vollständig verifiziert daneben" macht die
    Annahme *Vorgängerversion* belastbar. Fällt eine dieser Bedingungen
    weg, ist die Datei nach §5.5 Fremdbestand: sie bleibt und wird
    allenfalls gemeldet (durch :func:`plan_downloads`), nie gelöscht.

    ``.part``-Reste zu einer nicht mehr angebotenen Version dürfen
    ebenfalls in den Plan; sie sind nie eine Rückfallebene und deshalb
    von ``keep_versions`` ausgenommen.
    """
    plan = SyncPlan()
    if not config.prune:
        return plan

    dest = config.dest
    disk = _relevant_disk(on_disk, dest)
    if not disk:
        return plan

    by_slot: dict[SlotKey, list[ManifestEntry]] = {}
    known: set[Path] = set()
    slot_dirs: dict[SlotKey, Path] = {}
    verschachtelt: set[SlotKey] = set()
    for entry in local:
        target = _entry_target(entry, dest, slugs)
        if not _is_inside(target, dest):
            # Hartes Sicherheitsnetz: nichts außerhalb von dest.
            continue
        by_slot.setdefault(entry.slot, []).append(entry)
        known.add(target)
        known.add(_part_of(target))
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
        names = tuple(entry.filename for entry in entries if entry.filename)
        if not names:
            continue
        dirs.setdefault(slot_dirs[slot], {})[slot] = names

    per_slot: dict[SlotKey, list[tuple[Path, int, str]]] = {}
    part_leftovers: dict[SlotKey, list[tuple[Path, int]]] = {}
    for path, size in disk.items():
        if path in known:
            continue
        slot_names = dirs.get(path.parent)
        if not slot_names:
            continue  # Fremdverzeichnis oder dest-Wurzel — nie anfassen.
        slot = _attribute_slot(path.name, slot_names)
        if slot is None:
            continue
        if not _slot_is_replaceable(by_slot[slot]):
            continue
        if path.name.endswith(PART_SUFFIX):
            part_leftovers.setdefault(slot, []).append((path, size))
        else:
            per_slot.setdefault(slot, []).append((path, size, path.name))

    keep_old = max(config.keep_versions, 1) - 1

    for slot in set(per_slot) | set(part_leftovers):
        entries = by_slot[slot]
        new_version = next((e.version for e in entries if e.version is not None), None)
        replaced_by = tuple(sorted(entry.file_id for entry in entries))
        ersatz = f"Version {new_version}" if new_version else "die aktuelle Version"

        for path, size, old_version in _keep_newest_generations(
            per_slot.get(slot, ()), keep_old
        ):
            plan.prunes.append(
                PruneItem(
                    path=path,
                    slot=slot,
                    reason=(
                        f"Altversion {old_version or '(unbekannt)'} — ersetzt durch "
                        f"{ersatz}; Slot {slot.as_str()} liegt vollständig verifiziert vor"
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
                        f"unvollständiger Rest (.part) einer nicht mehr angebotenen "
                        f"Version — ersetzt durch {ersatz}"
                    ),
                    size=size,
                    old_version=parsed[0] if parsed else None,
                    new_version=new_version,
                    replaced_by=replaced_by,
                )
            )

    plan.prunes.sort(key=lambda item: (item.slot.as_str(), str(item.path)))
    return plan
