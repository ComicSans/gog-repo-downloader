"""Tests für das SQLite-Manifest.

Der Schwerpunkt liegt auf der Trennung von Remote-Stand und lokalem
Zustand: ``replace_remote`` darf lokale Felder nie überschreiben, und
Dateien, die GOG entfernt hat, dürfen nicht still verschwinden.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

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
from gogdl.store import SCHEMA_VERSION, SqliteStore

PRODUCT_ID = 4242
SEEN_1 = "2026-08-01T10:00:00Z"
SEEN_2 = "2026-08-02T10:00:00Z"
SEEN_3 = "2026-08-03T10:00:00Z"

INSTALLER_EN = SlotKey(PRODUCT_ID, FileKind.INSTALLER, OsName.WINDOWS, "en")
INSTALLER_DE = SlotKey(PRODUCT_ID, FileKind.INSTALLER, OsName.WINDOWS, "de")
EXTRA_SLOT = SlotKey(PRODUCT_ID, FileKind.EXTRA)
EXTRA_HANDBUCH = SlotKey(PRODUCT_ID, FileKind.EXTRA, variant="handbuch")
EXTRA_SOUNDTRACK = SlotKey(PRODUCT_ID, FileKind.EXTRA, variant="soundtrack")
PATCH_WIN = SlotKey(PRODUCT_ID, FileKind.PATCH, OsName.WINDOWS)


@pytest.fixture
def store() -> Iterator[SqliteStore]:
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
    dlc_of: int | None = None,
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
        dlc_of=dlc_of,
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
    """Die Verifikation galt der alten Version — sie verfällt.

    Und der Zustand mit ihr: was vollständig war, ist gegenüber dem neuen
    Angebot veraltet. Diese Entscheidung fällt hier und nirgends sonst -
    der Vergleich weiter oben in der Kette hat keinen zweiten Stand mehr,
    gegen den er prüfen könnte.
    """
    store.replace_remote(PRODUCT_ID, [remote()], SEEN_1)
    entry = only(store.entries(), "f1")
    entry.state = LocalState.COMPLETE
    entry.bytes_done = 1000
    entry.relative_path = "Spiel/setup.exe"
    entry.last_verified_utc = SEEN_1
    store.update_entry(entry)

    store.replace_remote(PRODUCT_ID, [remote(**changed)], SEEN_2)

    after = only(store.entries(), "f1")
    assert after.last_verified_utc is None
    assert after.state is LocalState.STALE
    assert after.bytes_done == 1000
    assert after.relative_path == "Spiel/setup.exe", "was auf der Platte liegt, bleibt bekannt"
    for field, value in changed.items():
        assert getattr(after, field) == value


@pytest.mark.parametrize(
    "zustand",
    [
        pytest.param(LocalState.MISSING, id="missing"),
        pytest.param(LocalState.PARTIAL, id="partial"),
    ],
)
def test_geaendertes_signal_laesst_unfertige_zustaende_stehen(
    store: SqliteStore, zustand: LocalState
) -> None:
    """``STALE`` meint „vollständig, aber überholt" - sonst wäre es eine Lüge.

    Ein halb geladener Stand ist nicht veraltet, er ist unfertig; er wird
    ohnehin geladen.
    """
    store.replace_remote(PRODUCT_ID, [remote()], SEEN_1)
    entry = only(store.entries(), "f1")
    entry.state = zustand
    entry.bytes_done = 512
    store.update_entry(entry)

    store.replace_remote(PRODUCT_ID, [remote(version="2.0.0")], SEEN_2)

    after = only(store.entries(), "f1")
    assert after.state is zustand
    assert after.bytes_done == 512


def test_unveraenderter_stand_laesst_complete_stehen(store: SqliteStore) -> None:
    """Die Gegenprobe: derselbe Stand darf nichts entwerten."""
    store.replace_remote(PRODUCT_ID, [remote()], SEEN_1)
    entry = only(store.entries(), "f1")
    entry.state = LocalState.COMPLETE
    entry.relative_path = "Spiel/setup.exe"
    entry.last_verified_utc = SEEN_1
    store.update_entry(entry)

    store.replace_remote(PRODUCT_ID, [remote()], SEEN_2)

    after = only(store.entries(), "f1")
    assert after.state is LocalState.COMPLETE
    assert after.last_verified_utc == SEEN_1


def test_stale_bleibt_stale_bis_es_geladen_ist(store: SqliteStore) -> None:
    """Ein zweiter Update-Lauf darf den Befund nicht zurücknehmen."""
    store.replace_remote(PRODUCT_ID, [remote()], SEEN_1)
    entry = only(store.entries(), "f1")
    entry.state = LocalState.COMPLETE
    entry.relative_path = "Spiel/setup.exe"
    entry.last_verified_utc = SEEN_1
    store.update_entry(entry)

    store.replace_remote(PRODUCT_ID, [remote(version="2.0.0")], SEEN_2)
    assert only(store.entries(), "f1").state is LocalState.STALE

    store.replace_remote(PRODUCT_ID, [remote(version="2.0.0")], SEEN_3)

    assert only(store.entries(), "f1").state is LocalState.STALE


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


def _verwaisen_lassen(store: SqliteStore) -> None:
    """Zwei Läufe: ``f2`` ist erst da, dann weg - und damit ``ORPHANED``."""
    store.replace_remote(PRODUCT_ID, [remote(), remote(file_id="f2")], SEEN_1)
    store.replace_remote(PRODUCT_ID, [remote()], SEEN_2)
    assert only(store.entries(), "f2").state is LocalState.ORPHANED


def test_zurueckgekehrte_datei_mit_bestand_wird_wieder_complete(
    store: SqliteStore,
) -> None:
    """Wieder angeboten heißt: nicht mehr verwaist (sonst nie wieder ladbar).

    Der lokale Bestand ist unverändert gültig - Pfad vorhanden, Verifikation
    hat den Abgleich überlebt -, also gilt wieder ``COMPLETE``.
    """
    store.replace_remote(PRODUCT_ID, [remote(), remote(file_id="f2")], SEEN_1)
    entry = only(store.entries(), "f2")
    entry.state = LocalState.COMPLETE
    entry.relative_path = "Spiel/alt.exe"
    entry.bytes_done = 1000
    entry.last_verified_utc = SEEN_1
    store.update_entry(entry)

    store.replace_remote(PRODUCT_ID, [remote()], SEEN_2)
    assert only(store.entries(), "f2").state is LocalState.ORPHANED

    store.replace_remote(PRODUCT_ID, [remote(), remote(file_id="f2")], SEEN_3)

    back = only(store.entries(), "f2")
    assert back.state is not LocalState.ORPHANED
    assert back.state is LocalState.COMPLETE
    assert back.is_verified_complete
    assert back.last_verified_utc == SEEN_1
    assert back.relative_path == "Spiel/alt.exe"
    assert back.last_seen_utc == SEEN_3


def test_zurueckgekehrte_datei_ohne_bestand_wird_missing(store: SqliteStore) -> None:
    """Ohne lokalen Pfad bleibt nur der Neuladen-Zustand."""
    _verwaisen_lassen(store)

    store.replace_remote(PRODUCT_ID, [remote(), remote(file_id="f2")], SEEN_3)

    back = only(store.entries(), "f2")
    assert back.state is not LocalState.ORPHANED
    assert back.state is LocalState.MISSING
    assert back.relative_path == ""
    assert back.last_seen_utc == SEEN_3


def test_zurueckgekehrte_datei_ohne_verifikation_wird_missing(
    store: SqliteStore,
) -> None:
    """Pfad allein genügt nicht: unverifiziert ist der Bestand kein Beleg."""
    store.replace_remote(PRODUCT_ID, [remote(), remote(file_id="f2")], SEEN_1)
    entry = only(store.entries(), "f2")
    entry.state = LocalState.COMPLETE
    entry.relative_path = "Spiel/alt.exe"
    store.update_entry(entry)  # last_verified_utc bleibt None

    store.replace_remote(PRODUCT_ID, [remote()], SEEN_2)
    store.replace_remote(PRODUCT_ID, [remote(), remote(file_id="f2")], SEEN_3)

    back = only(store.entries(), "f2")
    assert back.state is LocalState.MISSING
    assert back.relative_path == "Spiel/alt.exe", "der Fund auf der Platte bleibt bekannt"


def test_zurueckgekehrte_datei_mit_neuer_version_wird_missing(
    store: SqliteStore,
) -> None:
    """GOG stellt sie neu ein: die alte Verifikation zählt nicht mehr."""
    store.replace_remote(PRODUCT_ID, [remote(), remote(file_id="f2")], SEEN_1)
    entry = only(store.entries(), "f2")
    entry.state = LocalState.COMPLETE
    entry.relative_path = "Spiel/alt.exe"
    entry.last_verified_utc = SEEN_1
    store.update_entry(entry)

    store.replace_remote(PRODUCT_ID, [remote()], SEEN_2)
    store.replace_remote(
        PRODUCT_ID, [remote(), remote(file_id="f2", version="2.0.0")], SEEN_3
    )

    back = only(store.entries(), "f2")
    assert back.last_verified_utc is None
    assert back.state is LocalState.MISSING
    assert back.version == "2.0.0"


def test_rueckkehr_faesst_nicht_verwaiste_zustaende_nicht_an(store: SqliteStore) -> None:
    """Der Rückweg gilt nur für ``ORPHANED``; alles andere bleibt stehen."""
    store.replace_remote(PRODUCT_ID, [remote(), remote(file_id="f2")], SEEN_1)
    entry = only(store.entries(), "f2")
    entry.state = LocalState.PARTIAL
    entry.bytes_done = 512
    entry.relative_path = "Spiel/alt.exe"
    store.update_entry(entry)

    store.replace_remote(PRODUCT_ID, [remote(), remote(file_id="f2")], SEEN_2)

    after = only(store.entries(), "f2")
    assert after.state is LocalState.PARTIAL
    assert after.bytes_done == 512


def test_zwei_extras_mit_variant_kollidieren_nicht(store: SqliteStore) -> None:
    """Handbuch und Soundtrack sind getrennte Slots - sonst prunt eins das andere."""
    files = [
        remote(EXTRA_HANDBUCH, "same", version=None, filename="handbuch.pdf"),
        remote(EXTRA_SOUNDTRACK, "same", version=None, filename="ost.zip"),
    ]
    store.replace_remote(PRODUCT_ID, files, SEEN_1)

    assert len(store.entries(PRODUCT_ID)) == 2

    handbuch = store.entries_for_slot(EXTRA_HANDBUCH)
    soundtrack = store.entries_for_slot(EXTRA_SOUNDTRACK)
    assert len(handbuch) == 1 and len(soundtrack) == 1
    assert handbuch[0].slot == EXTRA_HANDBUCH
    assert handbuch[0].slot.variant == "handbuch"
    assert handbuch[0].filename == "handbuch.pdf"
    assert soundtrack[0].slot == EXTRA_SOUNDTRACK
    assert soundtrack[0].slot.variant == "soundtrack"
    assert soundtrack[0].filename == "ost.zip"

    # Der variantenlose Slot ist ein dritter, eigener Schlüssel.
    assert store.entries_for_slot(EXTRA_SLOT) == []

    # ``variant`` überlebt auch den vollen Schreibweg.
    entry = handbuch[0]
    entry.state = LocalState.COMPLETE
    entry.relative_path = "Spiel/extras/handbuch.pdf"
    store.update_entry(entry)

    assert len(store.entries(PRODUCT_ID)) == 2, "update_entry darf nicht doppeln"
    wieder = store.entries_for_slot(EXTRA_HANDBUCH)
    assert len(wieder) == 1
    assert wieder[0].slot.variant == "handbuch"
    assert wieder[0].state is LocalState.COMPLETE


def test_dlc_of_ueberlebt_roundtrip_und_update(store: SqliteStore) -> None:
    files = [
        remote(dlc_of=99),
        remote(EXTRA_SLOT, "x1", version=None, dlc_of=None),
    ]
    store.replace_remote(PRODUCT_ID, files, SEEN_1)

    dlc = only(store.entries(), "f1")
    assert dlc.dlc_of == 99
    assert only(store.entries(), "x1").dlc_of is None

    dlc.state = LocalState.COMPLETE
    dlc.relative_path = "Spiel/dlc.exe"
    store.update_entry(dlc)
    assert only(store.entries(), "f1").dlc_of == 99

    # Auch ein weiterer Remote-Lauf hält die Zuordnung.
    store.replace_remote(PRODUCT_ID, files, SEEN_2)
    assert only(store.entries(), "f1").dlc_of == 99

    # Und eine aufgelöste Zuordnung wird übernommen, nicht konserviert.
    store.replace_remote(PRODUCT_ID, [remote(dlc_of=None)], SEEN_3)
    assert only(store.entries(), "f1").dlc_of is None


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


def test_leerer_remote_stand_orphaned_alles(store: SqliteStore) -> None:
    """Der Grenzfall: GOG liefert nichts mehr — nichts darf verschwinden."""
    store.replace_remote(PRODUCT_ID, [remote(), remote(file_id="f2")], SEEN_1)

    store.replace_remote(PRODUCT_ID, [], SEEN_2)

    entries = store.entries(PRODUCT_ID)
    assert {e.file_id for e in entries} == {"f1", "f2"}
    assert all(e.state is LocalState.ORPHANED for e in entries)
    assert all(e.last_seen_utc == SEEN_1 for e in entries)


def test_pragmas_und_schemaversion(tmp_path) -> None:
    """WAL, foreign_keys und user_version sind Vorgaben aus §4.4."""
    store = SqliteStore(tmp_path / "manifest.sqlite3")
    try:
        conn = store._conn
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        store.close()

    # Erneutes Öffnen migriert nicht noch einmal und lässt die Version stehen.
    again = SqliteStore(tmp_path / "manifest.sqlite3")
    try:
        assert again._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        again.close()


SCHEMA_V1_ALT = """
CREATE TABLE products (
    product_id  INTEGER PRIMARY KEY,
    title       TEXT    NOT NULL,
    slug        TEXT    NOT NULL,
    has_updates INTEGER NOT NULL DEFAULT 0,
    is_new      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE files (
    slot_key          TEXT    NOT NULL,
    file_id           TEXT    NOT NULL,
    product_id        INTEGER NOT NULL,
    kind              TEXT    NOT NULL,
    os                TEXT,
    language          TEXT,
    filename          TEXT    NOT NULL DEFAULT '',
    version           TEXT,
    size              INTEGER,
    md5               TEXT,
    downlink          TEXT    NOT NULL DEFAULT '',
    part_index        INTEGER NOT NULL DEFAULT 1,
    total_parts       INTEGER NOT NULL DEFAULT 1,
    relative_path     TEXT    NOT NULL DEFAULT '',
    state             TEXT    NOT NULL DEFAULT 'missing',
    bytes_done        INTEGER NOT NULL DEFAULT 0,
    last_seen_utc     TEXT,
    last_verified_utc TEXT,
    PRIMARY KEY (slot_key, file_id)
);

CREATE INDEX idx_files_product ON files(product_id);
CREATE INDEX idx_files_state   ON files(state);
"""
"""Schema 1 wörtlich: ohne ``variant``, ohne ``dlc_of``."""

# Schlüssel im alten Format: ohne Platzhalter für fehlende os/language.
ALTE_ZEILEN = [
    (
        "4242/installer/windows/en", "f1", 4242, "installer", "windows", "en",
        "setup.exe", "1.0.0", 1000, "aaaa", "/downlink/f1", 1, 1,
        "Spiel/setup.exe", "complete", 1000, SEEN_1, SEEN_1,
    ),
    (
        "4242/extra", "x1", 4242, "extra", None, None,
        "handbuch.pdf", None, 50, None, "/downlink/x1", 1, 1,
        "", "missing", 0, SEEN_1, None,
    ),
    (
        "4242/patch/windows", "p1", 4242, "patch", "windows", None,
        "patch.exe", "1.0.1", 20, None, "/downlink/p1", 1, 1,
        "Spiel/patch.exe.part", "partial", 5, SEEN_1, None,
    ),
]


def _lege_alte_datenbank_an(db_path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SCHEMA_V1_ALT)
        conn.executemany(
            "INSERT INTO files (slot_key, file_id, product_id, kind, os, language, "
            "filename, version, size, md5, downlink, part_index, total_parts, "
            "relative_path, state, bytes_done, last_seen_utc, last_verified_utc) "
            "VALUES (" + ", ".join("?" * 18) + ")",
            ALTE_ZEILEN,
        )
        conn.execute(
            "INSERT INTO products (product_id, title, slug, has_updates, is_new) "
            "VALUES (?, ?, ?, ?, ?)",
            (PRODUCT_ID, "Spiel", "spiel", 1, 0),
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


def test_migration_von_schema_1_auf_2(tmp_path) -> None:
    """Bestand bleibt, Spalten kommen dazu, ``slot_key`` wird neu berechnet."""
    db_path = tmp_path / "alt.sqlite3"
    _lege_alte_datenbank_an(db_path)

    store = SqliteStore(db_path)
    try:
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

        entries = store.entries()
        assert {e.file_id for e in entries} == {"f1", "x1", "p1"}
        assert store.products() == [
            ProductRef(PRODUCT_ID, "Spiel", "spiel", has_updates=True)
        ]

        # Kein Feld ist unterwegs verloren gegangen.
        installer = only(entries, "f1")
        assert installer.slot == INSTALLER_EN
        assert installer.state is LocalState.COMPLETE
        assert installer.relative_path == "Spiel/setup.exe"
        assert installer.bytes_done == 1000
        assert installer.md5 == "aaaa"
        assert installer.last_verified_utc == SEEN_1
        assert installer.is_verified_complete

        patch = only(entries, "p1")
        assert patch.slot == PATCH_WIN
        assert patch.state is LocalState.PARTIAL
        assert patch.bytes_done == 5

        # Die neuen Spalten existieren und sind für Altbestand leer.
        assert all(e.dlc_of is None for e in entries)
        assert all(e.slot.variant is None for e in entries)

        # Jeder gespeicherte Schlüssel entspricht wieder ``as_str()`` ...
        gespeichert = {
            row["file_id"]: row["slot_key"]
            for row in store._conn.execute("SELECT file_id, slot_key FROM files")
        }
        assert gespeichert == {e.file_id: e.slot.as_str() for e in entries}
        # ... und für die Zeilen ohne os/language ist das ein anderer als vorher.
        assert gespeichert["x1"] != "4242/extra"
        assert gespeichert["p1"] != "4242/patch/windows"
        assert gespeichert["f1"] == "4242/installer/windows/en"

        # Entscheidend: der Slot findet seine Dateien wieder.
        for entry in entries:
            assert [e.file_id for e in store.entries_for_slot(entry.slot)] == [
                entry.file_id
            ]

        # Und der migrierte Schlüssel trägt durch den UPSERT, ohne zu doppeln.
        patch.state = LocalState.COMPLETE
        patch.relative_path = "Spiel/patch.exe"
        store.update_entry(patch)
        assert len(store.entries()) == 3
        assert only(store.entries(), "p1").state is LocalState.COMPLETE

        store.replace_remote(
            PRODUCT_ID, [remote(EXTRA_SLOT, "x1", version=None, md5=None)], SEEN_2
        )
        assert len(store.entries()) == 3, "replace_remote trifft die migrierte Zeile"
        assert only(store.entries(), "x1").last_seen_utc == SEEN_2
    finally:
        store.close()

    # Ein zweites Öffnen migriert nicht noch einmal.
    wieder = SqliteStore(db_path)
    try:
        assert wieder._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert {e.file_id for e in wieder.entries()} == {"f1", "x1", "p1"}
    finally:
        wieder.close()


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


# ---------------------------------------------------------------------------
# Bündelung mehrerer Schreibzugriffe zu einer Transaktion
# ---------------------------------------------------------------------------


def eintrag(file_id: str) -> ManifestEntry:
    """Ein vollständiger Eintrag, je ``file_id`` in einem eigenen Slot."""
    return ManifestEntry(
        slot=SlotKey(PRODUCT_ID, FileKind.EXTRA, variant=file_id),
        file_id=file_id,
        filename=f"{file_id}.bin",
        version="1.0.0",
        size=100,
        md5="aaaa",
        downlink=f"/downlink/{file_id}",
        relative_path=f"Spiel/{file_id}.bin",
        state=LocalState.COMPLETE,
        bytes_done=100,
        last_seen_utc=SEEN_1,
        last_verified_utc=SEEN_1,
    )


def zaehle_transaktionen(store: SqliteStore) -> list[str]:
    """Hängt einen Mitschnitt an die Verbindung und gibt die Liste zurück.

    ``sqlite3.Connection`` bietet keinen Commit-Hook an; ``set_trace_callback``
    sieht dafür jede abgesetzte Anweisung und damit ``BEGIN``/``COMMIT``
    unmittelbar. Das zählt die Transaktionen, statt sie zu behaupten.
    """
    mitschnitt: list[str] = []

    def aufzeichnen(anweisung: str) -> None:
        text = anweisung.strip().upper()
        if text.startswith(("BEGIN", "COMMIT", "ROLLBACK")):
            mitschnitt.append(text.split()[0])

    store._conn.set_trace_callback(aufzeichnen)
    return mitschnitt


def unschreibbar(file_id: str) -> ManifestEntry:
    """Ein Eintrag, an dem SQLite scheitern muss: ``filename`` ist NOT NULL."""
    kaputt = eintrag(file_id)
    kaputt.filename = None  # type: ignore[assignment]
    return kaputt


def test_gebuendelt_geschrieben_ergibt_denselben_zustand(tmp_path) -> None:
    """Einzeln und gebündelt müssen zum selben Manifest führen."""
    eintraege = [eintrag(f"f{i}") for i in range(10)]

    einzeln = SqliteStore(tmp_path / "einzeln.sqlite3")
    gebuendelt = SqliteStore(tmp_path / "gebuendelt.sqlite3")
    try:
        for item in eintraege:
            einzeln.update_entry(item)
        gebuendelt.update_entries(eintraege)

        assert gebuendelt.entries() == einzeln.entries()
        assert len(gebuendelt.entries()) == 10
    finally:
        einzeln.close()
        gebuendelt.close()


def test_buendelung_setzt_genau_eine_transaktion_ab(store: SqliteStore) -> None:
    """Der eigentliche Nachweis: zehn Einträge, ein BEGIN, ein COMMIT."""
    eintraege = [eintrag(f"f{i}") for i in range(10)]

    mitschnitt = zaehle_transaktionen(store)
    store.update_entries(eintraege)
    assert mitschnitt == ["BEGIN", "COMMIT"]

    # Zum Vergleich derselbe Schreibvorgang ohne Bündelung.
    mitschnitt.clear()
    for item in eintraege:
        store.update_entry(item)
    assert mitschnitt.count("BEGIN") == 10
    assert mitschnitt.count("COMMIT") == 10


def test_leere_buendelung_faesst_die_datenbank_nicht_an(store: SqliteStore) -> None:
    mitschnitt = zaehle_transaktionen(store)
    store.update_entries([])
    assert mitschnitt == []


def test_verschachtelte_transaktion_committet_nur_aussen(store: SqliteStore) -> None:
    mitschnitt = zaehle_transaktionen(store)
    with store.transaction():
        store.update_entry(eintrag("f1"))
        with store.transaction():
            store.update_entry(eintrag("f2"))
        store.update_entry(eintrag("f3"))

    assert mitschnitt == ["BEGIN", "COMMIT"]
    assert {e.file_id for e in store.entries()} == {"f1", "f2", "f3"}


def test_fehler_in_der_buendelung_laesst_keinen_teil_zurueck(store: SqliteStore) -> None:
    """``filename`` ist NOT NULL - der dritte Eintrag scheitert in SQLite."""
    eintraege = [eintrag("f1"), eintrag("f2"), unschreibbar("f3"), eintrag("f4")]

    mitschnitt = zaehle_transaktionen(store)
    with pytest.raises(StoreError):
        store.update_entries(eintraege)

    assert store.entries() == [], "auch der bereits geschriebene Teil muss weg sein"
    assert mitschnitt == ["BEGIN", "ROLLBACK"]


def test_ausnahme_des_aufrufers_rollt_zurueck_und_bleibt_sie_selbst(
    store: SqliteStore,
) -> None:
    """Ein Fehler des Aufrufers wird nicht als ``StoreError`` verkleidet."""
    with pytest.raises(ValueError):
        with store.transaction():
            store.update_entry(eintrag("f1"))
            raise ValueError("Abbruch mitten drin")

    assert store.entries() == []


def test_transaktionszaehler_bleibt_nach_einem_fehler_nicht_haengen(
    store: SqliteStore,
) -> None:
    """Ein hängender Zähler würde jeden späteren Commit still verschlucken."""
    with pytest.raises(StoreError):
        store.update_entries([unschreibbar("f1")])

    store.update_entry(eintrag("f2"))
    assert [e.file_id for e in store.entries()] == ["f2"]


def test_wirksamer_journalmodus_ist_auslesbar(tmp_path) -> None:
    """Auf einer lokalen Platte greift WAL; entscheidend ist der Rückgabewert."""
    store = SqliteStore(tmp_path / "manifest.sqlite3")
    try:
        assert store.journal_mode == "wal"
        # Der festgehaltene Wert ist der, den SQLite selbst meldet.
        gemeldet = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert store.journal_mode == gemeldet.lower()
        assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    finally:
        store.close()


def test_speicherdatenbank_meldet_ihren_eigenen_modus(store: SqliteStore) -> None:
    """``:memory:`` kann kein WAL - der Store behauptet es deshalb auch nicht."""
    assert store.journal_mode == "memory"
    assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
