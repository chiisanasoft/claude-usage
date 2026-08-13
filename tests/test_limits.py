"""Tests for limits.py - session/weekly window reconstruction, calibration,
config I/O, and the API response parser. No network or keychain access."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

import limits
from scanner import init_db
from cli import calc_cost

NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


def _ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _make_db(rows):
    """rows: list of (dt, model, inp, out, cr, cc). Returns Path to a temp DB."""
    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    fd.close()
    path = Path(fd.name)
    conn = sqlite3.connect(path)
    init_db(conn)
    for i, (dt, model, inp, out, cr, cc) in enumerate(rows):
        conn.execute(
            """INSERT INTO turns
               (session_id, timestamp, model, input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens, tool_name, cwd, message_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("s1", _ts(dt), model, inp, out, cr, cc, None, "/p", f"m{i}"),
        )
    conn.commit()
    conn.close()
    return path


_REAL_CONFIG_PATH = limits.CONFIG_PATH
_REAL_STATE_PATH = limits.STATE_PATH
_MODULE_TMPDIR = None


def setUpModule():
    """Safety net: no test in this module may read or write the user's real
    ~/.claude/claude-usage-limits.json or the cached API state file next to it
    (see AGENTS.md)."""
    global _MODULE_TMPDIR
    _MODULE_TMPDIR = tempfile.TemporaryDirectory()
    limits.CONFIG_PATH = Path(_MODULE_TMPDIR.name) / "module-limits.json"
    limits.STATE_PATH = Path(_MODULE_TMPDIR.name) / "module-limits-state.json"


def tearDownModule():
    limits.CONFIG_PATH = _REAL_CONFIG_PATH
    limits.STATE_PATH = _REAL_STATE_PATH
    if _MODULE_TMPDIR is not None:
        _MODULE_TMPDIR.cleanup()


class TempConfigTestCase(unittest.TestCase):
    """Base class giving every test its own absent-at-start config + API state
    files. Both must be redirected: fetch_api_usage caches the raw API snapshot
    and its 429 back-off clock in STATE_PATH, so a test that left it pointing at
    the real file would read and overwrite the user's cached snapshot."""

    def setUp(self):
        self._orig_cfg = limits.CONFIG_PATH
        self._orig_state = limits.STATE_PATH
        self._cfg_dir = tempfile.TemporaryDirectory()
        limits.CONFIG_PATH = Path(self._cfg_dir.name) / "limits.json"
        limits.STATE_PATH = Path(self._cfg_dir.name) / "limits-state.json"
        self.assertNotEqual(limits.CONFIG_PATH, _REAL_CONFIG_PATH)
        self.assertNotEqual(limits.STATE_PATH, _REAL_STATE_PATH)

    def tearDown(self):
        limits.CONFIG_PATH = self._orig_cfg
        limits.STATE_PATH = self._orig_state
        self._cfg_dir.cleanup()


class TestSessionWindow(TempConfigTestCase):
    def test_block_start_and_consumption(self):
        rows = [
            (NOW - timedelta(hours=9), "claude-opus-4-8", 100, 100, 0, 0),   # 03:00 old block
            (NOW - timedelta(hours=1, minutes=30), "claude-opus-4-8", 1000, 2000, 50, 10),  # 10:30
            (NOW - timedelta(hours=1), "claude-opus-4-8", 500, 800, 20, 5),  # 11:00
            (NOW - timedelta(minutes=30), "claude-opus-4-8", 200, 400, 10, 0),  # 11:30
        ]
        db = _make_db(rows)
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        s = data["session"]
        # Only the 3 recent turns belong to the current 5h block (floored to 10:30).
        self.assertEqual(s["turns"], 3)
        expected = sum(calc_cost("claude-opus-4-8", i, o, cr, cc)
                       for (_, _, i, o, cr, cc) in rows[1:])
        self.assertAlmostEqual(s["consumption_usd"], round(expected, 4), places=3)
        # Real windows are anchored on 30-min boundaries: 10:30 -> reset 15:30.
        self.assertEqual(s["resets_in_seconds"], 3 * 3600 + 1800)
        self.assertEqual(s["source"], "uncalibrated")
        self.assertIsNone(s["pct"])

    def test_no_active_window_when_idle(self):
        # Last activity 7h ago -> its 5h window elapsed -> no active session.
        rows = [(NOW - timedelta(hours=7), "claude-opus-4-8", 100, 100, 0, 0)]
        db = _make_db(rows)
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        s = data["session"]
        self.assertEqual(s["turns"], 0)
        self.assertEqual(s["consumption_usd"], 0.0)
        self.assertIsNone(s["reset_at"])


