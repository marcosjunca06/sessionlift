"""Where sessionlift keeps its configuration and state on the laptop.

Every location can be moved with an environment variable, which is how the tests keep away from the
real ~/.claude:

    SESSIONLIFT_CONFIG      the config file
    SESSIONLIFT_HOME        state: logs, pid files, the keeper's address, copy records
    SESSIONLIFT_CLAUDE_DIR  Claude Code's own directory (default ~/.claude)
    SESSIONLIFT_CLAUDE_JSON Claude Code's ~/.claude.json
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def config_path() -> Path:
    if os.environ.get("SESSIONLIFT_CONFIG"):
        return Path(os.environ["SESSIONLIFT_CONFIG"])
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "sessionlift" / "config.toml"


def default_state_dir() -> Path:
    if os.environ.get("SESSIONLIFT_HOME"):
        return Path(os.environ["SESSIONLIFT_HOME"])
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "sessionlift"


def claude_dir() -> Path:
    return Path(os.environ.get("SESSIONLIFT_CLAUDE_DIR") or Path.home() / ".claude")


def claude_json() -> Path:
    return Path(os.environ.get("SESSIONLIFT_CLAUDE_JSON") or Path.home() / ".claude.json")
