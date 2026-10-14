import json
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from trioditis.app import create_app
from trioditis.config import ConfigError, parse_config
from trioditis.router import RoutingError, SessionPins, decide
from trioditis.security import assert_safe_bind, detect_sensitive
from trioditis_metrics import BudgetCaps, BudgetGuard, MetricsDB, Prices

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


BASE = {
    "providers": {
        "local": {"base_url": "http://localhost:11434/v1", "local": True},
        "cloud": {"base_url": "https://api.example.com/v1", "api_key_env": "CLOUD_KEY"},
    },
    "models": {
        "small": {"provider": "local", "id": "llama3.2:3b", "capabilities": ["tools"]},
        "coder": {"provider": "local", "id": "qwen-coder"},
        "big": {"provider": "cloud", "id": "big-1", "capabilities": ["vision", "tools"],
                "context_tokens": 200000},
    },
    "routes": {
        "code": {"models": ["big", "coder"], "keywords": ["python", "bug"], "patterns": ["```"]},
        "quick": {"models": ["small"]},
    },
    "default_route": "quick",
    "logging": {"path": None},
}


def cfg(**over):
    raw = json.loads(json.dumps(BASE))
    raw.update(over)
    return parse_config(raw)


def body(text, **kw):
    return {"model": "auto", "messages": [{"role": "user", "content": text}], **kw}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLOUD_KEY", "cloud-secret")
    monkeypatch.delenv("TRIODITIS_API_KEY", raising=False)
    monkeypatch.delenv("SWITCHBOARD_API_KEY", raising=False)


# ------------------------------------------------------------------ config

@pytest.mark.parametrize("bad, msg", [
    ({"providers": {"x": {"base_url": "https://a.com", "api_key": "sk-123"}}}, "api_key_env"),
    ({"providers": {"x": {"base_url": "http://evil.com/v1"}}}, "plain http"),
    ({"providers": {"x": {"base_url": "https://u:p@a.com"}}}, "credentials"),
    ({"providers": {"x": {"base_url": "file:///etc/passwd"}}}, "http"),
])
def test_config_rejects_unsafe_providers(bad, msg):
    with pytest.raises(ConfigError, match=msg):
        cfg(**bad)


def test_config_rejects_cloud_embeddings():
    with pytest.raises(ConfigError, match="local provider"):
        cfg(embeddings={"provider": "cloud", "model": "e"})


def test_config_rejects_unknown_route_model():
    with pytest.raises(ConfigError, match="unknown model"):
        cfg(routes={"r": {"models": ["nope"]}}, default_route="r")


# ------------------------------------------------------------------ privacy

def test_detectors():
    p = cfg().privacy
    assert "email" in detect_sensitive("mail me at a.b@corp.com", p)
    assert "card_number" in detect_sensitive("card 4111 1111 1111 1111", p)
    assert "card_number" not in detect_sensitive("order 1234 5678 9012 3456", p)  # fails Luhn
    assert "api_key" in detect_sensitive("key sk-ant-abcdefghijklmnopqrstuvwxyz", p)
    assert detect_sensitive("what's the weather like", p) == []


# ------------------------------------------------------------------ routing

async def test_keyword_routing_prefers_first_model():
    d = await decide(cfg(), body("I have a python bug"))
    assert d.route == "code" and d.candidates == ["big", "coder"]


async def test_default_route():
    d = await decide(cfg(), body("hello"))
    assert d.route == "quick" and d.candidates == ["small"]


async def test_missing_key_skips_cloud(monkeypatch):
    monkeypatch.delenv("CLOUD_KEY")
    d = await decide(cfg(), body("python bug"))
    assert d.candidates == ["coder"]


async def test_sensitive_forces_local():
    d = await decide(cfg(), body("python bug, password: hunter2"))
    assert d.local_only and d.candidates == ["coder"]


async def test_sensitive_without_local_is_rejected():
    c = cfg(routes={"code": {"models": ["big"]}}, default_route="code",
            models={"big": BASE["models"]["big"]})
    with pytest.raises(RoutingError) as e:
        await decide(c, body("email jane@corp.com about it"))
    assert e.value.status == 422


async def test_image_needs_vision():
    msg = {"role": "user", "content": [{"type": "text", "text": "what is this"},
                                         {"type": "image_url", "image_url": {"url": "data:,"}}]}
    d = await decide(cfg(), {"model": "auto", "messages": [msg]})
    assert d.candidates == ["big"]  # quick route had no vision model -> global fallback


async def test_explicit_and_forced_route():
    assert (await decide(cfg(), body("x", model="coder"))).candidates == ["coder"]
    assert (await decide(cfg(), body("hello", model="route/code"))).route == "code"
    with pytest.raises(RoutingError):
        await decide(cfg(), body("x", model="gpt-unknown"))


async def test_session_pinning():
    pins = SessionPins(60)
    c = cfg()
    d1 = await decide(c, body("python bug"), pins=pins, session_id="s1")
    d2 = await decide(c, body("thanks, and now?"), pins=pins, session_id="s1")
    assert d1.route == d2.route == "code"


