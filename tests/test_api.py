"""Tests für gogdl.api gegen ``httpx.MockTransport``.

Die Fixtures bilden die echten GOG-Payloads als JSON-Strings ab; jeder
Test beschreibt genau ein Verhalten aus KONZEPT.md §3/§4.

Gearbeitet wird auf dem Offline-Weg von ``embed.gog.com``:
``account/gameDetails/{id}.json`` plus ein 302 auf jede ``manualUrl``.
Der alte Weg über ``api.gog.com/products/{id}?expand=downloads`` ist
gegen ein echtes Konto als kaputt nachgewiesen (die dort genannten
``downlink``-URLs antworten mit HTTP 404 und einer HTML-Fehlerseite).
"""

from __future__ import annotations

import json

import httpx
import pytest

from gogdl.api import GogApiClient
from gogdl.api.client import language_code, language_fallback
from gogdl.constants import EMBED_BASE
from gogdl.errors import ApiError, AuthError, RateLimitError
from gogdl.model.protocols import GogApi
from gogdl.model.types import FileKind, OsName, SlotKey

# -- Hilfsmittel ------------------------------------------------------


class FakeAuth:
    """Minimaler AuthProvider - liefert immer dasselbe Token."""

    def __init__(self, token: str = "token-123") -> None:
        self.token = token
        self.calls = 0

    async def access_token(self) -> str:
        self.calls += 1
        return self.token

    def is_authenticated(self) -> bool:
        return True