class TestWeeklyWindow(TempConfigTestCase):
    def test_rolling_seven_days(self):
        rows = [
            (NOW - timedelta(days=2), "claude-opus-4-8", 1000, 1000, 0, 0),   # in window
            (NOW - timedelta(days=8), "claude-opus-4-8", 9999, 9999, 0, 0),   # out of window
            (NOW - timedelta(hours=1), "claude-sonnet-4-6", 500, 500, 0, 0),  # in window
        ]
        db = _make_db(rows)
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        # weekly_all excludes the 8-day-old turn
        self.assertEqual(data["weekly_all"]["turns"], 2)
        # opus sub-limit only counts the opus turn within the window
        self.assertEqual(data["weekly_opus"]["turns"], 1)
        self.assertEqual(data["weekly_sonnet"]["turns"], 1)


class TestCalibration(TempConfigTestCase):
    def test_calibrate_sets_cap_and_pct(self):
        rows = [(NOW - timedelta(minutes=30), "claude-opus-4-8", 1000, 2000, 100, 50)]
        db = _make_db(rows)
        cap, cost = limits.calibrate("session", 20.0, db_path=db, now=NOW)
        self.assertAlmostEqual(cap, round(cost / 0.20, 4), places=3)
        # After calibration, compute (no api) should show ~20%.
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        self.assertEqual(data["session"]["source"], "calibrated")
        self.assertAlmostEqual(data["session"]["pct"], 20.0, delta=0.6)

    def test_config_roundtrip(self):
        cfg = limits.load_config()
        cfg["session"]["cap_usd"] = 42.5
        limits.save_config(cfg)
        again = limits.load_config()
        self.assertEqual(again["session"]["cap_usd"], 42.5)
        # defaults preserved for untouched keys
        self.assertIn("weekly_opus", again)


class TestApiParser(TempConfigTestCase):
    def test_parse_fraction_and_reset(self):
        raw = {
            "five_hour": {"utilization": 0.2, "resets_at": "2026-06-27T15:00:00Z"},
            "seven_day": {"utilization": 0.4, "resets_at": "2026-06-30T23:00:00Z"},
            "seven_day_opus": {"utilization": 0.39},
        }
        out = limits.parse_api_usage(raw)
        self.assertAlmostEqual(out["session"]["pct"], 20.0)
        self.assertAlmostEqual(out["weekly_all"]["pct"], 40.0)
        self.assertAlmostEqual(out["weekly_opus"]["pct"], 39.0)
        self.assertEqual(out["session"]["resets_at"], "2026-06-27T15:00:00Z")

    def test_parse_real_shape(self):
        # Mirrors the live /api/oauth/usage response: utilization is a 0-100
        # percent, seven_day_opus is null for accounts without an Opus sub-limit.
        raw = {
            "five_hour": {"utilization": 62.0, "resets_at": "2026-06-27T12:09:59.797540+00:00"},
            "seven_day": {"utilization": 48.0, "resets_at": "2026-06-30T13:59:59+00:00"},
            "seven_day_opus": None,
            "seven_day_sonnet": {"utilization": 0.0, "resets_at": None},
        }
        out = limits.parse_api_usage(raw)
        self.assertAlmostEqual(out["session"]["pct"], 62.0)
        self.assertAlmostEqual(out["weekly_all"]["pct"], 48.0)
        self.assertIn("weekly_sonnet", out)
        self.assertNotIn("weekly_opus", out)  # null -> no row

    def test_parse_used_over_limit(self):
        raw = {"session": {"used": 50, "limit": 200}}
        out = limits.parse_api_usage(raw)
        self.assertAlmostEqual(out["session"]["pct"], 25.0)

    def test_parse_empty(self):
        self.assertEqual(limits.parse_api_usage({}), {})
        self.assertEqual(limits.parse_api_usage(None), {})

    def test_find_key_recursive(self):
        self.assertEqual(limits._find_key({"a": {"b": {"accessToken": "x"}}}, "accessToken"), "x")
        self.assertIsNone(limits._find_key({"a": 1}, "missing"))


# The live /api/oauth/usage response shape as of 2026-07: the per-model
# top-level keys are null and the only place a real 84% scoped weekly limit is
# reported is the `limits` array.
LIVE_RAW = {
    "five_hour": {"utilization": 3.0, "resets_at": "2026-06-27T15:30:00+00:00",
                  "limit_dollars": None},
    "seven_day": {"utilization": 89.0, "resets_at": "2026-06-30T14:00:00+00:00"},
    "seven_day_oauth_apps": None,
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "seven_day_cowork": None,
    "extra_usage": {"is_enabled": False},
    "limits": [
        {"kind": "session", "group": "session", "percent": 3, "severity": "normal",
         "resets_at": "2026-06-27T15:30:00+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 89, "severity": "warning",
         "resets_at": "2026-06-30T14:00:00+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 84, "severity": "warning",
         "resets_at": "2026-06-30T14:00:00+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
         "is_active": False},
    ],
    "member_dashboard_available": False,
}


