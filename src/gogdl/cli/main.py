"""Argument-Parsing und Einsprungpunkt."""

from __future__ import annotations

import argparse
import asyncio
import sys

from gogdl import __version__
from gogdl.errors import GogdlError

from . import commands
from .context import AppContext, build_sync_config, check_dest

_FILTER_HELP = "Kommagetrennt, z. B. --lang de,en"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gogdl",
        description=(
            "Lädt die eigene GOG.com-Bibliothek herunter, hält sie aktuell "
            "und räumt alte Versionen auf."
        ),
    )
    parser.add_argument("--version", action="version", version=f"gogdl {__version__}")
    # Bewusst nicht "." als Default: das Tool legt hier eine Datenbank an,
    # lädt Gigabytes hinein und löscht darin alte Versionen. Ein versehentlicher
    # Aufruf im falschen Verzeichnis - etwa im Quellbaum - soll nichts anrichten.
    parser.add_argument(
        "--dest", default="~/GOG", help="Zielverzeichnis der Sammlung (Default: ~/GOG)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Mehr Details")
    parser.add_argument("-q", "--quiet", action="store_true", help="Nur Fehler ausgeben")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Maschinenlesbar")

    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Bei GOG anmelden (einmalig)")
    _add_dest(login)
    login.add_argument(
        "--no-browser", action="store_true", help="Browser nicht automatisch öffnen"
    )

    update = sub.add_parser("update", help="Metadaten von GOG holen")
    _add_selection(update)
    _add_filters(update)
    update.add_argument("--jobs", type=int, default=4, help="Parallele Metadaten-Abrufe")

    status = sub.add_parser("status", help="Zeigen, was zu tun wäre - ohne etwas zu tun")
    _add_filters(status)
    # Ohne diese Schalter kann status den Aufraeumteil von download nicht
    # vorhersagen, obwohl genau das sein Zweck ist.
    _add_prune_flags(status)

    download = sub.add_parser("download", help="Fehlende und veraltete Dateien laden")
    _add_filters(download)
    _add_prune_flags(download)
    download.add_argument("--jobs", type=int, default=2, help="Parallele Downloads")
    download.add_argument("--dry-run", action="store_true", help="Nur zeigen, nichts tun")
    download.add_argument("--limit-rate", help="Drosselung, z. B. 5M")

    verify = sub.add_parser("verify", help="Lokalen Bestand prüfen")
    _add_filters(verify)
    verify.add_argument("--deep", action="store_true", help="MD5 und Archivtest statt nur Größe")

    imp = sub.add_parser(
        "import",
        help="Vorhandenen Bestand dem Manifest zuordnen (für gewachsene Sammlungen)",
    )
    _add_dest(imp)
    imp.add_argument("--apply", action="store_true", help="Zuordnung tatsächlich übernehmen")
    imp.add_argument(
        "--trust",
        choices=["none", "size", "md5"],
        default="none",
        help=(
            "Wie stark der vorhandene Bestand als geprüft gilt. "
            "none: übernehmen, aber nichts bestätigen - Aufräumen bleibt wirkungslos. "
            "size: Größenübereinstimmung genügt als Beleg. "
            "md5: Prüfsummen rechnen, dauert bei großen Sammlungen sehr lange"
        ),
    )

    clean = sub.add_parser("clean", help="Aufräumen nachholen (z. B. nach --no-prune)")
    _add_filters(clean)
    _add_prune_flags(clean)
    clean.add_argument("--apply", action="store_true", help="Tatsächlich löschen")

    sync = sub.add_parser("sync", help="update und download in einem Aufruf (für cron)")
    _add_selection(sync)
    _add_filters(sync)
    _add_prune_flags(sync)
    sync.add_argument("--jobs", type=int, default=2)
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--limit-rate")

    return parser


def _add_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--only", action="append", default=[], help="Nur diese Spiele (slug oder id)")
    parser.add_argument("--skip", action="append", default=[], help="Diese Spiele auslassen")


def _add_dest(parser: argparse.ArgumentParser) -> None:
    """Globale Schalter auch nach dem Unterkommando erlauben.

    ``gogdl status --dest ~/GOG -v`` ist die naheliegende Schreibweise; ein
    rein globales Argument würde sie mit einer Fehlermeldung abweisen.
    Eigene Zielnamen, damit die Werte die globalen nur überschreiben, wenn
    sie wirklich angegeben wurden.
    """
    parser.add_argument(
        "--dest", dest="dest_local", default=None, help="Zielverzeichnis der Sammlung"
    )
    parser.add_argument("-v", "--verbose", dest="verbose_local", action="store_true")
    parser.add_argument("-q", "--quiet", dest="quiet_local", action="store_true")
    parser.add_argument("--json", dest="json_local", action="store_true")


