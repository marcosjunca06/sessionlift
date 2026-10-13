"""What sessionlift runs on the server, one operation per call.

The laptop runs `python3 -m sessionlift.server <operation>` over ssh and writes a frame to its stdin:
8 bytes of length, a JSON object of arguments, then the payload (a .tar.gz) when there is one. The
answer is JSON on stdout, or a .tar.gz for the operations that send files back; a failure is JSON on
stderr with a non-zero exit code. The laptop keeps a copy of this package on the server
(~/.local/share/sessionlift/lib) and replaces it whenever its own code differs, so both ends always
run the same version.

Sessions run in tmux, one session each, named sl-<first 8 characters of the session id>, with Remote
Control on so the Claude app can drive them. A session that left the server is kept in
~/.claude/sessionlift-archive, where a later move looks first, so only what it lacks travels again.

Standard library only; Python 3.8 or newer. The tests run every operation in-process against a temp
directory with a fake tmux (tests/fakes.py).
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from . import __version__, gitsync
from .locks import FileLock, LockTimeout
from .transcripts import is_session_id, safe_name, tail_title, tar_files, untar, write_file

MUX_PREFIX = "sl-"
LOGIN_MUX = "sessionlift-login"
RC_SERVICE = "sessionlift-rc"
URL = re.compile(r"https://claude\.ai/code/[\w\-./?=&%]+")
EXIT_QUESTION = "Background work is running"   # Claude Code's question when exiting would stop tasks
CHROME_NOTICE = "Site-level permissions are inherited from the Chrome extension"
META = ".sessionlift-meta.json"                 # the first member of an archive that answers with files


def mux_name(sid: str) -> str:
    return MUX_PREFIX + sid[:8]


def is_session_mux(name: str) -> bool:
    return bool(re.fullmatch(MUX_PREFIX + r"[0-9a-f]{8}", name))


class OpError(Exception):
    def __init__(self, code: int, message: str, **extra) -> None:
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


class Mux:
    """tmux, as the sessions on the server use it."""

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def names(self) -> list:
        r = self._run("ls", "-F", "#S")
        return r.stdout.split() if r.returncode == 0 else []

    def has(self, name: str) -> bool:
        return self._run("has-session", "-t", name).returncode == 0

    def new(self, name: str, cwd: str, command: str, width: int = 220, height: int = 50) -> None:
        r = self._run("new-session", "-d", "-s", name, "-x", str(width), "-y", str(height), "-c", cwd,
                      "bash", "-lc", command)
        if r.returncode:
            raise OpError(1, f"tmux could not start {name}: {r.stderr.strip()}")

    def send(self, name: str, *keys: str, literal: bool = False) -> None:
        self._run("send-keys", "-t", name, *(["-l"] if literal else []), *keys)

    def send_repeat(self, name: str, key: str, times: int) -> None:
        self._run("send-keys", "-t", name, "-N", str(times), key)

    def capture(self, name: str, start: int = None, join: bool = False):
        """The pane's text, or None when the session is gone."""
        args = ["capture-pane", "-p", *(["-J"] if join else []), "-t", name]
        if start is not None:
            args += ["-S", str(start)]
        r = self._run(*args)
        return r.stdout if r.returncode == 0 else None

    def kill(self, name: str) -> None:
        self._run("kill-session", "-t", name)