def make_client(handler, *, auth: FakeAuth | None = None, **kwargs):
    """Client auf einem MockTransport, mit protokolliertem statt echtem Schlaf."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = GogApiClient(auth or FakeAuth(), http, sleep=fake_sleep, **kwargs)
    return client, slept


def json_response(payload, status: int = 200) -> httpx.Response:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(status, text=text, headers={"Content-Type": "application/json"})


GAME_DETAILS_BASE = json.loads(
    """
{
  "title": "15 Days",
  "backgroundImage": "//images.gog.com/x_bg.jpg",
  "cdKey": "",
  "textInformation": "",
  "downloads": [],
  "galaxyDownloads": [],
  "extras": [],
  "dlcs": [],
  "tags": [],
  "isPreOrder": false,
  "releaseTimestamp": 1234567890,
  "messages": [],
  "changelog": "",
  "forumLink": "https://www.gog.com/forum/15_days",
  "isBaseProductMissing": false,
  "missingBaseProduct": null,
  "simpleGalaxyInstallers": []
}
"""
)


def details_payload(**overrides):
    """gameDetails-Payload aus der Basis plus gezielten Ergänzungen."""
    payload = json.loads(json.dumps(GAME_DETAILS_BASE))
    payload.update(overrides)
    return payload


# -- Protokoll --------------------------------------------------------


def test_client_erfuellt_protocol():
    client, _ = make_client(lambda request: json_response({}))
    assert isinstance(client, GogApi)


# -- userData ---------------------------------------------------------


async def test_user_data():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer token-123"
        assert request.headers["User-Agent"].startswith("gogdl/")
        return json_response({"username": "tobias", "userId": "48000", "isLoggedIn": True})

    client, _ = make_client(handler)
    user = await client.user_data()
    assert user.username == "tobias"
    assert user.user_id == "48000"
    assert user.is_logged_in is True


# -- Bibliothek -------------------------------------------------------

LIBRARY_PAGES = {
    1: """
{"page": 1, "totalPages": 3, "products": [
  {"id": 1, "title": "Spiel A", "slug": "spiel_a", "updates": 0, "isNew": false},
  {"id": 2, "title": "Spiel B", "slug": "spiel_b", "updates": 2, "isNew": false}
]}
""",
    2: """
{"page": 2, "totalPages": 3, "products": [
  {"id": 3, "title": "Spiel C", "slug": "spiel_c", "updates": null, "isNew": true}
]}
""",
    3: """
{"page": 3, "totalPages": 3, "products": [
  {"id": 4, "title": "Spiel D", "slug": "spiel_d"}
]}
""",
}


async def test_library_sammelt_alle_seiten():
    gesehen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["mediaType"] == "1"
        page = int(request.url.params["page"])
        gesehen.append(str(page))
        return json_response(LIBRARY_PAGES[page])

    client, _ = make_client(handler)
    products = await client.library()

    assert gesehen == ["1", "2", "3"]
    assert [p.product_id for p in products] == [1, 2, 3, 4]
    assert [p.title for p in products] == ["Spiel A", "Spiel B", "Spiel C", "Spiel D"]
    assert products[0].has_updates is False
    assert products[1].has_updates is True
    assert products[2].is_new is True
    # ``updates``/``isNew`` sind null oder fehlen -> kein Absturz, Default False
    assert products[2].has_updates is False
    assert products[3].has_updates is False and products[3].is_new is False


async def test_library_einzelseite_ohne_total_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response('{"products": [{"id": 7, "title": "T", "slug": "t"}]}')

    client, _ = make_client(handler)
    assert [p.product_id for p in await client.library()] == [7]


# -- Sprachabbildung --------------------------------------------------


def test_sprachcode_erkennt_englische_und_muttersprachliche_namen():
    assert language_code("English") == "en"
    assert language_code("Deutsch") == "de"
    assert language_code("German") == "de"
    assert language_code("français") == "fr"
    assert language_code("Polish") == "pl"
    assert language_code("русский") == "ru"
    assert language_code("日本語") == "ja"
    assert language_code("Türkçe") == "tr"


def test_sprachcode_ignoriert_gross_klein_und_randleerraum():
    assert language_code("  ENGLISH ") == "en"
    assert language_code("deutsch") == "de"


def test_sprachcode_ist_bei_unbekanntem_none():
    assert language_code("Klingon") is None
    assert language_code(None) is None
    assert language_code(17) is None


def test_alle_sprachcodes_sind_kleingeschrieben():
    """``Preference.select`` schreibt klein - ein Code mit Großbuchstaben
    fiele beim Filtern lautlos durch."""
    from gogdl.api.client import LANGUAGE_CODES

    assert all(code == code.lower() for code in LANGUAGE_CODES.values())


def test_sprach_fallback_ist_klartext_ohne_leerraum():
    assert language_fallback("Klingon") == "klingon"
    assert language_fallback("Old English") == "oldenglish"
    assert language_fallback("") == ""


# -- product_files ----------------------------------------------------


async def test_game_details_endpunkt_wird_angefragt():
    gesehen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer token-123"
        return json_response(details_payload())

    client, _ = make_client(handler)
    await client.product_files(42)

    assert gesehen == [f"{EMBED_BASE}/account/gameDetails/42.json"]


ZWEI_SPRACHEN_ZWEI_PLATTFORMEN = """
[
  ["English", {
    "windows": [{"manualUrl": "/downloads/15_days/en1installer0",
                 "name": "15 Days", "version": "1.0", "date": "", "size": "1 MB"}],
    "mac": [{"manualUrl": "/downloads/15_days/en2installer0",
             "name": "15 Days", "version": "1.0", "date": "", "size": "1 MB"}]
  }],
  ["Deutsch", {
    "windows": [{"manualUrl": "/downloads/15_days/de1installer0",
                 "name": "15 Days", "version": "1.0", "date": "", "size": "1 MB"}],
    "mac": [{"manualUrl": "/downloads/15_days/de2installer0",
             "name": "15 Days", "version": "1.0", "date": "", "size": "1 MB"}]
  }]
]
"""


async def test_zwei_sprachen_und_zwei_plattformen_ergeben_vier_slots():
    payload = details_payload(downloads=json.loads(ZWEI_SPRACHEN_ZWEI_PLATTFORMEN))
    client, _ = make_client(lambda request: json_response(payload))

    files = await client.product_files(1207658930)

    assert len(files) == 4
    assert {f.slot for f in files} == {
        SlotKey(1207658930, FileKind.INSTALLER, OsName.WINDOWS, "en"),
        SlotKey(1207658930, FileKind.INSTALLER, OsName.MAC, "en"),
        SlotKey(1207658930, FileKind.INSTALLER, OsName.WINDOWS, "de"),
        SlotKey(1207658930, FileKind.INSTALLER, OsName.MAC, "de"),
    }
    assert all(f.slot.variant is None for f in files)
    assert all(f.part_index == 1 and f.total_parts == 1 for f in files)
    assert [f.file_id for f in files] == [
        "en1installer0",
        "en2installer0",
        "de1installer0",
        "de2installer0",
    ]


DREITEILIGER_INSTALLER = """
[
  ["English", {"windows": [
    {"manualUrl": "/downloads/15_days/en1installer0",
     "name": "15 Days (Part 1 of 3)", "version": "1.0", "date": "", "size": "1 MB"},
    {"manualUrl": "/downloads/15_days/en1installer1",
     "name": "15 Days (Part 2 of 3)", "version": "1.0", "date": "", "size": "4 GB"},
    {"manualUrl": "/downloads/15_days/en1installer2",
     "name": "15 Days (Part 3 of 3)", "version": "1.0", "date": "", "size": "2 GB"}
  ]}]
]
"""


async def test_mehrteiliger_installer_teilt_einen_slot():
    """Alle Einträge einer (Sprache, Plattform) sind eine Auslieferung - §5.5."""
    payload = details_payload(downloads=json.loads(DREITEILIGER_INSTALLER))
    client, _ = make_client(lambda request: json_response(payload))

    files = await client.product_files(1207658930)

    assert len(files) == 3
    assert {f.slot for f in files} == {
        SlotKey(1207658930, FileKind.INSTALLER, OsName.WINDOWS, "en")
    }
    assert [f.part_index for f in files] == [1, 2, 3]
    assert [f.total_parts for f in files] == [3, 3, 3]
    assert [f.file_id for f in files] == ["en1installer0", "en1installer1", "en1installer2"]
    assert [f.downlink for f in files] == [
        "/downloads/15_days/en1installer0",
        "/downloads/15_days/en1installer1",
        "/downloads/15_days/en1installer2",
    ]
    assert all(f.version == "1.0" for f in files)


async def test_groesse_aus_game_details_wird_nicht_uebernommen():
    """"1 MB" ist gerundeter Text und als Aktualitätssignal unbrauchbar -
    die echte Größe liefert erst ``content_length`` (§4.2)."""
    payload = details_payload(downloads=json.loads(DREITEILIGER_INSTALLER))
    client, _ = make_client(lambda request: json_response(payload))

    files = await client.product_files(1207658930)
    assert all(f.size is None for f in files)


LEERE_VERSION = """
[["English", {"windows": [
  {"manualUrl": "/downloads/x/en1installer0", "name": "X", "version": "", "size": "1 MB"}
]}]]
"""


async def test_leere_version_wird_zu_none():
    payload = details_payload(downloads=json.loads(LEERE_VERSION))
    client, _ = make_client(lambda request: json_response(payload))

    (file,) = await client.product_files(1207658930)
    assert file.version is None


UNBEKANNTE_SPRACHE = """
[
  ["English", {"windows": [{"manualUrl": "/downloads/x/en1installer0", "version": "1.0"}]}],
  ["Klingon", {"windows": [{"manualUrl": "/downloads/x/kl1installer0", "version": "1.0"}]}]
]
"""


async def test_unbekannte_sprache_faellt_auf_den_klartext_zurueck(caplog):
    payload = details_payload(downloads=json.loads(UNBEKANNTE_SPRACHE))
    client, _ = make_client(lambda request: json_response(payload))

    with caplog.at_level("WARNING"):
        files = await client.product_files(1207658930)

    assert [f.slot.language for f in files] == ["en", "klingon"]
    assert any("Klingon" in record.getMessage() for record in caplog.records)


async def test_unbekannte_sprache_warnt_nur_einmal(caplog):
    payload = details_payload(downloads=json.loads(UNBEKANNTE_SPRACHE))
    client, _ = make_client(lambda request: json_response(payload))

    with caplog.at_level("WARNING"):
        await client.product_files(1207658930)
        await client.product_files(1207658930)

    treffer = [r for r in caplog.records if "Unbekannte Sprache" in r.getMessage()]
    assert len(treffer) == 1


UNBEKANNTE_PLATTFORM = """
[["English", {
  "amiga": [{"manualUrl": "/downloads/x/am1installer0", "version": "1.0"}],
  "windows": [{"manualUrl": "/downloads/x/en1installer0", "version": "1.0"}]
}]]
"""


async def test_unbekannte_plattform_wird_uebersprungen(caplog):
    payload = details_payload(downloads=json.loads(UNBEKANNTE_PLATTFORM))
    client, _ = make_client(lambda request: json_response(payload))

    with caplog.at_level("WARNING"):
        files = await client.product_files(1207658930)

    assert [f.file_id for f in files] == ["en1installer0"]
    assert any("amiga" in record.getMessage() for record in caplog.records)


async def test_unbekannte_plattform_warnt_nur_einmal(caplog):
    payload = details_payload(downloads=json.loads(UNBEKANNTE_PLATTFORM))
    client, _ = make_client(lambda request: json_response(payload))

    with caplog.at_level("WARNING"):
        await client.product_files(1207658930)
        await client.product_files(1207658930)

    treffer = [r for r in caplog.records if "Betriebssystem" in r.getMessage()]
    assert len(treffer) == 1


EXTRAS = """
[
  {"manualUrl": "/downloads/15_days/extra0", "name": "Handbuch (PDF)",
   "type": "manuals", "info": 1, "size": "12 MB"},
  {"manualUrl": "/downloads/15_days/extra1", "name": "Game Soundtrack",
   "type": "audio", "info": 1, "size": "300 MB"}
]
"""


async def test_extras_ergeben_je_variant_einen_slot():
    """Ohne Diskriminator wären Handbuch und Soundtrack fürs Aufräumen eine
    einzige Auslieferung - genau das verbietet §5.5."""
    payload = details_payload(extras=json.loads(EXTRAS))
    client, _ = make_client(lambda request: json_response(payload))

    files = await client.product_files(1207658930)

    assert len(files) == 2
    assert {f.slot for f in files} == {
        SlotKey(1207658930, FileKind.EXTRA, None, None, "handbuch-pdf"),
        SlotKey(1207658930, FileKind.EXTRA, None, None, "game-soundtrack"),
    }
    assert all(f.slot.os is None and f.slot.language is None for f in files)
    assert all(f.version is None and f.size is None for f in files)
    assert [f.file_id for f in files] == ["extra0", "extra1"]
    assert [f.downlink for f in files] == [
        "/downloads/15_days/extra0",
        "/downloads/15_days/extra1",
    ]


EXTRAS_OHNE_NAME = """
[
  {"manualUrl": "/downloads/15_days/extra0", "size": "1 MB"},
  {"manualUrl": "/downloads/15_days/extra1", "name": "!!!", "size": "1 MB"},
  {"name": "ohne manualUrl", "size": "1 MB"}
]
"""


async def test_extra_diskriminator_faellt_auf_die_id_der_manual_url_zurueck():
    payload = details_payload(extras=json.loads(EXTRAS_OHNE_NAME))
    client, _ = make_client(lambda request: json_response(payload))

    files = await client.product_files(1207658930)

    # Ohne brauchbaren Namen greift die id aus der manualUrl; der Eintrag
    # ohne manualUrl ist nicht ladbar und fällt weg.
    assert [f.slot.variant for f in files] == ["extra0", "extra1"]
    assert len({f.slot for f in files}) == 2


async def test_variant_ist_ueber_laeufe_stabil():
    """Der Diskriminator kommt aus der Payload, nicht aus der Reihenfolge -
    sonst gälte beim nächsten Lauf jeder Slot als neu."""
    eintraege = json.loads(EXTRAS)
    payloads = [
        details_payload(extras=eintraege),
        details_payload(extras=list(reversed(eintraege))),
    ]
    client, _ = make_client(lambda request: json_response(payloads.pop(0)))

    erster = await client.product_files(1207658930)
    zweiter = await client.product_files(1207658930)

    assert {f.slot for f in erster} == {f.slot for f in zweiter}
    assert {f.file_id: f.slot.variant for f in erster} == {
        f.file_id: f.slot.variant for f in zweiter
    }


DLC = """
[{"id": 555, "title": "15 Days DLC",
  "downloads": [["English", {"mac": [
    {"manualUrl": "/downloads/15_days_dlc/en2installer0",
     "name": "DLC", "version": "1.0", "size": "5 MB"}]}]],
  "extras": [],
  "dlcs": []}]
