"""Tests für importer/ - die Übernahme eines gewachsenen Fremdbestands.

Der Import entscheidet indirekt darüber, was ``prune/`` später löschen darf.
Deshalb prüfen diese Tests nicht nur die Zuordnung selbst, sondern auch das
Zusammenspiel mit :func:`gogdl.sync.planner.plan_prune`: was der Import
bezeugt, wird dort zur Löschbefugnis.

Gearbeitet wird ausschließlich auf ``tmp_path``. Die echte Sammlung wird
von diesen Tests nicht angefasst - auch nicht lesend.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from gogdl.cli.context import scan_disk
from gogdl.importer import Trust, apply_import, match_existing
from gogdl.model.types import (
    FileKind,
    LocalState,
    ManifestEntry,
    OsName,
    SlotKey,
    SyncConfig,
)
from gogdl.store.sqlite_store import SqliteStore
from gogdl.sync import plan_prune

NOW = "2026-08-04T12:00:00+00:00"

PRODUCT_ID = 1207658924
SLUG = "the_witcher_3_wild_hunt_game"
SLUGS = {PRODUCT_ID: SLUG}

WIN_SLOT = SlotKey(
    product_id=PRODUCT_ID, kind=FileKind.INSTALLER, os=OsName.WINDOWS, language="en"
)
EXTRA_SLOT = SlotKey(product_id=PRODUCT_ID, kind=FileKind.EXTRA, variant="handbuch")

EXE = "setup_the_witcher_3_wild_hunt_4.04a_redkit_update_2_(73883).exe"
BIN1 = "setup_the_witcher_3_wild_hunt_4.04a_redkit_update_2_(73883)-1.bin"
BIN2 = "setup_the_witcher_3_wild_hunt_4.04a_redkit_update_2_(73883)-2.bin"
ALT_EXE = "setup_the_witcher_3_wild_hunt_4.04a_redkit_update_1_(73519).exe"


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def entry(
    *,
    slot: SlotKey = WIN_SLOT,
    file_id: str = "f1",
    filename: str = EXE,
    size: int | None = 1000,
    md5: str | None = None,
    version: str | None = "4.04a",
    state: LocalState = LocalState.MISSING,
    part_index: int = 1,
    total_parts: int = 3,
    relative_path: str = "",
    last_verified_utc: str | None = None,
) -> ManifestEntry:
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
        relative_path=relative_path,
        state=state,
        last_verified_utc=last_verified_utc,
    )


def write(path: Path, payload: bytes) -> Path:
    """Datei mit Inhalt anlegen, Verzeichnisse inbegriffen."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Verzeichnisinhalt als Pfad -> (Größe, mtime).

    Die Zugriffszeit bleibt außen vor: die MD5-Prüfung liest die Dateien und
    verändert sie damit zwangsläufig.
    """
    result: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        result[str(path.relative_to(root))] = (
            stat.st_size if path.is_file() else -1,
            stat.st_mtime_ns,
        )
    return result


def store_for(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "manifest.sqlite3")


def multipart_fixture(dest: Path) -> tuple[list[ManifestEntry], dict[Path, int]]:
    """Dreiteiliger Installer, alle Größen passend auf der Platte."""
    write(dest / SLUG / EXE, b"E" * 1000)
    write(dest / SLUG / BIN1, b"B" * 2000)
    write(dest / SLUG / BIN2, b"C" * 3000)
    entries = [
        entry(file_id="f1", filename=EXE, size=1000, part_index=1),
        entry(file_id="f2", filename=BIN1, size=2000, part_index=2),
        entry(file_id="f3", filename=BIN2, size=3000, part_index=3),
    ]
    return entries, scan_disk(dest)


def prune_config(dest: Path, **overrides) -> SyncConfig:
    base = {
        "dest": dest,
        "os_filter": frozenset({OsName.WINDOWS}),
        "languages": frozenset({"en"}),
        "keep_versions": 1,
    }
    base.update(overrides)
    return SyncConfig(**base)


# ---------------------------------------------------------------------------
# Zuordnung
# ---------------------------------------------------------------------------


def test_mehrteiliger_installer_wird_vollstaendig_zugeordnet(tmp_path: Path) -> None:
    dest = tmp_path / "gog"
    entries, on_disk = multipart_fixture(dest)

    plan = match_existing(entries, on_disk, dest, SLUGS)

    assert len(plan.matches) == 3
    assert not plan.unsure
    assert not plan.unmatched
    assert {match.relative_path for match in plan.matches} == {
        f"{SLUG}/{EXE}",
        f"{SLUG}/{BIN1}",
        f"{SLUG}/{BIN2}",
    }
    assert plan.match_bytes == 6000

    store = store_for(tmp_path)
    summary = apply_import(plan, store, NOW)

    assert len(summary.imported) == 3
    written = {item.file_id: item for item in store.entries(PRODUCT_ID)}
    assert set(written) == {"f1", "f2", "f3"}
    for item in written.values():
        assert item.state is LocalState.COMPLETE
        assert item.relative_path == f"{SLUG}/{item.filename}"
        assert item.bytes_done == item.size
    store.close()


def test_gross_kleinschreibung_verhindert_zuordnung_nicht(tmp_path: Path) -> None:
    dest = tmp_path / "gog"
    write(dest / SLUG / "SETUP_Witcher_(73883).EXE", b"E" * 1000)
    entries = [entry(filename="setup_witcher_(73883).exe", size=1000)]

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert len(plan.matches) == 1
    # Übernommen wird der Pfad, wie er auf der Platte steht - nicht der
    # Name aus dem Manifest.
    assert plan.matches[0].relative_path == f"{SLUG}/SETUP_Witcher_(73883).EXE"


def test_datei_in_unterverzeichnis_wird_zugeordnet(tmp_path: Path) -> None:
    dest = tmp_path / "gog"
    write(dest / SLUG / "extras" / "handbuch.pdf", b"P" * 500)
    entries = [
        entry(
            slot=EXTRA_SLOT,
            file_id="x1",
            filename="handbuch.pdf",
            size=500,
            total_parts=1,
        )
    ]

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert len(plan.matches) == 1
    assert plan.matches[0].relative_path == f"{SLUG}/extras/handbuch.pdf"

    # Der Pfad muss auch so im Store landen: aus genau diesem String baut
    # ``sync/`` den Zielpfad wieder zusammen.
    store = store_for(tmp_path)
    apply_import(plan, store, NOW)
    geschrieben = store.entries(PRODUCT_ID)
    store.close()

    assert len(geschrieben) == 1
    assert geschrieben[0].relative_path == f"{SLUG}/extras/handbuch.pdf"
    assert dest / Path(geschrieben[0].relative_path) == dest / SLUG / "extras" / "handbuch.pdf"


def test_gleicher_name_andere_groesse_ist_unsicher(tmp_path: Path) -> None:
    dest = tmp_path / "gog"
    datei = write(dest / SLUG / EXE, b"E" * 999)
    entries = [entry(size=1000)]

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert not plan.matches
    assert len(plan.unsure) == 1
    kandidat = plan.unsure[0]
    assert kandidat.path == datei
    assert "999" in kandidat.reason and "1000" in kandidat.reason

    store = store_for(tmp_path)
    summary = apply_import(plan, store, NOW)

    assert not summary.imported
    # Der Eintrag bleibt MISSING, die Datei bleibt liegen.
    assert entries[0].state is LocalState.MISSING
    assert datei.exists() and datei.stat().st_size == 999
    store.close()


def test_unsicherer_kandidat_bleibt_in_jeder_vertrauensstufe_ausgeschlossen(
    tmp_path: Path,
) -> None:
    """``Trust.SIZE`` senkt die Beweislast, es erweitert die Treffermenge nicht."""
    dest = tmp_path / "gog"
    write(dest / SLUG / EXE, b"E" * 999)
    entries = [entry(size=1000)]
    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    for stufe in (Trust.NONE, Trust.SIZE, Trust.MD5):
        store = store_for(tmp_path / stufe.value)
        summary = apply_import(plan, store, NOW, trust=stufe)
        assert not summary.imported, stufe
        assert not store.entries(), stufe
        store.close()


def test_fremdbestand_und_unbekanntes_verzeichnis_bleiben_unzugeordnet(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "gog"
    entries, _ = multipart_fixture(dest)
    spielstand = write(dest / SLUG / "SaveFiles" / "spielstand.dat", b"S" * 64)
    fremd = write(dest / "irgendein_anderes_spiel" / "setup.exe", b"F" * 1000)
    wurzel = write(dest / "notizen.txt", b"N" * 10)

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert len(plan.matches) == 3
    assert set(plan.unmatched) == {spielstand, fremd, wurzel}
    assert not plan.unsure

    store = store_for(tmp_path)
    apply_import(plan, store, NOW)
    zugeordnet = {item.relative_path for item in store.entries()}
    assert f"{SLUG}/SaveFiles/spielstand.dat" not in zugeordnet
    assert "irgendein_anderes_spiel/setup.exe" not in zugeordnet
    store.close()


def test_gleicher_name_zweimal_im_produkt_ist_nicht_eindeutig(tmp_path: Path) -> None:
    """Ein Eintrag, zwei passende Dateien - das wäre ein stiller Münzwurf."""
    dest = tmp_path / "gog"
    write(dest / SLUG / "handbuch.pdf", b"P" * 500)
    write(dest / SLUG / "extras" / "handbuch.pdf", b"Q" * 500)
    entries = [
        entry(slot=EXTRA_SLOT, file_id="x1", filename="handbuch.pdf", size=500, total_parts=1)
    ]

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert not plan.matches
    assert len(plan.unsure) == 2


def _sprachfassungen(
    filename: str,
    groessen: Sequence[int | None],
    md5s: Sequence[str | None] | None = None,
) -> list[ManifestEntry]:
    """Ein Installer, den GOG unter demselben Namen je Sprache ausliefert.

    Jede Sprachfassung ist ein eigener Slot mit eigenem ``file_id``, aber
    alle tragen denselben Dateinamen. Genau dieser Fall trat im echten Lauf
    68-mal auf.
    """
    sprachen = ["de", "en", "fr", "es", "it", "pl", "ru", "pt", "cz", "jp"]
    pruefsummen = list(md5s) if md5s is not None else [None] * len(groessen)
    return [
        entry(
            slot=SlotKey(
                product_id=PRODUCT_ID,
                kind=FileKind.INSTALLER,
                os=OsName.WINDOWS,
                language=sprachen[index],
            ),
            file_id=f"f{index}",
            filename=filename,
            size=groesse,
            md5=pruefsummen[index],
            total_parts=1,
        )
        for index, groesse in enumerate(groessen)
    ]


def test_zehn_sprachfassungen_die_groesse_entscheidet(tmp_path: Path) -> None:
    """Der Kernfall: der Name passt zehnmal, die Größe genau einmal.

    Vor der Zweistufigkeit lehnte der Import hier mit "name matches 10
    entries" ab, obwohl er die Größen längst kannte.
    """
    dest = tmp_path / "gog"
    name = "setup_metro_2033_redux_2.0.0.2.exe"
    write(dest / SLUG / name, b"M" * 1050)
    entries = _sprachfassungen(name, [1000 + 10 * index for index in range(10)])

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert len(plan.matches) == 1
    assert not plan.unsure
    treffer = plan.matches[0]
    assert treffer.size == 1050
    # Und zwar die Fassung mit passender Größe, nicht irgendeine.
    assert treffer.entry.size == 1050
    assert treffer.entry.file_id == "f5"
    assert treffer.entry.slot.language == "pl"

    store = store_for(tmp_path)
    apply_import(plan, store, NOW)
    geschrieben = store.entries(PRODUCT_ID)
    store.close()

    assert len(geschrieben) == 1
    assert geschrieben[0].file_id == "f5"
    assert geschrieben[0].relative_path == f"{SLUG}/{name}"


def test_gleicher_name_und_gleiche_groesse_ohne_pruefsumme_bleibt_abgelehnt(
    tmp_path: Path,
) -> None:
    """Zwei Sprachfassungen identischer Größe, keine Prüfsumme - kein Beweis."""
    dest = tmp_path / "gog"
    name = "setup_gray_matter_2.2.0.8-1.bin"
    write(dest / SLUG / name, b"G" * 2000)
    entries = _sprachfassungen(name, [2000, 2000])

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert not plan.matches
    assert len(plan.unsure) == 1
    grund = plan.unsure[0].reason
    # Die Begründung muss den echten Gleichstand vom bloßen Namensdoppel
    # unterscheiden - ein Mensch soll sehen, dass die Größe nicht half.
    assert "name and size match 2 entries" in grund
    assert not plan.unsure[0].entry


PRUEFSUMME = "d41d8cd98f00b204e9800998ecf8427e"
ANDERE_PRUEFSUMME = "0123456789abcdef0123456789abcdef"


def test_identische_pruefsummen_erlauben_die_zuordnung(tmp_path: Path) -> None:
    """Der Normalfall aus der Messung: derselbe Installer in fünf Sprach-Slots.

    93 von 99 gemessenen Gruppen sind nachweislich byteidentisch. Die
    Sprache des Slots sagt dann nichts über die Datei aus, und ein
    Neu-Download wäre reine Verschwendung.
    """
    dest = tmp_path / "gog"
    name = "setup_surgeon_simulator2013_anniversary_edition_2.0.0.5.exe"
    write(dest / SLUG / name, b"S" * 2000)
    entries = _sprachfassungen(name, [2000] * 5, [PRUEFSUMME.upper()] * 5)

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert len(plan.matches) == 1
    assert not plan.unsure
    assert plan.matches[0].relative_path == f"{SLUG}/{name}"

    # Deterministisch: derselbe Lauf, dasselbe Ergebnis. Sonst wanderte der
    # Eintrag zwischen zwei Importen von Slot zu Slot.
    gewaehlt = plan.matches[0].entry.file_id
    assert gewaehlt == "f0"
    for _ in range(3):
        erneut = match_existing(
            list(reversed(entries)), scan_disk(dest), dest, SLUGS
        )
        assert erneut.matches[0].entry.file_id == gewaehlt


def test_verschiedene_pruefsummen_bleiben_abgelehnt(tmp_path: Path) -> None:
    """Der Theme-Hospital-Fall: gleicher Name, gleiche Größe, anderer Inhalt."""
    dest = tmp_path / "gog"
    name = "setup_theme_hospital_v3_(28027).exe"
    write(dest / SLUG / name, b"T" * 2000)
    entries = _sprachfassungen(
        name, [2000, 2000], [PRUEFSUMME, ANDERE_PRUEFSUMME]
    )

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert not plan.matches
    assert len(plan.unsure) == 1
    assert "different checksums" in plan.unsure[0].reason


def test_fehlende_pruefsumme_beweist_nichts(tmp_path: Path) -> None:
    """Ohne Prüfsumme bei einem Kandidaten ist die Gleichheit nicht belegt."""
    dest = tmp_path / "gog"
    name = "gog_master_of_magic_2.0.0.3.sh"
    write(dest / SLUG / name, b"M" * 2000)
    entries = _sprachfassungen(
        name, [2000, 2000, 2000], [PRUEFSUMME, None, PRUEFSUMME]
    )

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert not plan.matches
    assert len(plan.unsure) == 1
    grund = plan.unsure[0].reason
    assert "checksum missing for 1" in grund
    assert "name and size match 3 entries" in grund


def test_ein_eintrag_faellt_auch_bei_gleicher_pruefsumme_nur_einer_datei_zu(
    tmp_path: Path,
) -> None:
    """Zwei Dateien wählen denselben Eintrag - dann bekommt ihn keine.

    Die Prüfsumme belegt nur, dass die Slots austauschbar sind. Sie sagt
    nicht, welche der beiden Dateien auf der Platte gemeint ist.
    """
    dest = tmp_path / "gog"
    name = "handbuch.pdf"
    write(dest / SLUG / name, b"P" * 500)
    write(dest / SLUG / "extras" / name, b"Q" * 500)
    entries = _sprachfassungen(name, [500, 500], [PRUEFSUMME, PRUEFSUMME])

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert not plan.matches
    assert len(plan.unsure) == 2
    for kandidat in plan.unsure:
        assert "2 files match the same entry" in kandidat.reason

    store = store_for(tmp_path)
    apply_import(plan, store, NOW)
    assert not store.entries()
    store.close()


def test_gleicher_name_aber_keine_passende_groesse_bleibt_unsicher(
    tmp_path: Path,
) -> None:
    """Veraltete Fassung: der Name passt mehrfach, die Größe zu keinem Eintrag."""
    dest = tmp_path / "gog"
    name = "setup_the_witcher_patch.exe"
    write(dest / SLUG / name, b"W" * 3985000)
    entries = _sprachfassungen(name, [3985008, 4000000])

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert not plan.matches
    assert len(plan.unsure) == 1
    grund = plan.unsure[0].reason
    assert "3985000" in grund
    assert "3985008" in grund and "4000000" in grund


def test_zwei_dateien_auf_denselben_eintrag_bleiben_beide_liegen(
    tmp_path: Path,
) -> None:
    """Die Eindeutigkeit gilt in beide Richtungen, auch nach Stufe 1."""
    dest = tmp_path / "gog"
    name = "handbuch.pdf"
    write(dest / SLUG / name, b"P" * 500)
    write(dest / SLUG / "extras" / name, b"Q" * 500)
    entries = _sprachfassungen(name, [500, 750, 900])

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert not plan.matches
    assert len(plan.unsure) == 2
    for kandidat in plan.unsure:
        assert "2 files match the same entry" in kandidat.reason


def test_gleichnamiger_eintrag_ohne_sollgroesse_verhindert_die_zuordnung(
    tmp_path: Path,
) -> None:
    """Ohne Sollgröße ist ein Eintrag nicht ausschließbar - also kein Treffer.

    Sonst würde die Datei einer Fassung zugeschlagen, obwohl die andere
    genauso gut passen könnte.
    """
    dest = tmp_path / "gog"
    name = "setup_master_of_magic.sh"
    write(dest / SLUG / name, b"S" * 1000)
    entries = _sprachfassungen(name, [1000, None])

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert not plan.matches
    assert len(plan.unsure) == 1
    assert "expected size" in plan.unsure[0].reason


def test_fehlende_sollgroesse_haelt_die_pruefsumme_nicht_auf(tmp_path: Path) -> None:
    """Auch mit einem Kandidaten ohne Sollgröße entscheidet die Prüfsumme.

    Der Eintrag ohne Sollgröße bleibt ein möglicher Empfänger - aber wenn
    alle Kandidaten dieselbe Prüfsumme tragen, ist auch er nachweislich
    dieselbe Datei, und die Wahl ist wieder gegenstandslos.
    """
    dest = tmp_path / "gog"
    name = "setup_beneath_a_steel_sky.exe"
    write(dest / SLUG / name, b"B" * 2000)
    entries = _sprachfassungen(
        name, [2000, 2000, 2000, 2000, None], [PRUEFSUMME] * 5
    )

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert len(plan.matches) == 1
    assert not plan.unsure
    # Gewählt wird nur unter denen mit passender Sollgröße - der Eintrag im
    # Manifest soll zur Datei passen, nicht bloß zu ihrem Inhalt.
    assert plan.matches[0].entry.file_id == "f0"
    assert plan.matches[0].entry.size == 2000


def test_eintrag_ohne_sollgroesse_wird_nicht_zugeordnet(tmp_path: Path) -> None:
    dest = tmp_path / "gog"
    write(dest / SLUG / EXE, b"E" * 1000)
    entries = [entry(size=None)]

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)

    assert not plan.matches
    assert len(plan.unsure) == 1
    assert "expected size" in plan.unsure[0].reason


def test_bereits_belegte_eintraege_bleiben_unberuehrt(tmp_path: Path) -> None:
    """COMPLETE, STALE und ORPHANED fasst der Import nicht an."""
    dest = tmp_path / "gog"
    write(dest / SLUG / EXE, b"E" * 1000)
    vorhanden = entry(
        state=LocalState.COMPLETE,
        relative_path=f"{SLUG}/{EXE}",
        last_verified_utc="2026-01-01T00:00:00+00:00",
    )

    plan = match_existing([vorhanden], scan_disk(dest), dest, SLUGS)

    assert not plan.matches
    # Die Datei gehört bereits einem Eintrag - sie ist kein Fremdbestand.
    assert not plan.unmatched
    assert vorhanden.last_verified_utc == "2026-01-01T00:00:00+00:00"


def test_produkt_ohne_slug_faellt_auf_die_produkt_id_zurueck(tmp_path: Path) -> None:
    dest = tmp_path / "gog"
    write(dest / str(PRODUCT_ID) / EXE, b"E" * 1000)
    entries = [entry(size=1000)]

    plan = match_existing(entries, scan_disk(dest), dest, {})

    assert len(plan.matches) == 1
    assert plan.matches[0].relative_path == f"{PRODUCT_ID}/{EXE}"


# ---------------------------------------------------------------------------
# Vertrauensstufen und ihre Wirkung auf prune/
# ---------------------------------------------------------------------------


def test_import_ohne_pruefung_autorisiert_keine_loeschung(tmp_path: Path) -> None:
    """Der wichtigste Test: Trust.NONE darf keine Löschung rechtfertigen.

    Gegenprobe eingebaut - dieselbe Ausgangslage mit ``Trust.SIZE`` erzeugt
    sehr wohl eine Löschung. Ohne diese Gegenüberstellung wäre die negative
    Behauptung wertlos: ein leerer Prune-Plan kann auch daher kommen, dass
    die Altdatei gar nicht als Kandidat taugt.
    """
    dest = tmp_path / "gog"
    entries, _ = multipart_fixture(dest)
    # Überholte Vorgängerversion, direkt im Produktverzeichnis. Sie ist der
    # einzige denkbare Prune-Kandidat.
    alt = write(dest / SLUG / ALT_EXE, b"A" * 800)
    on_disk = scan_disk(dest)

    plan = match_existing(entries, on_disk, dest, SLUGS)
    assert len(plan.matches) == 3
    assert plan.unmatched == (alt,)

    store = store_for(tmp_path)
    apply_import(plan, store, NOW, trust=Trust.NONE)
    importiert = store.entries(PRODUCT_ID)
    store.close()

    assert len(importiert) == 3
    for item in importiert:
        assert item.state is LocalState.COMPLETE
        assert item.last_verified_utc is None
        assert item.is_verified_complete is False

    prune = plan_prune(importiert, prune_config(dest), on_disk, slugs=SLUGS)
    assert prune.prunes == [], "Trust.NONE darf keine Löschung autorisieren"
    assert alt.exists()

    # Gegenprobe: mit bezeugter Größe wird genau diese Altdatei löschbar.
    store2 = store_for(tmp_path / "zweit")
    apply_import(plan, store2, NOW, trust=Trust.SIZE)
    bezeugt = store2.entries(PRODUCT_ID)
    store2.close()

    prune_mit = plan_prune(bezeugt, prune_config(dest), on_disk, slugs=SLUGS)
    assert [item.path for item in prune_mit.prunes] == [alt]


def test_trust_size_greift_nicht_in_unterverzeichnisse(
    tmp_path: Path,
) -> None:
    """Auch ``Trust.SIZE`` greift nie in ein Unterverzeichnis.

    Ursprünglich leitete ``plan_prune`` das Kandidatenverzeichnis eines
    Slots aus ``relative_path`` ab. Ein in ``extras/`` importierter Eintrag
    machte damit ``<slug>/extras/`` zum Prune-Verzeichnis, und eine
    namensähnliche Nachbardatei dort (``handbuch_alt.pdf``) galt als
    Altversion. Bei einem gewachsenen Bestand liegt dort aber oft
    handverlesenes Material, das GOG teilweise nicht mehr anbietet.

    Seitdem sind Kandidaten ausschließlich Dateien direkt im
    Spielverzeichnis. Ein Slot, der woanders liegt, räumt gar nichts auf -
    lieber bleibt ein Extra liegen, als dass einmal das Falsche verschwindet.
    """
    dest = tmp_path / "gog"
    write(dest / SLUG / "extras" / "handbuch.pdf", b"P" * 500)
    nachbar = write(dest / SLUG / "extras" / "handbuch_alt.pdf", b"Q" * 400)
    on_disk = scan_disk(dest)
    entries = [
        entry(slot=EXTRA_SLOT, file_id="x1", filename="handbuch.pdf", size=500, total_parts=1)
    ]

    plan = match_existing(entries, on_disk, dest, SLUGS)
    assert plan.unmatched == (nachbar,)  # Der Import selbst rührt ihn nicht an.

    ohne = store_for(tmp_path / "ohne")
    apply_import(plan, ohne, NOW, trust=Trust.NONE)
    ohne_eintraege = ohne.entries(PRODUCT_ID)
    ohne.close()
    assert plan_prune(ohne_eintraege, prune_config(dest), on_disk, slugs=SLUGS).prunes == []

    mit = store_for(tmp_path / "mit")
    apply_import(plan, mit, NOW, trust=Trust.SIZE)
    mit_eintraege = mit.entries(PRODUCT_ID)
    mit.close()
    prune = plan_prune(mit_eintraege, prune_config(dest), on_disk, slugs=SLUGS)

    # Auch mit dem stärkeren Vertrauensgrad bleibt der Nachbar unberührt:
    # Prune-Kandidaten sind nur Dateien direkt im Spielverzeichnis.
    assert [item.path for item in prune.prunes] == []
    assert nachbar.exists()


def test_trust_size_setzt_zeitstempel_ohne_die_platte_zu_lesen(tmp_path: Path) -> None:
    dest = tmp_path / "gog"
    entries, on_disk = multipart_fixture(dest)
    plan = match_existing(entries, on_disk, dest, SLUGS)

    store = store_for(tmp_path)
    summary = apply_import(plan, store, NOW, trust=Trust.SIZE)
    store.close()

    assert summary.verified_count == 3
    assert summary.imported_bytes == 6000
    assert not summary.unverifiable
    for item in summary.imported:
        assert item.last_verified_utc == NOW
        assert item.is_verified_complete


def test_md5_pruefung_mit_passender_summe_setzt_zeitstempel(tmp_path: Path) -> None:
    dest = tmp_path / "gog"
    payload = b"E" * 1000
    write(dest / SLUG / EXE, payload)
    entries = [entry(size=1000, md5=hashlib.md5(payload).hexdigest(), total_parts=1)]

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)
    store = store_for(tmp_path)
    summary = apply_import(plan, store, NOW, trust=Trust.MD5)
    store.close()

    assert not summary.rejected
    assert len(summary.imported) == 1
    assert summary.imported[0].last_verified_utc == NOW
    assert summary.imported[0].is_verified_complete


def test_md5_pruefung_mit_falscher_summe_uebernimmt_nichts(tmp_path: Path) -> None:
    dest = tmp_path / "gog"
    datei = write(dest / SLUG / EXE, b"E" * 1000)
    vorher = snapshot(dest)
    entries = [entry(size=1000, md5="0" * 32, total_parts=1)]

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)
    assert len(plan.matches) == 1  # Die Größe stimmt, nur die Prüfsumme nicht.

    store = store_for(tmp_path)
    summary = apply_import(plan, store, NOW, trust=Trust.MD5)

    assert not summary.imported
    assert len(summary.rejected) == 1
    assert summary.rejected[0].path == datei
    assert "MD5" in summary.rejected[0].reason
    assert store.entries() == []
    store.close()

    assert entries[0].state is LocalState.MISSING
    assert snapshot(dest) == vorher


def test_md5_stufe_ohne_pruefsumme_im_manifest_bezeugt_nichts(tmp_path: Path) -> None:
    """``verify_entry(deep=True)`` gäbe hier allein wegen der Größe True zurück.

    Wer ausdrücklich MD5 verlangt, darf das nicht als Prüfsumme
    untergeschoben bekommen: übernommen ja, bezeugt nein.
    """
    dest = tmp_path / "gog"
    datei = write(dest / SLUG / EXE, b"E" * 1000)
    entries = [entry(size=1000, md5=None, total_parts=1)]

    plan = match_existing(entries, scan_disk(dest), dest, SLUGS)
    store = store_for(tmp_path)
    summary = apply_import(plan, store, NOW, trust=Trust.MD5)
    store.close()

    assert len(summary.imported) == 1
    assert summary.imported[0].last_verified_utc is None
    assert summary.imported[0].is_verified_complete is False
    assert summary.unverifiable == (datei,)


# ---------------------------------------------------------------------------
# Der Import fasst die Platte nicht an
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trust", [Trust.NONE, Trust.SIZE, Trust.MD5])
def test_import_veraendert_keine_datei_auf_der_platte(tmp_path: Path, trust: Trust) -> None:
    dest = tmp_path / "gog"
    entries, on_disk = multipart_fixture(dest)
    write(dest / SLUG / "SaveFiles" / "spielstand.dat", b"S" * 64)
    write(dest / SLUG / "extras" / "handbuch.pdf", b"P" * 500)
    write(dest / SLUG / ALT_EXE, b"A" * 800)
    write(dest / "fremdes_spiel" / "setup.exe", b"F" * 100)
    on_disk = scan_disk(dest)
    vorher = snapshot(dest)

    plan = match_existing(entries, on_disk, dest, SLUGS)
    store = store_for(tmp_path)
    apply_import(plan, store, NOW, trust=trust)
    store.close()

    assert snapshot(dest) == vorher
