"""Sperre gegen zwei gleichzeitige schreibende Läufe auf dasselbe Ziel.

Alles läuft gegen ``tmp_path``. Eine echte Sammlung wird hier nie angefasst.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gogdl.cli import context
from gogdl.cli.context import dest_lock, lock_path
from gogdl.cli.main import main
from gogdl.errors import GogdlError, LockBusy


def _toter_pid() -> int:
    """Eine Prozessnummer, die sicher niemandem mehr gehört.

    Ein beendeter und abgeholter Kindprozess ist verlässlicher als eine
    geratene hohe Zahl: Die Nummer war eben noch gültig und ist es jetzt
    nicht mehr.
    """
    kind = subprocess.Popen([sys.executable, "-c", ""])
    kind.wait()
    return kind.pid


def _schreibe_sperre(dest: Path, pid: int, started: str = "2026-08-04T10:00:00+00:00") -> Path:
    path = lock_path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n{started}\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- Kern


def test_zweite_sperre_auf_dasselbe_ziel_scheitert(tmp_path: Path) -> None:
    with dest_lock(tmp_path):
        with pytest.raises(GogdlError) as exc:
            with dest_lock(tmp_path):
                pytest.fail("Die zweite Sperre haette nicht gelingen duerfen")
    meldung = str(exc.value)
    assert "Another gogdl run" in meldung
    assert str(lock_path(tmp_path)) in meldung
    assert f"process {os.getpid()}" in meldung
    assert "gogdl status" in meldung


def test_nach_dem_verlassen_gelingt_die_zweite_sperre(tmp_path: Path) -> None:
    with dest_lock(tmp_path) as path:
        assert path.exists()
    assert not lock_path(tmp_path).exists()

    with dest_lock(tmp_path):
        pass
    assert not lock_path(tmp_path).exists()


def test_ausnahme_gibt_die_sperre_frei_und_wird_durchgereicht(tmp_path: Path) -> None:
    class EigenerFehler(Exception):
        pass

    with pytest.raises(EigenerFehler, match="unveraendert"):
        with dest_lock(tmp_path):
            raise EigenerFehler("unveraendert")

    assert not lock_path(tmp_path).exists()
    # Und der nächste Lauf kommt sofort wieder durch.
    with dest_lock(tmp_path):
        pass


def test_strg_c_gibt_die_sperre_frei(tmp_path: Path) -> None:
    """``KeyboardInterrupt`` erbt von ``BaseException``, nicht von ``Exception``."""
    with pytest.raises(KeyboardInterrupt):
        with dest_lock(tmp_path):
            raise KeyboardInterrupt

    assert not lock_path(tmp_path).exists()


def test_sperrdatei_eines_toten_prozesses_wird_uebernommen(tmp_path: Path) -> None:
    _schreibe_sperre(tmp_path, _toter_pid())

    with dest_lock(tmp_path) as path:
        inhalt = path.read_text(encoding="utf-8").splitlines()
    assert int(inhalt[0]) == os.getpid()
    assert not lock_path(tmp_path).exists()


def test_sperrdatei_mit_lebendem_prozess_blockiert(tmp_path: Path) -> None:
    _schreibe_sperre(tmp_path, os.getpid(), started="2026-08-04T09:30:00+00:00")

    with pytest.raises(GogdlError) as exc:
        with dest_lock(tmp_path):
            pytest.fail("Eine lebende Prozessnummer haette blockieren muessen")
    assert f"process {os.getpid()}" in str(exc.value)
    assert "2026-08-04T09:30:00+00:00" in str(exc.value)
    # Die fremde Sperrdatei bleibt unangetastet.
    assert lock_path(tmp_path).read_text(encoding="utf-8").startswith(str(os.getpid()))


def test_unlesbare_sperrdatei_sperrt_niemanden_aus(tmp_path: Path) -> None:
    path = lock_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00kaputt")

    with dest_lock(tmp_path):
        pass


def test_zwei_ziele_sperren_sich_nicht_gegenseitig(tmp_path: Path) -> None:
    eins = tmp_path / "eins"
    zwei = tmp_path / "zwei"
    eins.mkdir()
    zwei.mkdir()

    with dest_lock(eins):
        with dest_lock(zwei):
            assert lock_path(eins).exists()
            assert lock_path(zwei).exists()


# ------------------------------------------------------- Rueckfall ohne flock


def _flock_ersatz(errnummer: int):
    """``fcntl.flock`` durch eine Fehlernummer ersetzen."""

    def unwirksam(fd, op):
        raise OSError(errnummer, os.strerror(errnummer))

    return unwirksam


def test_ohne_flock_entscheidet_die_prozessnummer_allein(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exFAT und Netzlaufwerke melden "kenne ich nicht" statt zu sperren."""
    monkeypatch.setattr(context.fcntl, "flock", _flock_ersatz(errno.ENOTSUP))

    # Tote Prozessnummer: übernehmen.
    _schreibe_sperre(tmp_path, _toter_pid())
    with dest_lock(tmp_path):
        pass

    # Lebende Prozessnummer: abweisen.
    _schreibe_sperre(tmp_path, os.getpid())
    with pytest.raises(GogdlError):
        with dest_lock(tmp_path):
            pytest.fail("Ohne flock haette die Prozessnummer blockieren muessen")


