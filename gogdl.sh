#!/usr/bin/env bash
# Startet gogdl, ohne dass man den Pfad ins venv kennen muss.
#
# Von ueberall aufrufbar, auch ueber einen Symlink:
#   ln -s /pfad/zu/gog-repo-downloader/gogdl.sh /usr/local/bin/gogdl
#
# Fehlt die Umgebung, wird sie beim ersten Aufruf angelegt.

set -euo pipefail

# Eigenen Ort bestimmen und dabei Symlinks aufloesen. realpath gibt es nicht
# ueberall, deshalb die Schleife.
quelle="${BASH_SOURCE[0]}"
while [ -L "$quelle" ]; do
    verzeichnis="$(cd -P "$(dirname "$quelle")" && pwd)"
    quelle="$(readlink "$quelle")"
    [[ $quelle != /* ]] && quelle="$verzeichnis/$quelle"
done
WURZEL="$(cd -P "$(dirname "$quelle")" && pwd)"

VENV="$WURZEL/.venv"
BIN="$VENV/bin/gogdl"

if [ ! -x "$BIN" ]; then
    echo "Richte die Umgebung ein (einmalig)..." >&2
    if command -v uv >/dev/null 2>&1; then
        uv venv --directory "$WURZEL" >&2
        uv pip install --directory "$WURZEL" -e "$WURZEL" >&2
    elif command -v python3 >/dev/null 2>&1; then
        python3 -m venv "$VENV" >&2
        "$VENV/bin/pip" install --quiet --upgrade pip >&2
        "$VENV/bin/pip" install --quiet -e "$WURZEL" >&2
    else
        echo "Fehler: weder uv noch python3 gefunden. Python 3.12 oder neuer wird gebraucht." >&2
        exit 1
    fi
fi

if [ ! -x "$BIN" ]; then
    echo "Fehler: $BIN fehlt auch nach der Einrichtung. Bitte 'uv pip install -e .' in $WURZEL ausfuehren." >&2
    exit 1
fi

exec "$BIN" "$@"
