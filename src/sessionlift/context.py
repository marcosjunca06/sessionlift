"""What every command works with: the configuration, the laptop's Claude Code directory, the state
directory, and the way to the server."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import paths
from .config import Config, Project
from .transcripts import slug


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass
class Ctx:
    cfg: Config
    transport: object = None              # a transport.Transport; made on first use
    claude: Path = field(default_factory=paths.claude_dir)
    state: Path = None

    def __post_init__(self) -> None:
        if self.state is None:
            self.state = self.cfg.state_dir

    @property
    def server(self):
        if self.transport is None:
            from .transport import SshTransport
            self.transport = SshTransport(self.cfg, self.state)
        return self.transport

    def local_dir(self, project: Project) -> Path:
        """Where Claude Code keeps this project's transcripts on the laptop."""
        return self.claude / "projects" / slug(project.local)

    @staticmethod
    def remote_dir(project: Project) -> str:
        """The name of the same directory on the server (under its ~/.claude/projects)."""
        return slug(project.remote)

    def ensure_state(self) -> Path:
        self.state.mkdir(parents=True, exist_ok=True)
        return self.state

    def self_argv(self, *args: str, console: bool = False) -> list:
        """This tool as a command line for a helper process. The config goes as an argument: on
        Windows a helper started through WMI gets the user's default environment, not ours.

        On Windows helpers run under pythonw unless `console`, so none of them ever gets a console
        window: with Windows Terminal as the default terminal, a console started hidden can still be
        shown. A `console` helper is started with CREATE_NO_WINDOW by its parent."""
        from .desktop import python_exe, pythonw_exe
        exe = python_exe() if console else pythonw_exe()
        return [exe, "-m", "sessionlift", "--config", str(self.cfg.path), *args]


def out(text: str = "") -> None:
    """Text for the person running a command (stdout may be a log file under pythonw)."""
    if sys.stdout is not None:
        print(text, flush=True)
