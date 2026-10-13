"""Test doubles: a fake tmux, a server environment in a temp directory, transcripts, and two guards.

- Every place sessionlift would default to (its config, its state, ~/.claude, ~/.claude.json) points
  into a temp directory, so no test can read or write the real ones.
- Any attempt to open a window, start a process outside the test or type into a console fails the
  test instead of happening.
"""

from __future__ import annotations

import atexit
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

SANDBOX = Path(tempfile.mkdtemp(prefix="sessionlift-sandbox-"))
atexit.register(shutil.rmtree, SANDBOX, True)
os.environ.update({"SESSIONLIFT_CONFIG": str(SANDBOX / "config.toml"), "SESSIONLIFT_HOME": str(SANDBOX / "state"),
                   "SESSIONLIFT_CLAUDE_DIR": str(SANDBOX / "claude"),
                   "SESSIONLIFT_CLAUDE_JSON": str(SANDBOX / "claude.json")})
for _var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "SESSIONLIFT_ROLE"):  # as if not inside Claude Code
    os.environ.pop(_var, None)

from sessionlift import desktop, page, server  # noqa: E402 - after the sandbox is in place
from sessionlift.config import Config, Project  # noqa: E402
from sessionlift.context import Ctx  # noqa: E402
from sessionlift.transport import LocalTransport  # noqa: E402

LINK = "https://claude.ai/code/session_0123456789abcdef"


def _refuse(name):
    def refuse(*args, **kwargs):
        raise AssertionError(f"a test reached desktop.{name}, which would act on the real desktop")
    return refuse


for _name in ("spawn_detached", "open_terminal", "console_keys", "console_text", "make_shortcut", "install_logon_task",
              "uninstall_logon_task", "shut_down", "kill", "focus_window"):
    setattr(desktop, _name, _refuse(_name))
page.pick_session = _refuse("pick_session (a window)")
page.Page = _refuse("Page (a window)")


class FakeMux:
    """tmux as the server operations use it. A new session shows its Remote Control link at once
    (unless link=False); Ctrl+D twice ends it, after asking the exit question when ask=True."""

    def __init__(self, link: bool = True, ask: bool = False, busy: bool = False) -> None:
        self.sessions, self.link, self.ask, self.busy = {}, link, ask, busy
        self.log = []

    def names(self) -> list:
        return list(self.sessions)

    def has(self, name: str) -> bool:
        return name in self.sessions

    def new(self, name, cwd, command, width=220, height=50) -> None:
        self.log.append(("new", name, cwd, command))
        screen = f"Remote Control: {LINK}\n" if self.link else "starting\n"
        if self.busy:
            screen += "✻ Working… (esc to interrupt)\n"
        self.sessions[name] = {"screen": screen, "ctrl_d": 0, "asked": False}

    def send(self, name, *keys, literal=False) -> None:
        self.log.append(("send", name, keys, literal))
        s = self.sessions.get(name)
        if s is None:
            return
        for key in keys:
            if key == "C-d":
                s["ctrl_d"] += 1
                if s["ctrl_d"] >= 2:
                    if self.ask and not s["asked"]:
                        s["asked"] = True
                        s["screen"] += ("Background work is running\n  These will stop when you exit:\n"
                                        "  subagent · reviewing the board\n  shell · npm test\n"
                                        "❯ 1. Exit and stop tasks\n  2. Cancel\n")
                    else:
                        s["screen"] += "[claude exited 0]\n"
            elif key == "Enter" and s["asked"] and "[claude exited" not in s["screen"]:
                s["screen"] += "[claude exited 0]\n"

    def send_repeat(self, name, key, times) -> None:
        self.log.append(("repeat", name, key, times))

    def capture(self, name, start=None, join=False):
        s = self.sessions.get(name)
        return None if s is None else s["screen"]

    def kill(self, name) -> None:
        self.log.append(("kill", name))
        self.sessions.pop(name, None)


