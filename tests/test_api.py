"""Tests für gogdl.api gegen ``httpx.MockTransport``.

Die Fixtures bilden die echten GOG-Payloads als JSON-Strings ab; jeder
Test beschreibt genau ein Verhalten aus KONZEPT.md §3/§4.

Es gibt zwei Wege, und beide werden geprüft:

* Primaerweg ``api.gog.com/products/{id}?expand=downloads,expanded_dlcs``
  mit vier Kategorien und einem ``checksum``-Feld in der Downlink-Antwort.
  Handler dafür: ``api_handler``. Seine ``size`` ist auf volle MiB
  gerundet und darf nie als Bytewert durchgereicht werden.
* Rueckfallweg ``embed.gog.com/account/gameDetails/{id}.json`` plus ein
  302 auf jede ``manualUrl``. Handler dafür: ``details_handler``, der den
  Produktabruf mit 404 beantwortet und damit den Rückfall erzwingt.

Der frühere Befund, ``api.gog.com`` sei kaputt, war eine Fehldeutung: der
404 kam von einem einzelnen Produkt ohne Downloadrechte.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gogdl.api import GogApiClient
from gogdl.api.client import language_code, language_fallback
from gogdl.constants import API_BASE, EMBED_BASE
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


def ist_produktabruf(request: httpx.Request) -> bool:
    """Trifft der Request den Primaerweg ``api.gog.com/products/{id}``?"""
    return request.url.host == "api.gog.com" and request.url.path.startswith("/products/")


def details_handler(payload_or_factory):
    """Handler, der nur gameDetails bedient; der Produktabruf antwortet 404.

    Das ist der Rueckfallweg. Er wird ausdruecklich erzwungen, damit die
    Tests des gameDetails-Wegs auch wirklich diesen Weg pruefen und nicht
    nebenbei am Primaerweg vorbeirutschen. Ein 404 auf den Produktabruf
    ist der Normalfall fuer ein Produkt ohne Downloadrechte.

    ``payload_or_factory`` ist entweder eine feste Payload oder eine
    parameterlose Funktion, die je Aufruf eine neue liefert.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if ist_produktabruf(request):
            return json_response({"message": "not found"}, status=404)
        if callable(payload_or_factory):
            return json_response(payload_or_factory())
        return json_response(payload_or_factory)

    return handler


# -- api.gog.com/products: Payload-Bausteine ---------------------------


def api_file(file_id: str, size: int, *, produkt: int = 1104118179, art: str = "installer"):
    """Ein Eintrag aus ``downloads.*[].files[]``."""
    return {
        "id": file_id,
        "size": size,
        "downlink": f"https://api.gog.com/products/{produkt}/downlink/{art}/{file_id}",
    }


def api_payload(product_id: int = 1104118179, **downloads):
    """Produkt-Payload mit den vier Kategorien; fehlende bleiben leer."""
    return {
        "id": product_id,
        "title": "15 Days",
        "downloads": {
            "installers": downloads.get("installers", []),
            "patches": downloads.get("patches", []),
            "language_packs": downloads.get("language_packs", []),
            "bonus_content": downloads.get("bonus_content", []),
        },
        "expanded_dlcs": downloads.get("expanded_dlcs", []),
    }


def api_handler(payload, *, details=None):
    """Handler, der den Produktabruf bedient; gameDetails ist optional."""

    def handler(request: httpx.Request) -> httpx.Response:
        if ist_produktabruf(request):
            return json_response(payload)
        if details is None:
            raise AssertionError(f"Unexpected gameDetails request: {request.url}")
        return json_response(details)

    return handler


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


async def test_produktabruf_kommt_zuerst_dann_game_details():
    """Reihenfolge der Wege: erst api.gog.com, bei 404 erst gameDetails."""
    gesehen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer token-123"
        if ist_produktabruf(request):
            return json_response({"message": "not found"}, status=404)
        return json_response(details_payload())

    client, _ = make_client(handler)
    await client.product_files(42)

    assert len(gesehen) == 2
    assert gesehen[0].startswith(f"{API_BASE}/products/42?")
    assert "expand=downloads" in gesehen[0]
    assert gesehen[1] == f"{EMBED_BASE}/account/gameDetails/42.json"


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
    client, _ = make_client(details_handler(payload))

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
    client, _ = make_client(details_handler(payload))

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
    client, _ = make_client(details_handler(payload))

    files = await client.product_files(1207658930)
    assert all(f.size is None for f in files)


