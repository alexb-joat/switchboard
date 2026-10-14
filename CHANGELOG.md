# Changelog

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
