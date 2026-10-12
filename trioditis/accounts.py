"""What each provider knows about your account: balance, spend, caps.

There is no universal API for this, so there's one small adapter per
provider, chosen by the host in the provider's configured base_url, never a
hard-coded third party. Every adapter returns the same shape, so Hlidskjalv
can draw one kind of meter for all of them:

    {"kind": "credits" | "spend", "label": str, "used": float, "limit": float | None,
     "remaining": float | None, "currency": "USD", "period": "prepaid" | "month" | "day",
     "resets_at": float | None, "source": url, "checked_at": epoch}

or {"error": "..."} when it can't (no key, wrong key type, network). What each
needs:

- openrouter.ai: the normal API key. GET /api/v1/key reports the key's usage and
  limit, plus any budgets. GET /api/v1/credits (account balance) needs a
  *management* key: set admin_key_env for that.
- api.anthropic.com: the Admin API (organizations only; individual accounts have
  none): admin_key_env holding an sk-ant-admin... key. This month's cost report.
- api.openai.com: an admin key in admin_key_env. This month's organization costs.
- Local providers (Ollama): nothing to report; they're free.

Only GET requests to the provider's own host; keys never leave the process
otherwise. Results go to ~/.hlidskjalv/accounts.json every ACCOUNTS_EVERY_S.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from .config import Config, Provider

ACCOUNTS_EVERY_S = 300


def _month_start_utc(now: float) -> datetime:
    t = datetime.fromtimestamp(now, tz=timezone.utc)
    return t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _origin(p: Provider) -> str:
    u = urlparse(p.base_url)
    return f"{u.scheme}://{u.netloc}"


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


async def _get(client: httpx.AsyncClient, url: str, headers: dict, params: dict | None = None) -> dict:
    r = await client.get(url, headers=headers, params=params, timeout=15.0)
    if r.status_code != 200:
        raise AccountError(f"{r.status_code} from {urlparse(url).path}" + (
            ": only a management/admin key can read this" if r.status_code in (401, 403) else ""))
    data = r.json()
    if not isinstance(data, dict):
        raise AccountError("unexpected response")
    return data


class AccountError(Exception):
    pass


async def openrouter(p: Provider, client: httpx.AsyncClient, now: float) -> dict:
    key = p.api_key()
    if not key:
        raise AccountError("no API key")
    url = f"{_origin(p)}/api/v1/key"
    try:
        d = (await _get(client, url, {"Authorization": f"Bearer {key}"})).get("data") or {}
    except AccountError as e:
        if not str(e).startswith("404"):
            raise
        url = f"{_origin(p)}/api/v1/auth/key"  # the older path
        d = (await _get(client, url, {"Authorization": f"Bearer {key}"})).get("data") or {}
    budget = d.get("effective_budget") if isinstance(d.get("effective_budget"), dict) else None
    out = {"kind": "credits", "currency": "USD", "source": url, "checked_at": now,
           "used": _num(d.get("usage")) or 0.0, "used_month": _num(d.get("usage_monthly")),
           "used_day": _num(d.get("usage_daily")), "free_tier": bool(d.get("is_free_tier"))}
    if budget:  # a spending budget set on OpenRouter: the closest thing to a plan meter
        out.update(label="OpenRouter budget", period=str(budget.get("reset_interval") or "prepaid"),
                   used=_num(budget.get("spend_usd")) or 0.0, limit=_num(budget.get("limit_usd")),
                   remaining=_num(budget.get("remaining_usd")))
        ra = budget.get("resets_at")
        if isinstance(ra, str):
            try:
                out["resets_at"] = datetime.fromisoformat(ra.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    else:
        out.update(label="OpenRouter key", period="prepaid", limit=_num(d.get("limit")),
                   remaining=_num(d.get("limit_remaining")))
    admin = p.admin_key()
    if admin:  # account balance needs a management key
        try:
            c = (await _get(client, f"{_origin(p)}/api/v1/credits", {"Authorization": f"Bearer {admin}"})).get("data") or {}
            bought, spent = _num(c.get("total_credits")), _num(c.get("total_usage"))
            if bought is not None and spent is not None:
                out.update(label="OpenRouter balance", kind="credits", period="prepaid", used=spent, limit=bought,
                           remaining=round(bought - spent, 6))
        except (AccountError, httpx.HTTPError, ValueError) as e:
            out["note"] = f"balance not read: {e}"
    return out


async def anthropic(p: Provider, client: httpx.AsyncClient, now: float) -> dict:
    admin = p.admin_key()
    if not admin:
        raise AccountError("needs an Admin API key (organizations only) in admin_key_env")
    start = _month_start_utc(now)
    url = f"{_origin(p)}/v1/organizations/cost_report"
    headers = {"x-api-key": admin, "anthropic-version": "2023-06-01", "User-Agent": "Trioditis (https://github.com/alexb-joat/trioditis)"}
    params: dict = {"starting_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "ending_at": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 31}
    cents = 0.0
    for _ in range(10):  # paginated
        d = await _get(client, url, headers, params)
        for bucket in d.get("data") or []:
            for r in (bucket.get("results") or []) if isinstance(bucket, dict) else []:
                if isinstance(r, dict) and str(r.get("currency", "USD")).upper() == "USD":
                    try:
                        cents += float(r.get("amount") or 0)  # decimal strings, in cents
                    except (TypeError, ValueError):
                        pass
        if not d.get("has_more") or not d.get("next_page"):
            break
        params["page"] = d["next_page"]
    return {"kind": "spend", "label": "Anthropic organization, this month", "currency": "USD", "period": "month",
            "used": round(cents / 100, 4), "limit": None, "remaining": None, "source": url, "checked_at": now,
            "note": "Caps live on the Console billing page; the API doesn't report them."}


async def openai(p: Provider, client: httpx.AsyncClient, now: float) -> dict:
    admin = p.admin_key()
    if not admin:
        raise AccountError("needs an organization admin key in admin_key_env")
    url = f"{_origin(p)}/v1/organization/costs"
    params: dict = {"start_time": int(_month_start_utc(now).timestamp()), "bucket_width": "1d", "limit": 31}
    total = 0.0
    for _ in range(10):
        d = await _get(client, url, {"Authorization": f"Bearer {admin}"}, params)
        for bucket in d.get("data") or []:
            for r in (bucket.get("results") or []) if isinstance(bucket, dict) else []:
                amt = r.get("amount") if isinstance(r, dict) else None
                if isinstance(amt, dict) and str(amt.get("currency", "usd")).lower() == "usd":
                    total += _num(amt.get("value")) or 0.0
        if not d.get("has_more") or not d.get("next_page"):
            break
        params["page"] = d["next_page"]
    return {"kind": "spend", "label": "OpenAI organization, this month", "currency": "USD", "period": "month",
            "used": round(total, 4), "limit": None, "remaining": None, "source": url, "checked_at": now}


ADAPTERS = {"openrouter.ai": openrouter, "api.anthropic.com": anthropic, "api.openai.com": openai}


def adapter_for(p: Provider):
    return ADAPTERS.get(urlparse(p.base_url).hostname or "")


async def report(cfg: Config, client: httpx.AsyncClient, now: float | None = None) -> dict:
    """{provider name: account dict or {"error": ...}} for every configured cloud provider."""
    now = now if now is not None else time.time()
    out: dict = {}
    for name, p in cfg.providers.items():
        if p.local:
            continue
        fn = adapter_for(p)
        if fn is None:
            out[name] = {"error": "no adapter for this provider yet", "host": urlparse(p.base_url).hostname, "checked_at": now}
            continue
        if not p.available():
            out[name] = {"error": "no API key set", "checked_at": now}
            continue
        try:
            out[name] = await fn(p, client, now)
        except (AccountError, httpx.HTTPError, ValueError) as e:
            out[name] = {"error": str(e)[:200], "checked_at": now}
    return out
