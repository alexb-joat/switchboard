import asyncio
import json
import subprocess

import httpx
import pytest

from trioditis import hardware as hwm
from trioditis.hardware import (GB, GPU, Hardware, Installed, build_report, installed_models,
                                  load_prices, memory_budget, route_kind, suggest)
from tests.test_trioditis import cfg, make_client

GiB = 2**30


def ram_box(total_gb=16, free_gb=12, igpu=True):
    gpus = [GPU("Radeon 680M", "amd", 512 * 2**20, shared=True)] if igpu else []
    return Hardware("Linux", "x86_64", "Ryzen", 16, int(total_gb * GB), int(free_gb * GB), gpus)


def gpu_box(vram_gib=24):
    return Hardware("Linux", "x86_64", "Ryzen", 16, 64 * GB, 60 * GB,
                    [GPU("RTX 4090", "nvidia", vram_gib * GiB)])


def inst(name, gb, prov="local"):
    return Installed(prov, name, int(gb * GB))


@pytest.fixture(autouse=True)
def _private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HLIDSKJALV_HOME", str(tmp_path))  # never read the real ~/.hlidskjalv


# ------------------------------------------------------------------ budget rule

def test_budget_shared_ram_keeps_headroom():
    b = memory_budget(ram_box(16))
    assert b.bytes == int(16 * GB * 0.7) and b.shared
    assert "integrated GPU shares it" in b.rule and "4 GB" in b.rule
    assert memory_budget(ram_box(8)).bytes == 4 * GB  # small box: the 4 GB reserve wins


def test_budget_discrete_vram_and_unified_and_unknown():
    b = memory_budget(gpu_box(24))
    assert b.bytes == int(24 * GiB * 0.9) and not b.shared and "RTX 4090" in b.rule
    mac = Hardware("Darwin", "arm64", "Apple M2", 8, 32 * GiB, None,
                   [GPU("Apple M2 GPU", "apple", None, True)], unified_memory=True)
    assert memory_budget(mac).bytes == int(32 * GiB * 0.65)
    mac.ram_total = 64 * GiB
    assert memory_budget(mac).bytes == int(64 * GiB * 0.75)
    assert memory_budget(Hardware("Plan9", "?")).bytes == 4 * GB


def test_igpu_carveout_not_counted_as_vram():
    # A 512 MB APU carve-out must not become the budget (it is system RAM).
    assert memory_budget(ram_box(16)).bytes > 10 * GB


# ------------------------------------------------------------------ suggestions

def test_cloud_first_route_with_installed_local_suggests_reorder():
    s = {x.route: x for x in suggest(cfg(), ram_box(), [inst("qwen-coder", 4.7), inst("llama3.2:3b", 2.0)])}
    code = s["code"]
    assert code.action == "use-installed" and code.model_tag == "qwen-coder"
    assert "models: [coder, big]" in code.yaml and "models:\n" not in code.yaml.split("routes:")[0]
    assert any("starts at big (paid per token)" in r for r in code.reasons)
    assert any("-> fits" in r for r in code.reasons)
    assert "$0" in code.est_savings
    assert "quick" not in s  # already local-first and installed: nothing to say


def test_prices_are_quoted_when_known():
    [code] = [x for x in suggest(cfg(), ram_box(), [inst("qwen-coder", 4.7), inst("llama3.2:3b", 2)],
                                 prices={"big": (3.0, 15.0)}) if x.route == "code"]
    assert any("$3 in / $15 out per Mtok" in r for r in code.reasons)
    assert "$0.0105" in code.est_savings  # 1k in + 500 out


def test_route_without_local_model_gets_best_fitting_pull():
    c = cfg(routes={"code": {"models": ["big"]}, "quick": {"models": ["small"]}})
    [code] = [x for x in suggest(c, ram_box(16), [inst("llama3.2:3b", 2)]) if x.route == "code"]
    # 16 GB shared RAM: 14b would fit memory but is capped for speed -> 7b
    assert code.action == "pull" and code.model_tag == "qwen2.5-coder:7b"
    assert code.commands == ["ollama pull qwen2.5-coder:7b"]
    assert "id: qwen2.5-coder:7b" in code.yaml and "models: [local-qwen2.5-coder-7b, big]" in code.yaml
    assert any("RAM bandwidth" in r for r in code.reasons)
    # With a big discrete GPU the larger model is chosen.
    [code] = [x for x in suggest(c, gpu_box(24), []) if x.route == "code"]
    assert code.model_tag == "qwen2.5-coder:14b"


