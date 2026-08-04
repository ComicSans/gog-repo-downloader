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

Dazu kommen zwei Regeln, die den Bestand auf der Platte schützen:

3. Verifiziert wird die fertige ``.part`` auf der Platte, nicht der
   nebenbei mitgerechnete Digest des Streams. Nur so beantwortet die
   Prüfung die Frage „was liegt da" statt „was kam an".
4. Eine bereits vorhandene Zieldatei wird niemals überschrieben. GOG
   liefert neue Fassungen regelmäßig unter identischem Dateinamen
   (KONZEPT.md §4.1); der Altbestand wandert deshalb vor der Umbenennung
   nach ``<name>.old`` (:data:`OLD_SUFFIX`). Löschen darf ihn nur
   ``prune/``, das die Slot-Regel und ``--keep-versions`` kennt.
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

VERIFY_REPORT_STEP = 512 * 1024 * 1024
"""Abstand zwischen zwei Fortschrittsmeldungen der MD5-Prüfung.

Die Prüfung einer 4-GiB-Datei auf einem Netzlaufwerk dauert Minuten. Ohne
Zwischenmeldung sieht ein laufender Download in dieser Zeit aus wie ein
hängender.
"""

from gogdl.constants import OLD_SUFFIX  # noqa: E402  (Re-Export für Bestandscode)
"""Endung, unter der eine vorhandene Zieldatei beiseitegelegt wird.

Bei Namenskollision wird durchnummeriert: ``<name>.old``, ``<name>.old.1``,
``<name>.old.2`` ... Der ursprüngliche Dateiname bleibt dabei Präfix, damit
die Namensheuristik von ``sync/`` und ``prune/`` die Datei weiterhin ihrem
Slot zuordnen kann.
"""


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


def _hash_block(handle: Any, hasher: Any, chunk_size: int) -> int:
    """Liest einen Block und hasht ihn. Läuft im Worker-Thread.

    Absichtlich beides in einem Aufruf: so wandert weder der Block noch die
    CPU-Arbeit des Hashens zurück in den Loop-Thread.
    """
    block = handle.read(chunk_size)
    if block:
        hasher.update(block)
    return len(block)