class TestLimitsArrayParser(TempConfigTestCase):
    def test_array_is_primary_and_exposes_scoped_row(self):
        out = limits.parse_api_usage(LIVE_RAW)
        self.assertAlmostEqual(out["session"]["pct"], 3.0)
        self.assertAlmostEqual(out["weekly_all"]["pct"], 89.0)
        self.assertEqual(out["weekly_all"]["severity"], "warning")
        # The 84% per-model limit must not be invisible.
        scoped = out["scoped"]
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0]["name"], "Fable")
        self.assertAlmostEqual(scoped[0]["pct"], 84.0)
        self.assertEqual(scoped[0]["severity"], "warning")
        # Null per-model top-level keys must not create rows.
        self.assertNotIn("weekly_opus", out)
        self.assertNotIn("weekly_sonnet", out)

    def test_scoped_name_is_generic(self):
        raw = {"limits": [
            {"kind": "weekly_scoped", "group": "weekly", "percent": 12,
             "scope": {"model": {"display_name": "Tangelo"}}},
        ]}
        out = limits.parse_api_usage(raw)
        self.assertEqual(out["scoped"][0]["name"], "Tangelo")
        self.assertEqual(out["scoped"][0]["key"], "tangelo")

    def test_array_wins_over_legacy_keys(self):
        raw = {"five_hour": {"utilization": 10.0},
               "limits": [{"kind": "session", "group": "session", "percent": 55}]}
        self.assertAlmostEqual(limits.parse_api_usage(raw)["session"]["pct"], 55.0)

    def test_legacy_shape_without_array_still_parses(self):
        raw = {
            "five_hour": {"utilization": 62.0, "resets_at": "2026-06-27T15:00:00Z"},
            "seven_day": {"utilization": 48.0},
        }
        out = limits.parse_api_usage(raw)
        self.assertAlmostEqual(out["session"]["pct"], 62.0)
        self.assertAlmostEqual(out["weekly_all"]["pct"], 48.0)
        self.assertNotIn("scoped", out)

    def test_malformed_array_is_ignored(self):
        self.assertEqual(limits.parse_api_usage({"limits": "nope"}), {})
        self.assertEqual(limits.parse_api_usage({"limits": [None, 3]}), {})


class ApiComputeTestCase(TempConfigTestCase):
    """Base that stubs the network + keychain so compute() can use an overlay."""

    def _use_api(self, raw, cred=None):
        self._orig_fetch = limits.fetch_api_usage
        self._orig_cred = limits.read_oauth_credential
        limits.fetch_api_usage = lambda *a, **k: raw
        limits.read_oauth_credential = lambda *a, **k: cred
        self.addCleanup(self._restore)

    def _restore(self):
        limits.fetch_api_usage = self._orig_fetch
        limits.read_oauth_credential = self._orig_cred


class TestApiAnchoredWindow(ApiComputeTestCase):
    # API says the session resets at 15:30 -> the window really started at 10:30,
    # so the 10:00 turn must NOT be counted (the gap heuristic would count it).
    ROWS = [
        (NOW - timedelta(hours=2), "claude-opus-4-8", 100, 100, 0, 0),      # 10:00 - out
        (NOW - timedelta(hours=1), "claude-opus-4-8", 1000, 2000, 50, 10),  # 11:00 - in
        (NOW - timedelta(minutes=15), "claude-opus-4-8", 200, 400, 10, 0),  # 11:45 - in
    ]

    def test_window_start_comes_from_api_reset(self):
        db = _make_db(self.ROWS)
        self._use_api(LIVE_RAW)
        data = limits.compute(db_path=db, now=NOW)
        s = data["session"]
        self.assertEqual(s["turns"], 2)
        self.assertEqual(s["resets_in_seconds"], 3 * 3600 + 1800)
        self.assertEqual(s["pct"], 3.0)
        self.assertEqual(s["source"], "api")
        # The scoped 84% row is surfaced for renderers to iterate.
        scoped = data["weekly_scoped"]
        self.assertEqual([b["pct"] for b in scoped], [84.0])
        self.assertIn("Fable", scoped[0]["label"])

    def test_heuristic_would_have_used_a_different_window(self):
        db = _make_db(self.ROWS)
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        # No API and no persisted anchor -> gap heuristic starts at 10:00.
        self.assertEqual(data["session"]["turns"], 3)

    def test_anchor_persists_across_an_api_outage(self):
        db = _make_db(self.ROWS)
        self._use_api(LIVE_RAW)
        limits.compute(db_path=db, now=NOW)
        cfg = limits.load_config()
        self.assertEqual(cfg["session"]["window_reset_at"], "2026-06-27T15:30:00.000Z")
        # API now unavailable: the persisted anchor keeps the correct window.
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        self.assertEqual(data["session"]["turns"], 2)

    def test_stale_anchor_is_ignored(self):
        db = _make_db(self.ROWS)
        cfg = limits.load_config()
        # An anchor from a window that already reset must not be reused.
        cfg["session"]["window_reset_at"] = limits._iso_z(NOW - timedelta(hours=1))
        limits.save_config(cfg)
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        self.assertEqual(data["session"]["turns"], 3)


