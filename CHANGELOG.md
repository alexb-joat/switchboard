# Changelog

## 0.4.0 (2026-10-30)

Ready for batch jobs: an evaluation or pipeline can pin a model version, read back what one run cost, and wait on a rate limit instead of failing.

**Spending**
- Caps hold each cloud call's estimated cost (prompt size and `max_tokens` at your prices) from the moment it starts until its real cost is recorded, so a burst of parallel requests cannot overshoot a cap. A call whose estimate would cross a cap is skipped like one over it.
- Prices can carry a `long_context` tier (for example Claude Haiku 5.5 above 100,000 prompt tokens), applied to the whole call.
- `trioditis prices import provider-facts.json`: prices from a sourced price list, matched by model id, each with its source URL and date, instead of typed by hand.

**Usage reports**
- `trioditis usage`: calls, errors, tokens and spend per model, route, provider, calling tool, session or day; `--json` for scripts. Reads the metrics database read-only and needs no config.
- Each call records the calling tool (`X-Trioditis-Client`, or the first word of `User-Agent`) and a hash of its session id.

**Models and rate limits**
- A request can name a model by its provider id (`claude-haiku-5-5`) to pin an exact version; `X-Trioditis-Upstream-Model` says which id the provider was sent.
- When every model tried answers "too many requests", the client gets `429 rate_limited` with `Retry-After` (the shortest wait asked for) instead of a 502. `budget_exceeded` keeps meaning stop. The decision log records which.
- Example config: Claude Haiku 5.5 and Sonnet 5.5 with their 1M context, and `claude-opus` (Opus 5.5) callable by name, in no route.

**Security and engineering**
- Dependencies: `opentelemetry-api` 1.45.2 (unbounded `tracestate` parsing, GHSA-xcq7-p3pf-p88h) and `certifi` 2026.10.23 (PYSEC-2026-4320).
- ruff in CI (pyflakes, bugbear, import order, pyupgrade, bandit security rules), hash-pinned like everything else. It found a JSON error that chained the decoder's traceback and a cosine similarity that silently truncated vectors of different lengths; both fixed.

## 0.3.0 (2026-10-14)

First public release. Earlier versions were private and named Switchboard.

**Security**
- Refuses web pages in your browser: a foreign `Host` (DNS rebinding) or `Origin` gets 403 and a log line, and POST bodies must be JSON.
- Request bodies are capped while they are read, not after; every 413 is logged.
- `logs/decisions.jsonl` (50 MB) and `logs/security.jsonl` (5 MB) rotate to one backup.
- Refused requests are logged to `logs/security.jsonl`.
- Hardware probes run from system directories, never from `PATH`.
- Docker base image pinned by digest.

**Works with VS Code (Cline)**
- Tested against what Cline really sends (native tool calling, a large system prompt, streamed usage): a fixture, unit tests and a live replay script (`scripts/vscode_replay.py`).
- Streamed answers are relayed decoded. A gzip-compressed upstream stream (Anthropic) used to end in "stream ended without a finish reason".
- Prompts a local model probably cut short (Ollama's context limit) are flagged in the decision log.
- Setup guide: [docs/vscode-cline.md](docs/vscode-cline.md).

**Spending and limits**
- SQLite call history; daily and monthly spending caps with automatic local fallback.
- Caps per provider; provider rate limits and account balances where the provider reports them.
- Usage is requested on streams; cloud calls without a price are recorded and can be charged at an assumed price.

**Other**
- `trioditis hardware`: detects CPU, RAM and GPU and suggests local models that fit.
- `trioditis --version`.
- Renamed from Switchboard; the old `SWITCHBOARD_API_KEY` is still read.
- CI on Linux, macOS and Windows (two Windows generations), Python 3.11 to 3.13; CodeQL and dependency review.
- Short README; reference moved to `docs/`.

## Before 0.3.0 (private)

- 0.1 (2026-10-03): the router. Privacy detectors with local-only routing, routes by keywords and local embeddings, capability filters, fallback, session pinning, hash-pinned dependencies, localhost-only binding.
- Contributor, review and security tooling; token usage and local/cloud split logged per request.
