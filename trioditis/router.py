"""Routing decisions: which model should answer this request, and why."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from .config import Config, Route
from .security import detect_sensitive


class RoutingError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Decision:
    route: str
    candidates: list[str]  # first = primary, rest = fallbacks
    reasons: list[str] = field(default_factory=list)
    sensitive: list[str] = field(default_factory=list)  # detector names only
    local_only: bool = False
    estimates: dict[str, dict] = field(default_factory=dict)  # model -> {"usd": ..., "p50_ms": ...} when ordered by them

    def as_dict(self) -> dict:
        out = {
            "route": self.route,
            "model": self.candidates[0] if self.candidates else None,
            "fallbacks": self.candidates[1:],
            "reasons": self.reasons,
            "sensitive": self.sensitive,
            "local_only": self.local_only,
        }
        if self.estimates:
            out["estimates"] = self.estimates
        return out


# ---------------------------------------------------------------- request facts

def _parts(content) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [p for p in content if isinstance(p, dict)]
    return []


def all_text(messages: list) -> str:
    out = []
    for m in messages:
        for p in _parts(m.get("content")):
            if p.get("type") in ("text", "input_text"):
                out.append(str(p.get("text", "")))
    return "\n".join(out)


def last_user_text(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return "\n".join(
                str(p.get("text", "")) for p in _parts(m.get("content"))
                if p.get("type") in ("text", "input_text")
            )
    return ""


def has_images(messages: list) -> bool:
    return any(
        p.get("type") in ("image_url", "input_image")
        for m in messages for p in _parts(m.get("content"))
    )


def estimate_tokens(body: dict) -> int:
    # ~4 chars/token is a deliberately rough, dependency-free estimate; tool definitions count too.
    text = all_text(body.get("messages") or [])
    tools = json.dumps(body.get("tools") or [])
    return (len(text) + len(tools)) // 4 + int(body.get("max_tokens") or body.get("max_completion_tokens") or 1024)


def tool_names(body: dict) -> list[str]:
    """Names of the tools a request offers (OpenAI function tools), lower-cased."""
    names = []
    for t in body.get("tools") or []:
        if isinstance(t, dict):
            fn = t.get("function") if isinstance(t.get("function"), dict) else t
            if isinstance(fn.get("name"), str):
                names.append(fn["name"].lower()[:80])
    return names[:128]


def agent_key(messages: list) -> str | None:
    """A conversation key for an agent that sends no session id: the same system prompt and
    first request means the same task. Kept in memory only, never logged."""
    system = "\n".join(str(p.get("text", "")) for m in messages if m.get("role") == "system"
                       for p in _parts(m.get("content")) if p.get("type") in ("text", "input_text"))
    first = next((m for m in messages if m.get("role") == "user"), None)
    if first is None:
        return None
    first_text = "\n".join(str(p.get("text", "")) for p in _parts(first.get("content"))
                           if p.get("type") in ("text", "input_text"))
    return "agent:" + hashlib.sha256((system + "\x00" + first_text).encode("utf-8", "ignore")).hexdigest()[:24]


# ---------------------------------------------------------------- signals

def keyword_score(text: str, route: Route) -> int:
    low = text.lower()
    score = 0
    for kw in route.keywords:
        if re.search(r"(?<![\w])" + re.escape(kw) + r"(?![\w])", low):
            score += 1
    for rx in route.patterns:
        if rx.search(text):
            score += 2
    return score


def tool_score(names: list[str], route: Route) -> tuple[int, list[str]]:
    """+2 for each tool whose name contains one of the route's tool keywords."""
    hits = [n for n in names if any(k in n for k in route.tool_keywords)]
    return 2 * len(hits), hits


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):  # vectors from two different embedding models: not comparable
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class Embedder:
    """Semantic signal via any OpenAI-compatible /embeddings endpoint (local only)."""

    def __init__(self, cfg: Config, client: httpx.AsyncClient):
        self.cfg = cfg
        self.client = client
        self._route_vecs: dict[str, list[list[float]]] | None = None

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        e = self.cfg.embeddings
        prov = self.cfg.providers[e.provider]
        headers = {}
        if prov.api_key():
            headers["Authorization"] = f"Bearer {prov.api_key()}"
        r = await self.client.post(
            f"{prov.base_url}/embeddings",
            json={"model": e.model, "input": texts},
            headers=headers,
            timeout=15.0,
        )
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]

    async def similarities(self, text: str) -> dict[str, float]:
        if self._route_vecs is None:
            vecs: dict[str, list[list[float]]] = {}
            for name, route in self.cfg.routes.items():
                if route.examples:
                    vecs[name] = await self._embed(list(route.examples))
            self._route_vecs = vecs
        if not self._route_vecs or not text.strip():
            return {}
        [q] = await self._embed([text[:4000]])
        return {
            name: max(_cosine(q, v) for v in vs)
            for name, vs in self._route_vecs.items()
        }