class TestCalibrationGuard(ApiComputeTestCase):
    ROWS = [(NOW - timedelta(minutes=15), "claude-opus-4-8", 1000, 2000, 100, 50)]

    def _raw(self, session_pct):
        return {"limits": [
            {"kind": "session", "group": "session", "percent": session_pct,
             "resets_at": "2026-06-27T15:30:00+00:00"},
        ]}

    def test_low_percentage_does_not_write_a_cap(self):
        db = _make_db(self.ROWS)
        self._use_api(self._raw(3))
        limits.compute(db_path=db, now=NOW)
        # 3% of a coarse integer reading would swing the cap by a third.
        self.assertIsNone(limits.load_config()["session"]["cap_usd"])

    def test_low_percentage_does_not_overwrite_a_better_cap(self):
        db = _make_db(self.ROWS)
        self._use_api(self._raw(40))
        limits.compute(db_path=db, now=NOW)
        cap40 = limits.load_config()["session"]["cap_usd"]
        self.assertIsNotNone(cap40)
        self.assertAlmostEqual(limits.load_config()["session"]["calibrated_pct"], 40.0)

        # Same window generation, lower reading -> must be ignored.
        self._restore()
        self._use_api(self._raw(12))
        limits.compute(db_path=db, now=NOW)
        self.assertEqual(limits.load_config()["session"]["cap_usd"], cap40)

        # A higher reading in the same window is more reliable -> accepted.
        self._restore()
        self._use_api(self._raw(80))
        limits.compute(db_path=db, now=NOW)
        cfg = limits.load_config()
        self.assertNotEqual(cfg["session"]["cap_usd"], cap40)
        self.assertAlmostEqual(cfg["session"]["calibrated_pct"], 80.0)

    def test_new_window_generation_resets_the_guard(self):
        db = _make_db(self.ROWS)
        self._use_api(self._raw(80))
        limits.compute(db_path=db, now=NOW)
        self.assertAlmostEqual(limits.load_config()["session"]["calibrated_pct"], 80.0)
        # Next window (different resets_at) -> a 40% reading is allowed again.
        self._restore()
        self._use_api({"limits": [
            {"kind": "session", "group": "session", "percent": 40,
             "resets_at": "2026-06-27T16:00:00+00:00"},
        ]})
        limits.compute(db_path=db, now=NOW)
        self.assertAlmostEqual(limits.load_config()["session"]["calibrated_pct"], 40.0)

    def test_manual_calibration_records_its_percentage(self):
        db = _make_db(self.ROWS)
        limits.calibrate("session", 35.0, db_path=db, now=NOW)
        self.assertAlmostEqual(limits.load_config()["session"]["calibrated_pct"], 35.0)


class TestUnpricedConsumption(ApiComputeTestCase):
    """A window of turns on a model with no PRICING entry costs an *unknown*
    amount, not $0.00 (AGENTS.md: unknown models are deliberately not billed at
    Sonnet rates). The payload must let renderers tell the two apart."""

    def test_unpriced_window_reports_cost_unknown(self):
        db = _make_db([(NOW - timedelta(minutes=15), "fable-1", 1000, 2000, 0, 0)])
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        s = data["session"]
        self.assertEqual(s["turns"], 1)
        self.assertEqual(s["priced_turns"], 0)
        self.assertFalse(s["cost_known"])
        self.assertEqual(s["consumption_usd"], 0.0)

    def test_empty_window_is_genuinely_zero(self):
        db = _make_db([(NOW - timedelta(hours=9), "claude-opus-4-8", 100, 100, 0, 0)])
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        s = data["session"]
        self.assertEqual(s["turns"], 0)
        self.assertTrue(s["cost_known"])

    def test_partially_priced_window_keeps_its_cost(self):
        db = _make_db([
            (NOW - timedelta(minutes=20), "fable-1", 1000, 2000, 0, 0),
            (NOW - timedelta(minutes=10), "claude-opus-4-8", 1000, 2000, 0, 0),
        ])
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        s = data["session"]
        self.assertEqual(s["turns"], 2)
        self.assertEqual(s["priced_turns"], 1)
        self.assertTrue(s["cost_known"])
        self.assertGreater(s["consumption_usd"], 0)
        self.assertEqual([m["priced"] for m in s["by_model"] if m["model"] == "fable-1"],
                         [False])

    def test_no_percentage_from_an_unpriced_window(self):
        db = _make_db([(NOW - timedelta(minutes=15), "fable-1", 1000, 2000, 0, 0)])
        cfg = limits.load_config()
        cfg["session"]["cap_usd"] = 100.0
        limits.save_config(cfg)
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        # A cap over an unknown numerator would render a bogus 0%.
        self.assertIsNone(data["session"]["pct"])
        self.assertEqual(data["session"]["source"], "uncalibrated")

    def test_cli_renders_na_for_unpriced(self):
        import io
        import contextlib
        from cli import _render_limit_block
        db = _make_db([(NOW - timedelta(minutes=15), "fable-1", 1000, 2000, 0, 0)])
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _render_limit_block(data["session"])
        out = buf.getvalue()
        self.assertIn("n/a used", out)
        self.assertNotIn("$0.00 used", out)


