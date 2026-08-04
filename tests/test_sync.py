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
    Preference,
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

    # Voreinstellung: Goodies sind dabei, einzelne Patches nicht.
    default = plan_downloads(files, [], config(), {}, slugs=SLUGS, dlc_of=dlc_of)
    assert sorted(i.entry.file_id for i in default.downloads) == ["d", "w", "x"]

    ohne_goodies = plan_downloads(
        files, [], config(include_extras=False), {}, slugs=SLUGS, dlc_of=dlc_of
    )
    assert sorted(i.entry.file_id for i in ohne_goodies.downloads) == ["d", "w"]

    mit_patches = plan_downloads(
        files, [], config(include_patches=True), {}, slugs=SLUGS, dlc_of=dlc_of
    )
    assert sorted(i.entry.file_id for i in mit_patches.downloads) == ["d", "p", "w", "x"]

    # Der DLC-Filter greift nur bei DLC; das Extra bleibt davon unberührt.
    ohne_dlc = plan_downloads(
        files, [], config(include_dlc=False), {}, slugs=SLUGS, dlc_of=dlc_of
    )
    assert sorted(i.entry.file_id for i in ohne_dlc.downloads) == ["w", "x"]

    # Ohne dlc_of-Abbildung wird nicht geraten: nichts wird als DLC gefiltert.
    ohne_wissen = plan_downloads(files, [], config(include_dlc=False), {}, slugs=SLUGS)
    assert sorted(i.entry.file_id for i in ohne_wissen.downloads) == ["d", "w", "x"]


def test_filter_erzeugen_keine_fremdmeldung():
    """Eine wegen --os ausgefilterte Datei ist trotzdem bekannt."""
    files = [remote(slot=MAC_SLOT, file_id="m", filename="game_2.0.dmg")]
    on_disk = {GAME_DIR / "game_2.0.dmg": 1000}
    plan = plan_downloads(files, [], config(), on_disk, slugs=SLUGS)
    assert plan.downloads == []
    assert plan.reports == []


# ---------------------------------------------------------------------------
# §6 - Auswahl mit Rückfallebenen (--os / --lang)
# ---------------------------------------------------------------------------

PRODUCT_ID = 1207658924


def islot(os: OsName, lang: str, kind: FileKind = FileKind.INSTALLER) -> SlotKey:
    return SlotKey(product_id=PRODUCT_ID, kind=kind, os=os, language=lang)


def datei(os: OsName, lang: str, *, kind: FileKind = FileKind.INSTALLER) -> RemoteFile:
    """Eine Auslieferung, deren file_id sie sofort lesbar macht."""
    name = f"{os.value}_{lang}"
    return remote(
        slot=islot(os, lang, kind), file_id=name, filename=f"setup_game_{name}_2.0.exe"
    )


def geplant(files, cfg) -> list[str]:
    plan = plan_downloads(files, [], cfg, {}, slugs=SLUGS)
    return sorted(item.entry.file_id for item in plan.downloads)


def lang_config(text: str, **overrides) -> SyncConfig:
    return config(language_preference=Preference.parse(text), **overrides)


def test_lang_rueckfallebene_nimmt_die_erste_ebene_die_traegt():
    """``de,en``: deutsch, und nur wenn es das nicht gibt, englisch."""
    cfg = lang_config("de,en")
    beides = [datei(OsName.WINDOWS, "de"), datei(OsName.WINDOWS, "en")]
    assert geplant(beides, cfg) == ["windows_de"]

    assert geplant([datei(OsName.WINDOWS, "en")], cfg) == ["windows_en"]

    # Keine Ebene trägt: dann wird nichts geplant, nicht ersatzweise alles.
    assert geplant([datei(OsName.WINDOWS, "fr")], cfg) == []


def test_lang_plus_nimmt_beide_sprachen():
    """``de+en``: gleichrangig, also beide."""
    cfg = lang_config("de+en")
    files = [datei(OsName.WINDOWS, "de"), datei(OsName.WINDOWS, "en")]
    assert geplant(files, cfg) == ["windows_de", "windows_en"]


def test_lang_gemischte_notation():
    """``de+en,fr``: deutsch und englisch, ersatzweise französisch."""
    cfg = lang_config("de+en,fr")
    assert geplant([datei(OsName.WINDOWS, "de"), datei(OsName.WINDOWS, "fr")], cfg) == [
        "windows_de"
    ]
    assert geplant([datei(OsName.WINDOWS, "fr")], cfg) == ["windows_fr"]


