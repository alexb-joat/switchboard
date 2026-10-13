"""HTTP layer: an OpenAI-compatible API in front of many models."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from trioditis_metrics import BudgetGuard, MetricsDB, Prices
from trioditis_metrics.ratelimits import parse as parse_ratelimits
from trioditis_metrics.home import data_home

from . import __version__
from .envvars import env
from .config import Config
from .accounts import ACCOUNTS_EVERY_S, adapter_for, report as account_report
from .hardware import ReportCache
from .router import Decision, Embedder, RoutingError, SessionPins, all_text, decide
from .security import check_bearer, fingerprint, host_header_is_local, origin_is_local, router_api_key

# Upstream failures worth trying the next model for.
RETRYABLE = {404, 408, 409, 425, 429, 500, 502, 503, 504}


DEFAULT_HARDWARE_FILE = data_home() / "hardware.json"
DEFAULT_ACCOUNTS_FILE = data_home() / "accounts.json"
HARDWARE_EVERY_S = 600


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _error(status: int, message: str, etype: str = "router_error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": etype}}, status_code=status)


class DecisionLog:
    """Append-only JSONL of routing decisions. Metadata only unless opted in."""

    def __init__(self, path: str | None, log_prompts: bool):
        self.path = Path(path) if path else None
        self.log_prompts = log_prompts
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self.path.touch(mode=0o600)

    def write(self, record: dict) -> None:
        if not self.path:
            return
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


class SecurityLog:
    """Refused requests, next to the decision log (logs/security.jsonl). Hlidskjalv turns
    these into alerts. Method, path, client address and reason only: never keys or bodies."""

    def __init__(self, decisions_path: str | None):
        self.path = Path(decisions_path).with_name("security.jsonl") if decisions_path else None

    def write(self, request: Request, reason: str) -> None:
        if not self.path:
            return
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "method": request.method,
               "path": request.url.path[:200], "client": request.client.host if request.client else None,
               "reason": reason}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass


def create_app(cfg: Config, transport: httpx.AsyncBaseTransport | None = None,
               metrics: MetricsDB | None = None, budget: BudgetGuard | None = None,
               prices: Prices | None = None, hardware_file: Path | None = None,
               accounts_file: Path | None = None) -> FastAPI:
    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(cfg.limits.upstream_timeout_s, connect=10.0),
        follow_redirects=False,  # never let an upstream bounce us elsewhere
        trust_env=True,
    )
    embedder = Embedder(cfg, client) if cfg.embeddings else None
    pins = SessionPins(cfg.limits.session_ttl_s)
    log = DecisionLog(cfg.logging.path, cfg.logging.log_prompts)
    security = SecurityLog(cfg.logging.path)
    metrics = metrics or MetricsDB()  # one per process; ~/.hlidskjalv/metrics.db by default
    budget = budget or BudgetGuard(metrics)
    prices = prices or Prices()

    hw_cache = ReportCache()  # hardware once per process; installed models every 60 s
    hardware_file = hardware_file or DEFAULT_HARDWARE_FILE

    async def publish_hardware():
        """Keep ~/.hlidskjalv/hardware.json current so Hlidskjalv can show local-model
        suggestions without needing this router's API key."""
        while True:
            try:
                rep = await hw_cache.get(cfg, client)
                _write_private(hardware_file, json.dumps({**rep, "generated_at": time.time()}))
            except Exception:  # best-effort: never take the router down over a report
                pass
            await asyncio.sleep(HARDWARE_EVERY_S)

    accounts_file = accounts_file or DEFAULT_ACCOUNTS_FILE
    accounts: dict = {"providers": {}, "generated_at": 0.0}

    async def refresh_accounts() -> dict:
        accounts["providers"] = await account_report(cfg, client)
        accounts["generated_at"] = time.time()
        _write_private(accounts_file, json.dumps(accounts))
        return accounts

    async def publish_accounts():
        """Balances and spend from each provider's own account API, for Hlidskjalv. Only runs
        when a cloud provider with an adapter and a key is configured; never affects routing."""
        if not any(not p.local and p.available() and adapter_for(p) for p in cfg.providers.values()):
            return
        while True:
            try:
                await refresh_accounts()
            except Exception:
                pass
            await asyncio.sleep(ACCOUNTS_EVERY_S)

    @asynccontextmanager
    async def lifespan(_app):
        tasks = [asyncio.create_task(publish_hardware()), asyncio.create_task(publish_accounts())]
        yield
        for t in tasks:
            t.cancel()
        await client.aclose()

    # No interactive docs / schema endpoints: less surface, nothing to fingerprint.
    app = FastAPI(title="trioditis", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.cfg = cfg

    @app.middleware("http")
    async def guard(request: Request, call_next):
        key = router_api_key()
        if not key:
            # Without a key the router answers anyone who can reach 127.0.0.1, and a web page
            # open in the browser can: by DNS rebinding (a Host that isn't ours) or a plain
            # cross-site POST (a foreign Origin). Refuse both. With a key set, a page cannot
            # send it, so the key is the defence and remote UIs keep working.
            host = request.headers.get("host")
            if not host_header_is_local(host):
                security.write(request, f"foreign host {str(host)[:100]!r}")
                return _error(403, "forbidden host", "auth_error")
            origin = request.headers.get("origin")
            if origin is not None and not origin_is_local(origin):
                security.write(request, f"foreign origin {origin[:100]!r}")
                return _error(403, "forbidden origin", "auth_error")
        elif request.url.path != "/health" and not check_bearer(request.headers.get("authorization"), key):
            security.write(request, "invalid or missing API key")
            return _error(401, "invalid or missing API key", "auth_error")
        if request.method == "POST":
            ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                security.write(request, f"content type {ctype[:60]!r}")
                return _error(415, "Content-Type must be application/json")
            length = request.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > cfg.limits.max_body_bytes):
                return _error(413, "request body too large")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    async def read_json(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > cfg.limits.max_body_bytes:
            raise RoutingError(413, "request body too large")
        try:
            body = json.loads(raw)
        except ValueError:
            raise RoutingError(400, "body must be JSON")
        if not isinstance(body, dict):
            raise RoutingError(400, "body must be a JSON object")
        return body

    def session_of(request: Request, body: dict) -> str | None:
        sid = request.headers.get("x-session-id") or body.get("user")
        return str(sid)[:200] if sid else None

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models():
        now = int(time.time())
        data = [{"id": "auto", "object": "model", "created": now, "owned_by": "trioditis"}]
        data += [
            {"id": f"route/{r}", "object": "model", "created": now, "owned_by": "trioditis"}
            for r in cfg.routes
        ]
        data += [
            {"id": m, "object": "model", "created": now, "owned_by": cfg.models[m].provider}
            for m in cfg.models
            if cfg.provider_of(m).available()
        ]
        return {"object": "list", "data": data}

    @app.get("/v1/accounts")
    async def accounts_view():
        """Each cloud provider's own view of your account (balance, spend), freshly read."""
        try:
            return await refresh_accounts()
        except Exception:
            return accounts

    @app.get("/v1/hardware")
    async def hardware():
        """Hardware + local-model suggestions. Only local providers are queried."""
        return await hw_cache.get(cfg, client)

    @app.post("/v1/route")
    async def explain(request: Request):
        """Dry run: show which model would be chosen and why. Calls no model."""
        try:
            body = await read_json(request)
            d = await decide(cfg, body, embedder=embedder, pins=None,
                             session_id=None)
        except RoutingError as e:
            return _error(e.status, e.message)
        return d.as_dict()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        started = time.monotonic()
        try:
            body = await read_json(request)
            sid = session_of(request, body)
            d = await decide(cfg, body, embedder=embedder, pins=pins, session_id=sid)
        except RoutingError as e:
            return _error(e.status, e.message)

        stream = bool(body.get("stream"))
        attempts: list[dict] = []
        blocked: str | None = None  # the global budget, checked once before the first cloud call
        checked: dict[str, str | None] = {}  # provider -> block reason (global + that provider's own caps)
        for model in d.candidates:
            m = cfg.models[model]
            prov = cfg.providers[m.provider]
            if not prov.local:
                if m.provider not in checked:
                    checked[m.provider] = budget.check(m.provider)
                    blocked = blocked or checked[m.provider]
                if checked[m.provider]:
                    attempts.append({"model": model, "error": "budget_blocked", "reason": checked[m.provider]})
                    continue
            upstream_body = dict(body)
            upstream_body["model"] = m.upstream_id
            upstream_body.pop("user", None)  # don't forward session ids upstream
            if stream and prov.stream_usage:  # usage comes in the last event; without it a priced call counts $0
                upstream_body["stream_options"] = {**(upstream_body.get("stream_options") or {}), "include_usage": True}
            # Only our own headers go upstream: the client's Authorization (our
            # router key) must never be forwarded to a provider.
            headers = {"Content-Type": "application/json"}
            if prov.api_key():
                headers["Authorization"] = f"Bearer {prov.api_key()}"
            url = f"{prov.base_url}/chat/completions"
            try:
                req = client.build_request("POST", url, json=upstream_body, headers=headers)
                resp = await client.send(req, stream=stream)
            except httpx.HTTPError as e:
                attempts.append({"model": model, "error": type(e).__name__})
                continue
            if resp.status_code == 400 and stream and "stream_options" in upstream_body:
                detail = await resp.aread()
                await resp.aclose()
                if b"stream_options" in detail:  # a provider that doesn't know the option: once more without it
                    upstream_body.pop("stream_options", None)
                    try:
                        req = client.build_request("POST", url, json=upstream_body, headers=headers)
                        resp = await client.send(req, stream=stream)
                    except httpx.HTTPError as e:
                        attempts.append({"model": model, "error": type(e).__name__})
                        continue
                else:  # a 400 for another reason: the client's problem, passed through as for a non-stream call
                    _remember_limits(metrics, m.provider, resp.headers)
                    _log(log, metrics, prices, d, body, model, 400, attempts, started, sid, local=prov.local,
                         usage=_usage_from_json(detail), provider=m.provider)
                    return _passthrough(resp, detail, {"X-Trioditis-Route": d.route, "X-Trioditis-Model": model})
            _remember_limits(metrics, m.provider, resp.headers)

            if resp.status_code in RETRYABLE:
                attempts.append({"model": model, "status": resp.status_code})
                await resp.aclose()
                continue

            route_headers = {
                "X-Trioditis-Route": d.route,
                "X-Trioditis-Model": model,
            }
            local = prov.local
            if not stream:
                content = resp.content
                await resp.aclose()
                _log(log, metrics, prices, d, body, model, resp.status_code, attempts, started, sid,
                     local=local, usage=_usage_from_json(content), provider=m.provider)
                return _passthrough(resp, content, route_headers)

            def done(usage, _model=model, _status=resp.status_code, _local=local, _prov=m.provider):
                _log(log, metrics, prices, d, body, _model, _status, attempts, started, sid,
                     local=_local, usage=usage, provider=_prov)

            return StreamingResponse(
                _relay(resp, done),
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "text/event-stream"),
                headers=route_headers,
            )

        if blocked and not any(cfg.providers[cfg.models[mn].provider].local
                               for mn in d.candidates):
            _log(log, metrics, prices, d, body, None, 429, attempts, started, sid)
            return _error(429, f"cloud budget exceeded: {blocked}; no local model "
                               f"available for route {d.route}", "budget_exceeded")
        _log(log, metrics, prices, d, body, None, 502, attempts, started, sid)
        return _error(502, f"all candidate models failed: {attempts}", "upstream_error")

    return app


