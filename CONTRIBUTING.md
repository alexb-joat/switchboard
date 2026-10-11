# Contributing

Thanks for helping. Trioditis sits between people and every model they use, so the bar for changes is "small, tested, and no new attack surface without a reason".

## Workflow

1. **Fork** the repo and create a branch: `git switch -c fix/short-description`.
2. **Set up** (hash-verified install):
   ```bash
   python3 -m venv .venv && source .venv/bin/activate     # Windows: .\.venv\Scripts\Activate.ps1
   pip install --require-hashes --no-deps -r requirements-dev.txt
   ```
3. **Make the change** and add tests in `tests/`.
4. **Check** before pushing:
   ```bash
   python -m pytest -q
   python scripts/smoke.py
   ```
5. **Open a pull request** and fill in the template. CI, CodeQL and dependency review run automatically; for first-time contributors a maintainer approves the run first.

## Ground rules

- **Dependencies.** A new package needs a justification in the PR. Regenerate both lockfiles with [uv](https://docs.astral.sh/uv/):
  ```bash
  uv pip compile requirements.in --universal --generate-hashes --python-version 3.11 -o requirements.txt
  uv pip compile requirements-dev.in requirements.in --universal --generate-hashes --python-version 3.11 -o requirements-dev.txt
  ```
- **Network.** No new outbound calls (telemetry, update checks, remote config) without a discussion first.
- **Logging.** Never log prompt text, response text or keys by default.
- **Secrets.** Never commit `.env`, `config.yaml`, keys or real prompts — not even in tests. If one slips into a pushed commit, rotate it: forks keep history even after you delete it.
- **Security issues** go to the Security tab ("Report a vulnerability"), not public issues.

## Style

Small functions, type hints, plain names. Match the code around you.
