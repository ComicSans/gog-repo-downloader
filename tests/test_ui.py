"""Tests für die Fortschrittsanzeige (KONZEPT.md §5.4).

Keine Netzwerk-, Datei- oder Terminalabhängigkeit: Streams sind
``io.StringIO``, die Zeitquelle des PlainReporter ist injiziert.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from gogdl.model.protocols import ProgressReporter
from gogdl.ui import (
    PlainReporter,
    RichReporter,
    human_bytes,
    human_duration,
    make_reporter,
)

ANSI = "\x1b["
"""Beginn jeder ANSI-Steuersequenz — darf in Plain-Ausgabe nie vorkommen."""

GB = 1024**3


class FakeStream(io.StringIO):
    """StringIO mit steuerbarem ``isatty()``.

    Echte Datei-API, damit ``rich.Console`` daran nicht scheitert.
    """

    def __init__(self, tty: bool = False) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:  # noqa: D102 - siehe Klassendoc
        return self._tty


class FakeClock:
    """Manuell fortschaltbare Zeitquelle."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def lines(stream: io.StringIO) -> list[str]:
    """Nicht-leere Ausgabezeilen."""
    return [line for line in stream.getvalue().splitlines() if line.strip()]


# --------------------------------------------------------------------------
# make_reporter
# --------------------------------------------------------------------------


def test_make_reporter_waehlt_plain_ohne_tty() -> None:
    reporter = make_reporter(stream=FakeStream(tty=False))
    assert isinstance(reporter, PlainReporter)


def test_make_reporter_waehlt_rich_bei_tty() -> None:
    reporter = make_reporter(stream=FakeStream(tty=True))
    assert isinstance(reporter, RichReporter)
    reporter.close()


def test_make_reporter_force_plain_schlaegt_tty() -> None:
    reporter = make_reporter(force_plain=True, stream=FakeStream(tty=True))
    assert isinstance(reporter, PlainReporter)


def test_make_reporter_ohne_isatty_faellt_auf_plain_zurueck() -> None:
    class Dumb:
        def write(self, text: str) -> int:
            return len(text)

    assert isinstance(make_reporter(stream=Dumb()), PlainReporter)  # type: ignore[arg-type]


def test_make_reporter_reicht_quiet_durch() -> None:
    reporter = make_reporter(quiet=True, stream=FakeStream(tty=False))
    assert isinstance(reporter, PlainReporter)
    assert reporter._quiet is True


@pytest.mark.parametrize(
    "reporter",
    [PlainReporter(io.StringIO()), RichReporter(console=Console(file=io.StringIO(), width=100))],
)
def test_reporter_erfuellen_das_protocol(reporter: object) -> None:
    assert isinstance(reporter, ProgressReporter)


# --------------------------------------------------------------------------
# PlainReporter
# --------------------------------------------------------------------------


def test_plain_ausgabe_ohne_ansi_sequenzen() -> None:
    stream = FakeStream()
    reporter = PlainReporter(stream, clock=FakeClock())
    reporter.start_overall(2, 3 * GB)
    reporter.start_file("setup_bg3_de.bin", 2 * GB)
    reporter.advance(1024)
    reporter.message("Hinweis mit [Klammern] und *Sternen*")
    reporter.finish_file("setup_bg3_de.bin", ok=True)
    reporter.close()

    text = stream.getvalue()
    assert ANSI not in text
    assert "setup_bg3_de.bin" in text
    assert "Fertig:" in text


def test_plain_start_und_abschlusszeile_pro_datei() -> None:
    stream = FakeStream()
    reporter = PlainReporter(stream, clock=FakeClock())
    reporter.start_overall(1, GB)
    reporter.start_file("a.bin", GB)
    reporter.finish_file("a.bin", ok=True)

    out = lines(stream)
    assert any(line.startswith("[1/1] a.bin") for line in out)
    assert any("OK a.bin" in line for line in out)


def test_plain_quiet_meldet_erfolg_nur_im_abschluss() -> None:
    stream = FakeStream()
    reporter = PlainReporter(stream, quiet=True, clock=FakeClock())
    reporter.start_overall(1, GB)
    reporter.start_file("a.bin", GB)
    reporter.advance(GB)
    reporter.message("egal")
    reporter.finish_file("a.bin", ok=True)

    assert lines(stream) == []  # vor close() keine einzige Zeile

    reporter.close()
    out = lines(stream)
    assert len(out) == 1
    assert out[0].startswith("Fertig:")


def test_plain_quiet_meldet_fehler() -> None:
    stream = FakeStream()
    reporter = PlainReporter(stream, quiet=True, clock=FakeClock())
    reporter.start_overall(1, GB)
    reporter.start_file("a.bin", GB)
    reporter.finish_file("a.bin", ok=False, detail="HTTP 403")

    out = lines(stream)
    assert len(out) == 1
    assert "FEHLER" in out[0]
    assert "a.bin" in out[0]
    assert "HTTP 403" in out[0]
    assert ANSI not in out[0]


