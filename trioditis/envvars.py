"""Environment variables: TRIODITIS_<NAME>. (The pre-rename SWITCHBOARD_* names were dropped in 0.5.)"""

from __future__ import annotations

import os


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get("TRIODITIS_" + name) or default
