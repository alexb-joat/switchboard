"""Environment variables: TRIODITIS_* first, then the pre-rename SWITCHBOARD_* (one more version)."""

from __future__ import annotations

import os


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get("TRIODITIS_" + name) or os.environ.get("SWITCHBOARD_" + name) or default