def _add_filters(parser: argparse.ArgumentParser) -> None:
    _add_dest(parser)
    parser.add_argument(
        "--os",
        help=(
            "Plattformen. Komma heißt 'sonst', Plus heißt 'und': "
            "'linux+mac' lädt beide, 'mac,windows' nimmt Windows nur, wenn es "
            "keine Mac-Fassung gibt. 'all' hebt die Auswahl auf. "
            "Default: nur die laufende Plattform"
        ),
    )
    parser.add_argument(
        "--lang",
        help=(
            "Sprachen, gleiche Schreibweise wie --os: 'de,en' nimmt Deutsch "
            "und nur ersatzweise Englisch, 'de+en' beides. Default: en"
        ),
    )
    parser.add_argument("--dlc", action="store_true", default=True, help="DLC einschließen (Default)")
    parser.add_argument("--no-dlc", dest="dlc", action="store_false")
    parser.add_argument("--extras", action="store_true", default=False, help="Extras einschließen")
    parser.add_argument("--no-extras", dest="extras", action="store_false")
    parser.add_argument("--patches", action="store_true", default=False, help="Patches einschließen")
    parser.add_argument("--no-patches", dest="patches", action="store_false")
    parser.add_argument("--strict", action="store_true", help="MD5 auch bei Installern vergleichen")


def _add_prune_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--prune",
        action="store_true",
        default=True,
        help="Alte Versionen nach verifiziertem Ersatz entfernen (Default)",
    )
    parser.add_argument(
        "--no-prune", dest="prune", action="store_false", help="Alle Versionen behalten"
    )
    parser.add_argument(
        "--keep-versions",
        type=int,
        default=1,
        help="Wie viele Generationen behalten werden (Default 1 = nur die aktuelle)",
    )
    parser.add_argument(
        "--prune-mode",
        choices=["delete", "trash"],
        default="delete",
        help="trash verschiebt nach <dest>/.trash statt zu löschen",
    )


def _parse_rate(value: str | None) -> int | None:
    """``5M`` -> 5242880. Ohne Einheit: Bytes."""
    if not value:
        return None
    text = value.strip().upper()
    factor = 1
    if text.endswith("K"):
        factor, text = 1024, text[:-1]
    elif text.endswith("M"):
        factor, text = 1024 * 1024, text[:-1]
    elif text.endswith("G"):
        factor, text = 1024 * 1024 * 1024, text[:-1]
    try:
        return int(float(text) * factor)
    except ValueError as exc:
        raise ValueError(f"Ungültige Rate: {value!r}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        # Die Werte nach dem Unterkommando schlagen die davor.
        if getattr(args, "dest_local", None):
            args.dest = args.dest_local
        args.verbose = args.verbose or getattr(args, "verbose_local", False)
        args.quiet = args.quiet or getattr(args, "quiet_local", False)
        args.json_output = args.json_output or getattr(args, "json_local", False)
        config = build_sync_config(args)
        warning = check_dest(config.dest)
        if warning and args.command != "login":
            print(f"Warnung: {warning}", file=sys.stderr)

        ctx = AppContext(
            dest=config.dest,
            config=config,
            jobs=getattr(args, "jobs", 2),
            dry_run=getattr(args, "dry_run", False),
            quiet=args.quiet,
            verbose=args.verbose,
            json_output=args.json_output,
            limit_rate=_parse_rate(getattr(args, "limit_rate", None)),
        )

        match args.command:
            case "login":
                return asyncio.run(commands.cmd_login(ctx, no_browser=args.no_browser))
            case "update":
                return asyncio.run(commands.cmd_update(ctx, only=args.only, skip=args.skip))
            case "status":
                return commands.cmd_status(ctx)
            case "download":
                return asyncio.run(commands.cmd_download(ctx))
            case "verify":
                return commands.cmd_verify(ctx, deep=args.deep)
            case "import":
                return commands.cmd_import(ctx, trust=args.trust, apply=args.apply)
            case "clean":
                return commands.cmd_clean(ctx, apply=args.apply)
            case "sync":
                return asyncio.run(commands.cmd_sync(ctx, only=args.only, skip=args.skip))
            case _:  # pragma: no cover - argparse verhindert das
                parser.error(f"Unbekanntes Kommando {args.command!r}")
                return 1
    except GogdlError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return exc.exit_code
    except ValueError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAbgebrochen. Ein erneuter Aufruf setzt fort.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