def test_bind_guard(monkeypatch):
    assert_safe_bind("127.0.0.1")
    with pytest.raises(SystemExit):
        assert_safe_bind("0.0.0.0")
    monkeypatch.setenv("TRIODITIS_API_KEY", "short")
    with pytest.raises(SystemExit):
        assert_safe_bind("0.0.0.0")
    monkeypatch.setenv("TRIODITIS_API_KEY", "x" * 40)
    assert_safe_bind("0.0.0.0")


# ------------------------------------------------------------------ HTTP

def make_client(handler, config=None, metrics=None, caps=None, prices=None, accounts_file=None):
    # Always a throwaway DB, explicit caps and prices: tests must never touch ~/.hlidskjalv.
    transport = httpx.MockTransport(handler)
    tmp = Path(tempfile.mkdtemp())
    metrics = metrics or MetricsDB(tmp / "metrics.db")
    budget = BudgetGuard(metrics, caps or BudgetCaps())
    prices = prices or Prices(tmp / "prices.json")
    # base_url: the router answers only to its own names (127.0.0.1, localhost) without a key.
    return TestClient(create_app(config or cfg(), transport=transport, metrics=metrics, budget=budget,
                                 prices=prices, hardware_file=tmp / "hardware.json",
                                 accounts_file=accounts_file or tmp / "accounts.json"),
                      base_url="http://127.0.0.1:8787")


def overspent_db():
    db = MetricsDB(Path(tempfile.mkdtemp()) / "metrics.db")
    db.record({"route": "code", "model": "big", "local": False, "status": 200, "cost": 5.0})
    return db


def ok(model):
    return httpx.Response(200, json={"id": "x", "model": model, "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi"}}]})


def test_chat_routes_and_fallbacks():
    seen = []

    def handler(req: httpx.Request):
        sent = json.loads(req.content)
        seen.append((req.url.host, sent["model"], req.headers.get("authorization")))
        if sent["model"] == "big-1":
            return httpx.Response(503, json={"error": "overloaded"})
        return ok(sent["model"])

    r = make_client(handler).post("/v1/chat/completions", json=body("python bug"))
    assert r.status_code == 200
    assert r.headers["x-trioditis-model"] == "coder"
    assert seen[0] == ("api.example.com", "big-1", "Bearer cloud-secret")
    assert seen[1] == ("localhost", "qwen-coder", None)  # local: no key sent


def test_router_key_required_and_never_forwarded(monkeypatch):
    monkeypatch.setenv("TRIODITIS_API_KEY", "router-key-" + "x" * 30)
    captured = {}

    def handler(req):
        captured["auth"] = req.headers.get("authorization")
        return ok("x")

    c = make_client(handler)
    assert c.post("/v1/chat/completions", json=body("hello")).status_code == 401
    assert c.get("/v1/models").status_code == 401
    assert c.get("/health").status_code == 200
    r = c.post("/v1/chat/completions", json=body("hello"),
               headers={"Authorization": "Bearer router-key-" + "x" * 30})
    assert r.status_code == 200
    assert captured["auth"] is None  # local model gets no auth; router key not leaked


def test_streaming_relay():
    sse = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for part in sse.split(b"\n\n")[:-1]:
                yield part + b"\n\n"

    def handler(req):
        return httpx.Response(200, stream=Chunks(), headers={"content-type": "text/event-stream"})

    r = make_client(handler).post("/v1/chat/completions", json=body("hello", stream=True))
    assert r.status_code == 200 and r.content == sse