class Cooldowns:
    """Providers that asked us to wait (429/503 with Retry-After, or a rate limit at zero):
    tried last until then, never dropped. In memory: a restart forgets them."""

    MAX_S = 3600

    def __init__(self):
        self._until: dict[str, float] = {}

    def wait(self, provider: str, seconds: float | None = None, until: float | None = None) -> None:
        now = time.time()
        t = until if until is not None else now + float(seconds or 0)
        t = min(t, now + self.MAX_S)
        if t > now and t > self._until.get(provider, 0.0):
            self._until[provider] = t

    def until(self, provider: str) -> float | None:
        t = self._until.get(provider)
        if t is None:
            return None
        if t <= time.time():
            del self._until[provider]
            return None
        return t


@dataclass
class Signals:
    """What the router may use to order a route's models beyond the configured order."""
    prices: object | None = None  # trioditis_metrics.Prices: .estimate(model, tokens_in, tokens_out)
    latency: Callable[[str], int | None] | None = None  # model -> median latency in ms, recent successful calls
    cooldowns: Cooldowns | None = None


class SessionPins:
    """Keep one route per conversation so agents don't switch models mid-task."""

    def __init__(self, ttl_s: int, max_items: int = 10_000):
        self.ttl = ttl_s
        self.max = max_items
        self._d: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def get(self, sid: str) -> str | None:
        item = self._d.get(sid)
        if not item:
            return None
        route, exp = item
        if exp < time.monotonic():
            self._d.pop(sid, None)
            return None
        return route

    def set(self, sid: str, route: str) -> None:
        self._d[sid] = (route, time.monotonic() + self.ttl)
        self._d.move_to_end(sid)
        while len(self._d) > self.max:
            self._d.popitem(last=False)


# ---------------------------------------------------------------- decision

def _eligible(cfg: Config, model: str, *, vision: bool, tools: bool, tokens: int, local_only: bool) -> str | None:
    """Return None if eligible, else a short reason it was skipped."""
    m = cfg.models[model]
    prov = cfg.providers[m.provider]
    if not prov.available():
        return "no API key"
    if local_only and not prov.local:
        return "cloud model blocked (local-only)"
    if vision and "vision" not in m.capabilities:
        return "no vision"
    if tools and "tools" not in m.capabilities:
        return "no tool calling"
    if tokens > m.context_tokens:
        return "context too small"
    return None


