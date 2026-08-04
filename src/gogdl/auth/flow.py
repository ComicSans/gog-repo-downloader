"""Stub — wird von auth-Agent implementiert."""

from __future__ import annotations

from gogdl.model.types import Credentials


class FileCredentialStore:
    """Implementiert model.protocols.CredentialStore."""

    def __init__(self, path=None) -> None:
        raise NotImplementedError

    def load(self) -> Credentials | None:
        raise NotImplementedError

    def save(self, credentials: Credentials) -> None:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError


class GogAuth:
    """Implementiert model.protocols.AuthProvider."""

    def __init__(self, store, client=None, now=None) -> None:
        raise NotImplementedError

    async def access_token(self) -> str:
        raise NotImplementedError

    def is_authenticated(self) -> bool:
        raise NotImplementedError

    async def exchange_code(self, code: str) -> Credentials:
        raise NotImplementedError


def extract_code(pasted: str) -> str:
    """Akzeptiert die komplette Redirect-URL oder den nackten Code."""
    raise NotImplementedError
