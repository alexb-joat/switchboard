"""Cross-platform smoke test: start the real server, probe it, stop it.

Runs the same way on Linux, macOS and Windows (CI runs it on all three).
Calls no model: only /health, the dry-run /v1/route and the bind guard.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

CONFIG = """
providers:
  local: {base_url: "http://127.0.0.1:9/v1", local: true}
models:
  small: {provider: local, id: tiny}
  coder: {provider: local, id: coder}
routes:
  code: {models: [coder], keywords: [python]}
  quick: {models: [small]}
default_route: quick
logging: {path: null}
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg = tmp / "config.yaml"
    cfg.write_text(CONFIG, encoding="utf-8")
    repo = Path(__file__).resolve().parent.parent
    env = {**os.environ, "TRIODITIS_API_KEY": "", "TRIODITIS_ENV_FILE": str(tmp / "none"),
           "HLIDSKJALV_HOME": str(tmp / "home"),  # never touch the real ~/.hlidskjalv
           "PYTHONPATH": str(repo)}

    # 1. Refuses to listen beyond localhost without a key.
    r = subprocess.run(
        [sys.executable, "-m", "trioditis", "--config", str(cfg), "serve", "--host", "0.0.0.0"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode != 0 and "Refusing to bind" in r.stderr, r.stderr

    # 2. Serves on localhost.
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "trioditis", "--config", str(cfg), "serve", "--port", str(port)],
        env=env, cwd=tmp,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=1) as resp:
                    assert json.load(resp) == {"status": "ok"}
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise SystemExit("server did not start")

        req = urllib.request.Request(
            f"{base}/v1/route",
            data=json.dumps({"model": "auto", "messages": [
                {"role": "user", "content": "fix my python script"}]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            decision = json.load(resp)
        assert decision["route"] == "code" and decision["model"] == "coder", decision
        print(f"smoke OK on {sys.platform}: {decision['route']} -> {decision['model']}")
    finally:
        proc.terminate()
        proc.wait(timeout=15)


if __name__ == "__main__":
    main()
