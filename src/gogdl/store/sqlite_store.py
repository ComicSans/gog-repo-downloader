"""SQLite-Manifest.

Implementiert ``model.protocols.Store``. Die Datenbank ist die einzige
Stelle, an der der Remote-Stand und der lokale Zustand zusammenkommen -
und sie hält beide bewusst getrennt: ``replace_remote`` schreibt nur, was
GOG sagt, und fasst ``bytes_done``/``relative_path`` nicht an
(KONZEPT.md §4.3, §4.4). ``state`` und ``last_verified_utc`` folgen dem
Remote-Stand nur dort, wo er sie widerlegt: eine verschwundene Datei wird
``ORPHANED``, eine zurückgekehrte verlässt diesen Zustand wieder, und eine
geänderte Auslieferung entwertet die alte Verifikation und macht einen
vollständigen Bestand ``STALE``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from ..errors import StoreError
from ..model.types import (
    FileKind,
    LocalState,
    ManifestEntry,
    OsName,
    ProductRef,
    RemoteFile,
    SlotKey,
)

SCHEMA_VERSION = 2
"""Aktuelle Schemaversion, abgelegt im ``user_version``-Pragma."""

MEMORY_PATH = ":memory:"

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS products (
    product_id  INTEGER PRIMARY KEY,
    title       TEXT    NOT NULL,
    slug        TEXT    NOT NULL,
    has_updates INTEGER NOT NULL DEFAULT 0,
    is_new      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
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

CREATE INDEX IF NOT EXISTS idx_files_product ON files(product_id);
CREATE INDEX IF NOT EXISTS idx_files_state   ON files(state);
"""
"""Ausgangsschema. Bleibt unverändert - spätere Stände entstehen daraus
ausschließlich über die Migrationsschritte, damit eine neue und eine
hochgezogene Datenbank denselben Weg nehmen."""

_ENTRY_COLUMNS = """
    slot_key, file_id, product_id, kind, os, language, variant,
    filename, version, size, md5, downlink,
    part_index, total_parts, dlc_of,
    relative_path, state, bytes_done, last_seen_utc, last_verified_utc
"""

_SIGNALS_CHANGED = """(
        files.version IS NOT excluded.version
     OR files.size    IS NOT excluded.size
     OR (excluded.md5 IS NOT NULL AND files.md5 IS NOT excluded.md5)
    )"""
"""Liefert GOG eine andere Auslieferung als die gespeicherte?

Verglichen werden die drei Aktualitätssignale aus KONZEPT.md §4.2. Ein
fehlender ``md5`` im Remote-Stand ist dabei keine Abweichung: er bedeutet
nur, dass das Checksum-XML nicht abgerufen wurde.

**Hier und nur hier fällt die Aktualitätsentscheidung.** Weiter oben in
der Kette gibt es keinen zweiten Stand mehr, gegen den sich vergleichen
ließe - der Manifest-Eintrag *ist* dort bereits der neue Stand. Der
UPSERT ist der einzige Moment, in dem alter und neuer Wert nebeneinander
liegen (``files.*`` gegen ``excluded.*``).
"""

_KEEP_VERIFIED = f"""CASE
        WHEN {_SIGNALS_CHANGED}
        THEN NULL
        ELSE files.last_verified_utc
    END"""
"""Behält ``last_verified_utc`` nur, solange die Auslieferung dieselbe ist.

Wird zweimal eingesetzt: für die Spalte selbst und für die Entscheidung,
in welchen Zustand eine zurückgekehrte Datei fällt. SQLite wertet alle
``SET``-Ausdrücke gegen die Zeile *vor* dem Update aus, beide Kopien
sehen also dieselben Werte.
"""

