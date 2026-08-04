"""Tests für das auth-Paket.

Kein Test darf die echte ``~/.config/gog-repo-downloader/auth.json``
anfassen — jeder ``FileCredentialStore`` bekommt deshalb einen Pfad
unterhalb von ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import stat

import httpx
import pytest

from gogdl.auth import FileCredentialStore, GogAuth, extract_code, start_login
from gogdl.constants import GALAXY_CLIENT_ID, TOKEN_URL, login_url
from gogdl.errors import AuthError
from gogdl.model.protocols import AuthProvider, CredentialStore
from gogdl.model.types import Credentials

NOW = 1_700_000_000.0
"""Feste Zeitbasis. Der Ablauf-Margin von Credentials.is_expired beträgt
120 s — Restlaufzeiten müssen also deutlich darüber liegen, um als
gültig zu zählen."""

VALID_CODE = "abcdef0123456789ABCDEF"


def make_store(tmp_path, credentials: Credentials | None = None) -> FileCredentialStore:
    store = FileCredentialStore(tmp_path / "auth.json")
    if credentials is not None:
        store.save(credentials)
    return store


def stored_credentials(*, expires_at: float, access_token: str = "old-access") -> Credentials:
    return Credentials(
        access_token=access_token,
        refresh_token="old-refresh",
        expires_at=expires_at,
        user_id="4711",
        session_id="sess-1",
    )


def token_response(
    *,
    access_token: str = "new-access",
    refresh_token: str = "new-refresh",
    expires_in: int = 3600,
) -> dict:
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "token_type": "bearer",
        "user_id": "4711",
        "session_id": "sess-2",
    }


class Recorder:
    """MockTransport-Handler, der jede Anfrage protokolliert."""

    def __init__(self, response: httpx.Response | None = None, delay: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response
        self._delay = delay

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._delay:
            # Eindeutiger Await-Punkt: ohne Lock käme hier der zweite
            # Aufruf in denselben Refresh hinein.
            await asyncio.sleep(0)
        if self._response is None:
            raise AssertionError("Es hätte kein HTTP-Request stattfinden dürfen")
        return self._response


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# -- Protokoll-Konformität ------------------------------------------------


def test_implementiert_die_protokolle(tmp_path):
    store = make_store(tmp_path)
    assert isinstance(store, CredentialStore)
    assert isinstance(GogAuth(store), AuthProvider)


# -- extract_code ---------------------------------------------------------


def test_extract_code_aus_vollstaendiger_redirect_url():
    url = f"https://embed.gog.com/on_login_success?origin=client&code={VALID_CODE}"
    assert extract_code(url) == VALID_CODE


def test_extract_code_aus_url_mit_weiteren_parametern():
    url = (
        "https://embed.gog.com/on_login_success"
        f"?origin=client&code={VALID_CODE}&state=xyz&layout=client2"
    )
    assert extract_code(url) == VALID_CODE


def test_extract_code_aus_fragment_und_nacktem_code():
    assert extract_code(f"?code={VALID_CODE}") == VALID_CODE
    assert extract_code(f"code={VALID_CODE}") == VALID_CODE
    assert extract_code(VALID_CODE) == VALID_CODE


def test_extract_code_haelt_sonderzeichen_aus_der_url_unveraendert():
    # Steht `code=` explizit davor, wird der Wert nicht gefiltert — nur
    # Prozentkodierung wird aufgelöst, `+` bleibt ein `+`.
    assert extract_code("https://embed.gog.com/on_login_success?code=a+b/c==") == "a+b/c=="
    assert extract_code("https://embed.gog.com/on_login_success?code=a%2Bb") == "a+b"


def test_extract_code_toleriert_whitespace():
    pasted = f"  https://embed.gog.com/on_login_success?origin=client&code={VALID_CODE}\n"
    assert extract_code(pasted) == VALID_CODE
    assert extract_code(f"  {VALID_CODE}  ") == VALID_CODE


@pytest.mark.parametrize(
    "pasted",
    [
        "",
        "   ",
        "https://embed.gog.com/on_login_success?origin=client",
        "https://embed.gog.com/on_login_success?origin=client&code=",
        "kein Code, nur Prosa!",
        "https://auth.gog.com/auth",
    ],
)
def test_extract_code_lehnt_ungueltige_eingabe_ab(pasted):
    with pytest.raises(AuthError):
        extract_code(pasted)


# -- Login-URL ------------------------------------------------------------


def test_start_login_liefert_url_und_oeffnet_optional():
    geoeffnet: list[str] = []

    url = start_login(open_browser=True, opener=geoeffnet.append)
    assert url == login_url()
    assert GALAXY_CLIENT_ID in url
    assert geoeffnet == [url]

    assert start_login(open_browser=False, opener=geoeffnet.append) == url
    assert len(geoeffnet) == 1


# -- access_token ---------------------------------------------------------


async def test_ohne_credentials_verweist_der_fehler_auf_login(tmp_path):
    store = make_store(tmp_path)
    handler = Recorder()
    async with client_for(handler) as client:
        auth = GogAuth(store, client=client, now=lambda: NOW)
        assert auth.is_authenticated() is False
        with pytest.raises(AuthError, match="gogdl login"):
            await auth.access_token()
    assert handler.requests == []


async def test_gueltiges_token_loest_keinen_request_aus(tmp_path):
    store = make_store(tmp_path, stored_credentials(expires_at=NOW + 3600))
    handler = Recorder()
    async with client_for(handler) as client:
        auth = GogAuth(store, client=client, now=lambda: NOW)
        assert await auth.access_token() == "old-access"
        assert await auth.access_token() == "old-access"
    assert handler.requests == []


async def test_abgelaufenes_token_wird_genau_einmal_erneuert_und_persistiert(tmp_path):
    store = make_store(tmp_path, stored_credentials(expires_at=NOW - 1))
    handler = Recorder(httpx.Response(200, json=token_response()))

    async with client_for(handler) as client:
        auth = GogAuth(store, client=client, now=lambda: NOW)
        assert await auth.access_token() == "new-access"
        # Das frische Token gilt noch — kein zweiter Request.
        assert await auth.access_token() == "new-access"

    assert len(handler.requests) == 1
    request = handler.requests[0]
    assert str(request.url).startswith(TOKEN_URL)
    assert request.url.params["grant_type"] == "refresh_token"
    assert request.url.params["refresh_token"] == "old-refresh"
    assert request.url.params["client_id"] == GALAXY_CLIENT_ID

    persistiert = FileCredentialStore(tmp_path / "auth.json").load()
    assert persistiert is not None
    assert persistiert.access_token == "new-access"
    assert persistiert.refresh_token == "new-refresh"
    assert persistiert.expires_at == NOW + 3600


async def test_ablaufzeitpunkt_ist_jetzt_plus_expires_in(tmp_path):
    store = make_store(tmp_path, stored_credentials(expires_at=NOW - 1))
    handler = Recorder(httpx.Response(200, json=token_response(expires_in=1234)))
    async with client_for(handler) as client:
        auth = GogAuth(store, client=client, now=lambda: NOW)
        await auth.access_token()

    assert store.load().expires_at == NOW + 1234


async def test_parallele_aufrufe_loesen_nur_einen_refresh_aus(tmp_path):
    store = make_store(tmp_path, stored_credentials(expires_at=NOW - 1))
    handler = Recorder(httpx.Response(200, json=token_response()), delay=True)

    async with client_for(handler) as client:
        auth = GogAuth(store, client=client, now=lambda: NOW)
        tokens = await asyncio.gather(auth.access_token(), auth.access_token())

    assert tokens == ["new-access", "new-access"]
    assert len(handler.requests) == 1


async def test_abgelehnter_refresh_token_verwirft_die_credentials(tmp_path):
    path = tmp_path / "auth.json"
    store = make_store(tmp_path, stored_credentials(expires_at=NOW - 1))
    handler = Recorder(httpx.Response(400, json={"error": "invalid_grant"}))

    async with client_for(handler) as client:
        auth = GogAuth(store, client=client, now=lambda: NOW)
        with pytest.raises(AuthError, match="gogdl login"):
            await auth.access_token()
        assert auth.is_authenticated() is False

    assert len(handler.requests) == 1
    assert not path.exists()
    assert store.load() is None


async def test_netzwerkfehler_behaelt_den_refresh_token(tmp_path):
    store = make_store(tmp_path, stored_credentials(expires_at=NOW - 1))

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with client_for(handler) as client:
        auth = GogAuth(store, client=client, now=lambda: NOW)
        with pytest.raises(AuthError):
            await auth.access_token()

    behalten = store.load()
    assert behalten is not None
    assert behalten.refresh_token == "old-refresh"


# -- exchange_code --------------------------------------------------------


async def test_exchange_code_speichert_refresh_token(tmp_path):
    store = make_store(tmp_path)
    handler = Recorder(httpx.Response(200, json=token_response()))

    async with client_for(handler) as client:
        auth = GogAuth(store, client=client, now=lambda: NOW)
        credentials = await auth.exchange_code(VALID_CODE)
        assert auth.is_authenticated() is True
        assert await auth.access_token() == "new-access"

    assert credentials.refresh_token == "new-refresh"
    assert credentials.expires_at == NOW + 3600

    request = handler.requests[0]
    assert request.url.params["grant_type"] == "authorization_code"
    assert request.url.params["code"] == VALID_CODE

    persistiert = store.load()
    assert persistiert is not None
    assert persistiert.refresh_token == "new-refresh"
    assert persistiert.access_token == "new-access"


async def test_abgelehnter_code_meldet_klaren_fehler(tmp_path):
    store = make_store(tmp_path)
    handler = Recorder(httpx.Response(400, json={"error": "invalid_grant"}))
    async with client_for(handler) as client:
        auth = GogAuth(store, client=client, now=lambda: NOW)
        with pytest.raises(AuthError, match="rejected"):
            await auth.exchange_code(VALID_CODE)


# -- Ablage ---------------------------------------------------------------


def test_auth_json_hat_rechte_0600_auch_beim_neuschreiben(tmp_path):
    path = tmp_path / "nested" / "auth.json"
    store = FileCredentialStore(path)

    store.save(stored_credentials(expires_at=NOW))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    path.chmod(0o644)
    store.save(stored_credentials(expires_at=NOW, access_token="zweiter-durchgang"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load().access_token == "zweiter-durchgang"


def test_auth_json_enthaelt_nur_die_erlaubten_felder(tmp_path):
    store = make_store(tmp_path, stored_credentials(expires_at=NOW))
    data = json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))
    assert set(data) == {"access_token", "refresh_token", "expires_at", "user_id", "session_id"}


def test_store_load_und_clear(tmp_path):
    path = tmp_path / "auth.json"
    store = FileCredentialStore(path)
    assert store.load() is None

    store.save(stored_credentials(expires_at=NOW))
    assert store.load() == stored_credentials(expires_at=NOW)

    store.clear()
    assert store.load() is None
    store.clear()  # zweites Mal ist kein Fehler

    path.write_text("{kaputt", encoding="utf-8")
    assert store.load() is None
