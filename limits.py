"""
limits.py - Session (5-hour) and weekly usage-limit indicators.

Reconstructs Claude's account-level rate-limit windows from the local `turns`
table and presents them like the Claude desktop "Usage" screen: a rolling
5-hour *session* window and a *weekly* window (all-models, plus per-model
sub-limits for Opus / Sonnet).

The percentage shown by the desktop app is measured against plan caps that
Anthropic does not publish numerically and that are NOT present in the local
logs. So this module needs a *denominator* from one of three sources, in order
of preference:

  1. API      - the same authenticated `/api/oauth/usage` endpoint the desktop
                app and `claude /usage` use (read-only, no token cost). Gives
                the exact percentages and reset times, and is used to
                auto-calibrate the local caps so the estimate stays good even
                when the API is unavailable.
  2. Calibrated - the user told us "the desktop shows N%" once; we back out the
                cap from the current consumption (cap = consumption / (N/100)).
  3. Uncalibrated - we can still show the raw consumption ($-equivalent) and the
                reset countdown, but the bar is marked "uncalibrated".

Consumption is measured as the API-equivalent cost (USD) of each turn, reusing
cli.calc_cost. Anthropic's subscription limits behave roughly like an
API-dollar budget (cache reads barely count, Opus output counts a lot), so cost
is a good, self-consistent proxy for "how much of the limit was consumed".

Stdlib only. Python 3.8+.
"""

import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Reuse the single source of truth for pricing (AGENTS.md: don't add a 3rd copy).
from cli import calc_cost

DB_PATH = Path.home() / ".claude" / "usage.db"
CONFIG_PATH = Path.home() / ".claude" / "claude-usage-limits.json"

SESSION_HOURS = 5
WEEK_DAYS = 7

# OAuth usage endpoint (same one Claude Code's `/usage` uses). Read-only.
API_URL = "https://api.anthropic.com/api/oauth/usage"
API_BETA = "oauth-2025-04-20"
API_VERSION = "2023-06-01"
KEYCHAIN_SERVICE = "Claude Code-credentials"


def _api_headers(token):
    # OAuth-token requests need the bearer + version + the oauth beta header.
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": API_BETA,
        "anthropic-version": API_VERSION,
        "User-Agent": "claude-usage-limits/1.0",
    }

# How long an API snapshot is considered "fresh" for auto-calibration display.
API_FRESH_SECONDS = 120


# ── Config ──────────────────────────────────────────────────────────────────

