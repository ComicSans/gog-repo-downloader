"""Download-Engine: Resume, Range-Handling, ``.part``-Verwaltung, Verifikation."""

from .engine import OLD_SUFFIX, HttpDownloader

__all__ = ["HttpDownloader", "OLD_SUFFIX"]
