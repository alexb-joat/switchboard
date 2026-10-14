# Spending caps and provider limits

How Trioditis keeps cloud spend bounded, what it records, and what each provider reports about your account.

[← Back to the README](../README.md)

---

## Spending caps and call history

Every request is also stored in a small SQLite database, `~/.hlidskjalv/metrics.db` (owner-only, metadata only: route, model, tokens, cost, latency; never prompt text). `logs/decisions.jsonl` stays as it is.

Before any cloud call, Trioditis checks today's and this month's spend against your caps in `~/.hlidskjalv/budget.json` (defaults if the file is missing: $1/day, $15/month):

```json
{"daily_usd": 1.0, "monthly_usd": 15.0}
```

Over a cap, cloud models are skipped and the route's local model answers instead, the same way it would if the cloud model were down. The skip is logged in `failed_attempts`, so [Hlidskjalv](https://github.com/alexb-joat/hlidskjalv) shows it. A route with no local model returns `429 budget_exceeded` instead of a bill. Edits to the file apply on the next request, no restart needed.

Spend is calculated from `~/.hlidskjalv/prices.json`, the same file Hlidskjalv reads (USD per million tokens, keyed by model name in `config.yaml`):

```json
{"claude-sonnet": {"input": 3.0, "output": 15.0}}
```

**A cloud model without a price counts as $0 toward the caps** unless you set `"assume_unpriced_usd": 0.05` (an assumed cost per such call) in `budget.json`; Hlidskjalv raises an alert when calls without a price happen, so add every cloud model you use. Streaming answers carry their token usage too: Trioditis asks the provider for it (OpenAI's `stream_options`) and retries once without for a provider that doesn't know the option, or set `stream_usage: false` on that provider. Local models always cost $0.

**Caps per provider** go in the same file, under the provider names from `config.yaml`, on top of the global ones:

```json
{"daily_usd": 1.0, "monthly_usd": 15.0, "providers": {"openrouter": {"monthly_usd": 5.0}, "anthropic": {"daily_usd": 0.5}}}
```

A provider over its own cap is skipped while the others stay available.

## What providers report about your account

There is no universal API for balances and limits, so Trioditis collects what each one offers and writes it to `~/.hlidskjalv/accounts.json` every 5 minutes (also `GET /v1/accounts`), for Hlidskjalv to show:

| Provider (by host) | What you get | What it needs |
|---|---|---|
| openrouter.ai | The key's usage and limit, budgets; with a *management* key in `admin_key_env`, the account balance | the normal API key |
| api.anthropic.com | This month's organization spend from the Admin cost report | an Admin API key (`sk-ant-admin…`) in `admin_key_env`; organizations only, individual accounts have no Admin API |
| api.openai.com | This month's organization costs | an admin key in `admin_key_env` |
| any other | nothing yet (an adapter is ~30 lines in `trioditis/accounts.py`) | |

```yaml
providers:
  openrouter:
    base_url: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY
    admin_key_env: OPENROUTER_MANAGEMENT_KEY   # optional, read-only use
```

Admin keys are only ever sent to that provider's own host, with GET requests, and never used for model calls. Independently of adapters, the **rate-limit headers** every provider returns (requests and tokens remaining, when they refill) are kept per provider in the metrics database.

Your Claude *subscription* (the 5-hour and weekly limits of a Pro/Max plan) is not reachable through an API key at all; Hlidskjalv estimates it from Claude Code's transcripts instead.
