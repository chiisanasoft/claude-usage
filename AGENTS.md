# AGENTS.md

Guidance for any coding agent (Codex, Claude Code, etc.) working on this repository.

> **Naming note.** This project *analyzes* Claude Code's local usage logs, so "Claude Code" below always refers to that product (the source of the JSONL data) — not to the agent reading this file. The agent working on the codebase is referred to as "the coding agent" or just "you".

## Project shape

Four Python files, stdlib only, no `pip install` step. Python 3.8+.

- [scanner.py](scanner.py) — parses Claude Code JSONL transcripts into a SQLite DB at `~/.claude/usage.db`.
- [cli.py](cli.py) — terminal commands (`scan` / `today` / `week` / `stats` / `limits` / `dashboard`).
- [dashboard.py](dashboard.py) — single-file `http.server` serving an embedded HTML/JS SPA on `localhost:8080`.
- [limits.py](limits.py) — reconstructs the account-level rate-limit windows (rolling 5-hour session + weekly) from the `turns` table, optionally overlaid with the live read-only usage API.

Use `python` on Windows, `python3` on macOS/Linux. Both work the same.

## Common commands

```
python cli.py scan                  # incremental scan (fast on re-run)
python cli.py today                 # today's usage by model
python cli.py week                  # last 7 days, per-day + by-model
python cli.py stats                 # all-time stats
python cli.py limits                # 5h session + weekly limit indicators
python cli.py limits --use-api --json     # opt in to the usage API (reads the keychain)
python cli.py limits --debug-api          # verbose (token-free) API diagnostics
python cli.py dashboard                          # scan + open http://localhost:8080
python cli.py dashboard --host 0.0.0.0 --port 9000
python cli.py scan --projects-dir PATH           # scan a custom transcripts dir
# or via env vars:
HOST=0.0.0.0 PORT=9000 python cli.py dashboard

python -m unittest discover -s tests -v             # full test suite (CI runs this)
python -m unittest tests.test_scanner -v            # one file
python -m unittest tests.test_scanner.TestProjectNameFromCwd.test_windows_path  # one test
```

CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs the suite on Python 3.9 / 3.11 / 3.12 against `main` and PRs.

## Architecture

### Data flow

```
~/.claude/projects/**/*.jsonl   →   scanner.parse_jsonl_file()
~/Library/.../Xcode/...                  ↓
                              aggregate_sessions() → upsert_sessions() + insert_turns()
                                         ↓
                              ~/.claude/usage.db (SQLite)
                                         ↓
                  cli.py queries   ←──────────→   dashboard.py /api/data
```

By default the scanner walks both `~/.claude/projects/` and the Xcode coding-assistant directory; missing dirs are silently skipped. Override with `--projects-dir`.

### SQLite schema (created/migrated in [scanner.py](scanner.py) `init_db`)

- **`turns`** — one row per assistant API response. The source of truth for tokens and per-model attribution.
- **`sessions`** — aggregated per session (denormalized totals + chosen primary model).
- **`processed_files`** — incremental-scan tracking: `(path, mtime, lines)`. A file is skipped if its mtime matches; if it grew, only lines past the stored `lines` count are processed.

A conditional unique index on `turns.message_id` (where non-empty) lets `INSERT OR IGNORE` cheaply dedupe replays across rescans.

### Non-obvious invariants

These three things will bite you if you don't know them:

1. **Streaming dedupe by `message.id`.** Claude Code writes multiple JSONL records per API response — only the *last* one for a given `message.id` has the final usage tallies. `parse_jsonl_file` keeps the last record per `message_id` in a dict; earlier records are discarded. Don't sum across records of the same `message_id`.

2. **Session totals are recomputed from `turns` at the end of `scan()`.** During an incremental scan `upsert_sessions` adds tokens additively, but `insert_turns` uses `INSERT OR IGNORE` against the `message_id` unique index — so if a turn is a duplicate, session totals would drift. The final `UPDATE sessions ... (SELECT SUM ... FROM turns)` block reconciles this. Preserve it if you refactor scan logic.

