"""Claude Code transcripts as files: where they live, how a working directory maps between machines,
titles, and the tar archives that carry them. Runs on the laptop and on the server (stdlib only,
Python 3.8+ on the server).

A session is `~/.claude/projects/<slug of its working directory>/<session id>.jsonl` plus a directory
of the same name holding its side files (subagent transcripts, large tool results). These locations and
the JSONL layout are Claude Code internals, not a public API.
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
import time
from pathlib import Path, PurePosixPath

SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CWD_FIELD = re.compile(rb'"cwd":"((?:[^"\\]|\\.)*)"')
TITLE = re.compile(rb'"(customTitle|aiTitle)":"((?:[^"\\]|\\.)*)"')
UUID_FIELD = re.compile(rb'"uuid":"([0-9a-f-]{36})"')


def slug(path: str) -> str:
    """Claude Code's project directory name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def is_session_id(text: str) -> bool:
    return bool(SESSION_ID.fullmatch(text))


def _windows_style(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith("\\\\")


def rewrite_cwd(data: bytes, local: str, remote: str, to_server: bool) -> bytes:
    """Point every "cwd" in a transcript at the other machine's checkout; nothing else changes.
    `local` is the laptop's directory (a Windows path is matched without regard to case, with either
    slash), `remote` the server's."""
    windows = _windows_style(local)
    sep = "\\" if windows else "/"

    def sub(m: re.Match) -> bytes:
        cwd = json.loads(b'"' + m.group(1) + b'"')
        if to_server:
            head = cwd[:len(local)]
            same = head.lower() == local.lower() if windows else head == local
            if not (same and cwd[len(local):len(local) + 1] in ("", "\\", "/")):
                return m.group(0)
            new = remote + cwd[len(local):].replace("\\", "/")
        else:
            if not (cwd == remote or cwd.startswith(remote + "/")):
                return m.group(0)
            new = local + cwd[len(remote):].replace("/", sep)
        return b'"cwd":' + json.dumps(new, ensure_ascii=False).encode()

    return CWD_FIELD.sub(sub, data)


def session_files(project_dir: Path, sid: str) -> dict:
    """{name relative to the project directory: path} for a transcript and its side files."""
    files = {f"{sid}.jsonl": project_dir / f"{sid}.jsonl"}
    side = project_dir / sid
    if side.is_dir():
        for p in sorted(side.rglob("*")):
            if p.is_file():
                files[p.relative_to(project_dir).as_posix()] = p
    return files


def _titles(data: bytes) -> dict:
    return {key: json.loads(b'"' + text + b'"') for key, text in TITLE.findall(data)}


def session_title(data: bytes, sid: str) -> str:
    """The name given with /rename (customTitle), else Claude Code's own title (aiTitle)."""
    titles = _titles(data)
    return (titles.get(b"customTitle") or titles.get(b"aiTitle") or f"session {sid[:8]}")[:80]


class TitleCache:
    """session_title() for transcripts on disk, reading only what was added since the last call: the
    page asks every few seconds, and a transcript can run to hundreds of megabytes."""

    def __init__(self) -> None:
        self.seen = {}

    def title(self, path: Path, sid: str) -> str:
        size = path.stat().st_size
        seen, titles = self.seen.get(path, (0, {}))
        if size != seen:
            start = max(0, seen - 65536) if 0 < seen < size else 0
            titles = dict(titles) if start else {}
            with open(path, "rb") as f:
                f.seek(start)
                titles.update(_titles(f.read()))
            self.seen[path] = (size, titles)
        return (titles.get(b"customTitle") or titles.get(b"aiTitle") or f"session {sid[:8]}")[:80]


def tail_title(path: Path, sid: str, tail_bytes: int = 3_000_000) -> str:
    """The title, read from the end of the file first (titles are rewritten as a session goes on)."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - tail_bytes))
        titles = _titles(f.read())
    if not titles and size > tail_bytes:
        titles = _titles(path.read_bytes())
    return (titles.get(b"customTitle") or titles.get(b"aiTitle") or "")[:80]


def uuids(data: bytes) -> set:
    return set(UUID_FIELD.findall(data))


# ---------------------------------------------------------------- archives

def tar_files(files: dict) -> bytes:
    """A .tar.gz of {archive name: bytes or Path}, keeping each file's mtime."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, src in files.items():
            data = src.read_bytes() if isinstance(src, Path) else src
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(src.stat().st_mtime) if isinstance(src, Path) else int(time.time())
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def safe_name(name: str) -> str:
    """An archive member name as a relative path that stays inside its directory, or ValueError."""
    clean = name.replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
    parts = PurePosixPath(clean).parts
    if not parts or clean.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", clean):
        raise ValueError(f"refusing an archive member outside its directory: {name!r}")
    return "/".join(parts)


def untar(data: bytes) -> dict:
    """{name: (bytes, mtime)} for the regular files in a .tar.gz; names are checked with safe_name."""
    out = {}
    if not data:
        return out
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.isfile():
                out[safe_name(m.name)] = (tar.extractfile(m).read(), m.mtime)
    return out


def write_file(path: Path, data: bytes, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime:
        os.utime(path, (mtime, mtime))


def extract_into(data: bytes, target: Path) -> list:
    """Write the archive's files under `target`, keeping their mtimes; returns the names written."""
    names = []
    for name, (body, mtime) in untar(data).items():
        write_file(target / name, body, mtime)
        names.append(name)
    return names