def test_installed_model_preferred_over_download():
    c = cfg(routes={"code": {"models": ["big"]}, "quick": {"models": ["small"]}})
    [code] = [x for x in suggest(c, ram_box(), [inst("qwen2.5-coder:1.5b", 1.0), inst("llama3.2:3b", 2)])
              if x.route == "code"]
    assert code.action == "use-installed" and code.model_tag == "qwen2.5-coder:1.5b"
    assert code.commands == [] and code.alternative == "qwen2.5-coder:7b"  # optional upgrade


def test_too_big_local_model_flagged_with_smaller_alternative():
    c = cfg(models={"coder": {"provider": "local", "id": "qwen2.5-coder:14b"},
                    "small": {"provider": "local", "id": "llama3.2:3b"}},
            routes={"code": {"models": ["coder"]}, "quick": {"models": ["small"]}})
    [s] = [x for x in suggest(c, ram_box(8), [inst("llama3.2:3b", 2)]) if x.route == "code"]
    assert s.action == "too-big" and s.model_tag == "qwen2.5-coder:14b"
    assert s.alternative == "qwen2.5-coder:1.5b"  # largest coder under the 4 GB budget
    assert any("does NOT fit" in r for r in s.reasons) and any("swap" in r for r in s.reasons)
    assert "models: [local-qwen2.5-coder-1.5b]" in s.yaml


def test_configured_but_not_installed_is_pull_only_when_listed():
    c = cfg(models={"r1": {"provider": "local", "id": "deepseek-r1:8b"},
                    "big": {"provider": "cloud", "id": "big-1"},
                    "small": {"provider": "local", "id": "llama3.2:3b"}},
            routes={"reasoning": {"models": ["big", "r1"]}, "quick": {"models": ["small"]}})
    [s] = [x for x in suggest(c, ram_box(free_gb=3), [inst("llama3.2:3b", 2)]) if x.route == "reasoning"]
    assert s.action == "pull" and s.commands == ["ollama pull deepseek-r1:8b"]
    assert "models: [r1, big]" in s.yaml
    assert any("only ~3.0 GB RAM is free" in r for r in s.reasons)
    # Ollama unreachable: don't claim it's missing.
    assert not [x for x in suggest(c, ram_box(), [], listed=set()) if x.route == "reasoning"]


def test_route_kind_inference():
    c = cfg(routes={
        "writing": {"models": ["small"], "keywords": ["proofread", "email"]},
        "plan-stuff": {"models": ["small"]},
        "misc": {"models": ["small"], "description": "Describe an image"},
        "quick": {"models": ["small"]},
    })
    kinds = {n: route_kind(r) for n, r in c.routes.items()}
    assert kinds == {"writing": "general", "plan-stuff": "reasoning", "misc": "vision", "quick": "quick"}


def test_report_shape_and_prices_file(tmp_path):
    (tmp_path / "prices.json").write_text(json.dumps(
        {"_note": "x", "big": {"input": 3, "output": 15}, "bad": {"input": "?"}}))
    assert load_prices() == {"big": (3.0, 15.0)}
    rep = build_report(cfg(), ram_box(), [inst("llama3.2:3b", 2)], {}, load_prices())
    assert rep["budget"]["gb"] == 11.2
    assert {c["model"]: c["installed"] for c in rep["configured_local"]} == {"small": True, "coder": False}
    json.dumps(rep)  # JSON-serialisable


# ------------------------------------------------------------------ probes fail soft

def test_run_fails_soft(monkeypatch):
    monkeypatch.setattr(hwm.shutil, "which", lambda name, path=None: "/usr/bin/" + name)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("x", 3)

    monkeypatch.setattr(hwm.subprocess, "run", boom)
    assert hwm._run("nvidia-smi") is None and hwm._nvidia() == []
    monkeypatch.setattr(hwm.shutil, "which", lambda name, path=None: None)
    assert hwm._run("nvidia-smi") is None


def test_nvidia_parsing(monkeypatch):
    monkeypatch.setattr(hwm, "_run", lambda *a, **k: "NVIDIA GeForce RTX 3060, 12288\ngarbage\n")
    [g] = hwm._nvidia()
    assert g.name == "NVIDIA GeForce RTX 3060" and g.vram_bytes == 12 * GiB and not g.shared


def test_linux_sysfs_probes(tmp_path):
    for card, dev, vram in (("card0", "0x1681", "536870912"), ("card1", "0x744c", str(24 * GiB))):
        d = tmp_path / card / "device"
        d.mkdir(parents=True)
        (d / "vendor").write_text("0x1002\n")
        (d / "device").write_text(dev + "\n")
        (d / "mem_info_vram_total").write_text(vram + "\n")
    (tmp_path / "card0-eDP-1").mkdir()  # connector: ignored
    apu, dgpu = hwm._linux_drm(str(tmp_path))
    assert apu.shared and "680M" in apu.name
    assert not dgpu.shared and dgpu.vram_bytes == 24 * GiB
    assert hwm._linux_drm(str(tmp_path / "missing")) == []
    assert hwm._linux_mem(str(tmp_path / "missing")) == (None, None)
    (tmp_path / "meminfo").write_text("MemTotal:  16000000 kB\nMemAvailable: 8000000 kB\n")
    assert hwm._linux_mem(str(tmp_path / "meminfo")) == (16000000 * 1024, 8000000 * 1024)