def test_decision_log_records_usage_not_content(tmp_path):
    logf = tmp_path / "decisions.jsonl"
    sse = (b'data: {"choices":[{"delta":{"content":"secret answer"}}]}\n\n'
           b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3,'
           b'"total_tokens":15,"note":"x"}}\n\ndata: [DONE]\n\n')

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse

    def handler(req):
        if json.loads(req.content).get("stream"):
            return httpx.Response(200, stream=Chunks(), headers={"content-type": "text/event-stream"})
        r = ok("x").json()
        r["usage"] = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
        return httpx.Response(200, json=r)

    c = make_client(handler, cfg(logging={"path": str(logf)}))
    c.post("/v1/chat/completions", json=body("hello there"))
    c.post("/v1/chat/completions", json=body("hello again", stream=True))
    recs = [json.loads(line) for line in logf.read_text().splitlines()]
    assert recs[0]["usage"] == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    assert recs[1]["usage"] == {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
    assert recs[0]["local"] is True
    raw = logf.read_text()
    assert "hello" not in raw and "secret answer" not in raw


def test_all_fail_returns_502():
    def handler(req):
        raise httpx.ConnectError("down")

    r = make_client(handler).post("/v1/chat/completions", json=body("hello"))
    assert r.status_code == 502


def test_body_limit_and_bad_json():
    c = make_client(lambda r: ok("x"), cfg(limits={"max_body_bytes": 100}))
    assert c.post("/v1/chat/completions", json=body("a" * 500)).status_code == 413
    assert c.post("/v1/chat/completions", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400


def test_models_list_hides_unavailable(monkeypatch):
    monkeypatch.delenv("CLOUD_KEY")
    ids = [m["id"] for m in make_client(lambda r: ok("x")).get("/v1/models").json()["data"]]
    assert "auto" in ids and "route/code" in ids and "small" in ids and "big" not in ids


def test_dry_run_calls_no_model():
    def handler(req):
        raise AssertionError("no upstream call expected")

    r = make_client(handler).post("/v1/route", json=body("python bug"))
    assert r.json()["model"] == "big"


def test_no_docs_endpoints():
    c = make_client(lambda r: ok("x"))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404


def test_dotenv_loader(tmp_path, monkeypatch):
    from trioditis.__main__ import load_dotenv
    env = tmp_path / ".env"
    env.write_text('# comment\nNEW_KEY="abc"\nEMPTY_KEY=\nCLOUD_KEY=override\nnot a line\nlower=x\n')
    loaded = load_dotenv(env)
    import os
    assert loaded == ["NEW_KEY"] and os.environ["NEW_KEY"] == "abc"
    assert "EMPTY_KEY" not in os.environ
    assert os.environ["CLOUD_KEY"] == "cloud-secret"  # existing env wins
    monkeypatch.delenv("NEW_KEY")


# ------------------------------------------------------------------ metrics / budget

def test_metrics_db_records_each_call():
    db = MetricsDB(Path(tempfile.mkdtemp()) / "metrics.db")
    c = make_client(lambda req: ok("x"), metrics=db)
    c.post("/v1/chat/completions", json=body("hello"))
    [row] = db.recent()
    assert (row["route"], row["model"], row["local"], row["status"]) == ("quick", "small", 1, 200)


def test_over_budget_skips_cloud_and_falls_back_to_local(tmp_path):
    logf = tmp_path / "decisions.jsonl"
    seen = []

    def handler(req):
        seen.append(req.url.host)
        return ok("x")

    c = make_client(handler, cfg(logging={"path": str(logf)}), metrics=overspent_db())
    r = c.post("/v1/chat/completions", json=body("python bug"))
    assert r.status_code == 200
    assert r.headers["x-trioditis-model"] == "coder"
    assert seen == ["localhost"]  # cloud never contacted
    [rec] = [json.loads(line) for line in logf.read_text().splitlines()]
    [attempt] = rec["failed_attempts"]
    assert attempt["model"] == "big" and attempt["error"] == "budget_blocked"
    assert "daily cap" in attempt["reason"]


def test_over_budget_without_local_fallback_returns_clear_error():
    config = cfg(routes={"cloudy": {"models": ["big"]}}, default_route="cloudy")
    c = make_client(lambda req: pytest.fail("cloud must not be called"), config,
                    metrics=overspent_db())
    r = c.post("/v1/chat/completions", json=body("hello"))
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "budget_exceeded"
    assert "daily cap" in r.json()["error"]["message"]


def test_under_budget_cloud_is_used():
    seen = []

    def handler(req):
        seen.append(req.url.host)
        return ok("x")

    c = make_client(handler, metrics=overspent_db(), caps=BudgetCaps(daily_usd=10, monthly_usd=100))
    assert c.post("/v1/chat/completions", json=body("python bug")).status_code == 200
    assert seen == ["api.example.com"]


def test_cloud_cost_recorded_from_prices_and_counts_toward_budget(tmp_path):
    pf = tmp_path / "prices.json"
    pf.write_text(json.dumps({"_note": "x", "big": {"input": 3.0, "output": 15.0}}))
    db = MetricsDB(tmp_path / "metrics.db")

    def handler(req):
        r = ok("x").json()
        r["usage"] = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}
        return httpx.Response(200, json=r)

    c = make_client(handler, metrics=db, prices=Prices(pf))
    c.post("/v1/chat/completions", json=body("python bug"))   # cloud: big
    c.post("/v1/chat/completions", json=body("hello"))        # local: small
    rows = {r["model"]: r for r in db.recent()}
    assert rows["big"]["cost"] == pytest.approx(0.0045)        # 1000*3/1e6 + 100*15/1e6
    assert rows["small"]["cost"] == 0.0
    assert db.spend_today() == pytest.approx(0.0045)


def test_prices_reload_and_unpriced_is_none(tmp_path):
    pf = tmp_path / "prices.json"
    prices = Prices(pf)
    use = {"prompt_tokens": 10, "completion_tokens": 10}
    assert prices.cost("big", False, use) is None             # no file yet
    pf.write_text(json.dumps({"big": {"input": 1, "output": 1}}))
    assert prices.cost("big", False, use) == pytest.approx(0.00002)
    assert prices.cost("big", False, None) is None             # no usage reported
    assert prices.cost("other", True, None) == 0.0             # local is always free


def test_budget_caps_reload_without_restart(tmp_path, monkeypatch):
    import trioditis_metrics.guard as guard_mod
    cap_file = tmp_path / "budget.json"
    monkeypatch.setattr(guard_mod, "DEFAULT_CONFIG_PATH", cap_file)
    guard = BudgetGuard(overspent_db())                        # $5 spent today
    assert "daily cap" in guard.check()                        # default $1/day
    cap_file.write_text(json.dumps({"daily_usd": 10, "monthly_usd": 100}))
    assert guard.check() is None


def test_refused_requests_are_logged_without_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIODITIS_API_KEY", "router-key-" + "x" * 30)
    c = make_client(lambda req: ok("x"), cfg(logging={"path": str(tmp_path / "decisions.jsonl")}))
    assert c.post("/v1/chat/completions", json=body("hi"), headers={"Authorization": "Bearer wrong-guess"}).status_code == 401
    [rec] = [json.loads(line) for line in (tmp_path / "security.jsonl").read_text().splitlines()]
    assert rec["path"] == "/v1/chat/completions" and rec["reason"] == "invalid or missing API key"
    assert "wrong-guess" not in (tmp_path / "security.jsonl").read_text()


def test_browser_pages_cannot_reach_a_keyless_router(tmp_path, monkeypatch):
    """No key means anyone on 127.0.0.1 is trusted, so a web page must not count as one:
    DNS rebinding shows up as a foreign Host, a cross-site POST as a foreign Origin."""
    from trioditis.security import host_header_is_local, origin_is_local
    for good in ("127.0.0.1:8787", "localhost", "[::1]:8787", "trioditis.localhost:8787", "127.0.0.2"):
        assert host_header_is_local(good), good
    for bad in ("evil.example", "192.168.1.5:8787", "", None, "localhost.evil.example", "::1:8787"):
        assert not host_header_is_local(bad), bad
    assert origin_is_local("http://localhost:3000") and origin_is_local("https://127.0.0.1")
    for bad in ("http://evil.example", "null", "file://", "", "http://localhost.evil.example"):
        assert not origin_is_local(bad), bad

    c = make_client(lambda req: ok("x"), cfg(logging={"path": str(tmp_path / "decisions.jsonl")}))
    h = {"Host": "evil.example:8787"}
    assert c.post("/v1/chat/completions", json=body("hi"), headers=h).status_code == 403
    assert c.get("/health", headers=h).status_code == 403  # nothing answers a rebound page, not even health
    assert c.post("/v1/chat/completions", json=body("hi"), headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/v1/chat/completions", json=body("hi"), headers={"Origin": "http://localhost:3000"}).status_code == 200  # a local UI
    assert c.post("/v1/chat/completions", json=body("hi")).status_code == 200  # a plain client sends no Origin
    reasons = [json.loads(line)["reason"] for line in (tmp_path / "security.jsonl").read_text().splitlines()]
    assert reasons == ["foreign host 'evil.example:8787'", "foreign host 'evil.example:8787'", "foreign origin 'http://evil.example'"]

    # A form or text/plain POST is what a page can send without a CORS preflight: refused always.
    r = c.post("/v1/chat/completions", content=json.dumps(body("hi")), headers={"Content-Type": "text/plain"})
    assert r.status_code == 415 and "application/json" in r.text
    assert c.post("/v1/chat/completions", content=json.dumps(body("hi")),
                  headers={"Content-Type": "application/json; charset=utf-8"}).status_code == 200

    # With a key, the key is the defence: a page cannot send it, and a remote UI that has it may call from anywhere.
    monkeypatch.setenv("TRIODITIS_API_KEY", "router-key-" + "x" * 30)
    auth = {"Authorization": "Bearer router-key-" + "x" * 30}
    assert c.post("/v1/chat/completions", json=body("hi"), headers={**h, **auth, "Origin": "http://ui.example"}).status_code == 200
    assert c.post("/v1/chat/completions", json=body("hi"), headers={**h, "Origin": "http://ui.example"}).status_code == 401


def test_old_env_names_still_work_for_now(monkeypatch):
    from trioditis.envvars import env
    monkeypatch.delenv("TRIODITIS_CONFIG", raising=False)
    monkeypatch.setenv("SWITCHBOARD_CONFIG", "old.yaml")
    assert env("CONFIG") == "old.yaml"
    monkeypatch.setenv("TRIODITIS_CONFIG", "new.yaml")
    assert env("CONFIG") == "new.yaml"


def test_data_home_moves_once_and_leaves_a_link(tmp_path, monkeypatch):
    import os
    from trioditis_metrics.home import data_home, migrate_home
    monkeypatch.delenv("HLIDSKJALV_HOME", raising=False)
    monkeypatch.delenv("ARCADE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".arcade").mkdir()
    (tmp_path / ".arcade" / "metrics.db").write_text("x")
    assert data_home() == tmp_path / ".arcade"          # nothing moves on import or lookup
    assert migrate_home() == tmp_path / ".hlidskjalv"
    assert (tmp_path / ".hlidskjalv" / "metrics.db").read_text() == "x"
    if os.name == "posix":
        assert (tmp_path / ".arcade").is_symlink()       # old paths (shell hooks) keep working
    assert migrate_home() == tmp_path / ".hlidskjalv"   # second time: nothing to do
    assert data_home() == tmp_path / ".hlidskjalv"


# ------------------------------------------------------------------ rate limits, per-provider caps, accounts

def test_ratelimit_headers_parse_all_three_families():
    from trioditis_metrics.ratelimits import parse
    now = 1_800_000_000.0
    a = parse({"anthropic-ratelimit-requests-limit": "1000", "anthropic-ratelimit-requests-remaining": "999",
               "anthropic-ratelimit-requests-reset": "2027-01-15T10:00:30Z",
               "anthropic-ratelimit-input-tokens-remaining": "1999000", "content-type": "application/json"}, now)
    from datetime import datetime, timezone
    assert a["requests"] == {"limit": 1000, "remaining": 999,
                             "reset_at": datetime(2027, 1, 15, 10, 0, 30, tzinfo=timezone.utc).timestamp()}
    assert a["input_tokens"]["remaining"] == 1999000
    o = parse({"x-ratelimit-limit-tokens": "30000", "x-ratelimit-remaining-tokens": "29000",
               "x-ratelimit-reset-tokens": "6m0s", "x-ratelimit-reset-requests": "12ms"}, now)
    assert o["tokens"] == {"limit": 30000, "remaining": 29000, "reset_at": now + 360}
    assert o["requests"]["reset_at"] == pytest.approx(now + 0.012)
    r = parse({"X-RateLimit-Limit": "20", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int((now + 30) * 1000))}, now)
    assert r["requests"] == {"limit": 20, "remaining": 0, "reset_at": now + 30}
    assert parse({"x-ratelimit-limit-tokens": "-1"}, now) == {}  # Azure's "unknown"
    assert parse({"content-type": "text/plain"}, now) == {}


def test_old_metrics_db_gains_the_provider_column(tmp_path):
    import sqlite3
    db = tmp_path / "m.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, route TEXT NOT NULL, "
                "model TEXT NOT NULL, local INTEGER NOT NULL, status INTEGER, latency_ms INTEGER, tokens_in INTEGER, "
                "tokens_out INTEGER, cost REAL, fallbacks INTEGER NOT NULL DEFAULT 0, sensitive TEXT NOT NULL DEFAULT '[]')")
    con.execute("INSERT INTO calls (ts, route, model, local, cost) VALUES (1, 'code', 'big', 0, 0.5)")
    con.commit(); con.close()
    m = MetricsDB(db)
    m.record({"route": "code", "model": "big", "local": False, "cost": 0.25, "provider": "cloud"})
    assert m.spend_since(0) == pytest.approx(0.75)
    assert m.spend_since(0, provider="cloud") == pytest.approx(0.25)
    m.record_limits("cloud", {"requests": {"limit": 10, "remaining": 3, "reset_at": 5.0}})
    m.record_limits("cloud", {"requests": {"limit": 10, "remaining": 2, "reset_at": 6.0}})
    [row] = m.limits()
    assert (row["provider"], row["name"], row["remaining"], row["reset_at"]) == ("cloud", "requests", 2, 6.0)


def test_per_provider_caps(tmp_path):
    cap_file = tmp_path / "budget.json"
    cap_file.write_text(json.dumps({"daily_usd": 10, "monthly_usd": 100, "providers": {"cloud": {"daily_usd": 0.5}, "x": "junk"}}))
    caps = BudgetCaps.load(cap_file)
    assert caps.providers == {"cloud": {"daily_usd": 0.5}}
    db = MetricsDB(tmp_path / "m.db")
    db.record({"route": "code", "model": "big", "local": False, "cost": 0.6, "provider": "cloud"})
    guard = BudgetGuard(db, caps)
    assert guard.check() is None                         # global caps fine
    assert guard.check("other") is None                  # no own caps
    assert "cloud daily cap" in guard.check("cloud")     # its own cap hit


def test_router_records_provider_and_rate_limits(tmp_path):
    db = MetricsDB(tmp_path / "m.db")

    def handler(req):
        r = ok("x").json()
        r["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return httpx.Response(200, json=r, headers={"anthropic-ratelimit-requests-limit": "50",
                                                     "anthropic-ratelimit-requests-remaining": "49",
                                                     "anthropic-ratelimit-requests-reset": "2027-01-01T00:00:00Z"})
    c = make_client(handler, metrics=db)
    assert c.post("/v1/chat/completions", json=body("python bug")).status_code == 200
    [row] = db.recent()
    assert (row["model"], row["provider"]) == ("big", "cloud")
    [lim] = db.limits()
    assert (lim["provider"], lim["name"], lim["remaining"]) == ("cloud", "requests", 49)