_INSERT_REMOTE = f"""
INSERT INTO files ({_ENTRY_COLUMNS})
VALUES (
    :slot_key, :file_id, :product_id, :kind, :os, :language, :variant,
    :filename, :version, :size, :md5, :downlink,
    :part_index, :total_parts, :dlc_of,
    '', '{LocalState.MISSING.value}', 0, :last_seen_utc, NULL
)
ON CONFLICT(slot_key, file_id) DO UPDATE SET
    filename      = excluded.filename,
    downlink      = excluded.downlink,
    version       = excluded.version,
    size          = excluded.size,
    md5           = COALESCE(excluded.md5, files.md5),
    part_index    = excluded.part_index,
    total_parts   = excluded.total_parts,
    dlc_of        = excluded.dlc_of,
    last_seen_utc = excluded.last_seen_utc,
    state = CASE
        WHEN files.state IS '{LocalState.COMPLETE.value}' AND {_SIGNALS_CHANGED}
            THEN '{LocalState.STALE.value}'
        WHEN files.state IS NOT '{LocalState.ORPHANED.value}' THEN files.state
        WHEN files.relative_path <> '' AND ({_KEEP_VERIFIED}) IS NOT NULL
            THEN '{LocalState.COMPLETE.value}'
        ELSE '{LocalState.MISSING.value}'
    END,
    last_verified_utc = {_KEEP_VERIFIED}
"""

_UPSERT_ENTRY = f"""
INSERT INTO files ({_ENTRY_COLUMNS})
VALUES (
    :slot_key, :file_id, :product_id, :kind, :os, :language, :variant,
    :filename, :version, :size, :md5, :downlink,
    :part_index, :total_parts, :dlc_of,
    :relative_path, :state, :bytes_done, :last_seen_utc, :last_verified_utc
)
ON CONFLICT(slot_key, file_id) DO UPDATE SET
    product_id        = excluded.product_id,
    kind              = excluded.kind,
    os                = excluded.os,
    language          = excluded.language,
    variant           = excluded.variant,
    dlc_of            = excluded.dlc_of,
    filename          = excluded.filename,
    version           = excluded.version,
    size              = excluded.size,
    md5               = excluded.md5,
    downlink          = excluded.downlink,
    part_index        = excluded.part_index,
    total_parts       = excluded.total_parts,
    relative_path     = excluded.relative_path,
    state             = excluded.state,
    bytes_done        = excluded.bytes_done,
    last_seen_utc     = excluded.last_seen_utc,
    last_verified_utc = excluded.last_verified_utc
"""

_SELECT_ENTRY = f"SELECT {_ENTRY_COLUMNS} FROM files"

_ORDER_ENTRIES = " ORDER BY product_id, slot_key, part_index, file_id"


def _row_to_slot(row: sqlite3.Row) -> SlotKey:
    """Baut den Slot aus den typisierten Spalten.

    Der ``slot_key`` ist bewusst nur Schlüssel, nicht Quelle: er faltet
    ``None`` und Platzhalter zu demselben Zeichen und ist damit nicht
    umkehrbar. Deshalb führt die Tabelle jeden Slot-Anteil - auch
    ``variant`` - als eigene, typisierte Spalte.
    """
    return SlotKey(
        product_id=int(row["product_id"]),
        kind=FileKind(row["kind"]),
        os=OsName(row["os"]) if row["os"] is not None else None,
        language=row["language"],
        variant=row["variant"],
    )


def _row_to_entry(row: sqlite3.Row) -> ManifestEntry:
    """Baut den Eintrag aus den typisierten Spalten."""
    slot = _row_to_slot(row)
    return ManifestEntry(
        slot=slot,
        file_id=row["file_id"],
        filename=row["filename"],
        version=row["version"],
        size=row["size"],
        md5=row["md5"],
        downlink=row["downlink"],
        part_index=int(row["part_index"]),
        total_parts=int(row["total_parts"]),
        relative_path=row["relative_path"],
        state=LocalState(row["state"]),
        bytes_done=int(row["bytes_done"]),
        last_seen_utc=row["last_seen_utc"],
        last_verified_utc=row["last_verified_utc"],
        dlc_of=row["dlc_of"] if row["dlc_of"] is None else int(row["dlc_of"]),
    )


