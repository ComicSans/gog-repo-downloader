"""Fehlerhierarchie. Wird von allen Paketen genutzt, von keinem erweitert."""

from __future__ import annotations


class GogdlError(Exception):
    """Basisklasse. Die CLI zeigt diese Fehler als Meldung, nicht als Traceback."""

    exit_code = 1


class AuthError(GogdlError):
    """Login fehlt, ist abgelaufen oder wurde von GOG abgelehnt."""

    exit_code = 2


class ApiError(GogdlError):
    """Unerwartete Antwort von GOG."""

    exit_code = 3


class RateLimitError(ApiError):
    """HTTP 429. ``retry_after`` in Sekunden, falls GOG einen Wert nennt."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class DownloadError(GogdlError):
    """Download fehlgeschlagen oder Verifikation nicht bestanden."""

    exit_code = 4


class RangeNotHonoredError(DownloadError):
    """Der CDN hat auf einen Range-Request mit 200 statt 206 geantwortet.

    Anhängen wäre stille Korruption — die Teildatei muss verworfen werden.
    """


class PruneRefused(GogdlError):
    """Eine geplante Löschung hat die Sicherheitsprüfung nicht bestanden.

    Kein Fehler im engeren Sinn: das Sicherheitsnetz hat gegriffen.
    """

    exit_code = 5


class StoreError(GogdlError):
    """Manifest-Datenbank nicht les-/schreibbar."""

    exit_code = 6