class ServerEnv:
    """The server as the operations see it. Tests replace the home directory, tmux, the clock and
    Claude Code's sign-in check."""

    def __init__(self, home=None, mux=None) -> None:
        self.home = Path(home or os.environ.get("SESSIONLIFT_SERVER_HOME") or Path.home())
        self.mux = mux or Mux()

    @property
    def claude_dir(self) -> Path:
        return self.home / ".claude"

    @property
    def archive(self) -> Path:
        return self.claude_dir / "sessionlift-archive"

    @property
    def state(self) -> Path:
        return self.home / ".local" / "state" / "sessionlift"

    def project_dir(self, name: str) -> Path:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9-]+", name):
            raise OpError(2, f"not a project directory name: {name!r}")
        return self.claude_dir / "projects" / name

    def claude(self) -> str:
        return shutil.which("claude") or str(self.home / ".local" / "bin" / "claude")

    def auth_status(self) -> dict:
        try:
            r = subprocess.run([self.claude(), "auth", "status"], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=60)
            return json.loads(r.stdout or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {}

    def logged_in(self) -> bool:
        return bool(self.auth_status().get("loggedIn"))

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> float:
        return time.time()

    def lock(self, name: str, timeout: float) -> FileLock:
        return FileLock(self.state / f"{name}.lock", timeout)

    def run(self, argv: list, timeout: float = 600, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout, **kw)


# ---------------------------------------------------------------- helpers

def _sid(args: dict) -> str:
    sid = args.get("sid", "")
    if not is_session_id(sid):
        raise OpError(2, f"not a session id: {sid!r}")
    return sid


def sha256_of(path: Path, limit: int = None) -> str:
    h, left = hashlib.sha256(), limit
    with open(path, "rb") as f:
        while left is None or left > 0:
            chunk = f.read(1 << 20 if left is None else min(1 << 20, left))
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
    return h.hexdigest()


def copy_of(project: Path, sid: str) -> dict:
    """What a project directory holds of a session: transcript size and sha256, side file sizes."""
    transcript = project / f"{sid}.jsonl"
    if not transcript.exists():
        return {"size": 0, "sha": "", "sides": {}}
    sides = {}
    if (project / sid).is_dir():
        for p in sorted((project / sid).rglob("*")):
            if p.is_file():
                sides[p.relative_to(project).as_posix()] = p.stat().st_size
    return {"size": transcript.stat().st_size, "sha": sha256_of(transcript), "sides": sides}


def _write_session(target: Path, sid: str, data: bytes, append: bool) -> int:
    """Write a payload into a project or archive directory: its "arriving.jsonl" goes onto the end of
    the transcript (or replaces it), the rest are side files of that session. Returns the new size."""
    files = untar(data)
    arriving = files.pop("arriving.jsonl", (b"", 0))[0]
    for name in files:
        if name.split("/")[0] != sid:
            raise OpError(2, f"a side file outside the session's directory: {name}")
    target.mkdir(parents=True, exist_ok=True)
    with open(target / f"{sid}.jsonl", "ab" if append else "wb") as f:
        f.write(arriving)
    for name, (body, mtime) in files.items():
        write_file(target / name, body, mtime)
    return (target / f"{sid}.jsonl").stat().st_size


def _replace_tree(src: Path, dst: Path) -> None:
    if dst.is_dir():
        shutil.rmtree(dst)
    elif dst.exists():
        dst.unlink()
    os.replace(str(src), str(dst))


def _to_archive(env: ServerEnv, project: Path, sid: str) -> None:
    env.archive.mkdir(parents=True, exist_ok=True)
    for old in (env.archive / sid, env.archive / f"{sid}.jsonl"):
        if old.is_dir():
            shutil.rmtree(old)
        elif old.exists():
            old.unlink()
    if (project / f"{sid}.jsonl").exists():
        os.replace(str(project / f"{sid}.jsonl"), str(env.archive / f"{sid}.jsonl"))
    if (project / sid).is_dir():
        os.replace(str(project / sid), str(env.archive / sid))


def registry_status(env: ServerEnv, sid: str):
    """busy, waiting or idle for the window holding a session, from Claude Code's own registry."""
    for f in sorted((env.claude_dir / "sessions").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("sessionId") == sid:
            return d.get("status")
    return None


# ---------------------------------------------------------------- operations

def op_ping(env: ServerEnv, a: dict, data: bytes) -> dict:
    return {"version": __version__, "home": str(env.home), "user": getpass.getuser(),
            "python": sys.version.split()[0]}


def op_can_take(env: ServerEnv, a: dict, data: bytes) -> dict:
    sid = _sid(a)
    return {"logged_in": env.logged_in(), "running": env.mux.has(mux_name(sid))}


def op_copy_state(env: ServerEnv, a: dict, data: bytes) -> dict:
    """What the server already holds of a session, taken back out of the archive if that is where it
    is (the keeper keeps that copy current)."""
    sid, project = _sid(a), env.project_dir(a.get("project"))
    with env.lock("stage", 60):
        if not (project / f"{sid}.jsonl").exists() and (env.archive / f"{sid}.jsonl").exists():
            project.mkdir(parents=True, exist_ok=True)
            os.replace(str(env.archive / f"{sid}.jsonl"), str(project / f"{sid}.jsonl"))
            if (env.archive / sid).is_dir():
                _replace_tree(env.archive / sid, project / sid)
        return copy_of(project, sid)


def op_receive(env: ServerEnv, a: dict, data: bytes) -> dict:
    sid, project = _sid(a), env.project_dir(a.get("project"))
    return {"size": _write_session(project, sid, data, bool(a.get("append")))}


def claude_command(sid: str, cwd: str, title: str, args: list) -> str:
    extra = "".join(" " + shlex.quote(x) for x in args)
    return (f'export PATH="$HOME/.local/bin:$PATH"; cd {shlex.quote(cwd)} && '
            f"claude --resume {sid}{extra} --remote-control {shlex.quote(title)}; "
            f'echo "[claude exited $?]"; sleep 600')


def op_start(env: ServerEnv, a: dict, data: bytes) -> dict:
    """Write what the laptop sent, then resume the session in tmux with Remote Control on."""
    sid, project = _sid(a), env.project_dir(a.get("project"))
    cwd = a.get("cwd", "")
    if not (isinstance(cwd, str) and os.path.isabs(cwd) and Path(cwd).is_dir()):
        raise OpError(3, f"the checkout {cwd} does not exist on the server: run `sessionlift setup`")
    name = mux_name(sid)
    if env.mux.has(name):
        raise OpError(3, f"{sid} already runs on the server (tmux {name})")
    size = _write_session(project, sid, data, bool(a.get("append")))
    env.mux.new(name, cwd, claude_command(sid, cwd, a.get("title", "") or sid[:8], list(a.get("args") or [])))
    return {"size": size, "mux": name}


def op_await_link(env: ServerEnv, a: dict, data: bytes) -> dict:
    """Wait for a session to show its Remote Control link. The first --chrome start on a machine
    shows a one-time notice about the extension's site permissions, which waits for Enter and holds
    Remote Control back; it is confirmed here."""
    name, pane, confirmed = a.get("mux", ""), "", False
    if not is_session_mux(name):
        raise OpError(2, f"not a session's tmux name: {name!r}")
    deadline = env.now() + float(a.get("timeout", 25))
    while env.now() < deadline:
        pane = env.mux.capture(name, start=-300, join=True) or ""
        found = URL.search(pane)
        if found:
            return {"url": found.group(0), "confirmed_notice": confirmed}
        if "[claude exited" in pane:
            return {"url": None, "exited": pane.strip()[-1500:]}
        bottom = "\n".join(pane.strip().splitlines()[-12:])
        if CHROME_NOTICE in bottom and "Enter to confirm" in bottom and not confirmed:
            env.mux.send(name, "Enter")
            confirmed = True
        env.sleep(0.3)
    return {"url": None, "screen": pane.strip()[-1200:], "confirmed_notice": confirmed}


def op_type(env: ServerEnv, a: dict, data: bytes) -> dict:
    """Send a line to a session as if typed, then Enter as its own key (or it reads as a pasted
    newline)."""
    name = a.get("mux", "")
    if not is_session_mux(name):
        raise OpError(2, f"not a session's tmux name: {name!r}")
    env.mux.send(name, str(a.get("text", "")), literal=True)
    env.sleep(0.6)
    env.mux.send(name, "Enter")
    return {}


def op_unstage(env: ServerEnv, a: dict, data: bytes) -> dict:
    """Put a session's copy back in the archive, stopping it first if asked: a move that gives up
    leaves no session behind that stayed on the laptop."""
    sid, project = _sid(a), env.project_dir(a.get("project"))
    with env.lock("stage", 60):
        if a.get("stop") and env.mux.has(mux_name(sid)):
            env.mux.kill(mux_name(sid))
            env.sleep(1)
        _to_archive(env, project, sid)
    return {}


def op_list(env: ServerEnv, a: dict, data: bytes) -> dict:
    """The sessions in the given project directories, newest first per project (12 at most each)."""
    running = set(env.mux.names())
    rows = []
    for name in a.get("projects") or []:
        project = env.project_dir(name)
        files = [f for f in project.glob("*.jsonl") if is_session_id(f.stem)] if project.is_dir() else []
        for f in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:12]:
            sid = f.stem
            rows.append({"sid": sid, "state": "running" if mux_name(sid) in running else "idle",
                         "mtime": int(f.stat().st_mtime), "title": tail_title(f, sid), "project": name})
    return {"now": int(env.now()), "rows": rows}


def parse_have(text: str) -> tuple:
    """The laptop's "what I hold" lines: ({sid: (size, sha)}, {(size, side file)})."""
    have, sides = {}, set()
    for line in (text or "").splitlines():
        parts = line.split(" ", 3)
        if parts[0] == "HAVE" and len(parts) == 4 and parts[2].isdigit():
            have[parts[1]] = (int(parts[2]), parts[3])
        elif parts[0] == "SIDE" and len(parts) >= 3 and parts[1].isdigit():
            sides.add((int(parts[1]), line.split(" ", 2)[2]))
    return have, sides


def stop_session(env: ServerEnv, sid: str) -> dict:
    """Stop a session's Claude Code in tmux, mid-turn if need be. Esc stops a turn (given a moment to
    settle); the prompt is then emptied, since text in it keeps Ctrl+D from closing, and Ctrl+D twice
    closes. A typed /exit would go through the command menu, which can pick another command. If
    Claude Code asks because background work is running, "Exit and stop tasks" is chosen and the
    question is returned, so the laptop can tell the session which work stopped."""
    name = mux_name(sid)
    out = {"stopped": False, "busy": False, "question": None}
    if not env.mux.has(name):
        return out
    status = registry_status(env, sid)
    busy = status in ("busy", "waiting") or "esc to interrupt" in (env.mux.capture(name) or "")
    env.mux.send(name, "Escape")
    env.sleep(1.6 if busy else 0.3)
    env.mux.send_repeat(name, "BSpace", 400)
    env.sleep(0.4)
    env.mux.send(name, "C-d")
    env.sleep(0.25)
    env.mux.send(name, "C-d")
    for _ in range(60):
        env.sleep(0.5)
        pane = env.mux.capture(name)
        if pane is None or "[claude exited" in pane:
            break
        if out["question"] is None and EXIT_QUESTION in pane:
            out["question"] = pane
            if re.search(r"❯ *1\. *Exit and stop tasks", pane):
                env.mux.send(name, "Enter")
    env.mux.kill(name)
    out.update(stopped=True, busy=busy)
    return out


def op_bring_back(env: ServerEnv, a: dict, data: bytes) -> bytes:
    """One trip for the way back: find the session (an id or its first characters, else the one
    running) in the given projects, stop it, commit and push that project's checkout, and pack what
    the laptop's copy lacks: the transcript's new end when the laptop's copy is its beginning (else all
    of it), and the side files whose size differs. `projects`: [{"dir", "repo", "git"}]."""
    projects = [(env.project_dir(p.get("dir")), p) for p in a.get("projects") or []]
    want = a.get("want") or ""
    if want and not re.fullmatch(r"[0-9a-f-]{1,36}", want):
        raise OpError(2, f"not a session id: {want!r}")
    if not want:
        running = sorted(n for n in env.mux.names() if is_session_mux(n))
        if len(running) != 1:
            raise OpError(4, "say which session", running=running)
        want = running[0][len(MUX_PREFIX):]
    found = [(project, spec, f.name) for project, spec in projects if project.is_dir()
             for f in sorted(project.glob("*.jsonl")) if f.name.startswith(want) and is_session_id(f.stem)]
    if len(found) != 1:
        raise OpError(5, f"{len(found)} transcripts match {want}", found=[name for _, _, name in found])
    project, spec, name = found[0]
    sid = name[:-len(".jsonl")]
    meta = {"sid": sid, "project": project.name, **stop_session(env, sid), "markers": [], "delta": None}
    transcript = project / f"{sid}.jsonl"
    if spec.get("git") and spec.get("repo"):
        title = tail_title(transcript, sid) or sid[:8]
        why = f'Snapshot: uncommitted work when "{title}" moved back from {a.get("label") or "the server"}'
        if (Path(spec["repo"]) / ".git").exists():
            with env.lock("git", 180):
                meta["markers"], _ = gitsync.server_checkout(spec["repo"], leaving=sid, project_dir=project, why=why)
        else:
            meta["markers"] = ["NO_CHECKOUT"]
    have, sides = parse_have(a.get("have", ""))
    files = {}
    size, sha = have.get(sid, (None, None))
    if size is not None and transcript.stat().st_size >= size and sha256_of(transcript, size) == sha:
        with open(transcript, "rb") as f:
            f.seek(size)
            files[f"{sid}.tail"] = f.read()
        meta["delta"] = size
    else:
        files[f"{sid}.jsonl"] = transcript
    if (project / sid).is_dir():
        for p in sorted((project / sid).rglob("*")):
            rel = p.relative_to(project).as_posix()
            if p.is_file() and (p.stat().st_size, rel) not in sides:
                files[rel] = p
    return tar_files({META: json.dumps(meta).encode(), **files})


def op_archive(env: ServerEnv, a: dict, data: bytes) -> dict:
    sid, project = _sid(a), env.project_dir(a.get("project"))
    with env.lock("stage", 60):
        _to_archive(env, project, sid)
    return {}


def op_checkout(env: ServerEnv, a: dict, data: bytes) -> dict:
    """Bring the server checkout to the laptop's branch and origin's commits (see gitsync)."""
    repo = a.get("repo", "")
    if not (os.path.isabs(repo) and (Path(repo) / ".git").exists()):
        return {"markers": ["NO_CHECKOUT"], "ok": False}
    with env.lock("git", 180):
        markers, ok = gitsync.server_checkout(repo, branch=a.get("branch", ""))
    return {"markers": markers, "ok": ok}


def _memory_dir(env: ServerEnv, a: dict) -> Path:
    return env.project_dir(a.get("project")) / "memory"


def op_memory_list(env: ServerEnv, a: dict, data: bytes) -> dict:
    d = _memory_dir(env, a)
    return {p.name: p.stat().st_mtime for p in d.iterdir() if p.is_file()} if d.is_dir() else {}


def op_memory_put(env: ServerEnv, a: dict, data: bytes) -> dict:
    d = _memory_dir(env, a)
    names = []
    for name, (body, mtime) in untar(data).items():
        if "/" in name:
            raise OpError(2, f"memory files are flat: {name}")
        write_file(d / name, body, mtime)
        names.append(name)
    return {"written": names}


def op_memory_get(env: ServerEnv, a: dict, data: bytes) -> bytes:
    d = _memory_dir(env, a)
    files = {}
    for name in a.get("names") or []:
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            raise OpError(2, f"memory files are flat: {name}")
        if (d / name).is_file():
            files[name] = d / name
    return tar_files(files)


def op_stage_probe(env: ServerEnv, a: dict, data: bytes) -> dict:
    """For the keeper: whether the session is live on the server, else what the archive holds."""
    sid, project = _sid(a), env.project_dir(a.get("project"))
    with env.lock("stage", 30):
        if (project / f"{sid}.jsonl").exists():
            return {"on_server": True}
        copy = env.archive / f"{sid}.jsonl"
        if not copy.exists():
            return {"on_server": False, "size": 0, "sha": ""}
        return {"on_server": False, "size": copy.stat().st_size, "sha": sha256_of(copy)}


def op_stage_append(env: ServerEnv, a: dict, data: bytes) -> dict:
    """For the keeper: add to the archive's copy of a laptop session, only if that copy is still the
    size the keeper last left it (else it says so, and the keeper works it out afresh)."""
    sid, project = _sid(a), env.project_dir(a.get("project"))
    try:
        with env.lock("stage", 30):
            if (project / f"{sid}.jsonl").exists():
                return {"result": "onserver"}
            copy = env.archive / f"{sid}.jsonl"
            have = copy.stat().st_size if copy.exists() else 0
            if not a.get("fresh") and have != int(a.get("expected", -1)):
                return {"result": "mismatch", "size": have}
            size = _write_session(env.archive, sid, data, append=not a.get("fresh"))
            return {"result": "ok", "size": size}
    except LockTimeout:
        return {"result": "busy"}


def op_keeper_status(env: ServerEnv, a: dict, data: bytes) -> dict:
    on_server = []
    for name in a.get("projects") or []:
        project = env.project_dir(name)
        if project.is_dir():
            on_server += [f.stem for f in project.glob("*.jsonl") if is_session_id(f.stem)]
    return {"logged_in": env.logged_in(), "running": env.mux.names(), "on_server": on_server}


MACHINE = r"""
echo "== machine"
echo "  $(uptime -p 2>/dev/null), load $(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
free -h 2>/dev/null | awk '/Mem/{print "  memory " $3 " of " $2}'
df -h / 2>/dev/null | awk 'NR==2{print "  disk " $3 " of " $2}'
"""


def op_status(env: ServerEnv, a: dict, data: bytes) -> dict:
    lines = ["== sessions (tmux)"]
    for name in env.mux.names():
        pane = env.mux.capture(name, start=-300, join=True) or ""
        links = URL.findall(pane)
        lines.append(f"  {name}  {links[-1] if links else 'no link on screen'}")
    if len(lines) == 1:
        lines.append("  none")
    if a.get("remote_control_server"):
        r = env.run(["systemctl", "is-active", RC_SERVICE], timeout=20)
        lines += ["== Remote Control server", f"  {r.stdout.strip() or r.stderr.strip()}"]
    if a.get("browser"):
        r = env.run(["systemctl", "is-active", f"chrome-remote-desktop@{getpass.getuser()}"], timeout=20)
        lock = env.home / ".config" / "sessionlift-chrome" / "SingletonLock"
        target = os.readlink(lock) if lock.is_symlink() else ""
        alive = False
        if target.rsplit("-", 1)[-1].isdigit():
            try:
                os.kill(int(target.rsplit("-", 1)[-1]), 0)
                alive = True
            except OSError:
                alive = False
        lines += ["== the server's Chrome", f"  remote desktop {r.stdout.strip() or 'unknown'}",
                  f"  Chrome {'running' if alive else 'not running'}"]
    for name in a.get("projects") or []:
        project = env.project_dir(name)
        files = sorted(project.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:5] if project.is_dir() else []
        lines.append(f"== newest transcripts ({name})")
        lines += [f"  {f.stem}  {time.strftime('%m-%d %H:%M', time.localtime(f.stat().st_mtime))}  "
                  f"{tail_title(f, f.stem)}" for f in files] or ["  none"]
    r = env.run(["bash", "-c", MACHINE], timeout=30)
    lines += r.stdout.rstrip("\n").splitlines()
    for repo in a.get("repos") or []:
        r = env.run(["git", "-C", repo, "status", "-sb"], timeout=60)
        lines.append(f"== git ({repo})")
        lines += ["  " + line for line in (r.stdout or r.stderr).splitlines()[:4]]
    return {"text": "\n".join(lines)}


def op_merge_claude_json(env: ServerEnv, a: dict, data: bytes) -> dict:
    """Merge settings into ~/.claude.json: top-level keys replaced, projects merged per project."""
    path = env.home / ".claude.json"
    d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for k, v in (a.get("patch") or {}).items():
        if k == "projects":
            for proj, cfg in v.items():
                d.setdefault("projects", {}).setdefault(proj, {}).update(cfg)
        else:
            d[k] = v
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))
    return {}


