"""Tests der Download-Engine — Schwerpunkt: Range-Verhalten.

Der wichtigste Fall ist ``test_range_ignored_discards_part``: antwortet der
CDN auf einen Range-Request mit ``200``, darf die vorhandene ``.part`` nicht
weitergeschrieben werden. Deshalb prüfen die Tests nicht nur Größen, sondern
den vollständigen Bytevergleich der Zieldatei.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from gogdl.download import HttpDownloader
from gogdl.errors import RangeNotHonoredError
from gogdl.model.protocols import Downloader
from gogdl.model.types import (
    DownloadItem,
    FileKind,
    ManifestEntry,
    OsName,
    ResolvedLink,
    SlotKey,
)

BODY = bytes((i * 7 + 13) % 251 for i in range(200))
"""200 Bytes mit erkennbarem Muster — jede Verschiebung fällt beim Vergleich auf."""

BODY_MD5 = hashlib.md5(BODY).hexdigest()
JUNK = b"X" * 100
"""Altbestand einer ``.part``, der bei einem Neustart verschwinden muss."""


class FakeApi:
    """Minimale ``GogApi``-Attrappe: zählt Auflösungen und variiert die Signatur."""

    def __init__(self) -> None:
        self.calls = 0
        self.urls: list[str] = []

    async def resolve_downlink(self, downlink: str) -> ResolvedLink:
        self.calls += 1
        url = f"https://cdn.example/{downlink}?sig={self.calls}"
        self.urls.append(url)
        return ResolvedLink(url=url, filename="setup.bin")


class FakeHandle:
    """Undurchsichtiges Handle der Attrappe - nur die Identität zählt."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeReporter:
    """``ProgressReporter``-Attrappe. ``start_overall``/``close`` sind verboten.

    Protokolliert zusätzlich, mit welchem Handle jeder Aufruf kam: nur so
    lässt sich prüfen, dass die Engine das Handle aus ``start_file``
    wirklich durchreicht.
    """

    def __init__(self) -> None:
        self.started: list[tuple[str, int | None, int]] = []
        self.advanced: list[int] = []
        self.finished: list[tuple[str, bool, str]] = []
        self.messages: list[str] = []
        self.handles: list[FakeHandle] = []
        self.advance_handles: list[object] = []
        self.finish_handles: list[object] = []
        self.overall_calls = 0
        self.close_calls = 0

    def start_overall(self, total_files: int, total_bytes: int) -> None:
        self.overall_calls += 1

    def start_file(
        self, name: str, total_bytes: int | None, already_done: int = 0
    ) -> object:
        self.started.append((name, total_bytes, already_done))
        handle = FakeHandle(name)
        self.handles.append(handle)
        return handle

    def advance(self, n_bytes: int, handle: object | None = None) -> None:
        self.advanced.append(n_bytes)
        self.advance_handles.append(handle)

    def finish_file(
        self, name: str, ok: bool, detail: str = "", handle: object | None = None
    ) -> None:
        self.finished.append((name, ok, detail))
        self.finish_handles.append(handle)

    def message(self, text: str) -> None:
        self.messages.append(text)

    def close(self) -> None:
        self.close_calls += 1


