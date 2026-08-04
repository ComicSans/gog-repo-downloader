"""Vergleich Remote↔Lokal → Arbeitsliste und Prune-Plan (KONZEPT.md §7).

Bewusst frei von I/O: Eingabe sind Metadaten und ein beobachteter
Plattenzustand, Ausgabe ist ein Plan. Dadurch sind „ist das veraltet?"
und „darf das gelöscht werden?" ohne Netzwerk und ohne GOG-Konto testbar.
"""

from __future__ import annotations

from .planner import is_stale, plan_downloads, plan_prune

__all__ = ["is_stale", "plan_downloads", "plan_prune"]
