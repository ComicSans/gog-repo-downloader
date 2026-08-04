"""Zuordnung vorhandener Dateien zu Manifest-Einträgen.

Zweigeteilt wie der Rest des Werkzeugs: :func:`match_existing` rechnet rein
(Manifest-Einträge plus beobachteter Plattenzustand rein, Plan raus, kein
I/O), :func:`apply_import` schreibt als einzige Funktion in den Store und
liest die Platte nur für die optionale MD5-Prüfung.

Der Import löscht nichts, verschiebt nichts, benennt nichts um und öffnet
keine Datei außer lesend zur MD5-Bildung.

Grundhaltung bei Zweifeln: **nicht zuordnen**. Eine falsche Zuordnung kann
später eine Löschung mit autorisieren, und ein Neu-Download dieser Menge
dauert Wochen. Ein nicht zugeordneter Treffer kostet dagegen nur einen
zweiten Blick.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

from gogdl.constants import STATE_DIRNAME, TRASH_DIRNAME
from gogdl.download.verify import verify_entry
from gogdl.model.protocols import Store
from gogdl.model.types import LocalState, ManifestEntry

class Trust(str, Enum):
    """Wie stark der Beleg sein muss, damit ``last_verified_utc`` gesetzt wird.

    Die Stufe entscheidet **nicht**, welche Dateien übernommen werden - das
    tut allein :func:`match_existing`, und unsichere Kandidaten bleiben in
    jeder Stufe draußen. Sie entscheidet nur, ob eine übernommene Datei
    später eine Löschung mit autorisieren darf (§5.5).
    """

    NONE = "none"
    """Übernehmen, aber nichts bezeugen. Autorisiert keine Löschung."""

    SIZE = "size"
    """Größenübereinstimmung genügt als Beleg."""

    MD5 = "md5"
    """Nur eine bestandene MD5-Prüfung zählt."""


_IMPORTABLE_STATES = frozenset({LocalState.MISSING, LocalState.PARTIAL})
"""Zustände, die der Import überschreiben darf.