class FakeSleep:
    """Protokolliert Wartezeiten, statt zu warten."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


class FlakyStream(httpx.AsyncByteStream):
    """Liefert einen Chunk und bricht dann die Verbindung ab."""

    def __init__(self, chunk: bytes) -> None:
        self._chunk = chunk

    async def __aiter__(self):
        yield self._chunk
        raise httpx.ReadError("Verbindung abgebrochen")


def make_item(tmp_path, *, size: int | None = len(BODY), md5: str | None = None) -> DownloadItem:
    slot = SlotKey(product_id=42, kind=FileKind.INSTALLER, os=OsName.WINDOWS, language="en")
    entry = ManifestEntry(
        slot=slot,
        file_id="file-1",
        filename="setup.bin",
        version="1.0",
        size=size,
        md5=md5,
        downlink="downlink/42/file-1",
    )
    # Das Zielverzeichnis existiert bewusst noch nicht.
    return DownloadItem(entry=entry, target=tmp_path / "spiel" / "setup.bin")


def make_downloader(handler, api: FakeApi, sleep: FakeSleep, **kwargs) -> HttpDownloader:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpDownloader(api, client, sleep=sleep, **kwargs)


def partial_response(start: int, total: int = len(BODY)) -> httpx.Response:
    return httpx.Response(
        206,
        content=BODY[start:],
        headers={"Content-Range": f"bytes {start}-{total - 1}/{total}"},
    )


def test_fulfills_downloader_protocol():
    assert isinstance(HttpDownloader(FakeApi()), Downloader)


async def test_fresh_download_renames_after_verification(tmp_path):
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    seen: list[httpx.Request] = []
    item = make_item(tmp_path, md5=BODY_MD5)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=BODY)

    downloader = make_downloader(handler, api, sleep, chunk_size=64)
    result = await downloader.fetch(item, reporter)

    assert result.ok and result.verified and not result.restarted
    assert result.bytes_written == len(BODY)
    assert item.target.read_bytes() == BODY
    assert not item.part_path.exists()
    assert "Range" not in seen[0].headers
    assert reporter.started == [("setup.bin", len(BODY), 0)]
    assert sum(reporter.advanced) == len(BODY)
    assert reporter.finished == [("setup.bin", True, "")]
    assert reporter.overall_calls == 0 and reporter.close_calls == 0


async def test_resume_with_206(tmp_path):
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path)
    item.part_path.parent.mkdir(parents=True)
    item.part_path.write_bytes(BODY[:100])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return partial_response(100)

    downloader = make_downloader(handler, api, sleep)
    result = await downloader.fetch(item, reporter)

    assert result.ok and not result.restarted
    assert result.bytes_written == 100
    assert item.target.read_bytes() == BODY  # Bytevergleich, nicht nur Größe
    assert not item.part_path.exists()
    assert seen[0].headers["Range"] == "bytes=100-"
    assert reporter.started == [("setup.bin", len(BODY), 100)]


async def test_range_ignored_discards_part(tmp_path):
    """Der wichtigste Test: ``200`` auf einen Range-Request darf nie anhängen."""
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path, md5=BODY_MD5)
    item.part_path.parent.mkdir(parents=True)
    item.part_path.write_bytes(JUNK)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=BODY)  # Range wird ignoriert

    downloader = make_downloader(handler, api, sleep)
    result = await downloader.fetch(item, reporter)

    assert result.ok and result.verified
    assert result.restarted is True
    assert item.target.read_bytes() == BODY
    assert JUNK not in item.target.read_bytes()
    assert not item.part_path.exists()
    assert len(seen) == 2
    assert seen[0].headers["Range"] == "bytes=100-"
    assert "Range" not in seen[1].headers
    assert api.calls == 2  # auch der Neustart holt einen frischen Downlink


async def test_wrong_content_range_offset_restarts(tmp_path):
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path, md5=BODY_MD5)
    item.part_path.parent.mkdir(parents=True)
    item.part_path.write_bytes(BODY[:100])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "Range" in request.headers:
            return partial_response(50)  # Startoffset passt nicht zu 100
        return httpx.Response(200, content=BODY)

    downloader = make_downloader(handler, api, sleep)
    result = await downloader.fetch(item, reporter)

    assert result.ok and result.restarted is True
    assert item.target.read_bytes() == BODY
    assert not item.part_path.exists()
    assert len(seen) == 2


async def test_md5_mismatch_fails_without_target(tmp_path):
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path, md5="0" * 32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY)

    downloader = make_downloader(handler, api, sleep)
    result = await downloader.fetch(item, reporter)

    assert result.ok is False and result.verified is False
    assert "MD5" in (result.error or "")
    assert not item.part_path.exists()
    assert not item.target.exists()
    assert reporter.finished[0][1] is False


async def test_md5_correct_on_resume(tmp_path):
    """Der Hash muss den Altbestand der ``.part`` einschließen."""
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path, md5=BODY_MD5)
    item.part_path.parent.mkdir(parents=True)
    item.part_path.write_bytes(BODY[:100])

    def handler(request: httpx.Request) -> httpx.Response:
        return partial_response(100)

    downloader = make_downloader(handler, api, sleep, chunk_size=16)
    result = await downloader.fetch(item, reporter)

    assert result.ok and result.verified
    assert result.error is None
    assert item.target.read_bytes() == BODY


async def test_416_treats_part_as_complete(tmp_path):
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path, md5=BODY_MD5)
    item.part_path.parent.mkdir(parents=True)
    item.part_path.write_bytes(BODY)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(416)

    downloader = make_downloader(handler, api, sleep)
    result = await downloader.fetch(item, reporter)

    assert result.ok and result.verified
    assert result.bytes_written == 0
    assert item.target.read_bytes() == BODY
    assert not item.part_path.exists()
    assert seen[0].headers["Range"] == f"bytes={len(BODY)}-"


async def test_downlink_resolved_on_every_attempt(tmp_path):
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path)
    urls: list[str] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=BODY)

    downloader = make_downloader(handler, api, sleep)
    result = await downloader.fetch(item, reporter)

    assert result.ok
    assert api.calls == 2  # auch nach dem Retry frisch aufgelöst
    assert urls == api.urls
    assert urls[0] != urls[1]  # persistierte URL wird nie wiederverwendet


async def test_429_respects_retry_after(tmp_path):
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, content=BODY)

    downloader = make_downloader(handler, api, sleep)
    result = await downloader.fetch(item, reporter)

    assert result.ok
    assert sleep.slept == [7.0]
    assert item.target.read_bytes() == BODY


async def test_500_retries_then_succeeds(tmp_path):
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(500)
        return httpx.Response(200, content=BODY)

    downloader = make_downloader(handler, api, sleep)
    result = await downloader.fetch(item, reporter)

    assert result.ok
    assert sleep.slept == [1.0, 2.0]  # exponentielles Backoff
    assert item.target.read_bytes() == BODY


async def test_exhausted_retries_keep_part(tmp_path):
    """Netzfehler löschen die ``.part`` nicht — sonst wäre Resume sinnlos."""
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path)
    item.part_path.parent.mkdir(parents=True)
    item.part_path.write_bytes(BODY[:100])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    downloader = make_downloader(handler, api, sleep)
    result = await downloader.fetch(item, reporter)

    assert result.ok is False
    assert item.part_path.read_bytes() == BODY[:100]  # bleibt für den nächsten Lauf
    assert not item.target.exists()
    assert sleep.slept == [1.0, 2.0, 4.0, 8.0, 16.0]  # MAX_RETRIES = 5
    assert api.calls == 6


async def test_oversized_part_is_discarded(tmp_path):
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path, md5=BODY_MD5)
    item.part_path.parent.mkdir(parents=True)
    item.part_path.write_bytes(b"Y" * (len(BODY) + 100))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=BODY)

    downloader = make_downloader(handler, api, sleep)
    result = await downloader.fetch(item, reporter)

    assert result.ok and result.restarted is True
    assert item.target.read_bytes() == BODY
    assert len(seen) == 1
    assert "Range" not in seen[0].headers  # gar nicht erst als Resume versucht
    assert reporter.started == [("setup.bin", len(BODY), 0)]


async def test_repeated_range_rejection_raises(tmp_path):
    """Zweimal ignorierter Range in Folge — der Lauf darf nicht weitermachen."""
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path)
    item.part_path.parent.mkdir(parents=True)
    item.part_path.write_bytes(JUNK)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            # Neustart bricht mitten im Body ab -> es liegen wieder Bytes auf der Platte.
            return httpx.Response(200, stream=FlakyStream(BODY[:50]))
        return httpx.Response(200, content=BODY)

    # Kleine Chunks, damit die abgebrochene Antwort wirklich Bytes hinterlässt.
    downloader = make_downloader(handler, api, sleep, chunk_size=25)
    with pytest.raises(RangeNotHonoredError):
        await downloader.fetch(item, reporter)

    assert calls["n"] == 3
    assert not item.part_path.exists()  # Teildatei verworfen
    assert not item.target.exists()
    assert reporter.finished and reporter.finished[-1][1] is False


# --------------------------------------------------------------------------
# Fortschritts-Handle
# --------------------------------------------------------------------------


async def test_handle_aus_start_file_geht_an_jeden_advance(tmp_path):
    """Ohne durchgereichtes Handle verbucht ``--jobs 2`` Bytes bei der falschen Datei."""
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path, md5=BODY_MD5)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY)

    downloader = make_downloader(handler, api, sleep, chunk_size=16)
    result = await downloader.fetch(item, reporter)

    assert result.ok
    assert len(reporter.handles) == 1  # genau ein ``start_file`` pro Datei
    handle = reporter.handles[0]
    assert len(reporter.advance_handles) > 1  # mehrere Chunks, sonst sagt der Test nichts
    assert all(h is handle for h in reporter.advance_handles)
    assert reporter.finish_handles == [handle]


async def test_handle_bleibt_ueber_einen_neustart_gleich(tmp_path):
    """Auch der verworfene Neustart meldet auf dasselbe Handle."""
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path, md5=BODY_MD5)
    item.part_path.parent.mkdir(parents=True)
    item.part_path.write_bytes(JUNK)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY)  # Range wird ignoriert

    downloader = make_downloader(handler, api, sleep, chunk_size=16)
    result = await downloader.fetch(item, reporter)

    assert result.ok and result.restarted is True
    assert len(reporter.handles) == 1
    handle = reporter.handles[0]
    assert all(h is handle for h in reporter.advance_handles)
    assert reporter.finish_handles == [handle]


async def test_handle_auch_im_fehlerpfad(tmp_path):
    """``RangeNotHonoredError`` schließt dieselbe Zeile, die geöffnet wurde."""
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path)
    item.part_path.parent.mkdir(parents=True)
    item.part_path.write_bytes(JUNK)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(200, stream=FlakyStream(BODY[:50]))
        return httpx.Response(200, content=BODY)

    downloader = make_downloader(handler, api, sleep, chunk_size=25)
    with pytest.raises(RangeNotHonoredError):
        await downloader.fetch(item, reporter)

    assert len(reporter.handles) == 1
    assert reporter.finish_handles == [reporter.handles[0]]


async def test_handle_auch_bei_dateisystemfehler(tmp_path):
    """Der frühe Abbruch öffnet und schließt die Zeile mit demselben Handle."""
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path)
    # Ein Verzeichnis anstelle des ``.part``-Elternteils erzwingt den OSError.
    blocker = item.part_path.parent
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_bytes(b"kein Verzeichnis")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - nie erreicht
        return httpx.Response(200, content=BODY)

    downloader = make_downloader(handler, api, sleep)
    result = await downloader.fetch(item, reporter)

    assert result.ok is False
    assert len(reporter.handles) == 1
    assert reporter.finish_handles == [reporter.handles[0]]


async def test_limit_rate_throttles(tmp_path):
    api, sleep, reporter = FakeApi(), FakeSleep(), FakeReporter()
    item = make_item(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY)

    # 100 Bytes/s -> 200 Bytes brauchen rechnerisch ~2 s
    downloader = make_downloader(handler, api, sleep, chunk_size=50, limit_rate=100)
    result = await downloader.fetch(item, reporter)

    assert result.ok
    assert sleep.slept and sum(sleep.slept) > 0
