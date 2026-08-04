"""Tests für gogdl.api gegen ``httpx.MockTransport``.

Die Fixtures bilden die GOG-Payloads als JSON-Strings ab; jeder Test
beschreibt genau ein Verhalten aus KONZEPT.md §3/§4.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gogdl.api import GogApiClient
from gogdl.constants import API_BASE
from gogdl.errors import ApiError, AuthError, RateLimitError
from gogdl.model.protocols import GogApi
from gogdl.model.types import FileKind, OsName, SlotKey

# -- Hilfsmittel ------------------------------------------------------


class FakeAuth:
    """Minimaler AuthProvider — liefert immer dasselbe Token."""

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


PRODUCT_BASE = json.loads(
    """
{
  "id": 1207658930,
  "title": "Beispielspiel",
  "downloads": {
    "installers": [],
    "patches": [],
    "bonus_content": []
  },
  "expanded_dlcs": []
}
"""
)


def product_payload(**overrides):
    """Produkt-Payload aus der Basis plus gezielten Ergänzungen."""
    payload = json.loads(json.dumps(PRODUCT_BASE))
    downloads = overrides.pop("downloads", None)
    if downloads is not None:
        payload["downloads"].update(downloads)
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


# -- product_files ----------------------------------------------------

MULTIPART_INSTALLER = """
[{"id": "en1installer0", "os": "windows", "language": "en", "version": "2.1.0.9",
  "total_size": 3000,
  "files": [
    {"id": "en1installer0", "size": 2000,
     "downlink": "https://api.gog.com/products/1207658930/downlink/installer/en1installer0"},
    {"id": "en1installer1", "size": 1000,
     "downlink": "https://api.gog.com/products/1207658930/downlink/installer/en1installer1"}
  ]}]
"""


async def test_mehrteiliger_installer_teilt_einen_slot():
    payload = product_payload(downloads={"installers": json.loads(MULTIPART_INSTALLER)})
    client, _ = make_client(lambda request: json_response(payload))

    files = await client.product_files(1207658930)

    assert len(files) == 2
    erwartet = SlotKey(1207658930, FileKind.INSTALLER, OsName.WINDOWS, "en")
    assert {f.slot for f in files} == {erwartet}
    assert [f.part_index for f in files] == [1, 2]
    assert [f.total_parts for f in files] == [2, 2]
    assert [f.file_id for f in files] == ["en1installer0", "en1installer1"]
    assert [f.size for f in files] == [2000, 1000]
    assert all(f.version == "2.1.0.9" for f in files)


async def test_expand_parameter_wird_gesetzt():
    gesehen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(request.url.params["expand"])
        assert str(request.url).startswith(f"{API_BASE}/products/42")
        return json_response(product_payload())

    client, _ = make_client(handler)
    await client.product_files(42)
    assert gesehen == ["downloads,expanded_dlcs"]


TWO_LANGUAGES = """
[{"id": "en1installer0", "os": "windows", "language": "en", "version": "2.1",
  "files": [{"id": "en1installer0", "size": 10, "downlink": "/dl/en"}]},
 {"id": "de1installer0", "os": "windows", "language": "de", "version": "2.1",
  "files": [{"id": "de1installer0", "size": 11, "downlink": "/dl/de"}]}]
"""


async def test_zwei_sprachen_ergeben_zwei_slots():
    payload = product_payload(downloads={"installers": json.loads(TWO_LANGUAGES)})
    client, _ = make_client(lambda request: json_response(payload))

    files = await client.product_files(1207658930)

    assert len(files) == 2
    assert {f.slot for f in files} == {
        SlotKey(1207658930, FileKind.INSTALLER, OsName.WINDOWS, "en"),
        SlotKey(1207658930, FileKind.INSTALLER, OsName.WINDOWS, "de"),
    }
    assert all(f.total_parts == 1 and f.part_index == 1 for f in files)


PATCHES = """
[{"id": "en1patch0", "os": "linux", "language": "en", "version": "2.0_to_2.1",
  "files": [{"id": "en1patch0", "size": 500, "downlink": "/dl/patch"}]}]
"""


async def test_patches_werden_als_patch_kind_erfasst():
    payload = product_payload(downloads={"patches": json.loads(PATCHES)})
    client, _ = make_client(lambda request: json_response(payload))

    (file,) = await client.product_files(1207658930)
    assert file.slot == SlotKey(1207658930, FileKind.PATCH, OsName.LINUX, "en")
    assert file.version == "2.0_to_2.1"


BONUS_CONTENT = """
[{"id": 12345, "name": "manual", "type": "manuals", "count": 1, "total_size": 700,
  "files": [{"id": 12345, "size": 700, "downlink": "/dl/manual"}]}]
"""


async def test_extra_ohne_version():
    payload = product_payload(downloads={"bonus_content": json.loads(BONUS_CONTENT)})
    client, _ = make_client(lambda request: json_response(payload))

    (file,) = await client.product_files(1207658930)
    assert file.slot == SlotKey(1207658930, FileKind.EXTRA, None, None)
    assert file.slot.os is None and file.slot.language is None
    assert file.version is None
    assert file.file_id == "12345"
    assert file.size == 700


DLC_PAYLOAD = """
[{"id": 555, "title": "Beispielspiel DLC",
  "downloads": {"installers": [
    {"id": "en1installer0", "os": "mac", "language": "en", "version": "1.0",
     "files": [{"id": "dlc_file", "size": 99, "downlink": "/dl/dlc"}]}]},
  "expanded_dlcs": []}]
