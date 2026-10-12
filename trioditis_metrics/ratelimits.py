"""Rate-limit headers that providers send back with every response.

Nothing universal exists, but two families cover almost everyone:

- Anthropic: anthropic-ratelimit-<name>-{limit,remaining,reset}, reset as an
  RFC 3339 time. Names: requests, tokens, input-tokens, output-tokens
  (plus anthropic-priority-* and anthropic-fast-* on those tiers).
- OpenAI and most OpenAI-compatible APIs: x-ratelimit-{limit,remaining,reset}-<name>,
  reset as a duration ("6m0s", "1s", "12ms"). OpenRouter sends x-ratelimit-{limit,
  remaining,reset} only on 429s, with reset in epoch milliseconds.

parse(headers) turns any of these into {name: {limit, remaining, reset_at}} with
reset_at as epoch seconds, so the router can store them per provider.
"""

from __future__ import annotations

import re
import time
from datetime import datetime

_FIELDS = ("limit", "remaining", "reset")
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_UNIT_S = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _reset_at(raw: str, now: float) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):  # epoch ms, epoch s, or seconds from now
        v = float(raw)
        return v / 1000 if v > 1e12 else v if v > 1e9 else now + v
    if _DURATION.fullmatch(raw) or re.fullmatch(r"(?:\d+(?:\.\d+)?(?:ms|s|m|h|d))+", raw):
        return now + sum(float(n) * _UNIT_S[u] for n, u in _DURATION.findall(raw))
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _int(raw: str) -> int | None:
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None  # Azure-style -1 means "unknown"


def parse(headers, now: float | None = None) -> dict[str, dict]:
    """headers: any mapping with case-insensitive .items() (httpx.Headers, dict)."""
    now = now if now is not None else time.time()
    out: dict[str, dict] = {}
    for key, value in headers.items():
        k = key.lower()
        prefix = next((p for p in ("anthropic-ratelimit-", "anthropic-priority-", "anthropic-fast-", "x-ratelimit-")
                       if k.startswith(p)), None)
        if not prefix:
            continue
        tail = k[len(prefix):]
        parts = tail.split("-")
        if parts[-1] in _FIELDS:        # anthropic: <name>-<field>
            field, name = parts[-1], "-".join(parts[:-1])
        elif parts[0] in _FIELDS:       # openai: <field>-<name>
            field, name = parts[0], "-".join(parts[1:])
        else:
            continue
        name = (name or "requests").replace("-", "_")
        if prefix == "anthropic-priority-":
            name = "priority_" + name
        elif prefix == "anthropic-fast-":
            name = "fast_" + name
        entry = out.setdefault(name, {"limit": None, "remaining": None, "reset_at": None})
        if field == "reset":
            entry["reset_at"] = _reset_at(str(value), now)
        else:
            entry[field] = _int(str(value))
    return {n: v for n, v in out.items() if any(x is not None for x in v.values())}
