# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`gogdl` downloads the signed-in user's own GOG.com library, keeps it current, and removes
superseded installers automatically. Python 3.12+, packaged under `src/gogdl`, entry point
`gogdl.cli.main:main` (console script `gogdl`). Dependencies: `httpx`, `rich`.

GOG games are DRM-free, not copyright-free. There is deliberately no anonymous mode: every
command except `--help` requires an authenticated account. Do not add catalog scraping.

## Build & Test

```sh
uv venv && uv pip install -e ".[dev]"   # setup
.venv/bin/pytest                        # full suite, no network required
.venv/bin/pytest tests/test_sync.py     # single module
```

Every test runs offline. Network access is mocked through `httpx.MockTransport`; a test that
needs a real GOG account is a test in the wrong place. The live paths that no test covers are
listed under Gotchas.

## Architecture

Layers, and what may depend on what:

```
cli/        argument parsing, command orchestration - the only place modules meet
sync/       staleness decision and prune plan - PURE, no I/O of any kind
download/   resume, range handling, .part files, verification
prune/      executes a prune plan, re-checks its preconditions
api/        typed GOG clients, retry, rate limiting
auth/       OAuth2 token flow and storage
store/      SQLite manifest
ui/         progress reporting, TTY and plain
model/      dataclasses and Protocols - the contract, depended on by everyone
errors.py   exception hierarchy - flat, not extended by modules
```

`model/` and `errors.py` are the contract. A module implements a Protocol, it does not change
one. If a signature is wrong, fix it in `model/` deliberately and update every implementer, do
not widen it locally.

`sync/` must stay free of I/O. Observed disk state enters as an `on_disk: dict[Path, int]`
parameter. This is what makes the two hardest decisions in the tool testable without a network
or an account.

## Key patterns

- Staleness is decided by content, never by filename: `version`, then `size`, then `md5`, any
  mismatch means stale. GOG re-uploads under identical filenames.
- The prune unit is the slot (`SlotKey`: product, kind, os, language), never a single file.
  Multi-part installers share one slot.
- Deletion requires `ManifestEntry.is_verified_complete` on every replacement file. `sync/`
  plans it, `prune/` checks it again before touching anything.
- Signed CDN URLs are short-lived. Resolve the downlink fresh before every attempt and never
  persist a resolved URL.
- The file on disk is the truth, the database is a cache. `bytes_done` comes from the `.part`
  file's actual size.
- German docstrings and comments, matching `model/types.py`. User-facing CLI output is German,
  README and CLAUDE.md are English.

## Gotchas

- **A `200` answer to a `Range` request means the CDN ignored the range.** Appending to the
  existing `.part` corrupts the file silently. Discard and restart instead. This is the single
  most expensive failure mode in the project and it only shows up in production.
- **Pruning per file instead of per slot loses data.** A multi-part installer can lose part 1 of
  the old version while part 2 of the new one is still downloading, leaving no complete version
  at all.
- `sync/` must emit every part of a replacement in `PruneItem.replaced_by`. A 1:1 mapping of old
  part to new part means multi-part installers are never pruned, which is silent and looks like
  the feature simply not firing.
- Extras carry no `version` and no os/language. Filters must not drop them, and `md5` becomes
  their primary signal instead of a tiebreaker.
- SQLite treats NULLs in unique constraints as distinct, so the manifest keys on a computed
  `slot_key` string rather than the nullable columns.
- The Galaxy client id and secret in `constants.py` are the publicly documented values. They
  identify the client, not the user, and are not a credential to protect.
- Never store or accept a password. Only the refresh token is persisted, at mode 0600.

## Agent workflow

- Before planning or implementing, load `agent-memory` context for `global` and
  `gog-repo-downloader`; align the plan with active memory entries.
- Task queues in agent-memory are the only workflow state. Never treat `todo.md` or other
  markdown as task state.
- Planning: `memory_queue_create` + `memory_queue_enqueue`. Execution: `memory_queue_next` →
  `memory_queue_claim` → implement + tests → `memory_queue_complete`. Review:
  `memory_queue_review_result` with verdict `ok` | `revise` | `blocked`.
- Code exploration goes through the tokensave MCP tools, not file reads.
- When several agents write concurrently, give them disjoint module directories and one test
  file each. `model/`, `errors.py`, `constants.py` and `pyproject.toml` belong to nobody but the
  orchestrating session.

## Git

Work happens on `main`, no feature branches. Stage by name, never `git add -A`.

Nothing lands unless `.venv/bin/pytest` is green. `KONZEPT.md` is the design record and carries
the reasoning behind the rules above; update it when a rule changes rather than letting the two
drift apart.