3. **Session primary model priority is opus > sonnet > haiku** (`_model_priority` in [scanner.py](scanner.py)). This prevents a subagent's haiku turn from overwriting the session's opus model when an existing session is updated. Per-turn model is always honored in the `turns` table; only the session-level summary uses the priority.

4. **The OAuth token must be read from `claudeAiOauth.accessToken`** in the macOS keychain item `Claude Code-credentials` (`_claude_oauth` / `_find_token` in [limits.py](limits.py)). Never locate it by recursively searching for a key named `accessToken`: the same credential blob also contains `mcpOAuth.<plugin>.accessToken` entries that are empty strings and that a generic recursive search matches first, silently disabling the API overlay. (`_find_key` exists for the *response* parser, not for credentials.)

5. **`GET https://api.anthropic.com/api/oauth/usage` needs all three headers** — `Authorization: Bearer <token>`, `anthropic-beta: oauth-2025-04-20`, and `anthropic-version: 2023-06-01` (`_api_headers`). Dropping any one of them fails the request. The endpoint is read-only: it runs no inference and costs nothing. It **is** rate limited, though — it answers `429 rate_limit_error` under frequent polling, which was observed in practice with a 30s dashboard refresh plus ad-hoc CLI runs. So `fetch_api_usage` caches the raw response on disk (`~/.claude/claude-usage-limits-state.json`) for `API_CACHE_SECONDS` and enters an `API_BACKOFF_SECONDS` quiet period after a 429, serving the last good snapshot meanwhile. Never call it unconditionally from a loop, and never let a throttled request silently downgrade the UI to a local-only estimate. A `null` `seven_day_opus` in the response means the account has **no separate Opus weekly cap** — the corresponding UI row must be hidden, not rendered at 0%. That structure is remembered in `known_sublimits` so an API outage doesn't resurrect a fabricated Opus/Sonnet row (`include_opus` in `compute`).

### Cost calculation

Costs are computed **per turn** (each turn knows its own model), then summed. This is true in both the CLI ([cli.py](cli.py) `calc_cost`) and the dashboard JS ([dashboard.py](dashboard.py) `calcCost` inside the embedded HTML). Aggregating tokens first and applying a single price is wrong for sessions that span multiple models.

Pricing is duplicated in two places that **must stay in sync**:
- [cli.py](cli.py) `PRICING` dict (Python)
- [dashboard.py](dashboard.py) `PRICING` const inside `HTML_TEMPLATE` (JavaScript)

`get_pricing` / `getPricing` resolve in three tiers: exact match → `startswith` (handles date-suffixed model IDs like `claude-opus-4-7-20260215`) → substring fallback on `fable`/`mythos` / `opus` / `sonnet` / `haiku`. Models that don't match any tier return `None` — intentional, so local/3rd-party models (gemma, glm, etc.) aren't charged at Sonnet rates.

**A miss must stay visible.** Two rules follow from it, and both exist because breaking them once cost a database 33% of its true total with no symptom:

- Per-model rows render `n/a`, not `$0.00` (`fmt_model_cost`). "It cost nothing" and "we have no price for it" are different facts and must not share a glyph.
- Every miss is recorded in `cli.UNKNOWN_MODELS`, and `warn_unknown_models()` reports them on stderr *after* the report, saying the totals are an undercount. Don't move that call ahead of the output, and don't let a new code path compute costs without it.

The substring fallback is a second hazard: a newly released model resolves to whatever its family last cost. That is silently wrong in *both* directions — Sonnet 5 is cheaper than Sonnet 4.x, so the guess overcharged it by 50%, which is far harder to notice than a zero. **New Anthropic models need an explicit `PRICING` entry**, in `cli.py` and in the JS copy in `dashboard.py`. `tests/test_cli.py` guards both rules.

### Limit indicators

[limits.py](limits.py) answers "how much of my plan have I used" without any data Anthropic publishes numerically:

```
turns table  →  _window_by_model()  →  per-turn cli.calc_cost()  →  consumption ($ equivalent)
                                                  ↓
config ~/.claude/claude-usage-limits.json (cap_usd per window)   →  pct = consumption / cap
                                                  ↓
optional overlay: GET /api/oauth/usage  →  parse_api_usage()  →  exact pct + resets_at
                                                  ↓                     ↓
                          compute() → {session, weekly_all, weekly_opus, weekly_sonnet}
                                                                  auto-calibration write-back
```

