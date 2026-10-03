# Security

## Threat model

Switchboard sits between your apps and every LLM you use, so it sees all prompts and holds all provider keys. The design assumes:

- **Dependencies can be hijacked** (as LiteLLM's PyPI package was on 2026-03-24). Mitigation: minimal dependency set, exact versions with hashes, install with `--require-hashes`, `pip-audit` on every push, Dependabot updates held 7 days before proposing.
- **The network is hostile.** The server binds `127.0.0.1` by default and refuses to bind anything else unless `SWITCHBOARD_API_KEY` (≥32 chars) is set. Keys are compared in constant time. No docs/OpenAPI endpoints are exposed; `/health` returns nothing but `ok`.
- **Prompts may contain secrets or personal data.** Built-in and custom detectors force local-only routing; if no local model fits, the request is rejected (`privacy.on_no_local_model: reject`). Embeddings must use a local provider, since embedding a prompt sends it somewhere.
- **Clients are not trusted to choose destinations.** Upstream URLs come only from the config file. Plain `http` is only allowed for local hosts, URLs with embedded credentials are rejected, and HTTP redirects are not followed.
- **Logs leak.** Decision logs contain metadata and a SHA-256 prefix of the prompt, never the prompt itself, unless `logging.log_prompts: true`. The log file is created with mode `0600`.
- **Upstream providers are separate trust zones.** Each provider receives only its own key; the client's `Authorization` header and the session id are never forwarded.

## Not covered (yet)

- Multi-user isolation and per-user rate limits (single shared key today).
- Prompt-injection defence for the *content* of responses — Switchboard routes, it does not inspect model output.
- TLS termination: put a reverse proxy (Caddy, Tailscale) in front before exposing beyond localhost.
- Detectors are pattern-based and will miss some sensitive data; treat them as a safety net, not a guarantee.

## Operating checklist

- Generate the API key with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Keep `.env` and `config.yaml` out of Git (already in `.gitignore`).
- Protect `.env`: `chmod 600 .env` on Linux/macOS (Switchboard warns if others can read it); on Windows keep the repo inside your user profile, whose permissions already exclude other users.
- Pin the Docker base image to a digest before deploying.
- Enable GitHub secret scanning and push protection on the repository.
- Rotate any provider key that was ever pasted into a chat, screenshot or log.

## Reporting

Please report vulnerabilities privately via GitHub Security Advisories rather than public issues.
