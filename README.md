# gogdl

Keep a local, offline copy of **your own GOG.com library** - always up to date, resumable, and without the disk slowly filling up with obsolete installers.

GOG games are DRM-free, which makes archiving them straightforward. They are not copyright-free: `gogdl` only ever downloads games the signed-in account actually owns. That is why authentication is mandatory, why there is no anonymous mode, and why the tool will never grow a catalog scraper.

## Why another one

`gogrepo` and friends solved this years ago, and mostly still work. `gogdl` differs in three places that matter in daily use:

**Updates are detected by content, not by filename.** GOG bumps installer filenames *and* silently re-uploads under the same name. Comparing filenames looks correct in a quick test and quietly misses the second case forever. `gogdl` compares `version`, then `size`, then the MD5 from GOG's checksum sidecar - any mismatch means the local file is stale.

**Old versions are cleaned up automatically, and safely.** On by default, because otherwise every update cycle costs a full installer generation of disk space. Deletion only happens after the replacement is completely downloaded *and* verified, and it operates on whole releases - never on individual files, so a multi-part installer can never lose part 1 of the old version while part 2 of the new one is still downloading.

**Login uses your real browser.** No username/password automation for GOG's reCAPTCHA to break. You sign in at GOG the normal way, paste one code back, and `gogdl` refreshes its token silently from then on. A password is never accepted, let alone stored.

## Install

```bash
git clone https://github.com/tobias/gog-repo-downloader
cd gog-repo-downloader
./gogdl.sh --help          # sets everything up on first run
```

The launcher script creates the virtual environment on first use and passes all arguments through. It resolves its own location, so a symlink makes it available everywhere:

```bash
ln -s "$PWD/gogdl.sh" /usr/local/bin/gogdl
```

On Windows, use `gogdl.bat` the same way.

If you prefer to manage the environment yourself:

```bash
uv venv && uv pip install -e .
```

Requires Python 3.12+ either way. `pip install -e .` works just as well. Dependencies are `httpx` and `rich`, nothing else.

Note: the CLI output is currently in German. The behavior documented here is language-independent.

## Quickstart

```bash
gogdl login                       # once - opens your browser, paste the URL back
gogdl --dest ~/GOG update         # fetch library metadata
gogdl --dest ~/GOG status         # show what would happen, change nothing
gogdl --dest ~/GOG download       # download, resume, verify, clean up
```

`--dest` defaults to `~/GOG` and deliberately not to the current directory: the tool writes a database there, downloads gigabytes into it, and deletes superseded files inside it. Pointing it at a git working tree prints a warning. `--dest` works before and after the subcommand: `gogdl --dest ~/GOG status` and `gogdl status --dest ~/GOG` are equivalent.

By default this fetches installers for **your current platform** in **English**, including DLC. Everything else is opt-in:

```bash
gogdl --dest ~/GOG download --os windows+linux --lang de,en --extras
```

That reads as: Windows *and* Linux builds, German *or else* English, bonus content included. The notation is explained below.