def test_plain_drosselt_schnelle_advance_aufrufe() -> None:
    stream = FakeStream()
    clock = FakeClock()
    reporter = PlainReporter(stream, clock=clock)
    reporter.start_overall(1, 1000 * 1024)
    reporter.start_file("a.bin", 1000 * 1024)
    before = len(lines(stream))

    for _ in range(1000):
        reporter.advance(1024)  # 1000 Aufrufe, keine Zeit vergeht

    assert len(lines(stream)) == before


def test_plain_meldet_zwischenstand_nur_wenn_beide_drosseln_offen_sind() -> None:
    """Beide Bedingungen einzeln festgenagelt — die UND-Logik aus §5.4."""
    mb = 1024 * 1024
    stream = FakeStream()
    clock = FakeClock()
    reporter = PlainReporter(stream, clock=clock, min_interval=10.0, min_percent=10.0)
    reporter.start_overall(1, 100 * mb)
    reporter.start_file("a.bin", 100 * mb)
    before = len(lines(stream))

    # 50 % Fortschritt, aber keine Zeit vergangen -> keine Meldung.
    reporter.advance(50 * mb)
    assert len(lines(stream)) == before

    # Zeit UND Prozent erfüllt -> genau eine Meldung.
    clock.tick(30.0)
    reporter.advance(20 * mb)
    out = lines(stream)
    assert len(out) == before + 1
    assert "a.bin" in out[-1]
    assert "70%" in out[-1]

    # Nur Prozent, keine Zeit -> die Zeitdrossel hält dicht.
    reporter.advance(1 * mb)
    assert len(lines(stream)) == before + 1

    # Nur Zeit, aber bloß ~2 % mehr -> die Prozentdrossel hält dicht.
    # Genau dieser Fall unterscheidet die UND- von einer reinen Zeitdrossel.
    clock.tick(300.0)
    reporter.advance(1 * mb)
    assert len(lines(stream)) == before + 1

    # Sobald auch die Prozentschwelle überschritten ist, geht es weiter.
    reporter.advance(10 * mb)
    assert len(lines(stream)) == before + 2


def test_plain_ohne_gesamtgroesse_drosselt_nur_ueber_die_zeit() -> None:
    stream = FakeStream()
    clock = FakeClock()
    reporter = PlainReporter(stream, clock=clock, min_interval=10.0)
    reporter.start_overall(1, 0)
    reporter.start_file("a.bin", None)
    before = len(lines(stream))

    for _ in range(100):
        reporter.advance(1024)
    assert len(lines(stream)) == before

    clock.tick(11.0)
    reporter.advance(1024)
    out = lines(stream)
    assert len(out) == before + 1
    assert "a.bin" in out[-1]


def test_plain_advance_ohne_start_file_crasht_nicht() -> None:
    stream = FakeStream()
    clock = FakeClock()
    reporter = PlainReporter(stream, clock=clock)
    reporter.advance(4096)
    clock.tick(60.0)
    reporter.advance(4096)
    reporter.close()

    assert ANSI not in stream.getvalue()
    assert "Fertig:" in stream.getvalue()


def test_plain_doppeltes_close_schreibt_nur_eine_zeile() -> None:
    stream = FakeStream()
    reporter = PlainReporter(stream, clock=FakeClock())
    reporter.start_overall(0, 0)
    reporter.close()
    reporter.close()
    reporter.close()

    assert sum(1 for line in lines(stream) if line.startswith("Fertig:")) == 1


def test_plain_zaehlt_fehler_in_der_abschlusszeile() -> None:
    stream = FakeStream()
    reporter = PlainReporter(stream, clock=FakeClock())
    reporter.start_overall(2, 2 * GB)
    reporter.start_file("a.bin", GB)
    reporter.finish_file("a.bin", ok=True)
    reporter.start_file("b.bin", GB)
    reporter.finish_file("b.bin", ok=False, detail="Abbruch")
    reporter.close()

    summary = [line for line in lines(stream) if line.startswith("Fertig:")][0]
    assert "1/2 Dateien" in summary
    assert "1 Fehler" in summary


# --------------------------------------------------------------------------
# RichReporter
# --------------------------------------------------------------------------


def rich_reporter(**kwargs: object) -> tuple[RichReporter, io.StringIO]:
    stream = io.StringIO()
    console = Console(file=stream, width=100, force_terminal=False)
    return RichReporter(console=console, **kwargs), stream  # type: ignore[arg-type]