"""


async def test_dlc_wird_rekursiv_mit_eigener_produkt_id_erfasst():
    payload = details_payload(
        downloads=json.loads(ZWEI_SPRACHEN_ZWEI_PLATTFORMEN), dlcs=json.loads(DLC)
    )
    client, _ = make_client(lambda request: json_response(payload))

    files = await client.product_files(1207658930, include_dlc=True)

    dlc_files = [f for f in files if f.product_id == 555]
    assert len(dlc_files) == 1
    assert dlc_files[0].slot == SlotKey(555, FileKind.INSTALLER, OsName.MAC, "en")
    assert dlc_files[0].file_id == "en2installer0"
    assert len(files) == 5


async def test_dlc_datei_kennt_das_hauptspiel():
    payload = details_payload(
        downloads=json.loads(ZWEI_SPRACHEN_ZWEI_PLATTFORMEN), dlcs=json.loads(DLC)
    )
    client, _ = make_client(lambda request: json_response(payload))

    files = await client.product_files(1207658930, include_dlc=True)

    haupt = [f for f in files if f.product_id == 1207658930]
    (dlc,) = [f for f in files if f.product_id == 555]
    assert all(f.dlc_of is None and f.is_dlc is False for f in haupt)
    assert dlc.dlc_of == 1207658930
    assert dlc.is_dlc is True


async def test_dlc_wird_bei_include_dlc_false_ausgelassen():
    payload = details_payload(
        downloads=json.loads(ZWEI_SPRACHEN_ZWEI_PLATTFORMEN), dlcs=json.loads(DLC)
    )
    client, _ = make_client(lambda request: json_response(payload))

    files = await client.product_files(1207658930, include_dlc=False)

    assert all(f.product_id == 1207658930 for f in files)
    assert len(files) == 4


VERSCHACHTELTES_DLC = """
[{"id": 555, "title": "DLC", "downloads": [], "extras": [],
  "dlcs": [{"id": 556, "title": "DLC im DLC",
    "downloads": [["English", {"windows": [
      {"manualUrl": "/downloads/nested/en1installer0", "version": "1.0"}]}]],
    "extras": [], "dlcs": []}]}]
