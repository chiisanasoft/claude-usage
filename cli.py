"""
cli.py - Command-line interface for the Claude Code usage dashboard.

Commands:
  scan      - Scan JSONL files and update the database
  today     - Print today's usage summary
  stats     - Print all-time usage statistics
  dashboard - Scan + open browser + start dashboard server
"""

import os
import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime, date, timedelta

from scanner import VERSION

DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))

PRICING = {
    # Fable / Mythos — Anthropic's most capable class, priced at 2x Opus.
    # (Mythos 5 shares Fable 5's pricing; Project-Glasswing access only.)
    "claude-fable-5":    {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    "claude-mythos-5":   {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    # Sources: https://platform.claude.com/docs/en/about-claude/pricing
    # cache_write is the 5-minute TTL column throughout, matching every other
    # entry here; the transcripts record only a token count, never which TTL.
    "claude-opus-5":     {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-8":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-7":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    # Sonnet 5 is cheaper than Sonnet 4.x, so the substring fallback below would
    # overcharge it by 50%. It needs an explicit entry, not a family guess.
    "claude-sonnet-5":   {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-sonnet-4-7": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-7":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-6":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
}

# Every model name that reached get_pricing() without a price. A miss is not an
# error — local and third-party models legitimately have no Anthropic price —
# but it silently removes turns from every total, so it must be reported rather
# than absorbed. See warn_unknown_models().
UNKNOWN_MODELS = set()


def get_pricing(model):
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if model.startswith(key):
            return PRICING[key]
    # Substring fallback: match model family by keyword
    m = model.lower()
    if "fable" in m or "mythos" in m:
        return PRICING["claude-fable-5"]
    if "opus" in m:
        return PRICING["claude-opus-4-8"]
    if "sonnet" in m:
        return PRICING["claude-sonnet-4-6"]
    if "haiku" in m:
        return PRICING["claude-haiku-4-5"]
    UNKNOWN_MODELS.add(model)
    return None

def calc_cost(model, inp, out, cache_read, cache_creation):
    p = get_pricing(model)
    if not p:
        return 0.0
    return (
        inp            * p["input"]       / 1_000_000 +
        out            * p["output"]      / 1_000_000 +
        cache_read     * p["cache_read"]  / 1_000_000 +
        cache_creation * p["cache_write"] / 1_000_000
    )

def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def fmt_cost(c):
    return f"${c:.4f}"

def fmt_model_cost(model, c):
    """Cost cell for a per-model row.

    "$0.0000" and "no price for this model" are different facts, and printing
    both as zero is what lets an unpriced model disappear from a total without
    anyone noticing. Unpriced rows read "n/a"; warn_unknown_models() then says
    the total is short.
    """
    return "n/a" if get_pricing(model) is None else fmt_cost(c)

def warn_unknown_models(stream=sys.stderr):
    """Report models that had no price, once, after the numbers are printed.

    Returns True if anything was reported, so callers can test it.
    """
    if not UNKNOWN_MODELS:
        return False
    print(f"\nwarning: no price for {', '.join(sorted(UNKNOWN_MODELS))}",
          file=stream)
    print("         Those turns are counted as $0, so every total above is "
          "an undercount.", file=stream)
    print("         Add the model to PRICING in cli.py (and dashboard.py) if "
          "it is an Anthropic model.", file=stream)
    return True

def hr(char="-", width=60):
    print(char * width)

def require_db():
    if not DB_PATH.exists():
        print("Database not found. Run: python cli.py scan")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Ensure the schema is current before querying. The read commands query the
    # `agents` table and the `is_subagent`/`agent_id` columns, so a pre-existing
    # DB from before those were added would raise "no such column" when a read
    # command runs before the next scan migrates it. init_db is idempotent
    # (CREATE ... IF NOT EXISTS + additive column checks), so this is a cheap
    # no-op once migrated. Mirrors get_dashboard_data in dashboard.py.
    from scanner import init_db
    init_db(conn)
    return conn


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan(projects_dir=None):
    from scanner import scan
    scan(projects_dir=Path(projects_dir) if projects_dir else None)


def cmd_today():
    conn = require_db()
    today = date.today().isoformat()

    rows = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (today,)).fetchall()

    sessions = conn.execute("""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
    """, (today,)).fetchone()

    subagent = conn.execute("""
        SELECT
            COUNT(*) as turns,
            SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) as tokens
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
          AND COALESCE(is_subagent, 0) = 1
    """, (today,)).fetchone()

    print()
    hr()
    print(f"  Today's Usage  ({today})")
    hr()

    if not rows:
        print("  No usage recorded today.")
        print()
        return

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0

    for r in rows:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        total_cost += cost
        total_inp += r["inp"] or 0
        total_out += r["out"] or 0
        total_cr  += r["cr"]  or 0
        total_cc  += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"  {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_model_cost(r['model'], cost)}")

    hr()
    print(f"  {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions today:   {sessions['cnt']}")
    print(f"  Subagent tokens:  {fmt(subagent['tokens'] or 0)}  ({fmt(subagent['turns'] or 0)} turns)")
    print(f"  Cache read:       {fmt(total_cr)}")
    print(f"  Cache creation:   {fmt(total_cc)}")
    hr()
    print()
    conn.close()


def cmd_week():
    conn = require_db()

    today_d = date.today()
    start_d = today_d - timedelta(days=6)
    start = start_d.isoformat()
    end = today_d.isoformat()

    by_day_model = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
        GROUP BY day, model
    """, (start, end)).fetchall()

    by_model = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (start, end)).fetchall()

    sessions = conn.execute("""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
    """, (start, end)).fetchone()

    print()
    hr()
    print(f"  Weekly Usage  ({start} to {end})")
    hr()

    if not by_model:
        print("  No usage recorded in the last 7 days.")
        print()
        conn.close()
        return

    # Aggregate per-day across models (with per-turn cost attribution)
    per_day = {}
    for r in by_day_model:
        d = r["day"]
        bucket = per_day.setdefault(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        bucket["turns"] += r["turns"]
        bucket["inp"]   += r["inp"] or 0
        bucket["out"]   += r["out"] or 0
        bucket["cost"]  += calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)

    print("  By Day:")
    for i in range(7):
        d = (start_d + timedelta(days=i)).isoformat()
        b = per_day.get(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        print(f"    {d}  turns={b['turns']:<4}  in={fmt(b['inp']):<8}  out={fmt(b['out']):<8}  cost={fmt_cost(b['cost'])}")

    hr()
    print("  By Model:")

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        total_cost  += cost
        total_inp   += r["inp"] or 0
        total_out   += r["out"] or 0
        total_cr    += r["cr"]  or 0
        total_cc    += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"    {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_model_cost(r['model'], cost)}")

    hr()
    print(f"    {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions this week:  {sessions['cnt']}")
    print(f"  Cache read:          {fmt(total_cr)}")
    print(f"  Cache creation:      {fmt(total_cc)}")
    hr()
    print()
    conn.close()


def cmd_stats():
    conn = require_db()

    # Session-level info (count, date range)
    session_info = conn.execute("""
        SELECT
            COUNT(*)                  as sessions,
            MIN(first_timestamp)      as first,
            MAX(last_timestamp)       as last
        FROM sessions
    """).fetchone()

    # All-time totals from turns (more accurate — per-turn model attribution)
    totals = conn.execute("""
        SELECT
            SUM(input_tokens)             as inp,
            SUM(output_tokens)            as out,
            SUM(cache_read_tokens)        as cr,
            SUM(cache_creation_tokens)    as cc,
            COUNT(*)                      as turns
        FROM turns
    """).fetchone()

    # By model from turns (each turn has the actual model used)
    by_model = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns,
            COUNT(DISTINCT session_id) as sessions
        FROM turns
        GROUP BY model
        ORDER BY inp + out DESC
    """).fetchall()

    # Top 5 projects from turns (join with sessions for project name)
    top_projects = conn.execute("""
        SELECT
            COALESCE(s.project_name, 'unknown') as project_name,
            SUM(t.input_tokens)  as inp,
            SUM(t.output_tokens) as out,
            COUNT(*)             as turns,
            COUNT(DISTINCT t.session_id) as sessions
        FROM turns t
        LEFT JOIN sessions s ON t.session_id = s.session_id
        GROUP BY s.project_name
        ORDER BY inp + out DESC
        LIMIT 5
    """).fetchall()

    # Subagent totals (subagent tokens are included in the all-time totals above)
    subagent = conn.execute("""
        SELECT
            COUNT(*) as turns,
            SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) as tokens
        FROM turns
        WHERE COALESCE(is_subagent, 0) = 1
    """).fetchone()

    # Daily average (last 30 days)
    daily_avg = conn.execute("""
        SELECT
            AVG(daily_inp) as avg_inp,
            AVG(daily_out) as avg_out
        FROM (
            SELECT
                substr(timestamp, 1, 10) as day,
                SUM(input_tokens) as daily_inp,
                SUM(output_tokens) as daily_out
            FROM turns
            WHERE timestamp >= datetime('now', '-30 days')
            GROUP BY day
        )
    """).fetchone()

    # Build total cost across all models
    total_cost = sum(
        calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        for r in by_model
    )

    print()
    hr("=")
    print("  Claude Code Usage - All-Time Statistics")
    hr("=")

    first_date = (session_info["first"] or "")[:10]
    last_date = (session_info["last"] or "")[:10]
    print(f"  Period:           {first_date} to {last_date}")
    print(f"  Total sessions:   {session_info['sessions'] or 0:,}")
    print(f"  Total turns:      {fmt(totals['turns'] or 0)}")
    print(f"  Subagent turns:   {fmt(subagent['turns'] or 0)}")
    print()
    print(f"  Input tokens:     {fmt(totals['inp'] or 0):<12}  (raw prompt tokens)")
    print(f"  Output tokens:    {fmt(totals['out'] or 0):<12}  (generated tokens)")
    print(f"  Cache read:       {fmt(totals['cr'] or 0):<12}  (90% cheaper than input)")
    print(f"  Cache creation:   {fmt(totals['cc'] or 0):<12}  (25% premium on input)")
    print(f"  Subagent tokens:  {fmt(subagent['tokens'] or 0):<12}  (included in totals)")
    print()
    print(f"  Est. total cost:  ${total_cost:.4f}")
    hr()

    print("  By Model:")
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        print(f"    {r['model']:<30}  sessions={r['sessions']:<4}  turns={fmt(r['turns'] or 0):<6}  "
              f"in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_model_cost(r['model'], cost)}")

    hr()
    print("  Top Projects:")
    for r in top_projects:
        print(f"    {(r['project_name'] or 'unknown'):<40}  sessions={r['sessions']:<3}  "
              f"turns={fmt(r['turns'] or 0):<6}  tokens={fmt((r['inp'] or 0)+(r['out'] or 0))}")

    if daily_avg["avg_inp"]:
        hr()
        print("  Daily Average (last 30 days):")
        print(f"    Input:   {fmt(int(daily_avg['avg_inp'] or 0))}")
        print(f"    Output:  {fmt(int(daily_avg['avg_out'] or 0))}")

    hr("=")
    print()
    conn.close()


def _supports_color():
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _bar(pct, width=24):
    """Render a [████░░░░] bar. pct None -> empty/uncalibrated bar."""
    if pct is None:
        return "[" + ("." * width) + "]"
    frac = max(0.0, min(1.0, pct / 100.0))
    filled = int(round(frac * width))
    bar = "#" * filled + "." * (width - filled)
    if _supports_color():
        color = "\033[32m"  # green
        if pct >= 90:
            color = "\033[31m"   # red
        elif pct >= 70:
            color = "\033[33m"   # yellow
        return "[" + color + bar + "\033[0m" + "]"
    return "[" + bar + "]"


def _render_limit_block(b):
    pct = b["pct"]
    pct_str = f"{pct:>3.0f}%" if pct is not None else " n/a"
    line = f"  {b['label']:<26} {_bar(pct)} {pct_str}"
    if b["resets_in"]:
        line += f"   resets in {b['resets_in']}"
    sev = (b.get("severity") or "").lower()
    if sev and sev != "normal":
        line += f"   [{sev}]"
    print(line)
    # A window whose turns all ran on unpriced models (local/3rd-party, or a
    # model family not in PRICING) has an unknown cost, not a zero one — saying
    # "$0.00" next to 2000 turns reads as either a bug or as "this was free".
    used = "$%.2f" % b["consumption_usd"] if b.get("cost_known", True) else "n/a"
    detail = f"      {used} used  ·  {b['turns']} turns"
    if b["source"] == "uncalibrated":
        detail += "   (uncalibrated)"
    elif b["source"] == "calibrated" and b["cap_usd"]:
        detail += f"   (cap ~${b['cap_usd']:.2f}, calibrated)"
    elif b["source"] == "api":
        detail += "   (live from API)"
    print(detail)


def cmd_limits(args=None):
    import limits
    args = args or []

    # Dump the raw read-only usage-API response (no token is printed). Useful for
    # confirming the endpoint works and for refining the parser to the live shape.
    if "--debug-api" in args:
        diag = limits.debug_api_probe()
        print("=== API probe diagnostics (no token is printed) ===")
        print(json.dumps(diag, indent=2, ensure_ascii=False))
        if diag.get("http_status") == 200 and diag.get("body"):
            try:
                raw = json.loads(diag["body"])
                print("\nParsed by current parser:")
                print(json.dumps(limits.parse_api_usage(raw), indent=2, ensure_ascii=False))
            except Exception as e:
                print(f"\n(could not parse body as JSON: {e})")
        return

    # Calibration flags: --calibrate-<window> <observed_pct>
    calib_map = {
        "--calibrate-session": "session",
        "--calibrate-weekly":  "weekly_all",
        "--calibrate-opus":    "weekly_opus",
        "--calibrate-sonnet":  "weekly_sonnet",
    }
    for flag, window in calib_map.items():
        val = parse_named_arg(args, flag)
        if val is not None:
            try:
                cap, cost = limits.calibrate(window, float(val))
                print(f"Calibrated {window}: observed {val}% at ${cost:.2f} used "
                      f"-> cap ~${cap:.2f}")
            except Exception as e:
                print(f"Calibration failed: {e}")
                sys.exit(1)

    # Weekly reset anchor: --weekly-reset <DOW> <HOUR>  (DOW 0=Mon..6=Sun, local hour)
    if "--weekly-reset" in args:
        i = args.index("--weekly-reset")
        try:
            dow = int(args[i + 1]); hour = int(args[i + 2])
            cfg = limits.load_config()
            cfg["weekly_all"]["reset_dow"] = dow % 7
            cfg["weekly_all"]["reset_hour"] = hour % 24
            limits.save_config(cfg)
            dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            print(f"Weekly reset anchor set to {dows[dow % 7]} {hour % 24:02d}:00 (local)")
        except (IndexError, ValueError):
            print("Usage: --weekly-reset <DOW 0=Mon..6=Sun> <HOUR 0-23>")
            sys.exit(1)

    # Plan label override: --set-plan "Max (20x)"  (--set-plan auto to clear).
    # The keychain credential reports the tier from when the token was issued,
    # so it lags a plan upgrade until the token is reissued.
    if "--set-plan" in args:
        i = args.index("--set-plan")
        try:
            label = args[i + 1]
        except IndexError:
            print('Usage: --set-plan "Max (20x)"   (or: --set-plan auto)')
            sys.exit(1)
        cfg = limits.load_config()
        if label.lower() in ("auto", "clear", "none"):
            cfg["plan_override"] = None
            limits.save_config(cfg)
            print(f"Plan override cleared (detected: {cfg.get('plan') or 'unknown'})")
        else:
            cfg["plan_override"] = label
            limits.save_config(cfg)
            print(f"Plan set to {label}")

    # --no-api still works and still wins: it was the documented way to force
    # the local-only path, and a flag that silences a network call must never
    # stop being honoured.
    if "--no-api" in args:
        use_api = False
    elif "--use-api" in args:
        use_api = True
    else:
        use_api = None      # fall through to the config (default: off)
    data = limits.compute(use_api=use_api, scan_first="--no-scan" not in args)

    if "--json" in args:
        print(json.dumps(data, indent=2))
        return

    if "error" in data:
        print(data["error"])
        sys.exit(1)

    print()
    hr("=")
    plan = data["plan"] or "unknown plan"
    src = "API ✓" if data["api_ok"] else "local estimate"
    print(f"  Claude Usage Limits   Plan: {plan}   [{src}]")
    hr("=")
    _render_limit_block(data["session"])
    print()
    _render_limit_block(data["weekly_all"])
    if data.get("weekly_opus"):
        _render_limit_block(data["weekly_opus"])
    if data.get("weekly_sonnet"):
        _render_limit_block(data["weekly_sonnet"])
    # Scoped (per-model / per-surface) limits the API reports generically. These
    # are real limits that can be near their cap, so they must never be hidden.
    for block in data.get("weekly_scoped") or []:
        _render_limit_block(block)
    hr()
    if not data["api_ok"]:
        any_uncal = any(data[w]["source"] == "uncalibrated"
                        for w in ("session", "weekly_all"))
        if any_uncal:
            print("  Tip: calibrate against the desktop app's Usage screen, e.g.:")
            print("       claude-usage limits --calibrate-session 20 --calibrate-weekly 40")
    print(f"  Updated: {data['generated_at']}")
    hr("=")
    print()


def cmd_dashboard(projects_dir=None, host=None, port=None, no_browser=False, surface=None):
    import threading
    import time

    from dashboard import serve

    host = host or os.environ.get("HOST", "localhost")
    port = int(port or os.environ.get("PORT", "8080"))

    # Bind and serve the port *first*, then scan in the background. A cold scan
    # over a large ~/.claude/projects backlog can take well over a minute, and
    # the VS Code extension kills the process if it doesn't answer /api/data
    # within ~10s (see vscode-extension/src/server-manager.ts). Serving up front
    # means the port is live immediately; the dashboard shows whatever's already
    # in the DB and auto-refreshes as the background scan commits new data.
    #
    # Capture cmd_scan into a local so the background thread closes over the
    # current binding — keeps the test suite's mock.patch(cli.cmd_scan) effective
    # and prevents the thread from ever touching the real DB after a patch lifts.
    scan = cmd_scan

    def background_scan():
        print("Scanning in the background...")
        scan(projects_dir=projects_dir)
        print("Background scan complete.")

    threading.Thread(target=background_scan, daemon=True).start()

    # Open a browser for users running this as a script (see README). The VS Code
    # extension passes --no-browser since it embeds the dashboard in a webview.
    if not no_browser:
        import webbrowser

        def open_browser():
            time.sleep(1.0)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=open_browser, daemon=True).start()

    serve(host=host, port=port, surface=surface)


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
Claude Code Usage Dashboard