def test_rich_durchlaeuft_kompletten_zyklus_ohne_exception() -> None:
    reporter, stream = rich_reporter()
    reporter.start_overall(2, 3 * GB)
    reporter.start_file("setup_bg3_de.bin", 2 * GB)
    for _ in range(50):
        reporter.advance(1024 * 1024)
    reporter.message("Zwischenmeldung")
    reporter.finish_file("setup_bg3_de.bin", ok=True)
    reporter.start_file("extras.zip", None, already_done=512)
    reporter.advance(2048)
    reporter.finish_file("extras.zip", ok=False, detail="Timeout")
    reporter.close()

    text = stream.getvalue()
    assert "setup_bg3_de.bin" in text
    assert "FEHLER" in text
    assert "Fertig:" in text


def test_rich_kommt_mit_unbekannter_gesamtgroesse_klar() -> None:
    reporter, _ = rich_reporter()
    reporter.start_overall(1, 0)
    reporter.start_file("unbekannt.bin", None)
    reporter.advance(1024)
    reporter.finish_file("unbekannt.bin", ok=True)
    reporter.close()


def test_rich_zweite_datei_ohne_groesse_erbt_die_erste_groesse_nicht() -> None:
    """Regression: eine wiederverwendete Zeile behielt sonst ``total``."""
    reporter, _ = rich_reporter()
    reporter.start_overall(2, GB)
    reporter.start_file("mit_groesse.bin", GB)
    reporter.finish_file("mit_groesse.bin", ok=True)
    reporter.start_file("ohne_groesse.bin", None)

    aktuell = [t for t in reporter._progress.tasks if t.fields.get("row") == "file"]
    assert len(aktuell) == 1
    assert aktuell[0].total is None  # unbestimmter Balken
    reporter.close()


def test_rich_advance_ohne_start_file_crasht_nicht() -> None:
    reporter, _ = rich_reporter()
    reporter.advance(1024)
    reporter.finish_file("nie_gestartet.bin", ok=True)
    reporter.close()


def test_rich_doppeltes_close_crasht_nicht() -> None:
    reporter, stream = rich_reporter()
    reporter.start_overall(1, GB)
    reporter.close()
    reporter.close()
    reporter.close()

    assert stream.getvalue().count("Fertig:") == 1


def test_rich_quiet_zeigt_nur_fehler_und_abschluss() -> None:
    reporter, stream = rich_reporter(quiet=True)
    reporter.start_overall(2, 2 * GB)
    reporter.start_file("a.bin", GB)
    reporter.advance(GB)
    reporter.message("unterdrueckt")
    reporter.finish_file("a.bin", ok=True)
    assert stream.getvalue() == ""

    reporter.start_file("b.bin", GB)
    reporter.finish_file("b.bin", ok=False, detail="HTTP 500")
    reporter.close()

    text = stream.getvalue()
    assert "unterdrueckt" not in text
    assert "FEHLER" in text
    assert "b.bin" in text
    assert "Fertig:" in text


def test_rich_dateiname_mit_markup_wird_nicht_interpretiert() -> None:
    reporter, stream = rich_reporter()
    reporter.start_overall(1, GB)
    reporter.finish_file("setup_[bold]_1.bin", ok=False, detail="kaputt")
    reporter.close()

    assert "[bold]" in stream.getvalue()


# --------------------------------------------------------------------------
# human_bytes / human_duration
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (1, "1 B"),
        (512, "512 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024**2, "1.0 MB"),
        (int(4.2 * GB), "4.2 GB"),
        (1024**4, "1.0 TB"),
        (-2048, "-2.0 KB"),
    ],
)
def test_human_bytes(value: int, expected: str) -> None:
    assert human_bytes(value) == expected


def test_human_bytes_sehr_grosse_werte_laufen_nicht_aus_den_einheiten() -> None:
    # Weit jenseits der letzten Einheit: keine IndexError, sondern YB.
    assert human_bytes(1024**9).endswith(" YB")
    assert human_bytes(1024**20).endswith(" YB")
    assert human_bytes(10**30).endswith(" YB")


def test_human_bytes_unbrauchbare_eingaben() -> None:
    assert human_bytes(None) == "?"
    assert human_bytes(float("nan")) == "?"
    assert human_bytes(float("inf")) == "?"
    assert human_bytes("keine Zahl") == "?"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0s"),
        (1, "1s"),
        (59, "59s"),
        (60, "1m"),
        (80, "1m20s"),
        (42 * 60, "42m"),
        (3599.6, "1h"),
        (3600, "1h"),
        (7530, "2h05m"),
        (2 * 86400 + 3 * 3600, "2d03h"),
    ],
)
def test_human_duration(value: float, expected: str) -> None:
    assert human_duration(value) == expected


def test_human_duration_unbrauchbare_eingaben() -> None:
    assert human_duration(None) == "?"
    assert human_duration(-1) == "?"
    assert human_duration(float("inf")) == "?"
    assert human_duration("gleich") == "?"
