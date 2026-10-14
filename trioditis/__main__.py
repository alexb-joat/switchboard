"""Command line entry point: python -m trioditis [serve|check|explain|hardware]."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import re
import stat
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

    args = ap.parse_args(argv)
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


if __name__ == "__main__":
    main()
