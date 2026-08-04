"""Stub — wird von api-Agent implementiert."""

from __future__ import annotations


class GogApiClient:
    """Implementiert model.protocols.GogApi."""

    def __init__(self, auth, client=None) -> None:
        raise NotImplementedError

    async def user_data(self):
        raise NotImplementedError

    async def library(self):
        raise NotImplementedError

    async def product_files(self, product_id: int, *, include_dlc: bool = True):
        raise NotImplementedError

    async def resolve_downlink(self, downlink: str):
        raise NotImplementedError

    async def checksum(self, checksum_url: str):
        raise NotImplementedError