class TestHalfHourFloor(TempConfigTestCase):
    def test_floor_half_hour(self):
        d = datetime(2026, 6, 27, 1, 47, 30, tzinfo=timezone.utc)
        self.assertEqual(limits._floor_half_hour(d).minute, 30)
        self.assertEqual(limits._floor_half_hour(d.replace(minute=12)).minute, 0)

    def test_anchored_window_rejects_out_of_range(self):
        now = NOW
        self.assertIsNone(limits._anchored_window(None, timedelta(hours=5), now))
        self.assertIsNone(limits._anchored_window("garbage", timedelta(hours=5), now))
        # Already reset.
        self.assertIsNone(limits._anchored_window(
            limits._iso_z(now - timedelta(minutes=1)), timedelta(hours=5), now))
        start, reset = limits._anchored_window(
            limits._iso_z(now + timedelta(hours=1)), timedelta(hours=5), now)
        self.assertEqual(reset - start, timedelta(hours=5))


class _FakeResponse:
    """Minimal stand-in for the object urlopen() returns."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ApiFetchTestCase(TempConfigTestCase):
    """Base for fetch_api_usage tests: stubs the keychain and the urllib layer so
    nothing here can touch the network or the real credential store."""

    CRED = {"claudeAiOauth": {"accessToken": "tok-abc",
                              "rateLimitTier": "default_claude_max_5x"}}
    RAW = {"five_hour": {"utilization": 62.0}}

    def setUp(self):
        super().setUp()
        import urllib.request
        self._urllib = urllib.request
        self._orig_urlopen = urllib.request.urlopen
        self._orig_cred = limits.read_oauth_credential
        self._orig_findtok = limits._find_token
        limits.read_oauth_credential = lambda *a, **k: self.CRED
        limits._find_token = lambda cred: "tok-abc"
        self.calls = []
        self.addCleanup(self._restore_api)

    def _restore_api(self):
        self._urllib.urlopen = self._orig_urlopen
        limits.read_oauth_credential = self._orig_cred
        limits._find_token = self._orig_findtok

    def _serve(self, payload):
        """Network returns `payload`; every call is recorded."""
        def fake(req, timeout=None):
            self.calls.append(req.full_url)
            return _FakeResponse(payload)
        self._urllib.urlopen = fake

    def _serve_429(self):
        import urllib.error

        def fake(req, timeout=None):
            self.calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        self._urllib.urlopen = fake

    def _explode(self):
        """Any network call at all is a test failure."""
        def fake(req, timeout=None):
            raise AssertionError("fetch_api_usage made a network call")
        self._urllib.urlopen = fake

    def _write_state(self, **state):
        limits.STATE_PATH.write_text(json.dumps(state))

    def _state(self):
        return json.loads(limits.STATE_PATH.read_text())


class TestApiCache(ApiFetchTestCase):
    """The usage endpoint is rate limited and we poll it from a 30s dashboard
    refresh plus every CLI run, so responses are cached on disk."""

    def test_fresh_cache_short_circuits_without_a_network_call(self):
        self._write_state(api_raw=self.RAW, api_at=NOW.timestamp())
        self._explode()
        out = limits.fetch_api_usage(now=NOW + timedelta(seconds=limits.API_CACHE_SECONDS - 1))
        self.assertEqual(out, self.RAW)

    def test_expired_cache_refetches(self):
        self._write_state(api_raw=self.RAW, api_at=NOW.timestamp())
        fresh = {"five_hour": {"utilization": 7.0}}
        self._serve(fresh)
        out = limits.fetch_api_usage(now=NOW + timedelta(seconds=limits.API_CACHE_SECONDS + 1))
        self.assertEqual(out, fresh)
        self.assertEqual(len(self.calls), 1)

    def test_success_rewrites_the_cached_snapshot(self):
        self._serve(self.RAW)
        later = NOW + timedelta(hours=1)
        limits.fetch_api_usage(now=later)
        st = self._state()
        self.assertEqual(st["api_raw"], self.RAW)
        self.assertAlmostEqual(st["api_at"], later.timestamp())

    def test_no_credential_serves_a_recent_cache_without_calling(self):
        self._write_state(api_raw=self.RAW, api_at=NOW.timestamp())
        limits.read_oauth_credential = lambda *a, **k: None
        self._explode()
        # Older than the cache window (so we would have tried the network) but
        # still young enough to stand in for a live reading.
        out = limits.fetch_api_usage(
            now=NOW + timedelta(seconds=limits.API_SNAPSHOT_MAX_AGE_SECONDS - 1))
        self.assertEqual(out, self.RAW)


class TestStaleSnapshot(ApiFetchTestCase):
    """A snapshot that can no longer be refreshed must not be served forever.
    The state file can be mounted read-only into a container, where it is frozen
    at whatever the host last fetched — presenting that as "live from API" days
    later is worse than showing an honest calibrated estimate."""

    def test_stale_cache_without_a_credential_returns_none(self):
        self._write_state(api_raw=self.RAW, api_at=NOW.timestamp())
        limits.read_oauth_credential = lambda *a, **k: None
        self._explode()
        out = limits.fetch_api_usage(
            now=NOW + timedelta(seconds=limits.API_SNAPSHOT_MAX_AGE_SECONDS + 1))
        self.assertIsNone(out)

    def test_stale_cache_without_a_token_returns_none(self):
        self._write_state(api_raw=self.RAW, api_at=NOW.timestamp())
        limits._find_token = lambda cred: None
        self._explode()
        self.assertIsNone(limits.fetch_api_usage(now=NOW + timedelta(days=15)))

    def test_stale_cache_after_a_failed_request_returns_none(self):
        self._write_state(api_raw=self.RAW, api_at=NOW.timestamp())

        def fake(req, timeout=None):
            raise OSError("network unreachable")
        self._urllib.urlopen = fake
        self.assertIsNone(limits.fetch_api_usage(now=NOW + timedelta(days=15)))
        # ...while a recent one still is served.
        self.assertEqual(
            limits.fetch_api_usage(
                now=NOW + timedelta(seconds=limits.API_CACHE_SECONDS + 1)),
            self.RAW)

    def test_backoff_still_serves_a_stale_snapshot(self):
        # The back-off is at most API_BACKOFF_SECONDS long and only reachable
        # right after a real 429, so it keeps its "never downgrade" behaviour.
        self._write_state(api_raw=self.RAW, api_at=NOW.timestamp(),
                          api_backoff_until=(NOW + timedelta(days=30)).timestamp())
        self._explode()
        self.assertEqual(limits.fetch_api_usage(now=NOW + timedelta(days=15)),
                         self.RAW)


class TestApiBackoff(ApiFetchTestCase):
    """A 429 must not silently downgrade the UI to a local-only estimate."""

    def test_429_sets_backoff_and_serves_the_last_good_snapshot(self):
        self._write_state(api_raw=self.RAW, api_at=NOW.timestamp())
        self._serve_429()
        later = NOW + timedelta(seconds=limits.API_CACHE_SECONDS + 1)
        out = limits.fetch_api_usage(now=later)
        self.assertEqual(out, self.RAW)  # not None
        self.assertEqual(len(self.calls), 1)
        self.assertAlmostEqual(self._state()["api_backoff_until"],
                               later.timestamp() + limits.API_BACKOFF_SECONDS)

    def test_no_network_call_while_the_backoff_is_in_force(self):
        self._write_state(api_raw=self.RAW, api_at=NOW.timestamp(),
                          api_backoff_until=(NOW + timedelta(hours=2)).timestamp())
        self._explode()
        # Cache is stale, but the back-off must still suppress the request.
        out = limits.fetch_api_usage(now=NOW + timedelta(hours=1))
        self.assertEqual(out, self.RAW)

    def test_backoff_expires(self):
        self._write_state(api_raw=self.RAW, api_at=NOW.timestamp(),
                          api_backoff_until=(NOW + timedelta(minutes=1)).timestamp())
        fresh = {"five_hour": {"utilization": 9.0}}
        self._serve(fresh)
        out = limits.fetch_api_usage(now=NOW + timedelta(minutes=2))
        self.assertEqual(out, fresh)
        self.assertEqual(len(self.calls), 1)

    def test_429_without_a_prior_snapshot_returns_none(self):
        self._serve_429()
        self.assertIsNone(limits.fetch_api_usage(now=NOW))
        self.assertIn("api_backoff_until", self._state())

    def test_success_clears_an_existing_backoff(self):
        self._write_state(api_raw=self.RAW, api_at=NOW.timestamp(),
                          api_backoff_until=(NOW + timedelta(hours=2)).timestamp())
        self._serve(self.RAW)
        limits.fetch_api_usage(now=NOW + timedelta(hours=1), force=True)
        self.assertNotIn("api_backoff_until", self._state())

    def test_non_429_http_error_does_not_set_a_backoff(self):
        import urllib.error
        self._write_state(api_raw=self.RAW, api_at=NOW.timestamp())

        def fake(req, timeout=None):
            self.calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        self._urllib.urlopen = fake
        out = limits.fetch_api_usage(now=NOW + timedelta(seconds=limits.API_CACHE_SECONDS + 1))
        self.assertEqual(out, self.RAW)
        self.assertNotIn("api_backoff_until", self._state())


class TestApiForce(ApiFetchTestCase):
    def test_force_bypasses_a_fresh_cache(self):
        self._write_state(api_raw=self.RAW, api_at=NOW.timestamp())
        fresh = {"five_hour": {"utilization": 11.0}}
        self._serve(fresh)
        out = limits.fetch_api_usage(now=NOW, force=True)
        self.assertEqual(out, fresh)
        self.assertEqual(len(self.calls), 1)

    def test_force_bypasses_the_backoff(self):
        self._write_state(api_raw=self.RAW, api_at=NOW.timestamp(),
                          api_backoff_until=(NOW + timedelta(hours=2)).timestamp())
        fresh = {"five_hour": {"utilization": 13.0}}
        self._serve(fresh)
        out = limits.fetch_api_usage(now=NOW + timedelta(minutes=1), force=True)
        self.assertEqual(out, fresh)
        self.assertEqual(len(self.calls), 1)


class TestKnownSublimits(ApiComputeTestCase):
    """Which per-model weekly rows exist is a property of the *plan*, learned
    from the API — not something to guess from "are there Opus turns in the DB".
    Guessing fabricates a row that can never have a percentage, so it renders as
    a permanent 'n/a' next to a reset countdown."""

    OPUS_ROWS = [(NOW - timedelta(days=1), "claude-opus-4-8", 1000, 2000, 0, 0)]

    def _set_known(self, opus, sonnet):
        cfg = limits.load_config()
        cfg["known_sublimits"] = {"weekly_opus": opus, "weekly_sonnet": sonnet}
        limits.save_config(cfg)

    def test_api_records_the_sublimit_structure(self):
        db = _make_db(self.OPUS_ROWS)
        self._use_api({
            "five_hour": {"utilization": 20.0},
            "seven_day": {"utilization": 30.0},
            "seven_day_opus": {"utilization": 40.0},
            "seven_day_sonnet": None,
        })
        limits.compute(db_path=db, now=NOW)
        self.assertEqual(limits.load_config()["known_sublimits"],
                         {"weekly_opus": True, "weekly_sonnet": False})

    def test_live_response_without_per_model_keys_records_absence(self):
        db = _make_db(self.OPUS_ROWS)
        self._use_api(LIVE_RAW)
        data = limits.compute(db_path=db, now=NOW)
        self.assertIsNone(data["weekly_opus"])
        self.assertEqual(limits.load_config()["known_sublimits"],
                         {"weekly_opus": False, "weekly_sonnet": False})

    def test_outage_does_not_resurrect_an_absent_opus_row(self):
        # The user-visible bug: a DB full of Opus turns on an account whose API
        # reported no separate Opus cap must still not draw a Weekly-Opus row.
        db = _make_db(self.OPUS_ROWS)
        self._set_known(opus=False, sonnet=False)
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        self.assertGreater(data["weekly_all"]["turns"], 0)
        self.assertIsNone(data["weekly_opus"])
        self.assertIsNone(data["weekly_sonnet"])

    def test_outage_keeps_a_row_the_api_did_confirm(self):
        db = _make_db(self.OPUS_ROWS)
        self._set_known(opus=True, sonnet=False)
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        self.assertIsNotNone(data["weekly_opus"])
        self.assertEqual(data["weekly_opus"]["turns"], 1)
        self.assertIsNone(data["weekly_sonnet"])

    def test_confirmed_row_survives_even_with_no_matching_turns(self):
        db = _make_db([(NOW - timedelta(days=1), "claude-sonnet-4-6", 100, 100, 0, 0)])
        self._set_known(opus=True, sonnet=True)
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        self.assertIsNotNone(data["weekly_opus"])
        self.assertEqual(data["weekly_opus"]["turns"], 0)

    def test_without_known_sublimits_the_turn_heuristic_applies(self):
        db = _make_db(self.OPUS_ROWS)
        self.assertEqual(limits.load_config()["known_sublimits"], {})
        data = limits.compute(db_path=db, use_api=False, now=NOW)
        self.assertIsNotNone(data["weekly_opus"])   # opus turns exist
        self.assertIsNone(data["weekly_sonnet"])    # none do


class TestPlanLabel(TempConfigTestCase):
    def test_subscription_label_from_rate_limit_tier(self):
        self.assertEqual(limits.subscription_label(
            {"claudeAiOauth": {"rateLimitTier": "default_claude_max_20x"}}), "Max (20x)")
        self.assertEqual(limits.subscription_label(
            {"claudeAiOauth": {"rateLimitTier": "default_claude_max_5x"}}), "Max (5x)")

    def test_subscription_label_falls_back_to_subscription_type(self):
        self.assertEqual(limits.subscription_label(
            {"claudeAiOauth": {"subscriptionType": "pro"}}), "Pro")
        self.assertIsNone(limits.subscription_label({"claudeAiOauth": {}}))
        self.assertIsNone(limits.subscription_label(None))


class TestPlanOverride(ApiComputeTestCase):
    """The keychain records the tier as of token issue, so it goes stale after a
    plan change; an explicit override has to win — without destroying the
    knowledge of what the real tier is."""

    ROWS = [(NOW - timedelta(minutes=15), "claude-opus-4-8", 1000, 2000, 0, 0)]
    CRED_5X = {"claudeAiOauth": {"accessToken": "tok",
                                 "rateLimitTier": "default_claude_max_5x"}}
    RAW = {"five_hour": {"utilization": 20.0}, "seven_day": {"utilization": 30.0}}

    def test_derived_label_is_used_when_no_override(self):
        db = _make_db(self.ROWS)
        self._use_api(self.RAW, cred=self.CRED_5X)
        self.assertEqual(limits.compute(db_path=db, now=NOW)["plan"], "Max (5x)")

    def test_override_wins_over_the_derived_label(self):
        db = _make_db(self.ROWS)
        cfg = limits.load_config()
        cfg["plan_override"] = "Max (20x)"
        limits.save_config(cfg)
        self._use_api(self.RAW, cred=self.CRED_5X)
        self.assertEqual(limits.compute(db_path=db, now=NOW)["plan"], "Max (20x)")

    def test_clearing_the_override_falls_back_to_the_real_tier(self):
        db = _make_db(self.ROWS)
        cfg = limits.load_config()
        cfg["plan_override"] = "Max (20x)"
        limits.save_config(cfg)
        self._use_api(self.RAW, cred=self.CRED_5X)
        limits.compute(db_path=db, now=NOW)
        # The *derived* label is what gets cached — caching the override would
        # make clearing it fall back to the override itself.
        cfg = limits.load_config()
        self.assertEqual(cfg["plan"], "Max (5x)")
        cfg["plan_override"] = None
        limits.save_config(cfg)
        self.assertEqual(limits.compute(db_path=db, now=NOW)["plan"], "Max (5x)")


class TestContainerFallback(TempConfigTestCase):
    """The container case end-to-end (see docs/DOCKER.md): the config and the
    cached API state are bind-mounted read-only from the host, but there is no
    keychain to refresh the snapshot with. The card must still show numbers —
    calibrated ones, honestly labelled — instead of a frozen "live" reading."""

    ROWS = [(NOW - timedelta(minutes=30), "claude-opus-4-8", 1000, 2000, 500, 100)]

    def setUp(self):
        super().setUp()
        self._orig_cred = limits.read_oauth_credential
        limits.read_oauth_credential = lambda *a, **k: None   # no macOS keychain
        import urllib.request
        self._urllib = urllib.request
        self._orig_urlopen = urllib.request.urlopen

        def no_network(req, timeout=None):
            raise OSError("network unreachable")
        urllib.request.urlopen = no_network
        self.addCleanup(self._restore)

    def _restore(self):
        limits.read_oauth_credential = self._orig_cred
        self._urllib.urlopen = self._orig_urlopen

    def _seed(self, snapshot_age):
        """A host-written config (calibrated caps + plan override + known
        sub-limit structure) plus an API snapshot of the given age."""
        cfg = limits.default_config()
        cfg["plan_override"] = "Max (20x)"
        cfg["known_sublimits"] = {"weekly_opus": False, "weekly_sonnet": False}
        cfg["session"]["cap_usd"] = 100.0
        cfg["weekly_all"]["cap_usd"] = 1000.0
        limits.save_config(cfg)
        limits.STATE_PATH.write_text(json.dumps({
            "api_raw": {"five_hour": {"utilization": 99.0}},
            "api_at": (NOW - snapshot_age).timestamp(),
        }))

    def test_stale_state_file_falls_back_to_calibrated(self):
        db = _make_db(self.ROWS)
        self._seed(snapshot_age=timedelta(days=15))
        data = limits.compute(db_path=db, now=NOW)

        self.assertFalse(data["api_ok"])
        self.assertEqual(data["plan"], "Max (20x)")        # from the override
        cost = calc_cost("claude-opus-4-8", 1000, 2000, 500, 100)
        for key, cap in (("session", 100.0), ("weekly_all", 1000.0)):
            block = data[key]
            self.assertEqual(block["source"], "calibrated")
            self.assertEqual(block["pct"], round(cost / cap * 100.0, 1))
        # The 99% from the frozen snapshot must not leak through anywhere.
        self.assertNotEqual(data["session"]["pct"], 99.0)
        # known_sublimits says this account has neither sub-limit: no fabricated
        # rows, even though every turn in the DB is Opus.
        self.assertIsNone(data["weekly_opus"])
        self.assertIsNone(data["weekly_sonnet"])

    def test_fresh_state_file_is_still_used(self):
        db = _make_db(self.ROWS)
        self._seed(snapshot_age=timedelta(seconds=60))
        data = limits.compute(db_path=db, now=NOW)
        self.assertTrue(data["api_ok"])
        self.assertEqual(data["session"]["source"], "api")
        self.assertEqual(data["session"]["pct"], 99.0)

    def test_read_only_config_does_not_break_the_request(self):
        # The mounts are :ro, so the auto-calibration write-back must fail
        # silently rather than 500 the /api/limits endpoint.
        db = _make_db(self.ROWS)
        self._seed(snapshot_age=timedelta(seconds=60))
        orig_save = limits.save_config

        def readonly(cfg):
            raise PermissionError("Read-only file system")
        limits.save_config = readonly
        self.addCleanup(lambda: setattr(limits, "save_config", orig_save))
        data = limits.compute(db_path=db, now=NOW)
        self.assertEqual(data["session"]["pct"], 99.0)


if __name__ == "__main__":
    unittest.main()