class FakeEnv(server.ServerEnv):
    """The server's home in a temp directory, a fake tmux, a clock that sleeps instantly."""

    def __init__(self, home: Path, mux: FakeMux = None, logged_in: bool = True) -> None:
        super().__init__(home=home, mux=mux or FakeMux())
        self.logged, self.offset = logged_in, 0.0

    def auth_status(self) -> dict:
        return {"loggedIn": self.logged}

    def sleep(self, seconds: float) -> None:
        self.offset += seconds

    def now(self) -> float:
        return time.time() + self.offset

    def run(self, argv, timeout=600, **kw):
        if argv[0] == "git":
            return super().run(argv, timeout=timeout, **kw)
        return subprocess.CompletedProcess(argv, 0, "active\n", "")


def jl(**fields) -> bytes:
    """One transcript line, written the way Claude Code writes them (compact JSON)."""
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def turn(cwd: str, text: str = "hello", stamp: str = "2026-01-01T10:00:00.000Z") -> bytes:
    return (jl(type="user", cwd=cwd, uuid=str(uuid.uuid4()), timestamp=stamp, message={"role": "user", "content": text})
            + jl(type="assistant", cwd=cwd, uuid=str(uuid.uuid4()), timestamp=stamp,
                 message={"role": "assistant", "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}))


def stamp(t: float) -> str:
    """A transcript timestamp, as Claude Code writes them."""
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"


def tool_call(name: str, start: float, end: float, **tool_input) -> bytes:
    """A tool call and its result, two transcript lines."""
    call_id = f"toolu_{uuid.uuid4().hex[:12]}"
    return (jl(type="assistant", timestamp=stamp(start), uuid=str(uuid.uuid4()),
               message={"content": [{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}]})
            + jl(type="user", timestamp=stamp(end), uuid=str(uuid.uuid4()),
                 message={"content": [{"type": "tool_result", "tool_use_id": call_id, "content": "done"}]}))


def touch(path: Path, at: float, text: str = None) -> Path:
    """Write a file and give it modification time `at`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(path.name if text is None else text, encoding="utf-8")
    os.utime(path, (at, at))
    return path


def new_sid() -> str:
    return str(uuid.uuid4())


class SessionCase(unittest.TestCase):
    """A laptop (its ~/.claude, a project checkout, the state directory) and a server (a temp home
    behind LocalTransport) for one test."""

    git_sync = False
    sync_memory = False

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sessionlift-test-"))
        self.addCleanup(_rmtree, self.tmp)
        self.output = io.StringIO()  # what the moves log, for the tests that look at it
        quiet = contextlib.redirect_stdout(self.output)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.local = self.tmp / "laptop" / "work" / "demo"
        self.local.mkdir(parents=True)
        self.remote = self.tmp / "server" / "checkout" / "demo"
        self.remote.mkdir(parents=True)
        self.project = Project(name="demo", local=str(self.local), remote=str(self.remote).replace("\\", "/"),
                               git_sync=self.git_sync, sync_memory=self.sync_memory)
        self.cfg = Config(path=self.tmp / "config.toml", host="example-server", projects=[self.project],
                          name="test-server")
        self.env = FakeEnv(self.tmp / "server" / "home")
        self.ctx = Ctx(self.cfg, transport=LocalTransport(self.env), claude=self.tmp / "laptop" / "claude",
                       state=self.tmp / "laptop" / "state")
        self.folder = self.ctx.local_dir(self.project)
        self.folder.mkdir(parents=True)
        self.server_folder = self.env.project_dir(self.ctx.remote_dir(self.project))

    def write_session(self, sid: str, data: bytes, sides: dict = None) -> Path:
        path = self.folder / f"{sid}.jsonl"
        path.write_bytes(data)
        for rel, body in (sides or {}).items():
            side = self.folder / sid / rel
            side.parent.mkdir(parents=True, exist_ok=True)
            side.write_bytes(body)
        return path


def _rmtree(path: Path) -> None:
    """shutil.rmtree, also of read-only files (git's objects on Windows) and briefly held ones."""
    import stat
    import sys

    def writable(func, p, *_):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    handler = {"onexc": writable} if sys.version_info >= (3, 12) else {"onerror": writable}
    for _ in range(5):
        try:
            shutil.rmtree(path, **handler)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.2)
    shutil.rmtree(path, ignore_errors=True)


os.environ.setdefault("GIT_AUTHOR_NAME", "sessionlift tests")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "tests@example.invalid")
os.environ.setdefault("GIT_COMMITTER_NAME", "sessionlift tests")
os.environ.setdefault("GIT_COMMITTER_EMAIL", "tests@example.invalid")
