"""Prices from a provider-facts file instead of typed by hand.

A provider-facts file (schema "provider-facts.v1", the one Euboulia's catalog is built from) lists,
per provider, each model's API id and price with the documentation page it was read from. This module
matches it against config.yaml by the model id sent upstream and writes ~/.hlidskjalv/prices.json
entries that carry their source:

    {"claude-haiku": {"input": 0.1, "output": 0.5,
                      "long_context": {"threshold": 100000, "input": 0.5, "output": 2.5},
                      "_source": "https://…/pricing", "_fetched": "2026-10-14"}}
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

SCHEMA = "provider-facts.v1"
STALE_DAYS = 30


class FactsError(ValueError):
    pass


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def facts_by_api_id(facts: dict) -> dict[str, dict]:
    """{api id: {"input", "output", "long_context"?, "_source", "_fetched"}} for every priced model."""
    if not isinstance(facts, dict) or facts.get("$schema") != SCHEMA:
        raise FactsError(f"not a {SCHEMA} file")
    out: dict[str, dict] = {}
    for prov in (facts.get("providers") or {}).values():
        sources = prov.get("sources") or {}
        for m in (prov.get("models") or {}).values():
            api_id, price = m.get("apiId"), m.get("price") or {}
            if not isinstance(api_id, str) or not (_num(price.get("input")) and _num(price.get("output"))):
                continue
            src = sources.get((m.get("sources") or {}).get("price")) or {}
            entry = {"input": price["input"], "output": price["output"]}
            tier = price.get("longContext")
            if isinstance(tier, dict) and all(_num(tier.get(k)) for k in ("threshold", "input", "output")):
                entry["long_context"] = {k: tier[k] for k in ("threshold", "input", "output")}
            entry["_source"] = src.get("url")
            entry["_fetched"] = src.get("fetched")
            out[api_id] = entry
    return out


def plan_import(facts: dict, cloud_models: dict[str, str], current: dict) -> tuple[dict, list[str], list[str]]:
    """cloud_models: {config name: upstream id}. Returns (new prices table, change lines, unmatched names)."""
    known = facts_by_api_id(facts)
    table = dict(current)
    changes, unmatched = [], []
    for name, upstream_id in sorted(cloud_models.items()):
        new = known.get(upstream_id)
        if new is None:
            unmatched.append(name)
            continue
        old = current.get(name) if isinstance(current.get(name), dict) else None
        same = old is not None and all(old.get(k) == new.get(k) for k in ("input", "output", "long_context"))
        table[name] = new
        before = f"${old.get('input')}/${old.get('output')}" if old else "no price"
        tier = (f", ${new['long_context']['input']}/${new['long_context']['output']} above "
                f"{new['long_context']['threshold']:,} input tokens") if "long_context" in new else ""
        verb = "unchanged" if same else f"{before} -> ${new['input']}/${new['output']}{tier}"
        changes.append(f"{name} ({upstream_id}): {verb} per 1M tokens")
    return table, changes, unmatched


def stale_warning(facts: dict, today: dt.date | None = None) -> str | None:
    try:
        made = dt.date.fromisoformat(str(facts.get("generatedAt"))[:10])
    except ValueError:
        return "the facts file has no generatedAt date; check its prices against the provider's page"
    age = ((today or dt.date.today()) - made).days
    if age > STALE_DAYS:
        return f"the facts are {age} days old (from {made}); prices may have changed since"
    return None


def write_prices(path: Path, table: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(table, indent=2) + "\n")
    os.replace(tmp, path)