"""


async def test_dlc_rekursion_geht_in_die_tiefe():
    payload = details_payload(dlcs=json.loads(VERSCHACHTELTES_DLC))
    client, _ = make_client(lambda request: json_response(payload))

    (file,) = await client.product_files(1207658930)
    assert file.product_id == 556
    assert file.file_id == "en1installer0"
    # dlc_of zeigt auf das angefragte Hauptprodukt, nicht auf das Eltern-DLC 555.
    assert file.dlc_of == 1207658930


DLC_OHNE_ID = """
[{"title": "DLC ohne id", "extras": [
   {"manualUrl": "/downloads/15_days_dlc/extra0", "name": "DLC-Handbuch"}],
  "downloads": [], "dlcs": []}]
"""


async def test_dlc_ohne_id_laeuft_unter_der_produkt_id_des_hauptspiels(caplog):
    payload = details_payload(dlcs=json.loads(DLC_OHNE_ID))
    client, _ = make_client(lambda request: json_response(payload))

    with caplog.at_level("WARNING"):
        (file,) = await client.product_files(1207658930)

    assert file.product_id == 1207658930
    # Die Zugehörigkeit bleibt trotzdem erkennbar.
    assert file.dlc_of == 1207658930
    assert file.is_dlc is True
    assert any("ohne eigene id" in record.getMessage() for record in caplog.records)


async def test_produkt_ohne_downloads_liefert_leere_liste():
    client, _ = make_client(lambda request: json_response('{"title": "leer"}'))
    assert await client.product_files(1) == []


# -- resolve_downlink -------------------------------------------------

SIGNIERT = (
    "https://gog-cdn-fastly.gog.com/token=nva0000/secure/offline/1104118179/"
    "setup_15_days_1.0_%2819285%29.exe"
)


async def test_resolve_downlink_liest_die_signierte_url_aus_dem_location_header():
    aufrufe: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        aufrufe.append(request)
        return httpx.Response(302, headers={"Location": SIGNIERT})

    client, _ = make_client(handler)
    link = await client.resolve_downlink("/downloads/15_days/en1installer0")

    # Genau ein Request: der Redirect darf nicht verfolgt werden, sonst
    # lädt schon das Auflösen die ganze Datei.
    assert len(aufrufe) == 1
    assert str(aufrufe[0].url) == f"{EMBED_BASE}/downloads/15_days/en1installer0"
    assert aufrufe[0].headers["Authorization"] == "Bearer token-123"
    assert link.url == SIGNIERT
    assert link.filename == "setup_15_days_1.0_(19285).exe"
    # Das Checksum-XML liegt unter der signierten URL plus ".xml".
    assert link.checksum_url == SIGNIERT + ".xml"


async def test_resolve_downlink_akzeptiert_absoluten_link():
    absolut = f"{EMBED_BASE}/downloads/15_days/en1installer0"
    gesehen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(str(request.url))
        return httpx.Response(302, headers={"Location": SIGNIERT})

    client, _ = make_client(handler)
    link = await client.resolve_downlink(absolut)

    assert gesehen == [absolut]
    assert link.url == SIGNIERT


async def test_resolve_downlink_macht_relative_location_absolut():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/secure/offline/setup.exe"})

    client, _ = make_client(handler)
    link = await client.resolve_downlink("/downloads/x/en1installer0")

    assert link.url == f"{EMBED_BASE}/secure/offline/setup.exe"
    assert link.filename == "setup.exe"


async def test_resolve_downlink_akzeptiert_200_statt_302():
    """Antwortet GOG direkt mit der Datei, ist die angefragte URL bereits
    die signierte - das ist kein Fehler."""
    signiert = "https://gog-cdn-fastly.gog.com/token=x/secure/setup_spiel%20%282%29.bin"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"MZ", headers={"Content-Length": "2"})

    client, _ = make_client(handler)
    link = await client.resolve_downlink(signiert)

    assert link.url == signiert
    assert link.filename == "setup_spiel (2).bin"
    assert link.checksum_url == signiert + ".xml"


async def test_resolve_downlink_ohne_location_ist_api_error():
    client, _ = make_client(lambda request: httpx.Response(302))
    with pytest.raises(ApiError):
        await client.resolve_downlink("/downloads/x/en1installer0")


async def test_resolve_downlink_haengt_xml_an_den_pfad_ohne_query():
    """Die Query der signierten URL gehoert nicht in die Checksum-URL."""
    mit_query = SIGNIERT + "?ttl=3600&sig=abc"

    client, _ = make_client(lambda request: httpx.Response(302, headers={"Location": mit_query}))
    link = await client.resolve_downlink("/downloads/15_days/en1installer0")

    # Die signierte URL selbst bleibt unangetastet - sie braucht die Query.
    assert link.url == mit_query
    assert link.checksum_url == SIGNIERT + ".xml"
    assert "?" not in link.checksum_url
    assert "sig=abc" not in link.checksum_url


async def test_resolve_downlink_haengt_kein_zweites_xml_an():
    """Endet der Pfad schon auf .xml, wird nichts angehaengt."""
    signiert = "https://gog-cdn-fastly.gog.com/token=x/secure/offline/liste.xml"

    client, _ = make_client(lambda request: httpx.Response(302, headers={"Location": signiert}))
    link = await client.resolve_downlink("/downloads/x/en1installer0")

    assert link.checksum_url == signiert
    assert not link.checksum_url.endswith(".xml.xml")


async def test_resolve_downlink_und_checksum_liefern_md5():
    """Zusammenspiel: aufloesen, dann das XML dahinter holen.

    Zwei Requests - das ist der Preis der Pruefsumme, ein eigener Abruf
    pro Datei zusaetzlich zur Aufloesung.
    """
    aufrufe: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        aufrufe.append(str(request.url))
        if request.url.path.endswith(".xml"):
            return httpx.Response(
                200,
                text=(
                    '<file name="setup_15_days_1.0_(19285).exe" available="1" '
                    'notavailablemsg="" md5="eed77e8beeb270924d0aabbccddeeff0" '
                    'chunks="1" total_size="821824"/>'
                ),
            )
        return httpx.Response(302, headers={"Location": SIGNIERT})

    client, _ = make_client(handler)
    link = await client.resolve_downlink("/downloads/15_days/en1installer0")
    result = await client.checksum(link.checksum_url)

    assert len(aufrufe) == 2
    assert aufrufe[1] == SIGNIERT + ".xml"
    assert result is not None
    assert result.filename == "setup_15_days_1.0_(19285).exe"
    assert result.md5 == "eed77e8beeb270924d0aabbccddeeff0"
    assert result.total_size == 821824


async def test_checksum_url_mit_404_ist_kein_fehler():
    """GOG bietet nicht fuer jede Datei ein XML an - fehlt es, fehlt md5."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".xml"):
            return httpx.Response(404, text="<html>not found</html>")
        return httpx.Response(302, headers={"Location": SIGNIERT})

    client, _ = make_client(handler)
    link = await client.resolve_downlink("/downloads/15_days/en1installer0")

    assert link.checksum_url == SIGNIERT + ".xml"
    assert await client.checksum(link.checksum_url) is None


