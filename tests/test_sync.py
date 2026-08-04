"""Tests für sync/ — die Regeln aus KONZEPT.md §4 und §5.5.

``DEST`` zeigt bewusst auf ein **nicht existierendes** Verzeichnis. Sollte
die Planung je die Platte anfassen (``exists``, ``stat``, ``resolve``),
fällt das hier auf, statt unbemerkt durchzugehen: ``sync/`` ist I/O-frei.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gogdl.model.types import (
    FileKind,
    LocalState,
    ManifestEntry,
    OsName,
    RemoteFile,
    SlotKey,
    SyncConfig,
)
from gogdl.sync import is_stale, plan_downloads, plan_prune

DEST = Path("/nirgendwo/gog-archiv")
VERIFIED = "2026-08-04T10:00:00Z"

WIN_SLOT = SlotKey(product_id=1207658924, kind=FileKind.INSTALLER, os=OsName.WINDOWS, language="en")
MAC_SLOT = SlotKey(product_id=1207658924, kind=FileKind.INSTALLER, os=OsName.MAC, language="en")
EXTRA_SLOT = SlotKey(product_id=1207658924, kind=FileKind.EXTRA)
PATCH_SLOT = SlotKey(product_id=1207658924, kind=FileKind.PATCH, os=OsName.WINDOWS, language="en")
DLC_SLOT = SlotKey(product_id=999111, kind=FileKind.INSTALLER, os=OsName.WINDOWS, language="en")

SLUGS = {1207658924: "the_game", 999111: "the_game_dlc"}
GAME_DIR = DEST / "the_game"


def config(**overrides) -> SyncConfig:
    """Konfiguration mit für Tests deterministischen Defaults."""
    base = {
        "dest": DEST,
        "os_filter": frozenset({OsName.WINDOWS}),
        "languages": frozenset({"en"}),
    }
    base.update(overrides)
    return SyncConfig(**base)


def entry(
    *,
    slot: SlotKey = WIN_SLOT,
    file_id: str = "f1",
    filename: str = "setup_game_2.0.exe",
    version: str | None = "2.0",
    size: int | None = 1000,
    md5: str | None = None,
    state: LocalState = LocalState.COMPLETE,
    last_verified_utc: str | None = VERIFIED,
    relative_path: str | None = None,
    total_parts: int = 1,
    part_index: int = 1,
) -> ManifestEntry:
    slug = SLUGS.get(slot.product_id, str(slot.product_id))
    return ManifestEntry(
        slot=slot,
        file_id=file_id,
        filename=filename,
        version=version,
        size=size,
        md5=md5,
        downlink=f"/downlink/{file_id}",
        part_index=part_index,
        total_parts=total_parts,
        relative_path=relative_path if relative_path is not None else f"{slug}/{filename}",
        state=state,
        bytes_done=size or 0 if state is LocalState.COMPLETE else 0,
        last_seen_utc=VERIFIED,
        last_verified_utc=last_verified_utc,
    )


def remote(
    *,
    slot: SlotKey = WIN_SLOT,
    file_id: str = "f1",
    filename: str | None = "setup_game_2.0.exe",
    version: str | None = "2.0",
    size: int | None = 1000,
    md5: str | None = None,
    total_parts: int = 1,
    part_index: int = 1,
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


# ---------------------------------------------------------------------------
# §4.2 — is_stale
# ---------------------------------------------------------------------------


def test_gleiche_signale_sind_nicht_veraltet():
    """Baseline: ohne Abweichung passiert nichts."""
    assert is_stale(entry(), remote()) is False


def test_gleicher_name_geaenderte_groesse_ist_veraltet():
    """Der stille Re-Upload (§4.1) — Name und Version gleich, Größe nicht.

    ``version`` ist hier auf beiden Seiten vorhanden **und identisch**:
    nur so weist der Fall nach, dass ``size`` auch dann noch geprüft wird,
    wenn die Version bereits „passt".
    """
    assert is_stale(entry(size=1000), remote(size=1024)) is True


def test_gleicher_name_geaenderte_version_ist_veraltet():
    """Größe identisch, nur die Version wandert — trotzdem veraltet."""
    assert is_stale(entry(version="2.0", size=1000), remote(version="2.1", size=1000)) is True


def test_dateiname_allein_ist_kein_signal():
    """Anderer Name, gleiche Signale ⇒ nicht veraltet."""
    local = entry(filename="setup_game_2.0.exe")
    assert is_stale(local, remote(filename="setup_game_2.0_repack.exe")) is False


def test_extra_ohne_version_md5_abweichend_ist_veraltet():
    """Fehlt die Version beidseitig, ist md5 der Tiebreaker — ohne strict."""
    local = entry(slot=EXTRA_SLOT, version=None, size=500, md5="a" * 32)
    incoming = remote(slot=EXTRA_SLOT, version=None, size=500, md5="b" * 32)
    assert is_stale(local, incoming) is True


def test_extra_md5_nur_auf_einer_seite_ist_nicht_veraltet():
    """Kein Signal ≠ negatives Signal."""
    local = entry(slot=EXTRA_SLOT, version=None, size=500, md5=None)
    incoming = remote(slot=EXTRA_SLOT, version=None, size=500, md5="b" * 32)
    assert is_stale(local, incoming) is False
    assert is_stale(entry(slot=EXTRA_SLOT, version=None, size=500, md5="a" * 32),
                    remote(slot=EXTRA_SLOT, version=None, size=500, md5=None)) is False


def test_groesse_nur_auf_einer_seite_ist_nicht_veraltet():
    assert is_stale(entry(size=None), remote(size=500)) is False
    assert is_stale(entry(size=500), remote(size=None)) is False


def test_version_nur_auf_einer_seite_ist_nicht_veraltet():
    assert is_stale(entry(version=None), remote(version="2.0")) is False


def test_installer_md5_zaehlt_nur_mit_strict():
    """Bei versionierten Dateien kostet md5 einen Request — also opt-in."""
    local = entry(version="2.0", size=1000, md5="a" * 32)
    incoming = remote(version="2.0", size=1000, md5="b" * 32)
    assert is_stale(local, incoming, strict_md5=False) is False
    assert is_stale(local, incoming, strict_md5=True) is True


def test_strict_md5_ohne_beidseitigen_wert_bleibt_still():
    local = entry(md5=None)
    assert is_stale(local, remote(md5="b" * 32), strict_md5=True) is False


# ---------------------------------------------------------------------------
# plan_downloads
# ---------------------------------------------------------------------------


def test_vollstaendige_datei_erzeugt_keinen_download():
    plan = plan_downloads(
        [remote()], [entry()], config(), {GAME_DIR / "setup_game_2.0.exe": 1000}, slugs=SLUGS
    )
    assert plan.downloads == []
    assert plan.reports == []


def test_fehlende_datei_wird_geladen():
    plan = plan_downloads([remote()], [], config(), {}, slugs=SLUGS)
    assert len(plan.downloads) == 1
    item = plan.downloads[0]
    assert item.target == GAME_DIR / "setup_game_2.0.exe"
    assert item.resume_from == 0
    assert item.entry.state is LocalState.MISSING


def test_ohne_slugs_dient_die_produkt_id_als_verzeichnis():
    plan = plan_downloads([remote()], [], config(), {})
    assert plan.downloads[0].target == DEST / "1207658924" / "setup_game_2.0.exe"


def test_part_datei_setzt_resume_from():
    on_disk = {GAME_DIR / "setup_game_2.0.exe.part": 384}
    local = entry(state=LocalState.PARTIAL, last_verified_utc=None)
    plan = plan_downloads([remote()], [local], config(), on_disk, slugs=SLUGS)
    assert len(plan.downloads) == 1
    assert plan.downloads[0].resume_from == 384
    assert plan.downloads[0].entry.state is LocalState.PARTIAL
    assert plan.reports == []  # .part zu bekanntem Eintrag ist nicht fremd


def test_veraltete_datei_wird_trotz_vollstaendigkeit_geladen():
    """Und zwar von vorn: eine .part der Altversion fortzusetzen wäre Korruption."""
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "setup_game_2.0.exe.part": 500,
    }
    plan = plan_downloads(
        [remote(version="2.1", size=2048)], [entry()], config(), on_disk, slugs=SLUGS
    )
    assert len(plan.downloads) == 1
    item = plan.downloads[0]
    assert item.entry.state is LocalState.STALE
    assert item.resume_from == 0
    # Der Downloader verifiziert gegen entry.size — das muss der Remote-Stand sein.
    assert item.entry.size == 2048
    assert item.entry.version == "2.1"


def test_umbenannte_neue_version_landet_neben_der_alten():
    """§4.1 Fall 1: neuer Dateiname — die alte Datei wird nicht überschrieben.

    Das Verzeichnis stammt weiter aus dem Manifest (importierter
    gogrepo-Bestand bleibt so nutzbar), der Name aber vom Remote-Stand.
    """
    on_disk = {GAME_DIR / "setup_game_2.0.exe": 1000}
    neu = remote(version="2.1", filename="setup_game_2.1.exe", size=2048)
    plan = plan_downloads([neu], [entry()], config(), on_disk, slugs=SLUGS)
    assert len(plan.downloads) == 1
    item = plan.downloads[0]
    assert item.target == GAME_DIR / "setup_game_2.1.exe"
    assert item.resume_from == 0
    assert item.entry.filename == "setup_game_2.1.exe"
    assert item.entry.relative_path == str(Path("the_game/setup_game_2.1.exe"))
    assert plan.reports == []  # die Altversion ist bekannt, nicht fremd


def test_umbenannte_version_schliesst_den_kreis_zum_prune():
    """Nach verifiziertem Download der 2.1 wird die 2.0 löschbar (§5.2 → §5.5)."""
    on_disk = {
        GAME_DIR / "setup_game_2.1.exe": 2048,
        GAME_DIR / "setup_game_2.0.exe": 1000,
    }
    local = [entry(filename="setup_game_2.1.exe", version="2.1", size=2048)]
    plan = plan_prune(local, config(), on_disk, slugs=SLUGS)
    assert [i.path for i in plan.prunes] == [GAME_DIR / "setup_game_2.0.exe"]
    assert plan.prunes[0].replaced_by == ("f1",)
    assert plan.prunes[0].new_version == "2.1"
    assert plan.prunes[0].old_version == "2.0"


def test_orphaned_wird_gemeldet_und_nie_geladen():
    orphan = entry(file_id="alt", filename="setup_game_1.0.exe", version="1.0",
                   state=LocalState.ORPHANED)
    plan = plan_downloads(
        [remote(file_id="alt", filename="setup_game_1.0.exe", version="1.0")],
        [orphan],
        config(),
        {},
        slugs=SLUGS,
    )
    assert plan.downloads == []
    assert [r.kind for r in plan.reports] == ["orphaned"]
    assert plan.reports[0].path == GAME_DIR / "setup_game_1.0.exe"


def test_fremddatei_wird_gemeldet():
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        DEST / "meine_notizen.txt": 12,
    }
    plan = plan_downloads([remote()], [entry()], config(), on_disk, slugs=SLUGS)
    assert [(r.kind, r.path) for r in plan.reports] == [
        ("foreign", DEST / "meine_notizen.txt")
    ]


def test_dateien_ausserhalb_von_dest_werden_ignoriert():
    plan = plan_downloads(
        [remote()], [entry()], config(), {Path("/woanders/setup_game_2.0.exe"): 1000}, slugs=SLUGS
    )
    assert plan.reports == []
    assert len(plan.downloads) == 1  # das Ziel unter dest fehlt weiterhin


def test_os_und_sprachfilter_greifen_nicht_bei_extras():
    """Extras haben os/language None und dürfen davon nicht verschwinden."""
    files = [
        remote(slot=WIN_SLOT, file_id="w", filename="setup_game_2.0.exe"),
        remote(slot=MAC_SLOT, file_id="m", filename="game_2.0.dmg"),
        remote(slot=EXTRA_SLOT, file_id="x", filename="handbuch.pdf", version=None),
    ]
    cfg = config(os_filter=frozenset({OsName.WINDOWS}), include_extras=True)
    plan = plan_downloads(files, [], cfg, {}, slugs=SLUGS)
    assert sorted(item.entry.file_id for item in plan.downloads) == ["w", "x"]


def test_extras_patches_und_dlc_filter():
    files = [
        remote(slot=WIN_SLOT, file_id="w"),
        remote(slot=EXTRA_SLOT, file_id="x", filename="handbuch.pdf", version=None),
        remote(slot=PATCH_SLOT, file_id="p", filename="patch_2.0.exe"),
        remote(slot=DLC_SLOT, file_id="d", filename="setup_dlc_1.0.exe"),
    ]
    dlc_of = {999111: 1207658924, 1207658924: 1207658924}

    default = plan_downloads(files, [], config(), {}, slugs=SLUGS, dlc_of=dlc_of)
    assert sorted(i.entry.file_id for i in default.downloads) == ["d", "w"]

    alles = plan_downloads(
        files,
        [],
        config(include_extras=True, include_patches=True),
        {},
        slugs=SLUGS,
        dlc_of=dlc_of,
    )
    assert sorted(i.entry.file_id for i in alles.downloads) == ["d", "p", "w", "x"]

    ohne_dlc = plan_downloads(
        files, [], config(include_dlc=False), {}, slugs=SLUGS, dlc_of=dlc_of
    )
    assert sorted(i.entry.file_id for i in ohne_dlc.downloads) == ["w"]

    # Ohne dlc_of-Abbildung wird nicht geraten: nichts wird als DLC gefiltert.
    ohne_wissen = plan_downloads(files, [], config(include_dlc=False), {}, slugs=SLUGS)
    assert sorted(i.entry.file_id for i in ohne_wissen.downloads) == ["d", "w"]


def test_filter_erzeugen_keine_fremdmeldung():
    """Eine wegen --os ausgefilterte Datei ist trotzdem bekannt."""
    files = [remote(slot=MAC_SLOT, file_id="m", filename="game_2.0.dmg")]
    on_disk = {GAME_DIR / "game_2.0.dmg": 1000}
    plan = plan_downloads(files, [], config(), on_disk, slugs=SLUGS)
    assert plan.downloads == []
    assert plan.reports == []


# ---------------------------------------------------------------------------
# §5.5 — plan_prune
# ---------------------------------------------------------------------------


def _zweiteiliger_slot(*, zweiter_state=LocalState.COMPLETE, zweiter_verified=VERIFIED):
    return [
        entry(file_id="p1", filename="setup_game_2.0_(1).bin", part_index=1, total_parts=2),
        entry(
            file_id="p2",
            filename="setup_game_2.0_(2).bin",
            part_index=2,
            total_parts=2,
            state=zweiter_state,
            last_verified_utc=zweiter_verified,
        ),
    ]


def _mehrteiliger_bestand():
    return {
        GAME_DIR / "setup_game_2.0_(1).bin": 1000,
        GAME_DIR / "setup_game_2.0_(2).bin": 1000,
        GAME_DIR / "setup_game_1.0_(1).bin": 900,
        GAME_DIR / "setup_game_1.0_(2).bin": 900,
    }


def test_prune_verweigert_slot_mit_unvollstaendigem_teil():
    """Die Datenverlust-Falle: Teil 1 alt löschen, während Teil 2 neu fehlt."""
    local = _zweiteiliger_slot(zweiter_state=LocalState.PARTIAL, zweiter_verified=None)
    plan = plan_prune(local, config(), _mehrteiliger_bestand(), slugs=SLUGS)
    assert plan.prunes == []


def test_prune_verweigert_slot_ohne_verifikation():
    """COMPLETE allein genügt nicht — last_verified_utc muss gesetzt sein."""
    local = _zweiteiliger_slot(zweiter_verified=None)
    plan = plan_prune(local, config(), _mehrteiliger_bestand(), slugs=SLUGS)
    assert plan.prunes == []

    local_beide_unverifiziert = [
        entry(file_id="p1", filename="setup_game_2.0_(1).bin", last_verified_utc=None),
        entry(file_id="p2", filename="setup_game_2.0_(2).bin", last_verified_utc=None),
    ]
    plan = plan_prune(
        local_beide_unverifiziert, config(), _mehrteiliger_bestand(), slugs=SLUGS
    )
    assert plan.prunes == []


def test_prune_bei_vollstaendig_verifiziertem_slot():
    local = _zweiteiliger_slot()
    plan = plan_prune(local, config(), _mehrteiliger_bestand(), slugs=SLUGS)
    assert sorted(item.path.name for item in plan.prunes) == [
        "setup_game_1.0_(1).bin",
        "setup_game_1.0_(2).bin",
    ]
    for item in plan.prunes:
        assert item.slot == WIN_SLOT
        assert item.replaced_by == ("p1", "p2")
        assert item.old_version == "1.0"
        assert item.new_version == "2.0"
        assert item.reason
    assert plan.prune_bytes == 1800


def test_prune_einzelne_altdatei_genau_ein_eintrag():
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "setup_game_1.0.exe": 900,
    }
    plan = plan_prune([entry()], config(), on_disk, slugs=SLUGS)
    assert len(plan.prunes) == 1
    item = plan.prunes[0]
    assert item.path == GAME_DIR / "setup_game_1.0.exe"
    assert item.replaced_by == ("f1",)
    assert item.size == 900


def test_prune_false_liefert_leeren_plan():
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "setup_game_1.0.exe": 900,
    }
    plan = plan_prune([entry()], config(prune=False), on_disk, slugs=SLUGS)
    assert plan.prunes == []
    assert plan.downloads == []
    assert plan.reports == []


def test_keep_versions_2_behaelt_genau_eine_vorgaengergeneration():
    on_disk = {
        GAME_DIR / "setup_game_3.0.exe": 1000,
        GAME_DIR / "setup_game_2.0.exe": 950,
        GAME_DIR / "setup_game_1.0.exe": 900,
    }
    local = [entry(filename="setup_game_3.0.exe", version="3.0")]

    behalten = plan_prune(local, config(keep_versions=2), on_disk, slugs=SLUGS)
    assert [i.path.name for i in behalten.prunes] == ["setup_game_1.0.exe"]

    nur_aktuell = plan_prune(local, config(keep_versions=1), on_disk, slugs=SLUGS)
    assert sorted(i.path.name for i in nur_aktuell.prunes) == [
        "setup_game_1.0.exe",
        "setup_game_2.0.exe",
    ]


def test_orphaned_kommt_nie_in_den_prune_plan():
    orphan = entry(
        file_id="alt", filename="setup_game_1.0.exe", version="1.0",
        slot=MAC_SLOT, state=LocalState.ORPHANED,
    )
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "setup_game_1.0.exe": 900,
    }
    plan = plan_prune([entry(), orphan], config(), on_disk, slugs=SLUGS)
    assert plan.prunes == []


def test_fremddatei_kommt_nie_in_den_prune_plan():
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        DEST / "meine_notizen.txt": 12,
        DEST / "fremdes_spiel" / "irgendwas.bin": 4096,
    }
    plan = plan_prune([entry()], config(), on_disk, slugs=SLUGS)
    assert plan.prunes == []

    # Gegenprobe: plan_downloads meldet beide als foreign, löscht aber nichts.
    reports = plan_downloads([remote()], [entry()], config(), on_disk, slugs=SLUGS).reports
    assert {r.path for r in reports} == {
        DEST / "meine_notizen.txt",
        DEST / "fremdes_spiel" / "irgendwas.bin",
    }


def test_datei_ohne_namensnaehe_bleibt_liegen():
    """Im Produktverzeichnis, aber ohne Bezug zum Slot ⇒ Fremdbestand.

    ``set_alt.dat`` teilt drei Zeichen mit ``setup_game_2.0.exe`` — ein
    Zufallstreffer, der die Mindestlänge des Präfixes verfehlt und
    deshalb nicht als Vorgängerversion durchgeht.
    """
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "walkthrough.txt": 42,
        GAME_DIR / "set_alt.dat": 17,
    }
    plan = plan_prune([entry()], config(), on_disk, slugs=SLUGS)
    assert plan.prunes == []


def test_mehrdeutige_zuordnung_bleibt_liegen():
    """Zwei Slots mit identischem Präfix — Gleichstand löscht nichts."""
    local = [
        entry(slot=WIN_SLOT, file_id="w", filename="game_2.0.zip"),
        entry(slot=MAC_SLOT, file_id="m", filename="game_2.0.zip.mac"),
    ]
    on_disk = {
        GAME_DIR / "game_2.0.zip": 1000,
        GAME_DIR / "game_2.0.zip.mac": 1000,
        GAME_DIR / "game_1.0.zi": 900,  # gleich nah an beiden Namen
    }
    plan = plan_prune(local, config(), on_disk, slugs=SLUGS)
    assert plan.prunes == []


def test_sprachslots_werden_getrennt_zugeordnet():
    de_slot = SlotKey(1207658924, FileKind.INSTALLER, OsName.WINDOWS, "de")
    local = [
        entry(slot=WIN_SLOT, file_id="en2", filename="setup_game_en_2.0.exe"),
        entry(slot=de_slot, file_id="de2", filename="setup_game_de_2.0.exe"),
    ]
    on_disk = {
        GAME_DIR / "setup_game_en_2.0.exe": 1000,
        GAME_DIR / "setup_game_de_2.0.exe": 1000,
        GAME_DIR / "setup_game_de_1.0.exe": 900,
    }
    plan = plan_prune(local, config(languages=frozenset({"en", "de"})), on_disk, slugs=SLUGS)
    assert len(plan.prunes) == 1
    assert plan.prunes[0].slot == de_slot
    assert plan.prunes[0].replaced_by == ("de2",)


def test_part_rest_einer_alten_version_darf_weg():
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "setup_game_1.0.exe.part": 128,
    }
    plan = plan_prune([entry()], config(keep_versions=2), on_disk, slugs=SLUGS)
    assert [i.path.name for i in plan.prunes] == ["setup_game_1.0.exe.part"]
    assert plan.prunes[0].old_version == "1.0"


def test_laufender_download_wird_nicht_geprunt():
    """Die .part-Datei eines aktuellen Eintrags gehört zum Bestand."""
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "setup_game_2.0.exe.part": 128,
    }
    plan = plan_prune([entry()], config(), on_disk, slugs=SLUGS)
    assert plan.prunes == []


def test_trash_und_zustandsverzeichnis_bleiben_unberuehrt():
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        DEST / ".trash" / "2026-08-04" / "setup_game_1.0.exe": 900,
        DEST / ".gogdl" / "manifest.sqlite3": 4096,
    }
    plan = plan_prune([entry()], config(), on_disk, slugs=SLUGS)
    assert plan.prunes == []
    reports = plan_downloads([remote()], [entry()], config(), on_disk, slugs=SLUGS).reports
    assert reports == []


@pytest.mark.parametrize("keep", [1, 2, 3])
def test_prune_faellt_ohne_plattenzustand_nicht_um(keep):
    assert plan_prune([entry()], config(keep_versions=keep), None, slugs=SLUGS).prunes == []


# ---------------------------------------------------------------------------
# §7 — sync/ ist I/O-frei
# ---------------------------------------------------------------------------


def test_planung_fasst_die_platte_nie_an(monkeypatch):
    """Jeder Plattenzugriff ist ein Fehler — auch ``resolve`` (Symlinks!)."""

    def verboten(*_args, **_kwargs):
        raise AssertionError("sync/ darf kein I/O machen")

    for name in ("exists", "stat", "is_file", "is_dir", "resolve", "iterdir", "glob", "open"):
        monkeypatch.setattr(Path, name, verboten, raising=False)

    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "setup_game_1.0.exe": 900,
        GAME_DIR / "setup_game_3.0.exe.part": 12,
        DEST / "fremd.bin": 5,
    }
    local = [entry(), entry(file_id="alt", filename="setup_game_0.9.exe", version="0.9",
                            slot=MAC_SLOT, state=LocalState.ORPHANED)]
    plan_downloads([remote(version="3.0", size=2000)], local, config(), on_disk, slugs=SLUGS)
    plan_prune(local, config(keep_versions=2), on_disk, slugs=SLUGS)