async def decide(
    cfg: Config,
    body: dict,
    *,
    embedder: Embedder | None = None,
    pins: SessionPins | None = None,
    session_id: str | None = None,
    signals: Signals | None = None,
) -> Decision:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RoutingError(400, "messages must be a non-empty list")

    requested = str(body.get("model") or "auto")
    vision = has_images(messages)
    tools = bool(body.get("tools"))
    tokens = estimate_tokens(body)
    reasons: list[str] = []

    sensitive = detect_sensitive(all_text(messages), cfg.privacy)
    local_only = bool(sensitive)
    if sensitive:
        reasons.append(f"sensitive content ({', '.join(sensitive)}) -> local models only")

    # 1. Which route (or explicit model)?
    explicit_model = None
    if requested in cfg.models:
        explicit_model = requested
        route_name = "explicit"
        reasons.append(f"client asked for {requested}")
    elif requested.startswith("route/"):
        route_name = requested[6:]
        if route_name not in cfg.routes:
            raise RoutingError(404, f"unknown route {route_name!r}")
        reasons.append(f"client forced route {route_name}")
    elif requested == "auto":
        # An agent that sends no session id is still one conversation: pin it by its task.
        pin_key = session_id or (agent_key(messages) if tools else None)
        pinned = pins.get(pin_key) if (pins and pin_key) else None
        if pinned and pinned in cfg.routes:
            route_name = pinned
            reasons.append(f"session pinned to {pinned}" if session_id else f"agent conversation pinned to {pinned}")
        else:
            route_name = await _classify(cfg, last_user_text(messages), embedder, reasons, tool_names(body))
            if pins and pin_key:
                pins.set(pin_key, route_name)
    elif named := [n for n, m in cfg.models.items() if m.upstream_id == requested]:
        # The provider's own id ("claude-haiku-5-5") pins an exact version, where a config
        # name ("claude-haiku") may move to a newer one; useful for evaluations.
        if len(named) > 1:
            raise RoutingError(400, f"model id {requested!r} is configured more than once ({', '.join(named)}); "
                                    "ask for one of those names")
        explicit_model = named[0]
        route_name = "explicit"
        reasons.append(f"client asked for {requested} (model {explicit_model})")
    else:
        raise RoutingError(404, f"unknown model {requested!r}")

    # 2. Candidate models, filtered by what this request needs.
    if explicit_model:
        preferred = [explicit_model]
    else:
        route = cfg.routes[route_name]
        preferred = list(route.models)
        if route.local_only:
            local_only = True
            reasons.append(f"route {route_name} is local-only")
    if vision:
        reasons.append("image attached -> needs vision")
    if tools:
        reasons.append("tools present -> needs tool calling")

    def filt(models):
        keep = []
        for mn in models:
            why = _eligible(cfg, mn, vision=vision, tools=tools, tokens=tokens, local_only=local_only)
            if why is None:
                keep.append(mn)
            else:
                reasons.append(f"skip {mn}: {why}")
        return keep

    candidates = filt(preferred)
    if not candidates and not explicit_model:
        # Only models some route lists, or local ones: a model configured for calling by name
        # (an expensive one, say) is never reached by surprise.
        in_routes = {mn for r in cfg.routes.values() for mn in r.models}
        others = [m for m in cfg.models if m not in preferred
                  and (m in in_routes or cfg.providers[cfg.models[m].provider].local)]
        candidates = [
            m for m in others
            if _eligible(cfg, m, vision=vision, tools=tools, tokens=tokens, local_only=local_only) is None
        ]
        if candidates:
            reasons.append("no route model fits; using another route's capable model")

    estimates: dict[str, dict] = {}
    if signals and not explicit_model and len(candidates) > 1:
        prefer = cfg.routes[route_name].prefer
        if prefer == "cheapest":
            candidates, estimates = _by_cost(cfg, body, candidates, signals, reasons)
        elif prefer == "fastest":
            candidates, estimates = _by_speed(candidates, signals, reasons)
    if signals and signals.cooldowns and len(candidates) > 1:
        candidates = _cooled_last(cfg, candidates, signals.cooldowns, reasons)

    if not candidates:
        if sensitive and cfg.privacy.on_no_local_model == "reject":
            raise RoutingError(
                422,
                "Sensitive content detected and no local model can serve this request. "
                "Remove the sensitive data or add a local model.",
            )
        raise RoutingError(503, "no configured model can serve this request")

    return Decision(
        route=route_name,
        candidates=candidates[: 1 + cfg.limits.max_fallbacks],
        reasons=reasons,
        sensitive=sensitive,
        local_only=local_only,
        estimates=estimates,
    )


