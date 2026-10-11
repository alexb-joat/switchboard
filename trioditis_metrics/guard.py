"""Budget guard: decide whether a cloud call is allowed right now.

Reads today's and this month's spend from MetricsDB and compares against
caps loaded from ~/.hlidskjalv/budget.json (same convention as the existing
~/.hlidskjalv/prices.json). Call `guard.check()` before dispatching to a cloud
provider; if it returns a reason (a string), skip the cloud call and fall
back to a local model instead, and log the reason so it shows up as a
"blocked -- over budget" event in Hlidskjalv.

Defaults: $1.00/day, $15.00/month. Edit ~/.hlidskjalv/budget.json to change
them -- no code change or restart-time flag needed, just a file write:

    {"daily_usd": 1.0, "monthly_usd": 15.0}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .home import data_home

from .db import MetricsDB

DEFAULT_CONFIG_PATH = data_home() / "budget.json"


@dataclass
class BudgetCaps:
    daily_usd: float = 1.0
    monthly_usd: float = 15.0

    @classmethod
    def load(cls, path: Path | None = None) -> "BudgetCaps":
        p = path or DEFAULT_CONFIG_PATH
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(
            daily_usd=float(data.get("daily_usd", cls.daily_usd)),
            monthly_usd=float(data.get("monthly_usd", cls.monthly_usd)),
        )


class BudgetGuard:
    def __init__(self, db: MetricsDB, caps: BudgetCaps | None = None):
        self.db = db
        self._fixed = caps  # None = re-read budget.json on every check

    @property
    def caps(self) -> BudgetCaps:
        return self._fixed or BudgetCaps.load()

    def check(self) -> str | None:
        """Returns None if a cloud call is allowed right now, or a short
        human-readable reason why it's blocked."""
        caps = self.caps
        today = self.db.spend_today()
        if today >= caps.daily_usd:
            return f"daily cap reached (${today:.4f} >= ${caps.daily_usd:.2f})"
        month = self.db.spend_this_month()
        if month >= caps.monthly_usd:
            return f"monthly cap reached (${month:.4f} >= ${caps.monthly_usd:.2f})"
        return None