def test_sprachwahl_faellt_je_plattform_getrennt():
    """Der Kernfall: Windows hat de und en, Mac nur en.

    Eine global über alle Plattformen getroffene Sprachwahl käme auf
    ``{de}`` und würde die Mac-Fassung stumm verschlucken. Erwartet sind
    deshalb **beide** Auslieferungen: Windows auf Deutsch, Mac auf
    Englisch.
    """
    cfg = config(
        os_preference=Preference.parse("windows+mac"),
        language_preference=Preference.parse("de,en"),
    )
    files = [
        datei(OsName.WINDOWS, "de"),
        datei(OsName.WINDOWS, "en"),
        datei(OsName.MAC, "en"),
    ]
    assert geplant(files, cfg) == ["mac_en", "windows_de"]


def test_sprachwahl_je_plattform_auch_bei_patches():
    """Patches folgen derselben Logik - und getrennt von den Installern."""
    cfg = config(
        os_preference=Preference.parse("windows+mac"),
        language_preference=Preference.parse("de,en"),
        include_patches=True,
    )
    files = [
        datei(OsName.WINDOWS, "de", kind=FileKind.PATCH),
        datei(OsName.WINDOWS, "en", kind=FileKind.PATCH),
        datei(OsName.MAC, "en", kind=FileKind.PATCH),
        # Installer gibt es nur auf Englisch: das darf die Patch-Wahl nicht
        # verschieben und umgekehrt.
        datei(OsName.WINDOWS, "en"),
    ]
    assert geplant(files, cfg) == ["mac_en", "windows_de", "windows_en"]


def test_os_plus_schliesst_windows_aus():
    """``linux+mac``: Windows kommt nie, auch wenn es sonst nichts gibt."""
    cfg = config(os_preference=Preference.parse("linux+mac"))
    assert geplant([datei(OsName.WINDOWS, "en")], cfg) == []

    files = [datei(OsName.WINDOWS, "en"), datei(OsName.LINUX, "en"), datei(OsName.MAC, "en")]
    assert geplant(files, cfg) == ["linux_en", "mac_en"]


def test_os_rueckfallebene():
    """``mac,windows``: Mac, und nur ohne Mac-Fassung Windows."""
    cfg = config(os_preference=Preference.parse("mac,windows"))
    assert geplant([datei(OsName.WINDOWS, "en")], cfg) == ["windows_en"]
    assert geplant([datei(OsName.WINDOWS, "en"), datei(OsName.MAC, "en")], cfg) == ["mac_en"]


def test_rueckfallebenen_gelten_pro_produkt():
    """Die Wahl fällt pro Produkt, nicht einmal für die ganze Bibliothek."""
    zweites = SlotKey(product_id=999111, kind=FileKind.INSTALLER, os=OsName.MAC, language="en")
    cfg = config(
        os_preference=Preference.parse("mac,windows"),
        language_preference=Preference.parse("de,en"),
    )
    files = [
        datei(OsName.WINDOWS, "de"),  # Produkt 1: kein Mac -> Windows/de
        remote(slot=zweites, file_id="zweites", filename="setup_dlc_mac_2.0.pkg"),
    ]
    assert geplant(files, cfg) == ["windows_de", "zweites"]


def test_plattform_ohne_akzeptable_sprache_gilt_als_nicht_getroffen():
    """Mac nur auf Englisch, gewünscht ist Deutsch -> Windows/de rückt nach.

    Eine Mac-Fassung, die es nur auf Chinesisch (hier: Englisch) gibt,
    erfüllt den Wunsch „auf Deutsch" nicht und darf die Rückfallebene auf
    Windows nicht blockieren. Ohne die gemeinsame Kaskade wäre das
    Ergebnis leer, obwohl der Nutzer das Spiel auf Deutsch besitzt.
    """
    cfg = config(
        os_preference=Preference.parse("mac,windows"),
        language_preference=Preference.parse("de"),
    )
    files = [datei(OsName.MAC, "en"), datei(OsName.WINDOWS, "de")]
    assert geplant(files, cfg) == ["windows_de"]


def test_sprache_erschoepft_ihre_ebenen_vor_der_plattform():
    """Die Reihenfolge der beiden Rückfälle - der eigentliche Kern.

    ``--os mac,windows --lang de,en`` mit Mac auf Englisch und Windows auf
    Deutsch: Die Sprach-Rückfallebene greift **innerhalb** von Mac, bevor
    die Plattform-Ebene weiterrückt. Also Mac/en, nicht Windows/de.
    """
    cfg = config(
        os_preference=Preference.parse("mac,windows"),
        language_preference=Preference.parse("de,en"),
    )
    files = [datei(OsName.MAC, "en"), datei(OsName.WINDOWS, "de")]
    assert geplant(files, cfg) == ["mac_en"]


