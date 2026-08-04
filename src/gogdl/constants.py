"""Feste Endpunkte und Galaxy-Client-Credentials.

Die Credentials sind die öffentlich dokumentierten Werte des GOG-Galaxy-
Clients; dieselben verwendet auch lgogdownloader. Sie identifizieren den
Client, nicht den Nutzer.
"""

from __future__ import annotations

GALAXY_CLIENT_ID = "46899977096215655"
GALAXY_CLIENT_SECRET = "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"
REDIRECT_URI = "https://embed.gog.com/on_login_success?origin=client"

AUTH_URL = "https://auth.gog.com/auth"
TOKEN_URL = "https://auth.gog.com/token"

EMBED_BASE = "https://embed.gog.com"
API_BASE = "https://api.gog.com"

USER_DATA_URL = f"{EMBED_BASE}/userData.json"
FILTERED_PRODUCTS_URL = f"{EMBED_BASE}/account/getFilteredProducts"
PRODUCT_URL = f"{API_BASE}/products/{{product_id}}"

USER_AGENT = "gogdl/0.1 (+https://github.com/ComicSans/gog-repo-downloader)"

DEFAULT_JOBS = 2
DEFAULT_TIMEOUT = 30.0
"""Verbindungs-/Lese-Timeout in Sekunden für Metadaten-Requests."""

MAX_RETRIES = 5

CHUNK_SIZE = 8 * 1024 * 1024
"""Blockgröße für Schreiben und Verifizieren.

Nicht die Bandbreite begrenzt ein Netzlaufwerk, sondern die Latenz pro
Roundtrip: auf einer SMB-Freigabe brachte dieselbe Datei mit 1-MiB-Blöcken
2.6 MB/s und mit 8-MiB-Blöcken 3.9 MB/s. Bei ``--jobs`` gleichzeitigen
Aufträgen hält der Prozess entsprechend viele Blöcke gleichzeitig im
Speicher, deshalb nicht beliebig groß.
"""

TRASH_DIRNAME = ".trash"

OLD_SUFFIX = ".old"
"""Endung für eine beiseitegelegte Vorgängerfassung.

Der Downloader vergibt sie, wenn GOG eine neue Fassung unter identischem
Dateinamen ausliefert; ``prune/`` räumt sie später auf. Die Konstante steht
hier und nicht im Downloader, damit ``sync/`` sie nutzen kann, ohne die
Download-Schicht und damit ``httpx`` zu importieren - dieses Modul soll
frei von I/O-Abhängigkeiten bleiben.
"""

STATE_DIRNAME = ".gogdl"
DB_FILENAME = "manifest.sqlite3"

LOCK_FILENAME = "lock"
"""Sperrdatei im Zustandsordner, die schreibende Läufe gegeneinander abgrenzt."""


def login_url() -> str:
    """Vollständige URL, die der Nutzer im Browser öffnet."""
    from urllib.parse import urlencode

    params = {
        "client_id": GALAXY_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "layout": "client2",
    }
    return f"{AUTH_URL}?{urlencode(params)}"
