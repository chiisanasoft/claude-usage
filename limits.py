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
from cli import calc_cost, get_pricing

DB_PATH = Path.home() / ".claude" / "usage.db"
CONFIG_PATH = Path.home() / ".claude" / "claude-usage-limits.json"
# Cached API snapshot + back-off clock. Kept out of CONFIG_PATH so the config
# stays a small hand-editable settings file.
STATE_PATH = Path.home() / ".claude" / "claude-usage-limits-state.json"

# The usage endpoint answers 429 under frequent polling, and we are called from
# a 30-second dashboard refresh as well as from every CLI run. Serve a cached
# snapshot inside this window, and stay quiet for a while after a 429.
API_CACHE_SECONDS = 120
API_BACKOFF_SECONDS = 600

SESSION_HOURS = 5
WEEK_DAYS = 7

# Auto-calibration is only trustworthy above this percentage: the API reports
# whole-integer percentages, so at 3% a single point of rounding moves the
# derived cap by a third. Below this we keep whatever cap we already have.
MIN_CALIBRATION_PCT = 10.0

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
        "plan": None,                 # e.g. "Max (5x)" - derived from the credential
        "plan_override": None,        # user-set label; wins over the derived one
        # Which per-model weekly sub-limits the API last confirmed. Empty until
        # a first successful API call; see the inclusion logic in compute().
        "known_sublimits": {},
        "use_api": True,              # try the read-only usage API
        # calibrated_pct / window_reset_at record *what* a cap was derived from so
        # a coarse low reading can't clobber a cap derived from a reliable high one.
        "session": {"cap_usd": None, "calibrated_at": None,
                    "calibrated_pct": None, "window_reset_at": None},
        "weekly_all": {
            "cap_usd": None,
            "calibrated_at": None,
            "calibrated_pct": None,
            "window_reset_at": None,  # API-supplied anchor, survives an API outage
            "reset_dow": None,        # 0=Mon .. 6=Sun, in LOCAL time (None = rolling 7d)
            "reset_hour": None,       # local hour of weekly reset
        },
        "weekly_opus":   {"cap_usd": None, "calibrated_at": None,
                          "calibrated_pct": None, "window_reset_at": None},
        "weekly_sonnet": {"cap_usd": None, "calibrated_at": None,
                          "calibrated_pct": None, "window_reset_at": None},
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


def _floor_half_hour(dt):
    """Real 5-hour windows are anchored on :00 / :30 boundaries (the API reports
    reset times like 06:30), so the no-API heuristic floors to 30 minutes."""
    return dt.replace(minute=(30 if dt.minute >= 30 else 0), second=0, microsecond=0)


def _parse_any_ts(value):
    """Parse an API timestamp (ISO string or epoch seconds) -> aware UTC datetime."""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            dt = _parse_z(value)
        else:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
    priced_turns = 0
    for r in conn.execute(q, params).fetchall():
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        # Models with no PRICING entry (local/3rd-party, or a family we don't
        # know yet) are deliberately not billed at Sonnet rates — but their $0
        # must not be mistaken for "this window cost nothing". Track how many
        # turns we could actually price.
        priced = get_pricing(r["model"]) is not None
        if priced:
            priced_turns += r["turns"] or 0
        total_cost += cost
        total_turns += r["turns"] or 0
        by_model.append({
            "model": r["model"],
            "turns": r["turns"] or 0,
            "priced": priced,
            "input": r["inp"] or 0,
            "output": r["out"] or 0,
            "cache_read": r["cr"] or 0,
            "cache_creation": r["cc"] or 0,
            "cost": cost,
        })
    return total_cost, total_turns, by_model, priced_turns


def _current_block_start(conn, now, lookback_hours=12):
    """Start (floored to 30 minutes) of the 5-hour session window containing the
    latest activity, or None if there has been no activity for >5h.

    This is the *fallback* used only when the API can't tell us where the real
    window is anchored (see `_anchored_window`)."""
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
            block_start = _floor_half_hour(dt)
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


