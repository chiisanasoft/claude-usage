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
_MODULE_TMPDIR = None


def setUpModule():
    """Safety net: no test in this module may read or write the user's real
    ~/.claude/claude-usage-limits.json (see AGENTS.md)."""
    global _MODULE_TMPDIR
    _MODULE_TMPDIR = tempfile.TemporaryDirectory()
    limits.CONFIG_PATH = Path(_MODULE_TMPDIR.name) / "module-limits.json"


def tearDownModule():
    limits.CONFIG_PATH = _REAL_CONFIG_PATH
    if _MODULE_TMPDIR is not None:
        _MODULE_TMPDIR.cleanup()


class TempConfigTestCase(unittest.TestCase):
    """Base class giving every test its own absent-at-start config file."""

    def setUp(self):
        self._orig_cfg = limits.CONFIG_PATH
        self._cfg_dir = tempfile.TemporaryDirectory()
        limits.CONFIG_PATH = Path(self._cfg_dir.name) / "limits.json"
        self.assertNotEqual(limits.CONFIG_PATH, _REAL_CONFIG_PATH)

    def tearDown(self):
        limits.CONFIG_PATH = self._orig_cfg
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

    def _use_api(self, raw):
        self._orig_fetch = limits.fetch_api_usage
        self._orig_cred = limits.read_oauth_credential
        limits.fetch_api_usage = lambda *a, **k: raw
        limits.read_oauth_credential = lambda *a, **k: None
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


if __name__ == "__main__":
    unittest.main()
