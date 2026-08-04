"""Tests für das SQLite-Manifest.

Der Schwerpunkt liegt auf der Trennung von Remote-Stand und lokalem
Zustand: ``replace_remote`` darf lokale Felder nie überschreiben, und
Dateien, die GOG entfernt hat, dürfen nicht still verschwinden.
"""

from __future__ import annotations

import pytest

from gogdl.errors import StoreError
from gogdl.model.protocols import Store
from gogdl.model.types import (
    FileKind,
    LocalState,
    ManifestEntry,
    OsName,
    ProductRef,
    RemoteFile,
    SlotKey,
)
from gogdl.store import SqliteStore

PRODUCT_ID = 4242
SEEN_1 = "2026-08-01T10:00:00Z"
SEEN_2 = "2026-08-02T10:00:00Z"

INSTALLER_EN = SlotKey(PRODUCT_ID, FileKind.INSTALLER, OsName.WINDOWS, "en")
INSTALLER_DE = SlotKey(PRODUCT_ID, FileKind.INSTALLER, OsName.WINDOWS, "de")
EXTRA_SLOT = SlotKey(PRODUCT_ID, FileKind.EXTRA)


@pytest.fixture
def store() -> SqliteStore:
    s = SqliteStore(":memory:")
    yield s
    s.close()


def remote(
    slot: SlotKey = INSTALLER_EN,
    file_id: str = "f1",
    *,
    version: str | None = "1.0.0",
    size: int | None = 1000,
    md5: str | None = "aaaa",
    part_index: int = 1,
    total_parts: int = 1,
    filename: str | None = "setup.exe",
) -> RemoteFile:
    return RemoteFile(
        slot=slot,
        file_id=file_id,
        downlink=f"/downlink/{file_id}",
        filename=filename,
        size=size,
        md5=md5,
        version=version,
        part_index=part_index,
        total_parts=total_parts,
    )


def only(entries: list[ManifestEntry], file_id: str) -> ManifestEntry:
    matching = [e for e in entries if e.file_id == file_id]
    assert len(matching) == 1, f"{file_id} nicht eindeutig: {entries}"
    return matching[0]


def test_erfuellt_store_protocol(store: SqliteStore) -> None:
    assert isinstance(store, Store)


def test_roundtrip_erhaelt_alle_felder(store: SqliteStore) -> None:
    """Enums, ``None``-Werte und Zahlen kommen unverändert zurück."""
    files = [
        remote(),
        remote(EXTRA_SLOT, "x1", version=None, size=None, md5=None, filename=None),
    ]
    store.replace_remote(PRODUCT_ID, files, SEEN_1)

    entries = store.entries(PRODUCT_ID)
    assert len(entries) == 2

    installer = only(entries, "f1")
    assert installer.slot == INSTALLER_EN
    assert installer.slot.kind is FileKind.INSTALLER
    assert installer.slot.os is OsName.WINDOWS
    assert installer.product_id == PRODUCT_ID
    assert installer.filename == "setup.exe"
    assert installer.version == "1.0.0"
    assert installer.size == 1000
    assert installer.md5 == "aaaa"
    assert installer.downlink == "/downlink/f1"
    assert installer.part_index == 1
    assert installer.total_parts == 1
    assert installer.relative_path == ""
    assert installer.state is LocalState.MISSING
    assert installer.bytes_done == 0
    assert installer.last_seen_utc == SEEN_1
    assert installer.last_verified_utc is None

    extra = only(entries, "x1")
    assert extra.slot == EXTRA_SLOT
    assert extra.slot.os is None
    assert extra.slot.language is None
    assert extra.version is None
    assert extra.size is None
    assert extra.md5 is None
    assert extra.filename == ""

    # Ohne Produktfilter dieselben Einträge.
    assert store.entries() == entries