LEERE_VERSION = """
[["English", {"windows": [
  {"manualUrl": "/downloads/x/en1installer0", "name": "X", "version": "", "size": "1 MB"}
]}]]
"""


async def test_leere_version_wird_zu_none():
    payload = details_payload(downloads=json.loads(LEERE_VERSION))
    client, _ = make_client(details_handler(payload))

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
    client, _ = make_client(details_handler(payload))

    with caplog.at_level("WARNING"):
        files = await client.product_files(1207658930)

    assert [f.slot.language for f in files] == ["en", "klingon"]
    assert any("Klingon" in record.getMessage() for record in caplog.records)


async def test_unbekannte_sprache_warnt_nur_einmal(caplog):
    payload = details_payload(downloads=json.loads(UNBEKANNTE_SPRACHE))
    client, _ = make_client(details_handler(payload))

    with caplog.at_level("WARNING"):
        await client.product_files(1207658930)
        await client.product_files(1207658930)

    treffer = [r for r in caplog.records if "Unknown language" in r.getMessage()]
    assert len(treffer) == 1


UNBEKANNTE_PLATTFORM = """
[["English", {
  "amiga": [{"manualUrl": "/downloads/x/am1installer0", "version": "1.0"}],
  "windows": [{"manualUrl": "/downloads/x/en1installer0", "version": "1.0"}]
}]]
"""


async def test_unbekannte_plattform_wird_uebersprungen(caplog):
    payload = details_payload(downloads=json.loads(UNBEKANNTE_PLATTFORM))
    client, _ = make_client(details_handler(payload))

    with caplog.at_level("WARNING"):
        files = await client.product_files(1207658930)

    assert [f.file_id for f in files] == ["en1installer0"]
    assert any("amiga" in record.getMessage() for record in caplog.records)


async def test_unbekannte_plattform_warnt_nur_einmal(caplog):
    payload = details_payload(downloads=json.loads(UNBEKANNTE_PLATTFORM))
    client, _ = make_client(details_handler(payload))

    with caplog.at_level("WARNING"):
        await client.product_files(1207658930)
        await client.product_files(1207658930)

    treffer = [r for r in caplog.records if "operating system" in r.getMessage()]
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
    client, _ = make_client(details_handler(payload))

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
    client, _ = make_client(details_handler(payload))

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
    client, _ = make_client(details_handler(lambda: payloads.pop(0)))

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
    client, _ = make_client(details_handler(payload))

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
    client, _ = make_client(details_handler(payload))

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
    client, _ = make_client(details_handler(payload))

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
    client, _ = make_client(details_handler(payload))

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
    client, _ = make_client(details_handler(payload))

    with caplog.at_level("WARNING"):
        (file,) = await client.product_files(1207658930)

    assert file.product_id == 1207658930
    # Die Zugehörigkeit bleibt trotzdem erkennbar.
    assert file.dlc_of == 1207658930
    assert file.is_dlc is True
    assert any("without an id of its own" in record.getMessage() for record in caplog.records)


async def test_produkt_ohne_downloads_liefert_leere_liste():
    client, _ = make_client(details_handler('{"title": "leer"}'))
    assert await client.product_files(1) == []


# -- product_files über api.gog.com/products --------------------------


def api_installer(file_id: str, size: int, *, os_name="windows", lang="en", version="1.0"):
    """Ein Eintrag aus ``downloads.installers[]`` mit genau einem Teil."""
    return {
        "id": file_id,
        "name": "15 Days",
        "os": os_name,
        "language": lang,
        "language_full": "English",
        "version": version,
        "total_size": size,
        "files": [api_file(file_id, size)],
    }