async def test_checksum_xml_ohne_total_size_ist_gueltig():
    """Fehlt total_size, bleibt md5 trotzdem verwertbar."""
    client, _ = make_client(
        lambda request: httpx.Response(200, text='<file name="a.exe" md5="abc" available="1"/>')
    )
    result = await client.checksum(SIGNIERT + ".xml")

    assert result is not None
    assert result.md5 == "abc"
    assert result.total_size is None


# -- content_length ---------------------------------------------------


async def test_content_length_liest_den_header():
    gesehen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(request.method)
        return httpx.Response(200, headers={"Content-Length": "821824", "Accept-Ranges": "bytes"})

    client, _ = make_client(handler)
    assert await client.content_length(SIGNIERT) == 821824
    assert gesehen == ["HEAD"]


async def test_content_length_ohne_header_ist_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Accept-Ranges": "bytes"})

    client, _ = make_client(handler)
    assert await client.content_length(SIGNIERT) is None


async def test_content_length_bei_404_ist_none():
    client, _ = make_client(lambda request: httpx.Response(404, text=""))
    assert await client.content_length(SIGNIERT) is None


async def test_content_length_ohne_url_ist_none():
    client, _ = make_client(lambda request: pytest.fail("es darf kein Request erfolgen"))
    assert await client.content_length("") is None


