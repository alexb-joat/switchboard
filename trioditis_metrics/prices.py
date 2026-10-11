"""Per-token prices for cloud models, from ~/.hlidskjalv/prices.json.

Same file and format Hlidskjalv already reads, so one price list feeds
both Hlidskjalv's cost display and Trioditis' budget guard:

    {"claude-sonnet": {"input": 3.0, "output": 15.0}}   # USD per 1M tokens

Keys are Trioditis model names (config.yaml). Keys starting with "_" are
notes. The file is re-read when it changes, so edits apply without a restart.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .home import data_home

DEFAULT_PRICES_PATH = data_home() / "prices.json"


class Prices:
    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_PRICES_PATH
        self._mtime: float | None = None
        self._table: dict[str, dict] = {}

    def table(self) -> dict[str, dict]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime, self._table = None, {}
            return self._table
        if mtime != self._mtime:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            self._table = {k: v for k, v in data.items()
                           if isinstance(v, dict) and not k.startswith("_")} if isinstance(data, dict) else {}
            self._mtime = mtime
        return self._table

    def cost(self, model: str | None, local: bool | None, usage: dict | None) -> float | None:
        """USD for one call: 0 for local models, None when it can't be priced."""
        if local:
            return 0.0
        p = self.table().get(model or "")
        usage = usage or {}
        tin, tout = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if not p or not isinstance(tin, int) or not isinstance(tout, int):
            return None
        try:
            return round((tin * float(p.get("input", 0)) + tout * float(p.get("output", 0))) / 1e6, 6)
        except (TypeError, ValueError):
            return None
