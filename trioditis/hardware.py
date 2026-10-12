"""Hardware detection and local-model suggestions.

Answers "which local models can this machine run, and where would they save
money?" so cheap local models can take work that would otherwise go to paid
cloud models.

Design rules (security / honesty):
- Detection is stdlib only and read-only: /proc, /sys, sysctl, nvidia-smi.
  Commands run without a shell, by absolute path, with a timeout.
- Every probe fails soft (None / "unknown"); detection never raises.
- Hardware facts never leave the machine. The only network calls are GETs to
  the *local* providers already in config (Ollama /api/tags), with no body.
- Suggestions are text plus YAML to paste; config.yaml is never edited.
- Catalog sizes are approximate and labelled as such.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import httpx

from .config import Config, Route

GB = 1_000_000_000  # decimal GB, the unit `ollama list` shows
OVERHEAD = 1.2  # loaded model ~ file size +20% (KV cache at default context, buffers)
RAM_FRACTION = 0.7  # shared memory: share of RAM a model may use...
RAM_RESERVE = 4 * GB  # ...while always leaving this much for the OS and apps
VRAM_FRACTION = 0.9  # dedicated VRAM minus ~10% for context / driver
INTERACTIVE_GB = 6.0  # shared memory: speed is RAM-bandwidth bound; prefer <= ~7-8B models
GB_PER_B_PARAMS = 0.62  # rough Q4_K_M file size per billion parameters


# ---------------------------------------------------------------- detection

@dataclass
class GPU:
    name: str
    vendor: str  # nvidia | amd | intel | apple | unknown
    vram_bytes: int | None = None  # dedicated memory; None = unknown
    shared: bool = False  # integrated / unified: uses system RAM


@dataclass
class Hardware:
    os: str
    arch: str
    cpu: str | None = None
    cores: int | None = None  # logical
    ram_total: int | None = None
    ram_available: int | None = None
    gpus: list[GPU] = field(default_factory=list)
    unified_memory: bool = False  # Apple Silicon
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["ram_total_gb"] = _gb(self.ram_total)
        d["ram_available_gb"] = _gb(self.ram_available)
        for g in d["gpus"]:
            g["vram_gb"] = _gb(g["vram_bytes"])
        return d


def _gb(n: int | float | None) -> float | None:
    return None if n is None else round(n / GB, 1)


def _run(*args: str, timeout: float = 3.0) -> str | None:
    """Run a fixed command (no shell) and return stdout, or None on any failure."""
    # System directories only: a fake nvidia-smi dropped into ~/.local/bin must not run here.
    exe = shutil.which(args[0], path="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
                       if os.name != "nt" else None)
    if not exe:
        return None
    try:
        r = subprocess.run([exe, *args[1:]], capture_output=True, text=True,
                           timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.stdout if r.returncode == 0 else None


def _read(path: str | Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:1_000_000]
    except (OSError, ValueError):
        return None


def _linux_mem(meminfo: str = "/proc/meminfo") -> tuple[int | None, int | None]:
    text = _read(meminfo) or ""
    vals = {}
    for line in text.splitlines():
        m = re.match(r"(MemTotal|MemAvailable):\s+(\d+)\s*kB", line)
        if m:
            vals[m.group(1)] = int(m.group(2)) * 1024
    return vals.get("MemTotal"), vals.get("MemAvailable")


def _linux_cpu(cpuinfo: str = "/proc/cpuinfo") -> str | None:
    for line in (_read(cpuinfo) or "").splitlines():
        key, _, val = line.partition(":")
        if key.strip() in ("model name", "Hardware", "Model") and val.strip():
            return val.strip()[:200]
    return None


def _nvidia() -> list[GPU]:
    out = _run("nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits")
    gpus = []
    for line in (out or "").splitlines():
        name, _, mib = line.rpartition(",")
        if name.strip() and mib.strip().isdigit():
            gpus.append(GPU(name.strip()[:200], "nvidia", int(mib.strip()) * 1024 * 1024))
    return gpus


# PCI device ids of AMD APUs (integrated Radeon): no lspci needed for a name.
_AMD_APU = {
    "0x15dd": "Radeon Vega (Raven Ridge)", "0x15d8": "Radeon Vega (Picasso)",
    "0x1636": "Radeon (Renoir)", "0x1638": "Radeon (Cezanne)", "0x164c": "Radeon (Lucienne)",
    "0x163f": "Radeon (Van Gogh)", "0x1681": "Radeon 680M/660M (Rembrandt)",
    "0x164e": "Radeon (Raphael iGPU)", "0x15bf": "Radeon 780M/760M (Phoenix)",
    "0x15c8": "Radeon 740M (Phoenix2)", "0x150e": "Radeon 890M/880M (Strix Point)",
    "0x1586": "Radeon 8060S/8050S (Strix Halo)",
}
_VENDORS = {"0x1002": "amd", "0x10de": "nvidia", "0x8086": "intel"}


def _linux_drm(root: str = "/sys/class/drm") -> list[GPU]:
    gpus = []
    try:
        cards = sorted(p for p in Path(root).iterdir() if re.fullmatch(r"card\d+", p.name))
    except OSError:
        return []
    for card in cards:
        dev = card / "device"
        vendor = _VENDORS.get((_read(dev / "vendor") or "").strip(), "unknown")
        devid = (_read(dev / "device") or "").strip()
        if vendor == "nvidia":
            gpus.append(GPU("NVIDIA GPU (install nvidia-smi for VRAM)", "nvidia"))
            continue
        name = (_read(dev / "product_name") or "").strip() or _AMD_APU.get(devid) or \
            f"{vendor.upper()} GPU [{devid or '?'}]"
        vram = (_read(dev / "mem_info_vram_total") or "").strip()
        vram_b = int(vram) if vram.isdigit() else None
        # An APU's "VRAM" is a BIOS carve-out of system RAM, so it is shared.
        # Small carve-outs (<= 2 GiB) are treated the same way.
        shared = vendor == "intel" or devid in _AMD_APU or (vram_b is not None and vram_b <= 2 * 2**30)
        if vendor == "intel" and vram_b is None:
            name += " (integrated)"
        gpus.append(GPU(name[:200], vendor, vram_b, shared))
    return gpus


def _mac() -> tuple[str | None, int | None, list[GPU], bool]:
    cpu = (_run("sysctl", "-n", "machdep.cpu.brand_string") or "").strip() or None
    mem = (_run("sysctl", "-n", "hw.memsize") or "").strip()
    total = int(mem) if mem.isdigit() else None
    apple = platform.machine() == "arm64"
    gpus = [GPU(f"{cpu or 'Apple Silicon'} GPU", "apple", None, True)] if apple else []
    return cpu, total, gpus, apple


def _windows() -> tuple[str | None, int | None, int | None]:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(st)
    total = avail = None
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        total, avail = int(st.ullTotalPhys), int(st.ullAvailPhys)
    cpu = None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            cpu = str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()[:200]
    except OSError:
        pass
    return cpu or platform.processor() or None, total, avail


def _probe(hw: Hardware, label: str, fn, default):
    try:
        return fn()
    except Exception as e:  # a probe must never take detection down
        hw.notes.append(f"{label} probe failed ({type(e).__name__})")
        return default


def detect() -> Hardware:
    """Best-effort snapshot of this machine. Never raises."""
    system = platform.system() or "unknown"
    hw = Hardware(os=system, arch=platform.machine() or "unknown", cores=os.cpu_count())
    if system == "Linux":
        hw.ram_total, hw.ram_available = _probe(hw, "memory", _linux_mem, (None, None))
        hw.cpu = _probe(hw, "cpu", _linux_cpu, None)
        nv = _probe(hw, "nvidia", _nvidia, [])
        drm = _probe(hw, "drm", _linux_drm, [])
        # nvidia-smi knows VRAM; drop the sysfs duplicates of the same cards.
        hw.gpus = nv + [g for g in drm if not (nv and g.vendor == "nvidia")]
    elif system == "Darwin":
        hw.cpu, hw.ram_total, hw.gpus, hw.unified_memory = _probe(
            hw, "macos", _mac, (None, None, [], False))
        if not hw.unified_memory:
            hw.notes.append("Intel Mac: GPU not probed; budget uses system RAM")
    elif system == "Windows":
        hw.cpu, hw.ram_total, hw.ram_available = _probe(hw, "windows", _windows, (None, None, None))
        hw.gpus = _probe(hw, "nvidia", _nvidia, [])
        if not hw.gpus:
            hw.notes.append("Windows: only NVIDIA GPUs are detected (via nvidia-smi)")
    else:
        hw.notes.append(f"{system}: no probes; budget is a conservative guess")
    if hw.cpu is None:
        hw.cpu = platform.processor() or None
    return hw


def refresh_available(hw: Hardware) -> Hardware:
    """Free RAM changes all the time; everything else is fixed per boot."""
    if hw.os == "Linux":
        avail = _probe(hw, "memory", _linux_mem, (None, None))[1]
        if avail is not None:
            return replace(hw, ram_available=avail)
    return hw


# ---------------------------------------------------------------- memory budget

@dataclass
class Budget:
    bytes: int
    rule: str  # how it was computed, in words
    context: str  # short "why", e.g. "16.0 GB RAM, integrated GPU shares it"
    shared: bool  # model runs from system RAM (speed limited by RAM bandwidth)


def memory_budget(hw: Hardware) -> Budget:
    discrete = [g for g in hw.gpus if not g.shared and g.vram_bytes]
    if discrete:
        g = max(discrete, key=lambda x: x.vram_bytes)
        b = int(g.vram_bytes * VRAM_FRACTION)
        many = " (largest single GPU; splitting across GPUs is possible but not assumed)" \
            if len(discrete) > 1 else ""
        return Budget(b, f"dedicated VRAM of {g.name} ({_gb(g.vram_bytes)} GB) minus ~10% for "
                         f"context and driver{many}; bigger models spill into system RAM and "
                         f"run several times slower", f"{_gb(g.vram_bytes)} GB VRAM on {g.name}", False)
    if hw.ram_total is None:
        return Budget(4 * GB, "memory size unknown: assuming a conservative 4 GB",
                      "memory unknown", True)
    total = hw.ram_total
    if hw.unified_memory:
        frac = 0.75 if total > 36 * 2**30 else 0.65
        return Budget(int(total * frac), f"Apple unified memory: macOS lets the GPU use about "
                      f"{int(frac * 100)}% of {_gb(total)} GB by default",
                      f"{_gb(total)} GB unified memory", False)
    b = max(0, int(min(total * RAM_FRACTION, total - RAM_RESERVE)))
    igpu = any(g.shared for g in hw.gpus)
    why = "integrated GPU shares it" if igpu else "no GPU with dedicated memory"
    return Budget(b, f"{int(RAM_FRACTION * 100)}% of {_gb(total)} GB RAM, keeping at least "
                     f"{RAM_RESERVE // GB} GB for the OS and apps ({why}; counted once)",
                  f"{_gb(total)} GB RAM, {why}", True)


def need_bytes(size: int) -> int:
    return int(size * OVERHEAD)


# ---------------------------------------------------------------- catalog

@dataclass(frozen=True)
class CatalogModel:
    tag: str
    kinds: tuple[str, ...]
    size_gb: float  # APPROXIMATE download size at Ollama's default quant (mostly Q4_K_M)
    context: int
    capabilities: tuple[str, ...] = ()


# Well-known Ollama library models, best-first within each kind.
# Sizes are approximate (Ollama library page, default quant); check the real
# number with `ollama show <tag>` or `ollama list` after pulling.
CATALOG: list[CatalogModel] = [
    CatalogModel("qwen2.5-coder:14b", ("code",), 9.0, 32768, ("tools",)),
    CatalogModel("qwen2.5-coder:7b", ("code",), 4.7, 32768, ("tools",)),
    CatalogModel("qwen2.5-coder:1.5b", ("code",), 1.0, 32768, ("tools",)),
    CatalogModel("deepseek-r1:14b", ("reasoning",), 9.0, 131072),
    CatalogModel("deepseek-r1:8b", ("reasoning",), 5.2, 131072),
    CatalogModel("deepseek-r1:1.5b", ("reasoning",), 1.1, 131072),
    CatalogModel("qwen2.5:14b", ("general",), 9.0, 32768, ("tools",)),
    CatalogModel("llama3.1:8b", ("general",), 4.9, 131072, ("tools",)),
    CatalogModel("qwen2.5:7b", ("general",), 4.7, 32768, ("tools",)),
    CatalogModel("llama3.2:3b", ("quick", "general"), 2.0, 131072, ("tools",)),
    CatalogModel("llama3.2:1b", ("quick",), 1.3, 131072, ("tools",)),
    CatalogModel("llama3.2-vision:11b", ("vision",), 7.8, 131072, ("vision",)),
    CatalogModel("llava:7b", ("vision",), 4.7, 4096, ("vision",)),
    CatalogModel("moondream", ("vision",), 1.7, 2048, ("vision",)),
    CatalogModel("nomic-embed-text", ("embeddings",), 0.27, 8192),
]


def norm_tag(tag: str) -> str:
    tag = tag.strip().lower()
    return tag if ":" in tag else tag + ":latest"


def catalog_entry(tag: str) -> CatalogModel | None:
    t = norm_tag(tag)
    return next((c for c in CATALOG if norm_tag(c.tag) == t), None)


# ---------------------------------------------------------------- installed models

@dataclass
class Installed:
    provider: str
    name: str
    size_bytes: int | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    family: str | None = None
    capabilities: tuple[str, ...] = ()


async def installed_models(cfg: Config, client: httpx.AsyncClient,
                           timeout: float = 3.0) -> tuple[list[Installed], dict[str, str]]:
    """List models in each local Ollama provider. Returns (models, {provider: error})."""
    out: list[Installed] = []
    errors: dict[str, str] = {}
    for p in cfg.providers.values():
        if not p.local:
            continue  # only ever ask local providers; nothing about this machine goes out
        root = p.base_url[:-3] if p.base_url.endswith("/v1") else p.base_url
        headers = {"Authorization": f"Bearer {p.api_key()}"} if p.api_key() else {}
        try:
            r = await client.get(f"{root}/api/tags", headers=headers, timeout=timeout)
            r.raise_for_status()
            items = r.json().get("models") or []
            if not isinstance(items, list):
                raise ValueError("bad /api/tags payload")
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            errors[p.name] = type(e).__name__
            continue
        for m in items[:500]:
            if not isinstance(m, dict) or not isinstance(m.get("name"), str):
                continue
            d = m.get("details") if isinstance(m.get("details"), dict) else {}
            caps = m.get("capabilities") if isinstance(m.get("capabilities"), list) else []
            s = lambda v: str(v)[:40] if v not in (None, "") else None  # noqa: E731
            out.append(Installed(
                provider=p.name, name=m["name"][:200],
                size_bytes=m["size"] if isinstance(m.get("size"), int) else None,
                parameter_size=s(d.get("parameter_size")), quantization=s(d.get("quantization_level")),
                family=s(d.get("family")), capabilities=tuple(str(c)[:20] for c in caps[:10]),
            ))
    return out, errors


def installed_kinds(m: Installed) -> tuple[str, ...]:
    c = catalog_entry(m.name)
    if c:
        return c.kinds
    n = (m.name + " " + (m.family or "")).lower()
    if "embedding" in m.capabilities or "embed" in n or "bert" in n:
        return ("embeddings",)
    if "vision" in m.capabilities or any(k in n for k in ("llava", "vision", "moondream")):
        return ("vision",)
    if "coder" in n or "code" in n:
        return ("code",)
    if "thinking" in m.capabilities or any(k in n for k in ("-r1", "qwq", "reason")):
        return ("reasoning",)
    return ("general",)


# ---------------------------------------------------------------- prices

def load_prices(path: Path | None = None) -> dict[str, tuple[float, float]]:
    """~/.hlidskjalv/prices.json as {"model": ($/Mtok in, $/Mtok out)}, via the shared reader."""
    from trioditis_metrics.home import data_home
    from trioditis_metrics.prices import Prices
    p = path or data_home() / "prices.json"
    out = {}
    for k, v in Prices(p).table().items():
        try:
            out[str(k)] = (float(v["input"]), float(v["output"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- suggestions

@dataclass
class Suggestion:
    route: str
    kind: str
    action: str  # use-installed | pull | too-big
    model_tag: str
    reasons: list[str]
    est_savings: str | None = None
    yaml: str = ""  # paste into config.yaml yourself; never applied automatically
    commands: list[str] = field(default_factory=list)
    alternative: str | None = None  # too-big: smaller tag; otherwise: optional upgrade


_KIND_STEMS = [
    ("vision", ("vision", "image", "photo", "screenshot", "ocr")),
    ("code", ("code", "coder", "coding", "program", "debug", "refactor")),
    ("reasoning", ("reason", "plan", "math", "logic", "proof", "prove")),
    ("quick", ("quick", "fast", "small", "chat", "simple")),
]


def route_kind(route: Route) -> str:
    name = route.name.lower()
    for kind, stems in _KIND_STEMS:
        if any(s in name for s in stems):
            return kind
    # Whole words only ("proofread" is not "proof"), with plural/-ing forms.
    words = set(re.findall(r"[a-z]+", (route.description + " " + " ".join(route.keywords)).lower()))
    words |= {w[:-1] for w in words if w.endswith("s")} | {w[:-3] for w in words if w.endswith("ing")}
    for kind, stems in _KIND_STEMS:
        if words & set(stems):
            return kind
    return "general"


def _size_of(tag: str, installed: dict[str, Installed]) -> tuple[int | None, str]:
    m = installed.get(norm_tag(tag))
    if m and m.size_bytes:
        return m.size_bytes, "installed size"
    c = catalog_entry(tag)
    if c:
        return int(c.size_gb * GB), "approx., catalog"
    hit = re.search(r"[:-](\d+(?:\.\d+)?)b\b", tag.lower())
    if hit:
        return int(float(hit.group(1)) * GB_PER_B_PARAMS * GB), f"estimated from '{hit.group(1)}b' in the tag"
    return None, "unknown"


def _fit_text(tag: str, size: int, src: str, b: Budget) -> str:
    ok = need_bytes(size) <= b.bytes
    return (f"{tag} needs ~{_gb(need_bytes(size))} GB ({_gb(size)} GB {src} +20% for context); "
            f"you have ~{_gb(b.bytes)} GB usable ({b.context}) -> {'fits' if ok else 'does NOT fit'}")


def _price(prices, cfg: Config, model: str) -> tuple[float, float] | None:
    return prices.get(model) or prices.get(cfg.models[model].upstream_id)


def _cost_words(prices, cfg: Config, model: str) -> str:
    pr = _price(prices, cfg, model)
    if pr:
        return f"${pr[0]:g} in / ${pr[1]:g} out per Mtok (from prices.json)"
    return "paid per token"


def _savings(prices, cfg: Config, cloud: str, local_name: str) -> str:
    pr = _price(prices, cfg, cloud)
    if pr:
        eg = (1000 * pr[0] + 500 * pr[1]) / 1e6
        return (f"each prompt {local_name} answers instead of {cloud} saves its full API cost "
                f"(e.g. ~${eg:.4f} for 1k input + 500 output tokens)")
    return f"each prompt {local_name} answers instead of {cloud} (paid per token) costs $0 in API fees"


def _pick(kind: str, b: Budget, installed: list[Installed]) -> tuple[str, bool, str | None, list[str]]:
    """Best local model for a kind: installed first (zero download), then catalog.

    Returns (tag, is_installed, upgrade_or_None, reasons). tag == "" if nothing fits.
    """
    target = min(b.bytes, int(INTERACTIVE_GB * GB)) if b.shared and kind != "reasoning" else b.bytes
    why = []
    if target < b.bytes:
        why.append(f"no dedicated GPU memory: speed is limited by RAM bandwidth, so picks stay "
                   f"under ~{INTERACTIVE_GB:g} GB (about 7-8B parameters) to keep replies interactive")
    accept = {kind, "general"} if kind == "quick" else {kind, "quick"} if kind == "general" else {kind}
    fits = [m for m in installed if m.size_bytes and need_bytes(m.size_bytes) <= target
            and accept & set(installed_kinds(m))]
    cat = next((c for c in CATALOG if kind in c.kinds and need_bytes(int(c.size_gb * GB)) <= target), None)
    if fits:
        best = max(fits, key=lambda m: m.size_bytes)
        up = None
        if cat and norm_tag(cat.tag) not in {norm_tag(m.name) for m in installed} \
                and cat.size_gb * GB >= 1.5 * best.size_bytes:
            up = cat.tag
            why.append(f"upgrade option: {cat.tag} (~{cat.size_gb:g} GB download, approx.) also fits "
                       f"and is a larger model for {kind}")
        return best.name, True, up, why
    if cat:
        return cat.tag, False, None, why
    return "", False, None, why + [f"no catalog model for {kind} fits ~{_gb(target)} GB"]


def _snippet(cfg: Config, route: Route, local_name: str, tag: str | None, drop: str | None,
             cloud_first: bool) -> str:
    lines = []
    if tag:  # new models: entry
        prov = next((p.name for p in cfg.providers.values() if p.local), "ollama")
        c = catalog_entry(tag)
        lines += ["models:", f"  {local_name}:", f"    provider: {prov}", f"    id: {tag}"]
        if c and c.capabilities:
            lines.append(f"    capabilities: [{', '.join(c.capabilities)}]")
        lines.append(f"    context_tokens: {min(c.context if c else 8192, 32000)}")
    order = [local_name] + [m for m in route.models if m not in (local_name, drop)]
    note = "local first = $0; cloud stays as fallback" if cloud_first else "local model first"
    lines += ["routes:", f"  {route.name}:", f"    models: [{', '.join(order)}]   # {note}"]
    return "\n".join(lines)


def _model_for_tag(cfg: Config, tag: str) -> str | None:
    return next((n for n, m in cfg.models.items()
                 if cfg.providers[m.provider].local and norm_tag(m.upstream_id) == norm_tag(tag)), None)


def _new_name(cfg: Config, tag: str) -> str:
    return _model_for_tag(cfg, tag) or "local-" + re.sub(r"[^a-z0-9.]+", "-", tag.lower()).strip("-")


def suggest(cfg: Config, hw: Hardware, installed: list[Installed],
            prices: dict | None = None, listed: set[str] | None = None) -> list[Suggestion]:
    """Per-route local-model suggestions, cost-efficiency first.

    `listed` = local providers whose installed models we could read; a model is
    only called "not installed" when its provider was listed.
    """
    prices = prices or {}
    listed = {p.name for p in cfg.providers.values() if p.local} if listed is None else listed
    b = memory_budget(hw)
    inst = {norm_tag(m.name): m for m in installed}
    out: list[Suggestion] = []
    free_note = None
    if b.shared and hw.ram_available is not None:
        free_note = hw.ram_available

    def free_warn(size: int, reasons: list[str]):
        if free_note is not None and need_bytes(size) > free_note:
            reasons.append(f"right now only ~{_gb(free_note)} GB RAM is free; close other apps "
                           f"or the first load will swap")

    for route in cfg.routes.values():
        kind = route_kind(route)
        local = [m for m in route.models if cfg.provider_of(m).local]
        first = route.models[0]
        cloud_first = not cfg.provider_of(first).local
        too_big = unknown = False
        fitting = None
        for mn in local:
            tag = cfg.models[mn].upstream_id
            size, src = _size_of(tag, inst)
            if size is None:
                unknown = True  # can't judge fit; say nothing rather than guess
                continue
            if need_bytes(size) <= b.bytes:
                fitting = fitting or (mn, tag, size, src)
                continue
            too_big = True
            alt, alt_inst, _, why = _pick(kind, b, installed)
            reasons = [_fit_text(tag, size, src, b), f"budget rule: {b.rule}"]
            reasons.append("it would " + ("spill out of VRAM and run several times slower"
                                          if not b.shared else "swap and run very slowly"))
            if alt:
                reasons.append(f"use {alt} instead ({'already installed' if alt_inst else 'pull it'})")
            reasons += why
            name = _new_name(cfg, alt) if alt else mn
            out.append(Suggestion(
                route.name, kind, "too-big", tag, reasons, alternative=alt or None,
                yaml=_snippet(cfg, route, name, None if (not alt or _model_for_tag(cfg, alt)) else alt,
                              mn, cloud_first) if alt else "",
                commands=[f"ollama pull {alt}"] if alt and not alt_inst else []))

        if fitting:
            mn, tag, size, src = fitting
            is_inst = norm_tag(tag) in inst
            provider = cfg.models[mn].provider
            ahead = route.models[:route.models.index(mn)]
            cloud_ahead = [m for m in ahead if not cfg.provider_of(m).local]
            if not is_inst and provider in listed:
                reasons = [f"{mn} ({tag}) is configured but not installed in {provider}, "
                           f"so this route can't use it yet", _fit_text(tag, size, src, b)]
                if cloud_ahead:
                    reasons.append(f"route {route.name} currently starts at {cloud_ahead[0]} "
                                   f"({_cost_words(prices, cfg, cloud_ahead[0])}); putting {mn} first "
                                   f"for everyday prompts costs $0")
                free_warn(size, reasons)
                out.append(Suggestion(
                    route.name, kind, "pull", tag, reasons,
                    est_savings=_savings(prices, cfg, cloud_ahead[0], mn) if cloud_ahead else None,
                    yaml=_snippet(cfg, route, mn, None, None, True) if cloud_ahead else "",
                    commands=[f"ollama pull {tag}"]))
            elif is_inst and cloud_ahead:
                _, _, up, why = _pick(kind, b, installed)
                reasons = [f"route {route.name} currently starts at {cloud_ahead[0]} "
                           f"({_cost_words(prices, cfg, cloud_ahead[0])}); {mn} only runs as a "
                           f"fallback. Putting it first for everyday prompts costs $0",
                           f"{tag} is already installed (zero download)", _fit_text(tag, size, src, b),
                           "cloud models stay as fallbacks: the router skips to them when the local "
                           "model is down or the request needs vision, tools or more context than "
                           "it has", "trade-off: hard prompts get a smaller model unless the client "
                           f"asks for a cloud model by name (model: {cloud_ahead[0]})"]
                if kind == "code":
                    reasons.append("if an agent such as Cline drives this route, keep the cloud model "
                                   "first: small local models often lose track of long multi-step "
                                   "instructions. Local-first suits chat-style coding questions")
                if up:
                    reasons += [w for w in why if w.startswith("upgrade")]
                out.append(Suggestion(
                    route.name, kind, "use-installed", tag, reasons,
                    est_savings=_savings(prices, cfg, cloud_ahead[0], mn),
                    yaml=_snippet(cfg, route, mn, None, None, True), alternative=up))
            continue

        if too_big or unknown:
            continue  # too-big already carries the replacement; unknown size: don't guess
        # No usable local model on this route: recommend one.
        tag, is_inst, up, why = _pick(kind, b, installed)
        if not tag:
            continue
        size, src = _size_of(tag, inst)
        name = _new_name(cfg, tag)
        reasons = [f"route {route.name} has no local model"
                   + (f"; it starts at {first} ({_cost_words(prices, cfg, first)})" if cloud_first else ""),
                   (f"{tag} is already installed (zero download)" if is_inst
                    else f"best {kind} model in the catalog that fits"),
                   _fit_text(tag, size, src, b)] + why
        if size:
            free_warn(size, reasons)
        out.append(Suggestion(
            route.name, kind, "use-installed" if is_inst else "pull", tag, reasons,
            est_savings=_savings(prices, cfg, first, name) if cloud_first else None,
            yaml=_snippet(cfg, route, name, None if _model_for_tag(cfg, tag) else tag, None, cloud_first),
            commands=[] if is_inst else [f"ollama pull {tag}"], alternative=up))

    # Semantic routing needs its embedding model locally.
    e = cfg.embeddings
    if e and e.provider in listed and norm_tag(e.model) not in inst:
        size, src = _size_of(e.model, inst)
        reasons = [f"embeddings.model {e.model} is not installed: routing falls back to keywords only"]
        if size:
            reasons.append(_fit_text(e.model, size, src, b))
        out.append(Suggestion("(embeddings)", "embeddings", "pull", e.model, reasons,
                              commands=[f"ollama pull {e.model}"]))
    return out


# ---------------------------------------------------------------- report

CATALOG_NOTE = ("Catalog sizes are approximate (default Ollama quant, mostly Q4_K_M); "
                "check with `ollama show <tag>`. 'Needs' = size +20% for context.")


def build_report(cfg: Config, hw: Hardware, installed: list[Installed],
                 errors: dict[str, str] | None = None, prices: dict | None = None) -> dict:
    errors = errors or {}
    b = memory_budget(hw)
    inst = {norm_tag(m.name): m for m in installed}
    listed = {p.name for p in cfg.providers.values() if p.local} - set(errors)
    configured = []
    for name, m in cfg.models.items():
        if not cfg.providers[m.provider].local:
            continue
        size, src = _size_of(m.upstream_id, inst)
        configured.append({
            "model": name, "tag": m.upstream_id, "provider": m.provider,
            "installed": (norm_tag(m.upstream_id) in inst) if m.provider in listed else None,
            "size_gb": _gb(size), "size_source": src,
            "need_gb": _gb(need_bytes(size)) if size else None,
            "fits": need_bytes(size) <= b.bytes if size else None,
        })
    return {
        "hardware": hw.as_dict(),
        "budget": {"bytes": b.bytes, "gb": _gb(b.bytes), "rule": b.rule, "context": b.context,
                   "shared_memory": b.shared},
        "installed": [{**asdict(m), "size_gb": _gb(m.size_bytes), "kinds": list(installed_kinds(m)),
                       "need_gb": _gb(need_bytes(m.size_bytes)) if m.size_bytes else None,
                       "fits": need_bytes(m.size_bytes) <= b.bytes if m.size_bytes else None}
                      for m in installed],
        "installed_errors": errors,
        "configured_local": configured,
        "suggestions": [asdict(s) for s in suggest(cfg, hw, installed, prices, listed)],
        "notes": [CATALOG_NOTE, "Nothing here was sent anywhere; config.yaml was not changed."],
    }


class ReportCache:
    """Hardware is detected once per process; installed models are re-read every `ttl` s."""

    def __init__(self, ttl: float = 60.0):
        self.ttl = ttl
        self.hw: Hardware | None = None
        self._rep: dict | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def get(self, cfg: Config, client: httpx.AsyncClient) -> dict:
        async with self._lock:
            if self._rep is None or time.monotonic() - self._at > self.ttl:
                if self.hw is None:
                    self.hw = await asyncio.to_thread(detect)
                hw = refresh_available(self.hw)
                inst, errs = await installed_models(cfg, client)
                self._rep = build_report(cfg, hw, inst, errs, load_prices())
                self._at = time.monotonic()
            return self._rep


def format_report(rep: dict) -> str:
    hw, b = rep["hardware"], rep["budget"]
    L = ["Hardware",
         f"  OS        {hw['os']} ({hw['arch']})",
         f"  CPU       {hw['cpu'] or 'unknown'}, {hw['cores'] or '?'} logical cores",
         f"  RAM       {hw['ram_total_gb'] or '?'} GB total, {hw['ram_available_gb'] or '?'} GB free now"]
    for g in hw["gpus"] or [{"name": "none detected", "vram_gb": None, "shared": False}]:
        mem = ("shares system RAM" if g["shared"]
               else f"{g['vram_gb']} GB VRAM" if g["vram_gb"] else "VRAM unknown")
        L.append(f"  GPU       {g['name']} ({mem})")
    L += [f"  note      {n}" for n in hw["notes"]]
    L += ["", f"Usable for a local model: ~{b['gb']} GB", f"  rule: {b['rule']}", "",
          "Installed local models"]
    if not rep["installed"]:
        L.append("  (none listed)")
    for m in rep["installed"]:
        fit = {True: "fits", False: "too big", None: "?"}[m["fits"]]
        meta = " ".join(x for x in (m["parameter_size"], m["quantization"]) if x)
        L.append(f"  {m['name']:<26} {m['size_gb'] or '?':>5} GB  {meta:<16} need ~{m['need_gb']} GB  {fit}")
    for p, err in rep["installed_errors"].items():
        L.append(f"  {p}: could not list models ({err}); is Ollama running?")
    L += ["", "Configured local models"]
    for c in rep["configured_local"]:
        state = {True: "installed", False: "not installed", None: "install state unknown"}[c["installed"]]
        fit = {True: "fits", False: "too big", None: "size unknown"}[c["fits"]]
        size = f"~{c['size_gb']} GB ({c['size_source']})" if c["size_gb"] else "size unknown"
        L.append(f"  {c['model']:<18} {c['tag']:<22} {state:<14} {size}, {fit}")
    L += ["", "Suggestions (cost-efficiency first; nothing is applied automatically)"]
    if not rep["suggestions"]:
        L.append("  none: every route already starts with a local model that fits")
    for s in rep["suggestions"]:
        L += ["", f"  [{s['route']}] ({s['kind']}) {s['action']}: {s['model_tag']}"]
        L += [f"    - {r}" for r in s["reasons"]]
        if s["est_savings"]:
            L.append(f"    $ {s['est_savings']}")
        if s["alternative"]:
            L.append(f"    alternative: {s['alternative']}")
        L += [f"    run: {c}" for c in s["commands"]]
        if s["yaml"]:
            L += ["    config.yaml:"] + [f"      {y}" for y in s["yaml"].splitlines()]
    L += [""] + [f"Note: {n}" for n in rep["notes"]]
    return "\n".join(L)
