"""Stub — wird von download-Agent implementiert."""

from __future__ import annotations


class HttpDownloader:
    """Implementiert model.protocols.Downloader."""

    def __init__(self, api, client=None, limit_rate=None) -> None:
        raise NotImplementedError

    async def fetch(self, item, reporter):
        raise NotImplementedError