def _share(done: int, total: int | None) -> str:
    """Fortschritt als Prozentangabe, bei unbekannter Gesamtgröße in MiB."""
    if total is None or total <= 0:
        return f"{done // (1024 * 1024)} MiB"
    return f"{min(100, done * 100 // total)}%"


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
            message = f"Filesystem error: {exc}"
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
            result = self._failure(item, 0, restarted, f"Filesystem error: {exc}")
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
        # Harte Schleifengrenze: Neustarts dürfen das Retry-Budget nicht
        # aufbrauchen, deshalb ein zweiter, unabhängiger Zähler.
        rounds = 0
        max_rounds = self._max_retries + 5

        while True:
            rounds += 1
            if rounds > max_rounds:
                return self._failure(item, written, restarted, "Too many attempts, giving up")

            # Regel 1: die signierte URL wird nie wiederverwendet.
            try:
                link = await self._api.resolve_downlink(entry.downlink)
            except Exception as exc:  # noqa: BLE001 - Fehler der API-Schicht durchreichen
                return self._failure(
                    item, written, restarted, f"Download link could not be resolved: {exc}"
                )

            headers = {"Range": f"bytes={resume_from}-"} if resume_from > 0 else {}
            try:
                async with self._client.stream("GET", link.url, headers=headers) as response:
                    status = response.status_code

                    if status == 429:
                        attempts += 1
                        if attempts > self._max_retries:
                            return self._failure(
                                item, written, restarted, "Rate limit (429) persists"
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
                                item, written, restarted, f"Server error {status} persists"
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
                                f"CDN keeps ignoring Range (200 instead of 206) for "
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
                                    f"Content-Range keeps mismatching "
                                    f"(expected {resume_from}, got {start})"
                                )
                            self._discard(part)
                            resume_from = 0
                            restarted = True
                            continue
                        range_failures = 0

                    if status not in (200, 206):
                        return self._failure(
                            item, written, restarted, f"Unexpected HTTP status {status}"
                        )

                    written += await self._stream_to_part(
                        response, part, resume_from, reporter, progress_handle
                    )
            except httpx.HTTPError as exc:
                attempts += 1
                if attempts > self._max_retries:
                    return self._failure(item, written, restarted, f"Network error: {exc}")
                await self._nap(self._backoff(attempts))
                # Die tatsächlich geschriebenen Bytes sind der neue Startpunkt.
                resume_from = self._part_size(part)
                expected = item.expected_size
                if expected is not None and resume_from >= expected:
                    # Der Body kam vollständig an, erst danach brach die
                    # Verbindung ab. Damit ist der laufende Versuch - auch ein
                    # Neustart von vorn - erfolgreich abgeschlossen; ein später
                    # erneut ignorierter Range ist dann kein Fehler *in Folge*.
                    # Ohne diese Rücksetzung reißt ein einzelner Netzabbruch
                    # den ganzen Lauf mit ``RangeNotHonoredError`` ab.
                    range_failures = 0
                continue

            break

        return await self._finish(item, written, restarted, reporter)

    # -------------------------------------------------------------- Details

    async def _stream_to_part(
        self,
        response: httpx.Response,
        part: Path,
        resume_from: int,
        reporter: ProgressReporter,
        progress_handle: object = None,
    ) -> int:
        """Schreibt den Body nach ``.part`` und meldet jeden Chunk.

        ``progress_handle`` stammt aus ``start_file`` und ordnet die Bytes
        der richtigen Datei zu; ``handle`` ist hier die offene ``.part``.

        Hier wird bewusst *nicht* mitgehasht: verifiziert wird später die
        Datei auf der Platte. Ein Digest aus dem Stream beschreibt nur, was
        durch den Puffer lief - schreiben zwei Läufe dieselbe ``.part``,
        bestünden beide ihre Prüfung, obwohl nur ein Inhalt auf der Platte
        liegt.
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

    async def _finish(
        self,
        item: DownloadItem,
        written: int,
        restarted: bool,
        reporter: ProgressReporter | None = None,
    ) -> DownloadResult:
        """Verifiziert die ``.part`` und benennt sie erst danach atomar um.

        Geprüft wird immer die Datei auf der Platte (:meth:`_digest_file`),
        nie ein während des Streams mitgerechneter Digest - ein fortgesetzter
        Download schließt den Altbestand der ``.part`` damit selbstverständlich
        ein, und ein von fremder Hand veränderter Rest fällt auf.

        Asynchron, weil genau diese Prüfung die Event-Loop nicht blockieren
        darf; ``reporter`` nimmt die Zwischenmeldungen der MD5-Prüfung auf.
        """
        entry = item.entry
        part = item.part_path
        if not part.exists():
            return self._failure(item, written, restarted, "Partial file is missing after the download")

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
                    error=f"Wrong size: {actual} instead of {entry.size} bytes",
                    restarted=restarted,
                )

        if entry.md5:
            checked = True
            digest = await self._digest_file(part, reporter, total=actual)
            if digest.lower() != entry.md5.lower():
                self._discard(part)
                return DownloadResult(
                    item=item,
                    ok=False,
                    bytes_written=written,
                    verified=False,
                    error=f"Wrong MD5: {digest} instead of {entry.md5}",
                    restarted=restarted,
                )

        item.target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(item.target):
            # Am Zielpfad liegt bereits etwas. Es kann nicht das Ergebnis
            # dieses Downloads sein - geschrieben wurde ausschließlich nach
            # ``.part``. Also ist es Altbestand (§4.1: neue Fassung unter
            # identischem Namen).
            if not checked:
                # Ein ungeprüftes Ergebnis darf eine bekannte gute Datei
                # nicht ersetzen. Die ``.part`` bleibt liegen, damit der
                # nächste Lauf mit Prüfsumme darüber entscheiden kann.
                return self._failure(
                    item,
                    written,
                    restarted,
                    "Existing target file not replaced: neither size nor MD5 is checkable",
                )
            try:
                self._preserve_existing(item.target)
            except OSError as exc:
                return self._failure(
                    item, written, restarted, f"Could not set the old version aside: {exc}"
                )
        os.replace(part, item.target)
        return DownloadResult(
            item=item,
            ok=True,
            bytes_written=written,
            verified=checked,
            error=None,
            restarted=restarted,
        )

    @staticmethod
    def _preserve_existing(target: Path) -> Path:
        """Vorhandene Zieldatei beiseitelegen und den frei gewordenen Pfad melden.

        GOG stellt neue Fassungen regelmäßig unter identischem Dateinamen ein
        (KONZEPT.md §4.1) - der Zielpfad des neuen Downloads ist dann exakt
        der Pfad der alten Datei. Ein ``os.replace`` darüber vernichtet sie an
        Ort und Stelle. Bei einem zweiteiligen Installer genügt dann ein
        Abbruch zwischen Teil 1 und Teil 2, und es existiert überhaupt keine
        vollständige Fassung mehr: Teil 1 ist neu, Teil 2 alt. Weder die
        Slot-Regel noch ``--keep-versions 2`` greifen da noch, denn zerstört
        wird, bevor irgendein Aufräumschritt erreicht ist.

        Die Altdatei wandert deshalb nach ``<name>.old`` im selben
        Verzeichnis; ist der Name belegt, wird durchnummeriert, damit auch
        das zweite Update in Folge nichts überschreibt. Gelöscht wird hier
        nichts - das ist Sache von ``prune/``, das als einziges weiß, wann
        eine Altfassung entbehrlich ist.
        """
        candidate = target.with_name(target.name + OLD_SUFFIX)
        counter = 1
        while os.path.lexists(candidate):
            candidate = target.with_name(f"{target.name}{OLD_SUFFIX}.{counter}")
            counter += 1
        os.replace(target, candidate)
        return candidate

    async def _digest_file(
        self,
        path: Path,
        reporter: ProgressReporter | None = None,
        *,
        total: int | None = None,
    ) -> str:
        """MD5 der Datei auf der Platte, ohne die Event-Loop zu blockieren.

        Gelesen und gehasht wird blockweise in einem Worker-Thread. Lief das
        synchron in der Loop, stand während der Prüfung einer 4-GiB-Datei auf
        einem Netzlaufwerk minutenlang der gesamte Lauf still: der zweite
        Auftrag (``--jobs 2``) schrieb kein Byte mehr, seine kurzlebige
        CDN-URL lief derweil weiter ab, und die Fortschrittsanzeige rührte
        sich nicht - von außen nicht von einem Absturz zu unterscheiden.

        Gemeldet wird zwischen den Blöcken und damit im Loop-Thread:
        ``ProgressReporter`` ist nicht threadsicher.
        """
        hasher = hashlib.md5()
        handle = await asyncio.to_thread(open, path, "rb")
        try:
            done = 0
            reported = 0
            next_report = VERIFY_REPORT_STEP
            while True:
                read = await asyncio.to_thread(_hash_block, handle, hasher, self._chunk_size)
                if not read:
                    break
                done += read
                if reporter is not None and done >= next_report:
                    reporter.message(f"Verifying {path.name}: {_share(done, total)}")
                    reported = done
                    next_report = done + VERIFY_REPORT_STEP
            # Die Dateigröße ist kein Vielfaches des Meldeabstands: der letzte
            # Abschnitt bliebe sonst stumm und die Prüfung endete sichtbar bei
            # 87 statt bei 100 Prozent. Nur melden, wenn überhaupt gemeldet
            # wurde - eine kleine Datei ist ohne Zwischenstand schnell genug.
            if reporter is not None and reported and reported < done:
                reporter.message(f"Verifying {path.name}: {_share(done, total)}")
        finally:
            await asyncio.to_thread(handle.close)
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
