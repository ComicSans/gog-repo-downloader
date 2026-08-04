"""Tests über Modulgrenzen hinweg.

Die Modultests prüfen jeden Baustein für sich. Die teuersten Fehler dieses
Projekts liegen aber genau zwischen ihnen: ``sync/`` plant eine Löschung,
``prune/`` prüft sie erneut - und wenn beide Seiten unterschiedliche
Annahmen über den Plan haben, passiert schlicht nichts, ohne dass ein Test
rot wird.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gogdl.download.verify import verify_entry
from gogdl.model.types import (
    FileKind,
    LocalState,
    ManifestEntry,
    OsName,
    ProductRef,
    PruneMode,
    SlotKey,
    SyncConfig,
)
from gogdl.prune.executor import PruneExecutor, freed_bytes
from gogdl.store.sqlite_store import SqliteStore
from gogdl.sync.planner import plan_prune

PRODUCT_ID = 100
SLUG = "beispielspiel"
NOW = "2026-08-04T10:00:00+00:00"


def _slot() -> SlotKey:
    return SlotKey(PRODUCT_ID, FileKind.INSTALLER, OsName.MAC, "de")


def _entry(file_id: str, filename: str, size: int, part: int, parts: int) -> ManifestEntry:
    """Ein vollständig geladener und verifizierter Teil der neuen Version."""
    return ManifestEntry(
        slot=_slot(),
        file_id=file_id,
        filename=filename,
        version="2.1.1",
        size=size,
        md5=None,
        downlink=f"/downlink/{file_id}",
        part_index=part,
        total_parts=parts,
        relative_path=f"{SLUG}/{filename}",
        state=LocalState.COMPLETE,
        bytes_done=size,
        last_seen_utc=NOW,
        last_verified_utc=NOW,
    )


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _config(dest: Path, **overrides) -> SyncConfig:
    values = {
        "dest": dest,
        "os_filter": frozenset({OsName.MAC}),
        "languages": frozenset({"de"}),
        "prune": True,
        "keep_versions": 1,
        "prune_mode": PruneMode.DELETE,
    }
    values.update(overrides)
    return SyncConfig(**values)


def _store(entries: list[ManifestEntry]) -> SqliteStore:
    store = SqliteStore(":memory:")
    store.replace_products([ProductRef(PRODUCT_ID, "Beispielspiel", SLUG)])
    for entry in entries:
        store.update_entry(entry)
    return store


def _on_disk(root: Path) -> dict[Path, int]:
    return {p: p.stat().st_size for p in root.rglob("*") if p.is_file()}


def test_mehrteiliger_installer_wird_tatsaechlich_aufgeraeumt(tmp_path: Path) -> None:
    """Der Fall, an dem die Kernfunktion lautlos scheitern würde.

    Große Installer sind mehrteilig. Nennt der Plan pro alter Datei nur den
    einen neuen Teil als Ersatz, lehnt ``prune/`` die Löschung ab, weil es
    zu Recht alle Teile sehen will. Ergebnis: die Platte läuft voll und
    beide Modultests bleiben grün.
    """
    dest = tmp_path / "sammlung"
    game = dest / SLUG

    # Neue Version: beide Teile vollständig und verifiziert.
    neu = [
        ("f1", "setup_beispiel_2.1.1.pkg", 400),
        ("f2", "setup_beispiel_2.1.1-1.bin", 900),
    ]
    entries = [_entry(fid, name, size, i + 1, 2) for i, (fid, name, size) in enumerate(neu)]
    for _, name, size in neu:
        _write(game / name, size)

    # Alte Version: beide Teile liegen noch daneben.
    alt = [("setup_beispiel_2.1.0.pkg", 380), ("setup_beispiel_2.1.0-1.bin", 850)]
    for name, size in alt:
        _write(game / name, size)

    store = _store(entries)
    try:
        config = _config(dest)
        plan = plan_prune(store.entries(), config, on_disk=_on_disk(dest), slugs={PRODUCT_ID: SLUG})

        assert len(plan.prunes) == 2, (
            "sync/ muss beide Altdateien einplanen, sonst bleibt die halbe "
            f"Vorgängerversion liegen. Geplant: {[p.path.name for p in plan.prunes]}"
        )

        results = PruneExecutor(dest, store, mode=PruneMode.DELETE).execute(plan.prunes)

        abgelehnt = [(r.item.path.name, r.reason) for r in results if not r.removed]
        assert not abgelehnt, (
            "prune/ hat die geplanten Löschungen abgelehnt - die beiden Module "
            f"sind sich über den Plan nicht einig: {abgelehnt}"
        )

        for name, _ in alt:
            assert not (game / name).exists(), f"{name} liegt noch da"
        for _, name, _ in neu:
            assert (game / name).exists(), f"{name} wurde fälschlich entfernt"

        assert freed_bytes(results) == sum(size for _, size in alt)
    finally:
        store.close()


def test_unvollstaendiger_ersatz_verhindert_jede_loeschung(tmp_path: Path) -> None:
    """Solange ein Teil der neuen Version fehlt, bleibt die alte komplett."""
    dest = tmp_path / "sammlung"
    game = dest / SLUG

    fertig = _entry("f1", "setup_beispiel_2.1.1.pkg", 400, 1, 2)
    offen = _entry("f2", "setup_beispiel_2.1.1-1.bin", 900, 2, 2)
    offen.state = LocalState.PARTIAL
    offen.last_verified_utc = None
    offen.bytes_done = 100

    _write(game / fertig.filename, 400)
    _write(game / (offen.filename + ".part"), 100)
    alt = [("setup_beispiel_2.1.0.pkg", 380), ("setup_beispiel_2.1.0-1.bin", 850)]
    for name, size in alt:
        _write(game / name, size)

    store = _store([fertig, offen])
    try:
        plan = plan_prune(
            store.entries(), _config(dest), on_disk=_on_disk(dest), slugs={PRODUCT_ID: SLUG}
        )
        results = PruneExecutor(dest, store, mode=PruneMode.DELETE).execute(plan.prunes)

        for name, _ in alt:
            assert (game / name).exists(), (
                f"{name} wurde entfernt, obwohl die neue Version unvollständig ist - "
                "genau der Datenverlust, den die Slot-Regel verhindern soll"
            )
        assert all(not r.removed for r in results)
    finally:
        store.close()


def test_unverifizierter_ersatz_wird_von_prune_abgelehnt(tmp_path: Path) -> None:
    """Zweite Sicherheitsstufe: vollständig genügt nicht, geprüft muss es sein."""
    dest = tmp_path / "sammlung"
    game = dest / SLUG

    entry = _entry("f1", "setup_beispiel_2.1.1.pkg", 400, 1, 1)
    _write(game / entry.filename, 400)
    _write(game / "setup_beispiel_2.1.0.pkg", 380)

    store = _store([entry])
    try:
        plan = plan_prune(
            store.entries(), _config(dest), on_disk=_on_disk(dest), slugs={PRODUCT_ID: SLUG}
        )
        assert plan.prunes, "Voraussetzung des Tests: sync/ plant hier eine Löschung"

        # Erst nach der Planung fällt die Verifikation weg, etwa weil ein
        # späterer verify-Lauf die Datei beanstandet hat.
        entry.last_verified_utc = None
        store.update_entry(entry)

        results = PruneExecutor(dest, store, mode=PruneMode.DELETE).execute(plan.prunes)

        assert (game / "setup_beispiel_2.1.0.pkg").exists()
        assert all(not r.removed for r in results)
        assert any(r.reason for r in results)
    finally:
        store.close()


def test_trash_modus_verschiebt_statt_zu_loeschen(tmp_path: Path) -> None:
    dest = tmp_path / "sammlung"
    game = dest / SLUG

    entry = _entry("f1", "setup_beispiel_2.1.1.pkg", 400, 1, 1)
    _write(game / entry.filename, 400)
    _write(game / "setup_beispiel_2.1.0.pkg", 380)

    store = _store([entry])
    try:
        plan = plan_prune(
            store.entries(),
            _config(dest, prune_mode=PruneMode.TRASH),
            on_disk=_on_disk(dest),
            slugs={PRODUCT_ID: SLUG},
        )
        results = PruneExecutor(dest, store, mode=PruneMode.TRASH).execute(plan.prunes)

        assert all(r.removed for r in results)
        assert not (game / "setup_beispiel_2.1.0.pkg").exists()
        im_papierkorb = list((dest / ".trash").rglob("setup_beispiel_2.1.0.pkg"))
        assert im_papierkorb, "Datei ist weder am Platz noch im Papierkorb"
    finally:
        store.close()


def test_dry_run_veraendert_nichts(tmp_path: Path) -> None:
    dest = tmp_path / "sammlung"
    game = dest / SLUG

    entry = _entry("f1", "setup_beispiel_2.1.1.pkg", 400, 1, 1)
    _write(game / entry.filename, 400)
    _write(game / "setup_beispiel_2.1.0.pkg", 380)

    store = _store([entry])
    try:
        plan = plan_prune(
            store.entries(), _config(dest), on_disk=_on_disk(dest), slugs={PRODUCT_ID: SLUG}
        )
        PruneExecutor(dest, store, mode=PruneMode.DELETE).execute(plan.prunes, dry_run=True)
        assert (game / "setup_beispiel_2.1.0.pkg").exists()
    finally:
        store.close()


@pytest.mark.parametrize("deep", [False, True])
def test_verify_erkennt_falsche_groesse(tmp_path: Path, deep: bool) -> None:
    entry = _entry("f1", "setup_beispiel_2.1.1.pkg", 400, 1, 1)
    path = tmp_path / entry.filename
    _write(path, 399)

    ok, detail = verify_entry(entry, path, deep=deep)
    assert not ok
    assert "399" in detail


def test_verify_ohne_signale_gilt_als_ungeprueft(tmp_path: Path) -> None:
    """Keine Sollwerte bedeutet nicht 'in Ordnung', sondern 'nicht prüfbar'.

    Sonst würde eine Datei ohne Größe und ohne MD5 eine Löschung
    autorisieren, ohne dass je etwas verglichen wurde.
    """
    entry = _entry("f1", "setup_beispiel_2.1.1.pkg", 400, 1, 1)
    entry.size = None
    entry.md5 = None
    path = tmp_path / entry.filename
    _write(path, 400)

    ok, _ = verify_entry(entry, path, deep=True)
    assert not ok
