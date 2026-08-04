"""Lesezugriff auf die GOG-Endpunkte (KONZEPT.md §3, Pfad A).

Dieses Modul kennt nur HTTP und die Payload-Formen von GOG. Es trifft
keine Entscheidungen ueber Aktualitaet oder Ablage - es liefert die
Rohsignale aus §4.2 (``version``, ``size``, ``md5``) an die Planung.

Authentifiziert wird ausschliesslich per ``Authorization: Bearer`` aus dem
injizierten ``AuthProvider``; es gibt bewusst keinen Cookie-Jar.

Es gibt zwei Wege zur Dateiliste, und dieses Modul kennt beide.

Primaerweg ist ``api.gog.com/products/{id}?expand=downloads,expanded_dlcs``.
Er ist der bessere, weil er zwei Dinge liefert, die der andere nicht hat:
die Adresse des Checksum-XML als eigenes Feld ``checksum`` der
Downlink-Antwort, und vier statt zwei Kategorien (``installers``,
``patches``, ``language_packs``, ``bonus_content``). Ausserdem fasst er
die Aktualisierungsmarker im GOG-Konto nicht an.

Was er NICHT liefert, ist eine brauchbare Groesse - siehe die Messung in
``_collect_api_group``. Beide Wege sind darin gleich schlecht, und beide
setzen ``size=None``.

Rueckfallweg ist der Offline-Weg von ``embed.gog.com``:
``account/gameDetails/{id}.json`` liefert die Dateiliste, und die dort
genannten ``manualUrl`` antworten mit einem 302 auf eine signierte
CDN-URL. Er greift, wenn der Produktabruf mit 404 antwortet oder keine
Dateien liefert. Beides ist der Normalfall fuer ein Produkt, an dem
dieses Konto keine Downloadrechte hat, und kein Fehler.

Frueher stand hier, ``api.gog.com`` sei tot. Das war eine Fehldeutung:
der beobachtete 404 kam von einem einzelnen Produkt ohne Downloadrechte,
nicht vom Endpunkt.
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

GAME_DETAILS_URL = f"{EMBED_BASE}/account/gameDetails/{{product_id}}.json"
"""Offline-Dateiliste eines Produkts. Lokal definiert, nicht in constants.py."""

PRODUCT_EXPAND = "downloads,expanded_dlcs"
"""``expand``-Parameter des Produktabrufs: Dateiliste plus DLCs in einem Zug."""

LANGUAGE_PACK_PREFIX = "langpack-"
"""Variant-Praefix der ``language_packs``.

