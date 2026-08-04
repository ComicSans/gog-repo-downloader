"""Lesezugriff auf die GOG-Endpunkte (KONZEPT.md §3, Pfad A).

Dieses Modul kennt nur HTTP und die Payload-Formen von GOG. Es trifft
keine Entscheidungen ueber Aktualitaet oder Ablage - es liefert die
Rohsignale aus §4.2 (``version``, ``size``, ``md5``) an die Planung.

Authentifiziert wird ausschliesslich per ``Authorization: Bearer`` aus dem
injizierten ``AuthProvider``; es gibt bewusst keinen Cookie-Jar.

Gearbeitet wird auf dem Offline-Weg von ``embed.gog.com``:
``account/gameDetails/{id}.json`` liefert die Dateiliste, und die dort
genannten ``manualUrl`` antworten mit einem 302 auf eine signierte
CDN-URL. Der frueher genutzte Weg ueber ``api.gog.com/products/{id}
?expand=downloads`` ist nicht mehr benutzbar: seine ``downlink``-URLs
antworten mit HTTP 404 und einer HTML-Fehlerseite, mit und ohne Token.
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
    EMBED_BASE,
    FILTERED_PRODUCTS_URL,
    MAX_RETRIES,
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

GAME_DETAILS_URL = f"{EMBED_BASE}/account/gameDetails/{{product_id}}.json"
"""Offline-Dateiliste eines Produkts. Lokal definiert, nicht in constants.py."""

_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})

_WHITESPACE = re.compile(r"\s+")
_VARIANT_FORBIDDEN = re.compile(r"[^a-z0-9._-]")

_OS_NAMES: dict[str, OsName] = {
    "windows": OsName.WINDOWS,
    "linux": OsName.LINUX,
    "mac": OsName.MAC,
}

LANGUAGE_CODES: dict[str, str] = {
    # GOG nennt die Sprache in gameDetails im Klartext, teils englisch
    # ("English"), teils in der Sprache selbst ("Deutsch"). Beide Formen
    # stehen deshalb als Schluessel. Alle Werte sind kleingeschrieben,
    # weil ``Preference.select`` in model/types.py kleinschreibt und ein
    # Code mit Grossbuchstaben beim Filtern lautlos danebenlaege.
    "english": "en",
    "german": "de",
    "deutsch": "de",
    "french": "fr",
    "francais": "fr",
    "français": "fr",
    "spanish": "es",
    "espanol": "es",
    "español": "es",
    "italian": "it",
    "italiano": "it",
    "polish": "pl",
    "polski": "pl",
    "russian": "ru",
    "русский": "ru",
    "portuguese": "pt",
    "portugues": "pt",
    "português": "pt",
    "czech": "cs",
    "cestina": "cs",
    "čeština": "cs",
    "hungarian": "hu",
    "magyar": "hu",
    "japanese": "ja",
    "日本語": "ja",
    "korean": "ko",
    "한국어": "ko",
    "chinese": "zh",
    "中文": "zh",
    "dutch": "nl",
    "nederlands": "nl",
    "danish": "da",
    "dansk": "da",
    "swedish": "sv",
    "svenska": "sv",
    "norwegian": "no",
    "norsk": "no",
    "finnish": "fi",
    "suomi": "fi",
    "turkish": "tr",
    "turkce": "tr",
    "türkçe": "tr",
}


def language_code(name: Any) -> str | None:
    """Klartext-Sprache auf einen Sprachcode; ``None`` bei Unbekanntem.

    Bewusst nur exakte Treffer: eine geratene Zuordnung waere schlimmer
    als gar keine, weil zwei verschiedene GOG-Sprachen sonst auf denselben
    Code fielen und sich damit einen Slot teilten.
    """
    if not isinstance(name, str):
        return None
    return LANGUAGE_CODES.get(_WHITESPACE.sub(" ", name.strip().lower()))


def language_fallback(name: Any) -> str:
    """Ersatzcode fuer unbekannte Sprachen: kleingeschrieben, ohne Leerraum.

    Der Wert muss stabil sein, sonst gilt der Slot beim naechsten Lauf als
    neu. Er bleibt verschieden fuer verschiedene Klartexte - genau das
    haelt zwei unbekannte Sprachen in getrennten Slots.
    """
    if name is None or isinstance(name, bool):
        return ""
    return "".join(str(name).lower().split())


def _filename_from_url(url: str) -> str:
    """Dateiname aus dem Pfad einer signierten URL.

    Reihenfolge ist wichtig: erst Query abschneiden, dann den letzten
    Pfadteil nehmen, erst zuletzt dekodieren. Umgekehrt würde ein
    kodiertes ``%3F`` als Query-Trenner missverstanden.
    """
    return unquote(posixpath.basename(urlsplit(url).path))


def _checksum_url_from_signed(signed: str) -> str:
    """Checksum-XML zu einer signierten CDN-URL: Pfad ohne Query plus ``.xml``.

    Abgeschnitten wird am ersten ``?``; nur der Pfad zaehlt, die
    Query-Parameter tauchen in der Checksum-URL nicht auf. Bewusst ein
    reiner String-Schnitt und kein Zerlegen und Wiederzusammensetzen: das
    Token steckt im Pfad, und jede Normalisierung koennte die Signatur
    zerstoeren.

    Endet der Pfad bereits auf ``.xml``, wird nichts angehaengt.
    """
    base = signed.split("?", 1)[0]
    if base.lower().endswith(".xml"):
        return base
    return base + ".xml"


def _file_id_from_manual(manual_url: Any) -> str:
    """Letzter Pfadbestandteil einer ``manualUrl``, z. B. ``en1installer0``.

    GOG vergibt diesen Bezeichner pro Produkt eindeutig; er ist damit die
    ``file_id`` und ueberlebt auch einen Versionswechsel.
    """
    if not isinstance(manual_url, str) or not manual_url.strip():
        return ""
    return posixpath.basename(urlsplit(manual_url.strip()).path)


def _text_or_none(value: Any) -> str | None:
    """Leerstring und Fehlwert auf ``None``; GOG schickt ``"version": ""``."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int | None:
    """Tolerante Zahl-Konvertierung; GOG liefert Groessen mal als String."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
    mehrere Eintraege einen Slot; ``_register_slot`` meldet das.
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
    """``Retry-After`` als Sekunden - der Header darf auch ein Datum sein."""
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
        self._warned_languages: set[str] = set()

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

        Alle Eintraege derselben Sprache und Plattform sind die Teile
        EINER Auslieferung und teilen sich einen Slot. Diese Buendelung ist
        Vorbedingung der Prune-Sicherheit (KONZEPT.md §5.5) - mehrteilige
        Installer duerfen nie in getrennte Slots zerfallen.
        """
        payload = await self._get_json(GAME_DETAILS_URL.format(product_id=product_id))
        out: list[RemoteFile] = []
        self._collect_details(
            payload,
            product_id=product_id,
            root_id=product_id,
            is_dlc=False,
            include_dlc=include_dlc,
            out=out,
            visited=set(),
            seen_slots=set(),
        )
        return out

    async def resolve_downlink(self, downlink: str) -> ResolvedLink:
        """Signierte CDN-URL frisch aufloesen. Ergebnis ist kurzlebig (§5.1).

        ``downlink`` ist die ``manualUrl`` aus gameDetails. GOG antwortet
        darauf mit einem 302; die signierte URL steht im ``Location``.
        Redirects duerfen deshalb nicht gefolgt werden - sonst laedt schon
        dieser Aufruf die ganze Datei herunter.

        ``checksum_url`` ist die signierte URL ohne Query plus ``.xml`` -
        dieser Pfad liefert gegen ein echtes Konto das Checksum-XML und
        damit ``md5``, das dritte Aktualitaetssignal aus §4.2.

        Beide Werte sind nur so lange gueltig wie die Signatur. Sie
        gehoeren in denselben Arbeitsgang und duerfen niemals persistiert
        werden - ein gespeicherter ``checksum_url`` waere beim naechsten
        Lauf abgelaufen.

        Der Abruf des XML kostet einen eigenen Request pro Datei. Ob er
        sich lohnt, entscheidet allein der Aufrufer (``cli/commands.py::
        _enrich``); hier wird nur die URL geliefert, nichts geholt.
        """
        url = self._embed_absolute(downlink)
        response = await self._request("GET", url, follow_redirects=False)
        if response.status_code in _REDIRECT_STATUS:
            location = response.headers.get("Location")
            if not location:
                raise ApiError(f"Weiterleitung ohne Location-Header: {url}")
            # Absolute Location unveraendert uebernehmen: die signierte URL
            # traegt ihr Token im Pfad, und jede Normalisierung koennte die
            # Signatur zerstoeren.
            signed = location if urlsplit(location).scheme else urljoin(url, location)
        else:
            # 200 statt 302: die Antwort ist bereits die Datei selbst, die
            # angefragte URL also die signierte. Kein Fehlerfall.
            signed = str(response.url)
        filename = _filename_from_url(signed)
        if not filename:
            raise ApiError(f"Kein Dateiname in der signierten URL ableitbar: {url}")
        return ResolvedLink(
            url=signed,
            filename=filename,
            checksum_url=_checksum_url_from_signed(signed),
        )

    async def content_length(self, url: str) -> int | None:
        """Echte Bytegroesse einer signierten URL per HEAD.

        Nicht Teil des Protocols ``GogApi`` und bewusst getrennt von
        ``resolve_downlink``: das Modell ``ResolvedLink`` bleibt
        unveraendert. Die ``size`` aus gameDetails ist gerundeter Text
        ("1 MB") und als Aktualitaetssignal unbrauchbar; nur dieser Wert
        taugt fuer §4.2.

        Fehlt der Header oder ist er unlesbar, ist das Ergebnis ``None``.
        Wie bei ``checksum`` ist ein nicht abrufbarer Wert kein Abbruch -
        nur 401 und 429 werden durchgereicht.
        """
        if not url:
            return None
        try:
            response = await self._request("HEAD", url, follow_redirects=True)
        except RateLimitError:
            raise
        except ApiError as exc:
            _LOG.debug("Groesse nicht abrufbar (%s): %s", url, exc)
            return None
        return _as_int(response.headers.get("Content-Length"))

    async def checksum(self, checksum_url: str) -> FileChecksum | None:
        """Checksum-XML einer Datei. Kostet einen eigenen Request.

        ``checksum_url`` kommt aus ``resolve_downlink`` und ist nur so
        lange gueltig wie die Signatur - im selben Zug verwenden, nie
        speichern.

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

    def _collect_details(
        self,
        payload: Any,
        *,
        product_id: int,
        root_id: int,
        is_dlc: bool,
        include_dlc: bool,
        out: list[RemoteFile],
        visited: set[int],
        seen_slots: set[SlotKey],
    ) -> None:
        """Ein gameDetails-Objekt und - rekursiv - seine DLCs einsammeln.

        ``root_id`` ist immer das urspruenglich angefragte Hauptprodukt und
        wird in der Rekursion nie neu gesetzt. Auch ein DLC im DLC zeigt
        deshalb per ``dlc_of`` auf das Hauptspiel, nicht auf sein
        unmittelbares Elternprodukt.

        Die Wiederholungssperre haengt an der Objektidentitaet, nicht an
        der Produkt-ID: ein DLC ohne eigene id erbt die des Hauptprodukts
        und wuerde sonst faelschlich als bereits gesehen uebersprungen.
        """
        if not isinstance(payload, dict):
            return
        marker = id(payload)
        if marker in visited:
            return
        visited.add(marker)

        dlc_of = root_id if is_dlc else None
        self._collect_downloads(payload.get("downloads"), product_id, out, seen_slots, dlc_of)
        self._collect_extras(payload.get("extras"), product_id, out, seen_slots, dlc_of)

        if not include_dlc:
            return
        for dlc in payload.get("dlcs") or []:
            if not isinstance(dlc, dict):
                continue
            dlc_id = _as_int(dlc.get("id"))
            if dlc_id is None:
                # Ohne eigene id bleibt nur die des Hauptprodukts. Die
                # Slots des DLC fallen dann mit denen des Hauptspiels
                # zusammen; die Warnung unten in _register_slot meldet das.
                dlc_id = root_id
                _LOG.warning(
                    "DLC ohne eigene id in Produkt %s - Dateien laufen unter der "
                    "Produkt-ID des Hauptspiels",
                    root_id,
                )
            self._collect_details(
                dlc,
                product_id=dlc_id,
                root_id=root_id,
                is_dlc=True,
                include_dlc=include_dlc,
                out=out,
                visited=visited,
                seen_slots=seen_slots,
            )

    def _collect_downloads(
        self,
        downloads: Any,
        product_id: int,
        out: list[RemoteFile],
        seen_slots: set[SlotKey],
        dlc_of: int | None,
    ) -> None:
        """``downloads`` aus gameDetails in Installer-RemoteFiles uebersetzen.

        Die Struktur ist eine Liste von Paaren::

            [["English", {"windows": [ {...}, {...} ]}], ...]

        Alle Eintraege einer Plattform sind die Teile einer einzigen
        Auslieferung - GOG teilt grosse Installer dort in ``(Part 1 of 2)``
        und ``(Part 2 of 2)`` auf. Sie muessen deshalb denselben Slot
        bekommen (KONZEPT.md §5.5).
        """
        if not isinstance(downloads, Iterable) or isinstance(downloads, (str, bytes, dict)):
            return
        for pair in downloads:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            language = self._language_code(pair[0])
            platforms = pair[1]
            if not isinstance(platforms, dict):
                continue
            for raw_os, entries in platforms.items():
                os_name = self._map_os(raw_os)
                if os_name is None:
                    continue
                if not isinstance(entries, list):
                    continue
                usable = [
                    entry
                    for entry in entries
                    if isinstance(entry, dict) and _file_id_from_manual(entry.get("manualUrl"))
                ]
                if not usable:
                    continue
                slot = SlotKey(
                    product_id=product_id,
                    kind=FileKind.INSTALLER,
                    os=os_name,
                    language=language,
                )
                self._register_slot(slot, seen_slots)
                total_parts = len(usable)
                for index, entry in enumerate(usable, start=1):
                    manual_url = str(entry["manualUrl"]).strip()
                    out.append(
                        RemoteFile(
                            slot=slot,
                            file_id=_file_id_from_manual(manual_url),
                            downlink=manual_url,
                            # Die Groesse aus gameDetails ist gerundeter
                            # Text; die echte holt content_length.
                            size=None,
                            version=_text_or_none(entry.get("version")),
                            part_index=index,
                            total_parts=total_parts,
                            dlc_of=dlc_of,
                        )
                    )

    def _collect_extras(
        self,
        extras: Any,
        product_id: int,
        out: list[RemoteFile],
        seen_slots: set[SlotKey],
        dlc_of: int | None,
    ) -> None:
        """``extras`` in RemoteFiles uebersetzen; je Eintrag ein eigener Slot.

        Extras haben weder Plattform noch Sprache. Ohne Diskriminator
        fielen Handbuch, Soundtrack und Wallpaper eines Spiels in einen
        Slot und waeren fuers Aufraeumen eine einzige Auslieferung - genau
        das verbietet §5.5.
        """
        if not isinstance(extras, Iterable) or isinstance(extras, (str, bytes, dict)):
            return
        for entry in extras:
            if not isinstance(entry, dict):
                continue
            manual_url = entry.get("manualUrl")
            file_id = _file_id_from_manual(manual_url)
            if not file_id:
                _LOG.warning("Extra ohne manualUrl in Produkt %s uebersprungen", product_id)
                continue
            slot = SlotKey(
                product_id=product_id,
                kind=FileKind.EXTRA,
                os=None,
                language=None,
                variant=_pick_variant((entry.get("name"),), file_id),
            )
            self._register_slot(slot, seen_slots)
            out.append(
                RemoteFile(
                    slot=slot,
                    file_id=file_id,
                    downlink=str(manual_url).strip(),
                    size=None,
                    version=_text_or_none(entry.get("version")),
                    part_index=1,
                    total_parts=1,
                    dlc_of=dlc_of,
                )
            )

    def _register_slot(self, slot: SlotKey, seen_slots: set[SlotKey]) -> None:
        """Doppelt vergebene Slots melden - Prune saehe sie als eine Einheit."""
        if slot in seen_slots:
            _LOG.warning(
                "Zwei Eintraege teilen den Slot %s - Prune sieht sie als eine Auslieferung",
                slot.as_str(),
            )
        seen_slots.add(slot)

    def _map_os(self, raw: Any) -> OsName | None:
        """Plattform-Schluessel auf ``OsName``; Unbekanntes wird einmal gemeldet."""
        if not isinstance(raw, str):
            key = ""
        else:
            key = raw.strip().lower()
        os_name = _OS_NAMES.get(key)
        if os_name is None and key not in self._warned_os:
            self._warned_os.add(key)
            _LOG.warning("Unbekanntes Betriebssystem %r von GOG - Eintraege uebersprungen", raw)
        return os_name

    def _language_code(self, raw: Any) -> str | None:
        """Klartext-Sprache auf einen Code; Unbekanntes wird einmal gemeldet."""
        text = str(raw).strip() if isinstance(raw, str) else ""
        if not text:
            return None
        code = language_code(text)
        if code is not None:
            return code
        fallback = language_fallback(text)
        if fallback and fallback not in self._warned_languages:
            self._warned_languages.add(fallback)
            _LOG.warning(
                "Unbekannte Sprache %r von GOG - Code %r wird ersatzweise verwendet",
                text,
                fallback,
            )
        return fallback or None

    # -- HTTP --------------------------------------------------------

    def _absolute(self, url: str) -> str:
        """Relative Links auf ``API_BASE`` beziehen.

        Die Checksum-URLs aus ``resolve_downlink`` sind bereits absolut
        und bleiben unveraendert; das hier ist nur der Notnagel fuer einen
        relativ hereingereichten Wert.
        """
        if urlsplit(url).scheme:
            return url
        return urljoin(API_BASE + "/", url.lstrip("/"))

    def _embed_absolute(self, url: str) -> str:
        """Relative ``manualUrl`` auf ``EMBED_BASE`` beziehen.

        Getrennt von ``_absolute``: die Downloads liegen hinter
        ``embed.gog.com``, das Checksum-XML hinter ``api.gog.com``.
        """
        if urlsplit(url).scheme:
            return url
        return urljoin(EMBED_BASE + "/", url.lstrip("/"))

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
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
    ) -> httpx.Response:
        """Ein Request mit Retry fuer 5xx, Netzwerkfehler und 429.

        401 ist ein Auth-Problem und wird sofort gemeldet; andere 4xx sind
        endgueltig. Wiederholt wird hoechstens ``max_retries`` mal.

        ``follow_redirects`` wird nur weitergereicht, wenn es gesetzt ist -
        sonst gilt die Einstellung des injizierten Clients. Die
        Downlink-Aufloesung braucht ausdruecklich ``False``, weil die
        Antwort hinter dem 302 die vollstaendige Datei waere.
        """
        attempt = 0
        extra: dict[str, Any] = {}
        if follow_redirects is not None:
            extra["follow_redirects"] = follow_redirects
        while True:
            try:
                response = await self._client.request(
                    method, url, params=params, headers=await self._headers(), **extra
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