def test_detect_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(hwm.platform, "system", lambda: "Linux")
    for fn in ("_linux_mem", "_linux_cpu", "_nvidia", "_linux_drm"):
        monkeypatch.setattr(hwm, fn, boom)
    hw = hwm.detect()
    assert hw.ram_total is None and hw.gpus == [] and len(hw.notes) == 4
    assert memory_budget(hw).bytes == 4 * GB


# ------------------------------------------------------------------ installed models + HTTP

TAGS = {"models": [
    {"name": "llama3.2:3b", "size": 2_019_393_189,
     "details": {"parameter_size": "3.2B", "quantization_level": "Q4_K_M", "family": "llama"}},
    {"name": "nomic-embed-text:latest", "size": 274_302_450, "capabilities": ["embedding"]},
    "junk",
]}


def test_installed_models_queries_only_local_providers():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json=TAGS)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await installed_models(cfg(), c)

    models, errors = asyncio.run(go())
    assert seen == ["http://localhost:11434/api/tags"] and errors == {}
    assert [m.name for m in models] == ["llama3.2:3b", "nomic-embed-text:latest"]
    assert models[0].parameter_size == "3.2B" and hwm.installed_kinds(models[1]) == ("embeddings",)


def test_installed_models_fail_soft():
    def handler(req):
        raise httpx.ConnectError("down")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await installed_models(cfg(), c)

    assert asyncio.run(go()) == ([], {"local": "ConnectError"})


def test_hardware_endpoint(monkeypatch):
    monkeypatch.setattr(hwm, "detect", lambda: ram_box())
    calls = []

    def handler(req):
        calls.append(req.url.path)
        assert req.url.host == "localhost"  # hardware info never goes to the cloud
        return httpx.Response(200, json=TAGS)

    c = make_client(handler)
    r = c.get("/v1/hardware")
    assert r.status_code == 200
    rep = r.json()
    assert rep["hardware"]["cpu"] == "Ryzen" and rep["budget"]["gb"] == 11.2
    assert {m["name"] for m in rep["installed"]} == {"llama3.2:3b", "nomic-embed-text:latest"}
    coder = next(x for x in rep["configured_local"] if x["model"] == "coder")
    assert coder["installed"] is False and coder["fits"] is None  # unknown size: no guessing
    c.get("/v1/hardware")
    assert calls == ["/api/tags"]  # cached


def test_hardware_endpoint_requires_auth(monkeypatch):
    monkeypatch.setenv("TRIODITIS_API_KEY", "router-key-" + "x" * 30)
    monkeypatch.setattr(hwm, "detect", lambda: ram_box())
    c = make_client(lambda req: httpx.Response(200, json=TAGS))
    assert c.get("/v1/hardware").status_code == 401


def test_hardware_report_is_published_for_hlidskjalv(tmp_path, monkeypatch):
    """At startup the router writes the same report as /v1/hardware to a file (0600)."""
    import json as _json
    import os as _os
    from pathlib import Path as _Path
    import httpx as _httpx
    from fastapi.testclient import TestClient as _TC
    from trioditis.app import create_app
    from trioditis_metrics import BudgetCaps, BudgetGuard, MetricsDB, Prices
    import trioditis.hardware as hwmod

    monkeypatch.setattr(hwmod, "detect", lambda: hwmod.Hardware(os="Linux", arch="x86_64", cpu="test", cores=4,
                                                                ram_total=16 * hwmod.GB, ram_available=8 * hwmod.GB))
    out = tmp_path / "hardware.json"
    db = MetricsDB(tmp_path / "m.db")
    app = create_app(cfg(), transport=_httpx.MockTransport(lambda r: _httpx.Response(200, json={"models": []})),
                     metrics=db, budget=BudgetGuard(db, BudgetCaps()), prices=Prices(tmp_path / "p.json"),
                     hardware_file=out)
    with _TC(app):
        import time as _t
        for _ in range(50):
            if out.exists():
                break
            _t.sleep(0.05)
    rep = _json.loads(out.read_text())
    assert rep["hardware"]["cpu"] == "test" and "suggestions" in rep and rep["generated_at"] > 0
    if _os.name == "posix":
        assert out.stat().st_mode & 0o777 == 0o600
