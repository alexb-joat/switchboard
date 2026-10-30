"""Command line entry point: python -m trioditis [serve|check|explain|hardware|usage|prices]."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import re
import stat
import time
from pathlib import Path

from . import __version__
from .envvars import env
from .config import ConfigError, load_config
from .security import assert_safe_bind

_ENV_LINE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$")


def load_dotenv(path: str | Path = ".env") -> list[str]:
    """Minimal, dependency-free .env loader (same behaviour on Linux and Windows).

    Only KEY=VALUE lines; never overrides variables already set in the
    environment; values are not executed or expanded.
    """
    p = Path(path)
    if not p.is_file():
        return []
    if os.name == "posix" and p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"warning: {p} is readable by other users; run: chmod 600 {p}", file=sys.stderr)
    loaded = []
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def main(argv: list[str] | None = None) -> None:
    load_dotenv(env("ENV_FILE", ".env"))
    ap = argparse.ArgumentParser(prog="trioditis")
    ap.add_argument("--config", default=env("CONFIG", "config.yaml"))
    ap.add_argument("--version", action="version", version=f"trioditis {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the router (default)")
    s.add_argument("--host", default=env("HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(env("PORT", "8787")))

    sub.add_parser("check", help="validate the config and show which models are usable")

    e = sub.add_parser("explain", help="show which model a prompt would go to (no model call)")
    e.add_argument("prompt")

    h = sub.add_parser("hardware", help="detect hardware and suggest local models (no config change)")
    h.add_argument("--json", action="store_true", help="machine-readable output")

    u = sub.add_parser("usage", help="calls, tokens and spend from the metrics database (no config needed)")
    u.add_argument("--since", default="24h", help="30m, 24h, 7d, today, month or YYYY-MM-DD (default 24h)")
    u.add_argument("--until", default=None, help="same forms as --since; default now")
    u.add_argument("--by", default="model", choices=["model", "route", "provider", "client", "session", "day"])
    u.add_argument("--session", default=None, help="only this X-Session-Id (matched by its hash)")
    u.add_argument("--db", default=None, help="metrics database (default ~/.hlidskjalv/metrics.db)")
    u.add_argument("--json", action="store_true", help="machine-readable output")

    pr = sub.add_parser("prices", help="manage ~/.hlidskjalv/prices.json")
    pr_sub = pr.add_subparsers(dest="prices_cmd", required=True)
    imp = pr_sub.add_parser("import", help="take cloud model prices from a provider-facts file, with their sources")
    imp.add_argument("facts", help="a provider-facts.v1 JSON file (for example Euboulia's src/catalog/provider-facts.json)")
    imp.add_argument("--prices", default=None, help="prices file to update (default ~/.hlidskjalv/prices.json)")
    imp.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")

    args = ap.parse_args(argv)
    if args.cmd == "usage":  # reads the metrics database only: works without a config
        sys.exit(usage_report(args))
    try:
        cfg = load_config(args.config)
    except (OSError, ConfigError) as err:
        sys.exit(f"config error: {err}")

    if args.cmd == "check":
        print(f"config OK: {len(cfg.models)} models, {len(cfg.routes)} routes")
        for name, m in cfg.models.items():
            p = cfg.providers[m.provider]
            state = "ready" if p.available() else f"missing ${p.api_key_env}"
            where = "local" if p.local else "cloud"
            print(f"  {name:<24} {where:<6} {state}")
        return

    if args.cmd == "prices":
        sys.exit(prices_import(cfg, args))

    if args.cmd == "explain":
        from .router import decide
        body = {"model": "auto", "messages": [{"role": "user", "content": args.prompt}]}
        # Keyword/rule signals only here, so the CLI never calls any network service.
        d = asyncio.run(decide(cfg, body))
        print(json.dumps(d.as_dict(), indent=2))
        return

    if args.cmd == "hardware":
        import httpx
        from .hardware import build_report, detect, format_report, installed_models, load_prices

        async def listed():
            # Only local providers are queried (GET /api/tags); no proxy, no redirects.
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as c:
                return await installed_models(cfg, c)

        inst, errs = asyncio.run(listed())
        rep = build_report(cfg, detect(), inst, errs, load_prices())
        print(json.dumps(rep, indent=2) if args.json else format_report(rep))
        return

    from trioditis_metrics.home import migrate_home
    migrate_home()  # ~/.arcade -> ~/.hlidskjalv, once (the pre-rename location)
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8787)
    assert_safe_bind(host)

    if sys.stderr is None or sys.stdout is None:
        # Started without a console (pythonw.exe on Windows, some launchers):
        # send server output to a private log file instead of nowhere.
        Path("logs").mkdir(exist_ok=True)
        fd = os.open("logs/server.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        stream = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream

    import uvicorn
    from .app import create_app

    uvicorn.run(
        create_app(cfg),
        host=host,
        port=port,
        server_header=False,
        date_header=False,
        proxy_headers=False,
        log_level="info",
    )


_AGO = re.compile(r"^(\d+)([mhdw])$")
_UNIT_S = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_when(text: str, now: float | None = None) -> float:
    """'30m', '24h', '7d', '2w' ago; 'today' or 'month' (local midnight); 'YYYY-MM-DD' (local midnight)."""
    now = time.time() if now is None else now
    text = text.strip().lower()
    if m := _AGO.match(text):
        return now - int(m.group(1)) * _UNIT_S[m.group(2)]
    t = time.localtime(now)
    if text == "today":
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    if text == "month":
        return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    try:
        d = time.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"cannot read {text!r}: use 30m, 24h, 7d, today, month or YYYY-MM-DD") from None
    return time.mktime((d.tm_year, d.tm_mon, d.tm_mday, 0, 0, 0, 0, 0, -1))


def prices_import(cfg, args) -> int:
    from trioditis_metrics.prices import DEFAULT_PRICES_PATH
    from .pricefacts import FactsError, plan_import, stale_warning, write_prices

    try:
        facts = json.loads(Path(args.facts).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        print(f"prices import: cannot read {args.facts}: {err}", file=sys.stderr)
        return 2
    path = Path(args.prices).expanduser() if args.prices else DEFAULT_PRICES_PATH
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        print(f"prices import: {path} is not valid JSON; fix or move it first", file=sys.stderr)
        return 2
    cloud = {name: m.upstream_id for name, m in cfg.models.items() if not cfg.providers[m.provider].local}
    try:
        table, changes, unmatched = plan_import(facts, cloud, current if isinstance(current, dict) else {})
    except FactsError as err:
        print(f"prices import: {err}", file=sys.stderr)
        return 2
    if warning := stale_warning(facts):
        print(f"warning: {warning}")
    for line in changes:
        print("  " + line)
    for name in unmatched:
        print(f"  {name} ({cloud[name]}): not in the facts file, left as it is")
    if not changes:
        print("nothing to import: no configured cloud model is in the facts file")
        return 1
    if args.dry_run:
        print(f"dry run: {path} not changed")
        return 0
    write_prices(path, table)
    print(f"wrote {path}")
    return 0


def usage_report(args) -> int:
    import sqlite3
    from trioditis_metrics.db import DEFAULT_PATH, open_readonly, usage
    from .security import fingerprint

    try:
        since = parse_when(args.since)
        until = parse_when(args.until) if args.until else None
    except ValueError as err:
        print(f"usage: {err}", file=sys.stderr)
        return 2
    path = Path(args.db).expanduser() if args.db else DEFAULT_PATH
    sfp = fingerprint(args.session) if args.session else None
    try:
        conn = open_readonly(path)
        try:
            rows = usage(conn, since, until, args.by, sfp)
        finally:
            conn.close()
    except sqlite3.Error:
        rows = None  # no database yet (Trioditis has not served a call), or not one of ours
    total = {k: sum((r[k] or 0) for r in rows or []) for k in
             ("calls", "errors", "tokens_in", "tokens_out", "cost", "unpriced", "fallbacks", "local_calls")}
    total["cost"] = round(total["cost"], 6)
    stamp = lambda ts: time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)) if ts else None  # noqa: E731
    if args.json:
        for r in rows or []:
            r["first"], r["last"] = stamp(r.pop("first_ts")), stamp(r.pop("last_ts"))
        print(json.dumps({"since": stamp(since), "until": stamp(until), "by": args.by, "session": sfp,
                          "database": str(path), "rows": rows or [], "total": total}, indent=2))
        return 0
    if rows is None:
        print(f"no calls recorded yet (no metrics database at {path})")
        return 0
    span = f"since {stamp(since)}" + (f" until {stamp(until)}" if until else "")
    print(f"usage by {args.by} {span}" + (f", session {args.session!r}" if args.session else ""))
    if not rows:
        print("no calls in this window")
        return 0
    head = (args.by, "calls", "errors", "tokens in", "tokens out", "cost USD", "unpriced")
    table = [head] + [(str(r["key"]) if r["key"] is not None else "-", r["calls"], r["errors"], r["tokens_in"],
                       r["tokens_out"], f"{r['cost']:.4f}", r["unpriced"]) for r in rows]
    table.append(("total", total["calls"], total["errors"], total["tokens_in"], total["tokens_out"],
                  f"{total['cost']:.4f}", total["unpriced"]))
    widths = [max(len(f"{row[i]:,}" if isinstance(row[i], int) else str(row[i])) for row in table)
              for i in range(len(head))]
    for n, row in enumerate(table):
        cells = [f"{v:,}" if isinstance(v, int) else str(v) for v in row]
        print("  ".join(c.ljust(widths[i]) if i == 0 else c.rjust(widths[i]) for i, c in enumerate(cells)))
        if n == 0 or n == len(table) - 2:
            print("  ".join("-" * w for w in widths))
    if total["unpriced"]:
        print(f"{total['unpriced']} cloud call(s) had no price: add them to ~/.hlidskjalv/prices.json")
    return 0


if __name__ == "__main__":
    main()