If you already have a collection downloaded with `gogrepo` or another tool, run `gogdl import` before the first `download` - otherwise everything gets downloaded again. See [Importing an existing collection](#importing-an-existing-collection).

## Commands

| Command | What it does |
|---|---|
| `login` | Authenticate once; stores only a refresh token (`--no-browser` to skip auto-opening) |
| `update` | Fetch library and file metadata into the local manifest |
| `status` | Show what is missing, outdated, and cleanable - changes nothing |
| `download` | Download missing and outdated files, then clean up old versions |
| `verify` | Check local files against the manifest; `--deep` adds MD5 and an archive integrity test |
| `import` | Match an existing collection against the manifest (`--apply` to commit, `--trust` see below) |
| `clean` | Run the cleanup separately (`--apply` to actually delete; without it, preview only) |
| `sync` | `update` + `download` in one call, for cron |

## Choosing platforms and languages

`--os` and `--lang` share one notation. **A comma means "otherwise", a plus means "and".**

```bash
--lang de,en          German, and only if there is no German build, English
--lang de+en          both German and English
--lang de+en,fr       German and English; French only if neither exists
--os linux+mac        Linux and macOS, never Windows
--os mac,windows      macOS, falling back to Windows for games without a Mac build
--os all              every platform
```

Platform names are `windows`, `linux`, `mac`. The default for `--os` is the platform you are running on; the default for `--lang` is `en`.

The language choice is made **per platform**, not once for the whole game. A title that ships German on Windows but only English on macOS gives you `windows/de` and `mac/en` under `--os windows+mac --lang de,en` - a single global choice would silently drop the Mac build.

A platform level that offers nothing in an acceptable language counts as a miss, and the next fallback level applies: `--os mac,windows --lang de` on a game whose Mac build is English-only gives you the German Windows build rather than nothing.

## Options

Filters, available on `update`, `status`, `download`, `verify`, `clean`, and `sync`:

```
--os / --lang                  see above; defaults: current platform, en
--dlc / --no-dlc               default: on
--extras / --no-extras         default: off (bonus content is large and rarely needed)
--patches / --no-patches       default: off
--strict                       compare MD5 for installers too, not only for extras
```

Game selection, available on `update` and `sync` (they decide what enters the manifest):

```
--only <slug|id>               restrict to specific games (repeatable)
--skip <slug|id>               exclude specific games (repeatable)
```

Download options (`download`, `sync`):

```
--jobs N                       parallel downloads (default 2; update uses 4 for metadata)
--limit-rate 5M                throttle (K/M/G suffixes)
--dry-run                      show downloads and deletions, do neither
```

### Cleanup options

Available on `download`, `clean`, and `sync`:

```
--prune                  default - remove old versions once replaced and verified
--no-prune               keep every version (full archive / version history)
--keep-versions N        default 1 (current only); 2 keeps one generation as a fallback
--prune-mode trash       move to <dest>/.trash/<date>/ instead of deleting
```

`gogdl` only ever deletes files it recorded itself and for which a verified replacement exists on disk. Files GOG has withdrawn from sale, and anything in the destination the tool did not put there, are reported and left alone. Every removal is logged with size and reason, so a cron log shows where the space went.

One interaction worth knowing: after a prune, the old version is gone. If a later `verify --deep` fails on the new file, there is no local fallback and a re-download is the only fix. If that bothers you, use `--keep-versions 2` or `--prune-mode trash`.

## Importing an existing collection

A collection downloaded with `gogrepo` or by hand is, from `gogdl`'s point of view, foreign data: it would re-download everything and clean up nothing. `import` matches the files on disk against the manifest so the tool adopts them as its own:

```bash
gogdl --dest ~/GOG update            # the manifest must exist first
gogdl --dest ~/GOG import            # preview the matching
gogdl --dest ~/GOG import --apply --trust size
```

The import changes only the database, never a file on disk. When in doubt it does not match: a file whose name fits but whose size does not is reported and skipped, because a wrong match could later authorize a deletion. Running `import` twice is harmless - it only touches manifest entries that have no local file yet.

`--trust` decides whether the adopted files count as *verified*, which is the precondition for ever deleting their predecessors:

| Level | Meaning |
|---|---|
| `none` (default) | Adopt, but attest nothing. Pruning stays inert for these files until `verify` confirms them. |
| `size` | A size match is accepted as evidence. Fast, reasonable for a collection you trust. |
| `md5` | Only a passed MD5 check counts. Thorough, but takes a long time on large collections. |

## Running unattended

```bash
0 4 * * *  gogdl --dest /srv/gog --quiet sync
```

Output automatically switches to plain line-by-line messages without ANSI control codes when not attached to a terminal; `--quiet` reduces it to errors.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | nothing to do |
| 10 | work was done (or, for `status` and dry runs, would be done) |
| 2 | authentication missing, expired, or rejected |
| 3 | unexpected API response |
| 4 | download or verification failure (rerun resumes where it stopped) |
| 5 | a planned deletion failed its safety check |
| 6 | manifest database not readable or writable |
| 7 | network failure (DNS, timeout, connection loss) |
| 130 | interrupted; a rerun resumes |

## How it stores things

```
<dest>/
  witcher-3-wild-hunt/
    setup_witcher3_4.04_(64bit)_(12345).exe
    setup_witcher3_4.04_(64bit)_(12345)-1.bin
  .gogdl/manifest.sqlite3      # metadata and local state
  .trash/                      # only with --prune-mode trash
```

Flat per game, using GOG's own filenames - which is what makes existing `gogrepo` collections importable. The manifest is SQLite in WAL mode rather than one big JSON file: an interrupted run can never cost the whole state, and queries like "all stale Windows files" need no full parse. In-progress downloads live next to their target as `.part` files.

## Design notes

This section explains why the tool behaves the way it does. You do not need it to use `gogdl`; you do need it to change `gogdl`.

### Ownership and authentication

Because only owned games are downloadable, every command except `--help` requires an authenticated account. `gogrepo`-style form-POST logins break whenever GOG decides the pattern looks automated and raises reCAPTCHA; the open issues of those projects document this repeatedly. `gogdl` does not try. `login` opens GOG's regular OAuth2 login page in your browser (where reCAPTCHA and 2FA mail codes are a non-issue), you paste back the redirect URL containing `?code=...`, and the tool exchanges it for tokens.

Only the refresh token is persisted, in `~/.config/gog-repo-downloader/auth.json` with mode 0600. Access tokens are renewed silently. The client id and secret in `constants.py` are the publicly documented GOG Galaxy client credentials, the same ones `lgogdownloader` uses; they identify the client software, not a user, and are not a secret to protect.

### Update detection

GOG changes installers in two ways: with a new filename, and by silently re-uploading under the identical name (repacks, hotfixes, changed language files). A filename comparison passes every test you write for it and permanently misses the second case, so the filename is treated as a storage detail, never as a freshness signal.

The authoritative signals, in order of precedence:

| # | Signal | Source | Cost |
|---|---|---|---|
| 1 | `version` | product metadata | free, included in `update` |
| 2 | `size` | product metadata | free |
| 3 | `md5` | checksum XML sidecar | one request per file |

A mismatch in any signal marks the file stale. Extras carry no `version` (and no OS or language), so for them `size` moves up and `md5` becomes the primary signal instead of a tiebreaker; for installers, MD5 comparison during planning is opt-in via `--strict` and always part of `verify --deep`. Some files have no checksum sidecar at all; they are marked as such in the manifest and `verify` says so rather than pretending.

Files that GOG withdraws from the offering are never deleted locally. They are marked `orphaned` and reported - your archive keeps them.

### Downloading and resuming

Resolving a download link yields a **time-limited signed CDN URL**. Persisting it and reusing it hours later gets a 403, so resolved URLs are never stored: every attempt, including every resume, resolves the link freshly.

The resume path has one trap that only shows up in production: a CDN that ignores the `Range` header answers `200` instead of `206` and sends the file from the beginning. Appending that to the existing partial file corrupts it **silently** - size and progress look right, the content is garbage. `gogdl` therefore requires a `206` (and a matching `Content-Range` offset) to append; on a `200` it discards the partial file and restarts cleanly. This case has dedicated tests.

Downloads go to a `.part` file and are renamed only after the size and, where available, MD5 check passes. The number of bytes already done is read from the `.part` file itself, not from the database: the file on disk is the truth, the database is a cache. Parallelism is per file (default 2 jobs); a single file is never split across connections, which would irritate rate limits and complicate resume for little gain on GOG's CDN. HTTP 429 is answered with exponential backoff honoring `Retry-After`.

### Pruning safely

Deletion is the one irreversible operation, so everything hinges on ordering and on what counts as an "old version".

The unit of pruning is the **slot**: product + kind + OS + language. Never the single file. Large installers are multi-part (`..._(1).bin`, `..._(2).bin`); pruning per file would delete part 1 of the old version while part 2 of the new one is still downloading, and an aborted run then leaves *no* complete version at all. A slot's old files are removed only when **every** file of the new version in that slot is complete and verified (size, plus MD5 where a checksum existed).

What may be deleted, and what never:

| Category | Behavior |
|---|---|
| Earlier version of a slot, recorded in the manifest, verified replacement on disk | deleted |
| `.part` remnants of a version no longer offered | deleted |
| File GOG withdrew from the offering (`orphaned`) | kept, reported |
| File in the destination the tool never recorded | kept, reported |

On top of that sit hard mechanical guards: no path outside `<dest>/<slug>/`, no symlink traversal, an absolute path comparison against `dest` before every unlink. And the whole check runs twice: `sync/` plans a deletion only for entries with a verified replacement, and `prune/` re-checks every precondition before touching anything. A plan entry that fails re-checking is refused, not executed.

### What gogdl deliberately does not do

- **No anonymous mode, no catalog scraping.** Only the signed-in account's own library.
- **No Galaxy content-system downloads.** GOG's chunk-based Galaxy path would allow true delta updates, but it produces an unpacked game directory, not an installer archive, and costs considerable complexity (chunk reassembly, depot resolution, v1/v2 formats). For an offline archive of installers it is the wrong tool. The API layer is cut so that a second download backend could be added later without touching the sync logic.
- **No password login.** By design, see above.
- **No multi-connection download of a single file.**
- **No packaged release yet.** Install from source.

## Architecture

```
cli/        argument parsing, command orchestration - the only place modules meet
sync/       staleness decision and prune plan - pure, no I/O of any kind
download/   resume, range handling, .part files, verification
prune/      executes a prune plan, re-checks its preconditions
importer/   matches an existing collection to the manifest
api/        typed GOG clients, retry, rate limiting
auth/       OAuth2 token flow and storage
store/      SQLite manifest
ui/         progress reporting, TTY and plain
model/      dataclasses and Protocols - the contract, depended on by everyone
errors.py   exception hierarchy - flat, not extended by modules
```

Two rules carry the design:

**`sync/` is free of I/O.** Its inputs are plain values: the remote metadata, the manifest entries, and the observed disk state as a `dict[Path, int]`. Its outputs are a download work list and a prune plan. The two riskiest decisions in the tool - "is this stale?" and "may this be deleted?" - are thereby testable without a network, a GOG account, or a filesystem. `prune/` and `download/` only execute what `sync/` decided, and `prune/` re-validates before acting.

**`model/` and `errors.py` are the contract.** Modules implement the Protocols defined there; they do not change them. If a signature is wrong, it gets fixed in `model/` deliberately, with every implementer updated - not widened locally. The error hierarchy is flat and owned by `errors.py` alone; exit codes live on the exception classes.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest                        # full suite
.venv/bin/pytest tests/test_sync.py     # single module
```

**Every test runs offline.** Network access is mocked through `httpx.MockTransport`; a test that needs a real GOG account is a test in the wrong place. Docstrings, comments, and CLI output are in German; README and identifiers are English.

Pitfalls to know before changing things:

- A `200` answer to a `Range` request means the CDN ignored the range. Appending corrupts silently; discard and restart. This is the single most expensive failure mode in the project and it only shows up in production.
- Pruning per file instead of per slot loses data on multi-part installers. The slot logic is not an optimization, it is the safety property.
- `sync/` must list every part of a replacement in a prune item's `replaced_by`. A 1:1 mapping of old part to new part means multi-part installers are simply never pruned - which is silent and looks like the feature not firing.
- Extras have no `version`, no OS, and no language. Filters must not drop them, and `md5` is their primary staleness signal.
- SQLite treats NULLs in unique constraints as distinct values, so the manifest keys on a computed `slot_key` string rather than on the nullable columns.
- Signed CDN URLs expire. Resolve the downlink fresh before every attempt; never persist a resolved URL.
- Never store or accept a password. Only the refresh token is persisted, at mode 0600.

## Status

Phase 1, honestly stated: the sync planner, the prune executor, the import matcher, the download engine's range and resume handling, the store, and the auth flow are covered by the offline test suite, including the two failure modes above. What no test covers, because it needs a real GOG account: the live login against `auth.gog.com`, the exact shape of current API responses, and the real CDN's behavior under resume. The API layer is written against GOG's documented endpoints and against the behavior `lgogdownloader` relies on, but until it has run against a real library, treat those paths as implemented, not proven.

## Credits

Prior art this builds on: [gogrepo](https://github.com/eddie3/gogrepo) and its maintained fork [gogrepoc](https://github.com/Kalanyr/gogrepoc), and [lgogdownloader](https://github.com/Sude-/lgogdownloader), whose API usage is the clearest reference available.

Not affiliated with GOG.com.