async def test_produktabruf_gibt_die_gerundete_groesse_nicht_als_bytewert_aus():
    """Die ``size`` der Produkt-Payload ist auf volle MiB gerundet.

    Live gemessen an einer Datei: die Payload nennt 1048576, Content-Length
    und ``total_size`` des Checksum-XML nennen beide 821824. Wer den
    gerundeten Wert als ``RemoteFile.size`` durchreicht, füllt das Manifest
    mit Werten, an denen jede spätere Größenprüfung scheitert - der Import
    verwirft dann jede Datei mit "size 821824 instead of 1048576".

    Ein Request, keine Auflösung, keine Kopfanfrage - und trotzdem keine
    erfundene Größe.
    """
    gesehen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(request.method)
        assert ist_produktabruf(request)
        return json_response(api_payload(installers=[api_installer("en1installer0", 1048576)]))

    client, _ = make_client(handler)
    (file,) = await client.product_files(1104118179)

    assert gesehen == ["GET"]
    assert file.size is None, "1048576 ist gerundet und darf nie im Manifest landen"
    assert file.version == "1.0"
    assert file.slot == SlotKey(1104118179, FileKind.INSTALLER, OsName.WINDOWS, "en")
    assert file.slot.variant is None
    assert file.file_id == "en1installer0"
    assert file.downlink == (
        "https://api.gog.com/products/1104118179/downlink/installer/en1installer0"
    )


async def test_produktabruf_mehrteiliger_installer_teilt_einen_slot():
    """Ein Eintrag ist ein Slot, seine ``files`` sind die Teile - §5.5."""
    eintrag = {
        "id": "en1installer0",
        "os": "windows",
        "language": "en",
        "version": "2.0",
        "total_size": 9_000_000,
        "files": [
            api_file("en1installer0", 4_000_000),
            api_file("en1installer1", 4_000_000),
            api_file("en1installer2", 1_000_000),
        ],
    }
    client, _ = make_client(api_handler(api_payload(installers=[eintrag])))

    files = await client.product_files(1104118179)

    assert len({f.slot for f in files}) == 1
    assert [f.part_index for f in files] == [1, 2, 3]
    assert [f.total_parts for f in files] == [3, 3, 3]
    # Keine Größe aus der Payload - weder je Teil noch das total_size des
    # Eintrags, das für jeden einzelnen Teil ohnehin falsch wäre.
    assert [f.size for f in files] == [None, None, None]
    assert [f.file_id for f in files] == ["en1installer0", "en1installer1", "en1installer2"]


PATCHES_GLEICHE_SPRACHE = [
    {
        "id": "en1patch0",
        "name": "Patch 2.0 to 2.1",
        "os": "windows",
        "language": "en",
        "version": "2.0 to 2.1",
        "files": [api_file("en1patch0", 111, art="patch")],
    },
    {
        "id": "en1patch1",
        "name": "Patch 2.1 to 2.2",
        "os": "windows",
        "language": "en",
        "version": "2.1 to 2.2",
        "files": [api_file("en1patch1", 222, art="patch")],
    },
]


async def test_patches_gleicher_sprache_trennen_sich_ueber_die_version():
    """GOG bietet pro os/lang mehrere Versionsspannen an - ohne ``variant``
    fielen sie in einen Slot und wären fürs Aufräumen eine Auslieferung."""
    client, _ = make_client(api_handler(api_payload(patches=PATCHES_GLEICHE_SPRACHE)))

    files = await client.product_files(1104118179)

    assert [f.slot.kind for f in files] == [FileKind.PATCH, FileKind.PATCH]
    assert [f.slot.variant for f in files] == ["2.0-to-2.1", "2.1-to-2.2"]
    assert len({f.slot for f in files}) == 2
    assert all(f.slot.os is OsName.WINDOWS and f.slot.language == "en" for f in files)
    assert [f.size for f in files] == [None, None]


async def test_patch_ohne_version_faellt_auf_die_id_zurueck():
    eintrag = {
        "id": "en1patch0",
        "os": "windows",
        "language": "en",
        "version": "",
        "files": [api_file("en1patch0", 1, art="patch")],
    }
    client, _ = make_client(api_handler(api_payload(patches=[eintrag])))

    (file,) = await client.product_files(1104118179)
    assert file.slot.variant == "en1patch0"
    assert file.version is None