def test_lokaler_zustand_ueberlebt_zweiten_remote_lauf(store: SqliteStore) -> None:
    store.replace_remote(PRODUCT_ID, [remote()], SEEN_1)

    entry = only(store.entries(), "f1")
    entry.state = LocalState.COMPLETE
    entry.bytes_done = 1000
    entry.relative_path = "Spiel/setup.exe"
    entry.last_verified_utc = SEEN_1
    store.update_entry(entry)

    store.replace_remote(PRODUCT_ID, [remote()], SEEN_2)

    after = only(store.entries(), "f1")
    assert after.state is LocalState.COMPLETE
    assert after.bytes_done == 1000
    assert after.relative_path == "Spiel/setup.exe"
    assert after.last_verified_utc == SEEN_1
    assert after.last_seen_utc == SEEN_2


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param({"version": "1.0.1"}, id="version"),
        pytest.param({"size": 2000}, id="size"),
        pytest.param({"md5": "bbbb"}, id="md5"),
    ],
)
def test_geaendertes_signal_verwirft_verifikation(
    store: SqliteStore, changed: dict[str, object]
) -> None:
    """Die Verifikation galt der alten Version — sie verfällt."""
    store.replace_remote(PRODUCT_ID, [remote()], SEEN_1)
    entry = only(store.entries(), "f1")
    entry.state = LocalState.COMPLETE
    entry.bytes_done = 1000
    entry.last_verified_utc = SEEN_1
    store.update_entry(entry)

    store.replace_remote(PRODUCT_ID, [remote(**changed)], SEEN_2)

    after = only(store.entries(), "f1")
    assert after.last_verified_utc is None
    assert after.state is LocalState.COMPLETE
    assert after.bytes_done == 1000
    for field, value in changed.items():
        assert getattr(after, field) == value


def test_fehlender_remote_md5_loescht_gespeicherten_nicht(store: SqliteStore) -> None:
    """``RemoteFile.md5`` ist ``None``, solange kein Checksum-XML geholt wurde.

    Ein Überschreiben mit ``None`` würde Daten vernichten, die nur
    ``update_entry`` liefern kann — und die Verifikation grundlos verwerfen.
    """
    store.replace_remote(PRODUCT_ID, [remote(md5=None)], SEEN_1)
    entry = only(store.entries(), "f1")
    entry.md5 = "aaaa"
    entry.state = LocalState.COMPLETE
    entry.last_verified_utc = SEEN_1
    store.update_entry(entry)

    store.replace_remote(PRODUCT_ID, [remote(md5=None)], SEEN_2)

    after = only(store.entries(), "f1")
    assert after.md5 == "aaaa"
    assert after.last_verified_utc == SEEN_1


def test_entfernte_datei_wird_orphaned_und_bleibt(store: SqliteStore) -> None:
    store.replace_remote(PRODUCT_ID, [remote(), remote(file_id="f2")], SEEN_1)
    entry = only(store.entries(), "f2")
    entry.state = LocalState.COMPLETE
    entry.relative_path = "Spiel/alt.exe"
    store.update_entry(entry)

    store.replace_remote(PRODUCT_ID, [remote()], SEEN_2)

    entries = store.entries(PRODUCT_ID)
    assert {e.file_id for e in entries} == {"f1", "f2"}

    gone = only(entries, "f2")
    assert gone.state is LocalState.ORPHANED
    assert gone.last_seen_utc == SEEN_1, "altes last_seen_utc muss erhalten bleiben"
    assert gone.relative_path == "Spiel/alt.exe"

    assert only(entries, "f1").last_seen_utc == SEEN_2


def test_zwei_sprachslots_kollidieren_nicht(store: SqliteStore) -> None:
    """Gleiche file_id, anderer Slot — beides muss nebeneinander bestehen."""
    files = [
        remote(INSTALLER_EN, "same", version="1.0.0"),
        remote(INSTALLER_DE, "same", version="1.0.1"),
    ]
    store.replace_remote(PRODUCT_ID, files, SEEN_1)

    assert len(store.entries(PRODUCT_ID)) == 2

    en = store.entries_for_slot(INSTALLER_EN)
    de = store.entries_for_slot(INSTALLER_DE)
    assert len(en) == 1 and len(de) == 1
    assert en[0].version == "1.0.0"
    assert de[0].version == "1.0.1"
    assert en[0].slot.language == "en"
    assert de[0].slot.language == "de"


