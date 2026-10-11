"""SQLite-backed call history for Switchboard.

Replaces the plain decisions.jsonl log with a queryable store, so Switchboard
can answer "how much have I spent today / this month" before making a cloud
call, and so other tools (Workflow Arcade, future dashboards) can query
history without re-parsing a growing log file.

Stdlib only (sqlite3) -- no new dependency, matching the rest of the stack.

Usage inside Switchboard, once per process:

    from switchboard_metrics import MetricsDB
    db = MetricsDB()  # defaults to ~/.arcade/metrics.db

After each request completes (Switchboard already builds a dict with this
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

DEFAULT_PATH = Path(os.environ.get("ARCADE_HOME") or Path.home() / ".arcade") / "metrics.db"

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
"""


class MetricsDB:
    """One connection per process. Safe for a single writer (Switchboard's
    request handler) plus any number of read-only reporting queries run
    from elsewhere (a CLI report, a dashboard)."""

    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(SCHEMA)
        if os.name == "posix":
            os.chmod(self.path, 0o600)

    def record(self, decision: dict, ts: float | None = None) -> int:
        """Store one completed call. `ts` defaults to now; pass it explicitly
        only for backfilling or tests."""
        usage = decision.get("usage") or {}
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO calls (ts, route, model, local, status, latency_ms, "
                "tokens_in, tokens_out, cost, fallbacks, sensitive) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
                ),
            )
            return cur.lastrowid

    def spend_since(self, since_ts: float) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost), 0) AS s FROM calls WHERE ts >= ? AND local = 0",
            (since_ts,),
        ).fetchone()
        return float(row["s"])

    def spend_today(self, now: float | None = None) -> float:
        return self.spend_since(_start_of_day(now))

    def spend_this_month(self, now: float | None = None) -> float:
        return self.spend_since(_start_of_month(now))

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