Ohne dieses Praefix teilten sich ein Sprachpaket und ein echter Patch mit
derselben Versionsangabe einen Slot; fuer das Aufraeumen waeren sie dann
eine Auslieferung, und genau das verbietet KONZEPT.md §5.5.
"""

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


def _api_file_id(part: Any) -> str:
    """``file_id`` eines Teils aus ``downloads.*[].files[]``.

    Bevorzugt wird das Feld ``id``; fehlt es, liefert der letzte Pfadteil
    des ``downlink`` denselben Bezeichner. Beide Formen ergeben exakt den
    Wert, den der Rueckfallweg aus der ``manualUrl`` zieht - das muss so
    sein, sonst gaelte nach einem Wegwechsel jede Datei als neu.
    """
    if not isinstance(part, dict):
        return ""
    raw = part.get("id")
    if raw is not None and not isinstance(raw, bool):
        text = str(raw).strip()
        if text:
            return text
    return _file_id_from_manual(part.get("downlink"))


def _repair_serial(raw: Any) -> str:
    """Seriennummer lesbar machen; leer oder unrettbar ergibt ``""``.

    GOG liefert manche Schluessel UTF-16-kodiert aus, ohne das zu
    kennzeichnen: der JSON-String traegt dann je Zeichen ein Byte, und
    jedes zweite ist ein Nullbyte. ``str.isprintable`` erkennt das
    zuverlaessig, weil ein Nullbyte nie druckbar ist. Bei ungerader Laenge
    fehlt das letzte Fuellbyte und wird ergaenzt, sonst scheitert die
    Dekodierung. gogrepoc behandelt denselben Fall.

    Ein Schluessel, der auch nach der Reparatur nicht druckbar ist, ist
    unbrauchbar und faellt weg - ihn roh weiterzureichen hiesse, Muell als
    Seriennummer auszugeben.
    """
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text:
        return ""
    if text.isprintable():
        return text
    candidate = text if len(text) % 2 == 0 else text + "\x00"
    try:
        decoded = candidate.encode("latin-1").decode("utf-16").strip()
    except (UnicodeDecodeError, UnicodeEncodeError):
        _LOG.debug("Serial key is not decodable as UTF-16 - dropped")
        return ""
    if not decoded or not decoded.isprintable():
        _LOG.debug("Serial key stays unreadable after UTF-16 repair - dropped")
        return ""
    return decoded


def _downlink_payload(response: httpx.Response) -> dict[str, Any] | None:
    """JSON-Antwort eines ``api.gog.com``-Downlinks, sonst ``None``.

    Streng an den Content-Type gebunden: eine 200-Antwort auf eine bereits
    signierte CDN-URL ist die Datei selbst und darf nicht als JSON
    fehlgedeutet werden.
    """
    if "json" not in response.headers.get("Content-Type", "").lower():
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    signed = data.get("downlink")
    if not isinstance(signed, str) or not signed.strip():
        return None
    return data


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
                    _LOG.warning("Skipped a library entry without a usable id")
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

        Primaerquelle ist ``api.gog.com/products/{id}?expand=downloads,
        expanded_dlcs``. Antwortet der Abruf mit 404 oder liefert er
        keinerlei Dateien, greift der Rueckfall auf gameDetails. Welcher
        Weg gegriffen hat, steht im ``debug``-Protokoll.

        Alle Teile derselben Auslieferung teilen sich einen Slot. Diese
        Buendelung ist Vorbedingung der Prune-Sicherheit (KONZEPT.md §5.5)
        - mehrteilige Installer duerfen nie in getrennte Slots zerfallen.
        """
        files = await self._files_from_product(product_id, include_dlc=include_dlc)
        if files:
            _LOG.debug(
                "product_files(%s): served by api.gog.com/products (%d files)",
                product_id,
                len(files),
            )
            return files
        _LOG.debug("product_files(%s): falling back to gameDetails", product_id)
        return await self._files_from_details(product_id, include_dlc=include_dlc)

    async def serials(self, product_id: int) -> dict[str, str]:
        """Seriennummern eines Produkts und seiner DLCs: Titel -> Schluessel.

        Eigener Abruf, bewusst nicht Teil von ``product_files``: die
        Schluessel stehen nur in gameDetails, und wer sie nicht braucht,
        soll den Request nicht bezahlen. Der Aufrufer entscheidet.

        Nicht Teil des Protocols ``GogApi`` - wie ``content_length`` ist
        das eine Zusatzleistung dieses Clients, kein Vertragsbestandteil.

        Leere und unlesbare Schluessel fallen weg (siehe
        ``_repair_serial``). Zwei DLCs mit identischem Titel fallen im
        Ergebnis zusammen; die Abbildung ist auf den Titel geschluesselt.
        """
        payload = await self._get_json(GAME_DETAILS_URL.format(product_id=product_id))
        out: dict[str, str] = {}
        self._collect_serials(payload, str(product_id), out, set())
        return out

    async def resolve_downlink(self, downlink: str) -> ResolvedLink:
        """Signierte CDN-URL frisch aufloesen. Ergebnis ist kurzlebig (§5.1).

        Beide Wege enden hier, und sie antworten verschieden:

        * Ein ``downlink`` aus dem Produktabruf beantwortet GOG mit 200 und
          einem JSON-Objekt ``{"downlink": ..., "checksum": ...}``.
        * Eine ``manualUrl`` aus gameDetails beantwortet GOG mit einem 302;
          die signierte URL steht im ``Location``.

        Redirects duerfen deshalb nicht gefolgt werden - sonst laedt schon
        dieser Aufruf die ganze Datei herunter.

        ``checksum_url`` kommt aus dem Feld ``checksum`` der JSON-Antwort.
        Fehlt es, wird die Adresse wie bisher aus der signierten URL ohne
        Query plus ``.xml`` gebaut. Diese Konstruktion ist der schwaechere
        Weg - genau sie ist bei lgogdownloader gebrochen, als GOG das
        Adressformat aenderte - und darum nur noch der Rueckfall. Beide
        Formen liefern das Checksum-XML und damit ``md5``, das dritte
        Aktualitaetssignal aus §4.2.

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
        checksum_url: str | None = None
        if response.status_code in _REDIRECT_STATUS:
            location = response.headers.get("Location")
            if not location:
                raise ApiError(f"Redirect without a Location header: {url}")
            # Absolute Location unveraendert uebernehmen: die signierte URL
            # traegt ihr Token im Pfad, und jede Normalisierung koennte die
            # Signatur zerstoeren.
            signed = location if urlsplit(location).scheme else urljoin(url, location)
        elif (payload := _downlink_payload(response)) is not None:
            signed = str(payload["downlink"]).strip()
            checksum_url = _text_or_none(payload.get("checksum"))
        else:
            # 200 ohne JSON: die Antwort ist bereits die Datei selbst, die
            # angefragte URL also die signierte. Kein Fehlerfall.
            signed = str(response.url)
        filename = _filename_from_url(signed)
        if not filename:
            raise ApiError(f"No filename can be derived from the signed URL: {url}")
        return ResolvedLink(
            url=signed,
            filename=filename,
            checksum_url=checksum_url or _checksum_url_from_signed(signed),
        )

    async def content_length(self, url: str) -> int | None:
        """Echte Bytegroesse einer signierten URL per HEAD.

        Nicht Teil des Protocols ``GogApi`` und bewusst getrennt von
        ``resolve_downlink``: das Modell ``ResolvedLink`` bleibt
        unveraendert. Keiner der beiden Wege nennt eine brauchbare Groesse:
        gameDetails liefert gerundeten Text ("1 MB"), api.gog.com eine auf
        volle MiB gerundete Zahl (1048576 statt 821824). Als
        Aktualitaetssignal nach §4.2 taugen nur dieser Wert und das
        ``total_size`` des Checksum-XML.

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
            _LOG.debug("Size not retrievable (%s): %s", url, exc)
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
            _LOG.debug("Checksum not retrievable (%s): %s", url, exc)
            return None

        text = response.text.strip()
        if not text:
            _LOG.debug("Empty checksum XML: %s", url)
            return None
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            _LOG.debug("Checksum XML not parsable (%s): %s", url, exc)
            return None

        node = root if root.tag == "file" else root.find("file")
        if node is None:
            _LOG.debug("Checksum XML without a <file> element: %s", url)
            return None
        name = node.get("name")
        md5 = node.get("md5")
        if not name or not md5:
            _LOG.debug("Checksum XML without name/md5: %s", url)
            return None
        return FileChecksum(filename=name, md5=md5, total_size=_as_int(node.get("total_size")))

    # -- Payload-Auswertung, Primaerweg api.gog.com ------------------

    async def _files_from_product(
        self, product_id: int, *, include_dlc: bool
    ) -> list[RemoteFile]:
        """Dateiliste aus dem Produktabruf; leere Liste heisst "nichts hier".

        Ein 404 ist hier kein Fehler, sondern die uebliche Antwort fuer ein
        Produkt ohne Downloadrechte, und wird zur leeren Liste. 401 und 429
        sind etwas anderes und werden durchgereicht - ein abgelaufenes
        Login darf nicht als "keine Dateien" durchrutschen und stillschweigend
        in den Rueckfallweg fuehren.

        Bewusst in Kauf genommen: ``ApiError`` trifft auch ein 5xx, das
        ``_request`` nach allen Versuchen aufgibt. Eine Stoerung von
        api.gog.com schaltet damit ebenfalls auf gameDetails um - mitsamt
        dessen Nebenwirkung auf die Aktualisierungsmarker. Das ist die
        gewollte Wahl: eine Dateiliste vom schlechteren Weg ist besser als
        keine. Unterscheidbar waeren die beiden Faelle nur ueber den Text
        der Ausnahme, denn ``ApiError`` traegt keinen Statuscode.
        """
        url = PRODUCT_URL.format(product_id=product_id)
        try:
            payload = await self._get_json(url, params={"expand": PRODUCT_EXPAND})
        except RateLimitError:
            raise
        except ApiError as exc:
            _LOG.debug("Product payload not retrievable (%s): %s", url, exc)
            return []
        out: list[RemoteFile] = []
        self._collect_product(
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

    def _collect_product(
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
        """Ein Produktobjekt und - rekursiv - seine ``expanded_dlcs`` einsammeln.

        ``root_id`` ist immer das urspruenglich angefragte Hauptprodukt und
        wird in der Rekursion nie neu gesetzt; auch ein DLC im DLC zeigt per
        ``dlc_of`` auf das Hauptspiel. Die Wiederholungssperre haengt wie im
        Rueckfallweg an der Objektidentitaet, nicht an der Produkt-ID.

        Ein DLC ohne eigenen ``downloads``-Block wird uebergangen und nicht
        einzeln nachgeladen: ein Abruf je DLC waere genau die N+1-Last, die
        dieser Weg beseitigen soll.
        """
        if not isinstance(payload, dict):
            return
        marker = id(payload)
        if marker in visited:
            return
        visited.add(marker)

        dlc_of = root_id if is_dlc else None
        downloads = payload.get("downloads")
        if isinstance(downloads, dict):
            self._collect_api_group(
                downloads.get("installers"),
                product_id,
                FileKind.INSTALLER,
                out,
                seen_slots,
                dlc_of,
                variant_prefix=None,
            )
            self._collect_api_group(
                downloads.get("patches"),
                product_id,
                FileKind.PATCH,
                out,
                seen_slots,
                dlc_of,
                variant_prefix="",
            )
            # language_packs landen bei FileKind.PATCH, nicht bei EXTRA:
            # sie tragen os und language, und nur ein Slot mit gesetzter
            # Sprache laesst sich vom Sprachfilter der Planung ueberhaupt
            # aussortieren (sync/planner.py prueft slot.language). Als EXTRA
            # bekaeme der Nutzer die Pakete aller Sprachen.
            self._collect_api_group(
                downloads.get("language_packs"),
                product_id,
                FileKind.PATCH,
                out,
                seen_slots,
                dlc_of,
                variant_prefix=LANGUAGE_PACK_PREFIX,
            )
            self._collect_api_bonus(
                downloads.get("bonus_content"), product_id, out, seen_slots, dlc_of
            )
        else:
            _LOG.debug("Product %s has no downloads block in the product payload", product_id)

        if not include_dlc:
            return
        for dlc in payload.get("expanded_dlcs") or []:
            if not isinstance(dlc, dict):
                continue
            dlc_id = _as_int(dlc.get("id"))
            if dlc_id is None:
                dlc_id = root_id
                _LOG.warning(
                    "DLC without an id of its own in product %s - its files run under "
                    "the product id of the base game",
                    root_id,
                )
            self._collect_product(
                dlc,
                product_id=dlc_id,
                root_id=root_id,
                is_dlc=True,
                include_dlc=include_dlc,
                out=out,
                visited=visited,
                seen_slots=seen_slots,
            )

    def _collect_api_group(
        self,
        entries: Any,
        product_id: int,
        kind: FileKind,
        out: list[RemoteFile],
        seen_slots: set[SlotKey],
        dlc_of: int | None,
        *,
        variant_prefix: str | None,
    ) -> None:
        """``installers``/``patches``/``language_packs`` uebersetzen.

        Ein Eintrag der Liste ist genau EIN Slot; seine ``files`` sind die
        Teile (§5.5).

        Die ``size`` der Payload bleibt liegen - sie ist KEINE Bytegroesse.
        Live gemessen an einem einzelnen Teil::

            size aus api.gog.com : 1048576   <- auf volle MiB gerundet
            Content-Length       :  821824   <- die Wahrheit
            total_size aus XML   :  821824   <- ebenfalls die Wahrheit

        Wer sie trotzdem als ``RemoteFile.size`` durchreicht, fuellt das
        Manifest mit Werten, an denen jede spaetere Groessenpruefung
        scheitert: der Import verwirft dann jede Datei mit "size 821824
        instead of 1048576", und der Store haelt jeden Eintrag fuer
        veraltet. Genau das ist einmal passiert. Die echte Groesse holt
        ``cli/commands.py::_enrich`` aus ``total_size`` des Checksum-XML
        oder per ``content_length``.

        Auch ``total_size`` des Eintrags waere kein Ersatz: bei einem
        mehrteiligen Installer ist es fuer jeden einzelnen Teil die
        falsche Groesse.

        ``variant_prefix`` steuert den Diskriminator: ``None`` heisst "kein
        variant" (Installer sind durch os und Sprache eindeutig), ein
        String heisst "aus der Versionsangabe, mit diesem Praefix". GOG
        bietet pro os und Sprache mehrere Patches mit verschiedenen
        Versionsspannen an; ohne diese Trennung fielen sie in einen Slot.
        """
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            os_name = self._map_os(entry.get("os"))
            if os_name is None:
                continue
            parts = [
                part
                for part in (entry.get("files") or [])
                if isinstance(part, dict) and _api_file_id(part)
            ]
            if not parts:
                continue
            version = _text_or_none(entry.get("version"))
            variant: str | None = None
            if variant_prefix is not None:
                base = _pick_variant((version,), entry.get("id"))
                # Gibt weder Version noch id etwas her, bleibt das nackte
                # Praefix - ohne den Trenner, der sonst ins Leere zeigte.
                raw = f"{variant_prefix}{base}" if base else variant_prefix.rstrip("-")
                variant = _normalize_variant(raw) or None
            slot = SlotKey(
                product_id=product_id,
                kind=kind,
                os=os_name,
                language=self._api_language(entry),
                variant=variant,
            )
            self._register_slot(slot, seen_slots)
            total_parts = len(parts)
            for index, part in enumerate(parts, start=1):
                out.append(
                    RemoteFile(
                        slot=slot,
                        file_id=_api_file_id(part),
                        downlink=str(part.get("downlink") or "").strip(),
                        # ``part["size"]`` ist auf volle MiB gerundet - siehe
                        # die Messung im Docstring. Nur None ist hier ehrlich.
                        size=None,
                        version=version,
                        part_index=index,
                        total_parts=total_parts,
                        dlc_of=dlc_of,
                    )
                )

    def _collect_api_bonus(
        self,
        entries: Any,
        product_id: int,
        out: list[RemoteFile],
        seen_slots: set[SlotKey],
        dlc_of: int | None,
    ) -> None:
        """``bonus_content`` in Extras uebersetzen; je Eintrag ein Slot.

        Wie im Rueckfallweg ohne Plattform und Sprache. Der Diskriminator
        kommt zuerst aus ``name``, dann aus ``type``, zuletzt aus der
        ``file_id`` des ersten Teils. Diese Reihenfolge ist keine
        Geschmacksfrage: der Rueckfallweg leitet ``variant`` ebenfalls aus
        ``name`` mit Rueckfall auf die ``file_id`` ab, und beide Wege
        muessen denselben Slot ergeben. ``type`` zuerst faende fuer zwei
        verschiedene Handbuecher denselben Wert ("manuals").

        Eine Abweichung bleibt und ist nicht aufloesbar: api.gog.com fasst
        einen mehrteiligen Bonus (ein Soundtrack, zwei Dateien) zu EINEM
        Eintrag mit zwei ``files`` zusammen, gameDetails listet dieselben
        Dateien als zwei ``extras``-Eintraege. Tragen die beiden dort
        verschiedene Namen, ergibt der Rueckfallweg zwei Slots, wo dieser
        Weg einen liefert. Der Wechsel des Wegs laesst den einen Slot dann
        neu und die zwei alten verwaist aussehen. Die Buendelung hier ist
        die richtigere - sie ist genau das, was §5.5 verlangt -, aber der
        Unterschied ist real und trifft nur ``bonus_content``.
        """
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            parts = [
                part
                for part in (entry.get("files") or [])
                if isinstance(part, dict) and _api_file_id(part)
            ]
            if not parts:
                _LOG.warning("Skipped a bonus entry without usable files in product %s", product_id)
                continue
            slot = SlotKey(
                product_id=product_id,
                kind=FileKind.EXTRA,
                os=None,
                language=None,
                variant=_pick_variant(
                    (entry.get("name"), entry.get("type")), _api_file_id(parts[0])
                ),
            )
            self._register_slot(slot, seen_slots)
            version = _text_or_none(entry.get("version"))
            total_parts = len(parts)
            for index, part in enumerate(parts, start=1):
                out.append(
                    RemoteFile(
                        slot=slot,
                        file_id=_api_file_id(part),
                        downlink=str(part.get("downlink") or "").strip(),
                        # Gerundet wie in _collect_api_group; dort steht die
                        # Messung. Die echte Groesse holt _enrich.
                        size=None,
                        version=version,
                        part_index=index,
                        total_parts=total_parts,
                        dlc_of=dlc_of,
                    )
                )

    def _api_language(self, entry: dict[str, Any]) -> str | None:
        """Sprachcode eines Produkt-Eintrags.

        ``api.gog.com`` liefert in ``language`` bereits einen Code ("en",
        "de"); der wird nur kleingeschrieben, damit er nicht neben dem
        gleichlautenden Code des Rueckfallwegs einen zweiten Slot aufmacht.
        Die Klartext-Abbildung greift nur, falls dort wider Erwarten ein
        Klartext steht, und ``language_full`` ist der letzte Notnagel.
        """
        raw = entry.get("language")
        text = raw.strip().lower() if isinstance(raw, str) else ""
        if text:
            return LANGUAGE_CODES.get(text, text)
        return self._language_code(entry.get("language_full"))

    def _collect_serials(
        self, payload: Any, fallback_title: str, out: dict[str, str], visited: set[int]
    ) -> None:
        """``cdKey`` des Hauptspiels und seiner DLCs einsammeln."""
        if not isinstance(payload, dict):
            return
        marker = id(payload)
        if marker in visited:
            return
        visited.add(marker)

        key = _repair_serial(payload.get("cdKey"))
        if key:
            title = str(payload.get("title") or "").strip() or fallback_title
            if title in out and out[title] != key:
                _LOG.warning("Two entries share the title %r - only one serial is kept", title)
            out[title] = key
        for dlc in payload.get("dlcs") or []:
            self._collect_serials(dlc, fallback_title, out, visited)

    # -- Payload-Auswertung, Rueckfallweg gameDetails -----------------

    async def _files_from_details(
        self, product_id: int, *, include_dlc: bool
    ) -> list[RemoteFile]:
        """Dateiliste aus gameDetails - der Rueckfallweg.

        Nebenwirkung, die der Primaerweg nicht hat: dieser Abruf loescht
        serverseitig die Aktualisierungsmarker im GOG-Konto des Nutzers.
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
                    "DLC without an id of its own in product %s - its files run under "
                    "the product id of the base game",
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
                _LOG.warning("Skipped an extra without a manualUrl in product %s", product_id)
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
                "Two entries share slot %s - prune treats them as one release",
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
            _LOG.warning("Unknown operating system %r from GOG - entries skipped", raw)
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
                "Unknown language %r from GOG - falling back to code %r",
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
            raise ApiError(f"No valid JSON response from {url}: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(f"Unexpected JSON structure from {url}: {type(data).__name__}")
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
                        f"Network error on {url} after {attempt + 1} attempts: {exc}"
                    ) from exc
                await self._backoff(attempt)
                attempt += 1
                continue

            status = response.status_code
            if status == 401:
                raise AuthError(f"GOG denied access (HTTP 401): {url}")
            if status == 429:
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                if attempt >= self._max_retries:
                    raise RateLimitError(
                        f"GOG is rate-limiting access (HTTP 429): {url}", retry_after=retry_after
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
                        f"HTTP {status} from {url} after {attempt + 1} attempts"
                    )
                await self._backoff(attempt)
                attempt += 1
                continue
            if status >= 400:
                raise ApiError(f"HTTP {status} from {url}")
            return response

    async def _backoff(self, attempt: int) -> None:
        await self._sleep(min(BACKOFF_BASE * (2**attempt), BACKOFF_MAX))