def test_provider_cap_blocks_only_that_provider(tmp_path):
    db = MetricsDB(tmp_path / "m.db")
    db.record({"route": "code", "model": "big", "local": False, "cost": 5.0, "provider": "cloud"})
    seen = []

    def handler(req):
        seen.append(req.url.host)
        return ok("x")
    c = make_client(handler, metrics=db, caps=BudgetCaps(daily_usd=100, monthly_usd=100, providers={"cloud": {"monthly_usd": 1}}))
    r = c.post("/v1/chat/completions", json=body("python bug"))
    assert r.status_code == 200 and r.headers["x-trioditis-model"] == "coder"
    assert seen == ["localhost"]


def test_account_adapters(tmp_path, monkeypatch):
    monkeypatch.setenv("OR_KEY", "sk-or-xxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setenv("OR_ADMIN", "sk-or-mgmt-xxxxxxxxxxxxxxxx")
    monkeypatch.setenv("ANTHROPIC_ADMIN", "sk-ant-admin01-xxxxxxxxxxxx")
    monkeypatch.setenv("OPENAI_ADMIN", "sk-admin-xxxxxxxxxxxxxxxxxx")
    monkeypatch.setenv("CLOUD_KEY", "cloud-secret")
    config = cfg(providers={
        "local": {"base_url": "http://localhost:11434/v1", "local": True},
        "cloud": {"base_url": "https://api.example.com/v1", "api_key_env": "CLOUD_KEY"},
        "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OR_KEY", "admin_key_env": "OR_ADMIN"},
        "anthropic": {"base_url": "https://api.anthropic.com/v1", "api_key_env": "CLOUD_KEY", "admin_key_env": "ANTHROPIC_ADMIN"},
        "openai": {"base_url": "https://api.openai.com/v1", "api_key_env": "CLOUD_KEY", "admin_key_env": "OPENAI_ADMIN"},
        "nokey": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "UNSET_KEY_ENV"},
    })
    calls = []

    def handler(req):
        calls.append((req.url.host, req.url.path, req.headers.get("authorization") or req.headers.get("x-api-key")))
        if req.url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": {"usage": 12.5, "usage_monthly": 3.25, "usage_daily": 0.1, "limit": 20,
                                                       "limit_remaining": 7.5, "is_free_tier": False, "effective_budget": None}})
        if req.url.path == "/api/v1/credits":
            return httpx.Response(200, json={"data": {"total_credits": 100.5, "total_usage": 25.75}})
        if req.url.path == "/v1/organizations/cost_report":
            assert req.headers["anthropic-version"] == "2023-06-01"
            return httpx.Response(200, json={"data": [{"results": [{"currency": "USD", "amount": "123.45"}, {"currency": "USD", "amount": "6.55"}]}],
                                             "has_more": False})
        if req.url.path == "/v1/organization/costs":
            return httpx.Response(200, json={"data": [{"results": [{"amount": {"value": 0.42, "currency": "usd"}}]}], "has_more": False})
        return httpx.Response(404)

    out = tmp_path / "accounts.json"
    c = make_client(handler, config, accounts_file=out)
    r = c.get("/v1/accounts").json()
    p = r["providers"]
    assert p["openrouter"]["label"] == "OpenRouter balance" and p["openrouter"]["remaining"] == pytest.approx(74.75)
    assert p["openrouter"]["used_month"] == 3.25
    assert p["anthropic"] == {**p["anthropic"], "kind": "spend", "used": 1.3}      # cents -> dollars
    assert p["openai"]["used"] == pytest.approx(0.42)
    assert p["nokey"]["error"] == "no API key set"
    assert p["cloud"]["error"].startswith("no adapter")
    assert "local" not in p
    assert json.loads(out.read_text())["providers"]["openai"]["used"] == pytest.approx(0.42)
    # keys only ever go to the provider's own host, and the admin key only to the admin endpoints
    assert all(h in ("openrouter.ai", "api.anthropic.com", "api.openai.com") for h, _, _ in calls)
    assert [a for h, path, a in calls if path == "/api/v1/credits"] == ["Bearer sk-or-mgmt-xxxxxxxxxxxxxxxx"]