def op_put_files(env: ServerEnv, a: dict, data: bytes) -> dict:
    """Write files under a directory: `dir` absolute, or relative to the home directory."""
    target = a.get("dir", "")
    base = Path(target) if os.path.isabs(target) else env.home / safe_name(target)
    names = []
    for name, (body, mtime) in untar(data).items():
        write_file(base / name, body, mtime)
        if a.get("private"):
            os.chmod(str(base / name), 0o600)
        names.append(name)
    return {"written": names}


def op_clone(env: ServerEnv, a: dict, data: bytes) -> dict:
    path, url = a.get("path", ""), a.get("url", "")
    if not os.path.isabs(path):
        raise OpError(2, f"not an absolute path: {path!r}")
    if (Path(path) / ".git").exists():
        return {"cloned": False}
    if not url:
        raise OpError(3, f"{path} is not a git checkout on the server and the project has no clone_url")
    r = env.run(["git", "clone", "-q", url, path], timeout=1800, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    if r.returncode:
        raise OpError(1, f"git clone {url}: {r.stderr.strip()}")
    return {"cloned": True}


def op_install_rc(env: ServerEnv, a: dict, data: bytes) -> dict:
    """Install (and with `enable`, start) the systemd unit of the Remote Control server. Needs
    passwordless sudo; without it, says which commands to run by hand."""
    unit = env.state / f"{RC_SERVICE}.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(a.get("unit", ""), encoding="utf-8")
    steps = [["sudo", "-n", "install", "-m", "644", str(unit), f"/etc/systemd/system/{RC_SERVICE}.service"],
             ["sudo", "-n", "systemctl", "daemon-reload"]]
    if a.get("enable"):
        steps.append(["sudo", "-n", "systemctl", "enable", "--now", RC_SERVICE])
    for argv in steps:
        r = env.run(argv, timeout=120)
        if r.returncode:
            manual = "; ".join(" ".join(shlex.quote(x) for x in s if x != "-n") for s in steps)
            raise OpError(1, f"{' '.join(argv)}: {r.stderr.strip() or 'failed'}. Run on the server: {manual}")
    r = env.run(["systemctl", "is-active", RC_SERVICE], timeout=20)
    return {"active": r.stdout.strip()}


def op_provision(env: ServerEnv, a: dict, data: bytes) -> dict:
    """Run provision.sh (sent as the payload) in the background; its log is ~/sessionlift-provision.log."""
    script = env.home / "sessionlift-provision.sh"
    script.write_bytes(data.replace(b"\r\n", b"\n"))
    log = env.home / "sessionlift-provision.log"
    flags = [f for f in a.get("flags") or [] if re.fullmatch(r"--[a-z-]+", f)]
    with open(log, "ab") as out:
        subprocess.Popen(["bash", str(script), *flags], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         cwd=str(env.home), start_new_session=True)
    return {"log": str(log)}


def op_login_start(env: ServerEnv, a: dict, data: bytes) -> dict:
    if env.logged_in():
        return {"logged_in": True}
    email = a.get("email", "")
    cmd = (f'export PATH="$HOME/.local/bin:$PATH"; claude auth login --claudeai'
           f'{" --email " + shlex.quote(email) if email else ""}; sleep 3600')
    env.mux.kill(LOGIN_MUX)
    env.mux.new(LOGIN_MUX, str(env.home), cmd, width=250)
    pane = ""
    for _ in range(20):
        pane = env.mux.capture(LOGIN_MUX, start=-50, join=True) or ""
        found = re.search(r"https://\S+oauth\S+", pane)
        if found:
            return {"logged_in": False, "url": found.group(0)}
        env.sleep(1)
    return {"logged_in": False, "screen": pane.strip()[-800:]}


def op_login_code(env: ServerEnv, a: dict, data: bytes) -> dict:
    env.mux.send(LOGIN_MUX, str(a.get("code", "")).strip(), literal=True)
    env.mux.send(LOGIN_MUX, "Enter")
    for _ in range(20):
        if env.logged_in():
            return {"logged_in": True}
        env.sleep(2)
    return {"logged_in": False, "screen": (env.mux.capture(LOGIN_MUX, start=-30, join=True) or "").strip()[-800:]}


OPS = {name[3:]: fn for name, fn in globals().items() if name.startswith("op_") and callable(fn)}


def handle(env: ServerEnv, op: str, raw: bytes) -> tuple:
    """(exit code, stdout, stderr) for one framed request."""
    try:
        n = int.from_bytes(raw[:8], "big") if len(raw) >= 8 else 0
        args = json.loads(raw[8:8 + n] or b"{}")
        data = raw[8 + n:]
        fn = OPS.get(op)
        if fn is None:
            raise OpError(2, f"unknown operation {op!r}")
        result = fn(env, args, data)
        return 0, result if isinstance(result, bytes) else json.dumps(result).encode(), b""
    except OpError as e:
        return e.code, b"", json.dumps({"error": e.message, **e.extra}).encode()
    except LockTimeout as e:
        return 75, b"", json.dumps({"error": str(e)}).encode()
    except Exception as e:  # noqa: BLE001 - whatever it was, the laptop gets it whole
        return 1, b"", json.dumps({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-3000:]}).encode()


def main(argv: list = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(f"usage: python3 -m sessionlift.server <operation>  (operations: {', '.join(sorted(OPS))})",
              file=sys.stderr)
        return 2
    code, out, err = handle(ServerEnv(), argv[0], sys.stdin.buffer.read())
    sys.stdout.buffer.write(out)
    sys.stderr.buffer.write(err)
    return code


if __name__ == "__main__":
    sys.exit(main())