def default_config():
    return {
        "plan": None,                 # e.g. "Max (5x)" - filled from API when known
        "use_api": True,              # try the read-only usage API
        "session": {"cap_usd": None, "calibrated_at": None},
        "weekly_all": {
            "cap_usd": None,
            "calibrated_at": None,
            "reset_dow": None,        # 0=Mon .. 6=Sun, in LOCAL time (None = rolling 7d)
            "reset_hour": None,       # local hour of weekly reset
        },
        "weekly_opus":   {"cap_usd": None, "calibrated_at": None},
        "weekly_sonnet": {"cap_usd": None, "calibrated_at": None},
    }


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    cfg = default_config()
    if CONFIG_PATH.exists():
        try:
            cfg = _deep_merge(cfg, json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ── Time helpers ────────────────────────────────────────────────────────────

def _now_utc():
    return datetime.now(timezone.utc)


def _iso_z(dt):
    """UTC datetime -> '...T..:..:..000Z' so lexicographic compare == chronological."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_z(ts):
    """'2026-06-27T02:31:44.841Z' -> aware UTC datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _floor_hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def _fmt_duration(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


# ── Consumption from the local DB ───────────────────────────────────────────

def _window_by_model(conn, start_iso, end_iso=None, model_like=None):
    q = """
        SELECT COALESCE(NULLIF(model,''),'unknown') AS model,
               SUM(input_tokens)          AS inp,
               SUM(output_tokens)         AS out,
               SUM(cache_read_tokens)     AS cr,
               SUM(cache_creation_tokens) AS cc,
               COUNT(*)                   AS turns
        FROM turns
        WHERE timestamp >= ?
    """
    params = [start_iso]
    if end_iso:
        q += " AND timestamp < ?"
        params.append(end_iso)
    if model_like:
        q += " AND lower(model) LIKE ?"
        params.append(f"%{model_like}%")
    q += " GROUP BY 1 ORDER BY out DESC"

    by_model = []
    total_cost = 0.0
    total_turns = 0
    for r in conn.execute(q, params).fetchall():
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        total_cost += cost
        total_turns += r["turns"] or 0
        by_model.append({
            "model": r["model"],
            "turns": r["turns"] or 0,
            "input": r["inp"] or 0,
            "output": r["out"] or 0,
            "cache_read": r["cr"] or 0,
            "cache_creation": r["cc"] or 0,
            "cost": cost,
        })
    return total_cost, total_turns, by_model


def _current_block_start(conn, now, lookback_hours=12):
    """Start (floored to the hour) of the 5-hour session window containing the
    latest activity, or None if there has been no activity for >5h."""
    rows = conn.execute(
        "SELECT timestamp FROM turns WHERE timestamp >= ? ORDER BY timestamp",
        (_iso_z(now - timedelta(hours=lookback_hours)),),
    ).fetchall()
    block_start = None
    for (ts,) in rows:
        try:
            dt = _parse_z(ts)
        except Exception:
            continue
        if block_start is None or dt >= block_start + timedelta(hours=SESSION_HOURS):
            block_start = _floor_hour(dt)
    if block_start is None:
        return None
    # If the latest activity's window has already elapsed, there is no active window.
    if now >= block_start + timedelta(hours=SESSION_HOURS):
        return None
    return block_start


def _local_weekly_start(now, reset_dow, reset_hour):
    """Return (start_utc, reset_at_utc). If reset_dow is None -> rolling 7d,
    reset_at is None (no fixed reset)."""
    if reset_dow is None or reset_hour is None:
        return now - timedelta(days=WEEK_DAYS), None
    # Anchor is expressed in the user's local timezone.
    local_now = now.astimezone()
    # Most recent local datetime at the given weekday+hour, at or before now.
    anchor = local_now.replace(hour=int(reset_hour), minute=0, second=0, microsecond=0)
    delta_days = (local_now.weekday() - int(reset_dow)) % 7
    anchor = anchor - timedelta(days=delta_days)
    if anchor > local_now:
        anchor -= timedelta(days=WEEK_DAYS)
    start = anchor.astimezone(timezone.utc)
    return start, start + timedelta(days=WEEK_DAYS)


# ── API (read-only, no token cost) ──────────────────────────────────────────

def _claude_oauth(cred):
    """The Claude.ai OAuth sub-object of the keychain credential, or None.
    (The credential also holds unrelated `mcpOAuth.*` entries whose own
    `accessToken` fields are empty — read only this documented location.)"""
    if isinstance(cred, dict):
        node = cred.get("claudeAiOauth")
        if isinstance(node, dict):
            return node
    return None


def _find_token(cred):
    """Read the Claude.ai OAuth access token from its standard location."""
    node = _claude_oauth(cred)
    tok = node.get("accessToken") if node else None
    return tok if isinstance(tok, str) and tok else None


def _find_key(obj, target):
    """Recursively find the first value whose key == target (case-insensitive)."""
    tl = target.lower()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() == tl:
                return v
        for v in obj.values():
            found = _find_key(v, target)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, target)
            if found is not None:
                return found
    return None


def read_oauth_credential():
    """Read the Claude Code OAuth credential JSON from the macOS keychain.
    Returns the parsed dict, or None if unavailable. The caller is responsible
    for never logging the access/refresh tokens."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout.strip())
    except Exception:
        return None


def fetch_api_usage(timeout=10):
    """Call the read-only usage endpoint. Returns the raw parsed JSON dict on
    success, or None. No model inference is performed, so this costs nothing and
    does not consume the rate limit."""
    cred = read_oauth_credential()
    if not cred:
        return None
    token = _find_token(cred)
    if not token:
        return None
    import urllib.request
    req = urllib.request.Request(API_URL, headers=_api_headers(token), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def debug_api_probe(timeout=15):
    """Verbose, non-secret diagnostics for why the usage API does or doesn't
    work. Never returns or prints the token itself — only its length and
    expiry. Used by `claude-usage limits --debug-api`."""
    import time
    import urllib.request
    import urllib.error

    diag = {"url": API_URL, "headers_sent": ["Authorization: Bearer <hidden>",
            f"anthropic-beta: {API_BETA}", f"anthropic-version: {API_VERSION}"]}
    cred = read_oauth_credential()
    diag["credential_found"] = bool(cred)
    if not cred:
        diag["hint"] = "No credential in keychain — is Claude Code logged in?"
        return diag

    node = _claude_oauth(cred) or {}
    diag["subscriptionType"] = node.get("subscriptionType")
    diag["rateLimitTier"] = node.get("rateLimitTier")
    diag["scopes"] = node.get("scopes")
    token = _find_token(cred)
    diag["token_present"] = bool(token)
    diag["token_len"] = len(token) if token else 0
    exp = node.get("expiresAt")
    diag["expiresAt_raw"] = exp
    if exp is not None:
        try:
            exp_ms = float(exp)
            # expiresAt is epoch milliseconds in Claude Code credentials.
            now_ms = time.time() * 1000
            diag["expired"] = exp_ms < now_ms
            diag["expires_in_min"] = round((exp_ms - now_ms) / 60000, 1)
        except Exception as e:
            diag["expiry_parse_error"] = f"{type(e).__name__}: {e}"

    if not token:
        diag["hint"] = "Credential has no accessToken field."
        return diag

    req = urllib.request.Request(API_URL, headers=_api_headers(token), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            diag["http_status"] = resp.status
            diag["body"] = body[:2000]
    except urllib.error.HTTPError as e:
        diag["http_status"] = e.code
        try:
            diag["body"] = e.read().decode("utf-8", "replace")[:2000]
        except Exception:
            diag["body"] = "(no body)"
        if e.code == 401:
            diag["hint"] = ("401 Unauthorized — token rejected. If 'expired' is "
                            "true above, the keychain copy is stale; open Claude "
                            "Code (or run `claude`) to refresh, then retry.")
        elif e.code == 403:
            diag["hint"] = "403 Forbidden — token valid but lacks scope for this endpoint."
        elif e.code == 404:
            diag["hint"] = "404 — endpoint path/method may differ from /api/oauth/usage."
    except Exception as e:
        diag["error"] = f"{type(e).__name__}: {e}"
    return diag


def subscription_label(cred):
    """Best-effort human label for the plan from the credential, e.g. 'Max (5x)'.
    Prefers rateLimitTier (e.g. 'default_claude_max_5x') for the multiplier."""
    node = _claude_oauth(cred)
    if not node:
        return None
    tier = str(node.get("rateLimitTier") or "").lower()
    if "max_20x" in tier:
        return "Max (20x)"
    if "max_5x" in tier:
        return "Max (5x)"
    sub = node.get("subscriptionType")
    if not sub:
        return None
    return {"max": "Max", "pro": "Pro"}.get(str(sub).lower(), str(sub))


def parse_api_usage(raw):
    """Map the raw /api/oauth/usage response into our window dicts.

    The exact field names are confirmed at runtime against a live response; this
    parser is written defensively and looks the keys up by several plausible
    names. Returns a dict {session, weekly_all, weekly_opus, weekly_sonnet} with
    whatever could be extracted (pct 0..100, resets_at ISO), or {} if nothing
    recognisable was found.
    """
    if not isinstance(raw, dict):
        return {}

    def pct_of(node):
        if not isinstance(node, dict):
            return None
        for k in ("utilization", "used_pct", "percent_used", "percentage", "used_percent"):
            if k in node and node[k] is not None:
                v = float(node[k])
                return v * 100 if v <= 1.0 else v
        used = node.get("used") if "used" in node else node.get("used_tokens")
        limit = node.get("limit") if "limit" in node else node.get("max")
        try:
            if used is not None and limit:
                return float(used) / float(limit) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        return None

    def reset_of(node):
        if not isinstance(node, dict):
            return None
        for k in ("resets_at", "reset_at", "resetsAt", "resets", "reset"):
            if node.get(k):
                return node[k]
        return None

    out = {}
    # Session-like node
    for key in ("five_hour", "session", "5h", "current_session", "fiveHour"):
        node = _find_key(raw, key)
        if node:
            out["session"] = {"pct": pct_of(node), "resets_at": reset_of(node)}
            break
    # Weekly all-models
    for key in ("seven_day", "weekly", "7d", "week", "sevenDay", "all_models"):
        node = _find_key(raw, key)
        if node:
            out["weekly_all"] = {"pct": pct_of(node), "resets_at": reset_of(node)}
            break
    # Per-model weekly (Opus / Sonnet)
    for key in ("seven_day_opus", "weekly_opus", "opus"):
        node = _find_key(raw, key)
        if node:
            out["weekly_opus"] = {"pct": pct_of(node), "resets_at": reset_of(node)}
            break
    for key in ("seven_day_sonnet", "weekly_sonnet", "sonnet"):
        node = _find_key(raw, key)
        if node:
            out["weekly_sonnet"] = {"pct": pct_of(node), "resets_at": reset_of(node)}
            break
    return out


# ── Public: compute the indicators ──────────────────────────────────────────

def _block(consumption, turns, by_model, cap_usd, reset_at, now, label,
           api_pct=None, api_reset=None):
    api_reset_dt = None
    if api_reset:
        try:
            api_reset_dt = _parse_z(api_reset) if isinstance(api_reset, str) else \
                datetime.fromtimestamp(float(api_reset), tz=timezone.utc)
        except Exception:
            api_reset_dt = None
    eff_reset = api_reset_dt or reset_at

    if api_pct is not None:
        pct, source = api_pct, "api"
    elif cap_usd:
        pct, source = (consumption / cap_usd * 100.0), "calibrated"
    else:
        pct, source = None, "uncalibrated"

    return {
        "label": label,
        "consumption_usd": round(consumption, 4),
        "turns": turns,
        "cap_usd": cap_usd,
        "pct": round(pct, 1) if pct is not None else None,
        "source": source,
        "reset_at": _iso_z(eff_reset) if eff_reset else None,
        "resets_in_seconds": int((eff_reset - now).total_seconds()) if eff_reset else None,
        "resets_in": _fmt_duration((eff_reset - now).total_seconds()) if eff_reset else None,
        "by_model": by_model,
    }


def compute(db_path=DB_PATH, use_api=None, now=None, auto_calibrate=True,
            scan_first=False):
    """Return the full indicator payload. Pure read; may persist auto-calibrated
    caps back to the config file when fresh API values are available.

    scan_first runs an incremental scan first (fast on re-run) so the local
    $/turn figures reflect activity since the last scan — important right after
    a 5-hour window resets, when the DB would otherwise show 0 turns."""
    now = now or _now_utc()
    cfg = load_config()
    if use_api is None:
        use_api = cfg.get("use_api", True)

    if scan_first:
        try:
            import scanner
            scanner.scan(db_path=db_path, projects_dirs=scanner.DEFAULT_PROJECTS_DIRS,
                         verbose=False)
        except Exception:
            pass

    if not db_path.exists():
        return {"error": "Database not found. Run: claude-usage scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── Session (5h) ──
    bs = _current_block_start(conn, now)
    if bs is not None:
        s_cost, s_turns, s_models = _window_by_model(conn, _iso_z(bs))
        s_reset = bs + timedelta(hours=SESSION_HOURS)
    else:
        s_cost, s_turns, s_models, s_reset = 0.0, 0, [], None

    # ── Weekly (all models) ──
    w_start, w_reset = _local_weekly_start(
        now, cfg["weekly_all"].get("reset_dow"), cfg["weekly_all"].get("reset_hour"))
    w_cost, w_turns, w_models = _window_by_model(conn, _iso_z(w_start))

    # ── Weekly per-model ──
    o_cost, o_turns, o_models = _window_by_model(conn, _iso_z(w_start), model_like="opus")
    so_cost, so_turns, so_models = _window_by_model(conn, _iso_z(w_start), model_like="sonnet")

    conn.close()

    # ── Optional API overlay ──
    api = {}
    api_ok = False
    plan = cfg.get("plan")
    if use_api:
        cred = read_oauth_credential()
        plan = subscription_label(cred) or plan
        raw = fetch_api_usage()
        if raw is not None:
            api = parse_api_usage(raw)
            api_ok = bool(api)
            # Auto-calibrate caps from fresh API fractions so the local estimate
            # stays accurate even when the API is later unavailable.
            if auto_calibrate and api_ok:
                changed = False
                pairs = [
                    ("session", s_cost), ("weekly_all", w_cost),
                    ("weekly_opus", o_cost), ("weekly_sonnet", so_cost),
                ]
                for name, cost in pairs:
                    node = api.get(name) or {}
                    p = node.get("pct")
                    if p and p > 1.0 and cost > 0:
                        cfg[name]["cap_usd"] = round(cost / (p / 100.0), 4)
                        cfg[name]["calibrated_at"] = _iso_z(now)
                        changed = True
                if plan and plan != cfg.get("plan"):
                    cfg["plan"] = plan
                    changed = True
                if changed:
                    try:
                        save_config(cfg)
                    except Exception:
                        pass

    def api_node(name):
        return api.get(name, {}) if api_ok else {}

    session = _block(s_cost, s_turns, s_models, cfg["session"].get("cap_usd"),
                     s_reset, now, "Current session (5h)",
                     api_node("session").get("pct"), api_node("session").get("resets_at"))
    weekly_all = _block(w_cost, w_turns, w_models, cfg["weekly_all"].get("cap_usd"),
                        w_reset, now, "Weekly - all models",
                        api_node("weekly_all").get("pct"), api_node("weekly_all").get("resets_at"))
    # Per-model weekly sub-limits exist only for some plans. When the API is
    # live it tells us exactly which ones apply (a null seven_day_opus means the
    # account has no separate Opus cap — don't invent a row for it). Without the
    # API we can't know the cap structure, so only show a per-model row if there
    # is local usage to report.
    if api_ok:
        include_opus = "weekly_opus" in api
        include_sonnet = "weekly_sonnet" in api
    else:
        include_opus = o_turns > 0
        include_sonnet = so_turns > 0

    weekly_opus = _block(o_cost, o_turns, o_models, cfg["weekly_opus"].get("cap_usd"),
                         w_reset, now, "Weekly - Opus",
                         api_node("weekly_opus").get("pct"),
                         api_node("weekly_opus").get("resets_at")) if include_opus else None
    weekly_sonnet = _block(so_cost, so_turns, so_models, cfg["weekly_sonnet"].get("cap_usd"),
                           w_reset, now, "Weekly - Sonnet",
                           api_node("weekly_sonnet").get("pct"),
                           api_node("weekly_sonnet").get("resets_at")) if include_sonnet else None

    return {
        "plan": plan,
        "api_ok": api_ok,
        "session": session,
        "weekly_all": weekly_all,
        "weekly_opus": weekly_opus,
        "weekly_sonnet": weekly_sonnet,
        "generated_at": now.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
    }


def calibrate(window_name, observed_pct, db_path=DB_PATH):
    """Set the cap for a window from an observed desktop percentage.
    window_name in {session, weekly_all, weekly_opus, weekly_sonnet}."""
    data = compute(db_path=db_path, use_api=False)
    if "error" in data:
        raise RuntimeError(data["error"])
    if window_name not in data:
        raise ValueError(f"unknown window: {window_name}")
    cost = data[window_name]["consumption_usd"]
    if observed_pct <= 0:
        raise ValueError("observed percent must be > 0")
    cap = round(cost / (observed_pct / 100.0), 4)
    cfg = load_config()
    cfg[window_name]["cap_usd"] = cap
    cfg[window_name]["calibrated_at"] = _iso_z(_now_utc())
    save_config(cfg)
    return cap, cost