LANGUAGE_PACKS = [
    {
        "id": "de1langpack0",
        "name": "German language pack",
        "os": "windows",
        "language": "de",
        "version": "1.0",
        "files": [api_file("de1langpack0", 500, art="language_pack")],
    }
]


async def test_language_packs_landen_als_patch_mit_eigenem_praefix():
    """``language_packs`` sind PATCH, nicht EXTRA.

    Entscheidend ist die Sprache im Slot: ``sync/planner.py`` filtert nur
    Slots mit gesetztem ``language``. Als EXTRA (os/language = None) käme
    das Sprachpaket jeder Sprache durch den Filter.

    Das Präfix trennt ein Sprachpaket von einem echten Patch derselben
    Versionsangabe - sonst teilten sich beide einen Slot.
    """
    patch = {
        "id": "de1patch0",
        "os": "windows",
        "language": "de",
        "version": "1.0",
        "files": [api_file("de1patch0", 7, art="patch")],
    }
    client, _ = make_client(
        api_handler(api_payload(patches=[patch], language_packs=LANGUAGE_PACKS))
    )

    files = await client.product_files(1104118179)

    slots = {f.file_id: f.slot for f in files}
    assert slots["de1patch0"] == SlotKey(1104118179, FileKind.PATCH, OsName.WINDOWS, "de", "1.0")
    assert slots["de1langpack0"] == SlotKey(
        1104118179, FileKind.PATCH, OsName.WINDOWS, "de", "langpack-1.0"
    )
    assert len(set(slots.values())) == 2
    # Die Sprache ist gesetzt - nur so greift der Sprachfilter der Planung.
    assert all(f.slot.language == "de" for f in files)


BONUS_CONTENT = [
    {
        "id": 61,
        "name": "Handbuch (PDF)",
        "type": "manuals",
        "count": 1,
        "total_size": 12,
        "files": [api_file("extra0", 12, art="bonus_content")],
    },
    {
        "id": 62,
        "name": "Game Soundtrack",
        "type": "audio",
        "count": 2,
        "total_size": 30,
        "files": [
            api_file("extra1", 20, art="bonus_content"),
            api_file("extra2", 10, art="bonus_content"),
        ],
    },
]


async def test_bonus_content_wird_zu_extras_mit_stabilem_variant():
    client, _ = make_client(api_handler(api_payload(bonus_content=BONUS_CONTENT)))

    files = await client.product_files(1104118179)

    assert all(f.slot.kind is FileKind.EXTRA for f in files)
    assert all(f.slot.os is None and f.slot.language is None for f in files)
    assert {f.slot for f in files} == {
        SlotKey(1104118179, FileKind.EXTRA, None, None, "handbuch-pdf"),
        SlotKey(1104118179, FileKind.EXTRA, None, None, "game-soundtrack"),
    }
    # Der Soundtrack hat zwei Teile und bleibt trotzdem ein Slot.
    soundtrack = [f for f in files if f.slot.variant == "game-soundtrack"]
    assert [f.part_index for f in soundtrack] == [1, 2]
    # Auch bei bonus_content ist die Größe der Payload gerundet und bleibt
    # liegen; sie kommt später aus dem Checksum-XML.
    assert [f.size for f in soundtrack] == [None, None]


async def test_bonus_content_ohne_namen_faellt_auf_den_typ_zurueck():
    eintrag = {
        "id": 61,
        "type": "manuals",
        "files": [api_file("extra0", 1, art="bonus_content")],
    }
    client, _ = make_client(api_handler(api_payload(bonus_content=[eintrag])))

    (file,) = await client.product_files(1104118179)
    assert file.slot.variant == "manuals"