def test_innerhalb_der_ebene_gibt_es_keinen_weiteren_rueckfall():
    """``linux+mac``: Linux auf Deutsch, Mac auf Englisch - beide bleiben."""
    cfg = config(
        os_preference=Preference.parse("linux+mac"),
        language_preference=Preference.parse("de,en"),
    )
    files = [datei(OsName.LINUX, "de"), datei(OsName.MAC, "en")]
    assert geplant(files, cfg) == ["linux_de", "mac_en"]

    # Und eine Plattform ohne akzeptable Sprache fällt still heraus, statt
    # die ganze Ebene zu kippen.
    gemischt = [datei(OsName.LINUX, "de"), datei(OsName.MAC, "ja")]
    assert geplant(gemischt, cfg) == ["linux_de"]


def test_keine_plattform_ebene_traegt_liefert_leeres_ergebnis():
    """Kein Treffer ist kein Fehler: leerer Plan, keine Exception."""
    cfg = config(
        os_preference=Preference.parse("mac,windows"),
        language_preference=Preference.parse("de"),
    )
    files = [datei(OsName.MAC, "en"), datei(OsName.WINDOWS, "fr")]
    assert geplant(files, cfg) == []


def test_ohne_sprach_preference_traegt_jede_angebotene_ebene():
    """Ohne ``--lang`` gibt es keine Kopplung: Mac gewinnt, so oder so.

    Die flache Menge ``languages`` filtert danach wie bisher - auch wenn
    dabei nichts übrig bleibt. Das ist der alte Vertrag und bleibt so.
    """
    cfg = config(os_preference=Preference.parse("mac,windows"))
    files = [datei(OsName.MAC, "en"), datei(OsName.WINDOWS, "en")]
    assert geplant(files, cfg) == ["mac_en"]

    nur_mac_de = [datei(OsName.MAC, "de"), datei(OsName.WINDOWS, "en")]
    assert geplant(nur_mac_de, cfg) == []
    assert geplant(nur_mac_de, config(
        os_preference=Preference.parse("mac,windows"),
        languages=frozenset({"de", "en"}),
    )) == ["mac_de"]


def test_plattform_ohne_sprachdimension_traegt_immer():
    """Eine Auslieferung ohne Sprachsignal kann keinen Sprachwunsch verfehlen."""
    ohne_sprache = SlotKey(
        product_id=PRODUCT_ID, kind=FileKind.INSTALLER, os=OsName.MAC, language=None
    )
    cfg = config(
        os_preference=Preference.parse("mac,windows"),
        language_preference=Preference.parse("de"),
    )
    files = [
        remote(slot=ohne_sprache, file_id="mac_neutral", filename="game_2.0.pkg"),
        datei(OsName.WINDOWS, "de"),
    ]
    assert geplant(files, cfg) == ["mac_neutral"]


def test_extras_ueberstehen_jede_plattform_und_sprachwahl():
    """os=None und language=None: von beiden Achsen unberührt."""
    cfg = config(
        os_preference=Preference.parse("linux"),
        language_preference=Preference.parse("ja"),
        include_extras=True,
    )
    files = [
        datei(OsName.WINDOWS, "en"),
        remote(slot=EXTRA_SLOT, file_id="x", filename="handbuch.pdf", version=None),
    ]
    assert geplant(files, cfg) == ["x"]


def test_leere_preference_faellt_auf_die_flachen_mengen_zurueck():
    """Ohne Preference gelten os_filter/languages wie bisher."""
    files = [
        datei(OsName.WINDOWS, "en"),
        datei(OsName.WINDOWS, "de"),
        datei(OsName.MAC, "en"),
    ]
    assert geplant(files, config()) == ["windows_en"]
    assert geplant(files, config(languages=frozenset({"de", "en"}))) == [
        "windows_de",
        "windows_en",
    ]
    assert geplant(files, config(os_filter=frozenset({OsName.WINDOWS, OsName.MAC}))) == [
        "mac_en",
        "windows_en",
    ]


def test_gesetzte_preference_ersetzt_die_flache_menge():
    """Preference und Menge schneiden sich nicht - sonst trüge keine Ebene.

    ``os_filter``/``languages`` stehen hier bewusst auf dem alten Default
    (Windows/en), und trotzdem muss die Mac-Fassung auf Deutsch kommen.
    """
    cfg = config(
        os_filter=frozenset({OsName.WINDOWS}),
        languages=frozenset({"en"}),
        os_preference=Preference.parse("mac"),
        language_preference=Preference.parse("de"),
    )
    files = [datei(OsName.WINDOWS, "en"), datei(OsName.MAC, "de")]
    assert geplant(files, cfg) == ["mac_de"]


