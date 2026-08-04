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

<!-- msc:standards:start -->

## Workspace standards

Generated from `standards.json` (mcp-server) - change it there and reinstall,
never inside the markers. `project_standards` serves the incident behind a rule
(`rule: "<id>"`, ask before weakening one) and the setup rules not printed here;
they bind the same.

### Working with the user

- **Result first, details on request** - Status in one sentence, then at most three bullet points, keyword style and solution oriented. Reasons, alternatives and technical detail such as file paths and line numbers only on request. No tables or subheadings for intermediate states, nothing repeated that already stands in a task, no em dash anywhere - hyphen instead. `collab.answers`
- **Be critical, and say so in one sentence** - Name contradictions, mistakes and missing information in one sentence rather than working around them, and say what nobody has thought to ask yet. Never guess: ask while Tobias is reachable, decide autonomously offline and present the assumption later. `collab.not-a-yes-man`
- **Assume several sessions run in the same workspace** - Never assume a clean working tree or exclusive access to a device, a build or a file. Be frugal with memory and compute. `collab.parallel-sessions`
- **Neutral, gender-inclusive language and accessibility throughout** - Gender-inclusive wording and accessibility are requirements in every change, not a later pass. `collab.language`
- **Match the model to the job** - Agents run on Opus or Sonnet, whichever does the work reliably, and text deliverables - store texts, documentation, marketing copy - are written by Fable. An advisor always uses the stronger model available - Fable or Opus. `collab.models`

### Git

- **Work happens on `main`** - No feature branches. Commit to `main` directly, in small steps that keep it green. `git.trunk`
- **Claim files before editing them** - Claim via `memory_claim_files`, release when done. Rebase before pushing, never force-push `main`. Stage by name - `git add <path>`, never `git add -A`, never `git add .`, never `git commit -a`. What you did not change is not yours to commit. `git.parallel`
- **Never point a git command at the whole tree** - `git reset`, `git checkout -- .`, `git stash` without paths, `git clean` and `git restore .` hit every session working in that checkout, not just yours. Name paths, or do not run it. Needing a clean tree for a measurement is not an exception - use `git worktree add` and measure there. `git.no-sweeping-commands`
- **In a shared tree, commit as soon as it is green** - Do not carry a large uncommitted change set through a long measurement or a wait. Commit the part that builds and passes, keep working from there. A commit is cheap to revert; uncommitted work in a shared checkout is a bet on nobody else touching it. `git.commit-when-green`

### Tooling

- **Code exploration goes through tokensave** - Its MCP tools, not file reads and not Explore agents; a PreToolUse hook enforces this. `tooling.tokensave`
- **iOS builds, tests, simulators and devices go through `simulator-broker`** - Never `xcodebuild`, `simctl` or `devicectl` directly - scripts and physical devices go through `simulator-broker/src/cli.mjs run --project <name> -- <command>`. `tooling.builds`
- **Throwaway work goes in the session scratchpad, named so housekeeping finds it** - Working copies, build output and coverage runs go in the session scratchpad, never in a repository or loose in `/tmp`; name build output `build/`, `Build/` or `DerivedData/`. `tooling.scratch`
- **Task state lives in agent-memory** - Never in `todo.md` or another markdown file. Writing a read-only export is fine; reading state back out of it is not. `tooling.state`
- **One active queue per project** - Everything a project has to do goes in that one queue; `dependsOn` is the only hard gate and resolves inside its own queue. `tooling.one-queue-per-project`
- **Questions for Tobias go to the queue `entscheidungen-tobias`** - Anything blocked on a decision by Tobias goes to the queue `entscheidungen-tobias` (project `tobias`), never into the project backlog - three lines: the decision, the options with consequences, what stands still. `tooling.decisions-queue`
- **Writing agents get their own worktree while a run needs a stable tree** - A build or test run reads the working tree for minutes; an agent editing during that window makes the result meaningless. Give concurrently writing agents `isolation: "worktree"`, then apply their diffs with `git apply -3` once the run is done. Only bundle agents into the shared tree when nothing is measuring. `tooling.worktree-for-parallel-writes`
- **Say it to the other session, not only about it** - When you find that another session broke something, damaged your work or is about to, send it a `memory_control_send` message naming the files, the rule and the concrete next step - and claim your files in the same breath. A finding that only reaches the human arrives after the next collision. `tooling.tell-the-other-session`

<!-- msc:standards:end -->
