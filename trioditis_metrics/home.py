"""Where Trioditis and Hlidskjalv keep shared state: metrics.db, budget.json,
prices.json, hardware.json. $HLIDSKJALV_HOME, else ~/.hlidskjalv.

Before the rename this was ~/.arcade (and $ARCADE_HOME). data_home() still
honours both, and migrate_home() moves the old folder once, leaving a symlink
behind so old shell hooks and scripts keep working. It's only called when a
server starts, never on import (so running tests can't move anything).
"""

from __future__ import annotations

import os
from pathlib import Path


def data_home() -> Path:
    env = os.environ.get("HLIDSKJALV_HOME") or os.environ.get("ARCADE_HOME")
    if env:
        return Path(env)
    new, old = Path.home() / ".hlidskjalv", Path.home() / ".arcade"
    return old if not new.exists() and old.is_dir() else new


def migrate_home() -> Path:
    """~/.arcade -> ~/.hlidskjalv, once; safe if both apps start at the same moment."""
    if os.environ.get("HLIDSKJALV_HOME") or os.environ.get("ARCADE_HOME"):
        return data_home()
    new, old = Path.home() / ".hlidskjalv", Path.home() / ".arcade"
    if not new.exists() and old.is_dir() and not old.is_symlink():
        try:
            os.rename(old, new)
            os.symlink(new, old, target_is_directory=True)
        except OSError:
            pass  # the other app got there first, or symlinks aren't allowed (Windows)
    return new if new.exists() else data_home()
