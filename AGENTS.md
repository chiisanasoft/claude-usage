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
python cli.py limits --no-api --json      # local-only payload, machine readable
python cli.py limits --debug-api          # verbose (token-free) API diagnostics
python cli.py dashboard             # scan + open http://localhost:8080
python cli.py scan --projects-dir PATH    # scan a custom transcripts dir
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

5. **`GET https://api.anthropic.com/api/oauth/usage` needs all three headers** — `Authorization: Bearer <token>`, `anthropic-beta: oauth-2025-04-20`, and `anthropic-version: 2023-06-01` (`_api_headers`). Dropping any one of them fails the request. The endpoint is read-only: it runs no inference, costs nothing, and does not itself consume the rate limit, so it is safe to poll (the dashboard hits it every 30s). A `null` `seven_day_opus` in the response means the account has **no separate Opus weekly cap** — the corresponding UI row must be hidden, not rendered at 0% (`include_opus` in `compute`).

### Cost calculation

Costs are computed **per turn** (each turn knows its own model), then summed. This is true in both the CLI ([cli.py](cli.py) `calc_cost`) and the dashboard JS ([dashboard.py](dashboard.py) `calcCost` inside the embedded HTML). Aggregating tokens first and applying a single price is wrong for sessions that span multiple models.

Pricing is duplicated in two places that **must stay in sync**:
- [cli.py](cli.py) `PRICING` dict (Python)
- [dashboard.py](dashboard.py) `PRICING` const inside `HTML_TEMPLATE` (JavaScript)

`get_pricing` / `getPricing` resolve in three tiers: exact match → `startswith` (handles date-suffixed model IDs like `claude-opus-4-7-20260215`) → substring fallback on `opus` / `sonnet` / `haiku`. Models that don't match any tier return `None` and are billed at $0 (shown as `n/a`) — this is intentional so local/3rd-party models (gemma, glm, etc.) aren't charged at Sonnet rates.

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

### Container deployment

A container deployment of the dashboard exists: [Dockerfile](Dockerfile), [docker-compose.yml](docker-compose.yml), documented in [docs/DOCKER.md](docs/DOCKER.md). It is packaging only — no application code is involved, and it must stay that way. Constraints worth knowing before you change anything it depends on:

- **No path configuration exists.** `DB_PATH` is a module-level `Path.home() / ".claude" / "usage.db"` in `scanner.py` / `cli.py` / `dashboard.py` / `limits.py`, with no env var or flag (only `scan --projects-dir` is overridable, and `cmd_dashboard` doesn't take a projects dir on the server side). The container therefore configures paths purely by setting `HOME=/home/appuser` and bind-mounting onto the paths the code already hardcodes. If you ever add a `CLAUDE_USAGE_DB` env var, update the compose file to use it.
- **`HOST` / `PORT` env vars are the only network knobs** (`serve()` in [dashboard.py](dashboard.py), `cmd_dashboard` in [cli.py](cli.py)). The image sets `HOST=0.0.0.0` — mandatory inside a container — and compose publishes to `127.0.0.1:8080` so the unauthenticated UI is not exposed to the LAN. Don't change the default `localhost` bind in the app; the container overrides it deliberately.
- **Transcripts are mounted read-only**; the only writable mount is a named volume at `/home/appuser/.claude` holding the derived `usage.db` and `claude-usage-limits.json`. The scanner must keep treating the projects dirs as strictly read-only.
- **The live limits API cannot work there.** `read_oauth_credential()` shells out to the macOS `security` binary; in a Linux container that raises and is swallowed, so `/api/limits` degrades to calibrated/uncalibrated. That graceful degradation (exception-swallowing in `read_oauth_credential` / `fetch_api_usage`) is load-bearing for the container — keep it. Do not add any code that extracts, forwards or persists the OAuth token for container use.
- The CMD is `python cli.py dashboard`, which scans then serves; its `webbrowser.open` call is a harmless no-op in the container.

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