def test_extra_ohne_os_und_sprache_ist_aktualisierbar(store: SqliteStore) -> None:
    """Der NULL-Schlüssel-Fall: ohne ``slot_key`` würde UPSERT hier doppeln."""
    store.replace_remote(PRODUCT_ID, [remote(EXTRA_SLOT, "x1", version=None)], SEEN_1)

    entry = only(store.entries(), "x1")
    entry.state = LocalState.PARTIAL
    entry.bytes_done = 512
    entry.relative_path = "Spiel/extras/handbuch.pdf"
    store.update_entry(entry)

    entries = store.entries(PRODUCT_ID)
    assert len(entries) == 1, "update_entry darf keinen zweiten Eintrag anlegen"
    updated = entries[0]
    assert updated.state is LocalState.PARTIAL
    assert updated.bytes_done == 512
    assert updated.relative_path == "Spiel/extras/handbuch.pdf"
    assert updated.slot.os is None and updated.slot.language is None

    # Auch ein weiterer Remote-Lauf legt keinen zweiten Eintrag an.
    store.replace_remote(PRODUCT_ID, [remote(EXTRA_SLOT, "x1", version=None)], SEEN_2)
    assert len(store.entries(PRODUCT_ID)) == 1

    store.remove_entry(EXTRA_SLOT, "x1")
    assert store.entries(PRODUCT_ID) == []


def test_entries_for_slot_liefert_teile_nach_part_index(store: SqliteStore) -> None:
    files = [
        remote(file_id="p3", part_index=3, total_parts=3),
        remote(file_id="p1", part_index=1, total_parts=3),
        remote(file_id="p2", part_index=2, total_parts=3),
        remote(EXTRA_SLOT, "x1", version=None),
    ]
    store.replace_remote(PRODUCT_ID, files, SEEN_1)

    parts = store.entries_for_slot(INSTALLER_EN)
    assert [p.part_index for p in parts] == [1, 2, 3]
    assert [p.file_id for p in parts] == ["p1", "p2", "p3"]
    assert all(p.total_parts == 3 for p in parts)


def test_produktliste_wird_vollstaendig_ersetzt(store: SqliteStore) -> None:
    assert store.products() == []

    store.replace_remote(PRODUCT_ID, [remote()], SEEN_1)
    store.replace_products(
        [
            ProductRef(PRODUCT_ID, "Spiel", "spiel", has_updates=True),
            ProductRef(7, "Anderes", "anderes", is_new=True),
        ]
    )
    assert store.products() == [
        ProductRef(7, "Anderes", "anderes", is_new=True),
        ProductRef(PRODUCT_ID, "Spiel", "spiel", has_updates=True),
    ]

    store.replace_products([ProductRef(7, "Anderes", "anderes")])
    assert store.products() == [ProductRef(7, "Anderes", "anderes")]
    assert len(store.entries()) == 1, "Dateien hängen nicht an der Produktliste"


def test_datei_db_ueberlebt_schliessen_und_oeffnen(tmp_path) -> None:
    db_path = tmp_path / "neu" / "tief" / "manifest.sqlite3"

    store = SqliteStore(db_path)
    store.replace_remote(
        PRODUCT_ID,
        [remote(), remote(EXTRA_SLOT, "x1", version=None, md5=None)],
        SEEN_1,
    )
    entry = only(store.entries(), "f1")
    entry.state = LocalState.COMPLETE
    entry.bytes_done = 1000
    entry.relative_path = "Spiel/setup.exe"
    entry.last_verified_utc = SEEN_1
    store.update_entry(entry)
    store.replace_products([ProductRef(PRODUCT_ID, "Spiel", "spiel")])
    before = store.entries()
    store.close()
    store.close()  # idempotent

    assert db_path.exists()

    reopened = SqliteStore(db_path)
    try:
        assert reopened.entries() == before
        assert reopened.products() == [ProductRef(PRODUCT_ID, "Spiel", "spiel")]
        restored = only(reopened.entries(), "f1")
        assert restored.state is LocalState.COMPLETE
        assert restored.is_verified_complete
    finally:
        reopened.close()


def test_zugriff_nach_close_meldet_storeerror(tmp_path) -> None:
    store = SqliteStore(tmp_path / "manifest.sqlite3")
    store.close()
    with pytest.raises(StoreError):
        store.entries()
    with pytest.raises(StoreError):
        store.replace_remote(PRODUCT_ID, [remote()], SEEN_1)


def test_unschreibbarer_pfad_meldet_storeerror(tmp_path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("kein Verzeichnis")
    with pytest.raises(StoreError):
        SqliteStore(blocker / "unter" / "manifest.sqlite3")


def test_fremdes_produkt_in_replace_remote_wird_abgelehnt(store: SqliteStore) -> None:
    fremd = remote(SlotKey(999, FileKind.INSTALLER, OsName.LINUX, "en"), "f9")
    with pytest.raises(StoreError):
        store.replace_remote(PRODUCT_ID, [remote(), fremd], SEEN_1)
    assert store.entries() == [], "abgebrochene Transaktion darf nichts hinterlassen"