async def test_content_length_reicht_429_durch():
    client, _ = make_client(
        lambda request: httpx.Response(429, headers={"Retry-After": "5"}, text=""),
        max_retries=0,
    )
    with pytest.raises(RateLimitError):
        await client.content_length(SIGNIERT)


async def test_content_length_reicht_401_durch():
    client, _ = make_client(lambda request: httpx.Response(401, text=""))
    with pytest.raises(AuthError):
        await client.content_length(SIGNIERT)


# -- checksum ---------------------------------------------------------

CHECKSUM_XML = (
    '<file name="setup_spiel_2.1.0.9.exe" available="1" notavailablemsg="" '
    'md5="0123456789abcdef0123456789abcdef" chunks="2" timestamp="2024-01-01 00:00:00" '
    'total_size="3000">'
    '<chunk from="0" to="1499" method="md5">aaa</chunk>'
    '<chunk from="1500" to="2999" method="md5">bbb</chunk>'
    "</file>"
)


async def test_checksum_gueltiges_xml():
    client, _ = make_client(lambda request: httpx.Response(200, text=CHECKSUM_XML))
    result = await client.checksum("https://cdn.gog.com/x/setup.exe.xml")

    assert result is not None
    assert result.filename == "setup_spiel_2.1.0.9.exe"
    assert result.md5 == "0123456789abcdef0123456789abcdef"
    assert result.total_size == 3000


