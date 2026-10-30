# Spending caps and provider limits

How Trioditis keeps cloud spend bounded, what it records, and what each provider reports about your account.

[← Back to the README](../README.md)

---

## Spending caps and call history

Every request is also stored in a small SQLite database, `~/.hlidskjalv/metrics.db` (owner-only, metadata only: route, model, tokens, cost, latency, the calling tool's name and a hash of the session id; never prompt text). `logs/decisions.jsonl` stays as it is.

Before any cloud call, Trioditis checks today's and this month's spend against your caps in `~/.hlidskjalv/budget.json` (defaults if the file is missing: $1/day, $15/month):

```json
{"daily_usd": 1.0, "monthly_usd": 15.0}
```

**Each call is checked before it starts, including its own estimated cost.** Trioditis estimates the call from the prompt size (about four characters per token) and the request's `max_tokens` (or `assume_output_tokens` from `budget.json`, default 1024), and holds that amount against the caps until the real cost is recorded. A burst of parallel requests therefore cannot all slip under a cap that only the first of them fits: a call whose estimate would cross a cap is refused the same way as one over it, with a reason such as `daily cap would be crossed ($0.9700 spent + $0.0200 pending + $0.0300 this call > $1.00)`. A hold that never reports back (a stream the client dropped before it began) expires after 15 minutes.

Over a cap, cloud models are skipped and the route's local model answers instead, the same way it would if the cloud model were down. The skip is logged in `failed_attempts`, so [Hlidskjalv](https://github.com/alexb-joat/hlidskjalv) shows it. A route with no local model returns `429 budget_exceeded` instead of a bill. Edits to the file apply on the next request, no restart needed.

Spend is calculated from `~/.hlidskjalv/prices.json`, the same file Hlidskjalv reads (USD per million tokens, keyed by model name in `config.yaml`):

```json
{"claude-sonnet": {"input": 3.0, "output": 15.0}}
```

Rather than typing prices, import them from a provider-facts file, which lists each model's price with the documentation page it came from (for example Euboulia's [`provider-facts.json`](https://github.com/alexb-joat/euboulia/blob/main/src/catalog/provider-facts.json)). Models are matched by the `id` in `config.yaml`; prices of models it does not list are left as they are, and each imported entry keeps its `_source` and `_fetched` date:

```bash
python -m trioditis prices import provider-facts.json --dry-run   # show old -> new
python -m trioditis prices import provider-facts.json
```

It warns when the facts are more than 30 days old.

A model whose price rises with the prompt size carries a tier. Above `threshold` prompt tokens, the tier's prices apply to the whole call:

```json
{"claude-haiku": {"input": 0.1, "output": 0.5, "long_context": {"threshold": 100000, "input": 0.5, "output": 2.5}}}
```

**A cloud model without a price counts as $0 toward the caps** unless you set `"assume_unpriced_usd": 0.05` (an assumed cost per such call) in `budget.json`; Hlidskjalv raises an alert when calls without a price happen, so add every cloud model you use. Streaming answers carry their token usage too: Trioditis asks the provider for it (OpenAI's `stream_options`) and retries once without for a provider that doesn't know the option, or set `stream_usage: false` on that provider. Local models always cost $0.

**Caps per provider** go in the same file, under the provider names from `config.yaml`, on top of the global ones:

```json
{"daily_usd": 1.0, "monthly_usd": 15.0, "providers": {"openrouter": {"monthly_usd": 5.0}, "anthropic": {"daily_usd": 0.5}}}
```

A provider over its own cap is skipped while the others stay available.

## When a provider says "too many requests"

A model that answers `429` is skipped like one that is down, and the next candidate is tried. If **every** model that was tried answered `429`, the client gets a `429` with `"type": "rate_limited"` and a `Retry-After` header holding the shortest wait any provider asked for (a provider's seconds or HTTP date, at most an hour), instead of a `502` it would retry at once. A route over its budget still answers `429` with `"type": "budget_exceeded"`, which means stop, not wait. Trioditis never sleeps inside a request; waiting is the client's call. It does remember the wait: until it is over (or until a reported rate limit at zero refills), that provider's models are tried last by every request, so the next ones go straight to a model that can answer. `logs/decisions.jsonl` records which of the two it was in an `error` field.

## Usage reports

`trioditis usage` reads the metrics database and prints calls, errors, tokens and spend per group. It needs no config and never writes to the database, so it is safe while the router runs:

```bash
python -m trioditis usage                          # last 24 hours, per model
python -m trioditis usage --since 7d --by client   # per calling tool
python -m trioditis usage --since month --by day
python -m trioditis usage --session eval/run-7 --json
```

`--since` and `--until` take `30m`, `24h`, `7d`, `2w`, `today`, `month` or a date (`2026-10-01`). `--by` is `model`, `route`, `provider`, `client`, `session` or `day`.

**Per tool.** Each call records the calling tool's name: the `X-Trioditis-Client` header when a client sets one, else the first word of its `User-Agent` (`ninshubur/0.1 …` → `ninshubur`, `curl/8.5` → `curl`). Tools built on a shared library show that library's name; Cline, for example, sends `ai-sdk/openai-compatible/…` and appears as `ai-sdk`.

**Per session.** Requests that carry `X-Session-Id` (or the OpenAI `user` field) are stored under a hash of it, the same hash `logs/decisions.jsonl` holds; `--session ID` hashes the id you give and matches on that. A batch job that sends `X-Session-Id: myjob/<run id>` can therefore read back exactly what one run cost.

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
