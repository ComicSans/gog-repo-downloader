"""Tests der Kommandoschicht.

Schwerpunkt ist der Weg, den die Modultests strukturell nicht erreichen:
``_build_download_plan`` baut den Remote-Stand aus denselben Einträgen, die
es als lokalen Stand übergibt. Ein Vergleich beider Seiten kann dort nie
etwas finden - die Aktualitätsentscheidung muss deshalb im Store fallen und
in der Arbeitsliste ankommen.

Kein Netzwerk, kein GOG-Konto: die API, die Auth und der Downloader sind
Attrappen, die Ablage ist eine SQLite-Datei unter ``tmp_path``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gogdl.cli import commands
from gogdl.cli.context import AppContext, db_path
from gogdl.errors import ApiError, AuthError
from gogdl.model.types import (
    DownloadResult,
    FileKind,
    LocalState,
    ManifestEntry,
    OsName,
    Preference,
    ProductRef,
    RemoteFile,
    ResolvedLink,
    SlotKey,
    SyncConfig,
)
from gogdl.store.sqlite_store import SqliteStore

PRODUCT_ID = 4242
SLUG = "spiel"
FILENAME = "setup_spiel_1.0.0.exe"
SIZE = 1000
SEEN_1 = "2026-08-01T10:00:00+00:00"
SEEN_2 = "2026-08-02T10:00:00+00:00"

SLOT = SlotKey(PRODUCT_ID, FileKind.INSTALLER, OsName.WINDOWS, "en")


# --------------------------------------------------------------------- Helfer


def _slot(product_id: int) -> SlotKey:
    return SlotKey(product_id, FileKind.INSTALLER, OsName.WINDOWS, "en")


def _remote(
    *,
    slot: SlotKey = SLOT,
    file_id: str = "f1",
    version: str | None = "1.0.0",
    size: int | None = SIZE,
    md5: str | None = "aaaa",
    filename: str | None = FILENAME,
) -> RemoteFile:
    return RemoteFile(
        slot=slot,
        file_id=file_id,
        downlink=f"/downlink/{file_id}",
        filename=filename,
        size=size,
        md5=md5,
        version=version,
    )


def _config(dest: Path, **overrides) -> SyncConfig:
    """Konfiguration ohne Plattform- und Sprachbindung an den Testrechner.

    Der Default von ``SyncConfig`` nimmt die laufende Plattform; ein
    ``windows``-Installer fiele auf einem Mac still heraus und der Test wäre
    aus dem falschen Grund grün.
    """
    values = {
        "dest": dest,
        "os_filter": frozenset(OsName),
        "languages": frozenset({"en"}),
        "os_preference": Preference(),
        "language_preference": Preference(),
        "prune": False,
    }
    values.update(overrides)
    return SyncConfig(**values)


def _ctx(dest: Path, **overrides) -> AppContext:
    config = overrides.pop("config", None) or _config(dest)
    values = {
        "dest": dest,
        "config": config,
        "jobs": 1,
        "dry_run": False,
        "quiet": True,
        "verbose": False,
        "json_output": False,
    }
    values.update(overrides)
    return AppContext(**values)


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _open(dest: Path) -> SqliteStore:
    return SqliteStore(db_path(dest))


def _bestand(dest: Path, *, verified: bool = True) -> SqliteStore:
    """Ein Produkt, eine Datei: vollständig geladen und verifiziert."""
    store = _open(dest)
    store.replace_products([ProductRef(PRODUCT_ID, "Spiel", SLUG)])
    store.replace_remote(PRODUCT_ID, [_remote()], SEEN_1)
    entry = store.entries()[0]
    entry.state = LocalState.COMPLETE
    entry.bytes_done = SIZE
    entry.relative_path = f"{SLUG}/{FILENAME}"
    entry.last_verified_utc = SEEN_1 if verified else None
    store.update_entry(entry)
    _write(dest / SLUG / FILENAME, SIZE)
    return store


def _einziger(store: SqliteStore) -> ManifestEntry:
    entries = store.entries()
    assert len(entries) == 1, entries
    return entries[0]


class _DummyAuth:
    """Attrappe für ``GogAuth`` - kennt keine Datei und kein Netz."""

    def __init__(self, *, authenticated: bool = True) -> None:
        self.authenticated = authenticated

    def is_authenticated(self) -> bool:
        return self.authenticated

    async def access_token(self) -> str:
        return "token"


class _FakeApi:
    """Nur die vier Methoden, die die Kommandos tatsächlich rufen."""

    def __init__(
        self,
        products: list[ProductRef] | None = None,
        files: dict[int, list[RemoteFile]] | None = None,
        *,
        fail: set[int] | None = None,
        size: int | None = None,
    ) -> None:
        self._products = products or []
        self._files = files or {}
        self._fail = fail or set()
        self._size = size
        self.gefragt: list[int] = []
        self.aufgeloest: list[str] = []
        self.kopfanfragen: list[str] = []

    async def library(self) -> list[ProductRef]:
        return list(self._products)

    async def product_files(
        self, product_id: int, *, include_dlc: bool = True
    ) -> list[RemoteFile]:
        if product_id in self._fail:
            raise ApiError(f"GOG antwortete mit HTTP 500 für {product_id}")
        self.gefragt.append(product_id)
        return list(self._files.get(product_id, ()))

    async def resolve_downlink(self, downlink: str) -> ResolvedLink:
        self.aufgeloest.append(downlink)
        return ResolvedLink(url="https://cdn.example/x", filename=FILENAME, checksum_url=None)

    async def content_length(self, url: str) -> int | None:
        self.kopfanfragen.append(url)
        return self._size

    async def checksum(self, checksum_url: str):  # pragma: no cover - hier nie gerufen
        return None


class _FakeDownloader:
    """Schreibt die Zieldatei in Sollgröße und meldet Erfolg."""

    def __init__(self, api, client=None, limit_rate=None) -> None:
        self.geladen: list[Path] = []

    async def fetch(self, item, reporter) -> DownloadResult:
        size = item.entry.size or 0
        _write(item.target, size)
        item.part_path.unlink(missing_ok=True)
        self.geladen.append(item.target)
        return DownloadResult(item=item, ok=True, bytes_written=size, verified=True)


def _install_api(monkeypatch: pytest.MonkeyPatch, api: _FakeApi) -> _FakeApi:
    monkeypatch.setattr(commands, "GogApiClient", lambda *a, **kw: api)
    monkeypatch.setattr(commands, "_auth", lambda *a, **kw: _DummyAuth())
    return api


# ------------------------------------------------------- BEFUND A: Aktualität


def test_neue_version_macht_stale_und_kommt_in_die_arbeitsliste(tmp_path: Path) -> None:
    """Der Kernfall: GOG lädt still neu, Name und Größe bleiben gleich.

    Ohne die Entscheidung im Store bleibt der Eintrag COMPLETE, die Datei
    liegt in erwarteter Größe da und die Arbeitsliste bleibt leer - genau
    das Versprechen des Werkzeugs fällt aus.
    """
    dest = tmp_path / "sammlung"
    store = _bestand(dest)
    try:
        store.replace_remote(PRODUCT_ID, [_remote(version="2.0.0")], SEEN_2)

        entry = _einziger(store)
        assert entry.state is LocalState.STALE
        assert entry.last_verified_utc is None

        plan = commands._build_download_plan(store, _ctx(dest))

        assert [item.entry.file_id for item in plan.downloads] == ["f1"]
        assert plan.downloads[0].resume_from == 0
        assert plan.downloads[0].entry.version == "2.0.0"
        assert plan.downloads[0].target == dest / SLUG / FILENAME
    finally:
        store.close()


def test_geaenderte_groesse_macht_stale_und_kommt_in_die_arbeitsliste(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "sammlung"
    store = _bestand(dest)
    try:
        store.replace_remote(PRODUCT_ID, [_remote(size=2048)], SEEN_2)

        assert _einziger(store).state is LocalState.STALE

        plan = commands._build_download_plan(store, _ctx(dest))

        assert [item.entry.file_id for item in plan.downloads] == ["f1"]
        assert plan.downloads[0].resume_from == 0
    finally:
        store.close()


def test_geaenderter_md5_macht_stale_und_kommt_in_die_arbeitsliste(
    tmp_path: Path,
) -> None:
    """Gleiche Version, gleiche Größe - nur die Prüfsumme ist eine andere."""
    dest = tmp_path / "sammlung"
    store = _bestand(dest)
    try:
        store.replace_remote(PRODUCT_ID, [_remote(md5="bbbb")], SEEN_2)

        assert _einziger(store).state is LocalState.STALE

        plan = commands._build_download_plan(store, _ctx(dest))

        assert [item.entry.file_id for item in plan.downloads] == ["f1"]
        assert plan.downloads[0].entry.md5 == "bbbb"
    finally:
        store.close()


def test_unveraenderter_eintrag_bleibt_complete_und_wird_nicht_geladen(
    tmp_path: Path,
) -> None:
    """Die Gegenprobe: derselbe Stand darf nichts auslösen."""
    dest = tmp_path / "sammlung"
    store = _bestand(dest)
    try:
        store.replace_remote(PRODUCT_ID, [_remote()], SEEN_2)

        entry = _einziger(store)
        assert entry.state is LocalState.COMPLETE
        assert entry.last_verified_utc == SEEN_1

        plan = commands._build_download_plan(store, _ctx(dest))

        assert plan.downloads == []
    finally:
        store.close()


def test_stale_wird_nie_fortgesetzt(tmp_path: Path) -> None:
    """Eine ``.part`` der Altversion darf nie weitergeschrieben werden.

    Sie enthält Bytes einer anderen Auslieferung; anhängen wäre stille
    Korruption (KONZEPT.md §5.1).
    """
    dest = tmp_path / "sammlung"
    store = _bestand(dest)
    try:
        _write(dest / SLUG / (FILENAME + ".part"), 500)
        store.replace_remote(PRODUCT_ID, [_remote(size=2048)], SEEN_2)

        plan = commands._build_download_plan(store, _ctx(dest))

        assert len(plan.downloads) == 1
        assert plan.downloads[0].resume_from == 0
        assert plan.downloads[0].entry.bytes_done == 0
        assert plan.downloads[0].entry.state is LocalState.STALE
    finally:
        store.close()


def test_stale_oeffnet_keine_sprach_rueckfallebene(tmp_path: Path) -> None:
    """Ein veralteter Eintrag bleibt Teil des Angebots.

    Nähme man ihn aus dem Remote-Stand heraus, sähe die Sprachwahl die
    deutsche Fassung als „nicht angeboten" - und ``--lang de,en`` fiele auf
    Englisch zurück, das der Nutzer gar nicht will.
    """
    dest = tmp_path / "sammlung"
    slot_de = SlotKey(PRODUCT_ID, FileKind.INSTALLER, OsName.WINDOWS, "de")
    slot_en = SlotKey(PRODUCT_ID, FileKind.INSTALLER, OsName.WINDOWS, "en")

    store = _open(dest)
    try:
        store.replace_products([ProductRef(PRODUCT_ID, "Spiel", SLUG)])
        store.replace_remote(
            PRODUCT_ID,
            [
                _remote(slot=slot_de, file_id="de1", filename="setup_de.exe"),
                _remote(slot=slot_en, file_id="en1", filename="setup_en.exe"),
            ],
            SEEN_1,
        )
        deutsch = [e for e in store.entries() if e.file_id == "de1"][0]
        deutsch.state = LocalState.COMPLETE
        deutsch.bytes_done = SIZE
        deutsch.relative_path = f"{SLUG}/setup_de.exe"
        deutsch.last_verified_utc = SEEN_1
        store.update_entry(deutsch)
        _write(dest / SLUG / "setup_de.exe", SIZE)

        store.replace_remote(
            PRODUCT_ID,
            [
                _remote(slot=slot_de, file_id="de1", filename="setup_de.exe", version="2.0.0"),
                _remote(slot=slot_en, file_id="en1", filename="setup_en.exe"),
            ],
            SEEN_2,
        )

        config = _config(
            dest,
            languages=frozenset({"de", "en"}),
            language_preference=Preference.parse("de,en"),
        )
        plan = commands._build_download_plan(store, _ctx(dest, config=config))

        assert [item.entry.file_id for item in plan.downloads] == ["de1"]
    finally:
        store.close()


def test_stale_wird_geladen_und_danach_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "sammlung"
    store = _bestand(dest)
    store.replace_remote(PRODUCT_ID, [_remote(version="2.0.0")], SEEN_2)
    store.close()

    _install_api(monkeypatch, _FakeApi())
    monkeypatch.setattr(commands, "HttpDownloader", _FakeDownloader)

    rc = asyncio.run(commands.cmd_download(_ctx(dest)))

    assert rc == commands.EXIT_WORK_DONE
    store = _open(dest)
    try:
        after = _einziger(store)
        assert after.state is LocalState.COMPLETE
        assert after.version == "2.0.0"
        assert after.last_verified_utc is not None
    finally:
        store.close()


# --------------------------------------------- BEFUND B: sparsame Auflösung


def _bekannt(*, version: str | None = "1.0.0", size: int | None = SIZE) -> dict:
    """Der gespeicherte Stand einer Datei, wie ``_enrich`` ihn bekommt."""
    return {
        (SLOT, "f1"): ManifestEntry(
            slot=SLOT,
            file_id="f1",
            filename=FILENAME,
            version=version,
            size=size,
            md5=None,
            downlink="/downlink/f1",
            state=LocalState.COMPLETE,
        )
    }


def test_enrich_spart_die_kopfanfrage_bei_gleichem_stand() -> None:
    """Gleiche Version, Größe bekannt: nichts zu holen.

    Bei hunderten Titeln ist jede zusätzliche Anfrage pro Datei der
    Kostenfaktor des Laufs - und GOG drosselt.
    """
    api = _FakeApi(size=2048)

    result = asyncio.run(commands._enrich(api, [_remote(size=None, md5=None)], _bekannt()))

    assert api.aufgeloest == []
    assert api.kopfanfragen == []
    assert result[0].size == SIZE, "die bekannte Größe darf nicht verloren gehen"


def test_enrich_holt_die_groesse_bei_neuer_version() -> None:
    api = _FakeApi(size=2048)

    result = asyncio.run(
        commands._enrich(api, [_remote(version="2.0.0", size=None, md5=None)], _bekannt())
    )

    assert api.kopfanfragen == ["https://cdn.example/x"]
    assert result[0].size == 2048


def test_enrich_holt_die_groesse_wenn_keine_bekannt_ist() -> None:
    """Die Produkt-Payload nennt nur gerundeten Text - ohne Kopfanfrage kein Signal."""
    api = _FakeApi(size=2048)

    result = asyncio.run(
        commands._enrich(api, [_remote(size=None, md5=None)], _bekannt(size=None))
    )

    assert api.kopfanfragen == ["https://cdn.example/x"]
    assert result[0].size == 2048


def test_enrich_fragt_bei_strict_immer_nach() -> None:
    """Ein Re-Upload ohne Versionssprung fällt nur so auf."""
    api = _FakeApi(size=2048)

    result = asyncio.run(
        commands._enrich(api, [_remote(size=None, md5=None)], _bekannt(), strict_md5=True)
    )

    assert api.kopfanfragen == ["https://cdn.example/x"]
    assert result[0].size == 2048


def test_enrich_haelt_die_bekannte_groesse_wenn_die_kopfanfrage_nichts_liefert() -> None:
    """Kein Wert heißt „nichts erfahren" - nicht „hat keine Größe"."""
    api = _FakeApi(size=None)

    result = asyncio.run(
        commands._enrich(api, [_remote(version="2.0.0", size=None, md5=None)], _bekannt())
    )

    assert result[0].size == SIZE


def test_geholte_groesse_macht_den_eintrag_veraltet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Der ganze Weg: Kopfanfrage -> Store -> Arbeitsliste."""
    dest = tmp_path / "sammlung"
    _bestand(dest).close()

    api = _FakeApi(
        products=[ProductRef(PRODUCT_ID, "Spiel", SLUG)],
        files={PRODUCT_ID: [_remote(size=None, md5=None)]},
        size=2048,
    )
    _install_api(monkeypatch, api)

    ctx = _ctx(dest, config=_config(dest, strict_md5=True))
    assert asyncio.run(commands.cmd_update(ctx, only=[], skip=[])) == 0

    store = _open(dest)
    try:
        entry = _einziger(store)
        assert entry.size == 2048
        assert entry.state is LocalState.STALE
        assert [i.entry.file_id for i in commands._build_download_plan(store, ctx).downloads] == [
            "f1"
        ]
    finally:
        store.close()