async def test_checksum_404_ist_none():
    client, _ = make_client(lambda request: httpx.Response(404, text="not found"))
    assert await client.checksum("https://cdn.gog.com/x/fehlt.xml") is None


async def test_checksum_muell_xml_ist_none():
    client, _ = make_client(lambda request: httpx.Response(200, text="<file name=kaputt"))
    assert await client.checksum("https://cdn.gog.com/x/kaputt.xml") is None


async def test_checksum_leere_antwort_ist_none():
    client, _ = make_client(lambda request: httpx.Response(200, text="   "))
    assert await client.checksum("https://cdn.gog.com/x/leer.xml") is None


async def test_checksum_ohne_md5_attribut_ist_none():
    client, _ = make_client(lambda request: httpx.Response(200, text='<file name="a.exe"/>'))
    assert await client.checksum("https://cdn.gog.com/x/ohne.xml") is None


async def test_checksum_ohne_url_ist_none():
    client, _ = make_client(lambda request: pytest.fail("es darf kein Request erfolgen"))
    assert await client.checksum("") is None


async def test_checksum_reicht_429_durch():
    """RateLimitError ist ein ApiError - darf trotzdem nicht verschluckt werden."""
    client, _ = make_client(
        lambda request: httpx.Response(429, headers={"Retry-After": "5"}, text=""),
        max_retries=0,
    )
    with pytest.raises(RateLimitError) as excinfo:
        await client.checksum("https://cdn.gog.com/x/setup.exe.xml")
    assert excinfo.value.retry_after == 5.0


