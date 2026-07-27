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
        # Only the 3 recent turns belong to the current 5h block (floored to 10:00).
        self.assertEqual(s["turns"], 3)
        expected = sum(calc_cost("claude-opus-4-8", i, o, cr, cc)
                       for (_, _, i, o, cr, cc) in rows[1:])
        self.assertAlmostEqual(s["consumption_usd"], round(expected, 4), places=3)
        # Block floored to 10:00 -> reset 15:00 -> 3h from NOW(12:00).
        self.assertEqual(s["resets_in_seconds"], 3 * 3600)
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


if __name__ == "__main__":
    unittest.main()