def test_orphaned_belegt_keine_plattform():
    """Eine nicht mehr ladbare Mac-Fassung darf die Rückfallebene nicht blockieren."""
    mac = datei(OsName.MAC, "en")
    tot = entry(
        slot=mac.slot, file_id=mac.file_id, filename=mac.filename, state=LocalState.ORPHANED
    )
    cfg = config(os_preference=Preference.parse("mac,windows"))
    plan = plan_downloads([mac, datei(OsName.WINDOWS, "en")], [tot], cfg, {}, slugs=SLUGS)
    assert sorted(i.entry.file_id for i in plan.downloads) == ["windows_en"]


def test_no_dlc_filtert_ueber_remote_file_dlc_of():
    """``--no-dlc`` ohne Mapping: die Zuordnung steht in der Datei selbst."""
    files = [
        remote(slot=WIN_SLOT, file_id="w"),
        remote(slot=DLC_SLOT, file_id="d", filename="setup_dlc_1.0.exe", dlc_of=1207658924),
    ]
    assert geplant(files, config()) == ["d", "w"]
    assert geplant(files, config(include_dlc=False)) == ["w"]

    # Der Zeiger auf sich selbst ist kein DLC.
    eigen = [remote(slot=WIN_SLOT, file_id="w", dlc_of=1207658924)]
    assert geplant(eigen, config(include_dlc=False)) == ["w"]


def test_dlc_mapping_uebersteuert_die_angabe_an_der_datei():
    """Ist das Mapping gesetzt, gilt allein es."""
    files = [
        remote(slot=WIN_SLOT, file_id="w"),
        remote(slot=DLC_SLOT, file_id="d", filename="setup_dlc_1.0.exe", dlc_of=1207658924),
    ]
    plan = plan_downloads(
        files, [], config(include_dlc=False), {}, slugs=SLUGS, dlc_of={1207658924: 999111}
    )
    assert sorted(i.entry.file_id for i in plan.downloads) == ["d"]


def test_zwei_eintraege_mit_gleichem_zielpfad_kollidieren():
    """Das Layout ist flach - zwei Slots koennen denselben Namen tragen.

    Installer und Extra heissen beide ``doku.pdf`` und landen im selben
    Verzeichnis. Ohne Erkennung schreiben bei ``--jobs 2`` zwei Downloads
    gleichzeitig in dieselbe ``.part``. Der erste Eintrag behaelt den
    Pfad, der zweite wird abgelehnt und gemeldet.
    """
    files = [
        remote(slot=WIN_SLOT, file_id="w", filename="doku.pdf", version=None),
        remote(slot=EXTRA_SLOT, file_id="x", filename="doku.pdf", version=None),
    ]
    plan = plan_downloads(files, [], config(include_extras=True), {}, slugs=SLUGS)

    assert [item.entry.file_id for item in plan.downloads] == ["w"]
    kollisionen = [r for r in plan.reports if r.kind == "collision"]
    assert len(kollisionen) == 1
    assert kollisionen[0].path == GAME_DIR / "doku.pdf"
    assert "x" in kollisionen[0].detail


def test_gleicher_name_in_verschiedenen_produkten_kollidiert_nicht():
    """Gegenprobe: das Verzeichnis trennt die beiden."""
    files = [
        remote(slot=WIN_SLOT, file_id="w", filename="doku.pdf", version=None),
        remote(slot=DLC_SLOT, file_id="d", filename="doku.pdf", version=None),
    ]
    plan = plan_downloads(files, [], config(), {}, slugs=SLUGS)

    assert sorted(item.entry.file_id for item in plan.downloads) == ["d", "w"]
    assert [r for r in plan.reports if r.kind == "collision"] == []