def prompt_tokens(body: dict) -> int:
    """Rough prompt size: about four characters per token, messages plus tool definitions."""
    return (len(all_text(body.get("messages") or [])) + len(json.dumps(body.get("tools") or []))) // 4


def _by_cost(cfg: Config, body: dict, candidates: list[str], signals: Signals, reasons: list[str]):
    """Local models first ($0), then cloud models by this request's estimated cost; unpriced last."""
    tin = prompt_tokens(body)
    tout = int(body.get("max_completion_tokens") or body.get("max_tokens") or 1024)
    est: dict[str, dict] = {}

    def key(m: str):
        if cfg.providers[cfg.models[m].provider].local:
            est[m] = {"usd": 0.0}
            return (0, 0.0)
        usd = signals.prices.estimate(m, tin, tout) if signals.prices is not None else None
        est[m] = {"usd": usd}
        return (1, usd) if usd is not None else (2, 0.0)

    ordered = sorted(candidates, key=key)  # stable: ties keep the configured order
    unpriced = [m for m in ordered if est[m]["usd"] is None]
    reasons.append("prefer cheapest: local first, then by estimated cost"
                   + (f"; no price for {', '.join(unpriced)} (last)" if unpriced else ""))
    return ordered, est


def _by_speed(candidates: list[str], signals: Signals, reasons: list[str]):
    """By median latency of recent successful calls; models with no data yet keep their order, last."""
    est = {m: {"p50_ms": signals.latency(m) if signals.latency else None} for m in candidates}
    ordered = sorted(candidates, key=lambda m: (0, est[m]["p50_ms"]) if est[m]["p50_ms"] is not None else (1, 0))
    unknown = [m for m in ordered if est[m]["p50_ms"] is None]
    reasons.append("prefer fastest: by median latency of recent calls"
                   + (f"; no data yet for {', '.join(unknown)} (last)" if unknown else ""))
    return ordered, est


def _cooled_last(cfg: Config, candidates: list[str], cooldowns: Cooldowns, reasons: list[str]) -> list[str]:
    ready, waiting = [], []
    for m in candidates:
        until = cooldowns.until(cfg.models[m].provider)
        if until is None:
            ready.append(m)
        else:
            waiting.append(m)
            reasons.append(f"{m}: rate-limited until {time.strftime('%H:%M:%S', time.localtime(until))}, tried last")
    return ready + waiting


async def _classify(cfg: Config, text: str, embedder: Embedder | None, reasons: list[str],
                    tools: list[str] | None = None) -> str:
    kw, tool_hits = {}, {}
    for name, r in cfg.routes.items():
        ts, hits = tool_score(tools or [], r)
        kw[name], tool_hits[name] = keyword_score(text, r) + ts, hits
    best_kw = max(kw.values()) if kw else 0

    sims: dict[str, float] = {}
    if embedder is not None:
        try:
            sims = await embedder.similarities(text)
        except Exception as e:  # semantic signal is optional; never fail a request on it
            reasons.append(f"embeddings unavailable ({type(e).__name__}); keywords only")

    if best_kw > 0:
        top = [n for n, s in kw.items() if s == best_kw]
        name = max(top, key=lambda n: sims.get(n, 0.0))
        hits = tool_hits.get(name) or []
        via = f"; tools: {', '.join(hits[:6])}" if hits else ""
        reasons.append(f"keywords matched route {name} (score {best_kw}{via})")
        return name

    if sims:
        name, sim = max(sims.items(), key=lambda kv: kv[1])
        if sim >= cfg.embeddings.threshold:
            reasons.append(f"semantic match route {name} (similarity {sim:.2f})")
            return name
        reasons.append(f"best semantic match {name} {sim:.2f} below threshold")

    reasons.append(f"no strong signal -> default route {cfg.default_route}")
    return cfg.default_route
