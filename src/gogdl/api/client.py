"""Lesezugriff auf die GOG-Endpunkte (KONZEPT.md §3, Pfad A).

Dieses Modul kennt nur HTTP und die Payload-Formen von GOG. Es trifft
keine Entscheidungen über Aktualität oder Ablage — es liefert die
Rohsignale aus §4.2 (``version``, ``size``, ``md5``) an die Planung.

Authentifiziert wird ausschließlich per ``Authorization: Bearer`` aus dem
injizierten ``AuthProvider``; es gibt bewusst keinen Cookie-Jar.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Iterable
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from gogdl.constants import (
    API_BASE,
    DEFAULT_TIMEOUT,
    FILTERED_PRODUCTS_URL,
    MAX_RETRIES,
    PRODUCT_URL,
    USER_AGENT,
    USER_DATA_URL,
)
from gogdl.errors import ApiError, AuthError, RateLimitError
from gogdl.model.protocols import AuthProvider
from gogdl.model.types import (
    FileChecksum,
    FileKind,
    OsName,
    ProductRef,
    RemoteFile,
    ResolvedLink,
    SlotKey,
    UserData,
)

_LOG = logging.getLogger(__name__)

BACKOFF_BASE = 0.5
"""Erste Wartezeit in Sekunden; verdoppelt sich mit jedem Versuch."""

BACKOFF_MAX = 30.0

_OS_NAMES: dict[str, OsName] = {
    "windows": OsName.WINDOWS,
    "linux": OsName.LINUX,
    "mac": OsName.MAC,
}


def _filename_from_url(url: str) -> str:
    """Dateiname aus dem Pfad einer signierten URL.

    Reihenfolge ist wichtig: erst Query abschneiden, dann den letzten
    Pfadteil nehmen, erst zuletzt dekodieren. Umgekehrt würde ein
    kodiertes ``%3F`` als Query-Trenner missverstanden.
    """
    return unquote(posixpath.basename(urlsplit(url).path))


def _as_int(value: Any) -> int | None:
    """Tolerante Zahl-Konvertierung; GOG liefert Größen mal als String."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_WHITESPACE = re.compile(r"\s+")
_VARIANT_FORBIDDEN = re.compile(r"[^a-z0-9._-]")


def _normalize_variant(raw: Any) -> str:
    """Diskriminator auf eine stabile, dateinamentaugliche Form bringen.

    Reihenfolge ist wichtig: erst kleinschreiben, dann Leerraum zu ``-``,
    erst danach filtern. Andersherum würde aus "Game Soundtrack" ein
    "gamesoundtrack" statt "game-soundtrack".

    Die Regel muss über Läufe hinweg dasselbe Ergebnis liefern - sie darf
    deshalb weder von Reihenfolge noch von Zählern abhängen, sonst gilt
    beim nächsten Update jeder Slot als neu.
    """
    if raw is None or isinstance(raw, bool):
        return ""
    text = _WHITESPACE.sub("-", str(raw).strip().lower())
    return _VARIANT_FORBIDDEN.sub("", text)


def _pick_variant(candidates: Iterable[Any], entry_id: Any) -> str | None:
    """Ersten brauchbaren Diskriminator wählen, sonst auf die id zurückfallen.

    Liefert ``None``, wenn auch die id nichts hergibt. Dann teilen sich
    mehrere Einträge einen Slot; ``_collect_group`` meldet das.
    """
    for candidate in candidates:
        normalized = _normalize_variant(candidate)
        if normalized:
            return normalized
    normalized = _normalize_variant(entry_id)
    if normalized:
        return normalized
    return str(entry_id) if entry_id is not None else None