def test_download_eintrag_traegt_dlc_of_weiter():
    """Sonst stirbt die Zuordnung beim ersten Roundtrip durch den Store."""
    plan = plan_downloads(
        [remote(slot=DLC_SLOT, file_id="d", filename="setup_dlc_1.0.exe", dlc_of=1207658924)],
        [],
        config(),
        {},
        slugs=SLUGS,
    )
    assert plan.downloads[0].entry.dlc_of == 1207658924


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

    Beide Dateien tragen ueberhaupt kein Versionstoken und koennen damit
    keine Vorgaengerversion sein - ``set_alt.dat`` teilt zwar drei Zeichen
    mit ``setup_game_2.0.exe``, aber Namensnaehe allein autorisiert keine
    Loeschung.
    """
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "walkthrough.txt": 42,
        GAME_DIR / "set_alt.dat": 17,
    }
    plan = plan_prune([entry()], config(), on_disk, slugs=SLUGS)
    assert plan.prunes == []


def test_mehrdeutige_zuordnung_bleibt_liegen():
    """``game_1.0.zi`` passt auf kein Namensschema - also bleibt es liegen.

    Der Teil hinter dem Versionstoken (``.zi`` gegen ``.zip``) stimmt mit
    keinem der beiden Slots ueberein, und der Teil davor (``game_``) ist
    zu kurz. Namensnaehe allein genuegt nicht mehr.
    """
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


def test_prune_greift_nie_in_ein_unterverzeichnis():
    """Ein Slot in ``<slug>/extras/`` erzeugt gar keine Prune-Einträge.

    ``handbuch_alt.pdf`` teilt mit ``handbuch.pdf`` weit mehr als die
    geforderten vier Zeichen Präfix - über die Namensheuristik geriete es
    in den Löschplan. In den ``extras/``-Unterordnern eines gewachsenen
    Bestands liegt aber handverlesenes Material, das GOG teils gar nicht
    mehr anbietet. Kandidat ist deshalb nur, was **direkt** im
    Spielverzeichnis liegt.
    """
    on_disk = {
        GAME_DIR / "extras" / "handbuch.pdf": 500,
        GAME_DIR / "extras" / "handbuch_alt.pdf": 400,
    }
    local = [
        entry(
            slot=EXTRA_SLOT,
            file_id="x1",
            filename="handbuch.pdf",
            version=None,
            size=500,
            relative_path="the_game/extras/handbuch.pdf",
        )
    ]
    # include_extras=True, damit hier wirklich die Unterverzeichnis-Regel
    # greift und nicht schon der Extras-Filter.
    assert plan_prune(local, config(include_extras=True), on_disk, slugs=SLUGS).prunes == []


def test_altdatei_im_unterordner_bleibt_liegen():
    """Auch wenn der Slot selbst direkt im Spielverzeichnis liegt."""
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "unterordner" / "setup_game_1.0.exe": 900,
    }
    plan = plan_prune([entry()], config(), on_disk, slugs=SLUGS)
    assert plan.prunes == []


def test_prune_im_spielverzeichnis_bleibt_erhalten():
    """Gegenprobe: der normale Fall darf durch die Einschränkung nicht sterben."""
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "setup_game_1.0.exe": 900,
        GAME_DIR / "extras" / "handbuch_alt.pdf": 400,
    }
    plan = plan_prune([entry()], config(), on_disk, slugs=SLUGS)
    assert [i.path for i in plan.prunes] == [GAME_DIR / "setup_game_1.0.exe"]


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
# §5.5 — flacher Fremdbestand, Schreibweise, Ersatz auf der Platte
# ---------------------------------------------------------------------------


def test_flacher_fremdbestand_neben_extras_bleibt_liegen():
    """Der teuerste Fall: handverlesenes Material neben einem Extra.

    GOG-Extras haben ``os=None`` und ``language=None`` und landen deshalb
    flach in ``<dest>/<slug>/`` - der Schutz fuer Unterverzeichnisse
    greift hier nicht. Daneben liegt Material des Nutzers, das mit dem
    Extra ein langes Praefix teilt (``manual_v1_scan.pdf`` neben
    ``manual.pdf``, ``soundtrack_flac.zip`` neben ``soundtrack.zip``).
    Keine dieser Dateien traegt ein Versionstoken, also ist keine von
    ihnen eine Vorgaengerversion.
    """
    on_disk = {
        GAME_DIR / "manual.pdf": 500,
        GAME_DIR / "manual_v1_scan.pdf": 400,
        GAME_DIR / "soundtrack.zip": 800,
        GAME_DIR / "soundtrack_flac.zip": 900,
    }
    local = [
        entry(slot=EXTRA_SLOT, file_id="x1", filename="manual.pdf", version=None, size=500),
        entry(slot=EXTRA_SLOT, file_id="x2", filename="soundtrack.zip", version=None, size=800),
    ]

    plan = plan_prune(local, config(include_extras=True), on_disk, slugs=SLUGS)

    assert plan.prunes == []


def test_echte_altversion_neben_der_aktuellen_wird_weiterhin_geprunt():
    """Gegenprobe zum Fremdbestand: der Normalfall muss leben."""
    on_disk = {
        GAME_DIR / "setup_spiel_2.1.1.exe": 1000,
        GAME_DIR / "setup_spiel_2.1.0.exe": 900,
    }
    local = [entry(filename="setup_spiel_2.1.1.exe", version="2.1.1")]

    plan = plan_prune(local, config(), on_disk, slugs=SLUGS)

    assert [item.path for item in plan.prunes] == [GAME_DIR / "setup_spiel_2.1.0.exe"]
    assert plan.prunes[0].old_version == "2.1.0"


def test_echter_gog_name_mit_gleicher_version_aber_neuerem_build():
    """Die Generation steckt nicht immer im Versionstoken.

    Beide Namen tragen ``4.04``; unterschieden werden sie ueber
    ``update_1``/``update_2`` und die Build-Nummer. Ein Vergleich, der nur
    das Versionstoken kennt, wuerde hier nie aufraeumen - deshalb
    vergleicht die Regel alle Ziffernfolgen der Reihe nach.
    """
    aktuell = "setup_the_witcher_3_wild_hunt_4.04a_redkit_update_2_(73883).exe"
    alt = "setup_the_witcher_3_wild_hunt_4.04a_redkit_update_1_(73519).exe"
    on_disk = {GAME_DIR / aktuell: 1000, GAME_DIR / alt: 800}
    local = [entry(filename=aktuell, version="4.04a")]

    plan = plan_prune(local, config(), on_disk, slugs=SLUGS)

    assert [item.path.name for item in plan.prunes] == [alt]


def test_anderes_spiel_mit_gleichem_schema_bleibt_liegen():
    """Nur Zahlen unterscheiden - aber die Zahl steckt im Spielnamen.

    ``setup_spiel1_…`` und ``setup_spiel2_…`` tragen dasselbe Schema und
    duerfen sich trotzdem nicht gegenseitig aufraeumen. Dafuer muss der
    Teil vor dem Versionstoken **woertlich** uebereinstimmen.
    """
    on_disk = {
        GAME_DIR / "setup_spiel2_3.0.exe": 1000,
        GAME_DIR / "setup_spiel1_2.0.exe": 900,
    }
    local = [entry(filename="setup_spiel2_3.0.exe", version="3.0")]

    assert plan_prune(local, config(), on_disk, slugs=SLUGS).prunes == []


def test_keep_versions_1_loescht_nichts_was_2_verschont():
    """Der Default darf nicht die gefaehrlichste Einstellung sein.

    Dieselbe Ausgangslage, zweimal geplant: eine Datei ohne
    Versionstoken (``setup_game_beilage.exe``) darf bei **keiner**
    Einstellung in den Plan geraten. Was ``keep_versions=1`` zusaetzlich
    loescht, ist genau die juengste Altgeneration - nichts sonst.
    """
    on_disk = {
        GAME_DIR / "setup_game_3.0.exe": 1000,
        GAME_DIR / "setup_game_2.0.exe": 950,
        GAME_DIR / "setup_game_1.0.exe": 900,
        GAME_DIR / "setup_game_beilage.exe": 42,
    }
    local = [entry(filename="setup_game_3.0.exe", version="3.0")]

    def namen(keep: int) -> set[str]:
        plan = plan_prune(local, config(keep_versions=keep), on_disk, slugs=SLUGS)
        return {item.path.name for item in plan.prunes}

    eins, zwei = namen(1), namen(2)

    assert "setup_game_beilage.exe" not in eins
    assert "setup_game_beilage.exe" not in zwei
    assert zwei <= eins
    assert eins - zwei == {"setup_game_2.0.exe"}
    assert eins == {"setup_game_1.0.exe", "setup_game_2.0.exe"}


def test_abweichende_schreibweise_macht_die_aktuelle_datei_nicht_zum_kandidaten():
    """macOS: die Platte schreibt anders als ``relative_path``.

    Der Nutzer hat umbenannt oder ein rsync kam von einem case-sensitiven
    Volume. Ein case-sensitiver Vergleich haelt die aktuelle Fassung fuer
    unbekannt, ordnet sie ihrem eigenen Slot zu und loescht sie - die
    einzige vorhandene Fassung.
    """
    on_disk = {
        GAME_DIR / "Setup_Spiel_2.1.1.exe": 1000,
        GAME_DIR / "setup_spiel_2.1.0.exe": 900,
    }
    local = [entry(filename="setup_spiel_2.1.1.exe", version="2.1.1")]

    plan = plan_prune(local, config(), on_disk, slugs=SLUGS)

    assert [item.path for item in plan.prunes] == [GAME_DIR / "setup_spiel_2.1.0.exe"]


def test_ersatz_fehlt_auf_der_platte_verhindert_jede_loeschung():
    """§5.5 verlangt einen Ersatz *auf der Platte*, nicht im Manifest.

    Das Manifest sagt COMPLETE mit ``last_verified_utc``, die Datei ist
    aber weg (verschoben, geloescht, externes Volume beim Scan nicht da).
    Wuerde die Altversion trotzdem fallen, bliebe gar nichts uebrig.
    """
    on_disk = {GAME_DIR / "setup_game_1.0.exe": 900}

    assert plan_prune([entry()], config(), on_disk, slugs=SLUGS).prunes == []


def test_ersatz_mit_falscher_groesse_verhindert_jede_loeschung():
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 512,  # Soll waeren 1000
        GAME_DIR / "setup_game_1.0.exe": 900,
    }

    assert plan_prune([entry()], config(), on_disk, slugs=SLUGS).prunes == []


def test_skip_goodies_raeumt_das_extras_umfeld_nicht_auf():
    """Wer ``--skip-goodies`` setzt, will dort auch keine Loeschungen."""
    on_disk = {
        GAME_DIR / "handbuch_2.0.pdf": 500,
        GAME_DIR / "handbuch_1.0.pdf": 400,
    }
    local = [
        entry(
            slot=EXTRA_SLOT, file_id="x1", filename="handbuch_2.0.pdf", version="2.0", size=500
        )
    ]

    ohne_goodies = plan_prune(local, config(include_extras=False), on_disk, slugs=SLUGS)
    assert ohne_goodies.prunes == []

    # Voreinstellung: Goodies werden verwaltet, also auch aufgeraeumt.
    mit_goodies = plan_prune(local, config(), on_disk, slugs=SLUGS)
    assert [item.path.name for item in mit_goodies.prunes] == ["handbuch_1.0.pdf"]


def test_zwei_slots_mit_demselben_namensschema_bleiben_liegen():
    """Passt der Kandidat auf zwei Slots, ist die Zuordnung wertlos."""
    de_slot = SlotKey(1207658924, FileKind.INSTALLER, OsName.WINDOWS, "de")
    local = [
        entry(slot=WIN_SLOT, file_id="en", filename="setup_game_2.0.exe"),
        entry(slot=de_slot, file_id="de", filename="setup_game_3.0.exe"),
    ]
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "setup_game_3.0.exe": 1000,
        GAME_DIR / "setup_game_1.0.exe": 900,
    }

    plan = plan_prune(local, config(languages=frozenset({"en", "de"})), on_disk, slugs=SLUGS)

    assert plan.prunes == []


def test_neuer_aussehende_datei_gilt_nicht_als_altversion():
    """Nur ein *aelteres* Token macht eine Datei zur Vorgaengerversion."""
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "setup_game_3.0.exe": 1100,
    }

    assert plan_prune([entry()], config(), on_disk, slugs=SLUGS).prunes == []


# ---------------------------------------------------------------------------
# §4.1/§5.5 — vom Downloader beiseitegelegte Fassungen (<name>.old)
# ---------------------------------------------------------------------------


def test_beiseitegelegte_fassung_wird_geplant():
    """GOG hat unter identischem Namen neu ausgeliefert.

    Der Downloader hat die alte Datei nach ``<name>.old`` gerettet. Sie
    traegt kein eigenes Versionstoken und passt zu keinem
    Manifest-Eintrag - trotzdem ist sie kein Fremdbestand, denn diese
    Endung vergibt nur das Werkzeug selbst.
    """
    aktuell = GAME_DIR / "setup_game_2.0.exe"
    on_disk = {aktuell: 1000, GAME_DIR / "setup_game_2.0.exe.old": 950}

    plan = plan_prune([entry()], config(), on_disk, slugs=SLUGS)

    assert [item.path.name for item in plan.prunes] == ["setup_game_2.0.exe.old"]
    assert plan.prunes[0].replaced_by == ("f1",)

    # Und plan_downloads darf sie nicht als Fremdbestand melden.
    reports = plan_downloads([remote()], [entry()], config(), on_disk, slugs=SLUGS).reports
    assert reports == []


def test_beiseitegelegte_fassung_ohne_versionstoken():
    """Der Fall aus Befund 1 - nur eben vom Werkzeug selbst angelegt.

    ``manual.pdf.old`` sieht der Namensheuristik aus wie
    ``manual_v1_scan.pdf``. Der Unterschied ist die Endung.
    """
    on_disk = {
        GAME_DIR / "manual.pdf": 500,
        GAME_DIR / "manual.pdf.old": 480,
        GAME_DIR / "manual_v1_scan.pdf": 400,
    }
    local = [entry(slot=EXTRA_SLOT, file_id="x1", filename="manual.pdf", version=None, size=500)]

    plan = plan_prune(local, config(include_extras=True), on_disk, slugs=SLUGS)

    assert [item.path.name for item in plan.prunes] == ["manual.pdf.old"]


def test_beiseitegelegte_fassungen_werden_nach_laufender_nummer_gestaffelt():
    """Alle Generationen heissen gleich - das Token taugt nicht als Ordnung.

    Wuerden sie nach Versionstoken gruppiert, laege alles in einem Bucket:
    bei ``keep_versions=2`` waere nie etwas loeschbar, bei ``1`` fiele
    alles auf einmal weg. Massgeblich ist die laufende Nummer; die
    hoechste ist die juengste Fassung.
    """
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "setup_game_2.0.exe.old": 900,
        GAME_DIR / "setup_game_2.0.exe.old.1": 910,
        GAME_DIR / "setup_game_2.0.exe.old.2": 920,
    }

    zwei = plan_prune([entry()], config(keep_versions=2), on_disk, slugs=SLUGS)
    assert sorted(item.path.name for item in zwei.prunes) == [
        "setup_game_2.0.exe.old",
        "setup_game_2.0.exe.old.1",
    ]

    eins = plan_prune([entry()], config(keep_versions=1), on_disk, slugs=SLUGS)
    assert sorted(item.path.name for item in eins.prunes) == [
        "setup_game_2.0.exe.old",
        "setup_game_2.0.exe.old.1",
        "setup_game_2.0.exe.old.2",
    ]


def test_beiseitegelegte_fassung_bei_unvollstaendigem_slot():
    """Weder geplant noch als Fremdbestand gemeldet - einfach still liegen."""
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe.part": 300,
        GAME_DIR / "setup_game_2.0.exe.old": 950,
    }
    local = [entry(state=LocalState.PARTIAL, last_verified_utc=None)]

    assert plan_prune(local, config(), on_disk, slugs=SLUGS).prunes == []

    reports = plan_downloads([remote()], local, config(), on_disk, slugs=SLUGS).reports
    assert [r for r in reports if r.kind == "foreign"] == []


def test_beiseitegelegte_fassung_ohne_bezug_bleibt_fremd():
    """``.old`` allein genuegt nicht - der Basisname muss zum Slot passen."""
    on_disk = {
        GAME_DIR / "setup_game_2.0.exe": 1000,
        GAME_DIR / "meine_sicherung.zip.old": 700,
    }

    assert plan_prune([entry()], config(), on_disk, slugs=SLUGS).prunes == []

    reports = plan_downloads([remote()], [entry()], config(), on_disk, slugs=SLUGS).reports
    assert [r.path.name for r in reports] == ["meine_sicherung.zip.old"]


def test_unversionierter_slot_hat_keine_altversion():
    """Ohne Versionstoken im aktuellen Namen gibt es nichts zu ersetzen."""
    on_disk = {
        GAME_DIR / "handbuch.pdf": 500,
        GAME_DIR / "handbuch_1.0.pdf": 400,
    }
    local = [
        entry(slot=EXTRA_SLOT, file_id="x1", filename="handbuch.pdf", version=None, size=500)
    ]

    assert plan_prune(local, config(include_extras=True), on_disk, slugs=SLUGS).prunes == []


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
        GAME_DIR / "setup_game_2.0.exe.old": 950,
        GAME_DIR / "setup_game_2.0.exe.old.1": 960,
        DEST / "fremd.bin": 5,
    }
    local = [entry(), entry(file_id="alt", filename="setup_game_0.9.exe", version="0.9",
                            slot=MAC_SLOT, state=LocalState.ORPHANED)]
    # Zwei Remote-Eintraege mit demselben Zielpfad: auch die
    # Kollisionserkennung darf die Platte nicht anfassen.
    kollidierend = [
        remote(version="3.0", size=2000),
        remote(slot=EXTRA_SLOT, file_id="x", filename="setup_game_2.0.exe", version=None),
    ]
    plan_downloads(kollidierend, local, config(include_extras=True), on_disk, slugs=SLUGS)
    plan_prune(local, config(keep_versions=2), on_disk, slugs=SLUGS)