``COMPLETE`` und ``STALE`` haben bereits einen lokalen Pfad, ``ORPHANED``
hält die Information fest, dass GOG die Datei aus dem Angebot genommen hat
(KONZEPT.md §4.3). Alles davon würde der Import nur zerstören, deshalb
fasst er ausschließlich Einträge ohne lokale Ablage an. Nebeneffekt: ein
zweiter Importlauf ist wirkungslos statt schädlich.
"""


# ---------------------------------------------------------------------------
# Plan- und Ergebnistypen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportMatch:
    """Sicherer Treffer: die Datei gehört eindeutig zu diesem Eintrag."""

    entry: ManifestEntry
    path: Path
    relative_path: str
    """Pfad relativ zu ``dest``, POSIX-Schreibweise - so erwartet ihn der Store."""
    size: int


@dataclass(frozen=True)
class ImportCandidate:
    """Unsicherer Kandidat: wird gemeldet, aber nie übernommen."""

    path: Path
    reason: str
    entry: ManifestEntry | None = None
    """Fehlt, wenn die Datei auf mehrere Einträge passt - dann gibt es keinen."""


@dataclass(frozen=True)
class ImportRejection:
    """Bei der MD5-Prüfung durchgefallen. Eintrag und Datei bleiben unberührt."""

    entry: ManifestEntry
    path: Path
    reason: str


@dataclass(frozen=True)
class ImportPlan:
    """Was der Import übernehmen würde, was er meldet, was er liegen lässt."""

    matches: tuple[ImportMatch, ...] = ()
    unsure: tuple[ImportCandidate, ...] = ()
    unmatched: tuple[Path, ...] = ()

    @property
    def match_bytes(self) -> int:
        return sum(match.size for match in self.matches)


@dataclass(frozen=True)
class ImportSummary:
    """Ergebnis von :func:`apply_import`."""

    imported: tuple[ManifestEntry, ...] = ()
    """Die geschriebenen Einträge - Kopien, die Eingabe bleibt unverändert."""
    rejected: tuple[ImportRejection, ...] = ()
    """Nur bei aktiver MD5-Prüfung: durchgefallen, nichts geschrieben."""
    unverifiable: tuple[Path, ...] = field(default_factory=tuple)
    """``Trust.MD5`` gewünscht, aber das Manifest kennt keine Prüfsumme.

    Diese Dateien werden übernommen, gelten aber als **nicht** geprüft.
    """

    @property
    def verified_count(self) -> int:
        return sum(1 for entry in self.imported if entry.last_verified_utc is not None)

    @property
    def imported_bytes(self) -> int:
        return sum(entry.bytes_done for entry in self.imported)


# ---------------------------------------------------------------------------
# Pfadhilfen (rein algebraisch, ohne Plattenzugriff)
# ---------------------------------------------------------------------------


def _product_dirname(product_id: int, slugs: Mapping[int, str]) -> str:
    """Verzeichnisname eines Produkts unterhalb von ``dest``.

    Gleiche Regel wie in ``sync/``: der Slug, ersatzweise die Produkt-ID.
    Ohne diese Rückfallebene wäre jedes Produkt, das in ``slugs`` fehlt,
    still nicht zuordenbar.
    """
    slug = slugs.get(product_id)
    return slug if slug else str(product_id)


def _relative_parts(path: Path, dest: Path) -> tuple[str, ...] | None:
    """Pfadteile unterhalb von ``dest``; ``None``, wenn außerhalb.

    Zusätzlich fliegen Zustandsverzeichnis und Papierkorb heraus - beides
    gehört dem Werkzeug und ist kein Bestand.
    """
    if path == dest or not path.is_relative_to(dest):
        return None
    parts = path.relative_to(dest).parts
    if not parts or parts[0] in (STATE_DIRNAME, TRASH_DIRNAME):
        return None
    return parts


def _fold(name: str) -> str:
    """Vergleichsform eines Dateinamens.

    GOG ist bei der Groß-/Kleinschreibung uneinheitlich, der Vergleich muss
    das aushalten.
    """
    return name.casefold()


# ---------------------------------------------------------------------------
# Zuordnung
# ---------------------------------------------------------------------------


def match_existing(
    entries: Sequence[ManifestEntry],
    on_disk: Mapping[Path, int],
    dest: Path,
    slugs: Mapping[int, str],
) -> ImportPlan:
    """Ordnet vorhandene Dateien den Manifest-Einträgen zu. Reine Rechnung.

    Die Regeln, strikt in dieser Reihenfolge:

    1. Die Datei liegt in ``<dest>/<slug des Eintrags>/`` oder einem
       Unterverzeichnis davon. Das Layout eines gewachsenen Bestands ist
       nicht flach (``extras/``), die Tiefe spielt deshalb keine Rolle -
       das Produktverzeichnis dagegen schon.
    2. Der Dateiname stimmt **exakt** mit ``entry.filename`` überein,
       Groß-/Kleinschreibung ausgenommen.
    3. Name und Größe zusammen treffen genau einen Eintrag -> sicherer
       Treffer. Der Name allein darf dabei ruhig auf mehrere passen: GOG
       liefert dieselbe Datei je Sprache unter demselben Namen aus, und
       die Sprachfassungen unterscheiden sich fast immer in der Größe.
    4. Name und Größe treffen mehrere Einträge, aber alle tragen dieselbe
       Prüfsumme -> sicherer Treffer auf den Eintrag mit der kleinsten
       ``file_id``. Bei gleicher Prüfsumme ist der Inhalt identisch, die
       Sprache des Slots also ohne Bedeutung für die Datei. Einträge ohne
       Sollgröße zählen dabei mit, weil sie sich nicht ausschließen
       lassen; gewählt wird aber nur unter denen mit passender Größe.
    5. Name und Größe treffen mehrere Einträge, und die Prüfsummen
       unterscheiden sich oder fehlen -> unsicherer Kandidat. Hier ist
       wirklich nichts zu entscheiden.
    6. Name passt, Größe zu keinem der gleichnamigen Einträge -> unsicherer
       Kandidat. Das ist meist eine andere Version oder ein abgebrochener
       Download; beides darf nicht als vollständig gelten.
    7. Kein Namenstreffer -> nicht zuordenbar. Über Präfixe oder
       Ähnlichkeit wird **nicht** geraten.
    8. Verzeichnisse ohne bekannten Slug sind vollständig nicht zuordenbar.

    Eindeutig heißt in beide Richtungen eindeutig: passt eine Datei auf
    zwei Einträge oder ein Eintrag auf zwei Dateien (derselbe Name in der
    Produktwurzel und in ``extras/``), wird keiner davon übernommen.
    """
    importable, occupied = _partition_entries(entries, dest, slugs)

    # Schritt 1 und 2: Kandidaten je (Produktverzeichnis, Dateiname).
    by_key: dict[tuple[str, str], list[ManifestEntry]] = {}
    for entry in importable:
        dirname = _product_dirname(entry.product_id, slugs)
        by_key.setdefault((dirname, _fold(entry.filename)), []).append(entry)

    # Bekannt ist ein Verzeichnis, sobald das Manifest das Produkt kennt -
    # auch wenn dort gerade nichts zu importieren ist. Sonst würde eine
    # bereits belegte Datei als Fremdbestand gemeldet.
    known_dirs = {
        _product_dirname(item.product_id, slugs) for item in entries
    }

    unsure: list[ImportCandidate] = []
    unmatched: list[Path] = []
    # Vorläufige Treffer, noch ohne Prüfung auf Eindeutigkeit je Eintrag.
    staged: list[tuple[ManifestEntry, Path, str, int]] = []

    for path in sorted(on_disk, key=str):
        size = on_disk[path]
        parts = _relative_parts(path, dest)
        if parts is None:
            continue  # Außerhalb von dest oder werkzeugintern - geht uns nichts an.
        if path in occupied:
            continue  # Gehört bereits einem Eintrag mit lokaler Ablage.
        if len(parts) < 2 or parts[0] not in known_dirs:
            # Regel 8: unbekanntes Verzeichnis - oder eine Datei direkt in
            # der Wurzel, die zu keinem Produkt gehören kann.
            unmatched.append(path)
            continue

        candidates = by_key.get((parts[0], _fold(path.name)))
        if not candidates:
            unmatched.append(path)  # Regel 7: kein Namenstreffer, kein Raten.
            continue

        chosen = _select_entry(candidates, path, size)
        if isinstance(chosen, ImportCandidate):
            unsure.append(chosen)
            continue

        staged.append((chosen, path, "/".join(parts), size))

    matches_out, ambiguous = _resolve_entry_conflicts(staged)
    unsure.extend(ambiguous)

    return ImportPlan(
        matches=tuple(matches_out),
        unsure=tuple(sorted(unsure, key=lambda c: str(c.path))),
        unmatched=tuple(unmatched),
    )


def _partition_entries(
    entries: Sequence[ManifestEntry], dest: Path, slugs: Mapping[int, str]
) -> tuple[list[ManifestEntry], set[Path]]:
    """Trennt importierbare Einträge von denen mit bereits belegtem Pfad.

    Die belegten Pfade kommen als Menge zurück, damit die zugehörigen
    Dateien nicht fälschlich als Fremdbestand gemeldet werden.
    """
    importable: list[ManifestEntry] = []
    occupied: set[Path] = set()
    for entry in entries:
        if entry.relative_path:
            occupied.add(dest / Path(entry.relative_path))
            continue
        if entry.state not in _IMPORTABLE_STATES:
            occupied.add(dest / _product_dirname(entry.product_id, slugs) / entry.filename)
            continue
        if not entry.filename:
            continue
        importable.append(entry)
    return importable, occupied


def _select_entry(
    candidates: Sequence[ManifestEntry], path: Path, size: int
) -> ManifestEntry | ImportCandidate:
    """Wählt unter gleichnamigen Einträgen den einen, der zur Datei gehört.

    Zweistufig: der Name grenzt ein, die Größe entscheidet. Der Grund ist
    die Auslieferung von GOG selbst - ein Produkt mit zehn Sprachfassungen
    hat zehn Manifest-Einträge desselben Dateinamens, je einen pro Slot.
    Der Name allein taugt dort nicht als Schlüssel, die Bytegröße meistens
    schon.

    Bleibt es nach der Größe mehrdeutig, entscheidet
    :func:`_checksum_objection` als dritte Stufe.

    Zurück kommt entweder der eine passende Eintrag oder ein fertig
    begründeter :class:`ImportCandidate`. Die Begründungen sind bewusst
    unterscheidbar: "keine passende Größe" ist ein Fund (meist eine
    veraltete Fassung), die Begründungen der dritten Stufe benennen
    dagegen, warum die Gleichheit der Kandidaten nicht zu beweisen war.
    """
    if len(candidates) == 1:
        # Der Normalfall. Bewusst getrennt gehalten: hier kann die
        # Begründung den Eintrag benennen, unten kann sie das nicht.
        entry = candidates[0]
        if entry.size is None:
            return ImportCandidate(
                path=path,
                reason="no expected size in the manifest - not checkable",
                entry=entry,
            )
        if entry.size != size:
            return ImportCandidate(
                path=path,
                reason=f"size {size} instead of {entry.size}",
                entry=entry,
            )
        return entry

    by_size = [item for item in candidates if item.size == size]
    # Ein Eintrag ohne Sollgröße lässt sich nicht ausschließen: er wäre
    # immer ein zweiter möglicher Empfänger.
    unsized = [item for item in candidates if item.size is None]

    if not by_size:
        if unsized:
            return _unsized_objection(candidates, unsized, path)
        expected = " or ".join(
            str(item)
            for item in sorted(
                {item.size for item in candidates if item.size is not None}
            )
        )
        return ImportCandidate(path=path, reason=f"size {size} instead of {expected}")

    contenders = [*by_size, *unsized]
    if len(contenders) == 1:
        return by_size[0]

    # Dritte Stufe: die Prüfsumme. Sie entscheidet nicht, welcher Slot
    # gemeint ist, sondern beweist, dass die Frage gegenstandslos ist.
    objection = _checksum_objection(contenders, path)
    if objection is None:
        # Nachweislich derselbe Inhalt. Gewählt wird trotzdem nur unter den
        # Einträgen mit passender Sollgröße, damit das Manifest die Datei
        # weiterhin richtig beschreibt.
        return min(by_size, key=lambda item: (item.file_id, item.slot.as_str()))
    if unsized:
        # Der schwächere Befund hat Vorrang: hier fehlt schon die Sollgröße.
        return _unsized_objection(candidates, unsized, path)
    return objection


def _unsized_objection(
    candidates: Sequence[ManifestEntry],
    unsized: Sequence[ManifestEntry],
    path: Path,
) -> ImportCandidate:
    """Begründung, wenn ein gleichnamiger Eintrag keine Sollgröße nennt."""
    return ImportCandidate(
        path=path,
        reason=(
            f"{len(candidates)} entries share this name, {len(unsized)} "
            "of them without expected size - not checkable"
        ),
    )


def _checksum_objection(
    candidates: Sequence[ManifestEntry], path: Path
) -> ImportCandidate | None:
    """Prüft, ob der Gleichstand gleichnamiger Einträge belanglos ist.

    ``None`` heißt: nachweislich derselbe Inhalt, die Wahl ist frei.
    Andernfalls kommt der fertig begründete Einwand zurück.

    Warum das trotz der Grundhaltung "bei Zweifeln nicht zuordnen" sicher
    ist: Tragen alle Kandidaten dieselbe Prüfsumme, ist ihr Inhalt
    identisch. GOG listet denselben mehrsprachigen Installer dann nur unter
    mehreren Sprach-Slots. Für die Datei auf der Platte ist die Sprache des
    Slots damit bedeutungslos - es gibt keine falsche Wahl, weil es nichts
    zu wählen gibt. Der schlimmste Fall ist, dass ein Eintrag unter einem
    Sprach-Slot steht, der eine byteidentische Datei beschreibt. Eine
    Messung an einer echten Sammlung fand 99 solcher Gruppen, davon 93
    nachweislich identisch.

    Bewiesen ist die Gleichheit nur, wenn **jeder** Kandidat eine
    Prüfsumme trägt. Fehlt eine, könnte der Kandidat ohne Prüfsumme sehr
    wohl ein anderer Inhalt sein; unterscheiden sie sich, ist er es
    nachweislich (gemessen einmal, ``setup_theme_hospital_v3_(28027).exe``).
    Beides bleibt abgelehnt.

    Solange das Manifest keine Prüfsummen führt - vor einem
    ``update --strict`` also -, greift diese Stufe nie und es bleibt bei
    der Ablehnung. Das ist der vorsichtige Zustand, nicht der neue.
    """
    unchecked = [item for item in candidates if not item.md5]
    if unchecked:
        return ImportCandidate(
            path=path,
            reason=(
                f"name and size match {len(candidates)} entries, checksum "
                f"missing for {len(unchecked)} of them"
            ),
        )

    digests = {item.md5.casefold() for item in candidates if item.md5}
    if len(digests) > 1:
        return ImportCandidate(
            path=path,
            reason=(
                f"name and size match {len(candidates)} entries with "
                "different checksums"
            ),
        )

    return None


def _resolve_entry_conflicts(
    staged: Sequence[tuple[ManifestEntry, Path, str, int]],
) -> tuple[list[ImportMatch], list[ImportCandidate]]:
    """Ein Eintrag darf höchstens eine Datei bekommen.

    Zwei gleichnamige Dateien gleicher Größe in verschiedenen
    Unterverzeichnissen desselben Produkts sind sonst ein stiller
    Münzwurf. Beide werden gemeldet, keine übernommen.
    """
    # Geschlüsselt wird auf den Primärschlüssel des Stores, nicht auf die
    # Objektidentität: zwei verschiedene Objekte mit demselben Schlüssel
    # würden sonst beide geschrieben und das zweite überschriebe das erste.
    per_entry: dict[tuple[str, str], list[tuple[ManifestEntry, Path, str, int]]] = {}
    for item in staged:
        entry = item[0]
        per_entry.setdefault((entry.slot.as_str(), entry.file_id), []).append(item)

    matches: list[ImportMatch] = []
    ambiguous: list[ImportCandidate] = []
    for group in per_entry.values():
        if len(group) > 1:
            for _entry, path, _rel, _size in group:
                ambiguous.append(
                    ImportCandidate(
                        path=path,
                        reason=(
                            f"{len(group)} files match the same entry "
                            "- not unique"
                        ),
                    )
                )
            continue
        entry, path, rel, size = group[0]
        matches.append(
            ImportMatch(entry=entry, path=path, relative_path=rel, size=size)
        )

    matches.sort(key=lambda m: str(m.path))
    return matches, ambiguous


# ---------------------------------------------------------------------------
# Übernahme in den Store
# ---------------------------------------------------------------------------


def apply_import(
    plan: ImportPlan,
    store: Store,
    now_utc: str,
    *,
    trust: Trust = Trust.NONE,
    on_progress: Callable[[int, int], None] | None = None,
) -> ImportSummary:
    """Schreibt die sicheren Treffer des Plans ins Manifest.

    Übernommen wird in jeder Vertrauensstufe genau dasselbe: die sicheren
    Treffer aus :attr:`ImportPlan.matches`. ``trust`` senkt oder hebt nur
    die Beweislast für ``last_verified_utc`` und damit dafür, ob eine
    übernommene Datei später eine Löschung mit autorisieren darf. Unsichere
    Kandidaten bleiben in **jeder** Stufe ausgeschlossen.

    Außer bei :attr:`Trust.MD5` wird die Platte überhaupt nicht angefasst:
    Größe und Pfad stehen bereits im Plan. Mit :attr:`Trust.MD5` wird jede
    Datei einmal vollständig gelesen
    (:func:`gogdl.download.verify.verify_entry` mit ``deep=True``); nur wer
    besteht, wird übernommen.

    Die Eingabe-Einträge bleiben unverändert - geschrieben werden Kopien.

    **Geschrieben wird erst am Ende, in einem Zug.** Die Schleife rechnet
    und prüft nur; der Store bekommt die fertige Liste danach über
    ``update_entries``. Zwei Gründe, beide wiegen schwerer als die
    Reihenfolge:

    * Ein Schreibaufruf je Eintrag ist eine Transaktion je Eintrag. Auf
      einem Ablageort ohne WAL - exFAT, Netzlaufwerk - kostet das je eine
      eigene Synchronisierung; ein Lauf über 2412 Einträge brauchte so rund
      neun Minuten.
    * Die Transaktion darf nicht offen stehen, während gerechnet wird. Mit
      :attr:`Trust.MD5` liest die Schleife jede Datei vollständig; über
      einer großen Sammlung sind das Stunden, und solange läge die
      Datenbank für jeden anderen Zugriff gesperrt.

    Der Preis, bewusst gezahlt: ein abgebrochener Lauf schreibt nun gar
    nichts statt des bereits Geprüften. Der Import ist wiederholbar und
    ändert nichts auf der Platte, ein zweiter Anlauf kostet also nur Zeit -
    ein halb übernommener Stand wäre teurer.

    ``on_progress`` wird, wenn gesetzt, nach jedem **bearbeiteten** Treffer
    mit ``(bearbeitet, gesamt)`` gerufen - auch für die, die an der
    MD5-Prüfung scheitern, denn die Arbeit ist getan. Diese Funktion gibt
    selbst nichts aus; wie der Fortschritt aussieht, entscheidet allein der
    Aufrufer.
    """
    imported: list[ManifestEntry] = []
    rejected: list[ImportRejection] = []
    unverifiable: list[Path] = []
    total = len(plan.matches)
    processed = 0

    for match in plan.matches:
        entry = match.entry
        verified_at: str | None = None

        if trust is Trust.MD5:
            if not entry.md5:
                # verify_entry(deep=True) gibt ohne md5 im Manifest allein
                # aufgrund der Größe True zurück. Wer ausdrücklich MD5
                # verlangt, soll das aber nicht als Prüfsumme untergeschoben
                # bekommen: übernehmen, aber ungeprüft. Wer sich mit der
                # Größe begnügt, wählt Trust.SIZE bewusst.
                unverifiable.append(match.path)
            else:
                ok, detail = verify_entry(entry, match.path, deep=True)
                if not ok:
                    # Nichts schreiben, nichts anfassen. Ein Eintrag, dessen
                    # Prüfsumme nicht stimmt, ist keine übernehmbare Datei.
                    rejected.append(
                        ImportRejection(entry=entry, path=match.path, reason=detail)
                    )
                    processed += 1
                    if on_progress is not None:
                        on_progress(processed, total)
                    continue
                verified_at = now_utc
        elif trust is Trust.SIZE:
            # Bewusst ein schwächerer Beleg als bei einem echten Download:
            # bezeugt wird hier nur, dass Name und Größe zum Manifest passen.
            # Vertretbar ist das, weil der schlimmste Fall ein erneuter
            # Download ist und kein Datenverlust - die Sammlung lässt sich
            # notfalls komplett neu laden. Dafür wird Aufräumen überhaupt
            # erst praktikabel: eine MD5-Prüfung über 3,5 TB läuft Stunden
            # bis Tage. Wer diesen Handel nicht will, bleibt bei Trust.NONE
            # oder zahlt den Preis mit Trust.MD5.
            verified_at = now_utc

        # Trust.NONE: ``last_verified_utc`` bleibt None. Das ist die
        # vorsichtigste und deshalb voreingestellte Stufe.
        # ``ManifestEntry.is_verified_complete`` autorisiert später
        # Löschungen (KONZEPT.md §5.5), und eine bloße Größenübereinstimmung
        # aus einem Fremdbestand ist dafür an sich zu schwach - die Datei
        # kann von einem anderen Werkzeug stammen, beschädigt oder eine
        # gleich große andere Auslieferung sein. Ein Import mit Trust.NONE
        # darf deshalb NIE eine spätere Löschung rechtfertigen.
        updated = replace(
            entry,
            state=LocalState.COMPLETE,
            relative_path=match.relative_path,
            bytes_done=match.size,
            last_verified_utc=verified_at,
        )
        imported.append(updated)
        processed += 1
        if on_progress is not None:
            on_progress(processed, total)

    _write_entries(store, imported)

    return ImportSummary(
        imported=tuple(imported),
        rejected=tuple(rejected),
        unverifiable=tuple(unverifiable),
    )


def _write_entries(store: Store, entries: Sequence[ManifestEntry]) -> None:
    """Schreibt gebündelt, wo der Store es kann, sonst einzeln.

    ``update_entries`` steht nicht im Protocol ``Store`` - dort gehörte es
    hin, aber ``model/protocols.py`` ist der Vertrag zwischen den Paketen
    und wird von einem Fachmodul nicht geändert. Deshalb wird die Bündelung
    hier erfragt statt vorausgesetzt: ein Store, der sie anbietet,
    bekommt einen Aufruf, jeder andere die bisherige Schleife. Das
    Ergebnis ist in beiden Fällen dasselbe, nur die Anzahl der
    Transaktionen unterscheidet sich.
    """
    if not entries:
        return
    bulk = getattr(store, "update_entries", None)
    if callable(bulk):
        bulk(entries)
        return
    for entry in entries:
        store.update_entry(entry)
