"""Fortschrittsanzeige für TTY und Nicht-TTY (KONZEPT.md §5.4).

Zwei Implementierungen desselben Protocols ``model.protocols.ProgressReporter``:

``RichReporter``
    Live-Anzeige im Terminal, auf ~4 Hz gedrosselt. Ganz oben steht immer
    die Gesamtzeile (Dateien x/y, Bytes, ETA), darunter je eine Zeile pro
    gerade laufender Datei (Name, Bytes, Rate, ETA), die beim Abschluss
    wieder verschwindet.

``PlainReporter``
    Zeilenweise Meldungen ohne jedes ANSI-Steuerzeichen, für Cron, Pipes
    und Logdateien. Jede Zeile nennt die Datei, denn bei ``--jobs 2`` sind
    mehrere Downloads gleichzeitig unterwegs. Zwischenstände werden stark
    gedrosselt, damit ein nächtlicher Lauf keine Logdatei füllt.

``make_reporter`` wählt anhand von ``stream.isatty()`` automatisch. Ein
manueller Schalter ist nicht nötig, ``force_plain`` überschreibt trotzdem.

Handles
-------
``start_file`` liefert ein undurchsichtiges Handle. Nur damit lassen sich
gleichzeitige Downloads auseinanderhalten: ohne Handle würden bei
``--jobs 2`` beide Läufe ihre Bytes derselben Zeile zurechnen. ``advance``
und ``finish_file`` nehmen das Handle wieder entgegen. Wird keines
übergeben, gilt die zuletzt begonnene, noch offene Datei - das ist nur bei
einem einzelnen Auftrag sicher, bleibt aber laut Protocol erlaubt.

Beide Reporter sind robust gegen Aufrufe in falscher Reihenfolge: ein
``advance()`` ohne vorheriges ``start_file()`` wird verbucht statt zu
scheitern, ein unbekanntes oder veraltetes Handle wird ignoriert statt zu
werfen, und ``close()`` ist idempotent. Eine Anzeige darf einen laufenden
Download unter keinen Umständen abbrechen.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Callable, TextIO

from rich.console import Console
from rich.progress import BarColumn, Progress, ProgressColumn, TaskID, TextColumn
from rich.progress import Task as _RichTask
from rich.table import Column
from rich.text import Text

from .format import human_bytes, human_duration

REFRESH_PER_SECOND = 4.0
"""Live-Refresh der TTY-Anzeige (KONZEPT.md §5.4: „gedrosselt auf ~4 Hz")."""

PLAIN_MIN_INTERVAL = 10.0
"""Mindestabstand zweier Zwischenmeldungen in Sekunden."""

PLAIN_MIN_PERCENT = 10.0
"""Mindestfortschritt zweier Zwischenmeldungen in Prozent."""

_NAME_WIDTH = 38
"""Breite der Namensspalte in der TTY-Anzeige."""

_ROW_OVERALL = "overall"
_ROW_FILE = "file"


def _shorten(name: str, width: int = _NAME_WIDTH) -> str:
    """Langen Dateinamen von links kürzen — hinten steht das Kennzeichnende."""
    text = str(name)
    if len(text) <= width:
        return text
    return "…" + text[-(width - 1) :]


# --------------------------------------------------------------------------
# Handles
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _FileHandle:
    """Undurchsichtiges Handle einer laufenden Datei.

    Der Aufrufer bekommt es von ``start_file`` und reicht es unverändert
    zurück. Der Inhalt ist Implementierungsdetail: gültig ist ein Handle
    nur bei dem Reporter, der es ausgegeben hat, und nur bis zu dessen
    ``finish_file``.
    """

    id: int


@dataclass
class _PlainFile:
    """Zustand einer offenen Datei im ``PlainReporter``.

    Die Drosselung hängt an diesem Objekt, nicht am Reporter: sonst würde
    eine schnelle Datei die Meldungen einer langsamen mit verschlucken.
    """

    name: str
    total: int | None
    index: int
    started_at: float
    done: int = 0
    last_report_at: float = 0.0
    last_report_percent: float = 0.0


@dataclass
class _RichFile:
    """Zustand einer offenen Datei im ``RichReporter``: Name plus eigene Zeile."""

    name: str
    task: TaskID


# --------------------------------------------------------------------------
# Spalten der TTY-Anzeige
# --------------------------------------------------------------------------


class _FilesColumn(ProgressColumn):
    """„12/47 Dateien" — existiert nur in der Gesamtzeile."""

    def render(self, task: _RichTask) -> Text:
        if task.fields.get("row") != _ROW_OVERALL:
            return Text("")
        done = task.fields.get("files_done", 0)
        total = task.fields.get("files_total", 0)
        return Text(f"{done}/{total} files", style="progress.percentage")


class _BytesColumn(ProgressColumn):
    """„3.1/4.0 GB", bei unbekannter Größe nur der geladene Anteil."""

    def render(self, task: _RichTask) -> Text:
        done = human_bytes(task.completed)
        if task.total is None:
            return Text(done, style="progress.download")
        return Text(f"{done}/{human_bytes(task.total)}", style="progress.download")


class _RateColumn(ProgressColumn):
    """„11.4 MB/s" — nur für die laufende Datei."""

    def render(self, task: _RichTask) -> Text:
        if task.fields.get("row") != _ROW_FILE:
            return Text("")
        speed = task.finished_speed or task.speed
        if not speed:
            return Text("--", style="progress.data.speed")
        return Text(f"{human_bytes(speed)}/s", style="progress.data.speed")


class _EtaColumn(ProgressColumn):
    """„ETA 1m20s", solange rich eine Restzeit schätzen kann."""

    def render(self, task: _RichTask) -> Text:
        remaining = task.time_remaining
        if remaining is None:
            return Text("ETA --", style="progress.remaining")
        return Text(f"ETA {human_duration(remaining)}", style="progress.remaining")


# --------------------------------------------------------------------------
# TTY
# --------------------------------------------------------------------------


class RichReporter:
    """Zweistufige Live-Anzeige für Terminals.

    Implementiert ``model.protocols.ProgressReporter``. Die Gesamtzeile
    wird als erste Aufgabe angelegt und steht damit immer oben; jede
    laufende Datei bekommt darunter eine eigene Zeile, die bei
    ``finish_file`` wieder entfernt wird. ``quiet=True`` unterdrückt die
    Balken vollständig und meldet nur noch Fehler und den Abschluss - im
    Zweifel ist eine stille Anzeige besser als eine, die einen Logstrom
    mit Steuerzeichen zerlegt.
    """

    def __init__(
        self,
        *,
        console: Console | None = None,
        stream: TextIO | None = None,
        quiet: bool = False,
        refresh_per_second: float = REFRESH_PER_SECOND,
    ) -> None:
        if console is None:
            console = Console(file=stream if stream is not None else sys.stdout)
        self._console = console
        self._quiet = bool(quiet)
        self._progress = Progress(
            TextColumn(
                "{task.description}",
                table_column=Column(width=_NAME_WIDTH, no_wrap=True, overflow="ellipsis"),
            ),
            BarColumn(bar_width=None),
            _FilesColumn(),
            _BytesColumn(),
            _RateColumn(),
            _EtaColumn(),
            console=console,
            refresh_per_second=refresh_per_second,
        )
        self._overall: TaskID | None = None
        self._files: dict[int, _RichFile] = {}
        self._next_handle = 0
        self._live = False
        self._closed = False

        self._files_total = 0
        self._files_ok = 0
        self._files_failed = 0
        self._bytes_done = 0
        self._clock: Callable[[], float] = time.monotonic
        self._started_at = self._clock()

    # -- Protokoll ---------------------------------------------------------

    def start_overall(self, total_files: int, total_bytes: int) -> None:
        self._files_total = int(total_files)
        self._started_at = self._clock()
        self._ensure_live()
        self._ensure_overall()
        self._update(
            self._overall,
            total=total_bytes,
            completed=0,
            files_done=0,
            files_total=self._files_total,
        )

    def start_file(
        self, name: str, total_bytes: int | None, already_done: int = 0
    ) -> object:
        self._ensure_live()
        # Die Gesamtzeile zuerst anlegen, damit sie in der Reihenfolge der
        # Aufgaben oben bleibt, auch wenn ``start_overall`` fehlt.
        self._ensure_overall()
        # Jede Datei bekommt eine eigene Zeile. Sie wird nie wiederverwendet:
        # ``reset()`` und ``update()`` behandeln ``total=None`` als „nicht
        # angegeben" und würden die vorige Größe stehen lassen - eine Datei
        # unbekannter Größe bekäme dann keinen unbestimmten Balken.
        #
        # ``already_done`` zählt nicht zur Gesamtsumme: die enthält nur noch
        # zu ladende Bytes (SyncPlan.download_bytes rechnet ``resume_from``
        # bereits heraus).
        task = self._progress.add_task(
            _shorten(name),
            total=total_bytes,
            completed=already_done,
            row=_ROW_FILE,
        )
        return self._register(_RichFile(name=str(name), task=task))

    def advance(self, n_bytes: int, handle: object | None = None) -> None:
        if self._closed or not n_bytes or n_bytes < 0:
            return
        # Die Gesamtsumme stimmt auch dann, wenn das Handle nicht mehr
        # auflösbar ist: die Bytes sind tatsächlich geflossen.
        self._bytes_done += n_bytes
        entry = self._resolve(handle)
        if entry is not None:
            self._advance_task(entry.task, n_bytes)
        self._advance_task(self._overall, n_bytes)

    def finish_file(
        self, name: str, ok: bool, detail: str = "", handle: object | None = None
    ) -> None:
        if ok:
            self._files_ok += 1
        else:
            self._files_failed += 1
        self._update(self._overall, files_done=self._files_ok + self._files_failed)

        entry_id = self._resolve_id(handle, name)
        if entry_id is not None:
            entry = self._files.pop(entry_id)
            # Die Zeile verschwindet, statt nur unsichtbar zu werden: bei
            # mehreren gleichzeitigen Dateien sammelt sich sonst Altbestand.
            try:
                self._progress.remove_task(entry.task)
            except Exception:  # pragma: no cover - Anzeige darf nie werfen
                pass

        if ok and self._quiet:
            return
        marker, style = ("OK", "green") if ok else ("FAILED", "bold red")
        line = Text.assemble((marker, style), " ", str(name))
        if detail:
            line.append(f" - {detail}")
        self._print(line)

    def message(self, text: str) -> None:
        if self._quiet:
            return
        self._print(Text(str(text)))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._live:
            self._live = False
            try:
                self._progress.stop()
            except Exception:  # pragma: no cover - Anzeige darf nie werfen
                pass
        self._print(Text(_summary_line(self)))
        self._flush()

    # -- intern ------------------------------------------------------------

    def _register(self, entry: _RichFile) -> _FileHandle:
        self._next_handle += 1
        self._files[self._next_handle] = entry
        return _FileHandle(self._next_handle)

    def _resolve(self, handle: object | None) -> _RichFile | None:
        """Eintrag zum Handle. Ohne Handle die zuletzt begonnene offene Datei."""
        entry_id = self._resolve_id(handle, None)
        return None if entry_id is None else self._files[entry_id]

    def _resolve_id(self, handle: object | None, name: str | None) -> int | None:
        """Schlüssel des gemeinten Eintrags, ``None`` wenn keiner passt.

        Ein unbekanntes oder veraltetes Handle liefert ``None`` statt eines
        falschen Eintrags: lieber nichts anzeigen als die falsche Datei.
        """
        if handle is not None:
            if isinstance(handle, _FileHandle) and handle.id in self._files:
                return handle.id
            return None
        if name is not None:
            for key in reversed(list(self._files)):
                if self._files[key].name == name:
                    return key
        return next(reversed(self._files), None)

    def _ensure_overall(self) -> None:
        """Gesamtzeile anlegen, falls sie noch fehlt."""
        if self._overall is not None:
            return
        self._overall = self._progress.add_task(
            "Total ",
            total=None,
            row=_ROW_OVERALL,
            files_done=self._files_ok + self._files_failed,
            files_total=self._files_total,
        )

    def _advance_task(self, task: TaskID | None, n_bytes: int) -> None:
        if task is None:
            return
        try:
            self._progress.advance(task, n_bytes)
        except Exception:  # pragma: no cover - Anzeige darf nie werfen
            pass

    def _update(self, task: TaskID | None, **fields: object) -> None:
        if task is None:
            return
        try:
            self._progress.update(task, **fields)
        except Exception:  # pragma: no cover - Anzeige darf nie werfen
            pass

    def _ensure_live(self) -> None:
        """Live-Anzeige verzögert starten — in ``quiet`` gar nicht."""
        if self._quiet or self._live or self._closed:
            return
        try:
            self._progress.start()
        except Exception:  # pragma: no cover - Anzeige darf nie werfen
            return
        self._live = True

    def _print(self, renderable: Text) -> None:
        try:
            self._console.print(renderable)
        except Exception:  # pragma: no cover - Anzeige darf nie werfen
            pass

    def _flush(self) -> None:
        flush = getattr(self._console.file, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception:  # pragma: no cover
                pass


# --------------------------------------------------------------------------
# Nicht-TTY
# --------------------------------------------------------------------------


class PlainReporter:
    """Zeilenweise Ausgabe ohne ANSI-Steuerzeichen.

    Für Cron, Pipes und Logdateien. Pro Datei erscheinen eine Start- und
    eine Abschlusszeile, und jede Zeile nennt die Datei: bei ``--jobs 2``
    verschränken sich sonst die Meldungen zweier Downloads unlesbar.
    Zwischenstände werden gedrosselt: eine Meldung erst, wenn **sowohl**
    ``min_interval`` Sekunden vergangen **als auch** ``min_percent``
    Prozentpunkte hinzugekommen sind - die seltenere der beiden
    Bedingungen bestimmt also den Takt. Bei unbekannter Dateigröße
    entfällt die Prozentbedingung, es bleibt der Zeittakt. Gedrosselt wird
    pro Datei, nicht pro Reporter, sonst verschluckt eine schnelle Datei
    die Meldungen einer langsamen.

    ``clock`` ist injizierbar, damit die Drosselung deterministisch
    testbar ist.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        quiet: bool = False,
        clock: Callable[[], float] | None = None,
        min_interval: float = PLAIN_MIN_INTERVAL,
        min_percent: float = PLAIN_MIN_PERCENT,
    ) -> None:
        self._stream = stream
        self._quiet = bool(quiet)
        self._clock = clock if clock is not None else time.monotonic
        self._min_interval = float(min_interval)
        self._min_percent = float(min_percent)
        self._closed = False

        self._files_total = 0
        self._files_ok = 0
        self._files_failed = 0
        self._bytes_done = 0
        self._started_at = self._clock()

        self._files: dict[int, _PlainFile] = {}
        self._next_handle = 0
        self._files_started = 0

    # -- Protokoll ---------------------------------------------------------

    def start_overall(self, total_files: int, total_bytes: int) -> None:
        self._files_total = int(total_files)
        self._started_at = self._clock()
        if not self._quiet:
            self._emit(f"Start: {self._files_total} files, {human_bytes(total_bytes)}")

    def start_file(
        self, name: str, total_bytes: int | None, already_done: int = 0
    ) -> object:
        now = self._clock()
        self._files_started += 1
        state = _PlainFile(
            name=str(name),
            total=total_bytes,
            index=self._files_started,
            started_at=now,
            done=max(int(already_done), 0),
            last_report_at=now,
        )
        state.last_report_percent = self._percent(state)
        handle = self._register(state)
        if self._quiet:
            return handle
        size = human_bytes(total_bytes) if total_bytes is not None else "unknown size"
        line = f"[{state.index}/{self._files_total}] {state.name} - {size}"
        if state.done:
            line += f" (resuming at {human_bytes(state.done)})"
        self._emit(line)
        return handle

    def advance(self, n_bytes: int, handle: object | None = None) -> None:
        if self._closed or not n_bytes or n_bytes < 0:
            return
        # Die Gesamtsumme stimmt auch dann, wenn das Handle nicht mehr
        # auflösbar ist: die Bytes sind tatsächlich geflossen.
        self._bytes_done += n_bytes
        state = self._resolve(handle)
        if state is None:
            return
        state.done += n_bytes
        self._maybe_report(state)

    def finish_file(
        self, name: str, ok: bool, detail: str = "", handle: object | None = None
    ) -> None:
        state_id = self._resolve_id(handle, name)
        state = self._files.pop(state_id) if state_id is not None else None
        now = self._clock()
        elapsed = now - (state.started_at if state is not None else self._started_at)
        done = state.done if state is not None else 0
        if ok:
            self._files_ok += 1
        else:
            self._files_failed += 1

        index = state.index if state is not None else self._files_ok + self._files_failed
        prefix = f"[{index}/{self._files_total}]"
        if ok:
            if not self._quiet:
                line = f"{prefix} OK {name} - {human_bytes(done)}"
                line += f" in {human_duration(elapsed)}"
                self._emit(line)
        else:
            line = f"{prefix} FAILED {name}"
            if detail:
                line += f" - {detail}"
            self._emit(line)

    def message(self, text: str) -> None:
        if self._quiet:
            return
        self._emit(str(text))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._emit(_summary_line(self))

    # -- intern ------------------------------------------------------------

    def _register(self, state: _PlainFile) -> _FileHandle:
        self._next_handle += 1
        self._files[self._next_handle] = state
        return _FileHandle(self._next_handle)

    def _resolve(self, handle: object | None) -> _PlainFile | None:
        """Eintrag zum Handle. Ohne Handle die zuletzt begonnene offene Datei."""
        state_id = self._resolve_id(handle, None)
        return None if state_id is None else self._files[state_id]

    def _resolve_id(self, handle: object | None, name: str | None) -> int | None:
        """Schlüssel des gemeinten Eintrags, ``None`` wenn keiner passt.

        Ein unbekanntes oder veraltetes Handle liefert ``None`` statt eines
        falschen Eintrags: lieber nichts verbuchen als bei der falschen
        Datei.
        """
        if handle is not None:
            if isinstance(handle, _FileHandle) and handle.id in self._files:
                return handle.id
            return None
        if name is not None:
            for key in reversed(list(self._files)):
                if self._files[key].name == name:
                    return key
        return next(reversed(self._files), None)

    @staticmethod
    def _percent(state: _PlainFile) -> float:
        if not state.total:
            return 0.0
        return 100.0 * state.done / state.total

    def _maybe_report(self, state: _PlainFile) -> None:
        """Zwischenstand nur ausgeben, wenn beide Drosseln dieser Datei offen sind."""
        if self._quiet:
            return
        now = self._clock()
        if now - state.last_report_at < self._min_interval:
            return
        percent = self._percent(state)
        if state.total and percent - state.last_report_percent < self._min_percent:
            return

        line = f"    {state.name}: {human_bytes(state.done)}"
        if state.total:
            line += f"/{human_bytes(state.total)} ({percent:.0f}%)"
        elapsed = now - state.started_at
        if elapsed > 0:
            line += f" · {human_bytes(state.done / elapsed)}/s"
        self._emit(line)

        state.last_report_at = now
        state.last_report_percent = percent

    def _emit(self, text: str) -> None:
        stream = self._stream if self._stream is not None else sys.stdout
        try:
            stream.write(text + "\n")
        except Exception:  # pragma: no cover - Anzeige darf nie werfen
            return
        flush = getattr(stream, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception:  # pragma: no cover
                pass


def _summary_line(reporter: RichReporter | PlainReporter) -> str:
    """Gemeinsame Abschlusszeile beider Reporter."""
    elapsed = reporter._clock() - reporter._started_at
    done = reporter._files_ok
    total = reporter._files_total or done + reporter._files_failed
    line = (
        f"Done: {done}/{total} files, "
        f"{human_bytes(reporter._bytes_done)} in {human_duration(elapsed)}"
    )
    if reporter._files_failed:
        line += f", {reporter._files_failed} failed"
    return line


# --------------------------------------------------------------------------
# Fabrik
# --------------------------------------------------------------------------


def _is_tty(stream: object) -> bool:
    """``True``, wenn ``stream`` verlässlich ein Terminal meldet."""
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except Exception:
        return False


def make_reporter(
    quiet: bool = False,
    force_plain: bool = False,
    stream: TextIO | None = None,
) -> RichReporter | PlainReporter:
    """Passenden Reporter wählen (KONZEPT.md §5.4).

    Erkennt automatisch per ``stream.isatty()`` — Default ist
    ``sys.stdout``, erst zum Aufrufzeitpunkt aufgelöst, damit
    umgeleitete Ausgabe erkannt wird. ``force_plain`` erzwingt die
    zeilenweise Ausgabe auch im Terminal.
    """
    target = stream if stream is not None else sys.stdout
    if force_plain or not _is_tty(target):
        return PlainReporter(target, quiet=quiet)
    return RichReporter(stream=target, quiet=quiet)
