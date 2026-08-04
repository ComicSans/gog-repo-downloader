"""Download-Engine: Resume, Range-Handling, ``.part``-Verwaltung, Verifikation.

Der teuerste Fehler dieses Moduls ist kein Absturz, sondern eine Datei, die
vollständig aussieht und es nicht ist. Zwei Regeln aus KONZEPT.md §5.1/§5.2
verhindern das und bestimmen deshalb den gesamten Aufbau:

1. Die signierte CDN-URL ist kurzlebig. Sie wird vor **jedem** Versuch frisch
   über ``GogApi.resolve_downlink()`` aufgelöst und niemals aus dem Manifest
   wiederverwendet.
2. Ein Range-Request muss mit ``206`` beantwortet werden. Ein ``200`` heißt:
   der CDN liefert die Datei von vorn. Wird dieser Body an eine vorhandene
   ``.part`` angehängt, entsteht stille Korruption. Die Teildatei wird
   deshalb verworfen und sauber neu geladen.

Geschrieben wird ausschließlich nach ``item.part_path``; die Umbenennung
nach ``item.target`` erfolgt atomar und erst nach bestandener Verifikation.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import re
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from ..constants import CHUNK_SIZE, DEFAULT_TIMEOUT, MAX_RETRIES, USER_AGENT
from ..errors import RangeNotHonoredError
from ..model.protocols import ProgressReporter
from ..model.types import DownloadItem, DownloadResult

_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)

BACKOFF_BASE = 1.0
"""Erste Wartezeit in Sekunden; verdoppelt sich mit jedem Fehlversuch."""

BACKOFF_CAP = 60.0
"""Obergrenze für einen einzelnen Backoff-Schlaf."""

RETRY_AFTER_CAP = 300.0
"""Obergrenze für ein von GOG genanntes ``Retry-After`` — schützt vor Hängern."""


def _parse_content_range_start(value: str | None) -> int | None:
    """Startoffset aus ``Content-Range: bytes 100-199/200``. ``None`` bei Unfug."""
    if not value:
        return None
    match = _CONTENT_RANGE_RE.search(value)
    if match is None:
        return None
    return int(match.group(1))


def _parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` als Sekunden — akzeptiert Zahl und HTTP-Datum."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    if target.tzinfo is None:
        target = target.replace(tzinfo=_dt.timezone.utc)
    return max(0.0, (target - now).total_seconds())


class HttpDownloader:
    """Implementiert ``model.protocols.Downloader``.

    Alles Nicht-Deterministische ist injizierbar, damit der Range-Pfad ohne
    Netz testbar bleibt: ``client`` (z. B. mit ``httpx.MockTransport``),
    ``sleep`` (synchron oder asynchron) und ``api`` (braucht nur
    ``resolve_downlink``).

    Hinweis zum Fortschritt: ``start_file`` wird genau einmal pro Datei
    gerufen; das dabei zurückgegebene Handle geht an jedes ``advance`` und
    an ``finish_file`` zurück, damit parallele Downloads ihre Bytes nicht
    gegenseitig zurechnen. Muss eine ``.part`` verworfen werden, summiert
    sich ``advance`` danach über die Dateigröße hinaus - das ist gewollt,
    denn die Bytes sind tatsächlich zweimal geflossen. ``start_overall``/``close`` ruft der
    Aufrufer, nicht dieses Modul.
    """

    def __init__(
        self,
        api: Any,
        client: httpx.AsyncClient | None = None,
        limit_rate: int | None = None,
        *,
        sleep: Callable[[float], Awaitable[None] | None] | None = None,
        max_retries: int = MAX_RETRIES,
        chunk_size: int = CHUNK_SIZE,
    ) -> None:
        self._api = api
        self._client = client or httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self._limit_rate = limit_rate if limit_rate and limit_rate > 0 else None
        self._sleep = sleep or asyncio.sleep
        self._max_retries = max_retries
        self._chunk_size = chunk_size

    # ------------------------------------------------------------------ API

    async def fetch(self, item: DownloadItem, reporter: ProgressReporter) -> DownloadResult:
        """Lädt eine Datei vollständig und verifiziert sie.

        Fehler führen zu ``DownloadResult(ok=False)`` statt zu einer Exception,
        damit ein Lauf über viele Dateien weiterläuft. Einzige Ausnahme ist
        ``RangeNotHonoredError``: ignoriert der CDN Ranges wiederholt, ist der
        Lauf als Ganzes nicht mehr vertrauenswürdig.
        """
        name = item.entry.filename or item.target.name
        part = item.part_path
        expected = item.expected_size
        resume_from = 0
        restarted = False

        try:
            part.parent.mkdir(parents=True, exist_ok=True)
            # Die Datei auf der Platte ist die Wahrheit, nicht ``item.resume_from``
            # und nicht die DB (KONZEPT.md §5.2).
            resume_from = self._part_size(part)
            if expected is not None and resume_from > expected:
                # Zu groß heißt: das kann kein Präfix der erwarteten Datei sein.
                self._discard(part)
                resume_from = 0
                restarted = True
        except OSError as exc:
            message = f"Dateisystemfehler: {exc}"
            progress_handle = reporter.start_file(name, expected, 0)
            reporter.finish_file(name, False, message, handle=progress_handle)
            return self._failure(item, 0, restarted, message)

        # Das Handle gehört genau dieser Datei und geht an jedes ``advance``
        # zurück. Ohne es rechnen bei ``--jobs 2`` zwei gleichzeitige
        # Downloads ihre Bytes gegenseitig der falschen Zeile zu.
        progress_handle = reporter.start_file(name, expected, resume_from)
        try:
            result = await self._run(item, reporter, progress_handle, resume_from, restarted)
        except RangeNotHonoredError as exc:
            self._discard(part)
            reporter.finish_file(name, False, str(exc), handle=progress_handle)
            raise
        except OSError as exc:
            # Volle Platte, fehlende Rechte: ein Lauf über viele Dateien soll
            # daran nicht sterben. Die ``.part`` bleibt liegen.
            result = self._failure(item, 0, restarted, f"Dateisystemfehler: {exc}")
        reporter.finish_file(name, result.ok, result.error or "", handle=progress_handle)
        return result

    # ------------------------------------------------------------- Ablauf

    async def _run(
        self,
        item: DownloadItem,
        reporter: ProgressReporter,
        progress_handle: object,
        resume_from: int,
        restarted: bool,
    ) -> DownloadResult:
        entry = item.entry
        part = item.part_path
        attempts = 0  # verbrauchte Netz-Wiederholungen
        range_failures = 0  # ignorierte Ranges *in Folge*
        written = 0
        digest: str | None = None
        # Harte Schleifengrenze: Neustarts dürfen das Retry-Budget nicht
        # aufbrauchen, deshalb ein zweiter, unabhängiger Zähler.
        rounds = 0
        max_rounds = self._max_retries + 5

        while True:
            rounds += 1
            if rounds > max_rounds:
                return self._failure(item, written, restarted, "Zu viele Versuche abgebrochen")

            # Regel 1: die signierte URL wird nie wiederverwendet.
            try:
                link = await self._api.resolve_downlink(entry.downlink)
            except Exception as exc:  # noqa: BLE001 - Fehler der API-Schicht durchreichen
                return self._failure(
                    item, written, restarted, f"Downlink nicht auflösbar: {exc}"
                )

            headers = {"Range": f"bytes={resume_from}-"} if resume_from > 0 else {}
            try:
                async with self._client.stream("GET", link.url, headers=headers) as response:
                    status = response.status_code

                    if status == 429:
                        attempts += 1
                        if attempts > self._max_retries:
                            return self._failure(
                                item, written, restarted, "Rate-Limit (429) bleibt bestehen"
                            )
                        wait = _parse_retry_after(response.headers.get("Retry-After"))
                        if wait is None:
                            wait = self._backoff(attempts)
                        await self._nap(min(wait, RETRY_AFTER_CAP))
                        resume_from = self._part_size(part)
                        continue

                    if status >= 500:
                        attempts += 1
                        if attempts > self._max_retries:
                            return self._failure(
                                item, written, restarted, f"Serverfehler {status} bleibt bestehen"
                            )
                        await self._nap(self._backoff(attempts))
                        resume_from = self._part_size(part)
                        continue

                    if status == 416 and resume_from > 0:
                        # Der Bereich liegt hinter dem Dateiende: die ``.part``
                        # ist mindestens so groß wie die Datei selbst. Nicht
                        # weiterladen, sondern prüfen.
                        break

                    if resume_from > 0 and status == 200:
                        # Range ignoriert — anhängen wäre stille Korruption.
                        range_failures += 1
                        if range_failures >= 2:
                            raise RangeNotHonoredError(
                                f"CDN ignoriert Range wiederholt (200 statt 206) für "
                                f"{entry.filename or item.target.name}"
                            )
                        self._discard(part)
                        resume_from = 0
                        restarted = True
                        continue

                    if resume_from > 0 and status == 206:
                        start = _parse_content_range_start(response.headers.get("Content-Range"))
                        if start != resume_from:
                            range_failures += 1
                            if range_failures >= 2:
                                raise RangeNotHonoredError(
                                    f"Content-Range passt wiederholt nicht "
                                    f"(erwartet {resume_from}, erhalten {start})"
                                )
                            self._discard(part)
                            resume_from = 0
                            restarted = True
                            continue
                        range_failures = 0

                    if status not in (200, 206):
                        return self._failure(
                            item, written, restarted, f"Unerwarteter HTTP-Status {status}"
                        )

                    hasher = self._make_hasher(entry.md5, part, resume_from)
                    written += await self._stream_to_part(
                        response, part, resume_from, hasher, reporter, progress_handle
                    )
                    digest = hasher.hexdigest() if hasher is not None else None
            except httpx.HTTPError as exc:
                attempts += 1
                if attempts > self._max_retries:
                    return self._failure(item, written, restarted, f"Netzwerkfehler: {exc}")
                await self._nap(self._backoff(attempts))
                # Die tatsächlich geschriebenen Bytes sind der neue Startpunkt.
                resume_from = self._part_size(part)
                digest = None
                continue

            break

        return self._finish(item, written, restarted, digest)

    # -------------------------------------------------------------- Details

    async def _stream_to_part(
        self,
        response: httpx.Response,
        part: Path,
        resume_from: int,
        hasher: Any,
        reporter: ProgressReporter,
        progress_handle: object = None,
    ) -> int:
        """Schreibt den Body nach ``.part`` und meldet jeden Chunk.

        ``progress_handle`` stammt aus ``start_file`` und ordnet die Bytes
        der richtigen Datei zu; ``handle`` ist hier die offene ``.part``.
        """
        written = 0
        started = time.monotonic()
        if resume_from > 0:
            handle = open(part, "r+b")
            handle.seek(resume_from)
            handle.truncate()
        else:
            handle = open(part, "wb")
        try:
            async for chunk in response.aiter_bytes(self._chunk_size):
                if not chunk:
                    continue
                handle.write(chunk)
                if hasher is not None:
                    hasher.update(chunk)
                written += len(chunk)
                reporter.advance(len(chunk), progress_handle)
                await self._throttle(written, started)
        finally:
            handle.close()
        return written

    async def _throttle(self, written: int, started: float) -> None:
        """Einfache Drossel: schläft, bis die Sollzeit für ``written`` erreicht ist."""
        if self._limit_rate is None:
            return
        due = written / self._limit_rate
        elapsed = time.monotonic() - started
        if due > elapsed:
            await self._nap(due - elapsed)

    def _make_hasher(self, expected_md5: str | None, part: Path, resume_from: int) -> Any:
        """MD5-Kontext, beim Resume mit dem Altbestand vorbefüllt.

        Ohne diese Vorbefüllung hasht ein fortgesetzter Download nur den
        neuen Teil und die Prüfung schlägt grundlos fehl.
        """
        if not expected_md5:
            return None
        hasher = hashlib.md5()
        if resume_from > 0 and part.exists():
            with open(part, "rb") as handle:
                remaining = resume_from
                while remaining > 0:
                    block = handle.read(min(self._chunk_size, remaining))
                    if not block:
                        break
                    hasher.update(block)
                    remaining -= len(block)
        return hasher

    def _finish(
        self, item: DownloadItem, written: int, restarted: bool, digest: str | None
    ) -> DownloadResult:
        """Verifiziert die ``.part`` und benennt sie erst danach atomar um."""
        entry = item.entry
        part = item.part_path
        if not part.exists():
            return self._failure(item, written, restarted, "Teildatei fehlt nach dem Download")

        actual = part.stat().st_size
        checked = False

        if entry.size is not None:
            checked = True
            if actual != entry.size:
                self._discard(part)
                return DownloadResult(
                    item=item,
                    ok=False,
                    bytes_written=written,
                    verified=False,
                    error=f"Größe falsch: {actual} statt {entry.size} Bytes",
                    restarted=restarted,
                )

        if entry.md5:
            checked = True
            if digest is None:
                digest = self._digest_file(part)
            if digest.lower() != entry.md5.lower():
                self._discard(part)
                return DownloadResult(
                    item=item,
                    ok=False,
                    bytes_written=written,
                    verified=False,
                    error=f"MD5 falsch: {digest} statt {entry.md5}",
                    restarted=restarted,
                )

        item.target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, item.target)
        return DownloadResult(
            item=item,
            ok=True,
            bytes_written=written,
            verified=checked,
            error=None,
            restarted=restarted,
        )

    def _digest_file(self, path: Path) -> str:
        hasher = hashlib.md5()
        with open(path, "rb") as handle:
            while True:
                block = handle.read(self._chunk_size)
                if not block:
                    break
                hasher.update(block)
        return hasher.hexdigest()

    @staticmethod
    def _failure(
        item: DownloadItem, written: int, restarted: bool, message: str
    ) -> DownloadResult:
        """Abbruch ohne Verifikationsfehler — die ``.part`` bleibt für den Resume liegen."""
        return DownloadResult(
            item=item,
            ok=False,
            bytes_written=written,
            verified=False,
            error=message,
            restarted=restarted,
        )

    @staticmethod
    def _part_size(part: Path) -> int:
        return part.stat().st_size if part.exists() else 0

    @staticmethod
    def _discard(part: Path) -> None:
        """Teildatei wegwerfen. Fehlt sie schon, ist nichts zu tun."""
        try:
            part.unlink()
        except FileNotFoundError:
            pass

    def _backoff(self, attempt: int) -> float:
        return min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)

    async def _nap(self, seconds: float) -> None:
        """Erlaubt sowohl ``asyncio.sleep`` als auch eine synchrone Test-Attrappe."""
        result = self._sleep(seconds)
        if inspect.isawaitable(result):
            await result
