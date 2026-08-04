"""Argument-Parsing und Einsprungpunkt."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from gogdl import __version__
from gogdl.errors import GogdlError

from . import commands
from .context import _NOTATION_HINT, AppContext, build_sync_config, check_dest

_FILTER_HELP = "Comma-separated, e.g. --lang de,en"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gogdl",
        description=(
            "Download your own GOG.com library, keep it up to date and clean "
            "up old versions."
        ),
    )
    parser.add_argument("--version", action="version", version=f"gogdl {__version__}")
    # Bewusst nicht "." als Default: das Tool legt hier eine Datenbank an,
    # lädt Gigabytes hinein und löscht darin alte Versionen. Ein versehentlicher
    # Aufruf im falschen Verzeichnis - etwa im Quellbaum - soll nichts anrichten.
    parser.add_argument(
        "--dest",
        default=os.environ.get("GOGDL_DEST") or "~/GOG",
        help=(
            "Destination directory of the collection. Without this the "
            "environment variable GOGDL_DEST applies, otherwise ~/GOG"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="More detail")
    parser.add_argument("-q", "--quiet", action="store_true", help="Report errors only")
    parser.add_argument(
        "--json", dest="json_output", action="store_true", help="Machine-readable output"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Sign in to GOG (once)")
    _add_dest(login)
    login.add_argument(
        "--no-browser", action="store_true", help="Do not open the browser automatically"
    )

    update = sub.add_parser("update", help="Fetch metadata from GOG")
    _add_selection(update)
    _add_filters(update)
    update.add_argument("--jobs", type=int, default=4, help="Parallel metadata fetches")

    status = sub.add_parser("status", help="Show what would happen, change nothing")
    _add_selection(status)
    _add_filters(status)
    # Ohne diese Schalter kann status den Aufraeumteil von download nicht
    # vorhersagen, obwohl genau das sein Zweck ist.
    _add_prune_flags(status)

    download = sub.add_parser("download", help="Download missing and outdated files")
    _add_selection(download)
    _add_filters(download)
    _add_prune_flags(download)
    download.add_argument("--jobs", type=int, default=2, help="Parallel downloads")
    download.add_argument(
        "--dry-run", action="store_true", help="Show what would happen, do nothing"
    )
    download.add_argument("--limit-rate", help="Throttle, e.g. 5M")

    verify = sub.add_parser("verify", help="Check local files against the manifest")
    _add_filters(verify)
    verify.add_argument(
        "--deep", action="store_true", help="MD5 and archive test instead of size only"
    )

    imp = sub.add_parser(
        "import",
        help="Match existing files against the manifest (for collections that grew over time)",
    )
    _add_dest(imp)
    _add_saves_flag(imp)
    imp.add_argument("--apply", action="store_true", help="Commit the matching")
    imp.add_argument(
        "--trust",
        choices=["none", "size", "md5"],
        default="none",
        help=(
            "How far existing files count as verified. "
            "none: adopt them, but attest nothing - pruning stays inert. "
            "size: a size match is accepted as evidence. "
            "md5: compute checksums, takes a long time on large collections"
        ),
    )

    clean = sub.add_parser("clean", help="Run the cleanup separately (e.g. after --no-prune)")
    _add_filters(clean)
    _add_prune_flags(clean)
    clean.add_argument("--apply", action="store_true", help="Actually delete")

    sync = sub.add_parser("sync", help="update and download in one call (for cron)")
    _add_selection(sync)
    _add_filters(sync)
    _add_prune_flags(sync)
    sync.add_argument("--jobs", type=int, default=2, help="Parallel downloads")
    sync.add_argument(
        "--dry-run", action="store_true", help="Show what would happen, do nothing"
    )
    sync.add_argument("--limit-rate", help="Throttle, e.g. 5M")

    return parser


def _add_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--only", action="append", default=[], help="Restrict to these games (slug or id)"
    )
    parser.add_argument("--skip", action="append", default=[], help="Exclude these games")


def _add_dest(parser: argparse.ArgumentParser) -> None:
    """Globale Schalter auch nach dem Unterkommando erlauben.

    ``gogdl status --dest ~/GOG -v`` ist die naheliegende Schreibweise; ein
    rein globales Argument würde sie mit einer Fehlermeldung abweisen.
    Eigene Zielnamen, damit die Werte die globalen nur überschreiben, wenn
    sie wirklich angegeben wurden.
    """
    parser.add_argument(
        "--dest",
        dest="dest_local",
        metavar="DEST",
        default=None,
        help="Destination directory of the collection",
    )
    parser.add_argument("-v", "--verbose", dest="verbose_local", action="store_true")
    parser.add_argument("-q", "--quiet", dest="quiet_local", action="store_true")
    parser.add_argument("--json", dest="json_local", action="store_true")


def _add_filters(parser: argparse.ArgumentParser) -> None:
    _add_dest(parser)
    parser.add_argument(
        "--os",
        help=(
            f"Platforms. {_NOTATION_HINT}: "
            "'linux+mac' fetches both, 'mac,windows' takes Windows only where "
            "there is no Mac build. 'all' lifts the restriction. "
            "Default: the platform you are running on"
        ),
    )
    parser.add_argument(
        "--lang",
        help=(
            "Languages, same notation as --os: 'de,en' takes German and falls "
            "back to English, 'de+en' takes both. Default: en"
        ),
    )
    parser.add_argument(
        "--dlc", action="store_true", default=True, help="Include DLC (default)"
    )
    parser.add_argument("--no-dlc", dest="dlc", action="store_false")
    # Goodies sind standardmäßig dabei; abgewählt wird ausdrücklich.
    # ``--extras``/``--no-extras`` bleiben als stille Zweitschreibweise
    # erhalten, damit bestehende Cron-Aufrufe nicht plötzlich abbrechen.
    # Alle drei Aktionen setzen denselben Zielnamen und tragen denselben
    # Vorgabewert, damit eine spätere Umsortierung ihn nicht still dreht.
    parser.add_argument(
        "--skip-goodies",
        dest="extras",
        action="store_false",
        default=True,
        help=(
            "Skip goodies (manuals, maps, wallpapers, soundtracks). "
            "Default: goodies are downloaded"
        ),
    )
    parser.add_argument(
        "--extras", dest="extras", action="store_true", default=True, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--no-extras", dest="extras", action="store_false", default=True, help=argparse.SUPPRESS
    )
    # Patches heben nur von einer Version auf die nächste; gewollt ist der
    # vollständige Installer der aktuellen Version. Sprachpakete hängen mit
    # daran, weil die Schnittstelle sie als FileKind.PATCH einliest
    # (api/client.py) und die Planung nur nach Art unterscheidet.
    parser.add_argument(
        "--include-patches",
        dest="patches",
        action="store_true",
        default=False,
        help=(
            "Also download patch files and language packs. Default: off - the "
            "full installer of the current version is downloaded instead"
        ),
    )
    _add_saves_flag(parser)
    parser.add_argument(
        "--strict", action="store_true", help="Compare MD5 for installers too"
    )


def _add_saves_flag(parser: argparse.ArgumentParser) -> None:
    """``--include-saves`` - betrifft die Platte, nicht GOG.

    Spielstandsordner gehören zu keinem Manifest-Eintrag und füllen sonst
    jede Ausgabe mit "not matchable". Auch ``import`` braucht den Schalter,
    denn genau dort fällt das auf.
    """
    parser.add_argument(
        "--include-saves",
        action="store_true",
        default=False,
        help=(
            "Also look at local save game folders (SaveFiles) when reading the "
            "collection. Default: off - they belong to no manifest entry"
        ),
    )


def _add_prune_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--prune",
        action="store_true",
        default=True,
        help="Remove old versions once a verified replacement exists (default)",
    )
    parser.add_argument(
        "--no-prune", dest="prune", action="store_false", help="Keep every version"
    )
    parser.add_argument(
        "--keep-versions",
        type=int,
        default=1,
        help="How many generations to keep (default 1 = the current one only)",
    )
    parser.add_argument(
        "--prune-mode",
        choices=["delete", "trash"],
        default="delete",
        help="trash moves files to <dest>/.trash instead of deleting them",
    )
    parser.add_argument(
        "--keep-old",
        action="store_true",
        default=False,
        help=(
            "Keep set-aside previous versions (.old). Without this switch they "
            "are removed as soon as the new version is complete and verified"
        ),
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
        raise ValueError(f"Invalid rate: {value!r}") from exc


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
            print(f"Warning: {warning}", file=sys.stderr)

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
                return commands.cmd_status(ctx, only=args.only, skip=args.skip)
            case "download":
                return asyncio.run(
                    commands.cmd_download(ctx, only=args.only, skip=args.skip)
                )
            case "verify":
                return commands.cmd_verify(ctx, deep=args.deep)
            case "import":
                return commands.cmd_import(ctx, trust=args.trust, apply=args.apply)
            case "clean":
                return commands.cmd_clean(ctx, apply=args.apply)
            case "sync":
                return asyncio.run(commands.cmd_sync(ctx, only=args.only, skip=args.skip))
            case _:  # pragma: no cover - argparse verhindert das
                parser.error(f"Unknown command {args.command!r}")
                return 1
    except GogdlError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return exc.exit_code
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Running the command again resumes.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
