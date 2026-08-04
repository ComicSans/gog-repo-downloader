"""Übernahme eines bereits vorhandenen Bestands in das Manifest.

Wer seine Sammlung mit einem anderen Werkzeug geladen hat, hat Dateien auf
der Platte, die das Manifest nicht kennt. Für ``sync/`` sind sie damit
Fremdbestand und ``prune/`` rührt sie nicht an - sicher, aber nutzlos. Der
Import schließt diese Lücke, indem er vorhandene Dateien den vorhandenen
Manifest-Einträgen zuordnet.

Er ordnet nur zu, wo die Zuordnung eindeutig ist, und er autorisiert damit
allein noch keine Löschung: siehe :func:`apply_import`.
"""

from .scanner import (
    ImportCandidate,
    ImportMatch,
    ImportPlan,
    ImportRejection,
    ImportSummary,
    Trust,
    apply_import,
    match_existing,
)

__all__ = [
    "ImportCandidate",
    "ImportMatch",
    "ImportPlan",
    "ImportRejection",
    "ImportSummary",
    "Trust",
    "apply_import",
    "match_existing",
]
