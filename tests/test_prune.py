"""Tests für die zweite Sicherheitsstufe: prune/ lehnt ab, was sync/ plante.

Jeder Test baut ein echtes Archiv unter ``tmp_path`` auf. Der Store ist ein
kleiner Fake — geprüft wird das Verhalten von ``PruneExecutor``, nicht die
Datenbank.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from gogdl.model.protocols import Pruner
from gogdl.model.types import (
    FileKind,
    LocalState,
    ManifestEntry,
    OsName,
    PruneItem,
    PruneMode,
    SlotKey,
)
from gogdl.prune import PruneExecutor, freed_bytes

SLOT = SlotKey(product_id=42, kind=FileKind.INSTALLER, os=OsName.WINDOWS, language="en")
FIXED_DAY = date(2026, 8, 4)


class FakeStore:
    """Nur ``entries_for_slot`` wird von prune/ benötigt."""

    def __init__(self, *entries: ManifestEntry) -> None:
        self._entries = list(entries)

    def entries_for_slot(self, slot: SlotKey) -> list[ManifestEntry]:
        return [entry for entry in self._entries if entry.slot == slot]

    # Restliche Store-Methoden sind hier nicht im Spiel.
    def __getattr__(self, name: str):  # pragma: no cover - Schutz vor Tippfehlern
        raise AssertionError(f"prune/ darf Store.{name} nicht aufrufen")


def make_entry(
    file_id: str,
    filename: str,
    *,
    relative_path: str | None = None,
    state: LocalState = LocalState.COMPLETE,
    verified: bool = True,
    version: str | None = "2.1.1",
    part_index: int = 1,
    total_parts: int = 1,
) -> ManifestEntry:
    return ManifestEntry(
        slot=SLOT,
        file_id=file_id,
        filename=filename,
        version=version,
        size=100,
        md5=None,
        downlink="/downlink/installer",
        part_index=part_index,
        total_parts=total_parts,
        relative_path=relative_path if relative_path is not None else f"spielname/{filename}",
        state=state,
        last_verified_utc="2026-08-04T10:00:00Z" if verified else None,
    )


def make_item(path: Path, *, replaced_by: tuple[str, ...] = (), size: int = 100) -> PruneItem:
    return PruneItem(
        path=path,
        slot=SLOT,
        reason="ersetzt durch 2.1.1",
        size=size,
        old_version="2.1.0",
        new_version="2.1.1",
        replaced_by=replaced_by,
    )


def write(path: Path, content: bytes = b"x" * 100) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def make_executor(
    dest: Path, store: FakeStore, mode: PruneMode | None = None
) -> PruneExecutor:
    return PruneExecutor(dest, store, mode, today=lambda: FIXED_DAY)


# -- Grundfall -------------------------------------------------------------


def test_protokoll_erfuellt(tmp_path: Path) -> None:
    assert isinstance(make_executor(tmp_path, FakeStore()), Pruner)


def test_loescht_bei_verifiziertem_ersatz(tmp_path: Path) -> None:
    alt = write(tmp_path / "spielname" / "setup_2.1.0.exe")
    neu = write(tmp_path / "spielname" / "setup_2.1.1.exe")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))
    item = make_item(alt, replaced_by=("neu",))

    results = make_executor(tmp_path, store).execute([item])

    assert [r.removed for r in results] == [True]
    assert not alt.exists()
    assert neu.exists()
    assert freed_bytes(results) == 100


# -- Prüfung 3: Ersatz -----------------------------------------------------


def test_ersatz_nicht_verifiziert_verhindert_loeschung(tmp_path: Path) -> None:
    """Der Plan enthielt den Eintrag — die zweite Stufe greift trotzdem."""
    alt = write(tmp_path / "spielname" / "setup_2.1.0.exe")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe", verified=False))

    results = make_executor(tmp_path, store).execute([make_item(alt, replaced_by=("neu",))])

    assert results[0].removed is False
    assert "nicht verifiziert" in results[0].reason
    assert alt.exists()
    assert freed_bytes(results) == 0


def test_unvollstaendiger_mehrteiliger_ersatz_verhindert_loeschung(tmp_path: Path) -> None:
    alt = write(tmp_path / "spielname" / "setup_2.1.0-1.bin")
    store = FakeStore(
        make_entry("neu1", "setup_2.1.1-1.bin", part_index=1, total_parts=2),
        make_entry(
            "neu2",
            "setup_2.1.1-2.bin",
            part_index=2,
            total_parts=2,
            state=LocalState.PARTIAL,
            verified=False,
        ),
    )

    results = make_executor(tmp_path, store).execute(
        [make_item(alt, replaced_by=("neu1", "neu2"))]
    )

    assert results[0].removed is False
    assert alt.exists()


def test_nur_ein_teil_als_ersatz_benannt_wird_abgelehnt(tmp_path: Path) -> None:
    """Über die übrigen Teile ist nichts bekannt — also keine Löschung."""
    alt = write(tmp_path / "spielname" / "setup_2.1.0-1.bin")
    store = FakeStore(make_entry("neu1", "setup_2.1.1-1.bin", part_index=1, total_parts=2))

    results = make_executor(tmp_path, store).execute([make_item(alt, replaced_by=("neu1",))])

    assert results[0].removed is False
    assert "unvollständig" in results[0].reason
    assert alt.exists()


def test_unbekannter_ersatz_wird_abgelehnt(tmp_path: Path) -> None:
    alt = write(tmp_path / "spielname" / "setup_2.1.0.exe")

    results = make_executor(tmp_path, FakeStore()).execute(
        [make_item(alt, replaced_by=("fehlt",))]
    )

    assert results[0].removed is False
    assert alt.exists()


def test_ohne_ersatz_wird_abgelehnt(tmp_path: Path) -> None:
    alt = write(tmp_path / "spielname" / "setup_2.1.0.exe")

    results = make_executor(tmp_path, FakeStore()).execute([make_item(alt)])

    assert results[0].removed is False
    assert results[0].reason == "kein Ersatz benannt"
    assert alt.exists()


def test_part_rest_darf_ohne_ersatz_weg(tmp_path: Path) -> None:
    rest = write(tmp_path / "spielname" / "setup_2.1.0.exe.part")

    results = make_executor(tmp_path, FakeStore()).execute([make_item(rest)])

    assert results[0].removed is True
    assert not rest.exists()


# -- Prüfung 5: Ersatz liegt wirklich auf der Platte -----------------------


def test_ersatz_fehlt_auf_der_platte_wird_abgelehnt(tmp_path: Path) -> None:
    """Das Manifest sagt COMPLETE, die Datei ist trotzdem weg.

    Verschoben, geloescht, oder das Volume war beim Scan nicht
    eingehaengt. KONZEPT.md §5.5 verlangt einen Ersatz *auf der Platte* -
    sonst bleibt nach der Loeschung gar nichts uebrig.
    """
    alt = write(tmp_path / "spielname" / "setup_2.1.0.exe")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))

    results = make_executor(tmp_path, store).execute([make_item(alt, replaced_by=("neu",))])

    assert results[0].removed is False
    assert "nicht auf der Platte" in results[0].reason
    assert alt.exists()


def test_ersatz_mit_falscher_groesse_wird_abgelehnt(tmp_path: Path) -> None:
    alt = write(tmp_path / "spielname" / "setup_2.1.0.exe")
    write(tmp_path / "spielname" / "setup_2.1.1.exe", b"x" * 42)  # Soll waeren 100
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))

    results = make_executor(tmp_path, store).execute([make_item(alt, replaced_by=("neu",))])

    assert results[0].removed is False
    assert "42" in results[0].reason
    assert alt.exists()


def test_ersatz_pruefung_auch_im_dry_run(tmp_path: Path) -> None:
    """Der Trockenlauf darf keine Loeschung versprechen, die nie faellt."""
    alt = write(tmp_path / "spielname" / "setup_2.1.0.exe")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))

    results = make_executor(tmp_path, store).execute(
        [make_item(alt, replaced_by=("neu",))], dry_run=True
    )

    assert results[0].removed is False


# -- Prüfung 4: Datei ist selbst der Ersatz --------------------------------


def test_ersatz_darf_nicht_selbst_geloescht_werden(tmp_path: Path) -> None:
    neu = write(tmp_path / "spielname" / "setup_2.1.1.exe")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))

    results = make_executor(tmp_path, store).execute([make_item(neu, replaced_by=("neu",))])

    assert results[0].removed is False
    assert "selbst" in results[0].reason
    assert neu.exists()


def test_abweichende_schreibweise_schuetzt_den_ersatz(tmp_path: Path) -> None:
    """Auf der Platte steht eine andere Schreibweise als im Manifest.

    Ein reiner ``Path``-Vergleich greift auf einem case-insensitiven
    Volume nicht: der Selbstschutz laeuft ins Leere und die einzige
    vorhandene Fassung wird geloescht. Der Vergleich muss deshalb ueber
    ``st_ino``/``st_dev`` laufen, mit casefold als Rueckfall, wenn die
    Datei unter der Manifest-Schreibweise gar nicht existiert.
    """
    auf_platte = write(tmp_path / "spielname" / "Setup_2.1.1.exe")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))

    results = make_executor(tmp_path, store).execute(
        [make_item(auf_platte, replaced_by=("neu",))]
    )

    assert results[0].removed is False
    assert auf_platte.exists()


# -- Prüfung 1 und 2: Pfad -------------------------------------------------


def test_pfad_ausserhalb_von_dest_wird_abgelehnt(tmp_path: Path) -> None:
    dest = tmp_path / "archiv"
    dest.mkdir()
    fremd = write(tmp_path / "fremd.bin")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))

    results = make_executor(dest, store).execute(
        [make_item(dest / ".." / "fremd.bin", replaced_by=("neu",))]
    )

    assert results[0].removed is False
    assert fremd.exists()


def test_absoluter_pfad_ausserhalb_von_dest_wird_abgelehnt(tmp_path: Path) -> None:
    dest = tmp_path / "archiv"
    dest.mkdir()
    fremd = write(tmp_path / "anderswo" / "fremd.bin")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))

    results = make_executor(dest, store).execute([make_item(fremd, replaced_by=("neu",))])

    assert results[0].removed is False
    assert "außerhalb" in results[0].reason
    assert fremd.exists()


def test_dest_ueber_symlink_benannt_blockiert_prune_nicht(tmp_path: Path) -> None:
    """``--dest /tmp/gog`` bei ``/tmp -> /private/tmp`` darf nicht alles ablehnen."""
    echt = tmp_path / "archiv"
    echt.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(echt, target_is_directory=True)
    alt = write(alias / "spielname" / "setup_2.1.0.exe")
    write(alias / "spielname" / "setup_2.1.1.exe")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))

    results = make_executor(alias, store).execute([make_item(alt, replaced_by=("neu",))])

    assert results[0].removed is True
    assert not (echt / "spielname" / "setup_2.1.0.exe").exists()


def test_symlink_als_zieldatei_wird_abgelehnt(tmp_path: Path) -> None:
    dest = tmp_path / "archiv"
    (dest / "spielname").mkdir(parents=True)
    opfer = write(tmp_path / "wichtig.bin")
    link = dest / "spielname" / "setup_2.1.0.exe"
    link.symlink_to(opfer)
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))

    results = make_executor(dest, store).execute([make_item(link, replaced_by=("neu",))])

    assert results[0].removed is False
    assert opfer.exists()
    assert link.is_symlink()


def test_symlink_als_elternverzeichnis_wird_abgelehnt(tmp_path: Path) -> None:
    dest = tmp_path / "archiv"
    dest.mkdir()
    fremdordner = tmp_path / "fremdordner"
    fremdordner.mkdir()
    opfer = write(fremdordner / "setup_2.1.0.exe")
    (dest / "spielname").symlink_to(fremdordner, target_is_directory=True)
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))

    results = make_executor(dest, store).execute(
        [make_item(dest / "spielname" / "setup_2.1.0.exe", replaced_by=("neu",))]
    )

    assert results[0].removed is False
    assert "Symlink" in results[0].reason
    assert opfer.exists()


# -- Nicht vorhanden, dry-run ----------------------------------------------


def test_fehlende_datei_ist_kein_fehler(tmp_path: Path) -> None:
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))
    fehlt = tmp_path / "spielname" / "setup_2.1.0.exe"

    results = make_executor(tmp_path, store).execute([make_item(fehlt, replaced_by=("neu",))])

    assert results[0].removed is False
    assert results[0].reason == "nicht vorhanden"


def test_dry_run_faesst_nichts_an(tmp_path: Path) -> None:
    alt = write(tmp_path / "spielname" / "setup_2.1.0.exe")
    abgelehnt = write(tmp_path / "spielname" / "fremd.bin")
    write(tmp_path / "spielname" / "setup_2.1.1.exe")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))
    items = [make_item(alt, replaced_by=("neu",)), make_item(abgelehnt)]

    results = make_executor(tmp_path, store).execute(items, dry_run=True)

    assert [r.removed for r in results] == [True, False]
    assert alt.exists()
    assert abgelehnt.exists()
    assert freed_bytes(results) == 100


# -- Trash-Modus -----------------------------------------------------------


def test_trash_modus_verschiebt_statt_zu_loeschen(tmp_path: Path) -> None:
    alt = write(tmp_path / "spielname" / "setup_2.1.0.exe")
    write(tmp_path / "spielname" / "setup_2.1.1.exe")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))

    results = make_executor(tmp_path, store, PruneMode.TRASH).execute(
        [make_item(alt, replaced_by=("neu",))]
    )

    ziel = tmp_path / ".trash" / "2026-08-04" / "spielname" / "setup_2.1.0.exe"
    assert results[0].removed is True
    assert not alt.exists()
    assert ziel.is_file()


def test_trash_modus_loest_namenskollision_auf(tmp_path: Path) -> None:
    write(tmp_path / "spielname" / "setup_2.1.1.exe")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))
    executor = make_executor(tmp_path, store, PruneMode.TRASH)
    tag = tmp_path / ".trash" / "2026-08-04" / "spielname"

    for durchgang in (b"a" * 100, b"b" * 100):
        alt = write(tmp_path / "spielname" / "setup_2.1.0.exe", durchgang)
        assert executor.execute([make_item(alt, replaced_by=("neu",))])[0].removed is True

    assert (tag / "setup_2.1.0.exe").read_bytes() == b"a" * 100
    assert (tag / "setup_2.1.0-1.exe").read_bytes() == b"b" * 100


def test_papierkorb_wird_nicht_selbst_gepruned(tmp_path: Path) -> None:
    rest = write(tmp_path / ".trash" / "2026-08-01" / "spielname" / "setup.exe.part")

    results = make_executor(tmp_path, FakeStore()).execute([make_item(rest)])

    assert results[0].removed is False
    assert rest.exists()


# -- Aufräumen leerer Verzeichnisse ---------------------------------------


def test_leeres_verzeichnis_wird_entfernt_dest_nicht(tmp_path: Path) -> None:
    alt = write(tmp_path / "spielname" / "extras" / "setup_2.1.0.exe")
    write(tmp_path / "anderswo" / "neu.exe")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe", relative_path="anderswo/neu.exe"))

    make_executor(tmp_path, store).execute([make_item(alt, replaced_by=("neu",))])

    assert not (tmp_path / "spielname" / "extras").exists()
    assert not (tmp_path / "spielname").exists()
    assert tmp_path.is_dir()


def test_nicht_leeres_verzeichnis_bleibt(tmp_path: Path) -> None:
    alt = write(tmp_path / "spielname" / "setup_2.1.0.exe")
    neu = write(tmp_path / "spielname" / "setup_2.1.1.exe")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe"))

    make_executor(tmp_path, store).execute([make_item(alt, replaced_by=("neu",))])

    assert neu.exists()
    assert (tmp_path / "spielname").is_dir()


# -- Fehlertoleranz --------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignoriert Verzeichnisrechte")
def test_permission_fehler_stoppt_den_lauf_nicht(tmp_path: Path) -> None:
    gesperrt_dir = tmp_path / "gesperrt"
    gesperrt = write(gesperrt_dir / "setup_2.1.0.exe")
    frei = write(tmp_path / "spielname" / "setup_2.1.0.exe")
    danach = write(tmp_path / "spielname2" / "setup_2.1.0.exe")
    write(tmp_path / "anderswo" / "neu.exe")
    store = FakeStore(make_entry("neu", "setup_2.1.1.exe", relative_path="anderswo/neu.exe"))
    items = [
        make_item(gesperrt, replaced_by=("neu",)),
        make_item(frei, replaced_by=("neu",)),
        make_item(danach, replaced_by=("neu",)),
    ]

    gesperrt_dir.chmod(0o500)
    try:
        results = make_executor(tmp_path, store).execute(items)
    finally:
        gesperrt_dir.chmod(0o700)

    assert [r.removed for r in results] == [False, True, True]
    assert "Fehler beim Entfernen" in results[0].reason
    assert gesperrt.exists()
    assert not frei.exists()
    assert not danach.exists()
    assert freed_bytes(results) == 200
