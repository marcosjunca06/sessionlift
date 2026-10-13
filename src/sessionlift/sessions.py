"""Sessions on the laptop: which Claude Code windows are open and in what state, which sessions closed
recently, and which project a session belongs to.

The windows come from Claude Code's own registry, ~/.claude/sessions/<pid>.json: the session each
window holds (it follows /resume), its working directory, and whether it is busy, waiting on a prompt
or dialog, or idle. That registry is a Claude Code internal, not a public API.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import desktop
from .config import Project
from .context import Ctx
from .transcripts import TitleCache, is_session_id

titles = TitleCache()
_talked: dict = {}


def same_dir(a: str, b: str) -> bool:
    if not a or not b:
        return False
    norm = lambda p: os.path.normcase(os.path.normpath(p)) if os.name == "nt" else os.path.normpath(p)  # noqa: E731
    return norm(a) == norm(b)


def project_of_cwd(ctx: Ctx, cwd: str):
    return next((p for p in ctx.cfg.projects if same_dir(cwd, p.local)), None)


def open_windows(ctx: Ctx, alive=desktop.process_alive) -> list:
    """The Claude Code windows open on this laptop in a configured project that hold a conversation,
    newest first: {pid, sid, status, title, active, project}."""
    rows = []
    for f in sorted((ctx.claude / "sessions").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            pid, sid = int(d["pid"]), str(d["sessionId"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        project = project_of_cwd(ctx, d.get("cwd") or "")
        if d.get("kind", "interactive") != "interactive" or project is None or not is_session_id(sid):
            continue
        transcript = ctx.local_dir(project) / f"{sid}.jsonl"
        if not transcript.exists() or not alive(pid, d.get("procStart")):
            continue
        rows.append({"pid": pid, "sid": sid, "status": d.get("status") or "idle", "project": project.name,
                     "title": titles.title(transcript, sid), "active": transcript.stat().st_mtime})
    return sorted(rows, key=lambda r: -r["active"])


def window_status(ctx: Ctx, pid: int):
    """busy, waiting, idle (or another word Claude Code writes) for window `pid`; None if unknown."""
    try:
        return json.loads((ctx.claude / "sessions" / f"{pid}.json").read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError):
        return None


def closed_sessions(ctx: Ctx, skip: set, limit: int = 5, hours: float = 24) -> list:
    """Laptop sessions no window holds, newest first, from the last `hours`: those a conversation
    happened in, not ones only a command was typed into."""
    since = time.time() - hours * 3600
    found = []
    for project in ctx.cfg.projects:
        folder = ctx.local_dir(project)
        if folder.is_dir():
            found += [(p.stat().st_mtime, p, project) for p in folder.glob("*.jsonl")]
    rows = []
    for mtime, path, project in sorted(found, key=lambda t: t[0], reverse=True):
        if mtime < since or len(rows) >= limit:
            break
        sid = path.stem
        if sid in skip or not is_session_id(sid):
            continue
        size = path.stat().st_size
        if _talked.get(path, (-1, False))[0] != size:
            with open(path, "rb") as f:
                f.seek(max(0, size - 262144))
                _talked[path] = (size, b'"type":"assistant"' in f.read())
        if _talked[path][1]:
            rows.append({"sid": sid, "title": titles.title(path, sid), "active": mtime, "project": project.name})
    return rows


def find_session(ctx: Ctx, sid: str) -> tuple:
    """(project, full session id) for a laptop session, by its id or its first characters."""
    if not re.fullmatch(r"[0-9a-f-]{1,36}", sid or ""):
        raise SystemExit(f"not a session id: {sid!r}")
    matches = []
    for project in ctx.cfg.projects:
        folder = ctx.local_dir(project)
        if folder.is_dir():
            matches += [(project, p.stem) for p in folder.glob(f"{sid}*.jsonl") if is_session_id(p.stem)]
    if len(matches) != 1:
        where = ", ".join(p.local for p in ctx.cfg.projects)
        raise SystemExit(f"no laptop session {sid} in {where}" if not matches else
                         f"{len(matches)} laptop sessions start with {sid}: {[m[1][:8] for m in matches]}")
    return matches[0]


def resolve_session(ctx: Ctx, arg: str = None):
    """(project, session id): the one named, else the session this command runs in (Claude Code sets
    CLAUDE_CODE_SESSION_ID for its shell); None when neither, so the caller asks. Never the newest
    transcript by itself: a move stops the session it takes."""
    if arg:
        return find_session(ctx, arg)
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return find_session(ctx, os.environ["CLAUDE_CODE_SESSION_ID"])
    return None


def transcript_path(ctx: Ctx, project: Project, sid: str) -> Path:
    return ctx.local_dir(project) / f"{sid}.jsonl"