def _retry_after_seconds(raw: str | None) -> float | None:
    """``Retry-After`` als Sekunden — der Header darf auch ein Datum sein."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(float(raw), 0.0)
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    if target.tzinfo is None:
        target = target.replace(tzinfo=_dt.timezone.utc)
    return max((target - now).total_seconds(), 0.0)


class GogApiClient:
    """Implementiert model.protocols.GogApi.

    ``client`` und ``sleep`` sind injizierbar, damit Tests mit
    ``httpx.MockTransport`` arbeiten und der Backoff nicht real wartet.
    """

    def __init__(
        self,
        auth: AuthProvider,
        client: httpx.AsyncClient | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self._auth = auth
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True)
        self._owns_client = client is None
        self._sleep = sleep or asyncio.sleep
        self._max_retries = max_retries
        self._warned_os: set[str] = set()

    async def aclose(self) -> None:
        """Schließt nur einen selbst erzeugten Client."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "GogApiClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # -- Endpunkte ---------------------------------------------------

    async def user_data(self) -> UserData:
        """Auth-Probe und Anzeigename (KONZEPT.md §3)."""
        data = await self._get_json(USER_DATA_URL)
        return UserData(
            username=str(data.get("username") or ""),
            user_id=str(data.get("userId") or ""),
            is_logged_in=bool(data.get("isLoggedIn", True)),
        )

    async def library(self) -> list[ProductRef]:
        """Vollständige Bibliothek über alle Seiten von ``getFilteredProducts``."""
        products: list[ProductRef] = []
        page = 1
        while True:
            data = await self._get_json(
                FILTERED_PRODUCTS_URL, params={"mediaType": "1", "page": page}
            )
            for item in data.get("products") or []:
                product_id = _as_int(item.get("id"))
                if product_id is None:
                    _LOG.warning("Bibliothekseintrag ohne verwertbare id übersprungen")
                    continue
                products.append(
                    ProductRef(
                        product_id=product_id,
                        title=str(item.get("title") or ""),
                        slug=str(item.get("slug") or ""),
                        has_updates=(_as_int(item.get("updates")) or 0) > 0,
                        is_new=bool(item.get("isNew") or False),
                    )
                )
            total_pages = _as_int(data.get("totalPages")) or 1
            if page >= total_pages:
                return products
            page += 1

    async def product_files(self, product_id: int, *, include_dlc: bool = True) -> list[RemoteFile]:
        """Alle Dateien eines Produkts, DLCs rekursiv eingeschlossen.

        Ein Installer-Eintrag ist genau ein Slot; seine ``files`` sind
        dessen Teile. Diese Bündelung ist Vorbedingung der Prune-Sicherheit
        (KONZEPT.md §5.5) — mehrteilige Installer dürfen nie in getrennte
        Slots zerfallen.
        """
        payload = await self._get_json(
            PRODUCT_URL.format(product_id=product_id),
            params={"expand": "downloads,expanded_dlcs"},
        )
        out: list[RemoteFile] = []
        seen_slots: set[SlotKey] = set()
        self._collect_product(
            payload,
            product_id=product_id,
            root_id=product_id,
            include_dlc=include_dlc,
            out=out,
            visited=set(),
            seen_slots=seen_slots,
        )
        return out

    async def resolve_downlink(self, downlink: str) -> ResolvedLink:
        """Signierte CDN-URL frisch auflösen. Ergebnis ist kurzlebig (§5.1)."""
        url = self._absolute(downlink)
        data = await self._get_json(url)
        signed = data.get("downlink")
        if not signed:
            raise ApiError(f"Antwort ohne downlink: {url}")
        signed = str(signed)
        filename = _filename_from_url(signed)
        if not filename:
            raise ApiError(f"Kein Dateiname in der signierten URL ableitbar: {url}")
        checksum = data.get("checksum")
        return ResolvedLink(
            url=signed,
            filename=filename,
            checksum_url=self._absolute(str(checksum)) if checksum else None,
        )

    async def checksum(self, checksum_url: str) -> FileChecksum | None:
        """Checksum-XML einer Datei.

        Ein fehlendes oder kaputtes XML ist laut KONZEPT.md §4.2 ein
        zulässiger Zustand (``md5`` bleibt dann ``None``) und darf den
        Lauf nicht abbrechen. Auth- und Rate-Limit-Fehler sind etwas
        anderes und werden durchgereicht.
        """
        if not checksum_url:
            return None
        url = self._absolute(checksum_url)
        try:
            response = await self._request("GET", url)
        except RateLimitError:
            raise
        except ApiError as exc:
            _LOG.debug("Checksum nicht abrufbar (%s): %s", url, exc)
            return None

        text = response.text.strip()
        if not text:
            _LOG.debug("Leeres Checksum-XML: %s", url)
            return None
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            _LOG.debug("Checksum-XML nicht parsebar (%s): %s", url, exc)
            return None

        node = root if root.tag == "file" else root.find("file")
        if node is None:
            _LOG.debug("Checksum-XML ohne <file>-Element: %s", url)
            return None
        name = node.get("name")
        md5 = node.get("md5")
        if not name or not md5:
            _LOG.debug("Checksum-XML ohne name/md5: %s", url)
            return None
        return FileChecksum(filename=name, md5=md5, total_size=_as_int(node.get("total_size")))

    # -- Payload-Auswertung ------------------------------------------

    def _collect_product(
        self,
        payload: Any,
        *,
        product_id: int,
        root_id: int,
        include_dlc: bool,
        out: list[RemoteFile],
        visited: set[int],
        seen_slots: set[SlotKey],
    ) -> None:
        """Ein Produkt und - rekursiv - seine DLCs einsammeln.

        ``root_id`` ist immer das ursprünglich angefragte Hauptprodukt und
        wird in der Rekursion nie neu gesetzt. Auch ein DLC im DLC zeigt
        deshalb per ``dlc_of`` auf das Hauptspiel, nicht auf sein
        unmittelbares Elternprodukt.
        """
        if not isinstance(payload, dict) or product_id in visited:
            return
        visited.add(product_id)

        dlc_of = root_id if product_id != root_id else None
        downloads = payload.get("downloads")
        if isinstance(downloads, dict):
            self._collect_group(
                downloads.get("installers"),
                FileKind.INSTALLER,
                product_id,
                out,
                seen_slots,
                dlc_of,
            )
            self._collect_group(
                downloads.get("patches"), FileKind.PATCH, product_id, out, seen_slots, dlc_of
            )
            self._collect_group(
                downloads.get("bonus_content"), FileKind.EXTRA, product_id, out, seen_slots, dlc_of
            )

        if not include_dlc:
            return
        for dlc in payload.get("expanded_dlcs") or []:
            if not isinstance(dlc, dict):
                continue
            dlc_id = _as_int(dlc.get("id"))
            if dlc_id is None:
                _LOG.warning("DLC ohne verwertbare id übersprungen (Produkt %s)", product_id)
                continue
            self._collect_product(
                dlc,
                product_id=dlc_id,
                root_id=root_id,
                include_dlc=include_dlc,
                out=out,
                visited=visited,
                seen_slots=seen_slots,
            )

    def _collect_group(
        self,
        entries: Any,
        kind: FileKind,
        product_id: int,
        out: list[RemoteFile],
        seen_slots: set[SlotKey],
        dlc_of: int | None = None,
    ) -> None:
        """Eine Liste (installers/patches/bonus_content) in RemoteFiles übersetzen.

        ``variant`` trennt hier die Auslieferungen, die sich sonst einen
        Slot teilen würden (KONZEPT.md §5.5): Extras haben weder Plattform
        noch Sprache, und GOG bietet pro Plattform/Sprache mehrere Patches
        mit verschiedenen Versionsspannen an. Installer brauchen es nicht.
        """
        if not isinstance(entries, Iterable) or isinstance(entries, (str, bytes, dict)):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            variant: str | None = None
            if kind is FileKind.EXTRA:
                # Extras sind weder OS- noch sprachgebunden.
                os_name: OsName | None = None
                language: str | None = None
                # Sprechender Diskriminator: type vor name vor id.
                variant = _pick_variant((entry.get("type"), entry.get("name")), entry_id)
            else:
                os_name = self._map_os(entry.get("os"))
                if os_name is None:
                    continue
                language = str(entry.get("language")) if entry.get("language") else None
            version = str(entry.get("version")) if entry.get("version") else None
            if kind is FileKind.PATCH:
                # Die Versionsspanne ist die Auslieferung; ohne sie die id.
                variant = _pick_variant((version,), entry_id)

            parts = entry.get("files")
            if not isinstance(parts, list) or not parts:
                continue
            slot = SlotKey(
                product_id=product_id,
                kind=kind,
                os=os_name,
                language=language,
                variant=variant,
            )
            if slot in seen_slots:
                _LOG.warning(
                    "Zwei Einträge teilen den Slot %s - Prune sieht sie als eine Auslieferung",
                    slot.as_str(),
                )
            seen_slots.add(slot)

            total_parts = len(parts)
            for index, part in enumerate(parts, start=1):
                if not isinstance(part, dict):
                    continue
                file_id = part.get("id")
                downlink = part.get("downlink")
                if file_id is None or not downlink:
                    _LOG.warning("Datei ohne id/downlink in Slot %s übersprungen", slot.as_str())
                    continue
                out.append(
                    RemoteFile(
                        slot=slot,
                        file_id=str(file_id),
                        downlink=str(downlink),
                        size=_as_int(part.get("size")),
                        version=version,
                        part_index=index,
                        total_parts=total_parts,
                        dlc_of=dlc_of,
                    )
                )

    def _map_os(self, raw: Any) -> OsName | None:
        """``os``-String auf ``OsName``; Unbekanntes wird einmal gemeldet."""
        if not isinstance(raw, str):
            key = ""
        else:
            key = raw.strip().lower()
        os_name = _OS_NAMES.get(key)
        if os_name is None and key not in self._warned_os:
            self._warned_os.add(key)
            _LOG.warning("Unbekanntes Betriebssystem %r von GOG — Einträge übersprungen", raw)
        return os_name

    # -- HTTP --------------------------------------------------------

    def _absolute(self, url: str) -> str:
        """Relative Links (z. B. ``/products/1/downlink/...``) auf ``API_BASE`` beziehen."""
        if urlsplit(url).scheme:
            return url
        return urljoin(API_BASE + "/", url.lstrip("/"))

    async def _headers(self) -> dict[str, str]:
        token = await self._auth.access_token()
        return {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._request("GET", url, params=params)
        try:
            data = response.json()
        except ValueError as exc:
            raise ApiError(f"Keine gültige JSON-Antwort von {url}: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(f"Unerwartete JSON-Struktur von {url}: {type(data).__name__}")
        return data

    async def _request(
        self, method: str, url: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        """Ein Request mit Retry für 5xx, Netzwerkfehler und 429.

        401 ist ein Auth-Problem und wird sofort gemeldet; andere 4xx sind
        endgültig. Wiederholt wird höchstens ``max_retries`` mal.
        """
        attempt = 0
        while True:
            try:
                response = await self._client.request(
                    method, url, params=params, headers=await self._headers()
                )
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise ApiError(
                        f"Netzwerkfehler bei {url} nach {attempt + 1} Versuchen: {exc}"
                    ) from exc
                await self._backoff(attempt)
                attempt += 1
                continue

            status = response.status_code
            if status == 401:
                raise AuthError(f"GOG hat den Zugriff abgelehnt (HTTP 401): {url}")
            if status == 429:
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                if attempt >= self._max_retries:
                    raise RateLimitError(
                        f"GOG drosselt den Zugriff (HTTP 429): {url}", retry_after=retry_after
                    )
                if retry_after is None:
                    await self._backoff(attempt)
                else:
                    await self._sleep(retry_after)
                attempt += 1
                continue
            if status >= 500:
                if attempt >= self._max_retries:
                    raise ApiError(
                        f"HTTP {status} von {url} nach {attempt + 1} Versuchen"
                    )
                await self._backoff(attempt)
                attempt += 1
                continue
            if status >= 400:
                raise ApiError(f"HTTP {status} von {url}")
            return response

    async def _backoff(self, attempt: int) -> None:
        await self._sleep(min(BACKOFF_BASE * (2**attempt), BACKOFF_MAX))