async def test_expanded_dlcs_werden_rekursiv_erfasst():
    dlc = {
        "id": 555,
        "title": "15 Days DLC",
        "downloads": {
            "installers": [api_installer("en2installer0", 5000, os_name="mac")],
            "patches": [],
            "language_packs": [],
            "bonus_content": [],
        },
        "expanded_dlcs": [],
    }
    client, _ = make_client(
        api_handler(
            api_payload(
                installers=[api_installer("en1installer0", 1000)], expanded_dlcs=[dlc]
            )
        )
    )

    files = await client.product_files(1104118179)

    (haupt,) = [f for f in files if f.product_id == 1104118179]
    (dlc_file,) = [f for f in files if f.product_id == 555]
    assert haupt.dlc_of is None
    assert dlc_file.dlc_of == 1104118179
    assert dlc_file.slot == SlotKey(555, FileKind.INSTALLER, OsName.MAC, "en")


async def test_expanded_dlcs_werden_bei_include_dlc_false_ausgelassen():
    dlc = {
        "id": 555,
        "downloads": {"installers": [api_installer("en2installer0", 5000, os_name="mac")]},
    }
    client, _ = make_client(
        api_handler(
            api_payload(
                installers=[api_installer("en1installer0", 1000)], expanded_dlcs=[dlc]
            )
        )
    )

    files = await client.product_files(1104118179, include_dlc=False)
    assert [f.product_id for f in files] == [1104118179]


async def test_sprachcode_aus_dem_produktabruf_wird_kleingeschrieben():
    """Ein Code mit Großbuchstaben machte neben dem gleichlautenden Code des
    Rückfallwegs einen zweiten Slot auf."""
    eintrag = api_installer("en1installer0", 1, lang="EN")
    client, _ = make_client(api_handler(api_payload(installers=[eintrag])))

    (file,) = await client.product_files(1104118179)
    assert file.slot.language == "en"


async def test_beide_wege_ergeben_denselben_slot_und_dieselbe_file_id():
    """Die Bruchstelle, falls sie je auseinanderliefen: ``_enrich``
    schlüsselt auf ``(slot, file_id)`` und der Store auf ``slot.as_str()``.
    Ein Wegwechsel dürfte nie die ganze Bibliothek als neu erscheinen
    lassen."""
    ueber_api = api_payload(
        1207658930,
        installers=[api_installer("en1installer0", 821824)],
        bonus_content=[
            {
                "id": 61,
                "name": "Handbuch (PDF)",
                "type": "manuals",
                "files": [api_file("extra0", 12, art="bonus_content")],
            }
        ],
    )
    ueber_details = details_payload(
        downloads=[
            [
                "English",
                {
                    "windows": [
                        {
                            "manualUrl": "/downloads/15_days/en1installer0",
                            "name": "15 Days",
                            "version": "1.0",
                            "size": "800 MB",
                        }
                    ]
                },
            ]
        ],
        extras=[{"manualUrl": "/downloads/15_days/extra0", "name": "Handbuch (PDF)"}],
    )

    client_a, _ = make_client(api_handler(ueber_api))
    client_b, _ = make_client(details_handler(ueber_details))

    von_api = await client_a.product_files(1207658930)
    von_details = await client_b.product_files(1207658930)

    assert {(f.slot.as_str(), f.file_id) for f in von_api} == {
        (f.slot.as_str(), f.file_id) for f in von_details
    }
    # Beide Wege lassen die Größe offen: keiner nennt eine exakte.
    assert all(f.size is None for f in von_api)
    assert all(f.size is None for f in von_details)


async def test_produktabruf_404_faellt_auf_game_details_zurueck():
    """404 heißt "dieses Konto hat hier keine Downloadrechte", nicht "kaputt"."""
    details = details_payload(downloads=json.loads(DREITEILIGER_INSTALLER))
    client, _ = make_client(details_handler(details))

    files = await client.product_files(1207658930)

    assert len(files) == 3
    assert [f.file_id for f in files] == ["en1installer0", "en1installer1", "en1installer2"]


async def test_produktabruf_ohne_dateien_faellt_auf_game_details_zurueck():
    """200, aber alle vier Kategorien leer - der Rückfall greift trotzdem."""
    details = details_payload(downloads=json.loads(DREITEILIGER_INSTALLER))
    gesehen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(str(request.url))
        if ist_produktabruf(request):
            return json_response(api_payload(1207658930))
        return json_response(details)

    client, _ = make_client(handler)
    files = await client.product_files(1207658930)

    assert len(gesehen) == 2
    assert len(files) == 3


