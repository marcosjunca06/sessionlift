"""Moving a session: from the laptop to the server (send) and back (bring_back).

A session lives on one machine at a time. A move takes it as it is and waits for nothing: a turn in
progress is interrupted and background agents and commands are stopped ("Exit and stop tasks"); the
session on the other side is told which, so it picks them up. The session's own uncommitted work is
committed and pushed (gitsync.own_files: never another session's), and the other machine checks out
the same commit. Only what the other machine lacks travels: each side's copy of a transcript is
compared by size and sha256 against the beginning of the other's, so a session that was there before
sends only its new end.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import desktop, gitsync
from .config import Project
from .context import Ctx, log, out
from .memory import sync_memory
from .screen import stopped_tasks
from .server import META, mux_name
from .sessions import open_windows, transcript_path
from .transcripts import rewrite_cwd, session_files, session_title, tar_files, untar, uuids, write_file


# ---------------------------------------------------------------- the laptop's git lock

_repo_locks: dict = {}
_repo_guard = threading.Lock()


class RepoLock:
    """One git writer at a time in a laptop checkout among this tool's processes ("Move all" starts a
    move per session at once, and two commits in one checkout collide on index.lock): a lock file
    holding its owner's pid, re-entrant within a process. If an owner holds it past `timeout`, the
    caller goes on without it rather than leave a session stuck."""

    def __init__(self, path: Path, timeout: float = 180) -> None:
        self.path, self.timeout = Path(path), timeout
        self.rlock, self.depth, self.mine = threading.RLock(), 0, False

    @classmethod
    def for_project(cls, ctx: Ctx, project: Project) -> "RepoLock":
        path = ctx.ensure_state() / f"git-{project.name}.lock"
        with _repo_guard:
            return _repo_locks.setdefault(str(path), cls(path))

    def __enter__(self) -> "RepoLock":
        self.rlock.acquire()
        if self.depth:
            self.depth += 1
            return self
        self.mine, deadline = False, time.time() + self.timeout
        while not self.mine:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, desktop.pid_stamp().encode())
                os.close(fd)
                self.mine = True
            except FileExistsError:
                try:
                    holder = self.path.read_text()
                except OSError:
                    holder = ""
                if holder.strip() and not desktop.stamp_alive(holder):  # its owner is gone
                    self.path.unlink(missing_ok=True)
                    continue
                if time.time() > deadline:
                    log(f"the git lock is still held ({holder.strip()}); going on without it")
                    break
                time.sleep(0.25)
        self.depth = 1
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.depth -= 1
            if not self.depth and self.mine:
                self.path.unlink(missing_ok=True)
                self.mine = False
        finally:
            self.rlock.release()


# ---------------------------------------------------------------- notes and git reports

def note_for(move: str, tasks: str, interrupted: bool, behind: bool = False) -> str:
    """One line for a session after a move, naming the work the move stopped, and saying when the
    other checkout could not take the newest commits; "" if neither."""
    stopped = []
    if interrupted:
        stopped.append("it interrupted your turn in progress")
    if tasks:
        stopped.append(f"it stopped background work that was still running: {tasks}")
    parts = []
    if stopped:
        parts.append("; ".join(stopped) + ". Its uncommitted work was committed and pushed first. Pick the "
                     "work up where it stopped: continue each stopped agent with SendMessage, or start it "
                     "again with what it had already done; rerun any stopped background command; then carry on.")
    if behind:
        parts.append("The server checkout could not take the laptop's newest commits, because another session's "
                     "uncommitted work there is in files they change: check git status and git log against "
                     "origin before relying on those files.")
    return f"[{move}] " + " ".join(parts) if parts else ""


def report_markers(ctx: Ctx, markers: list) -> None:
    for line in markers:
        if line.startswith("COMMITTED"):
            log(f"committed {line.split()[1]} file(s) the session wrote on the server")
        elif line.startswith("LEFT"):
            log(f"left {line.split()[1]} uncommitted file(s) on the server for the sessions that wrote them")
        elif line.startswith("PUSHED"):
            log(f"pushed the server's commits on {line.split()[1]}")
        elif line.startswith("CHECKOUT "):
            log(f"server checkout: {line[len('CHECKOUT '):]}")
        elif line == "BEHIND":
            log("the server checkout stays behind origin: uncommitted work there is in files the new commits change")
        elif line == "NOT_PUSHED":
            log(f"the server's commits could not be pushed until it takes origin's; see `git status` on {ctx.cfg.label}")
        elif line == "NO_CHECKOUT":
            log("the project has no git checkout on the server (sessionlift setup clones it when clone_url is set)")
        elif line in ("COMMIT_FAILED", "CHECKOUT_FAILED", "DIVERGED", "NO_UPSTREAM"):
            log(f"on the server: {line.lower().replace('_', ' ')}; see `git status` on {ctx.cfg.label}")


# ---------------------------------------------------------------- to the server

def can_take(ctx: Ctx, sid: str) -> None:
    state = ctx.server.call("can_take", {"sid": sid}).value("checking the server")
    if not state.get("logged_in"):
        raise SystemExit("Claude Code is not signed in on the server: sessionlift login")
    if state.get("running"):
        raise SystemExit(f"{sid} already runs on the server (tmux {mux_name(sid)}); bring it back first")


def _to_server(project: Project, data: bytes) -> bytes:
    return rewrite_cwd(data, project.local, project.remote, to_server=True)


def _to_laptop(project: Project, data: bytes) -> bytes:
    return rewrite_cwd(data, project.local, project.remote, to_server=False)


def copy_ahead(ctx: Ctx, project: Project, sid: str) -> tuple:
    """Copy the transcript and its side files as they stand, so that once the session closes only
    what was written after this moment is left to send. When the server still has an earlier copy (the
    session was there before), only what that copy lacks goes. Returns (bytes of the laptop transcript
    covered, when)."""
    at = time.time()
    folder = ctx.local_dir(project)
    data = (folder / f"{sid}.jsonl").read_bytes()
    cut = data.rfind(b"\n") + 1
    mine = _to_server(project, data[:cut])
    state = ctx.server.call("copy_state", {"sid": sid, "project": ctx.remote_dir(project)}).value("reading the server's copy")
    size, sha, sides = state.get("size", 0), state.get("sha", ""), state.get("sides", {})
    fresh = not (size and size <= len(mine) and hashlib.sha256(mine[:size]).hexdigest() == sha)
    payload = {}
    for name, path in session_files(folder, sid).items():
        if name == f"{sid}.jsonl":
            continue
        body = _to_server(project, path.read_bytes()) if name.endswith(".jsonl") else path.read_bytes()
        if fresh or sides.get(name) != len(body):
            payload[name] = body
    payload["arriving.jsonl"] = mine if fresh else mine[size:]
    ctx.server.call("receive", {"sid": sid, "project": ctx.remote_dir(project), "append": not fresh},
                    tar_files(payload)).value("copying the transcript to the server")
    sent = len(payload["arriving.jsonl"])
    log(f"copied ahead: {'all' if fresh else 'the new part of'} the transcript ({sent / 1e6:.1f} of "
        f"{len(mine) / 1e6:.1f} MB) and {len(payload) - 1} side file(s)")
    return cut, at


def cut_file(ctx: Ctx, sid: str) -> Path:
    return ctx.state / f"server-cut-{sid}.json"


def server_cut(ctx: Ctx, sid: str):
    """Where the server's copy of a session ended when it started there before its laptop window had
    closed: the bytes that window wrote after it (the blocked command, its exit records) never went."""
    try:
        return int(json.loads(cut_file(ctx, sid).read_text(encoding="utf-8"))["cut"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def server_claude_args(ctx: Ctx) -> list:
    args = ["--dangerously-skip-permissions"] if ctx.cfg.bypass_permissions else []
    return args + list(ctx.cfg.server_args)


def start_on_server(ctx: Ctx, project: Project, sid: str, title: str, ahead, group=frozenset(),
                    early: bool = False) -> threading.Thread:
    """Send what the server's copy lacks and start the session there in tmux. The laptop's commit and
    push and the server checkout's catching up run beside it: the thread returned. early: its laptop
    window is still closing (the conversation itself is final), so the copy ends at the last whole line
    now, and server_cut() remembers where for the way back."""
    cut, at = ahead or (0, 0.0)
    folder = ctx.local_dir(project)
    data = (folder / f"{sid}.jsonl").read_bytes()
    end = data.rfind(b"\n") + 1 if early else len(data)
    payload = {n: (_to_server(project, p.read_bytes()) if n.endswith(".jsonl") else p)
               for n, p in session_files(folder, sid).items()
               if n != f"{sid}.jsonl" and p.stat().st_mtime >= at - 1}
    payload["arriving.jsonl"] = _to_server(project, data[cut:end])
    marker = cut_file(ctx, sid)
    ctx.ensure_state()
    if early:
        marker.write_text(json.dumps({"cut": end}), encoding="utf-8")
    else:
        marker.unlink(missing_ok=True)
    git = threading.Thread(target=sync_checkouts, args=(ctx, project, title, sid, group), daemon=True)
    git.behind = False
    git.start()
    r = ctx.server.call("start", {"sid": sid, "project": ctx.remote_dir(project), "append": bool(cut),
                                  "cwd": project.remote, "title": title, "args": server_claude_args(ctx)},
                        tar_files(payload))
    if not r.ok:
        git.join()
        marker.unlink(missing_ok=True)
        raise SystemExit(f"the server could not take the session (exit {r.code}): {r.message()}")
    return git


def sync_checkouts(ctx: Ctx, project: Project, title: str, sid: str, group=frozenset()) -> None:
    """Commit and push the session's own work on the laptop, then bring the server checkout to the
    same commit."""
    if not project.git_sync:
        return
    try:
        branch = gitsync.commit_own_and_push(
            project.local, f'Snapshot: uncommitted work when "{title}" moved to {ctx.cfg.label}', sid,
            ctx.local_dir(project), log, RepoLock.for_project(ctx, project), group=group)
        r = ctx.server.call("checkout", {"repo": project.remote, "branch": branch})
        markers = r.json().get("markers", []) if r.ok else ["CHECKOUT_FAILED"]
        if not r.ok:
            log(f"the server checkout could not catch up: {r.message()}")
        report_markers(ctx, markers)
        threading.current_thread().behind = "BEHIND" in markers
    except SystemExit as e:  # nothing here stops a move
        log(f"git: {e}")


def finish_on_server(ctx: Ctx, project: Project, sid: str, title: str, git: threading.Thread, tasks: str,
                     interrupted: bool) -> None:
    """Wait for the session's Claude app link on the server and the checkouts, then tell it what the
    move stopped."""
    name = mux_name(sid)
    seen = ctx.server.call("await_link", {"mux": name, "timeout": 25}, timeout=120).value("starting the session")
    if seen.get("exited"):
        raise SystemExit(f"claude exited on the server:\n{seen['exited']}")
    if seen.get("confirmed_notice"):
        log("confirmed Claude Code's one-time Claude in Chrome notice on the server")
    if not seen.get("url"):
        log("no Remote Control link yet; last screen on the server:")
        out(seen.get("screen", ""))
    git.join()
    note = note_for(f"Moved from the laptop to {ctx.cfg.label}.", tasks, interrupted, getattr(git, "behind", False))
    if note:
        ctx.server.call("type", {"mux": name, "text": note}).value("telling the session")
        log("told the session on the server what the move stopped")
    log(f"running on the server in tmux {name} as \"{title}\"" + (f": {seen['url']}" if seen.get("url") else ""))
    out(f"Watch it here: ssh -t {ctx.cfg.host} tmux attach -t {name}  (detach: Ctrl-b d)\n"
        f"Bring it back: /from-server, or sessionlift back {sid}")


def unstage(ctx: Ctx, project: Project, sid: str, stop: bool = False) -> None:
    """A move that gives up takes back the transcript it copied ahead, so the server never lists a
    session that stayed on the laptop. stop: it had already started there while its window was
    closing; it stops first."""
    r = ctx.server.call("unstage", {"sid": sid, "project": ctx.remote_dir(project), "stop": stop})
    cut_file(ctx, sid).unlink(missing_ok=True)
    log(("stopped it on the server and took its copy back" if stop else "took the copied transcript back off the server")
        if r.ok else f"could not take the copy back: {r.message()}")


def start_send(ctx: Ctx, project: Project, sid: str, pid, hook_pid: int = None, now: bool = False,
               group=frozenset()) -> tuple:
    """Start the background half of a move for the session in window `pid`: from the hook, close the
    window once the hook has returned; from the page (now), at once, interrupting a turn in progress;
    from inside a turn, once that turn has ended."""
    transcript = transcript_path(ctx, project, sid)
    state = ctx.ensure_state()
    if desktop.stamp_alive(_read(state / f"send-{sid[:8]}.pid")):
        raise SystemExit(f"this session is already on its way to the server (log: {state / f'send-{sid[:8]}.log'})")
    logfile = state / f"send-{sid[:8]}.log"
    argv = ctx.self_argv("send", sid, "--after-pid", str(pid), "--log", str(logfile))
    if hook_pid:
        argv += ["--after-hook", str(hook_pid)]
    elif now:
        argv += ["--close-now"]
    else:
        argv += ["--type-exit-after", str(transcript.stat().st_size)]
    if group:
        argv += ["--group", ",".join(sorted(group))]
    desktop.spawn_detached(argv, state)
    return session_title(transcript.read_bytes(), sid), logfile


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def send_after_exit(ctx: Ctx, project: Project, sid: str, title: str, pid: int, type_offset, hook_pid: int = None,
                    close_now: bool = False, group=frozenset()) -> int:
    """The background half of a move from a window: copy the bulk of the transcript while the window
    is being closed (answering "Exit and stop tasks" if asked), and move the rest the moment the
    session has closed."""
    moving = ctx.ensure_state() / f"send-{sid[:8]}.pid"
    moving.write_text(desktop.pid_stamp())
    try:
        return _send_after_exit(ctx, project, sid, title, pid, type_offset, hook_pid, close_now, group)
    finally:
        moving.unlink(missing_ok=True)


def _send_after_exit(ctx: Ctx, project: Project, sid: str, title: str, pid: int, type_offset, hook_pid,
                     close_now: bool, group) -> int:
    began = time.time()
    state = ctx.state
    stopped, interrupted, ready = (state / f"stopped-{sid[:8]}.txt", state / f"interrupted-{sid[:8]}.txt",
                                   state / f"ready-{sid[:8]}.txt")
    for f in (stopped, interrupted, ready):
        f.unlink(missing_ok=True)
    can_take(ctx, sid)  # before anything is typed into the window: a refusal leaves the session as it is
    typer = None
    when = (["--after-exit-of", str(hook_pid)] if hook_pid else
            ["--now", "--interrupted-file", str(interrupted), "--draft-file", str(state / f"draft-{sid[:8]}.txt")]
            if close_now else
            ["--after-turn", str(transcript_path(ctx, project, sid)), "--offset", str(type_offset)]
            if type_offset is not None else None)
    if when and desktop.WINDOWS_UI:
        typer = subprocess.Popen(ctx.self_argv("_type", "--pid", str(pid), "--close", *when, "--exit-question",
                                               str(stopped), "--ready-file", str(ready), console=True),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    elif when:
        log("close the Claude Code window (Ctrl+D twice): the move goes on once it has closed")
    memory = threading.Thread(target=_quietly, args=(sync_memory, ctx, project), daemon=True)
    memory.start()
    copied: list = []
    copying = threading.Thread(target=lambda: copied.append(copy_ahead(ctx, project, sid)), daemon=True)
    copying.start()
    git = None
    if typer and (hook_pid or close_now):
        # From the hook or the page the conversation is final once the typer is about to press Ctrl+D:
        # start on the server while the window closes.
        while not ready.exists() and typer.poll() is None:
            time.sleep(0.05)
        copying.join()
        if ready.exists() and copied:
            try:
                git = start_on_server(ctx, project, sid, title, copied[0], group, early=True)
                log("starting on the server while its window closes")
            except SystemExit as e:
                log(f"could not start on the server yet, so once the window has closed: {e}")
    wait_s = 6 * 3600
    if typer:
        out(typer.communicate()[0].strip())
        if typer.returncode:
            copying.join()
            unstage(ctx, project, sid, stop=git is not None)
            raise SystemExit("could not close the window, so nothing moved; run /to-server again")
        wait_s = 90  # it was told to close; if it has not in 90 s, give up rather than move it later
    if not desktop.wait_for_pid(pid, wait_s):
        copying.join()
        unstage(ctx, project, sid, stop=git is not None)
        raise SystemExit(f"the session did not close within {wait_s} s, so nothing moved")
    log(f"the laptop session (pid {pid}) has closed")
    copying.join()
    tasks = _read(stopped).strip()
    if git is None:
        git = start_on_server(ctx, project, sid, title, copied[0] if copied else None, group)
    finish_on_server(ctx, project, sid, title, git, tasks, interrupted.exists())
    memory.join()
    log(f"the session was live on the server {time.time() - began:.0f} s after the move began")
    return 0


def _quietly(fn, *args) -> None:
    try:
        fn(*args)
    except SystemExit as e:
        log(f"{getattr(fn, '__name__', 'step')}: {e}")


def send_closed(ctx: Ctx, project: Project, sid: str, title: str, force: bool = False) -> int:
    """Move a laptop session that no window holds: only what the server lacks travels, then it starts
    there. force: even if a window holds it (send --now, run from inside that session)."""
    if not force and any(w["sid"] == sid for w in open_windows(ctx)):
        raise SystemExit(f"{sid} is open in a window here: move it with /to-server in that window, or from the page")
    moving = ctx.ensure_state() / f"send-{sid[:8]}.pid"
    if desktop.stamp_alive(_read(moving)):
        raise SystemExit("this session is already on its way to the server")
    moving.write_text(desktop.pid_stamp())
    try:
        started = time.time()
        can_take(ctx, sid)
        memory = threading.Thread(target=_quietly, args=(sync_memory, ctx, project), daemon=True)
        memory.start()
        ahead = copy_ahead(ctx, project, sid)
        finish_on_server(ctx, project, sid, title, start_on_server(ctx, project, sid, title, ahead), "", False)
        memory.join()
        log(f"the session was live on the server {time.time() - started:.0f} s after the move began")
    finally:
        moving.unlink(missing_ok=True)
    return 0


# ---------------------------------------------------------------- back to the laptop

def laptop_copy_state(ctx: Ctx, prefix: str) -> str:
    """What this laptop holds of the session(s) an id prefix could name, in the server's form: one line
    per transcript, "HAVE sid size sha256", then "SIDE size sid/rel" per side file. The server sends
    only what these lack."""
    lines = []
    for project in ctx.cfg.projects:
        folder = ctx.local_dir(project)
        if not folder.is_dir():
            continue
        for path in folder.glob(f"{prefix or ''}*.jsonl"):
            if path.stat().st_mtime < time.time() - 30 * 86400:
                continue
            body = path.read_bytes()
            cut = server_cut(ctx, path.stem)  # it started on the server while its window closed: only this much went
            body = _to_server(project, body[:cut] if cut is not None and cut <= len(body) else body)
            lines.append(f"HAVE {path.stem} {len(body)} {hashlib.sha256(body).hexdigest()}")
            side = folder / path.stem
            if side.is_dir():
                for f in side.rglob("*"):
                    if f.is_file():
                        n = len(_to_server(project, f.read_bytes())) if f.suffix == ".jsonl" else f.stat().st_size
                        lines.append(f"SIDE {n} {f.relative_to(folder).as_posix()}")
    return "\n".join(lines)


def bring_back(ctx: Ctx, session: str, pull: bool = True) -> tuple:
    """Stop a session on the server, commit and push there, copy home what the laptop's copy of its
    transcript lacks (all of it if the copies differ) and take the server's commits. Returns (project,
    session id, title, the note for it about the work the move stopped).

    `session` (its id or first characters) is always named by the person: with one session on the
    server, bringing that one back unasked once switched a fresh window to a session nobody chose."""
    if not session or not re.fullmatch(r"[0-9a-f-]{4,36}", session):
        raise SystemExit(f"not a session id: {session!r}")
    projects = [{"dir": ctx.remote_dir(p), "repo": p.remote, "git": p.git_sync} for p in ctx.cfg.projects]
    r = ctx.server.call("bring_back", {"want": session, "projects": projects, "have": laptop_copy_state(ctx, session),
                                       "label": ctx.cfg.label}, timeout=900)
    if r.code == 4:
        raise SystemExit(f"say which session; running on the server: {r.error().get('running') or 'none'}")
    if r.code == 5:
        raise SystemExit(f"{len(r.error().get('found', []))} transcripts on the server match {session}: "
                         f"{r.error().get('found')}")
    if not r.ok:
        raise SystemExit(f"bringing the session back failed on the server (exit {r.code}): {r.message()}")
    got = untar(r.out)
    meta = json.loads(got.pop(META)[0])
    report_markers(ctx, meta.get("markers", []))
    sid = meta["sid"]
    project = next(p for p in ctx.cfg.projects if ctx.remote_dir(p) == meta["project"])
    tasks = (stopped_tasks(meta["question"]) or "background work") if meta.get("question") else ""
    if meta.get("stopped"):
        log(f"stopped {mux_name(sid)} on the server" + (", mid-turn" if meta.get("busy") else "")
            + (f"; it stopped {tasks}" if tasks else ""))
    folder = ctx.local_dir(project)
    local = folder / f"{sid}.jsonl"
    mine = local.read_bytes() if local.exists() else b""
    cut = server_cut(ctx, sid)
    if cut is not None and cut <= len(mine):
        mine = mine[:cut]  # what its window wrote while closing (the blocked command, exit records) stays out
    if f"{sid}.tail" in got:  # the laptop's copy is the beginning of the server's: add the new end
        tail, mtime = got.pop(f"{sid}.tail")
        got[f"{sid}.jsonl"] = (_to_server(project, mine) + tail, mtime)
        log(f"only the new part of the transcript came back ({len(tail) / 1e6:.1f} MB)")
    if f"{sid}.jsonl" not in got:
        raise SystemExit(f"no transcript {sid} came back from the server")
    new = _to_laptop(project, got[f"{sid}.jsonl"][0])
    if local.exists() and not uuids(mine) <= uuids(new):
        backup = local.with_name(f"{sid}.laptop-{datetime.now():%Y%m%d-%H%M%S}.jsonl.bak")
        shutil.copy2(local, backup)
        log(f"the laptop copy had turns the server never saw; kept it as {backup.name}")
    for name, (data, mtime) in got.items():
        write_file(folder / name, _to_laptop(project, data) if name.endswith(".jsonl") else data, mtime)
    cut_file(ctx, sid).unlink(missing_ok=True)
    log(f"transcript back ({len(got)} file(s))")
    title = session_title(new, sid)

    def tidy() -> None:
        _quietly(sync_memory, ctx, project)
        archived = ctx.server.call("archive", {"sid": sid, "project": ctx.remote_dir(project)})
        if not archived.ok:
            log(f"could not archive the server's copy: {archived.message()}")

    memory = threading.Thread(target=tidy, daemon=True)
    memory.start()
    if pull and project.git_sync:  # the server's commits; the laptop's own uncommitted files stay as they are
        gitsync.rebase_on_origin(project.local, log, RepoLock.for_project(ctx, project))
    memory.join()
    return project, sid, title, note_for(f"Moved from {ctx.cfg.label} back to the laptop.", tasks, bool(meta.get("busy")))


def server_sessions(ctx: Ctx, strict: bool = False) -> list:
    """(id, running or idle, how long ago it was active, title, project name) per session on the server,
    newest first. An idle copy whose laptop transcript is newer is left out: that session went on on the
    laptop (a move that was cut short leaves such a copy behind). strict: raise if the server did not
    answer, rather than report no sessions."""
    by_dir = {ctx.remote_dir(p): p for p in ctx.cfg.projects}
    r = ctx.server.call("list", {"projects": list(by_dir)}, timeout=120)
    if not r.ok:
        if strict:
            raise SystemExit(f"the server did not answer: {r.message()}")
        return []
    answer = r.json()
    now, rows = answer.get("now", time.time()), []
    for row in sorted(answer.get("rows", []), key=lambda x: -x["mtime"]):
        project = by_dir.get(row["project"])
        here = ctx.local_dir(project) / f"{row['sid']}.jsonl" if project else None
        if (row["state"] == "idle" and here is not None and here.exists()
                and here.stat().st_mtime > row["mtime"] + 5 + (time.time() - now)):
            continue
        rows.append((row["sid"], row["state"], ago(now - row["mtime"]), row["title"] or "(untitled)",
                     project.name if project else row["project"]))
    return rows


def ago(seconds: float) -> str:
    age = max(0, int(seconds))
    return f"{age // 60} min ago" if age < 5400 else (f"{age // 3600} h ago" if age < 172800 else f"{age // 86400} days ago")


def switch_window(ctx: Ctx, pid, sid: str, note: str, after_turn: Path = None, after_hook: int = None) -> None:
    """Type /resume <sid> into the window of `pid` (then the note, if any): after its current turn
    ends, or after the hook that is running in it has returned."""
    state = ctx.ensure_state()
    argv = ctx.self_argv("_type", "--pid", str(pid), "--text", f"/resume {sid}", "--log", str(state / f"back-{sid[:8]}.log"))
    argv += (["--after-exit-of", str(after_hook)] if after_hook else
             ["--after-turn", str(after_turn), "--offset", str(after_turn.stat().st_size)])
    if note:
        note_file = state / f"note-{sid[:8]}.txt"
        note_file.write_text(note, encoding="utf-8")
        argv += ["--then-file", str(note_file)]
    desktop.spawn_detached(argv, state)


def laptop_command(ctx: Ctx, sid: str) -> list:
    return ["claude", *ctx.cfg.laptop_args, "--resume", sid]


def open_tab(ctx: Ctx, project: Project, sid: str, title: str, note: str) -> None:
    """Continue a session brought back in a new terminal tab, then tell it what the move stopped."""
    desktop.open_terminal(laptop_command(ctx, sid), project.local)
    log(f"opened \"{title}\" in a new tab")
    if not note:
        return
    deadline = time.time() + 90
    while time.time() < deadline:
        window = next((w for w in open_windows(ctx) if w["sid"] == sid), None)
        if window and window["status"] == "idle":
            time.sleep(1.5)
            ok = desktop.console_keys(window["pid"], ["\x1b", note, "\r"])
            log("told the session what the move stopped" if ok else f"could not type the note; tell it: {note}")
            return
        time.sleep(0.5)
    log(f"the tab did not show the session within 90 s; tell it yourself: {note}")


def resume_hint(ctx: Ctx, project: Project, sid: str) -> str:
    return f"cd {shlex.quote(project.local)} && " + " ".join(shlex.quote(a) for a in laptop_command(ctx, sid))


@contextlib.contextmanager
def pid_marker(path: Path):
    """A pid file the page follows a move by, removed when the move ends."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(desktop.pid_stamp())
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