"""


async def test_dlc_wird_rekursiv_mit_eigener_produkt_id_erfasst():
    payload = product_payload(
        downloads={"installers": json.loads(TWO_LANGUAGES)},
        expanded_dlcs=json.loads(DLC_PAYLOAD),
    )
    client, _ = make_client(lambda request: json_response(payload))

    files = await client.product_files(1207658930, include_dlc=True)

    dlc_files = [f for f in files if f.product_id == 555]
    assert len(dlc_files) == 1
    assert dlc_files[0].slot == SlotKey(555, FileKind.INSTALLER, OsName.MAC, "en")
    assert dlc_files[0].file_id == "dlc_file"
    assert len(files) == 3


async def test_dlc_wird_bei_include_dlc_false_ausgelassen():
    payload = product_payload(
        downloads={"installers": json.loads(TWO_LANGUAGES)},
        expanded_dlcs=json.loads(DLC_PAYLOAD),
    )
    client, _ = make_client(lambda request: json_response(payload))

    files = await client.product_files(1207658930, include_dlc=False)

    assert all(f.product_id == 1207658930 for f in files)
    assert len(files) == 2


NESTED_DLC = """
[{"id": 555, "title": "DLC", "downloads": {"installers": []},
  "expanded_dlcs": [
    {"id": 556, "title": "DLC im DLC",
     "downloads": {"installers": [
       {"id": "x", "os": "windows", "language": "en", "version": "1.0",
        "files": [{"id": "nested", "size": 5, "downlink": "/dl/nested"}]}]},
     "expanded_dlcs": []}]}]
"""


async def test_dlc_rekursion_geht_in_die_tiefe():
    payload = product_payload(expanded_dlcs=json.loads(NESTED_DLC))
    client, _ = make_client(lambda request: json_response(payload))

    (file,) = await client.product_files(1207658930)
    assert file.product_id == 556
    assert file.file_id == "nested"


UNKNOWN_OS = """
[{"id": "amiga0", "os": "amiga", "language": "en", "version": "1.0",
  "files": [{"id": "amiga_file", "size": 1, "downlink": "/dl/amiga"}]},
 {"id": "en1installer0", "os": "windows", "language": "en", "version": "1.0",
  "files": [{"id": "win_file", "size": 2, "downlink": "/dl/win"}]}]
"""


async def test_unbekanntes_os_wird_uebersprungen(caplog):
    payload = product_payload(downloads={"installers": json.loads(UNKNOWN_OS)})
    client, _ = make_client(lambda request: json_response(payload))

    with caplog.at_level("WARNING"):
        files = await client.product_files(1207658930)

    assert [f.file_id for f in files] == ["win_file"]
    assert any("amiga" in record.getMessage() for record in caplog.records)


async def test_unbekanntes_os_warnt_nur_einmal(caplog):
    payload = product_payload(downloads={"installers": json.loads(UNKNOWN_OS)})
    client, _ = make_client(lambda request: json_response(payload))

    with caplog.at_level("WARNING"):
        await client.product_files(1207658930)
        await client.product_files(1207658930)

    treffer = [r for r in caplog.records if "Betriebssystem" in r.getMessage()]
    assert len(treffer) == 1


async def test_produkt_ohne_downloads_liefert_leere_liste():
    client, _ = make_client(lambda request: json_response('{"id": 1, "title": "leer"}'))
    assert await client.product_files(1) == []


# -- resolve_downlink -------------------------------------------------


async def test_resolve_downlink_extrahiert_dateinamen():
    signed = (
        "https://gog-cdn-lumen.secure2.footprint.net/token/x/setup_the_witcher_2.1.0.9.exe"
        "?token=abc&expires=1700000000"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{API_BASE}/products/1/downlink/installer/en1installer0"
        return json_response(
            {"downlink": signed, "checksum": "https://cdn.gog.com/x/setup.exe.xml"}
        )

    client, _ = make_client(handler)
    link = await client.resolve_downlink("/products/1/downlink/installer/en1installer0")

    assert link.url == signed
    assert link.filename == "setup_the_witcher_2.1.0.9.exe"
    assert link.checksum_url == "https://cdn.gog.com/x/setup.exe.xml"


async def test_resolve_downlink_dekodiert_dateinamen():
    signed = "https://cdn.gog.com/token/setup%20spiel%20%282%29.bin?token=a%2Fb&x=1"
    client, _ = make_client(lambda request: json_response({"downlink": signed, "checksum": ""}))

    link = await client.resolve_downlink("https://api.gog.com/products/1/downlink/x")

    assert link.filename == "setup spiel (2).bin"
    assert link.checksum_url is None


async def test_resolve_downlink_akzeptiert_absoluten_link():
    absolut = "https://api.gog.com/products/1/downlink/installer/en1installer0"
    gesehen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(str(request.url))
        return json_response({"downlink": "https://cdn.gog.com/a/b/file.bin"})

    client, _ = make_client(handler)
    link = await client.resolve_downlink(absolut)

    assert gesehen == [absolut]
    assert link.filename == "file.bin"
    assert link.checksum_url is None


async def test_resolve_downlink_ohne_downlink_ist_api_error():
    client, _ = make_client(lambda request: json_response({"checksum": "x"}))
    with pytest.raises(ApiError):
        await client.resolve_downlink("/products/1/downlink/x")


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
    """RateLimitError ist ein ApiError — darf trotzdem nicht verschluckt werden."""
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
    assert "products/999" in str(excinfo.value)


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