async def test_produktabruf_reicht_401_durch():
    """Ein abgelaufenes Login darf nicht als "keine Dateien" durchrutschen
    und stillschweigend in den Rückfallweg führen."""
    client, _ = make_client(lambda request: httpx.Response(401, text="denied"))
    with pytest.raises(AuthError):
        await client.product_files(1207658930)


async def test_produktabruf_reicht_429_durch():
    client, _ = make_client(
        lambda request: httpx.Response(429, headers={"Retry-After": "7"}), max_retries=0
    )
    with pytest.raises(RateLimitError):
        await client.product_files(1207658930)


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


CHECKSUM_FELD = "https://gog-cdn-fastly.gog.com/token=nva0000/secure/offline/pruefsumme.xml"


async def test_resolve_downlink_nimmt_checksum_aus_der_json_antwort():
    """Der Primaerweg nennt die Adresse selbst - kein Zusammenbauen mehr.

    Genau die Konstruktion aus signierter URL plus ``.xml`` ist bei
    lgogdownloader gebrochen, als GOG das Adressformat änderte.
    """
    api_downlink = "https://api.gog.com/products/1104118179/downlink/installer/en1installer0"
    gesehen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(str(request.url))
        return json_response({"downlink": SIGNIERT, "checksum": CHECKSUM_FELD})

    client, _ = make_client(handler)
    link = await client.resolve_downlink(api_downlink)

    assert gesehen == [api_downlink]
    assert link.url == SIGNIERT
    assert link.filename == "setup_15_days_1.0_(19285).exe"
    assert link.checksum_url == CHECKSUM_FELD
    # Nicht die zusammengebaute Adresse.
    assert link.checksum_url != SIGNIERT + ".xml"


async def test_resolve_downlink_ohne_checksum_feld_baut_die_xml_adresse():
    client, _ = make_client(lambda request: json_response({"downlink": SIGNIERT}))
    link = await client.resolve_downlink("/products/1/downlink/installer/en1installer0")

    assert link.url == SIGNIERT
    assert link.checksum_url == SIGNIERT + ".xml"


async def test_resolve_downlink_ignoriert_ein_leeres_checksum_feld():
    client, _ = make_client(
        lambda request: json_response({"downlink": SIGNIERT, "checksum": "  "})
    )
    link = await client.resolve_downlink("/products/1/downlink/installer/en1installer0")
    assert link.checksum_url == SIGNIERT + ".xml"


async def test_resolve_downlink_deutet_eine_datei_nicht_als_json():
    """Eine 200-Antwort ohne JSON-Content-Type ist die Datei selbst."""
    signiert = "https://gog-cdn-fastly.gog.com/token=x/secure/setup.bin"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"downlink": "gelogen"}',
            headers={"Content-Type": "application/octet-stream"},
        )

    client, _ = make_client(handler)
    link = await client.resolve_downlink(signiert)

    assert link.url == signiert
    assert link.checksum_url == signiert + ".xml"


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


async def test_gerundete_payload_groesse_verliert_gegen_das_checksum_xml():
    """Der Fall, der einmal 7010 Manifest-Einträge verdorben hat.

    Dieselbe Datei, drei Zahlen: die Produkt-Payload nennt 1048576 (auf
    volle MiB gerundet), Content-Length und ``total_size`` des Checksum-XML
    nennen beide 821824. Die api-Schicht darf die gerundete Zahl nirgends
    als Bytegröße ausgeben - sonst scheitert jede spätere Größenprüfung
    ("size 821824 instead of 1048576") und der Import verwirft die Datei.
    """
    payload = api_payload(installers=[api_installer("en1installer0", 1048576)])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".xml"):
            return httpx.Response(
                200,
                text=(
                    '<file name="setup_15_days_1.0_(19285).exe" available="1" '
                    'md5="eed77e8beeb270924d0aabbccddeeff0" chunks="1" '
                    'total_size="821824"/>'
                ),
            )
        if "/downlink/" in request.url.path:
            return json_response({"downlink": SIGNIERT, "checksum": SIGNIERT + ".xml"})
        assert ist_produktabruf(request)
        return json_response(payload)

    client, _ = make_client(handler)
    (file,) = await client.product_files(1104118179)
    link = await client.resolve_downlink(file.downlink)
    pruefsumme = await client.checksum(link.checksum_url)

    assert file.size is None, "die gerundete Payload-Größe darf nicht durchkommen"
    assert pruefsumme is not None
    # Die einzige Bytegröße, die diese Schicht hergibt, ist die echte.
    assert pruefsumme.total_size == 821824


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


