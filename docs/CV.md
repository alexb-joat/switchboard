# Switchboard — CV breakdown

## One-liner

Built a security-first LLM router: one OpenAI-compatible endpoint that analyses each prompt and sends it to the best-fit model across local, cloud and legacy LLMs.

## CV bullet (pick one)

- Designed and built **Switchboard**, an open-source LLM router (Python/FastAPI) that classifies each prompt by task, sensitivity and required capabilities and routes it across local (Ollama) and cloud models (Anthropic, OpenAI, OpenRouter) with automatic fallback.
- Shipped a self-hosted multi-model AI gateway with a privacy guard that keeps sensitive prompts on local models, hash-pinned supply chain and hardened container — built in response to the 2026 LiteLLM PyPI compromise.

## What it does

- **Semantic routing:** keyword/regex signals plus local embedding similarity map each prompt to a task route (code, reasoning, writing, quick answers).
- **Capability-aware:** filters models by vision, tool calling, context window and key availability; falls back on outages and rate limits.
- **Agent-safe:** session pinning keeps multi-step agent runs on one model.
- **Explainable:** every response reports the chosen route/model; a dry-run endpoint shows the reasoning.

## Engineering decisions worth talking about

| Decision | Why |
|---|---|
| Wrote the proxy instead of wrapping LiteLLM | Smaller attack surface (4 deps) after LiteLLM's supply-chain incident; full control of routing logic |
| One OpenAI-compatible adapter for every provider | Anthropic, OpenRouter, Ollama all expose it — no per-vendor SDKs |
| Privacy detection before routing | Data classification decides *where* a prompt may go, not just which model is best |
| Rules + embeddings, not an LLM classifier, in v1 | Sub-millisecond, deterministic and testable; LLM classifier is a roadmap item |
| Secure defaults over configuration | Localhost bind, refuses network exposure without a key, no prompt logging |

## Stack

Python 3.11+, FastAPI, HTTPX (async streaming proxy), PyYAML, pytest, Docker, GitHub Actions (SHA-pinned), Dependabot, pip-audit, uv lockfiles with hashes.

## Talking points for an interview

1. How routing works and how you'd measure routing quality (decision logs → offline eval).
2. Trade-off: cheap deterministic signals vs. an LLM classifier.
3. Supply-chain security in practice: hashes, pinning, cooldowns, least-privilege CI.
4. Data governance for AI: keeping regulated data on local models.
