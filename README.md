# Switchboard

[![ci](https://github.com/alexb-joat/switchboard/actions/workflows/ci.yml/badge.svg)](https://github.com/alexb-joat/switchboard/actions/workflows/ci.yml)
[![codeql](https://github.com/alexb-joat/switchboard/actions/workflows/codeql.yml/badge.svg)](https://github.com/alexb-joat/switchboard/actions/workflows/codeql.yml)
![python](https://img.shields.io/badge/python-3.11%2B-3776AB)
![platforms](https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-555)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**One OpenAI-compatible endpoint that picks the right LLM for each prompt** — local models via Ollama, cloud models (Claude, GPT, anything on OpenRouter), and legacy models — with security as a design constraint, not an add-on.

```
Any OpenAI-compatible app  ──►  Switchboard  ──►  local models (Ollama)
(Odysseus, Open WebUI,          model: "auto"      cloud APIs (Anthropic, OpenAI, OpenRouter)
 scripts, agents)                                  legacy models (by name)
```

~800 lines of Python. Four runtime dependencies (FastAPI, Uvicorn, HTTPX, PyYAML), hash-pinned. No LiteLLM, no LangChain, no telemetry.

## How a request is routed

1. **Privacy check** — built-in detectors (emails, card numbers, IBANs, API keys, private keys, passwords) plus your own keywords/regexes. A hit restricts the request to **local models only**; if none can serve it, the request is rejected rather than leaked.
2. **Pick a route** (`code`, `reasoning`, `writing`, `quick`, … — you define them):
   - keyword and regex signals, then
   - semantic similarity to each route's example prompts (optional, using a *local* embedding model), else
   - the default route.
3. **Filter models** by what the request needs: vision (image attached), tool calling, context length, and whether the provider's API key is present.
4. **Call the first model; fall back** to the next on outage, rate limit or missing model.
5. **Session pinning** — requests carrying the same `X-Session-Id` (or `user`) stay on one route, so agents don't switch models mid-task.

Every response carries `X-Switchboard-Route` and `X-Switchboard-Model`. `POST /v1/route` (or `switchboard explain "…"`) shows the decision and reasons without calling any model.

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

**2. Install Switchboard** (hash-verified: every package must match the lockfile).

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
python -m switchboard check     # which models are usable
python -m switchboard serve     # http://127.0.0.1:8787/v1, localhost only
```

Add cloud API keys to `.env` only if you want cloud models; it's read automatically from the working directory, and variables already set in your environment take precedence.

## Run in the background

Each script installs a per-user autostart (no admin/root), bound to localhost, and has a `--remove` / `-Remove` option.

| OS | Command | Logs |
|---|---|---|
| Linux | `./deploy/linux/install-service.sh` | `journalctl --user -u switchboard -f` |
| macOS | `./deploy/macos/install-service.sh` | `logs/server.log` |
| Windows | `powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1` | `logs\server.log` |

## Try it

```bash
python -m switchboard explain "Why does my python function raise KeyError?"
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

## Docker (optional)

Running natively is simpler and keeps Ollama on localhost. If you want the container:

```bash
cp config.example.yaml config.yaml   # set the ollama base_url to http://host.docker.internal:11434/v1
cp .env.example .env                 # SWITCHBOARD_API_KEY is required inside a container
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

## Roadmap

- [ ] Small local LLM as an optional classifier for ambiguous prompts
- [ ] Learn from feedback: per-route model scores from thumbs up/down
- [ ] Cost and latency budgets per route
- [ ] Native Anthropic/Gemini adapters (today: their OpenAI-compatible endpoints)
- [ ] Per-user keys and rate limits for team use

## Contributing

Pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Report security issues privately via the Security tab.

## Support

If Switchboard saves you money or time, you can support it with the **Sponsor** button at the top of the repo.

## License

MIT. Not affiliated with Anthropic, OpenAI, Ollama, OpenRouter or any model provider; their names refer only to their public APIs.
