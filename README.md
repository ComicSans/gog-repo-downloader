# gogdl

Keep a local, offline copy of **your own GOG.com library** - always up to date, resumable, and without the disk slowly filling up with obsolete installers.

GOG games are DRM-free, which makes archiving them straightforward. They are not copyright-free: `gogdl` only ever downloads games the signed-in account actually owns, which is why authentication is mandatory and there is no anonymous mode.

---

## Why another one

`gogrepo` and friends solved this years ago, and mostly still work. `gogdl` differs in three places that matter in daily use:

**Updates are detected by content, not by filename.** GOG bumps installer filenames *and* silently re-uploads under the same name. Comparing filenames looks correct and quietly misses the second case. `gogdl` compares `version`, then `size`, then the MD5 from GOG's checksum sidecar - any mismatch means stale.

**Old versions are cleaned up automatically.** On by default, because otherwise every update cycle costs a full installer generation. The deletion only happens after the replacement is completely downloaded *and* verified, and it operates on whole releases - never on individual files, so a multi-part installer can never lose part 1 while part 2 of the new version is still downloading.

**Login uses the real browser.** No username/password automation to be broken by reCAPTCHA. You sign in at GOG the normal way, paste back the code once, and `gogdl` refreshes its token silently from then on.

---

## Install

```bash
git clone https://github.com/tobias/gog-repo-downloader
cd gog-repo-downloader
uv venv && uv pip install -e .
```

Requires Python 3.12+. `pip install -e .` works just as well.

## Quickstart

```bash
gogdl login                       # once - opens your browser, paste the code back
gogdl --dest ~/GOG update         # fetch library metadata
gogdl --dest ~/GOG status         # what would happen, without doing it
gogdl --dest ~/GOG download       # download, resume, verify, clean up
```

`--dest` defaults to `~/GOG` and never to the current directory: the tool writes a database there, downloads gigabytes into it, and deletes superseded files inside it. Pointing it at a git working tree prints a warning.

By default this fetches installers for **your current platform** in **English**, including DLC. Everything else is opt-in:

```bash
gogdl --dest ~/GOG download --os windows,linux --lang de,en --extras
```

## Commands

| Command | What it does |
|---|---|
| `login` | Authenticate once; stores only a refresh token |
| `update` | Fetch library and file metadata into the local manifest |
| `status` | Show what is missing, outdated, and cleanable - changes nothing |
| `download` | Download missing and outdated files, then clean up old versions |
| `verify` | Check local files against the manifest (`--deep` for MD5) |
| `clean` | Run the cleanup separately (`--apply` to actually delete) |
| `sync` | `update` + `download` in one call, for cron |

## Options

```
--os windows,linux,mac | all   default: the platform you are running on
--lang de,en                   default: en
--dlc / --no-dlc               default: on
--extras / --no-extras         default: off
--patches / --no-patches       default: off
--only <slug|id>               restrict to specific games (repeatable)
--skip <slug|id>               exclude specific games (repeatable)
--jobs N                       parallel downloads (default 2)
--limit-rate 5M                throttle
--dry-run                      show downloads and deletions, do neither
```

### Cleanup options

```
--prune                  default - remove old versions once replaced and verified
--no-prune               keep every version
--keep-versions N        default 1 (current only); 2 keeps one generation as a fallback
--prune-mode trash       move to <dest>/.trash/<date>/ instead of deleting
```

`gogdl` only ever deletes files it downloaded itself and for which a verified replacement exists. Files GOG has withdrawn, and anything in the destination it did not put there, are reported and left alone.

## Cron

```bash
0 4 * * *  gogdl --dest /srv/gog --quiet sync
```

Exit codes: `0` nothing to do · `10` something was downloaded or removed · anything else is an error. Output automatically drops ANSI progress bars when not attached to a terminal.

## How it stores things

```
<dest>/
  witcher-3-wild-hunt/
    setup_witcher3_4.04_(64bit)_(12345).exe
    setup_witcher3_4.04_(64bit)_(12345)-1.bin
  .gogdl/manifest.sqlite3      # metadata and local state
  .trash/                      # only with --prune-mode trash
```

Flat per game, matching GOG's own filenames - so an existing `gogrepo` collection can be adopted as-is.

## Status

Phase 1. The API layer is written against GOG's documented endpoints and covered by mocked tests; the live login and CDN behaviour need a real account to exercise. See [KONZEPT.md](KONZEPT.md) for the full design (in German), including the failure modes this tool is built to avoid.

## Credits

Prior art this builds on: [gogrepo](https://github.com/eddie3/gogrepo) and its maintained fork [gogrepoc](https://github.com/Kalanyr/gogrepoc), and [lgogdownloader](https://github.com/Sude-/lgogdownloader), whose API usage is the clearest reference available.

Not affiliated with GOG.com.