# ---------------------------------------------------------- BEFUND C: verify


def test_verify_flach_erneuert_stempel_bei_vorhandenem_md5_nicht(tmp_path: Path) -> None:
    """Ein flacher Lauf hat md5 nicht geprüft - er darf ihn nicht bestätigen."""
    dest = tmp_path / "sammlung"
    _bestand(dest).close()

    rc = commands.cmd_verify(_ctx(dest), deep=False)

    assert rc == 0
    store = _open(dest)
    try:
        assert _einziger(store).last_verified_utc == SEEN_1
    finally:
        store.close()


def test_verify_flach_ohne_md5_setzt_stempel(tmp_path: Path) -> None:
    """Ohne md5 im Manifest ist die Größe das einzige Signal - Prüfung komplett."""
    dest = tmp_path / "sammlung"
    store = _bestand(dest)
    entry = _einziger(store)
    entry.md5 = None
    entry.last_verified_utc = None
    store.update_entry(entry)
    store.close()

    rc = commands.cmd_verify(_ctx(dest), deep=False)

    assert rc == 0
    store = _open(dest)
    try:
        assert _einziger(store).last_verified_utc is not None
    finally:
        store.close()


def test_verify_deep_mit_falschem_md5_entwertet_stempel(tmp_path: Path) -> None:
    dest = tmp_path / "sammlung"
    _bestand(dest).close()

    rc = commands.cmd_verify(_ctx(dest), deep=True)

    assert rc == 4
    store = _open(dest)
    try:
        assert _einziger(store).last_verified_utc is None
    finally:
        store.close()


