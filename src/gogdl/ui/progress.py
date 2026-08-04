"""Fortschrittsanzeige für TTY und Nicht-TTY (KONZEPT.md §5.4).

Zwei Implementierungen desselben Protocols ``model.protocols.ProgressReporter``:

``RichReporter``
    Live-Anzeige im Terminal, auf ~4 Hz gedrosselt. Zwei Zeilen: eine
    Gesamtzeile (Dateien x/y, Bytes, ETA) und eine Zeile für die laufende
    Datei (Name, Bytes, Rate, ETA).

``PlainReporter``
    Zeilenweise Meldungen ohne jedes ANSI-Steuerzeichen — für Cron, Pipes
    und Logdateien. Zwischenstände werden stark gedrosselt, damit ein
    nächtlicher Lauf keine Logdatei füllt.

``make_reporter`` wählt anhand von ``stream.isatty()`` automatisch. Ein
manueller Schalter ist nicht nötig, ``force_plain`` überschreibt trotzdem.

Beide Reporter sind robust gegen Aufrufe in falscher Reihenfolge: ein
``advance()`` ohne vorheriges ``start_file()`` wird verbucht statt zu
scheitern, und ``close()`` ist idempotent. Eine Anzeige darf einen laufenden
Download unter keinen Umständen abbrechen.
"""

from __future__ import annotations

import sys
import time
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
# Spalten der TTY-Anzeige
# --------------------------------------------------------------------------


