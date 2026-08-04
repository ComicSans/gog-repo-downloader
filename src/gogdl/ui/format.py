"""Formatierung von Datenmengen und Zeitspannen.

Beide Funktionen werden nicht nur von der Fortschrittsanzeige, sondern auch
von ``cli/`` gebraucht und sind deshalb über ``gogdl.ui`` exportiert. Die
Basis ist 1024, die Schreibweise folgt der Skizze aus KONZEPT.md §5.4
("8.2/31.5 GB", "11.4 MB/s", "ETA 1m20s").

Beide Funktionen sind bewusst total: unbrauchbare Eingaben (``None``, NaN,
unendlich, negative Dauer) liefern ``"?"`` statt einer Exception. Eine
Fortschrittsanzeige darf einen Download niemals abbrechen.
"""

from __future__ import annotations

import math

_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
"""Einheiten zur Basis 1024. Die letzte Einheit ist die Obergrenze."""

UNKNOWN = "?"
"""Platzhalter für nicht darstellbare Werte."""


def _as_finite_float(value: object) -> float | None:
    """``value`` als endliche Fließkommazahl oder ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def human_bytes(n: object) -> str:
    """Byteanzahl als lesbare Größe zur Basis 1024.

    Ganze Bytes bleiben ohne Nachkommastelle ("512 B"), alles darüber
    bekommt genau eine ("1.5 KB", "4.2 GB"). Sehr große Werte laufen nicht
    aus der Einheitenliste heraus, sondern bleiben bei der letzten Einheit.
    """
    number = _as_finite_float(n)
    if number is None:
        return UNKNOWN

    sign = "-" if number < 0 else ""
    number = abs(number)

    index = 0
    last = len(_BYTE_UNITS) - 1
    while number >= 1024.0 and index < last:
        number /= 1024.0
        index += 1

    if index == 0:
        return f"{sign}{int(number)} {_BYTE_UNITS[0]}"

    # Rundung auf eine Nachkommastelle kann die nächste Einheit erreichen
    # (1023.97 KB -> 1024.0 KB). Dann lieber eine Stufe weiterschalten.
    if round(number, 1) >= 1024.0 and index < last:
        number /= 1024.0
        index += 1

    return f"{sign}{number:.1f} {_BYTE_UNITS[index]}"


def human_duration(seconds: object) -> str:
    """Zeitspanne in Sekunden als kompakte Dauer.

    Gerundet wird zuerst, dann zerlegt — sonst würde 3599.6 s als "60m"
    statt "1h" erscheinen. Die Ausgabe nennt nie mehr als zwei Stufen:
    "42s", "1m20s", "42m", "2h05m", "3d04h".
    """
    total = _as_finite_float(seconds)
    if total is None or total < 0:
        return UNKNOWN

    total_seconds = int(round(total))
    if total_seconds < 60:
        return f"{total_seconds}s"

    minutes, secs = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if secs == 0 else f"{minutes}m{secs:02d}s"

    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if minutes == 0 else f"{hours}h{minutes:02d}m"

    days, hours = divmod(hours, 24)
    return f"{days}d" if hours == 0 else f"{days}d{hours:02d}h"