# ------------------------------------------------- BEFUND D: hängender Zustand


def test_download_zieht_haengengebliebenen_zustand_nach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Datei fertig, Eintrag noch PARTIAL - der Slot wäre sonst dauerhaft blind."""
    dest = tmp_path / "sammlung"
    store = _bestand(dest, verified=False)
    entry = _einziger(store)
    entry.state = LocalState.PARTIAL
    entry.bytes_done = 300
    store.update_entry(entry)
    store.close()

    _install_api(monkeypatch, _FakeApi())
    monkeypatch.setattr(commands, "HttpDownloader", _FakeDownloader)

    asyncio.run(commands.cmd_download(_ctx(dest)))

    store = _open(dest)
    try:
        after = _einziger(store)
        assert after.state is LocalState.COMPLETE
        assert after.bytes_done == SIZE
        assert after.last_verified_utc is None, "nachgezogen ist nicht geprüft"
    finally:
        store.close()


def test_nachziehen_laesst_stale_in_ruhe(tmp_path: Path) -> None:
    """Eine veraltete Datei hat die passende Größe - und ist trotzdem falsch."""
    dest = tmp_path / "sammlung"
    store = _bestand(dest)
    try:
        store.replace_remote(PRODUCT_ID, [_remote(version="2.0.0")], SEEN_2)
        assert _einziger(store).state is LocalState.STALE

        commands._reconcile_states(store, _ctx(dest))

        assert _einziger(store).state is LocalState.STALE
    finally:
        store.close()


# ------------------------------------------------------ BEFUND E: HTTP-Client


def test_auth_uebernimmt_den_gemeinsamen_client() -> None:
    """Ein zweiter Client würde nie geschlossen."""

    class _Client:
        pass

    client = _Client()
    auth = commands._auth(client)

    assert auth._client is client
    assert auth._owns_client is False


# ------------------------------------------------------ BEFUND F: Auswahl


def _zwei_produkte() -> _FakeApi:
    return _FakeApi(
        products=[ProductRef(1, "Spiel A", "a"), ProductRef(2, "Spiel B", "b")],
        files={
            1: [_remote(slot=_slot(1), file_id="a1", filename="setup_a.exe")],
            2: [_remote(slot=_slot(2), file_id="b1", filename="setup_b.exe")],
        },
    )


def test_update_behaelt_die_vollstaendige_produktliste(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--only`` wählt aus, welche Dateien geholt werden - nicht, wer existiert.

    Fehlt ein Produkt in der Liste, kennt ``_slugs`` seinen Verzeichnisnamen
    nicht mehr und seine Dateien landen künftig unter der numerischen ID.
    """
    dest = tmp_path / "sammlung"
    api = _install_api(monkeypatch, _zwei_produkte())

    rc = asyncio.run(commands.cmd_update(_ctx(dest), only=["a"], skip=[]))

    assert rc == 0
    assert api.gefragt == [1]
    store = _open(dest)
    try:
        assert {p.slug for p in store.products()} == {"a", "b"}
        assert {e.product_id for e in store.entries()} == {1}
    finally:
        store.close()