async def test_checksum_reicht_401_durch():
    client, _ = make_client(lambda request: httpx.Response(401, text=""))
    with pytest.raises(AuthError):
        await client.checksum("https://cdn.gog.com/x/setup.exe.xml")


# -- Fehlerbehandlung / Retry ----------------------------------------


async def test_401_wird_zu_auth_error():
    client, _ = make_client(lambda request: httpx.Response(401, json={"error": "invalid_token"}))
    with pytest.raises(AuthError):
        await client.user_data()


async def test_404_wird_zu_api_error_mit_status_und_url():
    client, _ = make_client(lambda request: httpx.Response(404, text=""))
    with pytest.raises(ApiError) as excinfo:
        await client.product_files(999)
    assert "404" in str(excinfo.value)
    assert "gameDetails/999.json" in str(excinfo.value)


async def test_429_wird_zu_rate_limit_error_mit_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7"}, text="")

    client, slept = make_client(handler, max_retries=2)
    with pytest.raises(RateLimitError) as excinfo:
        await client.user_data()

    assert excinfo.value.retry_after == 7.0
    assert isinstance(excinfo.value, ApiError)
    # Retry-After wird respektiert, nicht der exponentielle Backoff
    assert slept == [7.0, 7.0]


async def test_429_danach_erfolg():
    versuche = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        versuche["n"] += 1
        if versuche["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, text="")
        return json_response({"username": "t", "userId": "1"})

    client, slept = make_client(handler)
    user = await client.user_data()

    assert user.username == "t"
    assert slept == [3.0]


async def test_500_wird_wiederholt_und_danach_erfolgreich():
    versuche = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        versuche["n"] += 1
        if versuche["n"] < 3:
            return httpx.Response(500, text="boom")
        return json_response({"username": "tobias", "userId": "1", "isLoggedIn": True})

    client, slept = make_client(handler)
    user = await client.user_data()

    assert user.username == "tobias"
    assert versuche["n"] == 3
    assert slept == [0.5, 1.0]


async def test_500_gibt_nach_max_retries_auf():
    versuche = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        versuche["n"] += 1
        return httpx.Response(503, text="")

    client, slept = make_client(handler, max_retries=3)
    with pytest.raises(ApiError) as excinfo:
        await client.user_data()

    assert versuche["n"] == 4
    assert len(slept) == 3
    assert "503" in str(excinfo.value)


async def test_netzwerkfehler_wird_wiederholt():
    versuche = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        versuche["n"] += 1
        if versuche["n"] < 2:
            raise httpx.ConnectError("kein Netz", request=request)
        return json_response({"username": "t", "userId": "1"})

    client, slept = make_client(handler)
    user = await client.user_data()

    assert user.username == "t"
    assert versuche["n"] == 2
    assert slept == [0.5]


async def test_token_wird_pro_request_neu_geholt():
    auth = FakeAuth()

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return json_response(LIBRARY_PAGES[page])

    client, _ = make_client(handler, auth=auth)
    await client.library()

    assert auth.calls == 3
