"""Git on both machines: which uncommitted files belong to the moving session, committing exactly those,
and bringing a checkout up to origin without touching anyone else's work. Runs on the laptop and on
the server (stdlib only, Python 3.8+ on the server).

The rule for "belongs to the session" came from what went wrong with simpler ones:

- "Commit everything" put other sessions' half-finished changes into the mover's commits.
- Looking only at Edit and Write calls missed sessions that edit files with scripts run through Bash.
- A session sleeping inside one long command took a file another session wrote during that command,
  hence the condition that a command only counts when the session also names the path.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

BIG_FILE = 20_000_000  # untracked files larger than this never ride along in a snapshot commit
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_STAMP = re.compile(rb'"timestamp":"(\d{4}-\d\d-\d\dT[0-9:.]+Z)"')


def run(argv: list, data: bytes = None, **kw) -> subprocess.CompletedProcess:
    """subprocess.run that never lets a child wait for input: stdin is `data` or nothing, git never
    prompts, and on Windows no child gets a console window of its own."""
    kw.setdefault("capture_output", True)
    kw.setdefault("env", dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    if os.name == "nt":
        kw.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    if data is None:
        kw["stdin"] = subprocess.DEVNULL
    return subprocess.run(argv, input=data, **kw)


def git(repo, *args: str, check: bool = True) -> str:
    r = run(["git", "-C", str(repo), *args], text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def dirty_paths(repo) -> list:
    """Every changed or untracked (not ignored) path in a working tree, relative to its top."""
    # Not git(): it strips the output, and with it the leading space of the first " M path" entry.
    out = run(["git", "-C", str(repo), "status", "--porcelain", "-z", "--untracked-files=all"], text=True).stdout
    parts, paths, i = out.split("\0"), [], 0
    while i < len(parts):
        entry = parts[i]
        if len(entry) > 3:
            paths.append(entry[3:])
            if entry[0] in "RC":  # a rename or copy: the next entry is its old path
                i += 1
        i += 1
    return paths


def small_dirty_paths(repo) -> list:
    return [p for p in dirty_paths(repo)
            if not ((Path(repo) / p).is_file() and (Path(repo) / p).stat().st_size > BIG_FILE)]


def _when(stamp) -> float:
    if not stamp:
        return None
    text = stamp.decode() if isinstance(stamp, bytes) else stamp
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def write_windows(transcript: Path) -> tuple:
    """When one transcript's tool calls could have written files: ([(path, start, end)] for Edit,
    Write, MultiEdit and NotebookEdit; [(start, end)] for Bash run in the foreground, from each call
    to its result, or open-ended while it has none; [start] for Bash run in the background, which
    may write at any time after it starts)."""
    pending = {}
    edits, bash, background = [], [], []
    try:
        f = open(transcript, "rb")
    except OSError:
        return edits, bash, background
    with f:
        for line in f:
            if b'"tool_use"' in line and (b'"name":"Bash"' in line or
                                          any(f'"name":"{t}"'.encode() in line for t in EDIT_TOOLS)):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                start = _when(d.get("timestamp"))
                content = (d.get("message") or {}).get("content")
                for block in content if isinstance(content, list) and start else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    args = block.get("input") or {}
                    if block.get("name") == "Bash" and args.get("run_in_background"):
                        background.append(start)
                    elif block.get("name") == "Bash":
                        pending[block.get("id")] = (None, start)
                    elif block.get("name") in EDIT_TOOLS and (args.get("file_path") or args.get("notebook_path")):
                        pending[block.get("id")] = (args.get("file_path") or args.get("notebook_path"), start)
            elif pending and b'"tool_use_id"' in line:
                done = [i.decode() for i in re.findall(rb'"tool_use_id":"([^"]+)"', line) if i.decode() in pending]
                stamps = _STAMP.findall(line)
                end = _when(stamps[-1]) if stamps else None
                for call in done:
                    path, start = pending.pop(call)
                    if path is None:
                        bash.append((start, end or start))
                    else:
                        edits.append((path, start, end or start))
    # A call with no result yet is still running, or was cut off: it may be writing now.
    for path, start in pending.values():
        if path is None:
            bash.append((start, float("inf")))
        else:
            edits.append((path, start, float("inf")))
    return edits, bash, background


def path_key(path: str) -> str:
    """A path as own_files compares them: on Windows without regard to case and with either slash."""
    return path.lower().replace("/", "\\") if os.name == "nt" else path


def session_transcripts(project_dir: Path, sid: str) -> list:
    """A session's transcript and its subagents'."""
    side = project_dir / sid / "subagents"
    return [project_dir / f"{sid}.jsonl", *(sorted(side.glob("*.jsonl")) if side.is_dir() else [])]


def own_files(sid: str, dirty: list, repo, project_dir: Path, group=frozenset(), now: float = None) -> tuple:
    """Split dirty paths into those the moving session wrote last and the rest.

    A session counts as a path's writer when the path's last modification falls inside one of its tool
    calls (its subagents' too, and calls still running): an Edit-like call on that path, or a Bash
    command in the foreground provided the session names the path somewhere. A path is the mover's
    own when it is a writer and no other session of this project (last 24 h) is. Anything another
    session wrote, two sessions could have written, or no one can be shown to have written stays
    uncommitted for its owner; so do deleted files, which have no time to go by. `group`: sessions
    moving together; a path any of them wrote travels, as long as this session is one of its writers."""
    repo, project_dir = Path(repo), Path(project_dir)
    since = (now or time.time()) - 24 * 3600
    sessions = {f.stem for f in project_dir.glob("*.jsonl") if f.stat().st_mtime > since} | {sid}
    windows = {}
    for s in sessions:
        edits, bash, background = [], [], []
        for t in session_transcripts(project_dir, s):
            e, b, g = write_windows(t)
            edits += [(path_key(path), start, end) for path, start, end in e]
            bash += b
            background += g
        windows[s] = (edits, bash, background)
    texts = {}

    def mentions(session: str, rel: str) -> bool:
        forms = (rel.encode(), rel.replace("/", "\\\\").encode())  # as typed, and as JSON writes "\"
        for t in session_transcripts(project_dir, session):
            if t not in texts:
                texts[t] = t.read_bytes() if t.exists() else b""
            if any(form in texts[t] for form in forms):
                return True
        return False

    def edited(s: str, key: str, mtime: float) -> bool:
        return any(path == key and start - 2 <= mtime <= end + 2 for path, start, end in windows[s][0])

    def ran(s: str, mtime: float) -> bool:
        return any(start - 1 <= mtime <= end + 2 for start, end in windows[s][1])

    def in_background(s: str, mtime: float) -> bool:
        return any(start - 1 <= mtime for start in windows[s][2])

    # The mover's own claim first: most dirty paths are other sessions' and end there, so other
    # transcripts are searched only for the few paths it claims.
    mine, rest = [], []
    for rel in dirty:
        target = repo / rel
        if not target.exists():
            rest.append(rel)
            continue
        mtime, key = target.stat().st_mtime, path_key(str(target))
        strong = edited(sid, key, mtime)
        if not (strong or (ran(sid, mtime) and mentions(sid, rel))):
            rest.append(rel)
            continue
        # Against the mover's own edit of this very path at that moment, only another session's edit
        # of it counts: a command of theirs that merely ran then and names the path (a `git status`
        # lists every dirty file) is no evidence. Against a claim that rests on a command, any other
        # session's command that ran then and names the path counts too, and so do background
        # commands, which may write at any time after they start.
        others = [s for s in windows if s != sid and s not in group]
        if any(edited(s, key, mtime) or (not strong and (ran(s, mtime) or in_background(s, mtime)) and mentions(s, rel))
               for s in others):
            rest.append(rel)
            continue
        mine.append(rel)
    return mine, rest


def commit_paths(repo, why: str, take: list) -> str:
    """Commit exactly these paths (`--only`: nothing else staged in the shared index rides along).
    Returns git's complaint, or "" when committed."""
    for _ in range(10):  # another process may hold index.lock for a moment
        r = run(["git", "-C", str(repo), "add", "-A", "--", *take], text=True)
        if r.returncode == 0 or "index.lock" not in r.stderr:
            break
        time.sleep(0.5)
    body = "\n".join(take[:40]) + (f"\n... and {len(take) - 40} more" if len(take) > 40 else "")
    for _ in range(10):
        r = run(["git", "-C", str(repo), "commit", "-q", "--only", "-m", f"{why}\n\n{body}", "--", *take], text=True)
        if r.returncode == 0 or "index.lock" not in r.stderr:
            break
        time.sleep(0.5)
    return (r.stderr or r.stdout).strip()[:400] if r.returncode else ""


def _count(repo, spec: str) -> int:
    out = git(repo, "rev-list", "--count", spec, check=False)
    return int(out) if out.isdigit() else 0


def rebase_on_origin(repo, log, lock=None) -> bool:
    """Take origin's new commits: a fast-forward, which leaves uncommitted files alone, or a rebase
    when this branch has commits of its own."""
    with lock or contextlib.nullcontext():
        run(["git", "-C", str(repo), "fetch", "-q"])
        if not _count(repo, "HEAD..@{u}"):
            return True
        if not _count(repo, "@{u}..HEAD"):
            r = run(["git", "-C", str(repo), "merge", "-q", "--ff-only", "@{u}"], text=True)
            if r.returncode == 0:
                log("took origin's new commits")
                return True
            log(f"could not fast-forward (uncommitted files here that the new commits change?): {r.stderr.strip()[:300]}")
            return False
        r = run(["git", "-C", str(repo), "pull", "-q", "--rebase", "--autostash"], text=True)
        if r.returncode:
            run(["git", "-C", str(repo), "rebase", "--abort"])
            log(f"could not rebase on origin, nothing pushed: {r.stderr.strip()[:300]}")
            return False
        log("took origin's new commits")
        return True


def commit_own_and_push(repo, why: str, sid: str, project_dir: Path, log, lock=None, pull: bool = False,
                        group=frozenset()) -> str:
    """Commit and push the moving session's own uncommitted work so the other machine has it, and
    nothing of anyone else's (own_files). pull: take origin's new commits too. Nothing here stops a
    move: a failure is logged and the move goes on with what origin has. Returns the branch."""
    lock = lock or contextlib.nullcontext()
    dirty = small_dirty_paths(repo)
    take, left = own_files(sid, dirty, repo, project_dir, group)
    with lock:
        if take:  # a session moving beside this one may have committed a shared file meanwhile
            still = set(dirty_paths(repo))
            take = [p for p in take if p in still]
        branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if left:
            log(f"left uncommitted for whoever wrote them: {len(left)} file(s), e.g. {sorted(left)[:3]}")
        if take:
            problem = commit_paths(repo, why, take)
            log(f"could not commit, so {len(take)} file(s) stay uncommitted here: {problem}" if problem
                else f"committed {len(take)} uncommitted file(s)")
        if not git(repo, "rev-parse", "--abbrev-ref", "@{u}", check=False):
            r = run(["git", "-C", str(repo), "push", "-q", "-u", "origin", branch], text=True)
            log(f"pushed {branch}" if r.returncode == 0 else f"push failed: {r.stderr.strip()[:300]}")
            return branch
        if pull and not rebase_on_origin(repo, log, lock):
            return branch
        ahead = _count(repo, "@{u}..HEAD")
        if ahead:
            r = run(["git", "-C", str(repo), "push", "-q"], text=True)
            if r.returncode and not pull and rebase_on_origin(repo, log, lock):  # origin moved on: take it, push again
                r = run(["git", "-C", str(repo), "push", "-q"], text=True)
            log(f"pushed {ahead} commit(s) on {branch}" if r.returncode == 0 else f"push failed: {r.stderr.strip()[:300]}")
        return branch


def server_checkout(repo, branch: str = "", leaving: str = "", project_dir: Path = None, why: str = "") -> tuple:
    """The server checkout, which every session on the server shares. leaving (a session going back to
    the laptop): commit the files it wrote there itself, and nothing of the other sessions' unfinished
    work. Then take origin's commits, on `branch` (checked out first) when given, and push the server's
    own. Taking them never touches uncommitted work: a fast-forward that would overwrite it is refused
    by git, and a rebase runs only when no uncommitted file is one origin changed.

    Returns (markers, ok): markers among COMMITTED n, LEFT n, COMMIT_FAILED, CHECKOUT_FAILED,
    NO_UPSTREAM, BEHIND, DIVERGED, PUSHED <branch>, NOT_PUSHED, CHECKOUT <hash subject>."""
    markers = []
    if leaving and project_dir is not None:
        take, left = own_files(leaving, small_dirty_paths(repo), repo, project_dir)
        if left:
            markers.append(f"LEFT {len(left)}")
        if take:
            problem = commit_paths(repo, why or "Snapshot: uncommitted work when a session moved back", take)
            markers.append("COMMIT_FAILED" if problem else f"COMMITTED {len(take)}")
    run(["git", "-C", str(repo), "fetch", "-q", "origin"])
    if branch:
        if (run(["git", "-C", str(repo), "checkout", "-q", branch]).returncode and
                run(["git", "-C", str(repo), "checkout", "-q", "-b", branch, f"origin/{branch}"]).returncode):
            markers.append("CHECKOUT_FAILED")
            return markers, False
    br = git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    upstream = f"origin/{br}"
    if run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", upstream]).returncode:
        markers.append("NO_UPSTREAM")
    elif not _count(repo, f"{upstream}..HEAD"):
        if run(["git", "-C", str(repo), "merge", "-q", "--ff-only", upstream]).returncode:
            markers.append("BEHIND")
    else:
        changed = set(git(repo, "diff", "--name-only", f"HEAD...{upstream}", check=False).splitlines())
        if changed & set(dirty_paths(repo)):
            markers.append("BEHIND")
        elif run(["git", "-C", str(repo), "rebase", "-q", "--autostash", upstream]).returncode:
            run(["git", "-C", str(repo), "rebase", "--abort"])
            markers.append("DIVERGED")
            return markers, False
    if "NO_UPSTREAM" in markers or _count(repo, f"{upstream}..HEAD"):
        pushed = run(["git", "-C", str(repo), "push", "-q", "origin", f"HEAD:{br}"]).returncode == 0
        markers.append(f"PUSHED {br}" if pushed else "NOT_PUSHED")
    markers.append("CHECKOUT " + git(repo, "log", "-1", "--format=%h %s", check=False))
    return markers, True
