"""Replay what VS Code (Cline) sends, against a running Trioditis and a real local model.

Not part of the unit suite: it needs a router (default http://127.0.0.1:8787/v1) and whatever
provider the router's config points at (Ollama, usually). Run it after `python -m trioditis serve`:

    python scripts/vscode_replay.py                      # key from TRIODITIS_API_KEY or .env
    python scripts/vscode_replay.py --url http://127.0.0.1:8787/v1 --model route/code

Checks, in order: /v1/models lists the model; the first Cline turn streams a well-formed SSE
answer (chunks, finish_reason, usage, [DONE]) with X-Trioditis headers; the follow-up turn too;
reported prompt_tokens is near what was sent (a provider that silently truncates the prompt
reports far fewer); two requests at once both finish; a client that aborts mid-stream does not
break the next request. Prints one line per check and exits non-zero on the first failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "cline_request.json"


def key_from_env() -> str:
    k = os.environ.get("TRIODITIS_API_KEY")
    if not k and Path(".env").exists():
        for line in Path(".env").read_text().splitlines():
            if line.startswith(("TRIODITIS_API_KEY=", "SWITCHBOARD_API_KEY=")):
                k = line.split("=", 1)[1].strip().strip('"').strip("'")
    return k or ""


def request(url, body=None, key="", headers=None, timeout=600):
    h = {"Accept": "*/*", "User-Agent": "ai-sdk/openai-compatible/3.0.37 (trioditis replay)", **(headers or {})}
    if key:
        h["Authorization"] = "Bearer " + key
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET"), timeout=timeout)


def read_sse(resp, stop_after: int | None = None):
    """Parse an SSE stream into (events, saw_done). stop_after=n closes after n data events (an abort)."""
    events, done, n = [], False, 0
    buf = b""
    while True:
        chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            for line in block.decode("utf-8", "replace").splitlines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done = True
                else:
                    events.append(json.loads(payload))
                    n += 1
            if stop_after and n >= stop_after:
                resp.close()
                return events, done
        if done:
            break
    return events, done


class Check:
    def __init__(self):
        self.failed = 0

    def __call__(self, name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)
        if not cond:
            self.failed += 1
            sys.exit(1)


def stream_turn(url, key, turn, check, label):
    t0 = time.monotonic()
    resp = request(url + "/chat/completions", turn, key)
    check(f"{label}: 200 and SSE", resp.status == 200 and resp.headers.get("Content-Type", "").startswith("text/event-stream"),
          f"{resp.status} {resp.headers.get('Content-Type')}")
    route, model = resp.headers.get("X-Trioditis-Route"), resp.headers.get("X-Trioditis-Model")
    check(f"{label}: routed", bool(route and model), f"route={route} model={model}")
    events, done = read_sse(resp)
    took = time.monotonic() - t0
    check(f"{label}: [DONE] after {len(events)} events in {took:.1f}s", done and events)
    finish = [c.get("finish_reason") for e in events for c in e.get("choices", []) if c.get("finish_reason")]
    check(f"{label}: finish_reason", bool(finish), str(finish))
    text = "".join(c.get("delta", {}).get("content") or "" for e in events for c in e.get("choices", []))
    calls = [tc.get("function", {}).get("name") for e in events for c in e.get("choices", [])
             for tc in (c.get("delta", {}).get("tool_calls") or []) if tc.get("function", {}).get("name")]
    print(f"     answer: {len(text)} chars of text, tool calls: {calls or 'none'}, finish: {finish}")
    usage = next((e["usage"] for e in events if e.get("usage")), None)
    check(f"{label}: usage in the stream", usage is not None, json.dumps(usage))
    sent = sum(len(json.dumps(m)) for m in turn["messages"]) + len(json.dumps(turn["tools"]))
    est = sent / 4
    ratio = usage["prompt_tokens"] / est if usage else 0
    check(f"{label}: prompt_tokens near what was sent", 0.4 < ratio < 2.5,
          f"reported {usage['prompt_tokens'] if usage else '?'}, sent ~{est:.0f} (chars/4), ratio {ratio:.2f}")
    return route, model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("TRIODITIS_URL", "http://127.0.0.1:8787/v1"))
    ap.add_argument("--model", default="auto", help="what Cline's Model ID is set to: auto, route/code, or a model name")
    ap.add_argument("--key", default=None)
    args = ap.parse_args()
    key = args.key if args.key is not None else key_from_env()
    fx = json.loads(FIXTURE.read_text())
    check = Check()
    print(f"router {args.url}, model {args.model}, key {'set' if key else 'none'}")

    models = json.loads(request(args.url + "/models", key=key, headers={"User-Agent": fx["models_request_user_agent"]}).read())
    ids = [m["id"] for m in models["data"]]
    check("GET /v1/models lists the model Cline will ask for", args.model in ids, ", ".join(ids[:8]))

    first = {**fx["turns"]["first"], "model": args.model}
    followup = {**fx["turns"]["followup"], "model": args.model}
    stream_turn(args.url, key, first, check, "first turn")
    stream_turn(args.url, key, followup, check, "follow-up")

    # Two at once: Cline never does this itself, but VS Code may have two Cline tabs.
    results, errors = {}, {}

    def one(i):
        try:
            r = request(args.url + "/chat/completions", first, key)
            ev, done = read_sse(r)
            results[i] = done and bool(ev)
        except Exception as e:  # noqa: BLE001
            errors[i] = repr(e)
    t0 = time.monotonic()
    ts = [threading.Thread(target=one, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check(f"two concurrent requests both finish ({time.monotonic() - t0:.1f}s)", results.get(0) and results.get(1), str(errors))

    # Abort mid-stream (the user pressed Cancel in Cline), then a normal request must still work.
    r = request(args.url + "/chat/completions", first, key)
    ev, _ = read_sse(r, stop_after=2)
    check("client abort after 2 events", len(ev) >= 1)
    plain = {k: v for k, v in first.items() if k != "stream_options"}  # strict providers refuse it without a stream
    r = request(args.url + "/chat/completions", {**plain, "stream": False}, key)
    body = json.loads(r.read())
    check("non-stream request after the abort", r.status == 200 and body.get("choices") and body.get("usage"),
          f"usage={body.get('usage')}")
    print("replay OK")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"FAIL HTTP {e.code} {e.reason}: {e.read()[:300]!r}")
        sys.exit(1)
