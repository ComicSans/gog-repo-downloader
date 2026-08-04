"""Ausführung des Prune-Plans — die zweite von zwei Sicherheitsstufen.

``sync/`` entscheidet, *was* gelöscht werden darf; dieses Modul prüft jede
einzelne Entscheidung vor dem Zugriff auf die Platte noch einmal und lehnt
ab, was die Vorbedingungen nicht erfüllt (KONZEPT.md §5.5, §8). Eine
Ablehnung ist kein Fehler, sondern ein ``PruneResult(removed=False,
reason=...)``: der Lauf geht weiter, nur diese eine Datei bleibt liegen.

Gelöscht wird ausschließlich, was das Tool selbst angelegt hat und wofür
nachweislich ein vollständiger, verifizierter Ersatz auf der Platte liegt.
Fremdbestand wird gemeldet, nie angefasst.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable, Sequence
from datetime import date
from pathlib import Path

from gogdl.constants import TRASH_DIRNAME
from gogdl.model.protocols import Store
from gogdl.model.types import ManifestEntry, PruneItem, PruneMode, PruneResult

__all__ = ["PruneExecutor", "freed_bytes"]

PART_SUFFIX = ".part"
"""Endung unfertiger Downloads. ``.part``-Reste dürfen auch ohne Ersatz weg."""


def freed_bytes(results: Iterable[PruneResult]) -> int:
    """Summe der Bytes, die die entfernten Einträge belegt haben.

    Grundlage ist ``PruneItem.size`` aus dem Plan, weil ``PruneResult``
    keine eigene Größenangabe trägt. Bei ``dry_run=True`` ist das Ergebnis
    das Volumen, das ein echter Lauf freigeben *würde*; bei
    ``PruneMode.TRASH`` wird der Platz erst frei, wenn der Papierkorb
    geleert wird.
    """
    return sum(result.item.size for result in results if result.removed)


class PruneExecutor:
    """Implementiert ``model.protocols.Pruner``.

    Parameter:
        dest: Wurzel des Archivs. Nichts außerhalb davon wird angefasst.
        store: Manifest — liefert den Zustand der Ersatzdateien.
        mode: ``DELETE`` (unlink) oder ``TRASH`` (verschieben nach
            ``<dest>/.trash/<datum>/``).
        today: Injizierbare Datumsquelle für den Papierkorb-Ordner, damit
            Tests deterministisch bleiben.
    """

    def __init__(
        self,
        dest: Path,
        store: Store,
        mode: PruneMode | None = None,
        *,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._dest = Path(dest).resolve()
        self._dest_given = Path(dest).absolute()
        self._store = store
        self._mode = mode or PruneMode.DELETE
        self._today = today

    # -- öffentliche Schnittstelle ------------------------------------

    def execute(
        self, items: Sequence[PruneItem], *, dry_run: bool = False
    ) -> list[PruneResult]:
        """Prüft und entfernt jeden Eintrag; Reihenfolge bleibt erhalten.

        Ein abgelehnter oder fehlgeschlagener Eintrag beendet den Lauf
        nicht — er wird als ``removed=False`` mit Begründung gemeldet.

        Bei ``dry_run=True`` laufen alle Prüfungen unverändert, es wird
        aber nichts angefasst. ``removed=True`` heißt dann „würde
        entfernt", nicht „wurde entfernt".
        """
        results: list[PruneResult] = []
        for item in items:
            results.append(self._execute_one(item, dry_run=dry_run))
        return results

    # -- ein Eintrag ---------------------------------------------------

    def _execute_one(self, item: PruneItem, *, dry_run: bool) -> PruneResult:
        path, refusal = self._safe_path(item)
        if refusal is not None:
            return PruneResult(item=item, removed=False, reason=refusal)
        assert path is not None

        if not path.exists():
            return PruneResult(item=item, removed=False, reason="nicht vorhanden")
        if not path.is_file():
            return PruneResult(item=item, removed=False, reason="keine reguläre Datei")

        refusal = self._replacement_refusal(item, path)
        if refusal is not None:
            return PruneResult(item=item, removed=False, reason=refusal)

        if dry_run:
            return PruneResult(item=item, removed=True, reason="würde entfernt (dry-run)")

        try:
            detail = self._remove(path)
        except OSError as exc:
            return PruneResult(item=item, removed=False, reason=f"Fehler beim Entfernen: {exc}")

        self._prune_empty_dirs(path.parent)
        return PruneResult(item=item, removed=True, reason=detail)

    # -- Prüfung 1 und 2: Pfad -----------------------------------------

    def _safe_path(self, item: PruneItem) -> tuple[Path | None, str | None]:
        """Pfad-Whitelist und Symlink-Sperre.

        Rückgabe ist entweder der geprüfte absolute Pfad oder eine
        Begründung. Relative Pfade werden gegen ``dest`` aufgelöst.
        """
        raw = Path(item.path)
        if ".." in raw.parts:
            return None, "unsicherer Pfad: '..'-Anteil"

        candidate = raw if raw.is_absolute() else self._dest / raw
        candidate = Path(os.path.normpath(candidate))

        # ``dest`` kann selbst über einen Symlink benannt worden sein
        # (``--dest /tmp/gog`` bei ``/tmp -> /private/tmp``). Plan-Pfade
        # tragen dann die ungelöste Schreibweise: auf die aufgelöste
        # Wurzel umbasieren, statt alles fälschlich abzulehnen.
        if not candidate.is_relative_to(self._dest) and candidate.is_relative_to(
            self._dest_given
        ):
            candidate = self._dest / candidate.relative_to(self._dest_given)

        if candidate == self._dest or not candidate.is_relative_to(self._dest):
            return None, f"Pfad liegt außerhalb von {self._dest}"

        trash_root = self._dest / TRASH_DIRNAME
        if candidate == trash_root or candidate.is_relative_to(trash_root):
            return None, "Pfad liegt im Papierkorb"

        # Kein Symlink auf dem gesamten Weg von dest bis zur Datei: sonst
        # zeigt ein Verzeichnis im Zielordner auf beliebige Fremddaten.
        current = self._dest
        for part in candidate.relative_to(self._dest).parts:
            current = current / part
            if current.is_symlink():
                if current == candidate:
                    return None, "Zieldatei ist ein Symlink"
                return None, f"Symlink im Pfad: {current}"

        # Gürtel und Hosenträger: nach dem Auflösen muss der Pfad immer
        # noch unter dest liegen.
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self._dest):
            return None, f"Pfad zeigt aufgelöst außerhalb von {self._dest}"

        return candidate, None

    # -- Prüfung 3 und 4: Ersatz ---------------------------------------

    def _replacement_refusal(self, item: PruneItem, path: Path) -> str | None:
        """Ersatz muss benannt, vollständig und verifiziert sein.

        Ohne benannten Ersatz wird nur ein ``.part``-Rest freigegeben —
        eine unfertige Datei ist per Definition kein Bestand, den es zu
        schützen gäbe.
        """
        if not item.replaced_by:
            if path.name.endswith(PART_SUFFIX):
                return None
            return "kein Ersatz benannt"

        entries = {entry.file_id: entry for entry in self._store.entries_for_slot(item.slot)}

        replacements: list[ManifestEntry] = []
        for file_id in item.replaced_by:
            entry = entries.get(file_id)
            if entry is None:
                return f"Ersatz {file_id} nicht im Manifest"
            if not entry.is_verified_complete:
                return f"Ersatz {entry.filename} nicht verifiziert vollständig"
            replacements.append(entry)

        # Schutz gegen einen fehlerhaften Plan, der die neue Datei löschen
        # würde: der Zielpfad eines Ersatzes darf nie das Löschziel sein.
        for entry in replacements:
            if self._entry_path(entry) == path:
                return "Datei ist selbst der benannte Ersatz"

        return self._missing_part_refusal(replacements)

    def _missing_part_refusal(self, replacements: Sequence[ManifestEntry]) -> str | None:
        """Mehrteilige Auslieferungen nur als Ganzes als Ersatz zählen.

        Prune-Einheit ist der Slot, nicht die Einzeldatei (KONZEPT.md
        §5.5). Nennt der Plan nur einen Teil einer mehrteiligen neuen
        Version, ist über die übrigen Teile nichts bekannt — dann bleibt
        die Altversion liegen.
        """
        groups: dict[tuple[str | None, int], set[int]] = {}
        for entry in replacements:
            if entry.total_parts <= 1:
                continue
            groups.setdefault((entry.version, entry.total_parts), set()).add(entry.part_index)

        for (version, total_parts), seen in groups.items():
            if len(seen) < total_parts:
                label = version or "ohne Version"
                return (
                    f"Ersatz unvollständig: nur {len(seen)} von {total_parts} Teilen "
                    f"({label}) als Ersatz benannt"
                )
        return None

    def _entry_path(self, entry: ManifestEntry) -> Path:
        """Zielpfad eines Manifest-Eintrags unterhalb von ``dest``."""
        relative = entry.relative_path or entry.filename
        return Path(os.path.normpath(self._dest / relative))

    # -- Ausführung ----------------------------------------------------

    def _remove(self, path: Path) -> str:
        if self._mode is PruneMode.TRASH:
            target = self._trash_target(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(path, target)
            except OSError:
                # Dateisystemgrenze innerhalb von dest: dann eben kopieren.
                shutil.move(str(path), str(target))
            return f"in Papierkorb verschoben: {target.relative_to(self._dest)}"

        path.unlink()
        return "entfernt"

    def _trash_target(self, path: Path) -> Path:
        """``<dest>/.trash/<YYYY-MM-DD>/<relativer Pfad>``, kollisionsfrei."""
        root = self._dest / TRASH_DIRNAME / self._today().isoformat()
        target = root / path.relative_to(self._dest)
        if not target.exists():
            return target

        stem, suffix = target.stem, target.suffix
        counter = 1
        while True:
            candidate = target.with_name(f"{stem}-{counter}{suffix}")
            if not candidate.exists():
                return candidate
            counter += 1

    def _prune_empty_dirs(self, start: Path) -> None:
        """Leer gewordene Verzeichnisse unterhalb von ``dest`` entfernen.

        ``dest`` selbst und der Papierkorb bleiben immer bestehen; ein
        Verzeichnis wird nur entfernt, wenn es wirklich leer ist.
        """
        trash_root = self._dest / TRASH_DIRNAME
        current = start
        while current != self._dest and current.is_relative_to(self._dest):
            if current == trash_root or current.is_relative_to(trash_root):
                return
            try:
                if any(current.iterdir()):
                    return
                current.rmdir()
            except OSError:
                return
            current = current.parent