Usage:
  python cli.py scan [--projects-dir PATH]   Scan JSONL files and update database
  python cli.py today                        Show today's usage summary
  python cli.py week                         Show last 7 days (per-day + by-model)
  python cli.py stats                        Show all-time statistics
  python cli.py limits [--use-api] [--json] [--no-scan]  Session (5h) + weekly limits
                       [--calibrate-session N] [--calibrate-weekly N]
                       [--calibrate-opus N] [--calibrate-sonnet N]
                       [--weekly-reset DOW HOUR]
  python cli.py dashboard [--projects-dir PATH] [--host HOST] [--port PORT] [--no-browser] [--surface SURFACE]
                                                 Scan + start dashboard (opens a browser unless --no-browser)
  python cli.py --version                    Print the version and exit
"""

COMMANDS = {
    "scan": cmd_scan,
    "today": cmd_today,
    "week": cmd_week,
    "stats": cmd_stats,
    "limits": cmd_limits,
    "dashboard": cmd_dashboard,
}

def parse_named_arg(args, flag):
    """Extract a --flag VALUE pair from an argument list."""
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
    return None

def main():
    """Console entry point (``claude-usage``) and ``python cli.py`` dispatch."""
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V", "version"):
        print(VERSION)
        sys.exit(0)

    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(USAGE)
        sys.exit(0)

    command = sys.argv[1]
    rest = sys.argv[2:]
    projects_dir = parse_named_arg(rest, "--projects-dir")

    if command == "dashboard":
        cmd_dashboard(
            projects_dir=projects_dir,
            host=parse_named_arg(rest, "--host"),
            port=parse_named_arg(rest, "--port"),
            no_browser="--no-browser" in rest,
            surface=parse_named_arg(rest, "--surface"),
        )
    elif command == "scan" and projects_dir:
        cmd_scan(projects_dir=projects_dir)
    elif command == "limits":
        cmd_limits(rest)
    else:
        COMMANDS[command]()

    # After the report, never instead of it: a warning printed first scrolls
    # away, and the numbers it qualifies are what the reader is looking at.
    warn_unknown_models()


if __name__ == "__main__":
    main()
