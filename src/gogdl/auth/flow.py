"""OAuth2-Flow, Token-Erneuerung und sichere Token-Ablage.

Umsetzung von KONZEPT.md §2: kein Formular-Login, kein Cookie-Jar. Der
Nutzer meldet sich im Systembrowser an und fügt den Redirect zurück ins
Terminal; ab da lebt das Tool vom ``refresh_token``.

Dieses Paket ist die einzige Stelle, die Tokens kennt. Ein Passwort wird
weder entgegengenommen noch gespeichert.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

import httpx

from gogdl.constants import (
    DEFAULT_TIMEOUT,
    GALAXY_CLIENT_ID,
    GALAXY_CLIENT_SECRET,
    REDIRECT_URI,
    TOKEN_URL,
    USER_AGENT,
    login_url,
)
from gogdl.errors import AuthError
from gogdl.model.types import Credentials

__all__ = [
    "FileCredentialStore",
    "GogAuth",
    "default_auth_path",
    "extract_code",
    "start_login",
]

NowFn = Callable[[], float]
"""Zeitquelle. Injizierbar, damit Ablaufverhalten ohne Warten testbar ist."""

OpenerFn = Callable[[str], bool]
"""Browser-Öffner. Injizierbar, damit Tests keinen Browser starten."""

_FILE_MODE = 0o600
_DIR_MODE = 0o700

_DEFAULT_EXPIRES_IN = 3600.0
"""Fallback, falls GOG die Lebensdauer einmal nicht mitschickt (§2.2)."""

_LOGIN_HINT = "Run `gogdl login` first."

_CODE_IN_URL = re.compile(r"(?:^|[?&#])code=([^&#?]+)")
_BARE_CODE = re.compile(r"^[A-Za-z0-9._~-]{8,}$")
"""Nur für die *nackte* Eingabe. Steht ``code=`` explizit davor, wird der
Wert nicht gegen dieses Muster geprüft: ein Zeichen, das GOG hier eines
Tages zusätzlich verwendet, darf keinen gültigen Redirect abweisen."""

_PERSISTED_KEYS = ("access_token", "refresh_token", "expires_at", "user_id", "session_id")
"""Genau diese Felder landen in auth.json — nie mehr, insbesondere kein Passwort."""


def default_auth_path() -> Path:
    """Standardablage der Tokens (KONZEPT.md §2.3)."""
    return Path.home() / ".config" / "gog-repo-downloader" / "auth.json"


class FileCredentialStore:
    """Implementiert model.protocols.CredentialStore als JSON-Datei mit 0600.

    Geschrieben wird immer atomar über eine temporäre Datei im selben
    Verzeichnis: ein Abbruch mitten im Schreiben darf keine halbe
    auth.json hinterlassen. Die Rechte werden bei *jedem* Schreiben neu
    gesetzt, nicht nur beim ersten Anlegen.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_auth_path()

    def load(self) -> Credentials | None:
        """Gespeicherte Tokens lesen.

        ``None`` bedeutet „nichts Brauchbares hinterlegt" — das gilt auch
        für eine beschädigte oder unvollständige Datei. Die Abhilfe ist
        in beiden Fällen dieselbe (`gogdl login`), deshalb wird hier
        nicht zwischen beidem unterschieden.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AuthError(f"{self.path} is not readable: {exc}") from exc

        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None

        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_at = data.get("expires_at")
        if not isinstance(access_token, str) or not access_token:
            return None
        if not isinstance(refresh_token, str) or not refresh_token:
            return None
        if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
            return None

        user_id = data.get("user_id")
        session_id = data.get("session_id")
        return Credentials(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=float(expires_at),
            user_id=user_id if isinstance(user_id, str) else None,
            session_id=session_id if isinstance(session_id, str) else None,
        )

    def save(self, credentials: Credentials) -> None:
        """Tokens atomar und mit Rechten 0600 ablegen."""
        payload = {key: getattr(credentials, key) for key in _PERSISTED_KEYS}

        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)

        fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=".auth-", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, _FILE_MODE)
            os.replace(tmp_path, self.path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        """Tokens verwerfen. Fehlt die Datei bereits, ist nichts zu tun."""
        self.path.unlink(missing_ok=True)


class GogAuth:
    """Implementiert model.protocols.AuthProvider.

    Hält die Credentials im Speicher und erneuert sie still, sobald sie
    ablaufen. Ein ``asyncio.Lock`` sorgt dafür, dass parallele Downloads
    bei abgelaufenem Token genau *einen* Refresh auslösen und nicht
    einen pro Task.
    """

    def __init__(
        self,
        store,
        client: httpx.AsyncClient | None = None,
        now: NowFn | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._owns_client = client is None
        self._now: NowFn = now if now is not None else time.time
        self._lock = asyncio.Lock()
        self._credentials: Credentials | None = None
        self._loaded = False

    # -- Protocol -----------------------------------------------------

    async def access_token(self) -> str:
        """Gültiges Access-Token liefern, bei Ablauf still erneuern."""
        credentials = self._current()
        if credentials is None:
            raise AuthError(f"Not signed in. {_LOGIN_HINT}")
        if not credentials.is_expired(self._now()):
            return credentials.access_token

        async with self._lock:
            # Zweite Prüfung unter dem Lock: hat ein paralleler Aufruf
            # bereits erneuert, ist hier nichts mehr zu tun.
            credentials = self._current()
            if credentials is None:
                raise AuthError(f"Not signed in. {_LOGIN_HINT}")
            if not credentials.is_expired(self._now()):
                return credentials.access_token
            refreshed = await self._refresh(credentials)
        return refreshed.access_token

    def is_authenticated(self) -> bool:
        """Ob überhaupt Zugangsdaten vorliegen — ohne Netzwerkzugriff."""
        return self._current() is not None

    # -- Login --------------------------------------------------------

    async def exchange_code(self, code: str) -> Credentials:
        """Authorization-Code gegen Tokens tauschen und speichern."""
        credentials = await self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": GALAXY_CLIENT_ID,
                "client_secret": GALAXY_CLIENT_SECRET,
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            rejected_message=(
                "GOG rejected the authorization code (HTTP {status}). "
                "The code is single-use and short-lived - run `gogdl login` again."
            ),
            discard_on_reject=False,
        )
        self._remember(credentials)
        return credentials

    async def aclose(self) -> None:
        """Nur den selbst angelegten HTTP-Client schließen, nie einen injizierten."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- Intern -------------------------------------------------------

    def _current(self) -> Credentials | None:
        """Credentials aus dem Cache oder — genau einmal — aus dem Store.

        Bewusst synchron: dadurch liegt zwischen Ablaufprüfung und Lock
        kein Await-Punkt, an dem ein zweiter Task hineinlaufen könnte.
        """
        if not self._loaded:
            self._credentials = self._store.load()
            self._loaded = True
        return self._credentials

    def _remember(self, credentials: Credentials) -> None:
        self._store.save(credentials)
        self._credentials = credentials
        self._loaded = True

    def _forget(self) -> None:
        self._store.clear()
        self._credentials = None
        self._loaded = True

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    async def _refresh(self, credentials: Credentials) -> Credentials:
        """Access-Token über den Refresh-Token erneuern."""
        refreshed = await self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": GALAXY_CLIENT_ID,
                "client_secret": GALAXY_CLIENT_SECRET,
                "refresh_token": credentials.refresh_token,
            },
            rejected_message=(
                "GOG rejected the stored refresh token (HTTP {status}). " + _LOGIN_HINT
            ),
            discard_on_reject=True,
            previous=credentials,
        )
        self._remember(refreshed)
        return refreshed

    async def _token_request(
        self,
        payload: dict[str, str],
        *,
        rejected_message: str,
        discard_on_reject: bool,
        previous: Credentials | None = None,
    ) -> Credentials:
        """Ein Aufruf gegen ``TOKEN_URL``; beide Grant-Typen laufen hier durch."""
        client = self._get_client()
        requested_at = self._now()
        try:
            response = await client.get(TOKEN_URL, params=payload)
        except httpx.HTTPError as exc:
            # Netzwerkfehler ist kein abgelehnter Token: nichts verwerfen.
            raise AuthError(f"GOG token endpoint not reachable: {exc}") from exc

        if response.status_code in (400, 401):
            if discard_on_reject:
                self._forget()
            raise AuthError(rejected_message.format(status=response.status_code))
        if response.status_code >= 400:
            raise AuthError(
                f"GOG token endpoint responded with HTTP {response.status_code}."
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise AuthError("The token endpoint response is not JSON.") from exc
        if not isinstance(data, dict):
            raise AuthError("The token endpoint response has an unexpected format.")

        return self._credentials_from(data, requested_at=requested_at, previous=previous)

    def _credentials_from(
        self,
        data: dict,
        *,
        requested_at: float,
        previous: Credentials | None,
    ) -> Credentials:
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise AuthError("The token endpoint response contains no access_token.")

        # GOG rotiert den Refresh-Token gelegentlich mit; fehlt er in der
        # Antwort, gilt der bisherige weiter.
        refresh_token = data.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            refresh_token = previous.refresh_token if previous is not None else None
        if not refresh_token:
            raise AuthError("The token endpoint response contains no refresh_token.")

        try:
            expires_in = float(data.get("expires_in", _DEFAULT_EXPIRES_IN))
        except (TypeError, ValueError):
            expires_in = _DEFAULT_EXPIRES_IN

        user_id = data.get("user_id")
        session_id = data.get("session_id")
        return Credentials(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=requested_at + expires_in,
            user_id=str(user_id) if user_id is not None else _kept(previous, "user_id"),
            session_id=str(session_id) if session_id is not None else _kept(previous, "session_id"),
        )


def _kept(previous: Credentials | None, field: str) -> str | None:
    """Feld aus den bisherigen Credentials übernehmen, falls vorhanden."""
    return getattr(previous, field) if previous is not None else None


def extract_code(pasted: str) -> str:
    """Authorization-Code aus dem herausfischen, was der Nutzer einfügt.

    Akzeptiert die komplette Redirect-URL, ein ``?code=…``-Fragment oder
    den nackten Code. Leerzeichen und Zeilenumbrüche — beim Kopieren aus
    dem Browser kaum vermeidbar — werden entfernt.
    """
    text = "".join(pasted.split()) if pasted else ""
    if not text:
        raise AuthError("No input. Paste the redirect URL or the code.")

    if "code=" in text:
        match = _CODE_IN_URL.search(text)
        if match is not None:
            code = _unquoted(match.group(1))
            if code:
                return code
        raise AuthError(
            "The pasted URL carries no usable `code` parameter. "
            "Expected is the full address of on_login_success."
        )

    if _BARE_CODE.match(text):
        return text

    raise AuthError(
        "The input looks like neither the redirect URL nor an authorization code."
    )


def _unquoted(raw: str) -> str:
    """Prozentkodierung auflösen.

    Bewusst ``unquote`` und nicht ``unquote_plus``: ein ``+`` im Code
    bliebe sonst als Leerzeichen zurück und der Code wäre zerstört.
    """
    return unquote(raw)


def start_login(*, open_browser: bool = True, opener: OpenerFn | None = None) -> str:
    """Login-URL liefern und sie optional im Systembrowser öffnen.

    Die URL wird immer zurückgegeben: schlägt das Öffnen fehl (kopfloser
    Server, kein Standardbrowser), kann der Nutzer sie von Hand
    kopieren. ``opener`` existiert, damit Tests keinen Browser starten.
    """
    url = login_url()
    if open_browser:
        open_fn = opener if opener is not None else webbrowser.open
        try:
            open_fn(url)
        except Exception:
            # Ein nicht startbarer Browser darf den Login nicht abbrechen.
            pass
    return url
