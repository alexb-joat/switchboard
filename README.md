# Τριοδῖτις

*Trioditis*: Hecate of the three ways, who stands where the road splits and decides who goes where. Every prompt reaches the same crossroads here: a local model, a cloud model, or no further.


[![ci](https://github.com/alexb-joat/trioditis/actions/workflows/ci.yml/badge.svg)](https://github.com/alexb-joat/trioditis/actions/workflows/ci.yml)
[![codeql](https://github.com/alexb-joat/trioditis/actions/workflows/codeql.yml/badge.svg)](https://github.com/alexb-joat/trioditis/actions/workflows/codeql.yml)
![python](https://img.shields.io/badge/python-3.11%2B-3776AB)
![platforms](https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-555)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**One OpenAI-compatible endpoint that picks the right LLM for each prompt** — local models via Ollama, cloud models (Claude, GPT, anything on OpenRouter), and legacy models — with security as a design constraint, not an add-on.

```
Any OpenAI-compatible app  ──►  Trioditis  ──►  local models (Ollama)
(Odysseus, Open WebUI,          model: "auto"      cloud APIs (Anthropic, OpenAI, OpenRouter)
 scripts, agents)                                  legacy models (by name)
```

About 2,600 lines of Python. Four runtime dependencies (FastAPI, Uvicorn, HTTPX, PyYAML), hash-pinned. No LiteLLM, no LangChain, no telemetry.

## How a request is routed

1. **Privacy check** — built-in detectors (emails, card numbers, IBANs, API keys, private keys, passwords) plus your own keywords/regexes. A hit restricts the request to **local models only**; if none can serve it, the request is rejected rather than leaked.
2. **Pick a route** (`code`, `reasoning`, `writing`, `quick`, … — you define them):
   - keyword and regex signals, then
   - semantic similarity to each route's example prompts (optional, using a *local* embedding model), else
   - the default route.
3. **Filter models** by what the request needs: vision (image attached), tool calling, context length, and whether the provider's API key is present.
4. **Call the first model; fall back** to the next on outage, rate limit or missing model.
5. **Session pinning** — requests carrying the same `X-Session-Id` (or `user`) stay on one route, so agents don't switch models mid-task.

Every response carries `X-Trioditis-Route` and `X-Trioditis-Model`. `POST /v1/route` (or `trioditis explain "…"`) shows the decision and reasons without calling any model.

Clients can also bypass routing: `model: "route/code"` forces a route, `model: "legacy-mistral"` calls a model directly.

## Platforms

| OS | Python | Run in background | CI |
|---|---|---|---|
| Linux (Ubuntu, Kubuntu, Fedora, Arch…) | 3.11+ | systemd user service | ✅ |
| macOS (Apple Silicon / Intel) | 3.11+ | launchd agent | ✅ |
| Windows 10/11 | 3.11+ | Scheduled Task | ✅ |

CI runs the unit tests and a real-server smoke test on all three, on Python 3.11 and 3.13.

## Quick start

**1. Install [Ollama](https://ollama.com/download)** and pull a few local models. Run it natively, not in Docker (on macOS, Docker can't use the GPU). It listens on `127.0.0.1:11434` by default; keep it that way.

```bash
ollama pull llama3.2:3b
ollama pull qwen2.5-coder:7b
ollama pull nomic-embed-text
```

**2. Install Trioditis** (hash-verified: every package must match the lockfile).

Linux / macOS:

```bash
python3 -m venv .venv && source .venv/bin/activate    # Debian/Ubuntu: sudo apt install python3-venv
pip install --require-hashes --no-deps -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env && chmod 600 .env
```

Windows (PowerShell):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --require-hashes --no-deps -r requirements.txt
Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env
```

**3. Check and run** (same on every OS):

```bash
python -m trioditis check     # which models are usable
python -m trioditis serve     # http://127.0.0.1:8787/v1, localhost only
```

Add cloud API keys to `.env` only if you want cloud models; it's read automatically from the working directory, and variables already set in your environment take precedence.

## Run in the background

Each script installs a per-user autostart (no admin/root), bound to localhost, and has a `--remove` / `-Remove` option.

| OS | Command | Logs |
|---|---|---|
| Linux | `./deploy/linux/install-service.sh` | `journalctl --user -u trioditis -f` |
| macOS | `./deploy/macos/install-service.sh` | `logs/server.log` |
| Windows | `powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1` | `logs\server.log` |

## Try it

```bash
python -m trioditis explain "Why does my python function raise KeyError?"
```

Linux / macOS:

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Write a regex for UK postcodes"}]}'
```

Windows (PowerShell):

```powershell
$body = @{ model = "auto"; messages = @(@{ role = "user"; content = "Write a regex for UK postcodes" }) } | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:8787/v1/chat/completions -Method Post -ContentType "application/json" -Body $body
```

Connect a UI: in Odysseus, Open WebUI or any OpenAI-compatible client, add the endpoint `http://127.0.0.1:8787/v1` and pick model `auto`.

## VS Code (Cline)

[Cline](https://marketplace.visualstudio.com/items?itemName=saoudrizwan.claude-dev) is a free, open-source coding agent for VS Code that asks before every edit and command. Pointed at Trioditis, your provider keys stay in Trioditis's `.env` and never enter VS Code.

1. Start Trioditis (`python -m trioditis serve`, or the background service).
2. In VS Code: Extensions → **Cline** → Install. In Cline's settings choose:
   - **API Provider:** OpenAI Compatible
   - **Base URL:** `http://127.0.0.1:8787/v1`
   - **API Key:** your `TRIODITIS_API_KEY` from `.env` (any text if you haven't set one)
   - **Model ID:** `route/code` (recommended), or `auto`

**Why `route/code`:** Cline's task text is short ("create hello.txt with one line"), and the classifier only sees that line, not Cline's system prompt, so `auto` often lands on the `quick` route and a small general model. `route/code` pins the coding route and still falls back down its list. Cline uses native tool calling, so only models with `capabilities: [tools]` are eligible; the others are skipped with a reason you can see in `python -m trioditis explain` or in Hlidskjalv.

**Local models and context.** Cline sends 3–4k tokens before you type anything, and every tool result adds to it. Ollama's default context window is 4,096 tokens, and it cuts the oldest part of the prompt *silently* when that is exceeded, so a local model "forgets" the task. Raise it once, for example `OLLAMA_CONTEXT_LENGTH=16384` in Ollama's environment (`systemctl edit ollama` on Linux, the app's settings on macOS/Windows). Trioditis compares the tokens a provider reports with what it sent and marks a decision `prompt_truncated` when they differ a lot; Hlidskjalv shows that reason. A 3B model answers Cline in 10–20 s on a laptop CPU; a 7B model takes longer and may hit the 120 s `upstream_timeout_s` on the first, uncached call. Keep a strong cloud model first in the `code` route when you have a key; local models shine on the quick and writing routes.

`python scripts/vscode_replay.py` sends the request shapes Cline really uses (`tests/fixtures/cline_request.json`) to a running router and checks the stream, usage and routing, with no VS Code involved.

See every routing decision live: [Hlidskjalv](https://github.com/alexb-joat/hlidskjalv) reads the decision log (route, model, tokens; never prompts) and shows it as a game-like map of your work.

## Docker (optional)

Running natively is simpler and keeps Ollama on localhost. If you want the container:

```bash
cp config.example.yaml config.yaml   # set the ollama base_url to http://host.docker.internal:11434/v1
cp .env.example .env                 # TRIODITIS_API_KEY is required inside a container
docker compose up -d --build
```

- **macOS / Windows (Docker Desktop):** `host.docker.internal` reaches Ollama on the host; nothing else to change.
- **Linux:** the container can only reach Ollama if Ollama also listens on the Docker bridge. Use `OLLAMA_HOST=172.17.0.1:11434`, **not** `0.0.0.0`, which would expose your models to the whole network.

The container runs as a non-root user on a read-only filesystem with all capabilities dropped, and is published on `127.0.0.1` only.

## Security

See [SECURITY.md](SECURITY.md) for the threat model. In short:

| Risk | Control |
|---|---|
| Malicious/hijacked dependency | 4 direct deps, every package hash-pinned (`--require-hashes`), `pip-audit` in CI, Dependabot with a 7-day cooldown |
| Router exposed to the network | Binds `127.0.0.1` by default; refuses any other bind without a ≥32-char API key |
| A web page in your browser reaching the router | Without a key, requests with a foreign `Host` (DNS rebinding) or `Origin` (cross-site POST) get 403 and a line in `logs/security.jsonl`; POST bodies must be `application/json` |
| Key leakage | Keys only in env vars (config loader rejects inline keys); client's router key is never forwarded upstream; local models get no key |
| Sensitive data sent to the cloud by accident | Privacy detectors force local-only routing or reject |
| Open proxy / SSRF | Upstream URLs only from config; plain `http` only for local hosts; redirects not followed |
| Prompt content in logs | Logs hold route, model, latency, detector names and a hash — not prompt text (opt-in only) |
| Abuse / DoS | Body size cap, upstream timeouts, bounded fallbacks and session cache |
| Compromised CI | Workflow is read-only, actions pinned to commit SHAs, no persisted credentials |

## Configuration

Everything lives in `config.yaml` (see the commented [example](config.example.yaml)): providers, models (with `capabilities` and `context_tokens`), routes (`keywords`, `patterns`, `examples`, `local_only`), embeddings, privacy, limits and logging.

## Tests

```bash
pip install --require-hashes --no-deps -r requirements-dev.txt
python -m pytest -q
```

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

## Hardware-aware suggestions

```bash
python -m trioditis hardware          # readable report
python -m trioditis hardware --json   # same data as GET /v1/hardware
```

Trioditis detects your CPU, RAM and GPU (NVIDIA via `nvidia-smi`, AMD via `/sys`, Apple Silicon via `sysctl`; stdlib only, every probe fails soft), lists the models installed in your local Ollama, and suggests per route where a local model could take work from a paid cloud model — with the reasoning spelled out:

- **Memory budget:** dedicated VRAM minus ~10% on a discrete GPU; ~65-75% of unified memory on Apple Silicon; otherwise 70% of RAM while keeping at least 4 GB for the OS (integrated GPUs share that RAM, so it is counted once). A model needs roughly its file size +20% for context.
- **Cost first:** routes that start with a cloud model get a "put the local model first" suggestion (cloud stays as fallback); routes without a usable local model get the best one that fits, preferring models you already have (zero download). Configured models that won't fit are flagged with a smaller alternative.
- **Honest numbers:** catalog sizes are approximate (check with `ollama show`); cloud prices are quoted only if `~/.hlidskjalv/prices.json` exists.

It prints YAML to paste and never edits `config.yaml`. Hardware facts never leave the machine: the only calls are `GET /api/tags` to your local providers. `GET /v1/hardware` sits behind the same API key as every other endpoint.

## Roadmap

- [ ] Small local LLM as an optional classifier for ambiguous prompts
- [ ] Learn from feedback: per-route model scores from thumbs up/down
- [x] Daily and monthly spending caps with local fallback
- [ ] Cost and latency budgets per route
- [ ] Native Anthropic/Gemini adapters (today: their OpenAI-compatible endpoints)
- [ ] Per-user keys and rate limits for team use
- [x] Hardware detection and local-model suggestions (`trioditis hardware`)

## Contributing

Pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Report security issues privately via the Security tab.

## Support

If Trioditis saves you money or time, you can support it with the **Sponsor** button at the top of the repo.

## License

MIT. Not affiliated with Anthropic, OpenAI, Ollama, OpenRouter or any model provider; their names refer only to their public APIs.
