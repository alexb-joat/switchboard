"""HTTP layer: an OpenAI-compatible API in front of many models."""

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .config import Config
from .router import Decision, Embedder, RoutingError, SessionPins, all_text, decide
from .security import check_bearer, fingerprint, router_api_key

# Upstream failures worth trying the next model for.
RETRYABLE = {404, 408, 409, 425, 429, 500, 502, 503, 504}


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


def create_app(cfg: Config, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(cfg.limits.upstream_timeout_s, connect=10.0),
        follow_redirects=False,  # never let an upstream bounce us elsewhere
        trust_env=True,
    )
    embedder = Embedder(cfg, client) if cfg.embeddings else None
    pins = SessionPins(cfg.limits.session_ttl_s)
    log = DecisionLog(cfg.logging.path, cfg.logging.log_prompts)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await client.aclose()

    # No interactive docs / schema endpoints: less surface, nothing to fingerprint.
    app = FastAPI(title="switchboard", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.cfg = cfg

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path != "/health":
            key = router_api_key()
            if key and not check_bearer(request.headers.get("authorization"), key):
                return _error(401, "invalid or missing API key", "auth_error")
        if request.method == "POST":
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
        data = [{"id": "auto", "object": "model", "created": now, "owned_by": "switchboard"}]
        data += [
            {"id": f"route/{r}", "object": "model", "created": now, "owned_by": "switchboard"}
            for r in cfg.routes
        ]
        data += [
            {"id": m, "object": "model", "created": now, "owned_by": cfg.models[m].provider}
            for m in cfg.models
            if cfg.provider_of(m).available()
        ]
        return {"object": "list", "data": data}

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
        for model in d.candidates:
            m = cfg.models[model]
            prov = cfg.providers[m.provider]
            upstream_body = dict(body)
            upstream_body["model"] = m.upstream_id
            upstream_body.pop("user", None)  # don't forward session ids upstream
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

            if resp.status_code in RETRYABLE:
                attempts.append({"model": model, "status": resp.status_code})
                await resp.aclose()
                continue

            route_headers = {
                "X-Switchboard-Route": d.route,
                "X-Switchboard-Model": model,
            }
            _log(log, d, body, model, resp.status_code, attempts, started, sid)
            if not stream:
                content = resp.content
                await resp.aclose()
                return _passthrough(resp, content, route_headers)
            return StreamingResponse(
                _relay(resp),
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "text/event-stream"),
                headers=route_headers,
            )

        _log(log, d, body, None, 502, attempts, started, sid)
        return _error(502, f"all candidate models failed: {attempts}", "upstream_error")

    return app


def _passthrough(resp: httpx.Response, content: bytes, extra: dict) -> JSONResponse:
    try:
        payload = json.loads(content)
    except ValueError:
        payload = {"error": {"message": content[:500].decode("utf-8", "replace"),
                             "type": "upstream_error"}}
    return JSONResponse(payload, status_code=resp.status_code, headers=extra)


async def _relay(resp: httpx.Response):
    try:
        async for chunk in resp.aiter_raw():
            yield chunk
    finally:
        await resp.aclose()


def _log(log: DecisionLog, d: Decision, body: dict, model, status, attempts, started, sid) -> None:
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
    }
    if log.log_prompts:
        rec["prompt"] = text
    log.write(rec)


def app_from_env() -> FastAPI:
    from .config import load_config
    return create_app(load_config(os.environ.get("SWITCHBOARD_CONFIG", "config.yaml")))