def _slot_params(slot: SlotKey) -> dict[str, object]:
    """Die fünf Slot-Spalten plus den berechneten Schlüssel."""
    return {
        "slot_key": slot.as_str(),
        "product_id": slot.product_id,
        "kind": slot.kind.value,
        "os": slot.os.value if slot.os is not None else None,
        "language": slot.language,
        "variant": slot.variant,
    }


def _remote_params(file: RemoteFile, seen_utc: str) -> dict[str, object]:
    params = _slot_params(file.slot)
    params.update(
        file_id=file.file_id,
        filename=file.filename or "",
        version=file.version,
        size=file.size,
        md5=file.md5,
        downlink=file.downlink,
        part_index=file.part_index,
        total_parts=file.total_parts,
        dlc_of=file.dlc_of,
        last_seen_utc=seen_utc,
    )
    return params


def _entry_params(entry: ManifestEntry) -> dict[str, object]:
    params = _remote_params_from_entry(entry)
    params.update(
        relative_path=entry.relative_path,
        state=entry.state.value,
        bytes_done=entry.bytes_done,
        last_verified_utc=entry.last_verified_utc,
    )
    return params


def _remote_params_from_entry(entry: ManifestEntry) -> dict[str, object]:
    params = _slot_params(entry.slot)
    params.update(
        file_id=entry.file_id,
        filename=entry.filename,
        version=entry.version,
        size=entry.size,
        md5=entry.md5,
        downlink=entry.downlink,
        part_index=entry.part_index,
        total_parts=entry.total_parts,
        dlc_of=entry.dlc_of,
        last_seen_utc=entry.last_seen_utc,
    )
    return params


def _upgrade_1_to_2(conn: sqlite3.Connection) -> None:
    """Ergänzt ``variant``/``dlc_of`` und berechnet alle ``slot_key`` neu.

    ``SlotKey.as_str()`` setzt seit Schema 2 auch für fehlende ``os``/
    ``language`` einen Platzhalter. Ein unter Schema 1 geschriebener
    Schlüssel wie ``4242/extra`` heißt jetzt ``4242/extra/-/-`` - ohne
    Neuberechnung fände ``entries_for_slot`` nach dem Update keinen
    einzigen Altbestand mehr wieder.
    """
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(files)")}
    if "variant" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN variant TEXT")
    if "dlc_of" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN dlc_of INTEGER")
    _recompute_slot_keys(conn)


def _recompute_slot_keys(conn: sqlite3.Connection) -> None:
    """Schreibt ``slot_key`` aus den typisierten Spalten neu.

    Bewusst über ``SlotKey.as_str()`` statt über eine SQL-Nachbildung: der
    Schlüssel darf nur eine Definition haben, sonst driften Migration und
    Laufzeit auseinander.

    Die Neuberechnung kann keine Kollision erzeugen: alte Schlüssel mit
    vollständigem ``os``/``language`` bleiben unverändert, alle übrigen
    unterscheiden sich schon in den typisierten Spalten.
    """
    updates: list[tuple[str, int]] = []
    for row in conn.execute(
        "SELECT rowid AS rowid, slot_key, product_id, kind, os, language, variant FROM files"
    ):
        new_key = _row_to_slot(row).as_str()
        if new_key != row["slot_key"]:
            updates.append((new_key, int(row["rowid"])))
    for new_key, rowid in updates:
        conn.execute("UPDATE files SET slot_key = ? WHERE rowid = ?", (new_key, rowid))


