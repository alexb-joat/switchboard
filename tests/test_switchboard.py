import json
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from switchboard.app import create_app
from switchboard.config import ConfigError, parse_config
from switchboard.router import RoutingError, SessionPins, decide
from switchboard.security import assert_safe_bind, detect_sensitive
from switchboard_metrics import BudgetCaps, BudgetGuard, MetricsDB, Prices

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
    monkeypatch.setenv("SWITCHBOARD_API_KEY", "short")
    with pytest.raises(SystemExit):
        assert_safe_bind("0.0.0.0")
    monkeypatch.setenv("SWITCHBOARD_API_KEY", "x" * 40)
    assert_safe_bind("0.0.0.0")


# ------------------------------------------------------------------ HTTP

def make_client(handler, config=None, metrics=None, caps=None, prices=None):
    # Always a throwaway DB, explicit caps and prices: tests must never touch ~/.arcade.
    transport = httpx.MockTransport(handler)
    metrics = metrics or MetricsDB(Path(tempfile.mkdtemp()) / "metrics.db")
    budget = BudgetGuard(metrics, caps or BudgetCaps())
    prices = prices or Prices(Path(tempfile.mkdtemp()) / "prices.json")
    return TestClient(create_app(config or cfg(), transport=transport,
                                 metrics=metrics, budget=budget, prices=prices))


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
    assert r.headers["x-switchboard-model"] == "coder"
    assert seen[0] == ("api.example.com", "big-1", "Bearer cloud-secret")
    assert seen[1] == ("localhost", "qwen-coder", None)  # local: no key sent


def test_router_key_required_and_never_forwarded(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_API_KEY", "router-key-" + "x" * 30)
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
    assert c.post("/v1/chat/completions", content=b"not json").status_code == 400


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
    from switchboard.__main__ import load_dotenv
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
    assert r.headers["x-switchboard-model"] == "coder"
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
    import switchboard_metrics.guard as guard_mod
    cap_file = tmp_path / "budget.json"
    monkeypatch.setattr(guard_mod, "DEFAULT_CONFIG_PATH", cap_file)
    guard = BudgetGuard(overspent_db())                        # $5 spent today
    assert "daily cap" in guard.check()                        # default $1/day
    cap_file.write_text(json.dumps({"daily_usd": 10, "monthly_usd": 100}))
    assert guard.check() is None


def test_refused_requests_are_logged_without_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_API_KEY", "router-key-" + "x" * 30)
    c = make_client(lambda req: ok("x"), cfg(logging={"path": str(tmp_path / "decisions.jsonl")}))
    assert c.post("/v1/chat/completions", json=body("hi"), headers={"Authorization": "Bearer wrong-guess"}).status_code == 401
    [rec] = [json.loads(line) for line in (tmp_path / "security.jsonl").read_text().splitlines()]
    assert rec["path"] == "/v1/chat/completions" and rec["reason"] == "invalid or missing API key"
    assert "wrong-guess" not in (tmp_path / "security.jsonl").read_text()