def _anchored_window(reset_at, length, now):
    """Given an authoritative window *reset* time, return (start, reset) for the
    window of the given length that is still open, or None when the reset is
    stale/in the future by more than one window length.

    The local $ figures must be summed over the same window the percentage
    refers to, so whenever the API (or a persisted API anchor) tells us when the
    window resets, the start is `reset - length` — never a guess from turn gaps.
    """
    reset = _parse_any_ts(reset_at)
    if reset is None:
        return None
    start = reset - length
    if not (start <= now < reset):
        return None
    return start, reset


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


def _read_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _write_state(state):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state))
    except Exception:
        pass


def fetch_api_usage(timeout=10, now=None, force=False):
    """Call the read-only usage endpoint. Returns the raw parsed JSON dict on
    success, or None.

    The endpoint performs no inference, so it costs nothing and consumes none of
    the plan's token budget — but it IS rate limited (it answers 429 under
    frequent polling), and this tool is called from a dashboard that refreshes
    every 30 seconds *and* from every CLI invocation. So responses are cached on
    disk for API_CACHE_SECONDS and a 429 triggers a back-off window during which
    we serve the last good snapshot instead of hammering the endpoint.
    """
    now_ts = (now or _now_utc()).timestamp()
    state = _read_state()
    cached = state.get("api_raw")
    cached_at = state.get("api_at") or 0

    if not force:
        if cached and now_ts - cached_at < API_CACHE_SECONDS:
            return cached
        if now_ts < (state.get("api_backoff_until") or 0):
            return cached  # may be None; either way, don't call during back-off

    cred = read_oauth_credential()
    if not cred:
        return cached
    token = _find_token(cred)
    if not token:
        return cached

    import urllib.request
    import urllib.error
    req = urllib.request.Request(API_URL, headers=_api_headers(token), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        state["api_raw"] = raw
        state["api_at"] = now_ts
        state.pop("api_backoff_until", None)
        _write_state(state)
        return raw
    except urllib.error.HTTPError as e:
        if e.code == 429:
            state["api_backoff_until"] = now_ts + API_BACKOFF_SECONDS
            _write_state(state)
        # Serve the last good snapshot rather than silently dropping to a
        # local-only estimate the moment one request is throttled.
        return cached
    except Exception:
        return cached


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


def _scope_name(scope):
    """Human label for a scoped limit, e.g. {'model': {'display_name': 'Fable'}}
    -> 'Fable'. Deliberately generic: new model families appear here without any
    code change, so never hardcode Opus/Sonnet/Fable."""
    if not isinstance(scope, dict):
        return None
    for node_key in ("model", "surface"):
        node = scope.get(node_key)
        if isinstance(node, dict):
            for k in ("display_name", "displayName", "name", "id"):
                v = node.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        elif isinstance(node, str) and node.strip():
            return node.strip()
    return None


def _parse_limits_array(raw):
    """Parse the modern `limits` array of the usage response.

    Each entry looks like:
      {"kind": "weekly_scoped", "group": "weekly", "percent": 84,
       "severity": "warning", "resets_at": "...", "scope": {...},
       "is_active": false}

    This array is the authoritative source when present: the legacy top-level
    keys (seven_day_opus / seven_day_sonnet) went null while a real 84% scoped
    weekly limit lives only here — and a hidden limit is the worst failure this
    tool can have. Returns {} when there is no usable array.
    """
    entries = raw.get("limits") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return {}

    out = {}
    scoped = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind") or "").lower()
        group = str(e.get("group") or "").lower()
        pct = e.get("percent")
        if pct is None:
            pct = e.get("percentage", e.get("utilization"))
        try:
            pct = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            pct = None
        node = {
            "pct": pct,
            "resets_at": e.get("resets_at") or e.get("reset_at"),
            "severity": e.get("severity"),
            "is_active": e.get("is_active"),
        }
        if kind in ("session", "five_hour", "5h") or (not kind and group == "session"):
            out["session"] = node
        elif kind in ("weekly_all", "seven_day", "weekly"):
            out["weekly_all"] = node
        elif "scoped" in kind or e.get("scope"):
            name = _scope_name(e.get("scope")) or "scoped"
            node["name"] = name
            node["key"] = name.lower().replace(" ", "_")
            node["group"] = group or "weekly"
            scoped.append(node)
        elif kind:
            # Unknown future kind — keep it visible rather than silently dropping.
            node["name"] = kind.replace("_", " ").title()
            node["key"] = kind
            node["group"] = group or "weekly"
            scoped.append(node)
    if scoped:
        out["scoped"] = scoped
    return out


def parse_api_usage(raw):
    """Map the raw /api/oauth/usage response into our window dicts.

    The endpoint is undocumented and its shape drifts (it has gained
    `seven_day_cowork`, a `limits` array, ...), so this parser is defensive: it
    reads the modern `limits` array as the primary source and falls back to the
    legacy top-level nodes when the array is absent.

    Returns {session, weekly_all, weekly_opus, weekly_sonnet, scoped} with
    whatever could be extracted (pct 0..100, resets_at ISO, severity), or {} if
    nothing recognisable was found. `session` and `weekly_all` keep their names
    and meanings for cli.py / dashboard.py; `scoped` is a list of extra rows.
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

    # The `limits` array wins where it overlaps — it is the only place scoped
    # (per-model) limits are still reported.
    for k, v in _parse_limits_array(raw).items():
        out[k] = v
    return out


# ── Public: compute the indicators ──────────────────────────────────────────

def _block(consumption, turns, by_model, cap_usd, reset_at, now, label,
           api_pct=None, api_reset=None, severity=None, priced_turns=None):
    eff_reset = _parse_any_ts(api_reset) or reset_at

    # "We couldn't price any of these turns" is not the same statement as "this
    # window cost nothing" — an empty window is genuinely $0.00, but a window of
    # 2042 unpriced turns must be rendered as n/a. Renderers (CLI + dashboard)
    # branch on cost_known rather than guessing from the number.
    if priced_turns is None:
        cost_known = True
    else:
        cost_known = turns == 0 or priced_turns > 0

    if api_pct is not None:
        pct, source = api_pct, "api"
    elif cap_usd and cost_known:
        pct, source = (consumption / cap_usd * 100.0), "calibrated"
    else:
        # Without a priced consumption there is no numerator, so a cap can't
        # produce a percentage — don't pass off 0% as a calibrated reading.
        pct, source = None, "uncalibrated"

    return {
        "label": label,
        "consumption_usd": round(consumption, 4),
        "cost_known": cost_known,
        "priced_turns": priced_turns if priced_turns is not None else turns,
        "turns": turns,
        "cap_usd": cap_usd,
        "pct": round(pct, 1) if pct is not None else None,
        "source": source,
        "severity": severity,
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

    # ── Optional API overlay (fetched BEFORE querying the DB: when it is live it
    # tells us where the real windows are anchored, and the local $ figures must
    # be summed over exactly the window the percentage refers to) ──
    api = {}
    api_ok = False
    derived_plan = cfg.get("plan")
    if use_api:
        cred = read_oauth_credential()
        derived_plan = subscription_label(cred) or derived_plan
        raw = fetch_api_usage()
        if raw is not None:
            api = parse_api_usage(raw)
            api_ok = bool(api)
    # The keychain credential records the tier as it was when the token was
    # issued, so it goes stale after a plan change (a Max 20x account can keep
    # reporting default_claude_max_5x until the token is reissued). An explicit
    # `claude-usage limits --set-plan` always wins over the derived label.
    plan = cfg.get("plan_override") or derived_plan

    def api_node(name):
        return api.get(name, {}) if api_ok else {}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── Session (5h) ──
    # Priority: live API reset > persisted API anchor (survives a brief outage)
    #           > gap-based heuristic.
    s_len = timedelta(hours=SESSION_HOURS)
    anchor = (_anchored_window(api_node("session").get("resets_at"), s_len, now)
              or _anchored_window(cfg["session"].get("window_reset_at"), s_len, now))
    if anchor:
        s_start, s_reset = anchor
        s_anchored = True
    else:
        s_start = _current_block_start(conn, now)
        s_reset = s_start + s_len if s_start else None
        s_anchored = False
    if s_start is not None:
        s_cost, s_turns, s_models, s_priced = _window_by_model(conn, _iso_z(s_start))
    else:
        s_cost, s_turns, s_models, s_priced = 0.0, 0, [], 0

    # ── Weekly (all models) ──
    w_len = timedelta(days=WEEK_DAYS)
    anchor = (_anchored_window(api_node("weekly_all").get("resets_at"), w_len, now)
              or _anchored_window(cfg["weekly_all"].get("window_reset_at"), w_len, now))
    if anchor:
        w_start, w_reset = anchor
        w_anchored = True
    else:
        w_start, w_reset = _local_weekly_start(
            now, cfg["weekly_all"].get("reset_dow"), cfg["weekly_all"].get("reset_hour"))
        w_anchored = False
    w_cost, w_turns, w_models, w_priced = _window_by_model(conn, _iso_z(w_start))

    # ── Weekly per-model ──
    o_cost, o_turns, o_models, o_priced = _window_by_model(conn, _iso_z(w_start), model_like="opus")
    so_cost, so_turns, so_models, so_priced = _window_by_model(conn, _iso_z(w_start), model_like="sonnet")

    # ── Scoped (per-model) weekly rows reported only by the `limits` array ──
    scoped_blocks = []
    for node in (api.get("scoped") or []) if api_ok else []:
        name = node.get("name") or "scoped"
        c, t, m, pr = _window_by_model(conn, _iso_z(w_start), model_like=name.lower())
        scoped_blocks.append(_block(
            c, t, m, None, w_reset, now, f"Weekly - {name}",
            node.get("pct"), node.get("resets_at"), node.get("severity"), pr))

    conn.close()

    # ── Auto-calibration write-back ──
    # Only from percentages large enough to be meaningful: the API reports whole
    # integers, so deriving a cap from 3% amplifies one point of rounding into a
    # ~33% swing (this is what made the caps thrash between runs).
    if use_api and api_ok and auto_calibrate:
        changed = False
        s_gen = _iso_z(s_reset) if s_anchored and s_reset else None
        w_gen = _iso_z(w_reset) if w_anchored and w_reset else None
        pairs = [
            ("session", s_cost, s_gen),
            ("weekly_all", w_cost, w_gen),
            ("weekly_opus", o_cost, w_gen), ("weekly_sonnet", so_cost, w_gen),
        ]
        for name, cost, anchor_iso in pairs:
            if anchor_iso and cfg[name].get("window_reset_at") != anchor_iso:
                cfg[name]["window_reset_at"] = anchor_iso
                # New window generation -> the old derivation no longer applies.
                cfg[name]["calibrated_pct"] = None
                changed = True
            p = (api.get(name) or {}).get("pct")
            if p is None or p < MIN_CALIBRATION_PCT or cost <= 0:
                continue
            # Within one window generation, never let a lower (coarser) reading
            # overwrite a cap derived from a higher, more reliable one.
            prev_pct = cfg[name].get("calibrated_pct")
            if prev_pct is not None and float(prev_pct) > p:
                continue
            cfg[name]["cap_usd"] = round(cost / (p / 100.0), 4)
            cfg[name]["calibrated_at"] = _iso_z(now)
            cfg[name]["calibrated_pct"] = p
            changed = True
        # Cache the *derived* label only. Caching the override instead would
        # make clearing it fall back to the override, not to the real tier.
        if derived_plan and derived_plan != cfg.get("plan"):
            cfg["plan"] = derived_plan
            changed = True
        # Remember which per-model sub-limits this account actually has, so an
        # API outage doesn't resurrect rows the account has no cap for.
        seen = {"weekly_opus": "weekly_opus" in api,
                "weekly_sonnet": "weekly_sonnet" in api}
        if seen != cfg.get("known_sublimits"):
            cfg["known_sublimits"] = seen
            changed = True
        if changed:
            try:
                save_config(cfg)
            except Exception:
                pass

    session = _block(s_cost, s_turns, s_models, cfg["session"].get("cap_usd"),
                     s_reset, now, "Current session (5h)",
                     api_node("session").get("pct"), api_node("session").get("resets_at"),
                     api_node("session").get("severity"), s_priced)
    weekly_all = _block(w_cost, w_turns, w_models, cfg["weekly_all"].get("cap_usd"),
                        w_reset, now, "Weekly - all models",
                        api_node("weekly_all").get("pct"),
                        api_node("weekly_all").get("resets_at"),
                        api_node("weekly_all").get("severity"), w_priced)
    # Per-model weekly sub-limits exist only for some plans. When the API is
    # live it tells us exactly which ones apply (a null seven_day_opus means the
    # account has no separate Opus cap — don't invent a row for it).
    #
    # When the API is down we must not fall back to "there are Opus turns, so
    # draw an Opus row": on an account with no Opus sub-limit that fabricates a
    # row that can never have a percentage, so it renders as a permanent "n/a"
    # with a countdown and no usage — which reads as a broken indicator. Instead
    # remember what the API last told us about the cap *structure* and reuse it;
    # the local turn heuristic is only for accounts we have never seen the API
    # for at all.
    known = cfg.get("known_sublimits") or {}
    if api_ok:
        include_opus = "weekly_opus" in api
        include_sonnet = "weekly_sonnet" in api
    elif known:
        include_opus = bool(known.get("weekly_opus"))
        include_sonnet = bool(known.get("weekly_sonnet"))
    else:
        include_opus = o_turns > 0
        include_sonnet = so_turns > 0

    weekly_opus = _block(o_cost, o_turns, o_models, cfg["weekly_opus"].get("cap_usd"),
                         w_reset, now, "Weekly - Opus",
                         api_node("weekly_opus").get("pct"),
                         api_node("weekly_opus").get("resets_at"),
                         api_node("weekly_opus").get("severity"),
                         o_priced) if include_opus else None
    weekly_sonnet = _block(so_cost, so_turns, so_models, cfg["weekly_sonnet"].get("cap_usd"),
                           w_reset, now, "Weekly - Sonnet",
                           api_node("weekly_sonnet").get("pct"),
                           api_node("weekly_sonnet").get("resets_at"),
                           api_node("weekly_sonnet").get("severity"),
                           so_priced) if include_sonnet else None

    return {
        "plan": plan,
        "api_ok": api_ok,
        "session": session,
        "weekly_all": weekly_all,
        "weekly_opus": weekly_opus,
        "weekly_sonnet": weekly_sonnet,
        # Extra per-model/per-surface limits the API reports generically. Both
        # renderers iterate this list, so a new scoped limit shows up with no
        # code change — a limit we can't render is a limit the user can't see.
        "weekly_scoped": scoped_blocks,
        "generated_at": now.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
    }


def calibrate(window_name, observed_pct, db_path=DB_PATH, now=None):
    """Set the cap for a window from an observed desktop percentage.
    window_name in {session, weekly_all, weekly_opus, weekly_sonnet}.
    `now` overrides wall-clock time (used by tests)."""
    data = compute(db_path=db_path, use_api=False, now=now)
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
    cfg[window_name]["calibrated_at"] = _iso_z(now or _now_utc())
    # Record the derivation so auto-calibration won't clobber this with a
    # coarser, lower API reading later in the same window.
    cfg[window_name]["calibrated_pct"] = float(observed_pct)
    save_config(cfg)
    return cap, cost
