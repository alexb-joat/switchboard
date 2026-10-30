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
    sensitive   TEXT NOT NULL DEFAULT '[]',
    provider    TEXT,
    unpriced    INTEGER NOT NULL DEFAULT 0   -- a cloud call with usage but no price in prices.json
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
# Added after 0.2: older databases get them on open.
MIGRATIONS = ["ALTER TABLE calls ADD COLUMN provider TEXT",
              "ALTER TABLE calls ADD COLUMN unpriced INTEGER NOT NULL DEFAULT 0",
              "ALTER TABLE calls ADD COLUMN session_fp TEXT",   # 0.4: hash of X-Session-Id / user, never the id
              "ALTER TABLE calls ADD COLUMN client TEXT"]       # 0.4: the calling tool's name (X-Trioditis-Client, User-Agent)

# What `usage()` can group by; a column an older database lacks reads as NULL.
GROUPS = {
    "model": "model",
    "route": "route",
    "provider": "provider",
    "client": "client",
    "session": "session_fp",
    "day": "date(ts, 'unixepoch', 'localtime')",
}


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
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_session ON calls(session_fp)")
        if os.name == "posix":
            os.chmod(self.path, 0o600)

    def record(self, decision: dict, ts: float | None = None) -> int:
        """Store one completed call. `ts` defaults to now; pass it explicitly
        only for backfilling or tests."""
        usage = decision.get("usage") or {}
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO calls (ts, route, model, local, status, latency_ms, "
                "tokens_in, tokens_out, cost, fallbacks, sensitive, provider, unpriced, session_fp, client) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    1 if decision.get("unpriced") else 0,
                    str(decision["session_fp"])[:32] if decision.get("session_fp") else None,
                    str(decision["client"])[:40] if decision.get("client") else None,
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

    def unpriced_since(self, since_ts: float, provider: str | None = None) -> int:
        """Cloud calls that answered but had no price: the caps can't see their cost."""
        sql = "SELECT COUNT(*) AS n FROM calls WHERE ts >= ? AND local = 0 AND unpriced = 1"
        args: tuple = (since_ts,)
        if provider:
            sql += " AND provider = ?"
            args += (provider,)
        return int(self._conn.execute(sql, args).fetchone()["n"])

    def unpriced_today(self, now: float | None = None, provider: str | None = None) -> int:
        return self.unpriced_since(_start_of_day(now), provider)

    def unpriced_this_month(self, now: float | None = None, provider: str | None = None) -> int:
        return self.unpriced_since(_start_of_month(now), provider)

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

    def usage(self, since: float, until: float | None = None, by: str = "model",
              session_fp: str | None = None) -> list[dict]:
        return usage(self._conn, since, until, by, session_fp)

    def limits(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM provider_limits ORDER BY provider, name")]

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM calls ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()


def open_readonly(path: Path) -> sqlite3.Connection:
    """For reports: never creates, migrates or locks the database Trioditis writes."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def usage(conn: sqlite3.Connection, since: float, until: float | None = None, by: str = "model",
          session_fp: str | None = None) -> list[dict]:
    """Calls, errors, tokens and cost per group between `since` and `until` (epoch seconds),
    most expensive first. Works on databases from before 0.4 (no session or client yet)."""
    if by not in GROUPS:
        raise ValueError(f"cannot group by {by!r}; choose from {', '.join(GROUPS)}")
    have = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
    key = GROUPS[by]
    if key in ("provider", "client", "session_fp") and key not in have:
        key = "NULL"
    where, args = "ts >= ?", [since]
    if until is not None:
        where += " AND ts < ?"
        args.append(until)
    if session_fp is not None:
        if "session_fp" not in have:
            return []
        where += " AND session_fp = ?"
        args.append(session_fp)
    unpriced = "SUM(unpriced)" if "unpriced" in have else "0"
    rows = conn.execute(
        f"SELECT {key} AS key, COUNT(*) AS calls, "
        "SUM(CASE WHEN status IS NULL OR status >= 400 THEN 1 ELSE 0 END) AS errors, "
        "COALESCE(SUM(tokens_in), 0) AS tokens_in, COALESCE(SUM(tokens_out), 0) AS tokens_out, "
        f"ROUND(COALESCE(SUM(cost), 0), 6) AS cost, {unpriced} AS unpriced, SUM(fallbacks) AS fallbacks, "
        "SUM(local) AS local_calls, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
        f"FROM calls WHERE {where} GROUP BY 1 ORDER BY cost DESC, calls DESC, key",
        args,
    ).fetchall()
    return [dict(r) for r in rows]


def _start_of_day(now: float | None = None) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _start_of_month(now: float | None = None) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))
