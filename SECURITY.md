# Security policy

## Reporting a vulnerability

Please report it **privately**: open the repository's **Security** tab and choose **Report a vulnerability**
([direct link](https://github.com/alexb-joat/trioditis/security/advisories/new)). Please do not open a public issue.

Include the version (`python -m trioditis --version`), your system, what you expected and what happened, and the
smallest steps that reproduce it. Never include real API keys or prompts; a redacted log line is enough.

This is a small project with one maintainer. Expect an acknowledgement within a week; confirmed issues get a fix or a
documented mitigation as soon as practical, and reporters are credited unless they prefer not to be.

## Supported versions

Fixes land on `main` and ship in the next release. Please reproduce on the latest release or `main` first.

## In scope

Anything that lets someone who is not the owner use the router or read what it holds: bypassing the API key, the
`Host` / `Origin` checks or the body limits; getting a provider key, a prompt or the client's key into a log, a
response or an upstream request; making Trioditis call a destination that is not in `config.yaml`; a flaw in the
local-only guarantee for sensitive prompts; or a weakness in the install, container or CI supply chain.

## Out of scope

Someone who already has your user account or your `.env`; prompts the pattern-based detectors do not recognise (they
are a safety net, and new patterns are welcome as ordinary pull requests); weaknesses in a model provider's own API;
exposing the router beyond localhost without TLS and a key, against the documentation.

## Running it safely

- Keep it on `127.0.0.1`. If you must expose it, set `TRIODITIS_API_KEY` (32+ characters) and put TLS in front.
- Keep `.env` private (`chmod 600`) and out of Git; rotate any provider key that ever appeared in a chat or log.
- Install with `--require-hashes` from the lockfile, as in the README.

The design, its controls and its limits are described in [docs/security-model.md](docs/security-model.md).
