# Changelog

## Unreleased

### Plan limits

- Add `limits.py` and a `python cli.py limits` command: Claude-desktop-style indicators for the current 5-hour session window and the weekly window (all models, plus Opus / Sonnet sub-limits when the plan has them), with consumption in API-equivalent dollars, turn counts and reset countdowns
- Read the exact percentages and reset times from the read-only `/api/oauth/usage` endpoint using the OAuth token Claude Code already stored, and auto-calibrate the local caps from them so estimates stay accurate when the API is unavailable
- Add manual calibration against the desktop Usage screen (`--calibrate-session/-weekly/-opus/-sonnet N`) and a fixed weekly reset anchor (`--weekly-reset DOW HOUR`), persisted in `~/.claude/claude-usage-limits.json`
- Add `--no-api`, `--no-scan`, `--json` and a token-free `--debug-api` diagnostic to the `limits` command
- Add a `GET /api/limits` endpoint and a "Plan Limits" bar card at the top of the dashboard, refreshed every 30 seconds
- Reuse `cli.calc_cost` for limit consumption so pricing stays single-sourced
- Fix the Plan Limits card showing nothing in the Docker container: the named volume shadowed the host's `~/.claude`, so the calibrated caps and plan label never reached it. Both settings files are now bind-mounted read-only
- Stop serving a cached API snapshot as if it were live once it is older than `API_SNAPSHOT_MAX_AGE_SECONDS`; a container that cannot refresh it now falls through to the calibrated tier instead of freezing at the host's last reading

## v1.1.0 — 2026-05-28

### Dashboard

- Fix `ReferenceError: cutoff is not defined` in the hourly filter that blanked the entire dashboard (#73, thanks @thomasleveil)
- Fix hourly chart ignoring the range upper bound for `week` / `month` / `prev-month` ranges
- Fix 404 on dashboard URLs containing query strings (`?range=...&models=...`) so reloads and bookmarks work, with regression tests (#81, thanks @jakduch)
- Fix blank dashboard for users whose data only contains non-billable / unknown models, or rows with empty model names: `COALESCE(NULLIF(model, ''), 'unknown')` normalises empty-string models (in both SELECT and GROUP BY so mixed NULL + '' rows collapse to a single "unknown" group), and the default selection falls back to all models when no billable models exist (#109, thanks @HaydenHaines)
- Use `ThreadingHTTPServer` so a slow `/api/data` no longer blocks other dashboard requests (#79, thanks @jakduch)
- Add a `Today` range button (#112, thanks @Fruhji)

### Scanner

- Fix incremental scan not updating `first_timestamp` when a newly discovered session's records arrive out of order (#111, thanks @Fruhji)

### Project / docs

- Adopt `AGENTS.md` as the canonical agent guide (shared with Codex); `CLAUDE.md` is now a thin `@AGENTS.md` import
- Codify the rule that future PR merges must preserve original-author commits (`merge --no-ff` for full merges, `cherry-pick` for partial, `Co-Authored-By` trailers when applying hunks manually)
- Drop unused `.claude/launch.json`

## v1.0.0 — 2026-04-09

- Fix token counts inflated ~2x by deduplicating streaming events that share the same message ID
- Fix session cost totals that were inflated when sessions spanned multiple JSONL files
- Fix pricing to match current Anthropic API rates (Opus $5/$25, Sonnet $3/$15, Haiku $1/$5)
- Add CI test suite (84 tests) and GitHub Actions workflow running on every PR
- Add sortable columns to Sessions, Cost by Model, and new Cost by Project tables
- Add CSV export for Sessions and Projects (all filtered data, not just top 20)
- Add Rescan button to dashboard for full database rebuild
- Add Xcode project directory support and `--projects-dir` CLI option
- Non-Anthropic models (gemma, glm, etc.) no longer incorrectly charged at Sonnet rates
- CLI and dashboard now both compute costs per-turn for consistent results
