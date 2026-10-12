"""SQLite-backed call history for Trioditis.

Replaces the plain decisions.jsonl log with a queryable store, so Trioditis
can answer "how much have I spent today / this month" before making a cloud
call, and so other tools (Hlidskjalv, future dashboards) can query
history without re-parsing a growing log file.

Stdlib only (sqlite3) -- no new dependency, matching the rest of the stack.

Usage inside Trioditis, once per process:

    from trioditis_metrics import MetricsDB
    db = MetricsDB()  # defaults to ~/.hlidskjalv/metrics.db

After each request completes (Trioditis already builds a dict with this
shape to write to decisions.jsonl -- pass that same object straight in):

    db.record({
        "route": "code", "model": "claude-sonnet", "local": False,
        "status": 200, "latency_ms": 2200,
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
        "cost": 0.0123, "sensitive": [], "failed_attempts": [],
    })

Before dispatching a cloud call, ask the budget guard (guard.py) whether
it's allowed -- see that module for the other half of Phase 1.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from .home import data_home

DEFAULT_PATH = data_home() / "metrics.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    route       TEXT NOT NULL,
    model       TEXT NOT NULL,
    local       INTEGER NOT NULL,
    status      INTEGER,
    latency_ms  INTEGER,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    cost        REAL,
    fallbacks   INTEGER NOT NULL DEFAULT 0,
    sensitive   TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_route ON calls(route);
CREATE TABLE IF NOT EXISTS provider_limits (
    provider    TEXT NOT NULL,
    name        TEXT NOT NULL,      -- requests | tokens | input_tokens | output_tokens | ...
    lim         INTEGER,
    remaining   INTEGER,
    reset_at    REAL,               -- epoch seconds, when the limit is fully replenished
    seen        REAL NOT NULL,
    PRIMARY KEY (provider, name)
);
"""
# Added after 0.2: older databases get it on open.
MIGRATIONS = ["ALTER TABLE calls ADD COLUMN provider TEXT"]


class MetricsDB:
    """One connection per process. Safe for a single writer (Trioditis's
    request handler) plus any number of read-only reporting queries run
    from elsewhere (a CLI report, a dashboard)."""

    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(SCHEMA)
            have = {r[1] for r in self._conn.execute("PRAGMA table_info(calls)")}
            for stmt in MIGRATIONS:
                if stmt.split("ADD COLUMN ")[1].split()[0] not in have:
                    self._conn.execute(stmt)
        if os.name == "posix":
            os.chmod(self.path, 0o600)

    def record(self, decision: dict, ts: float | None = None) -> int:
        """Store one completed call. `ts` defaults to now; pass it explicitly
        only for backfilling or tests."""
        usage = decision.get("usage") or {}
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO calls (ts, route, model, local, status, latency_ms, "
                "tokens_in, tokens_out, cost, fallbacks, sensitive, provider) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts if ts is not None else time.time(),
                    str(decision.get("route") or "?")[:40],
                    str(decision.get("model") or "none")[:60],
                    1 if decision.get("local") else 0,
                    decision.get("status"),
                    decision.get("latency_ms"),
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    decision.get("cost"),
                    len(decision.get("failed_attempts") or []),
                    json.dumps([str(s)[:30] for s in (decision.get("sensitive") or [])][:10]),
                    str(decision["provider"])[:40] if decision.get("provider") else None,
                ),
            )
            return cur.lastrowid

    def spend_since(self, since_ts: float, provider: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(cost), 0) AS s FROM calls WHERE ts >= ? AND local = 0"
        args: tuple = (since_ts,)
        if provider:
            sql += " AND provider = ?"
            args += (provider,)
        return float(self._conn.execute(sql, args).fetchone()["s"])

    def spend_today(self, now: float | None = None, provider: str | None = None) -> float:
        return self.spend_since(_start_of_day(now), provider)

    def spend_this_month(self, now: float | None = None, provider: str | None = None) -> float:
        return self.spend_since(_start_of_month(now), provider)

    def record_limits(self, provider: str, limits: dict[str, dict], seen: float | None = None) -> None:
        """Keep the latest rate-limit snapshot a provider sent back: {name: {limit, remaining, reset_at}}."""
        seen = seen if seen is not None else time.time()
        with self._conn:
            for name, v in limits.items():
                self._conn.execute(
                    "INSERT INTO provider_limits (provider, name, lim, remaining, reset_at, seen) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(provider, name) DO UPDATE SET lim=excluded.lim, remaining=excluded.remaining, "
                    "reset_at=excluded.reset_at, seen=excluded.seen",
                    (provider[:40], name[:40], v.get("limit"), v.get("remaining"), v.get("reset_at"), seen),
                )

    def limits(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM provider_limits ORDER BY provider, name")]

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM calls ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()


def _start_of_day(now: float | None = None) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _start_of_month(now: float | None = None) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))
