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

from collections.abc import Mapping, Sequence
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
    3. Die Größe stimmt mit ``entry.size`` überein -> sicherer Treffer.
    4. Name passt, Größe weicht ab -> unsicherer Kandidat. Das ist meist
       eine andere Version oder ein abgebrochener Download; beides darf
       nicht als vollständig gelten.
    5. Kein Namenstreffer -> nicht zuordenbar. Über Präfixe oder
       Ähnlichkeit wird **nicht** geraten.
    6. Verzeichnisse ohne bekannten Slug sind vollständig nicht zuordenbar.

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
            # Regel 6: unbekanntes Verzeichnis - oder eine Datei direkt in
            # der Wurzel, die zu keinem Produkt gehören kann.
            unmatched.append(path)
            continue

        matches = by_key.get((parts[0], _fold(path.name)))
        if not matches:
            unmatched.append(path)  # Regel 5: kein Namenstreffer, kein Raten.
            continue
        if len(matches) > 1:
            unsure.append(
                ImportCandidate(
                    path=path,
                    reason=f"Name passt auf {len(matches)} Einträge - nicht eindeutig",
                )
            )
            continue

        entry = matches[0]
        if entry.size is None:
            unsure.append(
                ImportCandidate(
                    path=path,
                    reason="keine Sollgröße im Manifest - nicht prüfbar",
                    entry=entry,
                )
            )
            continue
        if entry.size != size:
            unsure.append(
                ImportCandidate(
                    path=path,
                    reason=f"Größe {size} statt {entry.size}",
                    entry=entry,
                )
            )
            continue

        staged.append((entry, path, "/".join(parts), size))

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
                            f"{len(group)} Dateien passen auf denselben Eintrag "
                            "- nicht eindeutig"
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
    """
    imported: list[ManifestEntry] = []
    rejected: list[ImportRejection] = []
    unverifiable: list[Path] = []

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
        store.update_entry(updated)
        imported.append(updated)

    return ImportSummary(
        imported=tuple(imported),
        rejected=tuple(rejected),
        unverifiable=tuple(unverifiable),
    )