def test_streaming_asks_for_usage_and_retries_without_it_once():
    sse = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: {"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
    seen = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for part in sse.split(b"\n\n")[:-1]:
                yield part + b"\n\n"

    def handler(req):
        sent = json.loads(req.content)
        seen.append(sent.get("stream_options"))
        if sent.get("stream_options"):
            return httpx.Response(400, json={"error": {"message": "Unrecognized request argument supplied: stream_options"}})
        return httpx.Response(200, stream=Chunks(), headers={"content-type": "text/event-stream"})

    r = make_client(handler).post("/v1/chat/completions", json=body("hello", stream=True))
    assert r.status_code == 200 and r.content == sse
    assert seen == [{"include_usage": True}, None]  # asked once, then retried without

    from dataclasses import replace
    config = cfg()
    for name, p in list(config.providers.items()):
        config.providers[name] = replace(p, stream_usage=False)
    seen.clear()
    make_client(handler, config=config).post("/v1/chat/completions", json=body("hello", stream=True))
    assert seen == [None]  # switched off per provider: never asked

    def other_400(req):  # a 400 for another reason is passed through, not retried
        seen.append(1)
        return httpx.Response(400, json={"error": {"message": "max_tokens too large"}})
    seen.clear()
    r = make_client(other_400).post("/v1/chat/completions", json=body("hello", stream=True))
    assert r.status_code == 400 and len(seen) == 1 and b"max_tokens" in r.content


def test_unpriced_cloud_calls_are_recorded_and_can_count_toward_caps():
    from trioditis_metrics.guard import BudgetCaps, BudgetGuard

    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    tmp = Path(tempfile.mkdtemp())
    metrics = MetricsDB(tmp / "metrics.db")
    c = make_client(handler, config=cfg(), metrics=metrics)
    assert c.post("/v1/chat/completions", json=body("fix my python script", model="route/code")).status_code == 200
    rows = metrics.recent(5)
    assert rows and rows[0]["unpriced"] == 1 and rows[0]["cost"] is None  # a cloud call, answered, no price known
    assert metrics.unpriced_today() == 1
    guard = BudgetGuard(metrics, BudgetCaps(daily_usd=1.0, assume_unpriced_usd=2.0))
    assert "daily cap reached" in (guard.check("cloud") or "")
    assert BudgetGuard(metrics, BudgetCaps(daily_usd=1.0)).check("cloud") is None  # default: unpriced counts $0, as documented


# ------------------------------------------------------------------ VS Code (Cline) shapes

def cline_turn(name="first"):
    return json.loads((Path(__file__).parent / "fixtures" / "cline_request.json").read_text())["turns"][name]


def test_cline_request_streams_tool_calls_and_usage(tmp_path):
    """What Cline 4.1 sends: a 4 KB system prompt, six tools with tool_choice auto, a short user line,
    streaming with usage requested. The router must forward the tools, keep the stream intact,
    log usage and never the prompt, and keep the client's key to itself."""
    seen = []
    sse = (b'data: {"id":"c","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_1",'
           b'"type":"function","function":{"name":"editor","arguments":""}}]},"finish_reason":null}]}\n\n'
           b'data: {"id":"c","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":'
           b'"{\\"command\\":\\"create\\",\\"path\\":\\"hello.txt\\",\\"content\\":\\"hi\\\\n\\"}"}}]},"finish_reason":null}]}\n\n'
           b'data: {"id":"c","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
           b'data: {"id":"c","choices":[],"usage":{"prompt_tokens":3200,"completion_tokens":40,"total_tokens":3240}}\n\n'
           b'data: [DONE]\n\n')

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(0, len(sse), 64):
                yield sse[i:i + 64]

    def handler(req):
        seen.append((req.url.path, req.headers.get("authorization"), json.loads(req.content)))
        return httpx.Response(200, stream=Chunks(), headers={"content-type": "text/event-stream"})

    c = make_client(handler, cfg(logging={"path": str(tmp_path / "decisions.jsonl")}))
    turn = cline_turn()
    r = c.post("/v1/chat/completions", json=turn, headers={"Authorization": "Bearer my-router-key", **{"User-Agent": "ai-sdk/openai-compatible/3.0.37"}})
    assert r.status_code == 200 and r.content == sse  # the stream is relayed byte for byte, [DONE] included
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["X-Trioditis-Route"] == "quick" and r.headers["X-Trioditis-Model"] == "small"  # 30 chars of task text: the default route, and small can call tools
    path, auth, up = seen[0]
    assert auth is None  # the client's router key never goes upstream
    assert [t["function"]["name"] for t in up["tools"]] == [t["function"]["name"] for t in turn["tools"]]
    assert up["tool_choice"] == "auto" and up["stream"] is True and up["stream_options"] == {"include_usage": True}
    assert up["model"] == "llama3.2:3b" and len(up["messages"]) == 2 and up["messages"][0]["content"] == turn["messages"][0]["content"]
    [rec] = [json.loads(line) for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert rec["usage"] == {"prompt_tokens": 3200, "completion_tokens": 40, "total_tokens": 3240}
    assert "hello.txt" not in (tmp_path / "decisions.jsonl").read_text() and "ACT MODE" not in (tmp_path / "decisions.jsonl").read_text()
    assert "tools present -> needs tool calling" in rec["reasons"]

    # The follow-up turn carries the assistant's reply and a new user line; still one upstream call, same route.
    r = c.post("/v1/chat/completions", json=cline_turn("followup"))
    assert r.status_code == 200 and len(seen) == 2 and len(seen[1][2]["messages"]) == 4


def test_cline_tools_skip_models_that_cannot_call_them():
    """`coder` has no tools capability: with Cline's tools attached the code route must skip it, say why,
    and fall through to any model that can call tools rather than send a request coder cannot honour."""
    seen = []

    def handler(req):
        seen.append(json.loads(req.content)["model"])
        return ok("x")

    turn = {**cline_turn(), "model": "route/code", "stream": False}
    turn["messages"][-1]["content"] = "fix the python bug in main.py"
    import os
    os.environ.pop("CLOUD_KEY", None)  # only local models: big (tools) is unavailable, coder has none
    r = make_client(handler).post("/v1/chat/completions", json=turn)
    assert r.status_code == 200 and seen == ["llama3.2:3b"] and r.headers["X-Trioditis-Model"] == "small"
    d = make_client(handler).post("/v1/route", json=turn).json()
    assert "skip coder: no tool calling" in d["reasons"] and "no route model fits; using any capable model" in d["reasons"]


def test_truncated_prompt_is_flagged_from_reported_usage(tmp_path):
    """Ollama cuts a prompt past its context window without saying so; the only trace is a prompt_tokens
    count far below what was sent. The decision log marks that, with a reason Hlidskjalv can show."""
    reported = [120, 1300, 3]

    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                         "usage": {"prompt_tokens": reported.pop(0), "completion_tokens": 5}})

    c = make_client(handler, cfg(logging={"path": str(tmp_path / "decisions.jsonl")}))
    turn = {**cline_turn(), "stream": False}  # ~1,500 tokens of system prompt and tools
    assert c.post("/v1/chat/completions", json=turn).status_code == 200
    assert c.post("/v1/chat/completions", json=turn).status_code == 200
    cut, fine = [json.loads(line) for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert cut["prompt_truncated"] is True and any("OLLAMA_CONTEXT_LENGTH" in r for r in cut["reasons"])
    assert "prompt_truncated" not in fine and not any("cut" in r for r in fine["reasons"])
    assert c.post("/v1/chat/completions", json=body("hi")).status_code == 200  # a tiny prompt is never judged


def test_gzipped_upstream_stream_reaches_the_client_decoded(tmp_path):
    """Anthropic's streams arrive gzip-compressed. The router sends its own headers (no Content-Encoding),
    so relaying the raw bytes gave Cline garbage ("stream ended without a finish reason") and no usage."""
    import gzip
    sse = (b'data: {"id":"c","choices":[{"index":0,"delta":{"role":"assistant","content":"hi"},"finish_reason":null}]}\n\n'
           b'data: {"id":"c","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
           b'data: {"id":"c","choices":[],"usage":{"prompt_tokens":2100,"completion_tokens":7,"total_tokens":2107}}\n\n'
           b'data: [DONE]\n\n')
    packed = gzip.compress(sse)

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(0, len(packed), 16):
                yield packed[i:i + 16]

    def handler(req):
        return httpx.Response(200, stream=Chunks(), headers={"content-type": "text/event-stream", "content-encoding": "gzip"})

    c = make_client(handler, cfg(logging={"path": str(tmp_path / "decisions.jsonl")}))
    r = c.post("/v1/chat/completions", json=body("hello", stream=True))
    assert r.status_code == 200 and r.content == sse and "content-encoding" not in r.headers
    [rec] = [json.loads(line) for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert rec["usage"] == {"prompt_tokens": 2100, "completion_tokens": 7, "total_tokens": 2107}


def test_chunked_body_over_the_limit_is_refused_and_logged(tmp_path):
    """No Content-Length (chunked) skips the up-front check; the read itself must stop at the cap."""
    c = make_client(lambda r: ok("x"), cfg(limits={"max_body_bytes": 1000}, logging={"path": str(tmp_path / "decisions.jsonl")}))

    def chunks():
        for _ in range(50):
            yield b"a" * 100  # 5,000 bytes in all, sent without a Content-Length

    r = c.post("/v1/chat/completions", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    r = c.post("/v1/chat/completions", json=body("a" * 5000))  # declared length: refused before reading
    assert r.status_code == 413
    reasons = [json.loads(line)["reason"] for line in (tmp_path / "security.jsonl").read_text().splitlines()]
    assert reasons[0] == "body too large (streamed past the limit)" and reasons[1].startswith("body too large (declared")
    assert c.post("/v1/chat/completions", json=body("hi")).status_code == 200  # a normal body is unaffected


def test_logs_rotate_once_at_their_cap(tmp_path):
    from trioditis.app import DecisionLog, SecurityLog
    log = DecisionLog(str(tmp_path / "decisions.jsonl"), False, max_bytes=300)
    for i in range(20):
        log.write({"i": i, "pad": "x" * 40})
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["decisions.jsonl", "decisions.jsonl.1"]  # one backup, never more
    kept = [json.loads(line)["i"] for name in ("decisions.jsonl.1", "decisions.jsonl") for line in (tmp_path / name).read_text().splitlines()]
    assert kept == sorted(kept) and kept[-1] == 19  # the newest records survive, in order
    import os, stat
    if os.name == "posix":
        for p in tmp_path.iterdir():
            assert stat.S_IMODE(p.stat().st_mode) == 0o600, p.name
    sec = SecurityLog(str(tmp_path / "decisions.jsonl"), max_bytes=200)
    req = type("R", (), {"method": "POST", "url": type("U", (), {"path": "/x"})(), "client": None})()
    for _ in range(10):
        sec.write(req, "invalid or missing API key")
    assert (tmp_path / "security.jsonl.1").exists()