class _FilesColumn(ProgressColumn):
    """„12/47 Dateien" — existiert nur in der Gesamtzeile."""

    def render(self, task: _RichTask) -> Text:
        if task.fields.get("row") != _ROW_OVERALL:
            return Text("")
        done = task.fields.get("files_done", 0)
        total = task.fields.get("files_total", 0)
        return Text(f"{done}/{total} Dateien", style="progress.percentage")


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

    Implementiert ``model.protocols.ProgressReporter``. ``quiet=True``
    unterdrückt die Balken vollständig und meldet nur noch Fehler und den
    Abschluss — im Zweifel ist eine stille Anzeige besser als eine, die
    einen Logstrom mit Steuerzeichen zerlegt.
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
        self._file: TaskID | None = None
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
        if self._overall is None:
            self._overall = self._progress.add_task(
                "Gesamt ",
                total=total_bytes,
                row=_ROW_OVERALL,
                files_done=0,
                files_total=self._files_total,
            )
        else:
            self._progress.update(
                self._overall,
                total=total_bytes,
                completed=0,
                files_done=0,
                files_total=self._files_total,
            )

    def start_file(self, name: str, total_bytes: int | None, already_done: int = 0) -> None:
        self._ensure_live()
        # Die alte Zeile wird ersetzt, nicht zurückgesetzt: ``reset()`` und
        # ``update()`` behandeln ``total=None`` als „nicht angegeben" und
        # würden die vorige Größe stehen lassen — eine Datei unbekannter
        # Größe bekäme dann keinen unbestimmten Balken.
        if self._file is not None:
            try:
                self._progress.remove_task(self._file)
            except Exception:  # pragma: no cover - Anzeige darf nie werfen
                pass
        # ``already_done`` zählt nicht zur Gesamtsumme: die enthält nur noch
        # zu ladende Bytes (SyncPlan.download_bytes rechnet ``resume_from``
        # bereits heraus).
        self._file = self._progress.add_task(
            _shorten(name),
            total=total_bytes,
            completed=already_done,
            row=_ROW_FILE,
        )

    def advance(self, n_bytes: int) -> None:
        if self._closed or not n_bytes or n_bytes < 0:
            return
        self._bytes_done += n_bytes
        if self._file is not None:
            self._progress.advance(self._file, n_bytes)
        if self._overall is not None:
            self._progress.advance(self._overall, n_bytes)

    def finish_file(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self._files_ok += 1
        else:
            self._files_failed += 1
        if self._overall is not None:
            self._progress.update(
                self._overall, files_done=self._files_ok + self._files_failed
            )
        if self._file is not None:
            self._progress.update(self._file, visible=False)

        if ok and self._quiet:
            return
        marker, style = ("OK", "green") if ok else ("FEHLER", "bold red")
        line = Text.assemble((marker, style), " ", str(name))
        if detail:
            line.append(f" — {detail}")
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
    eine Abschlusszeile. Zwischenstände werden gedrosselt: eine Meldung
    erst, wenn **sowohl** ``min_interval`` Sekunden vergangen **als auch**
    ``min_percent`` Prozentpunkte hinzugekommen sind — die seltenere der
    beiden Bedingungen bestimmt also den Takt. Bei unbekannter Dateigröße
    entfällt die Prozentbedingung, es bleibt der Zeittakt.

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

        self._name: str | None = None
        self._file_total: int | None = None
        self._file_done = 0
        self._file_started_at = self._started_at
        self._last_report_at = self._started_at
        self._last_report_percent = 0.0

    # -- Protokoll ---------------------------------------------------------

    def start_overall(self, total_files: int, total_bytes: int) -> None:
        self._files_total = int(total_files)
        self._started_at = self._clock()
        self._last_report_at = self._started_at
        if not self._quiet:
            self._emit(f"Start: {self._files_total} Dateien, {human_bytes(total_bytes)}")

    def start_file(self, name: str, total_bytes: int | None, already_done: int = 0) -> None:
        now = self._clock()
        self._name = str(name)
        self._file_total = total_bytes
        self._file_done = max(int(already_done), 0)
        self._file_started_at = now
        self._last_report_at = now
        self._last_report_percent = self._percent()
        if self._quiet:
            return
        size = human_bytes(total_bytes) if total_bytes is not None else "unbekannte Größe"
        line = f"[{self._index()}/{self._files_total}] {self._name} — {size}"
        if self._file_done:
            line += f" (Fortsetzung ab {human_bytes(self._file_done)})"
        self._emit(line)

    def advance(self, n_bytes: int) -> None:
        if self._closed or not n_bytes or n_bytes < 0:
            return
        self._file_done += n_bytes
        self._bytes_done += n_bytes
        self._maybe_report()

    def finish_file(self, name: str, ok: bool, detail: str = "") -> None:
        elapsed = self._clock() - self._file_started_at
        done = self._file_done
        if ok:
            self._files_ok += 1
        else:
            self._files_failed += 1

        prefix = f"[{self._index() - 1}/{self._files_total}]"
        if ok:
            if not self._quiet:
                line = f"{prefix} OK {name} — {human_bytes(done)}"
                line += f" in {human_duration(elapsed)}"
                self._emit(line)
        else:
            line = f"{prefix} FEHLER {name}"
            if detail:
                line += f" — {detail}"
            self._emit(line)

        self._name = None
        self._file_total = None
        self._file_done = 0

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

    def _index(self) -> int:
        """Laufende Nummer der aktuellen Datei, 1-basiert."""
        return self._files_ok + self._files_failed + 1

    def _percent(self) -> float:
        if not self._file_total:
            return 0.0
        return 100.0 * self._file_done / self._file_total

    def _maybe_report(self) -> None:
        """Zwischenstand nur ausgeben, wenn beide Drosseln geöffnet sind."""
        if self._quiet or self._name is None:
            return
        now = self._clock()
        if now - self._last_report_at < self._min_interval:
            return
        percent = self._percent()
        if self._file_total and percent - self._last_report_percent < self._min_percent:
            return

        line = f"    {self._name}: {human_bytes(self._file_done)}"
        if self._file_total:
            line += f"/{human_bytes(self._file_total)} ({percent:.0f}%)"
        elapsed = now - self._file_started_at
        if elapsed > 0:
            line += f" · {human_bytes(self._file_done / elapsed)}/s"
        self._emit(line)

        self._last_report_at = now
        self._last_report_percent = percent

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
        f"Fertig: {done}/{total} Dateien, "
        f"{human_bytes(reporter._bytes_done)} in {human_duration(elapsed)}"
    )
    if reporter._files_failed:
        line += f", {reporter._files_failed} Fehler"
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