Key points:

- **Consumption is the API-equivalent USD cost of each turn, via `cli.calc_cost`** — imported, not reimplemented. Do not add a third pricing copy; subscription limits behave roughly like an API-dollar budget, so cost is a self-consistent proxy for limit consumption.
- **The session window is a rolling 5-hour block floored to the hour.** `_current_block_start` walks turns from the last 12 hours: the first turn opens a block starting at its timestamp floored to the whole hour, and any turn at or after `block_start + 5h` opens a new block. If the newest block has already elapsed there is no active session window (0 turns, no countdown) — this mirrors how Claude's own 5-hour windows are anchored.
- **The weekly window** is a rolling 7 days by default, or anchored to a local weekday+hour when `weekly_all.reset_dow` / `reset_hour` are set (`--weekly-reset DOW HOUR`).
- **Three cap sources per window**, resolved in `_block`: `api` (exact pct from the live response) > `calibrated` (`cap_usd` from the config) > `uncalibrated` (`pct` is `None`; the UI shows `—`).
- **Auto-calibration write-back.** When a fresh API response gives a percentage > 1% and local consumption > 0, `compute()` derives `cap_usd = cost / (pct/100)` and persists it (plus the detected `plan`) back to `~/.claude/claude-usage-limits.json`. This is the only place a read path writes state — keep it best-effort and exception-swallowing so a read-only home directory can't break `limits`. `calibrate()` does the same from a user-supplied observed percentage.
- **`compute(scan_first=True)`** runs an incremental scan before querying, so the numbers include turns written since the last scan. Both `cmd_limits` (unless `--no-scan`) and the dashboard's `/api/limits` use it.
- `parse_api_usage` looks keys up by several plausible names and tolerates missing nodes; keep it defensive — the endpoint is undocumented and its shape can change.
- The CLI surface is parsed by hand in `cmd_limits` ([cli.py](cli.py)), not by `argparse`: `--no-api`, `--json`, `--no-scan`, `--debug-api`, `--calibrate-session/-weekly/-opus/-sonnet N`, `--weekly-reset DOW HOUR`.

### Dashboard server

`http.server.BaseHTTPRequestHandler`-based, three endpoints:
- `GET /api/data` → JSON snapshot from `get_dashboard_data()`. Returns *all* history; client-side filters by date range and model.
- `GET /api/limits` → `limits.compute(db_path=DB_PATH, scan_first=True)`, rendered as the "Plan Limits" bar card at the top of the page and re-fetched every 30s. Import errors and failures are returned as `{"error": ...}`, which makes the client hide the card rather than break the dashboard.
- `POST /api/rescan` → deletes the DB and runs a full rescan. Passes `db_path` and `projects_dirs` explicitly so tests that monkey-patch the module globals work — scan's default arg values are frozen at def time, so don't switch to bare defaults.

The entire UI lives in `HTML_TEMPLATE` as a raw string. Chart.js is loaded from CDN.

Client-side UI state (collapsed sections, the 24h update-check cache) is kept in **`localStorage`**, which is keyed by the page's origin. In the VS Code extension the dashboard is embedded as an **iframe at `http://127.0.0.1:<port>/`**, so that state only survives a window reload if the port is stable. The extension therefore remembers the last port in `workspaceState` and reuses it when it's still free (`resolveStablePort` in [vscode-extension/src/port-allocator.ts](vscode-extension/src/port-allocator.ts)) — don't revert that to a fresh `pickFreePort` every launch, or the panel silently loses its state each reload.

### Container deployment

A container deployment of the dashboard exists: [Dockerfile](Dockerfile), [docker-compose.yml](docker-compose.yml), documented in [docs/DOCKER.md](docs/DOCKER.md). It is packaging only — no application code is involved, and it must stay that way. Constraints worth knowing before you change anything it depends on:

