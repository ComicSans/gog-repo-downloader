"""Die Kommandos. Hier laufen die Fachmodule zusammen — sonst nirgends."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx

from gogdl.api.client import GogApiClient
from gogdl.auth.flow import FileCredentialStore, GogAuth, extract_code, start_login
from gogdl.constants import DEFAULT_TIMEOUT, USER_AGENT
from gogdl.download.engine import HttpDownloader
from gogdl.errors import AuthError, GogdlError
from gogdl.model.types import (
    DownloadItem,
    LocalState,
    ManifestEntry,
    ProductRef,
    RemoteFile,
    SyncPlan,
)
from gogdl.prune.executor import PruneExecutor, freed_bytes
from gogdl.store.sqlite_store import SqliteStore
from gogdl.sync.planner import plan_downloads, plan_prune
from gogdl.ui import human_bytes, make_reporter

from .context import AppContext, db_path, now_utc, scan_disk

EXIT_NOTHING_TO_DO = 0
EXIT_WORK_DONE = 10


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


def _auth() -> GogAuth:
    return GogAuth(FileCredentialStore())


# --------------------------------------------------------------------------- login


async def cmd_login(ctx: AppContext, *, no_browser: bool = False) -> int:
    url = start_login(open_browser=not no_browser)
    print("Melde dich in deinem Browser bei GOG an:")
    print(f"\n  {url}\n")
    print(
        "Nach erfolgreicher Anmeldung landest du auf einer Seite, die vermutlich leer aussieht.\n"
        "Kopiere die komplette Adresszeile (sie enthält ?code=...) und füge sie hier ein."
    )
    try:
        pasted = input("\nURL oder Code: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAbgebrochen.")
        return 1

    code = extract_code(pasted)
    async with _http_client() as client:
        auth = GogAuth(FileCredentialStore(), client=client)
        await auth.exchange_code(code)
        api = GogApiClient(auth, client=client)
        user = await api.user_data()

    print(f"Angemeldet als {user.username}.")
    return 0


# --------------------------------------------------------------------------- update


async def cmd_update(ctx: AppContext, *, only: list[str], skip: list[str]) -> int:
    store = SqliteStore(db_path(ctx.dest))
    reporter = make_reporter(quiet=ctx.quiet)
    try:
        async with _http_client() as client:
            api = GogApiClient(_auth(), client=client)
            products = await api.library()
            products = _filter_products(products, only, skip)
            store.replace_products(products)

            reporter.message(f"{len(products)} Produkte in der Bibliothek.")
            seen = now_utc()
            semaphore = asyncio.Semaphore(ctx.jobs)

            async def one(product: ProductRef) -> None:
                async with semaphore:
                    files = await api.product_files(
                        product.product_id, include_dlc=ctx.config.include_dlc
                    )
                    known = {
                        (e.slot, e.file_id): e for e in store.entries(product.product_id)
                    }
                    enriched = await _enrich(api, files, known)
                    store.replace_remote(product.product_id, enriched, seen)
                    if not ctx.quiet:
                        reporter.message(f"  {product.title}: {len(enriched)} Dateien")

            await asyncio.gather(*(one(p) for p in products))
    finally:
        reporter.close()
        store.close()
    return 0


async def _enrich(
    api: GogApiClient,
    files: list[RemoteFile],
    known: dict[tuple, ManifestEntry],
) -> list[RemoteFile]:
    """Dateiname und MD5 ergänzen — nur wo nötig.

    Beides steckt nicht in der Produkt-Payload, sondern hinter je einem
    eigenen Request. Deshalb wird nur aufgelöst, was neu ist oder sich in
    ``version``/``size`` geändert hat; alles andere übernimmt die bekannten
    Werte aus dem Manifest. Der erste Lauf ist dadurch teuer, jeder weitere
    fast umsonst.
    """
    result: list[RemoteFile] = []
    for remote in files:
        previous = known.get((remote.slot, remote.file_id))
        unchanged = (
            previous is not None
            and previous.filename
            and previous.version == remote.version
            and previous.size == remote.size
        )
        if unchanged:
            result.append(
                replace(remote, filename=previous.filename, md5=previous.md5)
            )
            continue

        link = await api.resolve_downlink(remote.downlink)
        checksum = await api.checksum(link.checksum_url) if link.checksum_url else None
        result.append(
            replace(
                remote,
                filename=link.filename,
                md5=checksum.md5 if checksum else None,
                size=remote.size or (checksum.total_size if checksum else None),
            )
        )
    return result


def _filter_products(
    products: list[ProductRef], only: list[str], skip: list[str]
) -> list[ProductRef]:
    def matches(product: ProductRef, needles: list[str]) -> bool:
        keys = {product.slug.lower(), str(product.product_id)}
        return any(n.lower() in keys for n in needles)

    if only:
        products = [p for p in products if matches(p, only)]
    if skip:
        products = [p for p in products if not matches(p, skip)]
    return products


# --------------------------------------------------------------------------- status


def _slugs(store: SqliteStore) -> dict[int, str]:
    """Produkt-ID -> Verzeichnisname. Bestimmt das flache Ablagelayout."""
    return {p.product_id: p.slug for p in store.products()}


def _plan_prune(store: SqliteStore, ctx: AppContext) -> SyncPlan:
    return plan_prune(
        store.entries(),
        ctx.config,
        on_disk=scan_disk(ctx.dest),
        slugs=_slugs(store),
    )


def cmd_status(ctx: AppContext) -> int:
    store = SqliteStore(db_path(ctx.dest))
    try:
        plan = _build_download_plan(store, ctx)
        prune_plan = _plan_prune(store, ctx)
    finally:
        store.close()

    _print_plan(plan, prune_plan, ctx)
    return EXIT_WORK_DONE if (plan.downloads or prune_plan.prunes) else EXIT_NOTHING_TO_DO


def _build_download_plan(store: SqliteStore, ctx: AppContext) -> SyncPlan:
    entries = store.entries()
    slugs = _slugs(store)
    remote = [
        RemoteFile(
            slot=e.slot,
            file_id=e.file_id,
            downlink=e.downlink,
            filename=e.filename,
            size=e.size,
            md5=e.md5,
            version=e.version,
            part_index=e.part_index,
            total_parts=e.total_parts,
            dlc_of=e.dlc_of,
        )
        for e in entries
        if e.state is not LocalState.ORPHANED
    ]
    return plan_downloads(remote, entries, ctx.config, on_disk=scan_disk(ctx.dest), slugs=slugs)


def _print_plan(plan: SyncPlan, prune_plan: SyncPlan, ctx: AppContext) -> None:
    if plan.downloads:
        print(
            f"Zu laden: {len(plan.downloads)} Dateien, "
            f"{human_bytes(plan.download_bytes)}"
        )
        if ctx.verbose:
            for item in plan.downloads:
                marker = "fortsetzen" if item.resume_from else "neu"
                print(f"  [{marker}] {item.target.name}")
    else:
        print("Zu laden: nichts.")

    if prune_plan.prunes:
        print(
            f"Aufzuräumen: {len(prune_plan.prunes)} alte Dateien, "
            f"{human_bytes(prune_plan.prune_bytes)} werden frei"
        )
        if ctx.verbose:
            for item in prune_plan.prunes:
                print(f"  [entfernen] {item.path.name} — {item.reason}")

    for report in plan.reports:
        if report.kind == "orphaned":
            print(f"Hinweis: {report.path.name} wird von GOG nicht mehr angeboten (bleibt liegen)")
        elif ctx.verbose:
            print(f"Hinweis: {report.path.name} gehört nicht zum Manifest (bleibt unangetastet)")


# --------------------------------------------------------------------------- download


async def cmd_download(ctx: AppContext) -> int:
    store = SqliteStore(db_path(ctx.dest))
    reporter = make_reporter(quiet=ctx.quiet)
    downloaded = 0
    failed = 0
    try:
        plan = _build_download_plan(store, ctx)

        if ctx.dry_run:
            prune_plan = _plan_prune(store, ctx)
            _print_plan(plan, prune_plan, ctx)
            return EXIT_WORK_DONE if (plan.downloads or prune_plan.prunes) else EXIT_NOTHING_TO_DO

        if not plan.downloads:
            reporter.message("Alles aktuell — nichts zu laden.")
        else:
            reporter.start_overall(len(plan.downloads), plan.download_bytes)
            async with _http_client() as client:
                api = GogApiClient(_auth(), client=client)
                downloader = HttpDownloader(api, client=client, limit_rate=ctx.limit_rate)
                semaphore = asyncio.Semaphore(ctx.jobs)

                async def one(item: DownloadItem) -> None:
                    nonlocal downloaded, failed
                    async with semaphore:
                        result = await downloader.fetch(item, reporter)
                    entry = item.entry
                    if result.ok:
                        entry.state = LocalState.COMPLETE
                        entry.bytes_done = result.bytes_written
                        entry.relative_path = str(item.target.relative_to(ctx.dest))
                        # Nur eine tatsächlich geprüfte Datei darf später eine
                        # Löschung rechtfertigen (KONZEPT.md §5.5).
                        entry.last_verified_utc = now_utc() if result.verified else None
                        downloaded += 1
                    else:
                        entry.state = LocalState.PARTIAL
                        failed += 1
                        reporter.message(f"Fehlgeschlagen: {item.target.name} — {result.error}")
                    store.update_entry(entry)

                await asyncio.gather(*(one(item) for item in plan.downloads))

        # Prune wird bewusst ERST NACH den Downloads geplant: erst jetzt steht
        # fest, welche Ersatzdateien vollständig und verifiziert vorliegen.
        removed_bytes = _run_prune(store, ctx, reporter)
    finally:
        reporter.close()
        store.close()

    if failed:
        print(f"{failed} Datei(en) fehlgeschlagen — erneut ausführen setzt dort fort.")
        return 4
    if downloaded or removed_bytes:
        return EXIT_WORK_DONE
    return EXIT_NOTHING_TO_DO


def _run_prune(store: SqliteStore, ctx: AppContext, reporter) -> int:
    if not ctx.config.prune:
        return 0
    prune_plan = _plan_prune(store, ctx)
    if not prune_plan.prunes:
        return 0
    executor = PruneExecutor(ctx.dest, store, mode=ctx.config.prune_mode)
    results = executor.execute(prune_plan.prunes, dry_run=ctx.dry_run)
    for result in results:
        if result.removed:
            reporter.message(
                f"entfernt: {result.item.path.name} ({human_bytes(result.item.size)})"
                f" — {result.item.reason}"
            )
        elif result.reason:
            reporter.message(f"behalten: {result.item.path.name} — {result.reason}")
    freed = freed_bytes(results)
    if freed:
        reporter.message(f"Freigegeben: {human_bytes(freed)}")
    return freed


# --------------------------------------------------------------------------- verify / clean


def cmd_verify(ctx: AppContext, *, deep: bool) -> int:
    from gogdl.download.verify import verify_entry

    store = SqliteStore(db_path(ctx.dest))
    bad = 0
    try:
        entries = [e for e in store.entries() if e.relative_path]
        for entry in entries:
            path = ctx.dest / entry.relative_path
            ok, detail = verify_entry(entry, path, deep=deep)
            if ok:
                entry.last_verified_utc = now_utc()
                store.update_entry(entry)
            else:
                bad += 1
                entry.last_verified_utc = None
                store.update_entry(entry)
                print(f"FEHLER {path.name}: {detail}")
        print(f"{len(entries) - bad}/{len(entries)} Dateien in Ordnung.")
    finally:
        store.close()
    return 0 if bad == 0 else 4


def cmd_clean(ctx: AppContext, *, apply: bool) -> int:
    store = SqliteStore(db_path(ctx.dest))
    reporter = make_reporter(quiet=ctx.quiet)
    try:
        ctx = replace(ctx, dry_run=not apply)
        freed = _run_prune(store, ctx, reporter)
    finally:
        reporter.close()
        store.close()
    if not apply:
        print("Nichts verändert. Mit --apply wird tatsächlich gelöscht.")
    return EXIT_WORK_DONE if freed else EXIT_NOTHING_TO_DO


# --------------------------------------------------------------------------- sync


async def cmd_sync(ctx: AppContext, *, only: list[str], skip: list[str]) -> int:
    """``update`` und ``download`` in einem Aufruf — der Cron-Fall."""
    rc = await cmd_update(ctx, only=only, skip=skip)
    if rc not in (EXIT_NOTHING_TO_DO, EXIT_WORK_DONE):
        return rc
    return await cmd_download(ctx)


__all__ = [
    "cmd_login",
    "cmd_update",
    "cmd_status",
    "cmd_download",
    "cmd_verify",
    "cmd_clean",
    "cmd_sync",
    "AuthError",
    "GogdlError",
    "Path",
]