# -- serials ----------------------------------------------------------

# GOG liefert manche Schlüssel UTF-16-kodiert aus, ohne das zu
# kennzeichnen: im JSON-String steckt je Zeichen ein Byte, jedes zweite
# ist ein Nullbyte. Genau so entsteht der Wert hier.
UTF16_SCHLUESSEL = "XYZ12-ABC34-DEF56".encode("utf-16-le").decode("latin-1")


async def test_serials_liest_den_schluessel_des_hauptspiels():
    payload = details_payload(cdKey="ABCD-1234-EFGH-5678")
    gesehen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(str(request.url))
        return json_response(payload)

    client, _ = make_client(handler)
    assert await client.serials(1207658930) == {"15 Days": "ABCD-1234-EFGH-5678"}
    # Ein eigener Abruf - der Aufrufer entscheidet, ob er ihn bezahlt.
    assert gesehen == [f"{EMBED_BASE}/account/gameDetails/1207658930.json"]


async def test_serials_repariert_einen_utf16_schluessel():
    assert not UTF16_SCHLUESSEL.isprintable()
    payload = details_payload(cdKey=UTF16_SCHLUESSEL)
    client, _ = make_client(lambda request: json_response(payload))

    assert await client.serials(1207658930) == {"15 Days": "XYZ12-ABC34-DEF56"}


async def test_serials_repariert_auch_bei_ungerader_laenge():
    """Fehlt das letzte Füllbyte, scheitert die Dekodierung ohne Auffüllen."""
    verstuemmelt = UTF16_SCHLUESSEL[:-1]
    assert len(verstuemmelt) % 2 == 1
    payload = details_payload(cdKey=verstuemmelt)
    client, _ = make_client(lambda request: json_response(payload))

    assert await client.serials(1207658930) == {"15 Days": "XYZ12-ABC34-DEF56"}


async def test_serials_laesst_leere_schluessel_weg():
    payload = details_payload(cdKey="   ")
    client, _ = make_client(lambda request: json_response(payload))
    assert await client.serials(1207658930) == {}


async def test_serials_nimmt_die_dlc_schluessel_mit():
    payload = details_payload(
        cdKey="HAUPT-0000",
        dlcs=[
            {"title": "15 Days DLC", "cdKey": "DLC-1111", "dlcs": []},
            {"title": "Ohne Schlüssel", "cdKey": "", "dlcs": []},
            {
                "title": "DLC mit Unter-DLC",
                "cdKey": UTF16_SCHLUESSEL,
                "dlcs": [{"title": "Tiefes DLC", "cdKey": "TIEF-2222", "dlcs": []}],
            },
        ],
    )
    client, _ = make_client(lambda request: json_response(payload))

    assert await client.serials(1207658930) == {
        "15 Days": "HAUPT-0000",
        "15 Days DLC": "DLC-1111",
        "DLC mit Unter-DLC": "XYZ12-ABC34-DEF56",
        "Tiefes DLC": "TIEF-2222",
    }


async def test_serials_ohne_jeden_schluessel_ist_leer():
    client, _ = make_client(lambda request: json_response(details_payload()))
    assert await client.serials(1207658930) == {}


async def test_serials_ohne_titel_nutzt_die_produkt_id():
    payload = details_payload(cdKey="ABCD-1234")
    payload["title"] = ""
    client, _ = make_client(lambda request: json_response(payload))
    assert await client.serials(1207658930) == {"1207658930": "ABCD-1234"}


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
