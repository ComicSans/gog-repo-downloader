"""Die Kommandos. Hier laufen die Fachmodule zusammen - sonst nirgends."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import httpx

from gogdl.api.client import GogApiClient
from gogdl.auth.flow import FileCredentialStore, GogAuth, extract_code, start_login
from gogdl.constants import DEFAULT_TIMEOUT, USER_AGENT
from gogdl.download.engine import HttpDownloader
from gogdl.errors import ApiError, AuthError, GogdlError
from gogdl.model.types import (
    DownloadItem,
    LocalState,
    ManifestEntry,
    ProductRef,
    RemoteFile,
    Report,
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


def _auth(client: httpx.AsyncClient | None = None) -> GogAuth:
    """Zugang mit dem gemeinsamen HTTP-Client.

    Ohne durchgereichten Client legt ``GogAuth`` beim ersten Refresh einen
    eigenen an - und den schließt niemand mehr.
    """
    return GogAuth(FileCredentialStore(), client=client)


# --------------------------------------------------------------------------- login


async def cmd_login(ctx: AppContext, *, no_browser: bool = False) -> int:
    url = start_login(open_browser=not no_browser)
    print("Sign in to GOG in your browser:")
    print(f"\n  {url}\n")
    print(
        "After signing in you land on a page that probably looks empty.\n"
        "Copy the full address bar (it contains ?code=...) and paste it here."
    )
    try:
        pasted = input("\nURL or code: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1

    code = extract_code(pasted)
    async with _http_client() as client:
        auth = GogAuth(FileCredentialStore(), client=client)
        await auth.exchange_code(code)
        api = GogApiClient(auth, client=client)
        user = await api.user_data()

    print(f"Signed in as {user.username}.")
    return 0


# --------------------------------------------------------------------------- update


async def cmd_update(ctx: AppContext, *, only: list[str], skip: list[str]) -> int:
    """Metadaten holen. ``--only``/``--skip`` wählen die Arbeit, nicht die Welt.

    Die Bibliotheksliste wird **immer vollständig** gespeichert: aus ihr
    kommt der Verzeichnisname jedes Produkts. Würde eine Auswahl sie
    beschneiden, landeten die Dateien der übrigen Produkte künftig unter
    ihrer numerischen ID - und der alte Bestand wäre für das Werkzeug
    Fremdbestand. Die Auswahl entscheidet nur, für welche Produkte Dateien
    abgerufen werden.

    Ein Produkt, das GOG gerade nicht ausliefert, beendet den Lauf nicht:
    bei hunderten Titeln wäre ein einzelner HTTP 500 sonst sehr teuer. Die
    Fehler werden gesammelt, am Ende genannt und über den Rückgabewert
    kenntlich gemacht; ein erneuter Lauf holt die fehlenden nach.
    """
    store = SqliteStore(db_path(ctx.dest))
    reporter = make_reporter(quiet=ctx.quiet)
    failures: list[tuple[ProductRef, Exception]] = []
    products: list[ProductRef] = []
    try:
        async with _http_client() as client:
            api = GogApiClient(_auth(client), client=client)
            library = await api.library()
            store.replace_products(library)

            if not library:
                print("The library is empty - GOG lists no products at all.")
                return 0

            products = _filter_products(library, only, skip)
            if not products:
                print(_keine_auswahl(library, only, skip))
                return 1

            reporter.message(
                f"{len(products)} of {len(library)} products in the library."
            )
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
                    enriched = await _enrich(
                        api, files, known, strict_md5=ctx.config.strict_md5
                    )
                    store.replace_remote(product.product_id, enriched, seen)
                    if not ctx.quiet:
                        reporter.message(f"  {product.title}: {len(enriched)} files")

            results = await asyncio.gather(
                *(one(p) for p in products), return_exceptions=True
            )
            for product, result in zip(products, results):
                if isinstance(result, Exception):
                    failures.append((product, result))
                elif isinstance(result, BaseException):
                    # Abbruch von außen (KeyboardInterrupt, Cancel) ist kein
                    # Produktfehler und darf nicht verschluckt werden.
                    raise result
    finally:
        reporter.close()
        store.close()

    if not failures:
        return 0
    return _melde_fehlgeschlagene_produkte(products, failures)


def _melde_fehlgeschlagene_produkte(
    products: Sequence[ProductRef], failures: Sequence[tuple[ProductRef, Exception]]
) -> int:
    """Zusammenfassung und Rückgabewert eines unvollständigen Laufs."""
    print(f"{len(products) - len(failures)} of {len(products)} products updated.")
    print(f"Not fetched ({len(failures)}) - another run picks them up:")
    for product, exc in failures[:20]:
        print(f"  {product.title} ({product.slug}): {exc}")
    if len(failures) > 20:
        print(f"  ... and {len(failures) - 20} more")

    # Ein abgelehnter Zugang ist etwas anderes als ein wackelnder Endpunkt:
    # das eine braucht einen Menschen, das andere nur einen zweiten Lauf.
    if any(isinstance(exc, AuthError) for _, exc in failures):
        return AuthError.exit_code
    return ApiError.exit_code


async def _enrich(
    api: GogApiClient,
    files: list[RemoteFile],
    known: dict[tuple, ManifestEntry],
    *,
    strict_md5: bool = False,
) -> list[RemoteFile]:
    """Dateiname und echte Größe ergänzen - nur wo nötig.

    Keine der beiden Dateilisten nennt den Dateinamen, und keine nennt eine
    brauchbare Größe: gameDetails trägt gerundeten Text ("1 MB"),
    api.gog.com eine auf volle MiB gerundete Zahl. Die api-Schicht gibt
    deshalb in beiden Fällen ``size=None`` aus (siehe die Messung in
    ``api/client.py::_collect_api_group``). Die echte Größe kommt hier aus
    zwei Quellen, in dieser Reihenfolge:

    * ``checksum.total_size`` aus dem Checksum-XML - kostenlos, denn das XML
      wird für ``md5`` ohnehin geholt,
    * sonst ``api.content_length`` per Kopfanfrage auf die signierte URL.

    Ohne diese Größe bliebe ``version`` das einzige Signal, das den Store
    erreicht - und ein stiller Neu-Upload unter gleicher Version wäre
    unsichtbar. Genau dafür ist dieses Werkzeug da.

    Jede Auflösung kostet zwei Requests pro Datei. Bei einer Bibliothek mit
    hunderten Titeln ist das der Kostenfaktor des Laufs, und GOG drosselt.
    Geholt wird deshalb nur, wo der gespeicherte Stand nicht mehr trägt:

    * die ``version`` ist eine andere,
    * es gibt noch gar keine Größe (oder keinen Dateinamen),
    * ``--strict``: dann soll ein Re-Upload **ohne** Versionssprung
      auffallen, und den findet nur ein frisch geholter Wert. ``--strict``
      ist damit teuer - jede Datei wird in jedem Lauf aufgelöst.

    Der Kurzschluss übernimmt ``size`` und ``md5`` ausdrücklich aus dem
    Manifest. Ohne das schriebe der nächste ``replace_remote`` eine leere
    Größe über eine bekannte, und der Store hielte jede Datei für veraltet.
    Er vergleicht bewusst keine Größe: ``remote.size`` ist immer ``None``,
    und ``previous.size`` mit sich selbst zu vergleichen fände nie etwas.
    """
    result: list[RemoteFile] = []
    for remote in files:
        previous = known.get((remote.slot, remote.file_id))
        # ``remote.size`` ist hier immer None und taugt deshalb nicht als
        # Vergleichswert - die Dateilisten von GOG kennen keine exakte Größe.
        unchanged = (
            previous is not None
            and previous.filename
            and previous.size is not None
            and previous.version == remote.version
            and not strict_md5
        )
        if unchanged:
            result.append(
                replace(
                    remote,
                    filename=previous.filename,
                    md5=previous.md5,
                    size=previous.size,
                )
            )
            continue

        link = await api.resolve_downlink(remote.downlink)
        checksum = await api.checksum(link.checksum_url) if link.checksum_url else None
        size = checksum.total_size if checksum else None
        if size is None:
            # Erst wenn das XML nichts hergibt: eine eigene Kopfanfrage. Das
            # XML wird ohnehin geholt, die Kopfanfrage ist der Zusatzrequest.
            size = await api.content_length(link.url)
        if size is None and previous is not None:
            # Nicht ermittelbar heißt „nichts Neues erfahren", nicht „hat
            # keine Größe": den bekannten Wert stehen zu lassen ist besser,
            # als jede Datei mit einem verfallenen Signal veralten zu lassen.
            size = previous.size
        result.append(
            replace(
                remote,
                filename=link.filename,
                md5=checksum.md5 if checksum else None,
                size=size,
            )
        )
    return result


def _needles(values: Sequence[str] | None) -> list[str]:
    """``--only a,b`` und ``--only a --only b`` bedeuten dasselbe.

    Die Kommaform ist die naheliegende Schreibweise (``--lang`` verlangt
    sie), und sie darf nicht stillschweigend ins Leere greifen.
    """
    result: list[str] = []
    for value in values or ():
        for part in str(value).split(","):
            teil = part.strip().lower()
            if teil and teil not in result:
                result.append(teil)
    return result


def _matches(product_id: int, slug: str | None, needles: Sequence[str]) -> bool:
    """Trifft eine Auswahl dieses Produkt? Slug oder ID, beides gilt."""
    keys = {str(product_id)}
    if slug:
        keys.add(slug.lower())
    return any(needle in keys for needle in needles)


def _filter_products(
    products: Sequence[ProductRef], only: Sequence[str], skip: Sequence[str]
) -> list[ProductRef]:
    gewaehlt = _needles(only)
    verworfen = _needles(skip)
    result = list(products)
    if gewaehlt:
        result = [p for p in result if _matches(p.product_id, p.slug, gewaehlt)]
    if verworfen:
        result = [p for p in result if not _matches(p.product_id, p.slug, verworfen)]
    return result


def _keine_auswahl(
    library: Sequence[ProductRef], only: Sequence[str], skip: Sequence[str]
) -> str:
    genannt = ", ".join(_needles(only) + _needles(skip))
    beispiele = ", ".join(sorted(p.slug for p in library)[:5])
    return (
        f"The selection ({genannt}) matches none of the {len(library)} products "
        f"in the library. Expected are a slug or a product id, several of them "
        f"comma-separated. Available are for example: {beispiele}"
    )


# --------------------------------------------------------------------------- status


def _slugs(store: SqliteStore) -> dict[int, str]:
    """Produkt-ID -> Verzeichnisname. Bestimmt das flache Ablagelayout."""
    return {p.product_id: p.slug for p in store.products()}


_DISK_CACHE: dict[tuple[Path, bool], dict[Path, int]] = {}


def _scan(ctx: AppContext, *, fresh: bool = False) -> dict[Path, int]:
    """Plattenzustand so, wie der Nutzer ihn bestellt hat.

    Nur an einer Stelle, damit alle Kommandos dieselbe Sicht haben: sähe
    das Aufräumen mehr als die Planung, würde eine Datei geplant und
    gleich wieder gelöscht.

    **Und nur einmal pro Lauf.** Auf einer externen Platte mit 3,5 TB
    dauert ein vollständiger Durchlauf über 80 Sekunden. Ihn nach jeder
    fertigen Datei zu wiederholen, wie es das Aufräumen je Auslieferung
    zunächst tat, kostet bei tausend Dateien mehr Zeit als die Downloads
    selbst. Der Zustand wird deshalb einmal erhoben und danach von
    :func:`_note_written` und :func:`_note_removed` fortgeschrieben.
    """
    key = (ctx.dest, ctx.config.include_saves)
    if fresh or key not in _DISK_CACHE:
        _DISK_CACHE[key] = scan_disk(ctx.dest, include_saves=ctx.config.include_saves)
    return _DISK_CACHE[key]


def _note_written(ctx: AppContext, path: Path, size: int) -> None:
    """Eine gerade geschriebene Datei in den bekannten Zustand aufnehmen."""
    cached = _DISK_CACHE.get((ctx.dest, ctx.config.include_saves))
    if cached is not None:
        cached[path] = size


def _note_removed(ctx: AppContext, path: Path) -> None:
    """Eine gerade entfernte Datei aus dem bekannten Zustand nehmen."""
    cached = _DISK_CACHE.get((ctx.dest, ctx.config.include_saves))
    if cached is not None:
        cached.pop(path, None)


def _plan_prune(
    store: SqliteStore,
    ctx: AppContext,
    *,
    only: Sequence[str] = (),
    skip: Sequence[str] = (),
) -> SyncPlan:
    """Löschplan - und zwar nur für die ausgewählten Produkte.

    Die Auswahl muss gerade hier greifen: Löschen ist die unumkehrbare
    Hälfte der Arbeit. Wer ein Spiel per ``--skip`` heraushält, will nicht,
    dass dessen Altbestand trotzdem verschwindet.
    """
    slugs = _slugs(store)
    return plan_prune(
        _select_entries(store.entries(), slugs, only, skip),
        ctx.config,
        on_disk=_scan(ctx),
        slugs=slugs,
    )


def cmd_status(
    ctx: AppContext, *, only: Sequence[str] = (), skip: Sequence[str] = ()
) -> int:
    store = SqliteStore(db_path(ctx.dest))
    try:
        plan = _build_download_plan(store, ctx, only=only, skip=skip)
        prune_plan = _plan_prune(store, ctx, only=only, skip=skip)
    finally:
        store.close()

    _print_plan(plan, prune_plan, ctx)
    return EXIT_WORK_DONE if (plan.downloads or prune_plan.prunes) else EXIT_NOTHING_TO_DO


def _build_download_plan(
    store: SqliteStore,
    ctx: AppContext,
    *,
    only: Sequence[str] = (),
    skip: Sequence[str] = (),
) -> SyncPlan:
    """Arbeitsliste bauen - einschließlich der veralteten Einträge.

    ``plan_downloads`` vergleicht Remote-Stand gegen Manifest. Beide Seiten
    kommen hier aber aus derselben Tabelle: sobald ``update`` geschrieben
    hat, gibt es keinen zweiten, älteren Stand mehr, gegen den sich
    vergleichen ließe. Die Aktualitätsentscheidung ist deshalb schon im
    Store gefallen und steht als ``LocalState.STALE`` im Eintrag; hier wird
    sie nur noch durchgesetzt.

    Der Remote-Stand geht bewusst **vollständig** hinein, STALE
    eingeschlossen: Plattform- und Sprachwahl fallen in ``plan_downloads``
    über den angebotenen Bestand, und ein herausgenommener Eintrag würde
    dort eine Rückfallebene öffnen, die es gar nicht gibt - ``--lang de,en``
    lüde plötzlich Englisch, weil die deutsche Fassung „fehlt". Nachgezogen
    wird deshalb erst am fertigen Plan (:func:`_erzwinge_stale`).
    """
    slugs = _slugs(store)
    entries = _select_entries(store.entries(), slugs, only, skip)
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
    plan = plan_downloads(
        remote, entries, ctx.config, on_disk=_scan(ctx), slugs=slugs
    )
    _erzwinge_stale(plan, entries, ctx.dest, slugs)
    return plan


def _select_entries(
    entries: Sequence[ManifestEntry],
    slugs: dict[int, str],
    only: Sequence[str],
    skip: Sequence[str],
) -> list[ManifestEntry]:
    """``--only``/``--skip`` auf den Bestand anwenden.

    Ohne das schränkt die Auswahl nur den Metadaten-Abruf ein: ``--skip
    spiel`` würde das Spiel trotzdem laden, weil die Planung über alle
    Einträge geht.
    """
    gewaehlt = _needles(only)
    verworfen = _needles(skip)
    if not gewaehlt and not verworfen:
        return list(entries)

    result: list[ManifestEntry] = []
    for entry in entries:
        slug = slugs.get(entry.product_id)
        if gewaehlt and not _matches(entry.product_id, slug, gewaehlt):
            continue
        if verworfen and _matches(entry.product_id, slug, verworfen):
            continue
        result.append(entry)
    return result


def _erzwinge_stale(
    plan: SyncPlan,
    entries: Sequence[ManifestEntry],
    dest: Path,
    slugs: dict[int, str],
) -> None:
    """Veraltete Einträge in die Arbeitsliste zwingen - und immer von vorn.

    Zwei Fälle, die der Planer unterschiedlich behandelt:

    * Er hat den Eintrag bereits geplant (die Größe hat sich geändert) -
      dann steht dort aber ein ``resume_from`` aus einer ``.part``, die zur
      **alten** Auslieferung gehört. Weiterschreiben wäre stille Korruption
      (KONZEPT.md §5.1), also von vorn.
    * Er hat ihn übersprungen, weil die Datei in erwarteter Größe daliegt -
      der Normalfall eines stillen Neu-Uploads. Dann kommt er hier dazu.

    Der zweite Fall geht an den Plattform- und Sprachfiltern vorbei; das
    ist gewollt. Ein veralteter Eintrag beschreibt eine Datei, die bereits
    auf der Platte liegt - sie ist einmal durch die Filter gekommen. Sie
    dort in einer überholten Fassung liegen zu lassen, wäre schlechter als
    sie zu erneuern, zumal ``prune`` sie nie anfassen würde.
    """
    offen = {
        (entry.slot, entry.file_id): entry
        for entry in entries
        if entry.state is LocalState.STALE
    }
    if not offen:
        return

    for index, item in enumerate(plan.downloads):
        entry = offen.pop((item.entry.slot, item.entry.file_id), None)
        if entry is None:
            continue
        item.entry.state = LocalState.STALE
        item.entry.bytes_done = 0
        plan.downloads[index] = replace(item, resume_from=0)

    for entry in offen.values():
        target = _target_of(entry, dest, slugs)
        plan.downloads.append(
            DownloadItem(
                entry=replace(
                    entry,
                    state=LocalState.STALE,
                    bytes_done=0,
                    last_verified_utc=None,
                    relative_path=str(_relative_to(target, dest)),
                ),
                target=target,
                resume_from=0,
            )
        )

    plan.downloads.sort(key=lambda item: (item.entry.slot.as_str(), item.entry.file_id))


def _target_of(entry: ManifestEntry, dest: Path, slugs: dict[int, str]) -> Path:
    """Zielpfad eines Eintrags - dieselbe Regel wie in ``sync/planner``.

    Das Verzeichnis stammt aus dem Eintrag, sofern er einen Pfad kennt (ein
    importierter Bestand bleibt an seinem Ort), der Dateiname aus dem
    Manifest.
    """
    name = entry.filename or entry.file_id
    if entry.relative_path:
        return (dest / Path(entry.relative_path)).parent / name
    return dest / (slugs.get(entry.product_id) or str(entry.product_id)) / name


def _relative_to(target: Path, dest: Path) -> Path:
    return target.relative_to(dest) if target.is_relative_to(dest) else Path(target.name)


def _print_plan(plan: SyncPlan, prune_plan: SyncPlan, ctx: AppContext) -> None:
    if plan.downloads:
        print(
            f"To download: {len(plan.downloads)} files, "
            f"{human_bytes(plan.download_bytes)}"
        )
        if ctx.verbose:
            for item in plan.downloads:
                marker = "resume" if item.resume_from else "new"
                print(f"  [{marker}] {item.target.name}")
    else:
        print("To download: nothing.")

    if prune_plan.prunes:
        print(
            f"To clean up: {len(prune_plan.prunes)} old files, "
            f"{human_bytes(prune_plan.prune_bytes)} will be freed"
        )
        if ctx.verbose:
            for item in prune_plan.prunes:
                print(f"  [remove] {item.path.name} - {item.reason}")

    _print_reports(plan.reports, ctx)


def _print_reports(reports: Sequence[Report], ctx: AppContext) -> None:
    """Hinweise zusammenfassen statt sie einzeln aufzuzählen.

    Bei einer gewachsenen Sammlung nimmt GOG laufend Fassungen aus dem
    Angebot, vor allem Patches. Jede davon einzeln zu melden erzeugt
    hunderte Zeilen und begräbt die wenigen Meldungen, die wirklich eine
    Entscheidung verlangen - allen voran die Namenskollisionen.
    """
    kollisionen = [r for r in reports if r.kind == "collision"]
    verwaist = [r for r in reports if r.kind == "orphaned"]
    fremd = [r for r in reports if r.kind not in ("collision", "orphaned")]

    # Kollisionen zuerst und immer vollständig: hier bleibt eine Datei
    # ungeladen, das muss jemand sehen.
    for report in kollisionen:
        print(f"Renamed: {report.path.name} - {report.detail}".rstrip())

    if verwaist:
        patches = sum(1 for r in verwaist if r.path.name.startswith("patch_"))
        rest = len(verwaist) - patches
        teile = []
        if patches:
            teile.append(f"{patches} patch files")
        if rest:
            teile.append(f"{rest} other files")
        print(
            f"No longer offered by GOG: {' and '.join(teile)}. These cannot be "
            "downloaded again; only obsolete patches covered by a verified "
            "installer are cleaned up, everything else stays on disk."
        )
        if ctx.verbose:
            for report in verwaist[:20]:
                print(f"  {report.path.name}")
            if len(verwaist) > 20:
                print(f"  ... and {len(verwaist) - 20} more")

    if fremd and ctx.verbose:
        print(f"Not part of the manifest: {len(fremd)} files - left untouched")


# --------------------------------------------------------------------------- download


async def cmd_download(
    ctx: AppContext, *, only: Sequence[str] = (), skip: Sequence[str] = ()
) -> int:
    store = SqliteStore(db_path(ctx.dest))
    reporter = make_reporter(quiet=ctx.quiet)
    downloaded = 0
    failed = 0
    zugang_verloren = False
    try:
        if not ctx.quiet:
            # Der erste Scan dauert auf einer grossen, externen Sammlung
            # ueber eine Minute. Ohne Ansage sieht das aus wie ein Haenger.
            print(f"Reading {ctx.dest} ...", flush=True)
        plan = _build_download_plan(store, ctx, only=only, skip=skip)

        if ctx.dry_run:
            prune_plan = _plan_prune(store, ctx, only=only, skip=skip)
            _print_plan(plan, prune_plan, ctx)
            return EXIT_WORK_DONE if (plan.downloads or prune_plan.prunes) else EXIT_NOTHING_TO_DO

        if not plan.downloads:
            reporter.message("Everything up to date - nothing to download.")
        else:
            reporter.start_overall(len(plan.downloads), plan.download_bytes)
            async with _http_client() as client:
                auth = _auth(client)
                # Einmal vorab statt tausendmal einzeln: ohne Login scheitert
                # jede Datei mit derselben Meldung, und der Abbruchcode wäre
                # falsch. Ein fehlender Login ist kein Download-Fehler, den
                # ein zweiter Lauf heilt - hier muss ein Mensch ran.
                if not auth.is_authenticated():
                    raise AuthError(
                        "Not signed in. Run `gogdl login` first."
                    )
                api = GogApiClient(auth, client=client)
                downloader = HttpDownloader(api, client=client, limit_rate=ctx.limit_rate)
                semaphore = asyncio.Semaphore(ctx.jobs)

                async def one(item: DownloadItem) -> None:
                    nonlocal downloaded, failed, zugang_verloren
                    if zugang_verloren:
                        # Nichts mehr anfangen, was ohnehin nur scheitert.
                        return
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
                        if auth.is_authenticated():
                            reporter.message(
                                f"Failed: {item.target.name} - {result.error}"
                            )
                        else:
                            # GOG hat den Refresh-Token abgelehnt, die
                            # Zugangsdaten sind verworfen. Alles Weitere
                            # scheiterte identisch.
                            zugang_verloren = True
                    store.update_entry(entry)
                    if result.ok:
                        # Der bekannte Plattenzustand wird fortgeschrieben,
                        # nicht neu erhoben: ein Vollscan kostet auf einer
                        # externen Platte über 80 Sekunden.
                        _note_written(ctx, item.target, result.bytes_written)
                        _prune_slot_if_complete(store, ctx, entry.slot, reporter)

                await asyncio.gather(*(one(item) for item in plan.downloads))

            if zugang_verloren:
                raise AuthError(
                    "Access expired during the run and could not be renewed. "
                    "Run `gogdl login` again; files already downloaded are kept."
                )

        # Was fertig auf der Platte liegt, aber im Manifest noch als unfertig
        # steht, wird hier nachgezogen - sonst bleibt der Slot dauerhaft vom
        # Aufräumen ausgeschlossen.
        _reconcile_states(store, ctx)

        # Prune wird bewusst ERST NACH den Downloads geplant: erst jetzt steht
        # fest, welche Ersatzdateien vollständig und verifiziert vorliegen.
        removed_bytes = _run_prune(store, ctx, reporter, only=only, skip=skip)
    finally:
        reporter.close()
        store.close()

    if failed:
        print(f"{failed} file(s) failed - running the command again resumes there.")
        return 4
    if downloaded or removed_bytes:
        return EXIT_WORK_DONE
    return EXIT_NOTHING_TO_DO


def _print_unmatched(paths: Sequence[Path], dest: Path, limit: int = 25) -> None:
    """Nicht zuordenbare Dateien nach Verzeichnis zusammenfassen.

    Eine gewachsene Sammlung enthält tausende Dateien, die dem Werkzeug
    nichts sagen: Spielstände, Systemdateien, Beiwerk von Spielen. Sie
    einzeln aufzuzählen macht die Ausgabe unlesbar und verdeckt genau die
    Fälle, die jemand ansehen sollte. Gruppiert nach Verzeichnis wird aus
    tausend Zeilen eine.
    """
    nach_ordner: dict[Path, list[Path]] = {}
    for path in paths:
        try:
            ordner = path.relative_to(dest).parent
        except ValueError:
            ordner = path.parent
        nach_ordner.setdefault(ordner, []).append(path)

    sortiert = sorted(nach_ordner.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
    for ordner, dateien in sortiert[:limit]:
        ort = str(ordner) if str(ordner) != "." else "(top level)"
        if len(dateien) == 1:
            print(f"  {ort}/{dateien[0].name}")
        else:
            print(f"  {ort}/ - {len(dateien)} files")
    if len(sortiert) > limit:
        rest = sum(len(d) for _, d in sortiert[limit:])
        print(f"  ... and {rest} files in {len(sortiert) - limit} more directories")


def _prune_slot_if_complete(
    store: SqliteStore, ctx: AppContext, slot, reporter
) -> None:
    """Eine Auslieferung sofort aufräumen, sobald sie vollständig vorliegt.

    Der Downloader legt eine überschriebene Vorgängerfassung als ``.old``
    beiseite, statt sie zu ersetzen - sonst bliebe bei einem abgebrochenen
    mehrteiligen Installer keine vollständige Fassung übrig. Ohne diesen
    Aufruf lägen die Altdateien bis zum Ende des gesamten Laufs herum, was
    bei einer grossen Sammlung viel Platz bindet.

    Aufgeräumt wird erst, wenn ALLE Teile des Slots geprüft vollständig
    sind. Die Sicherheitsprüfung ist dieselbe wie am Laufende, nur früher.
    """
    if not ctx.config.prune:
        return
    teile = store.entries_for_slot(slot)
    if not teile or not all(teil.is_verified_complete for teil in teile):
        return

    plan = _plan_prune(store, ctx)
    fuer_slot = [item for item in plan.prunes if item.slot == slot]
    if not fuer_slot:
        return

    executor = PruneExecutor(ctx.dest, store, mode=ctx.config.prune_mode)
    for result in executor.execute(fuer_slot, dry_run=ctx.dry_run):
        if result.removed:
            _note_removed(ctx, result.item.path)
            reporter.message(
                f"removed: {result.item.path.name} ({human_bytes(result.item.size)})"
            )


def _reconcile_states(store: SqliteStore, ctx: AppContext) -> None:
    """Zieldatei da und in Sollgröße? Dann ist der Eintrag fertig.

    Bricht ein Lauf zwischen dem Umbenennen der fertigen Datei und dem
    Schreiben des Eintrags ab, liegt die Datei vollständig da, der Eintrag
    bleibt aber ``PARTIAL``. Der nächste Lauf überspringt sie (die Größe
    stimmt ja) und setzt sie nie auf ``COMPLETE``: der Slot ist dauerhaft
    vom Aufräumen ausgeschlossen und heilt nur über ``gogdl verify``.

    ``last_verified_utc`` bleibt dabei bewusst leer - nachgezogen ist nicht
    geprüft, und nur Geprüftes darf eine Löschung rechtfertigen
    (KONZEPT.md §5.5). ``STALE`` wird nicht angefasst: eine veraltete Datei
    hat oft genau die erwartete Größe und ist trotzdem die falsche.
    """
    disk = _scan(ctx)
    slugs = _slugs(store)
    for entry in store.entries():
        if entry.state not in (LocalState.MISSING, LocalState.PARTIAL):
            continue
        if entry.size is None or not entry.filename:
            # Ohne Dateinamen gibt es keinen Zielpfad, sondern nur die
            # File-ID als Notbehelf - darauf darf keine Datei zugeordnet
            # und schon gar kein Zustand nachgezogen werden.
            continue
        target = _target_of(entry, ctx.dest, slugs)
        if disk.get(target) != entry.size:
            continue
        entry.state = LocalState.COMPLETE
        entry.bytes_done = entry.size
        entry.relative_path = str(_relative_to(target, ctx.dest))
        store.update_entry(entry)


def _run_prune(
    store: SqliteStore,
    ctx: AppContext,
    reporter,
    *,
    only: Sequence[str] = (),
    skip: Sequence[str] = (),
) -> int:
    if not ctx.config.prune:
        return 0
    prune_plan = _plan_prune(store, ctx, only=only, skip=skip)
    if not prune_plan.prunes:
        return 0
    executor = PruneExecutor(ctx.dest, store, mode=ctx.config.prune_mode)
    results = executor.execute(prune_plan.prunes, dry_run=ctx.dry_run)
    for result in results:
        if result.removed:
            reporter.message(
                f"removed: {result.item.path.name} ({human_bytes(result.item.size)})"
                f" - {result.item.reason}"
            )
        elif result.reason:
            reporter.message(f"kept: {result.item.path.name} - {result.reason}")
    freed = freed_bytes(results)
    if freed:
        reporter.message(f"Freed: {human_bytes(freed)}")
    return freed


# --------------------------------------------------------------------------- verify / clean


def cmd_verify(ctx: AppContext, *, deep: bool) -> int:
    """Bestand prüfen. Der Stempel bedeutet mehr, als ein flacher Lauf weiß.

    ``last_verified_utc`` heißt nach KONZEPT.md §5.5: „Größe stimmt; md5
    stimmt, sofern vorhanden" - und nur damit autorisiert er Löschungen.
    Ohne ``--deep`` wird md5 gar nicht gelesen. Ein flacher Lauf darf einen
    Stempel deshalb weder setzen noch erneuern, solange das Manifest ein
    md5 führt; sonst höbe er genau den Stempel wieder an, den ``--deep``
    oder ein Versionswechsel gerade entwertet hat.

    Ein Fehlschlag entwertet dagegen immer: dass die Größe nicht stimmt,
    reicht als Gegenbeweis auch ohne md5.
    """
    from gogdl.download.verify import verify_entry

    store = SqliteStore(db_path(ctx.dest))
    bad = 0
    unbestaetigt = 0
    try:
        entries = [e for e in store.entries() if e.relative_path]
        for entry in entries:
            path = ctx.dest / entry.relative_path
            ok, detail = verify_entry(entry, path, deep=deep)
            if not ok:
                bad += 1
                entry.last_verified_utc = None
                store.update_entry(entry)
                print(f"FAILED {path.name}: {detail}")
                continue
            if entry.md5 and not deep:
                unbestaetigt += 1
                continue
            entry.last_verified_utc = now_utc()
            store.update_entry(entry)
        print(f"{len(entries) - bad}/{len(entries)} files are fine.")
        if unbestaetigt:
            print(
                f"{unbestaetigt} file(s) carry a checksum in the manifest that only "
                "--deep checks. Their verification stamp is left unchanged."
            )
    finally:
        store.close()
    return 0 if bad == 0 else 4


def cmd_import(ctx: AppContext, *, trust: str, apply: bool) -> int:
    """Vorhandenen Bestand dem Manifest zuordnen.

    Ohne diesen Schritt hält das Tool eine mit einem anderen Werkzeug
    geladene Sammlung komplett für Fremdbestand: es lädt alles erneut und
    räumt nichts auf. Der Import ändert ausschließlich die Datenbank, nie
    eine Datei auf der Platte.
    """
    from gogdl.importer import Trust, apply_import, match_existing

    store = SqliteStore(db_path(ctx.dest))
    try:
        entries = store.entries()
        if not entries:
            print(
                "The manifest is empty. Run 'gogdl login' and 'gogdl update' "
                "first, otherwise there is nothing to match the existing files "
                "against."
            )
            return 1

        plan = match_existing(entries, _scan(ctx), ctx.dest, _slugs(store))

        print(f"Matched: {len(plan.matches)} files, {human_bytes(plan.match_bytes)}")
        if plan.unsure:
            print(f"Unsure, not adopted: {len(plan.unsure)}")
            if ctx.verbose:
                for candidate in plan.unsure:
                    print(f"  {candidate.path.name}: {candidate.reason}")
        if plan.unmatched:
            print(f"Not matchable: {len(plan.unmatched)} files - left untouched")
            if ctx.verbose:
                _print_unmatched(plan.unmatched, ctx.dest)

        if not apply:
            print("\nNothing changed. Use --apply to adopt these files into the manifest.")
            return EXIT_WORK_DONE if plan.matches else EXIT_NOTHING_TO_DO

        level = Trust(trust)
        if level is Trust.MD5:
            print("Computing checksums. This takes a long time on large collections.")
        summary = apply_import(plan, store, now_utc(), trust=level)

        print(
            f"Adopted: {len(summary.imported)} files, "
            f"{human_bytes(summary.imported_bytes)}"
        )
        if summary.rejected:
            print(f"Rejected: {len(summary.rejected)}")
        if level is Trust.NONE:
            print(
                "Note: without --trust size or md5 these files count as unverified. "
                "Pruning old versions stays inert for them."
            )
        else:
            print(f"Recorded as verified: {summary.verified_count}")
            if summary.unverifiable:
                print(
                    f"No checksum in the manifest, therefore unverified: "
                    f"{len(summary.unverifiable)}"
                )
    finally:
        store.close()
    return EXIT_WORK_DONE


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
        print("Nothing changed. Use --apply to actually delete.")
    return EXIT_WORK_DONE if freed else EXIT_NOTHING_TO_DO


# --------------------------------------------------------------------------- sync


async def cmd_sync(ctx: AppContext, *, only: list[str], skip: list[str]) -> int:
    """``update`` und ``download`` in einem Aufruf - der Cron-Fall.

    Ein abgelehnter Zugang beendet den Lauf sofort: ohne Login ist auch der
    Download sinnlos. Fehlen dagegen nur einzelne Produkte, wird trotzdem
    geladen - was bekannt ist, soll auf die Platte, und der nächste Lauf
    holt den Rest der Metadaten nach. Der schwerere der beiden Fehler
    bestimmt am Ende den Rückgabewert.
    """
    rc = await cmd_update(ctx, only=only, skip=skip)
    if rc == AuthError.exit_code:
        return rc

    download_rc = await cmd_download(ctx, only=only, skip=skip)
    if rc in (EXIT_NOTHING_TO_DO, EXIT_WORK_DONE):
        return download_rc
    if download_rc in (EXIT_NOTHING_TO_DO, EXIT_WORK_DONE):
        return rc
    return download_rc


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