def _migrate(conn: sqlite3.Connection) -> None:
    """Legt fehlende Objekte an und hebt ``user_version`` an.

    Jede spätere Migration hängt hier einen weiteren ``if version < N``-
    Block an; die Reihenfolge der Blöcke ist die Reihenfolge der Schritte.
    Die Schritte laufen in einer Transaktion: eine abgebrochene Migration
    darf keine halb hochgezogene Datenbank hinterlassen.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= SCHEMA_VERSION:
        return
    if version < 1:
        # ``executescript`` committet implizit und gehört deshalb vor die
        # Transaktion; die DDL ist ohnehin ``IF NOT EXISTS``.
        conn.executescript(_SCHEMA_V1)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if version < 2:
            _upgrade_1_to_2(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        if isinstance(exc, sqlite3.Error):
            raise
        raise StoreError(
            f"Manifest kann nicht auf Schema {SCHEMA_VERSION:d} gehoben werden: {exc}"
        ) from exc
    conn.execute("COMMIT")


class SqliteStore:
    """Implementiert model.protocols.Store.

    Bewusst ohne FOREIGN KEY zwischen ``files`` und ``products``: der
    Datei-Stand eines Produkts wird pro Produkt geschrieben und darf nicht
    davon abhängen, ob die Bibliotheksliste schon durchgelaufen ist. Ein
    Vollersatz der Produktliste dürfte sonst Dateizustände mitreißen.
    """

    def __init__(self, path: str | Path = MEMORY_PATH) -> None:
        self._path = str(path)
        self._closed = False
        try:
            if self._path != MEMORY_PATH:
                parent = Path(self._path).expanduser().parent
                if str(parent):
                    parent.mkdir(parents=True, exist_ok=True)
                self._path = str(Path(self._path).expanduser())
            self._conn = sqlite3.connect(
                self._path, isolation_level=None, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            # Vor jeder Transaktion setzen - innerhalb einer ist es wirkungslos.
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            _migrate(self._conn)
        except (sqlite3.Error, OSError) as exc:
            raise StoreError(f"Manifest {path!s} nicht nutzbar: {exc}") from exc

    # -- interne Helfer ---------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise StoreError("Manifest ist bereits geschlossen.")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Eine Transaktion; bei Fehlern Rollback und ``StoreError``."""
        self._check_open()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise StoreError(f"Manifest nicht beschreibbar: {exc}") from exc
        try:
            yield self._conn
        except Exception as exc:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(exc, sqlite3.Error):
                raise StoreError(f"Schreiben ins Manifest fehlgeschlagen: {exc}") from exc
            raise
        try:
            self._conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise StoreError(f"Commit fehlgeschlagen: {exc}") from exc

    def _query(
        self, sql: str, params: Sequence[object] | dict[str, object] = ()
    ) -> list[sqlite3.Row]:
        self._check_open()
        try:
            return list(self._conn.execute(sql, params))
        except sqlite3.Error as exc:
            raise StoreError(f"Lesen aus dem Manifest fehlgeschlagen: {exc}") from exc

    # -- Store ------------------------------------------------------------

    def replace_remote(
        self, product_id: int, files: Sequence[RemoteFile], seen_utc: str
    ) -> None:
        """Remote-Stand eines Produkts übernehmen, lokale Zustände erhalten.

        Was GOG nicht mehr anbietet, wird nicht gelöscht, sondern auf
        ``ORPHANED`` gesetzt und behält sein altes ``last_seen_utc`` - nur
        so bleibt sichtbar, wann die Datei zuletzt existierte
        (KONZEPT.md §4.3). Ändert sich ``version``, ``size`` oder ein neu
        gelieferter ``md5``, verfällt ``last_verified_utc``: die
        Verifikation galt der alten Auslieferung. War der lokale Zustand
        dabei ``COMPLETE``, wird er ``STALE`` - vollständig, aber überholt.

        Dieser Zustandswechsel ist die eigentliche Aktualitätsentscheidung
        des Werkzeugs, und er gehört hierher: ab dem Commit ist der alte
        Stand nirgends mehr gespeichert. Wer später Manifest und
        Remote-Angebot vergleicht, vergleicht denselben Wert mit sich
        selbst und findet strukturell nie etwas. ``MISSING`` und
        ``PARTIAL`` bleiben dagegen stehen: unfertig ist nicht veraltet,
        und geladen werden sie ohnehin.

        Umgekehrt gilt: was im frischen Remote-Stand steht, ist per
        Definition nicht verwaist. Ein zurückgezogener und später wieder
        eingestellter Titel verlässt ``ORPHANED`` deshalb wieder - mit
        ``COMPLETE``, wenn ein lokaler Pfad existiert und die Verifikation
        den Abgleich überlebt hat, sonst mit ``MISSING``, damit der
        nächste Lauf ihn neu lädt. Ohne diesen Rückweg bliebe die Datei
        dauerhaft vom Download ausgeschlossen. ``bytes_done`` und
        ``relative_path`` bleiben dabei stehen: sie beschreiben, was auf
        der Platte liegt, und das ändert der Remote-Stand nicht.
        """
        with self._tx() as conn:
            known = {
                (row["slot_key"], row["file_id"])
                for row in conn.execute(
                    "SELECT slot_key, file_id FROM files WHERE product_id = ?",
                    (product_id,),
                )
            }
            seen: set[tuple[str, str]] = set()
            for file in files:
                if file.slot.product_id != product_id:
                    raise StoreError(
                        f"Datei {file.file_id} gehört zu Produkt "
                        f"{file.slot.product_id}, nicht zu {product_id}."
                    )
                params = _remote_params(file, seen_utc)
                conn.execute(_INSERT_REMOTE, params)
                seen.add((str(params["slot_key"]), file.file_id))

            for slot_key, file_id in sorted(known - seen):
                conn.execute(
                    "UPDATE files SET state = ? WHERE slot_key = ? AND file_id = ?",
                    (LocalState.ORPHANED.value, slot_key, file_id),
                )

    def entries(self, product_id: int | None = None) -> list[ManifestEntry]:
        """Alle Einträge, optional auf ein Produkt eingeschränkt."""
        if product_id is None:
            rows = self._query(_SELECT_ENTRY + _ORDER_ENTRIES)
        else:
            rows = self._query(
                _SELECT_ENTRY + " WHERE product_id = ?" + _ORDER_ENTRIES, (product_id,)
            )
        return [_row_to_entry(row) for row in rows]

    def entries_for_slot(self, slot: SlotKey) -> list[ManifestEntry]:
        """Alle Teile eines Slots, nach ``part_index`` sortiert."""
        rows = self._query(
            _SELECT_ENTRY + " WHERE slot_key = ? ORDER BY part_index, file_id",
            (slot.as_str(),),
        )
        return [_row_to_entry(row) for row in rows]

    def update_entry(self, entry: ManifestEntry) -> None:
        """Schreibt einen vollständigen Eintrag - Remote- wie Lokalfelder."""
        with self._tx() as conn:
            conn.execute(_UPSERT_ENTRY, _entry_params(entry))

    def remove_entry(self, slot: SlotKey, file_id: str) -> None:
        """Entfernt einen Eintrag endgültig aus dem Manifest."""
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM files WHERE slot_key = ? AND file_id = ?",
                (slot.as_str(), file_id),
            )

    def products(self) -> list[ProductRef]:
        """Die gespeicherte Bibliotheksliste, nach ``product_id`` sortiert."""
        rows = self._query(
            "SELECT product_id, title, slug, has_updates, is_new "
            "FROM products ORDER BY product_id"
        )
        return [
            ProductRef(
                product_id=int(row["product_id"]),
                title=row["title"],
                slug=row["slug"],
                has_updates=bool(row["has_updates"]),
                is_new=bool(row["is_new"]),
            )
            for row in rows
        ]

    def replace_products(self, products: Sequence[ProductRef]) -> None:
        """Ersetzt die Bibliotheksliste vollständig; Dateien bleiben unberührt."""
        with self._tx() as conn:
            conn.execute("DELETE FROM products")
            conn.executemany(
                "INSERT INTO products (product_id, title, slug, has_updates, is_new) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (p.product_id, p.title, p.slug, int(p.has_updates), int(p.is_new))
                    for p in products
                ],
            )

    def close(self) -> None:
        """Schließt die Verbindung; mehrfacher Aufruf ist erlaubt."""
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.close()
        except sqlite3.Error as exc:
            raise StoreError(f"Manifest konnte nicht geschlossen werden: {exc}") from exc

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