def test_update_versteht_kommaform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "sammlung"
    api = _install_api(monkeypatch, _zwei_produkte())

    rc = asyncio.run(commands.cmd_update(_ctx(dest), only=["a,b"], skip=[]))

    assert rc == 0
    assert sorted(api.gefragt) == [1, 2]


def test_update_ohne_treffer_meldet_fehler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Eine Auswahl, die nichts trifft, ist ein Fehler - kein stiller Leerlauf."""
    dest = tmp_path / "sammlung"
    api = _install_api(monkeypatch, _zwei_produkte())

    rc = asyncio.run(commands.cmd_update(_ctx(dest), only=["gibtsnicht"], skip=[]))

    assert rc != 0
    assert api.gefragt == []
    assert "gibtsnicht" in capsys.readouterr().out
    store = _open(dest)
    try:
        assert {p.slug for p in store.products()} == {"a", "b"}
    finally:
        store.close()


def test_downloadplan_beachtet_skip(tmp_path: Path) -> None:
    dest = tmp_path / "sammlung"
    store = _open(dest)
    try:
        store.replace_products([ProductRef(1, "Spiel A", "a"), ProductRef(2, "Spiel B", "b")])
        store.replace_remote(1, [_remote(slot=_slot(1), file_id="a1", filename="a.exe")], SEEN_1)
        store.replace_remote(2, [_remote(slot=_slot(2), file_id="b1", filename="b.exe")], SEEN_1)

        plan = commands._build_download_plan(store, _ctx(dest), skip=["b"])

        assert [item.entry.file_id for item in plan.downloads] == ["a1"]
    finally:
        store.close()


def test_downloadplan_beachtet_only_in_kommaform(tmp_path: Path) -> None:
    dest = tmp_path / "sammlung"
    store = _open(dest)
    try:
        store.replace_products(
            [
                ProductRef(1, "Spiel A", "a"),
                ProductRef(2, "Spiel B", "b"),
                ProductRef(3, "Spiel C", "c"),
            ]
        )
        for pid, name in ((1, "a.exe"), (2, "b.exe"), (3, "c.exe")):
            store.replace_remote(
                pid,
                [_remote(slot=_slot(pid), file_id=f"f{pid}", filename=name)],
                SEEN_1,
            )

        plan = commands._build_download_plan(store, _ctx(dest), only=["a,c"])

        assert sorted(item.entry.file_id for item in plan.downloads) == ["f1", "f3"]
    finally:
        store.close()


def test_skip_schuetzt_auch_vor_dem_aufraeumen(tmp_path: Path) -> None:
    """Löschen ist die unumkehrbare Hälfte - die Auswahl muss hier greifen."""
    dest = tmp_path / "sammlung"
    store = _bestand(dest)
    try:
        _write(dest / SLUG / "setup_spiel_0.9.0.exe", 900)
        ctx = _ctx(dest, config=_config(dest, prune=True))

        assert [p.path.name for p in commands._plan_prune(store, ctx).prunes] == [
            "setup_spiel_0.9.0.exe"
        ]
        assert commands._plan_prune(store, ctx, skip=["spiel"]).prunes == []
    finally:
        store.close()


# ------------------------------------------------- BEFUND G: Einzelner Ausfall


def test_update_haelt_bei_einem_fehlerhaften_produkt_durch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ein HTTP 500 auf einem Produkt darf die übrigen nicht mitreißen."""
    dest = tmp_path / "sammlung"
    api = _zwei_produkte()
    api._fail = {1}
    _install_api(monkeypatch, api)

    rc = asyncio.run(commands.cmd_update(_ctx(dest), only=[], skip=[]))

    assert rc != 0
    assert api.gefragt == [2]
    ausgabe = capsys.readouterr().out
    assert "Spiel A" in ausgabe
    store = _open(dest)
    try:
        assert {e.product_id for e in store.entries()} == {2}
        assert {p.slug for p in store.products()} == {"a", "b"}
    finally:
        store.close()


# ---------------------------------------------------------- BEFUND H: Login


def test_download_ohne_login_bricht_sofort_ab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fehlender Login ist kein Download-Fehler: hier muss ein Mensch ran."""
    dest = tmp_path / "sammlung"
    store = _bestand(dest)
    store.replace_remote(PRODUCT_ID, [_remote(version="2.0.0")], SEEN_2)
    store.close()

    monkeypatch.setattr(commands, "_auth", lambda *a, **kw: _DummyAuth(authenticated=False))
    monkeypatch.setattr(commands, "GogApiClient", lambda *a, **kw: _FakeApi())
    monkeypatch.setattr(commands, "HttpDownloader", _FakeDownloader)

    with pytest.raises(AuthError) as fehler:
        asyncio.run(commands.cmd_download(_ctx(dest)))

    assert fehler.value.exit_code == 2
