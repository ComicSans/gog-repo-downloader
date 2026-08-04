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
                print("Die Bibliothek ist leer - GOG nennt kein einziges Produkt.")
                return 0

            products = _filter_products(library, only, skip)
            if not products:
                print(_keine_auswahl(library, only, skip))
                return 1

            reporter.message(
                f"{len(products)} von {len(library)} Produkten in der Bibliothek."
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
                        reporter.message(f"  {product.title}: {len(enriched)} Dateien")

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
    print(f"{len(products) - len(failures)} von {len(products)} Produkten aktualisiert.")
    print(f"Nicht abgerufen ({len(failures)}) - ein erneuter Lauf holt sie nach:")
    for product, exc in failures[:20]:
        print(f"  {product.title} ({product.slug}): {exc}")
    if len(failures) > 20:
        print(f"  ... und {len(failures) - 20} weitere")

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

    Die Produkt-Payload nennt weder den Dateinamen noch eine brauchbare
    Größe: sie trägt gerundeten Text ("1 MB"), den die api-Schicht
    zulässigerweise nicht als Bytewert ausgibt. ``size`` kommt deshalb aus
    einer Kopfanfrage auf die signierte URL, ``md5`` gibt es auf diesem Weg
    gar nicht. Ohne diese Größe bliebe ``version`` das einzige Signal, das
    den Store erreicht - und ein stiller Neu-Upload unter gleicher Version
    wäre unsichtbar. Genau dafür ist dieses Werkzeug da.

    Jede Auflösung kostet zwei Requests pro Datei. Bei einer Bibliothek mit
    hunderten Titeln ist das der Kostenfaktor des Laufs, und GOG drosselt.
    Geholt wird deshalb nur, wo der gespeicherte Stand nicht mehr trägt:

    * die ``version`` ist eine andere,
    * es gibt noch gar keine Größe (oder keinen Dateinamen),
    * ``--strict``: dann soll ein Re-Upload **ohne** Versionssprung
      auffallen, und den findet nur eine frische Kopfanfrage. ``--strict``
      ist damit teuer - eine Kopfanfrage pro Datei und Lauf.

    Der Kurzschluss übernimmt ``size`` und ``md5`` ausdrücklich aus dem
    Manifest. Ohne das schriebe der nächste ``replace_remote`` eine leere
    Größe über eine bekannte, und der Store hielte jede Datei für veraltet.
    """
    result: list[RemoteFile] = []
    for remote in files:
        previous = known.get((remote.slot, remote.file_id))
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
        size = remote.size or (checksum.total_size if checksum else None)
        if size is None or strict_md5:
            size = await api.content_length(link.url) or size
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
        f"Die Auswahl ({genannt}) trifft kein einziges der "
        f"{len(library)} Produkte der Bibliothek. Erwartet werden Slug oder "
        f"Produkt-ID, mehrere kommagetrennt. Vorhanden sind z. B.: {beispiele}"
    )


# --------------------------------------------------------------------------- status


def _slugs(store: SqliteStore) -> dict[int, str]:
    """Produkt-ID -> Verzeichnisname. Bestimmt das flache Ablagelayout."""
    return {p.product_id: p.slug for p in store.products()}


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
        on_disk=scan_disk(ctx.dest),
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
        remote, entries, ctx.config, on_disk=scan_disk(ctx.dest), slugs=slugs
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
                print(f"  [entfernen] {item.path.name} - {item.reason}")

    for report in plan.reports:
        if report.kind == "orphaned":
            print(f"Hinweis: {report.path.name} wird von GOG nicht mehr angeboten (bleibt liegen)")
        elif report.kind == "collision":
            # Zwei Auslieferungen desselben Spiels tragen denselben Dateinamen.
            # Beide zu laden hiesse, sie in dieselbe Datei zu schreiben.
            print(
                f"Übersprungen: {report.path.name} - zwei Auslieferungen wollen "
                f"denselben Dateinamen. {report.detail}".rstrip()
            )
        elif ctx.verbose:
            print(f"Hinweis: {report.path.name} gehört nicht zum Manifest (bleibt unangetastet)")


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
        plan = _build_download_plan(store, ctx, only=only, skip=skip)

        if ctx.dry_run:
            prune_plan = _plan_prune(store, ctx, only=only, skip=skip)
            _print_plan(plan, prune_plan, ctx)
            return EXIT_WORK_DONE if (plan.downloads or prune_plan.prunes) else EXIT_NOTHING_TO_DO

        if not plan.downloads:
            reporter.message("Alles aktuell - nichts zu laden.")
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
                        "Keine gespeicherten Zugangsdaten. "
                        "Bitte zuerst `gogdl login` ausführen."
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
                                f"Fehlgeschlagen: {item.target.name} - {result.error}"
                            )
                        else:
                            # GOG hat den Refresh-Token abgelehnt, die
                            # Zugangsdaten sind verworfen. Alles Weitere
                            # scheiterte identisch.
                            zugang_verloren = True
                    store.update_entry(entry)

                await asyncio.gather(*(one(item) for item in plan.downloads))

            if zugang_verloren:
                raise AuthError(
                    "Der Zugang ist während des Laufs verfallen und ließ sich nicht "
                    "erneuern. Bitte `gogdl login` wiederholen; bereits geladene "
                    "Dateien bleiben erhalten."
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
        print(f"{failed} Datei(en) fehlgeschlagen - erneut ausführen setzt dort fort.")
        return 4
    if downloaded or removed_bytes:
        return EXIT_WORK_DONE
    return EXIT_NOTHING_TO_DO


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
    disk = scan_disk(ctx.dest)
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
                f"entfernt: {result.item.path.name} ({human_bytes(result.item.size)})"
                f" - {result.item.reason}"
            )
        elif result.reason:
            reporter.message(f"behalten: {result.item.path.name} - {result.reason}")
    freed = freed_bytes(results)
    if freed:
        reporter.message(f"Freigegeben: {human_bytes(freed)}")
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
                print(f"FEHLER {path.name}: {detail}")
                continue
            if entry.md5 and not deep:
                unbestaetigt += 1
                continue
            entry.last_verified_utc = now_utc()
            store.update_entry(entry)
        print(f"{len(entries) - bad}/{len(entries)} Dateien in Ordnung.")
        if unbestaetigt:
            print(
                f"{unbestaetigt} Datei(en) führen eine Prüfsumme im Manifest, die nur "
                "--deep prüft. Ihr Prüfstempel bleibt unverändert."
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
                "Das Manifest ist leer. Erst 'gogdl login' und 'gogdl update' "
                "ausführen, sonst gibt es nichts, dem der Bestand zugeordnet "
                "werden könnte."
            )
            return 1

        plan = match_existing(entries, scan_disk(ctx.dest), ctx.dest, _slugs(store))

        print(f"Eindeutig zugeordnet: {len(plan.matches)} Dateien, {human_bytes(plan.match_bytes)}")
        if plan.unsure:
            print(f"Unsicher (Name passt, Größe nicht): {len(plan.unsure)} - werden übergangen")
            if ctx.verbose:
                for candidate in plan.unsure:
                    print(f"  {candidate.path.name}: {candidate.reason}")
        if plan.unmatched:
            print(f"Nicht zuordenbar: {len(plan.unmatched)} Dateien - bleiben unangetastet")
            if ctx.verbose:
                for path in plan.unmatched[:50]:
                    print(f"  {path}")

        if not apply:
            print("\nNichts verändert. Mit --apply wird der Bestand ins Manifest übernommen.")
            return EXIT_WORK_DONE if plan.matches else EXIT_NOTHING_TO_DO

        level = Trust(trust)
        if level is Trust.MD5:
            print("Prüfe Prüfsummen. Das dauert bei großen Sammlungen lange.")
        summary = apply_import(plan, store, now_utc(), trust=level)

        print(
            f"Übernommen: {len(summary.imported)} Dateien, "
            f"{human_bytes(summary.imported_bytes)}"
        )
        if summary.rejected:
            print(f"Abgelehnt: {len(summary.rejected)}")
        if level is Trust.NONE:
            print(
                "Hinweis: Ohne --trust size oder md5 gilt der Bestand als unbestätigt. "
                "Das Aufräumen alter Versionen bleibt damit wirkungslos."
            )
        else:
            print(f"Als geprüft vermerkt: {summary.verified_count}")
            if summary.unverifiable:
                print(f"Ohne Prüfsumme im Manifest, daher unbestätigt: {len(summary.unverifiable)}")
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
        print("Nichts verändert. Mit --apply wird tatsächlich gelöscht.")
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
