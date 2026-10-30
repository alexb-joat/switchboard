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

Concurrent calls: a call reserves its estimated cost before it starts and releases it once
its real cost is recorded, so a burst of requests cannot all pass a cap that only the first
of them fits under. A call whose estimate would cross a cap is skipped like one over it.
"""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .db import MetricsDB
from .home import data_home

DEFAULT_CONFIG_PATH = data_home() / "budget.json"


@dataclass
class BudgetCaps:
    daily_usd: float = 1.0
    monthly_usd: float = 15.0
    # Optional caps per provider (the names from config.yaml), on top of the global ones:
    # {"openrouter": {"monthly_usd": 5.0}, "anthropic": {"daily_usd": 0.5}}
    providers: dict = field(default_factory=dict)
    # What a cloud call without a price is assumed to have cost, so the caps still count it (0 = ignore).
    assume_unpriced_usd: float = 0.0
    # Output tokens assumed for the estimate when a request sets no max_tokens.
    assume_output_tokens: int = 1024

    @classmethod
    def load(cls, path: Path | None = None) -> BudgetCaps:
        p = path or DEFAULT_CONFIG_PATH
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        providers = {}
        for name, caps in (data.get("providers") or {}).items():
            if isinstance(caps, dict):
                providers[str(name)] = {k: float(caps[k]) for k in ("daily_usd", "monthly_usd")
                                        if isinstance(caps.get(k), (int, float)) and not isinstance(caps.get(k), bool)}
        return cls(
            daily_usd=_num(data.get("daily_usd"), cls.daily_usd),
            monthly_usd=_num(data.get("monthly_usd"), cls.monthly_usd),
            providers=providers,
            assume_unpriced_usd=max(0.0, _num(data.get("assume_unpriced_usd"), 0.0)),
            assume_output_tokens=max(0, int(_num(data.get("assume_output_tokens"), cls.assume_output_tokens))),
        )


def _num(v, default: float) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


@dataclass
class Reservation:
    id: int
    provider: str | None
    usd: float
    at: float


class BudgetGuard:
    # A reservation older than this is dropped: a stream the client abandoned before it began
    # never reports back, and must not hold the cap forever.
    RESERVATION_TTL_S = 900

    def __init__(self, db: MetricsDB, caps: BudgetCaps | None = None):
        self.db = db
        self._fixed = caps  # None = re-read budget.json on every check
        self._pending: dict[int, Reservation] = {}
        self._ids = itertools.count(1)

    @property
    def caps(self) -> BudgetCaps:
        return self._fixed or BudgetCaps.load()

    def pending(self, provider: str | None = None) -> float:
        """Estimated cost of calls that started and have not been recorded yet."""
        cutoff = time.monotonic() - self.RESERVATION_TTL_S
        for rid in [rid for rid, r in self._pending.items() if r.at < cutoff]:
            del self._pending[rid]
        return sum(r.usd for r in self._pending.values() if provider is None or r.provider == provider)

    def check(self, provider: str | None = None, estimate: float = 0.0, caps: BudgetCaps | None = None) -> str | None:
        """Returns None if a cloud call costing about `estimate` is allowed right now, or a short
        human-readable reason why it's blocked. With `provider`, that provider's own caps (if any)
        are checked too."""
        caps = caps or self.caps
        assume = caps.assume_unpriced_usd
        pending = self.pending()
        windows = [("daily", caps.daily_usd, self.db.spend_today() + self.db.unpriced_today() * assume),
                   ("monthly", caps.monthly_usd, self.db.spend_this_month() + self.db.unpriced_this_month() * assume)]
        for label, cap, spent in windows:
            if reason := _over(label, cap, spent, pending, estimate):
                return reason
        own = caps.providers.get(provider or "")
        if own:
            mine = self.pending(provider)
            if "daily_usd" in own:
                spent = self.db.spend_today(provider=provider) + self.db.unpriced_today(provider=provider) * assume
                if reason := _over(f"{provider} daily", own["daily_usd"], spent, mine, estimate):
                    return reason
            if "monthly_usd" in own:
                spent = self.db.spend_this_month(provider=provider) + self.db.unpriced_this_month(provider=provider) * assume
                if reason := _over(f"{provider} monthly", own["monthly_usd"], spent, mine, estimate):
                    return reason
        return None

    def reserve(self, provider: str | None, estimate: float,
                caps: BudgetCaps | None = None) -> tuple[Reservation | None, str | None]:
        """Check and hold `estimate` in one step (no await in between, so concurrent requests on
        the event loop see each other's holds). Returns (reservation, None) or (None, reason)."""
        reason = self.check(provider, estimate, caps)
        if reason:
            return None, reason
        r = Reservation(next(self._ids), provider, max(0.0, estimate), time.monotonic())
        self._pending[r.id] = r
        return r, None

    def release(self, reservation: Reservation | None) -> None:
        """Call after the real cost is recorded (or the call failed): the hold goes away."""
        if reservation is not None:
            self._pending.pop(reservation.id, None)


def _over(label: str, cap: float, spent: float, pending: float, estimate: float) -> str | None:
    if spent >= cap:
        return f"{label} cap reached (${spent:.4f} >= ${cap:.2f})"
    if spent + pending + estimate > cap:
        return (f"{label} cap would be crossed (${spent:.4f} spent + ${pending:.4f} pending "
                f"+ ${estimate:.4f} this call > ${cap:.2f})")
    return None