def test_ohne_flock_blockiert_die_eigene_prozessnummer_weiterhin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auch ohne ``flock`` trägt der Prozesscheck den geschachtelten Fall.

    Die verbleibende Lücke ist eine andere und lässt sich nicht als Test
    schreiben: Starten zwei Läufe gleichzeitig, bevor einer geschrieben
    hat, sieht keiner den anderen. Das steht im Docstring von ``dest_lock``.
    """
    monkeypatch.setattr(context.fcntl, "flock", _flock_ersatz(errno.ENOTSUP))
    with dest_lock(tmp_path):
        with pytest.raises(GogdlError):
            with dest_lock(tmp_path):
                pytest.fail("Die eigene lebende Nummer blockiert weiterhin")


def test_belegtes_flock_weist_auch_ohne_eintrag_ab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context.fcntl, "flock", _flock_ersatz(errno.EAGAIN))
    with pytest.raises(GogdlError) as exc:
        with dest_lock(tmp_path):
            pytest.fail("Ein belegtes flock haette abweisen muessen")
    assert "an unknown process" in str(exc.value)


# --------------------------------------------------------------------------- CLI


def test_status_laeuft_waehrend_die_sperre_gehalten_wird(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path.resolve()
    with dest_lock(dest):
        assert main(["status", "--dest", str(dest)]) in (0, 10)
        # status ist ein reiner Lesevorgang und darf neben einem laufenden
        # Download jederzeit Auskunft geben.
        assert "Another gogdl run" not in capsys.readouterr().err

        # verify sperrt dagegen mit: es setzt und entwertet
        # last_verified_utc, und genau ein falscher Stempel autorisiert
        # spaeter eine Loeschung.
        assert main(["verify", "--dest", str(dest)]) == LockBusy.exit_code
        assert "Another gogdl run" in capsys.readouterr().err


def test_clean_mit_apply_wird_von_der_sperre_abgewiesen(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path.resolve()
    with dest_lock(dest):
        rc = main(["clean", "--dest", str(dest), "--apply"])
    assert rc == LockBusy.exit_code
    assert "Another gogdl run" in capsys.readouterr().err


def test_clean_ohne_apply_laeuft_trotz_sperre(tmp_path: Path) -> None:
    dest = tmp_path.resolve()
    with dest_lock(dest):
        assert main(["clean", "--dest", str(dest)]) in (0, 10)


def test_import_ohne_apply_laeuft_trotz_sperre(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Der Trockenlauf kommt an der Sperre vorbei.

    Auf einem leeren Manifest endet er mit 1, aber aus eigenem Grund - der
    Text unterscheidet den Fall vom Abweisen.
    """
    dest = tmp_path.resolve()
    with dest_lock(dest):
        main(["import", "--dest", str(dest)])
    ausgabe = capsys.readouterr()
    assert "Another gogdl run" not in ausgabe.err
    assert "The manifest is empty" in ausgabe.out


def test_import_mit_apply_wird_abgewiesen(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path.resolve()
    with dest_lock(dest):
        rc = main(["import", "--dest", str(dest), "--apply"])
    assert rc == LockBusy.exit_code
    assert "Another gogdl run" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["download"], ["sync"], ["update"], ["verify"]])
def test_schreibende_kommandos_werden_abgewiesen(
    tmp_path: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path.resolve()
    with dest_lock(dest):
        rc = main([*argv, "--dest", str(dest)])
    assert rc == LockBusy.exit_code
    assert "Another gogdl run" in capsys.readouterr().err


def test_die_cli_laesst_keine_sperrdatei_zurueck(tmp_path: Path) -> None:
    dest = tmp_path.resolve()
    assert main(["clean", "--dest", str(dest), "--apply"]) in (0, 10)
    assert not lock_path(dest).exists()