- **No path configuration exists.** `DB_PATH` honours `CLAUDE_USAGE_DB` in `scanner.py` / `cli.py` / `dashboard.py` / `limits.py` and otherwise falls back to `Path.home() / ".claude" / "usage.db"`; the transcripts dir is only overridable via `scan --projects-dir` (`cmd_dashboard` doesn't take one on the server side). The Compose stack deliberately leaves `CLAUDE_USAGE_DB` unset and configures paths by setting `HOME=/home/appuser` and bind-mounting onto the paths the code already resolves, so the container and a native run agree on the layout.
- **`HOST` / `PORT` env vars are the only network knobs** (`serve()` in [dashboard.py](dashboard.py), `cmd_dashboard` in [cli.py](cli.py)). The image sets `HOST=0.0.0.0` — mandatory inside a container — and compose publishes to `127.0.0.1:8080` so the unauthenticated UI is not exposed to the LAN. Don't change the default `localhost` bind in the app; the container overrides it deliberately.
- **Transcripts are mounted read-only**; the only writable mount is a named volume at `/home/appuser/.claude` holding the derived `usage.db`. `claude-usage-limits.json` and `claude-usage-limits-state.json` are bind-mounted **read-only** from the host on top of that volume — without the config the container has no `cap_usd`, so every Plan Limits bar renders `—`. Neither file contains a credential. Because those mounts are `:ro`, the auto-calibration write-back in `compute()` (and `_write_state`) *must* stay exception-swallowing, or a container request would 500. The scanner must keep treating the projects dirs as strictly read-only. Bind-mounting a file that doesn't exist on the host makes Docker create a directory at that path, so the docs tell users to run `limits` natively once first.
- **The live limits API cannot work there.** `read_oauth_credential()` shells out to the macOS `security` binary; in a Linux container that raises and is swallowed, so `/api/limits` degrades to calibrated (from the mounted config) or uncalibrated. That graceful degradation (exception-swallowing in `read_oauth_credential` / `fetch_api_usage`) is load-bearing for the container — keep it. Do not add any code that extracts, forwards or persists the OAuth token for container use.
- **A cached API snapshot is only served as a fallback while it is young** (`API_SNAPSHOT_MAX_AGE_SECONDS`, 10 min, matching the back-off window). The mounted state file can never be refreshed inside the container, and rendering a days-old percentage as `source: "api"` / "live from API" is worse than falling through to the calibrated tier. The fresh-cache (`API_CACHE_SECONDS`) and 429 back-off paths are unaffected.
- The CMD is `python cli.py dashboard --no-browser`, which scans then serves without trying to open a browser.

## Testing notes

- `tests/test_scanner.py` and `tests/test_dashboard.py` use `tempfile.NamedTemporaryFile` for an isolated DB; never touch the user's real `~/.claude/usage.db`.
- The `/api/rescan` test patches `dashboard.DB_PATH` and `scanner.DEFAULT_PROJECTS_DIRS` — keep that contract intact (see commit 8ae2664).
- On Windows, `~/.claude/` may not exist on a fresh checkout. `get_db` creates the parent dir (`mkdir(parents=True, exist_ok=True)`) — don't remove that or `sqlite3.connect` will fail in CI / fresh installs (commit b5d1e15).

## Respecting contributors

When merging community PRs, **preserve the original author's commit so they get GitHub contributor credit**. In practice:

- `git fetch origin pull/<N>/head:pr-<N>` → `git merge --no-ff pr-<N>` keeps the author commit verbatim inside the merge bubble (don't squash, don't rebase-flatten).
- For a partial merge — when only one hunk of a PR is wanted — use `git cherry-pick <commit-sha>` against the specific upstream commit so authorship is preserved. If the diff isn't a clean single commit, fall back to applying the hunk manually + adding a `Co-Authored-By: Name <email>` trailer.
- Improvements that the bot/maintainer makes _on top_ of a contributor's work go in **separate follow-up commits**, not amendments to the contributor's commit.
- When closing duplicate PRs (multiple authors fixed the same bug independently), thank each one and explain that landing the earliest version isn't a quality judgment.

This applies to all agents working on this repo, not just Claude Code.

## Versioning and releases

[SemVer](https://semver.org/). **`CHANGELOG.md` is the canonical version reference**; tags are a projection of it, created automatically.

The release flow:
1. While work accumulates on `DEV`, the `## vX.Y.Z — TBD` heading at the top of `CHANGELOG.md` collects bullets. (For automated triage runs, see the routine note below.)
2. When the maintainer is ready to release, they finalize the heading (`TBD` → today's date), **bump both `scanner.VERSION` and `vscode-extension/package.json`'s `version` to match the CHANGELOG version** (all three ship in lockstep — the extension bundles the Python sources, and `scanner.VERSION` is the runtime version reported by `cli.py --version` and the dashboard footer since the CHANGELOG isn't bundled into the `.vsix`), **run [`scripts/bump-formula.sh`](scripts/bump-formula.sh) on `DEV` to repoint the Homebrew formula at the previous release's tag tarball** (see "Homebrew formula and self-referential SHA" below — this is a plain `DEV` commit that reaches brew users via this same merge, so it never touches `main` directly), merge `DEV → main` with `merge --no-ff` (so the release boundary is visible in `git log main`), and push `main`. A parity test (`tests/test_version.py`) fails the suite if the three drift, so in practice they're bumped together on `DEV` when the version heading is written.
3. [`.github/workflows/tag-on-merge.yml`](.github/workflows/tag-on-merge.yml) fires on the push, sees the new `## vX.Y.Z` heading in the CHANGELOG diff, and:
   - creates a lightweight tag at the merge commit (**no `git tag` step for the maintainer**), then
   - builds the VS Code extension `.vsix` and publishes a **GitHub Release** for that tag — the matching CHANGELOG section as the release notes, the built `.vsix` attached as a release asset.

So every release is both a tag *and* a GitHub Release with the installable `.vsix` downloadable from it. This mirrors the manual procedure in the sibling `grok-build-vscode` repo (`scripts/release.*`: tag + `gh release create` with the `.vsix` attached), adapted to this repo's CHANGELOG-driven, merge-to-`main` model — so it's automated rather than a local script. Marketplace publish (`vsce publish`) stays separate and explicit, exactly as there.

**The release step asserts `vscode-extension/package.json`'s version equals the CHANGELOG version and fails loudly if not** — the `.vsix` filename embeds the package version, so a mismatch would mislabel the asset. If you forget the bump, the tag is still created but the Release step fails; bump `package.json` and create the Release by hand (`gh release create vX.Y.Z --notes-file <section> vscode-extension/<name>-X.Y.Z.vsix`), since a same-commit re-push won't re-add the heading to re-trigger the workflow.

The workflow is idempotent: if the tag already exists (someone tagged manually before the workflow caught up) the tag step is a no-op, and if the Release already exists the release step is a no-op. It also no-ops entirely on pushes that don't add a new version heading (typo fixes, docs-only edits, etc.).

Existing tags `v1.0.0`, `v1.1.0`, `v1.1.1` are lightweight and were created by hand before the workflow existed. `v1.1.2` was the first tag created by the workflow. The workflow only *adds* missing tags; it never reconciles existing ones. Don't bother re-tagging the legacy ones.

### CHANGELOG conventions

The workflow trusts the CHANGELOG, so the format matters. Every new release entry on `DEV` follows this exact shape:

```
## vX.Y.Z — TBD

### <Area>

- One bullet per change, past tense, with a PR/issue link and `thanks @author` where the change came from a contributor (#73, thanks @thomasleveil)
```

Format rules the workflow relies on:

| Field | Required form | Why |
|---|---|---|
| Heading | `## vX.Y.Z` (exactly two `#`, the `v` prefix, three numeric components — strict semver) | The workflow regex `^## v[0-9]+\.[0-9]+\.[0-9]+([[:space:]]|$)` won't match anything else. `v1.1`, `v1.1.0-rc1`, `V1.1.0` are all silently ignored. |
| Separator | ` — ` (em-dash with surrounding spaces) | Cosmetic but consistent. The workflow ignores everything after the version. |
| Date | `TBD` while accumulating on `DEV`; replace with `YYYY-MM-DD` *at the moment of merging to `main`* | The workflow doesn't enforce dates — but a `TBD` heading that ships to main means the release looks unfinished forever. |
| Subsections | `### Dashboard`, `### Scanner`, `### Packaging`, `### Project / docs` — pick the smallest set that fits | Keeps the CHANGELOG scannable. |
| Bullets | Past tense, link the PR/issue with `#N`, credit external contributors with `thanks @login` | Lets readers (and future maintainers tracing history) find the source quickly. |

**The TBD → date rule is the only step a human must remember at release time.** If you forget, the workflow still tags correctly, but the CHANGELOG entry on main reads `## v1.1.3 — TBD` forever. Fix-up commit can correct it, but it'll feel sloppy.

Patch (`Z` increments) is the default for any release. Bump minor (`Y`) when a non-breaking user-visible feature lands (e.g. Today range button shipping alone would have been a minor in a different world). Bump major (`X`) only on breaking changes — there have been none and likely won't be soon. There's no automation around picking the right bump; the maintainer (or `/triage`) decides when writing the CHANGELOG heading on `DEV`.

### Homebrew formula and self-referential SHA

The Homebrew formula at `Formula/claude-usage.rb` lives inside this same repo. Be careful when bumping it: if the formula's `url` points at a tarball that **contains the formula itself with that sha256**, the sha256 is self-referential and uncomputable. Practical rule: a release's formula must point at the **previous** release's tarball, never its own. In v1.1.1 the formula points at v1.1.0's commit-SHA tarball, so brew users installing v1.1.1's formula receive v1.1.0 code — that's the trade-off of keeping the formula in-tree.

Now that the auto-tag workflow exists, formula bumps use the tag-tarball URL (`archive/refs/tags/vX.Y.Z.tar.gz`) instead of commit SHAs — stabler and shorter — as long as the tag-tarball pointed at is from the *previous* release.

**Automate the bump; never hand-edit the three pinned lines.** [`scripts/bump-formula.sh`](scripts/bump-formula.sh) fetches a released tag's tarball, computes its `sha256`, and rewrites the `url` / `version` / `sha256` lines (leaving `head`, `homepage`, and comments alone). With no argument it targets the latest `v*` tag on origin — run during release prep, before the new tag exists, that's the previous release, exactly what the self-referential rule requires.

**Why a `DEV`-only commit is enough — and why it's one release behind.** Brew reads the formula from the tap's default branch (`main`) HEAD, never from `DEV` or a tag. So the bump is a normal `DEV` commit that becomes visible to brew users only when `DEV → main` merges — which is precisely at the next release. That timing is the point: it lets us pin at the just-frozen *previous* tag and ship it with the release, so **brew always tracks one release behind, advancing automatically each release**, with no direct push to `main` (dodging `main`'s branch protection) and no hand-editing to forget. The manual, forget-prone bump is what let the pin silently rot at v1.1.0 from v1.1.1 through v1.5.0; v1.5.2 caught it up to v1.5.1 and wired in this routine. The only thing a `DEV`-only bump can't do is move brew *between* releases — you'd need a release (a `DEV → main` merge) for that, which is fine because the pin only ever changes at release boundaries anyway.

## Weekly triage routine

The repo has a self-contained slash command at [.claude/commands/triage.md](.claude/commands/triage.md) that automates the weekly PR/issue cleanup we used to ship v1.1.0: classify open items with Codex, merge no-brainers to DEV preserving authorship, run tests, close duplicates / scope-violations with friendly messages, bump CHANGELOG by patch, push DEV. **The routine never pushes to `main`** — release decisions stay with the maintainer.

Register the Windows Task Scheduler entry with [scripts/setup-weekly-triage.ps1](scripts/setup-weekly-triage.ps1). Logs go to `logs/triage-*.log`.

If you're working on this repo and want to invoke the routine ad-hoc, just type `/triage` in Claude Code. Hard safety rails (test-passing gates, no security-sensitive auto-merges, no scope-changing merges, Codex sign-off required on closures) live inside `triage.md`.