def _passthrough(resp: httpx.Response, content: bytes, extra: dict) -> JSONResponse:
    try:
        payload = json.loads(content)
    except ValueError:
        payload = {"error": {"message": content[:500].decode("utf-8", "replace"),
                             "type": "upstream_error"}}
    return JSONResponse(payload, status_code=resp.status_code, headers=extra)


def _usage_from_json(content: bytes) -> dict | None:
    try:
        u = json.loads(content).get("usage")
    except (ValueError, AttributeError):
        return None
    return _clean_usage(u)


def _clean_usage(u) -> dict | None:
    """Keep only the token counters (numbers), nothing else from the upstream."""
    if not isinstance(u, dict):
        return None
    out = {k: u[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
           if isinstance(u.get(k), int)}
    return out or None


def _usage_from_sse(tail: bytes) -> dict | None:
    """Streaming: the usage block, when the provider sends one, is in the last events."""
    for line in reversed(tail.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if not line.startswith("data:") or '"usage"' not in line:
            continue
        try:
            u = _clean_usage(json.loads(line[5:].strip()).get("usage"))
        except (ValueError, AttributeError):
            continue
        if u:
            return u
    return None


async def _relay(resp: httpx.Response, on_done=None):
    tail = b""
    try:
        async for chunk in resp.aiter_raw():
            tail = (tail + chunk)[-16384:]
            yield chunk
    finally:
        await resp.aclose()
        if on_done:
            on_done(_usage_from_sse(tail))


def _remember_limits(metrics: MetricsDB, provider: str, headers) -> None:
    """Rate-limit headers from an upstream response -> the metrics DB (Hlidskjalv shows them)."""
    try:
        limits = parse_ratelimits(headers)
        if limits:
            metrics.record_limits(provider, limits)
    except (sqlite3.Error, ValueError, TypeError):
        pass


def _log(log: DecisionLog, metrics: MetricsDB, prices: Prices, d: Decision, body: dict, model,
         status, attempts, started, sid, local: bool | None = None,
         usage: dict | None = None, provider: str | None = None) -> None:
    text = all_text(body.get("messages") or [])
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "route": d.route,
        "model": model,
        "status": status,
        "failed_attempts": attempts,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "reasons": d.reasons,
        "sensitive": d.sensitive,
        "prompt_fp": fingerprint(text),
        "prompt_chars": len(text),
        "session_fp": fingerprint(sid) if sid else None,
        "local": local,
        "usage": usage,
    }
    if log.log_prompts:
        rec["prompt"] = text
    log.write(rec)
    try:
        # Cost goes to the metrics DB only; decisions.jsonl keeps its format
        # (Hlidskjalv prices it itself from the same prices.json).
        cost = prices.cost(model, local, usage)
        metrics.record({**rec, "cost": cost, "provider": provider,
                        "unpriced": bool(local is False and cost is None and usage and isinstance(status, int) and status < 400)})
    except sqlite3.Error:
        pass  # metrics are best-effort; never fail (or break a stream) over them


def app_from_env() -> FastAPI:
    from .config import load_config
    return create_app(load_config(env("CONFIG", "config.yaml")))
