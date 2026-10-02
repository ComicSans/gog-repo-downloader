#!/bin/bash
# Schnelle Tests des Projekts: einheitlicher Einstieg in allen Repos unter
# ~/GitHub (social-video T-101, Tobias 02.10.2026). Läuft über die
# Lauf-Warteschlange von local-ci (ohne Gerät); der pre-push-Hook ruft es auf.
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 64
[ -n "${SIM_LAUF_ID:-}" ] || exec "$HOME/GitHub/local-ci/share/sim-lauf.sh" --projekt gog-repo-downloader \
  --zweck "${SIM_LAUF_ZWECK:-test}" --geraet keins -- "$PWD/scripts/test.sh" "$@"
set -euo pipefail
# venv anlegen, wenn es fehlt; Abhängigkeiten bei jedem Lauf abgleichen (uv ist dabei schnell),
# damit geänderte Abhängigkeiten und eine halbe .venv ohne pytest nicht rot machen.
[ -d .venv ] || uv venv -q
uv pip install -q -e ".[dev]"
.venv/bin/pytest -q "$@"
