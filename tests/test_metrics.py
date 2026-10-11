import json
import tempfile
import time
import unittest
from pathlib import Path

from switchboard_metrics.db import MetricsDB
from switchboard_metrics.guard import BudgetCaps, BudgetGuard


def mk_db() -> MetricsDB:
    return MetricsDB(Path(tempfile.mkdtemp()) / "metrics.db")


LOCAL_CALL = {"route": "code", "model": "qwen2.5-coder:7b", "local": True, "status": 200,
              "latency_ms": 3100, "usage": {"prompt_tokens": 1980, "completion_tokens": 410},
              "cost": 0.0, "sensitive": [], "failed_attempts": []}

CLOUD_CALL = {"route": "code", "model": "claude-sonnet", "local": False, "status": 200,
              "latency_ms": 6200, "usage": {"prompt_tokens": 3120, "completion_tokens": 840},
              "cost": 0.0219, "sensitive": [], "failed_attempts": ["local-coder"]}


class MetricsDBTest(unittest.TestCase):
    def test_record_and_recent(self):
        db = mk_db()
        db.record(LOCAL_CALL)
        db.record(CLOUD_CALL)
        rows = db.recent()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["model"], "claude-sonnet")  # most recent first
        self.assertEqual(rows[0]["fallbacks"], 1)
        self.assertEqual(json.loads(rows[0]["sensitive"]), [])
        self.assertEqual(rows[1]["tokens_in"], 1980)

    def test_local_calls_never_count_as_spend(self):
        db = mk_db()
        for _ in range(5):
            db.record(LOCAL_CALL)
        self.assertEqual(db.spend_today(), 0.0)

    def test_spend_today_sums_only_todays_cloud_calls(self):
        db = mk_db()
        now = time.time()
        yesterday = now - 36 * 3600
        db.record(CLOUD_CALL, ts=yesterday)
        db.record(CLOUD_CALL, ts=now)
        db.record(CLOUD_CALL, ts=now)
        self.assertAlmostEqual(db.spend_today(now), 0.0438, places=4)

    def test_spend_this_month_includes_whole_month(self):
        db = mk_db()
        now = time.time()
        earlier_this_month = now - 5 * 24 * 3600
        db.record(CLOUD_CALL, ts=earlier_this_month)
        db.record(CLOUD_CALL, ts=now)
        self.assertAlmostEqual(db.spend_this_month(now), 0.0438, places=4)

    def test_sensitive_tags_round_trip(self):
        db = mk_db()
        db.record({**LOCAL_CALL, "sensitive": ["email", "api_key"]})
        self.assertEqual(json.loads(db.recent()[0]["sensitive"]), ["email", "api_key"])


class BudgetCapsTest(unittest.TestCase):
    def test_defaults_when_no_file(self):
        caps = BudgetCaps.load(Path(tempfile.mkdtemp()) / "missing.json")
        self.assertEqual(caps.daily_usd, 1.0)
        self.assertEqual(caps.monthly_usd, 15.0)

    def test_loads_overrides(self):
        p = Path(tempfile.mkdtemp()) / "budget.json"
        p.write_text(json.dumps({"daily_usd": 0.5, "monthly_usd": 8}))
        caps = BudgetCaps.load(p)
        self.assertEqual(caps.daily_usd, 0.5)
        self.assertEqual(caps.monthly_usd, 8.0)

    def test_ignores_malformed_file(self):
        p = Path(tempfile.mkdtemp()) / "budget.json"
        p.write_text("not json")
        caps = BudgetCaps.load(p)
        self.assertEqual(caps.daily_usd, 1.0)


class BudgetGuardTest(unittest.TestCase):
    def test_allows_under_cap(self):
        db = mk_db()
        db.record(CLOUD_CALL)
        guard = BudgetGuard(db, BudgetCaps(daily_usd=1.0, monthly_usd=15.0))
        self.assertIsNone(guard.check())

    def test_blocks_once_daily_cap_hit(self):
        db = mk_db()
        for _ in range(10):
            db.record(CLOUD_CALL)  # 10 * 0.0219 = 0.219
        guard = BudgetGuard(db, BudgetCaps(daily_usd=0.2, monthly_usd=15.0))
        reason = guard.check()
        self.assertIsNotNone(reason)
        self.assertIn("daily cap", reason)

    def test_blocks_once_monthly_cap_hit_even_under_daily(self):
        # A cap so low that even today's single call exceeds it -- independent
        # of which day of the month the test happens to run on.
        db = mk_db()
        db.record(CLOUD_CALL)
        guard = BudgetGuard(db, BudgetCaps(daily_usd=5.0, monthly_usd=0.001))
        reason = guard.check()
        self.assertIsNotNone(reason)
        self.assertIn("monthly cap", reason)

    def test_local_only_usage_never_blocks(self):
        db = mk_db()
        for _ in range(50):
            db.record(LOCAL_CALL)
        guard = BudgetGuard(db, BudgetCaps(daily_usd=0.01, monthly_usd=0.01))
        self.assertIsNone(guard.check())


if __name__ == "__main__":
    unittest.main()
