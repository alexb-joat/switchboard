# VS Code with Cline

Use Trioditis as the model provider of [Cline](https://marketplace.visualstudio.com/items?itemName=saoudrizwan.claude-dev), a free, open-source coding agent for VS Code that asks before every edit and command: your provider keys stay in Trioditis, and every step is routed, capped and logged.

[← Back to the README](../README.md)

---

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
