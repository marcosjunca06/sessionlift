"""The keeper: a background process on the laptop that makes moves fast.

- One connection, held open. A fresh ssh connection costs seconds before its first command (about
  1.5 s to connect and 1 s for the first channel were measured; later commands took 0.35 s). The
  keeper keeps one and runs other processes' commands over it, on 127.0.0.1 with a token from
  keeper.json in the state directory.
- Copies ahead. Every 15 s it brings the server's copy of each session open here, and of the five
  closed most recently, up to date, in ~/.claude/sessionlift-archive on the server, where a move looks
  first and the server's session list never does. Only what is new goes, 8 MB at a time, so a long
  conversation's first copy runs in the background instead of holding up its move.

Without it, moves work the same, only slower. On Windows it starts at logon (`keeper --install`), and
the hooks and the page start it when it is not running.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

from . import desktop
from .context import Ctx, log
from .moves import _to_server
from .sessions import closed_sessions, open_windows
from .transcripts import session_files, tar_files
from .transport import NotStarted, SshTransport, paramiko_run, recv_frame, send_frame

STAGE_CHUNK = 8_000_000   # transcript bytes per upload, so a move never waits long behind one
CYCLE_S = 15
TASK = "sessionlift keeper"


def _after_lines(data: bytes, k: int) -> int:
    """The offset just past the k-th newline of data."""
    pos = 0
    for _ in range(k):
        pos = data.index(b"\n", pos) + 1
    return pos


def _tail_hash(path: Path, cut: int) -> str:
    """sha256 of the 64 KB before `cut`: whether a transcript still begins as it did."""
    with open(path, "rb") as f:
        lo = max(0, cut - 65536)
        f.seek(lo)
        return hashlib.sha256(f.read(cut - lo)).hexdigest()


def stage_session(ctx: Ctx, project, sid: str, rec, chunk: int = STAGE_CHUNK) -> tuple:
    """Bring the archive's copy of laptop session `sid` up to date on the server. Only what is new goes,
    at most `chunk` bytes of transcript per call. rec is what the server held after the last call.
    Returns the new record (None when the server's copy is not where rec left it, which the next call
    works out afresh) and whether more is waiting to go."""
    folder = ctx.local_dir(project)
    path = folder / f"{sid}.jsonl"
    st = path.stat()
    files = {n: p for n, p in session_files(folder, sid).items() if n != f"{sid}.jsonl"}
    sides = {n: [p.stat().st_size, p.stat().st_mtime_ns] for n, p in files.items()}
    if rec and rec.get("fsize") == st.st_size and rec.get("mtime") == st.st_mtime_ns and rec.get("sides") == sides:
        return rec, False
    fresh, base, expected, known = False, 0, 0, {}
    where = {"sid": sid, "project": ctx.remote_dir(project)}
    if rec and rec["cut"] <= st.st_size and _tail_hash(path, rec["cut"]) == rec["tail"]:
        base, expected, known = rec["cut"], rec["size"], rec.get("sides", {})
        with open(path, "rb") as f:
            f.seek(base)
            new = f.read()
    else:
        # What the archive holds already counts when it is how this transcript begins: a session back
        # from the server, or a keeper restarted without its record.
        data = path.read_bytes()
        mine = _to_server(project, data[:data.rfind(b"\n") + 1])
        probe = ctx.server.call("stage_probe", where)
        if not probe.ok:
            return rec, False
        probe = probe.json()
        if probe.get("on_server"):
            return None, False
        have = int(probe.get("size") or 0)
        if have and have <= len(mine) and hashlib.sha256(mine[:have]).hexdigest() == probe.get("sha"):
            base, expected = _after_lines(data, mine.count(b"\n", 0, have)), have
        else:
            fresh = True
        new = data[base:]
    stop = (new.rfind(b"\n", 0, chunk) + 1 or new.find(b"\n") + 1) if len(new) > chunk else new.rfind(b"\n") + 1
    more = new.find(b"\n", stop) != -1
    delta = _to_server(project, new[:stop])
    payload = {n: (_to_server(project, p.read_bytes()) if n.endswith(".jsonl") else p)
               for n, p in files.items() if known.get(n) != sides[n]}
    if delta or payload or fresh:
        payload["arriving.jsonl"] = delta
        r = ctx.server.call("stage_append", {**where, "fresh": fresh, "expected": expected}, tar_files(payload))
        if not r.ok:
            return rec, False  # again next cycle
        answer = r.json()
        if answer.get("result") == "busy":
            return rec, False
        if answer.get("result") != "ok" or int(answer.get("size", -1)) != expected + len(delta):
            return None, False
        expected = int(answer["size"])
        if len(delta) > 1_000_000:
            log(f"copied {len(delta) / 1e6:.1f} MB of {sid[:8]} ahead to the server" + (", more to come" if more else ""))
    cut = base + stop
    return ({"cut": cut, "size": expected, "tail": _tail_hash(path, cut), "fsize": -1 if more else st.st_size,
             "mtime": st.st_mtime_ns, "sides": sides}, more)


def keeper_cycle(ctx: Ctx, staged: dict, budget_s: float = 10) -> bool:
    """One round: what the server is doing (for the hooks and the page), then the copies. True while a
    long first copy still has more to send."""
    state = ctx.ensure_state()
    transport = ctx.server
    if isinstance(transport, SshTransport) and transport.client() is None:
        (state / "keeper-status.json").write_text(json.dumps({"at": time.time(), "ok": False}))
        return False
    r = ctx.server.call("keeper_status", {"projects": [ctx.remote_dir(p) for p in ctx.cfg.projects]})
    status = r.json() if r.ok else {}
    on_server = set(status.get("on_server", []))
    (state / "keeper-status.json").write_text(json.dumps({"at": time.time(), "ok": r.ok,
                                                          "logged_in": bool(status.get("logged_in")),
                                                          "running": status.get("running", [])}))
    windows = open_windows(ctx)
    by_name = {p.name: p for p in ctx.cfg.projects}
    sids = [(by_name[w["project"]], w["sid"]) for w in windows] + \
           [(by_name[c["project"]], c["sid"]) for c in closed_sessions(ctx, {w["sid"] for w in windows})]
    budget = time.time() + budget_s  # a long first copy goes on next round
    pending = False
    for project, sid in sids:
        while True:
            if time.time() > budget:
                pending = True
                break
            if (sid in on_server or desktop.stamp_alive(_read(state / f"send-{sid[:8]}.pid"))
                    or desktop.stamp_alive(_read(state / f"back-{sid[:8]}.pid"))):
                break
            try:
                rec, more = stage_session(ctx, project, sid, staged.get(sid))
            except Exception as e:  # noqa: BLE001 - one session's trouble never stops the others
                log(f"copying {sid[:8]} ahead failed: {e!r}")
                rec, more = None, False
            if rec is None:
                staged.pop(sid, None)
            else:
                staged[sid] = rec
            if not more:
                break
    known = {t.stem for p in ctx.cfg.projects for t in ctx.local_dir(p).glob("*.jsonl")}
    for sid in [s for s in staged if s not in known]:
        del staged[sid]
    (state / "staged.json").write_text(json.dumps(staged))
    return pending


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def serve(transport: SshTransport, token: str):
    """The keeper's door for other processes: on 127.0.0.1 only, the token required. Each request runs
    on the one connection, side by side with the others."""
    import socketserver

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            sock = self.request
            try:
                sock.settimeout(30)
                head = json.loads(recv_frame(sock))
                data = recv_frame(sock)
            except (OSError, ValueError):
                return
            if not secrets.compare_digest(str(head.get("token", "")), token):
                return
            meta, out, err = {"code": 255, "not_started": True}, b"", b""
            client = transport.client()
            if client is not None:
                try:
                    r = paramiko_run(client, str(head["remote"]), data or None, float(head.get("timeout", 600)))
                    meta, out, err = {"code": r.returncode}, r.stdout, r.stderr
                except NotStarted:
                    transport.forget()
                except TimeoutError as e:
                    meta, err = {"code": 124, "timeout": True}, str(e).encode()
                except Exception as e:  # noqa: BLE001 - dropped mid-command: it may have run, so not again
                    transport.forget()
                    meta, err = {"code": 254}, f"connection lost during the command: {e}".encode()
            with contextlib.suppress(OSError):
                sock.settimeout(60)
                send_frame(sock, json.dumps(meta).encode())
                send_frame(sock, out)
                send_frame(sock, err)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def write_private(path: Path, text: str) -> None:
    """Write a file only this user can read (0600 on POSIX; on Windows the state directory is already
    the user's own), replacing it whole: keeper.json holds the token that lets a process run commands
    on the server through the keeper."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    for attempt in range(20):  # Windows refuses to replace a file another process is reading
        try:
            os.replace(str(tmp), str(path))
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def _claim_lock(path: Path) -> bool:
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if desktop.stamp_alive(_read(path)):
                return False
            path.unlink(missing_ok=True)
            continue
        os.write(fd, desktop.pid_stamp().encode())
        os.close(fd)
        return True
    return False


def run_keeper(ctx: Ctx) -> int:
    state = ctx.ensure_state()
    logfile = state / "keeper.log"
    with contextlib.suppress(OSError):
        if logfile.stat().st_size > 2_000_000:
            logfile.replace(state / "keeper.old.log")
    sys.stdout = sys.stderr = open(logfile, "a", encoding="utf-8", buffering=1)
    if not _claim_lock(state / "keeper.lock"):
        return 0  # one runs already
    transport = SshTransport(ctx.cfg, state, use_keeper=False)  # never through itself
    ctx.transport = transport
    token = secrets.token_hex(16)
    server = serve(transport, token)
    write_private(state / "keeper.json", json.dumps({"pid": desktop.pid_stamp(), "port": server.server_address[1],
                                                     "token": token}))
    (state / "keeper.starting").unlink(missing_ok=True)
    log(f"keeper up: pid {os.getpid()}, port {server.server_address[1]}")
    try:
        staged = json.loads((state / "staged.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        staged = {}
    while True:
        pending = False
        try:
            pending = keeper_cycle(ctx, staged)
        except Exception as e:  # noqa: BLE001 - the server away, the laptop asleep: next round
            log(f"keeper round failed: {e!r}")
        time.sleep(1 if pending else CYCLE_S)


def keeper_running(ctx: Ctx):
    try:
        info = json.loads((ctx.state / "keeper.json").read_text(encoding="utf-8"))
        return info if desktop.stamp_alive(str(info.get("pid", ""))) else None
    except (OSError, ValueError, AttributeError):
        return None


def ensure_keeper(ctx: Ctx) -> None:
    """Start the keeper unless it runs (Windows): a file read and a process check when it does. A move
    never fails for want of it; it only goes slower."""
    if not desktop.WINDOWS_UI or keeper_running(ctx):
        return
    starting = ctx.ensure_state() / "keeper.starting"
    with contextlib.suppress(OSError):
        if time.time() - starting.stat().st_mtime < 20:
            return  # one is starting
    try:
        starting.write_text(desktop.pid_stamp())
        desktop.spawn_detached(ctx.self_argv("keeper"), ctx.state)
    except Exception as e:  # noqa: BLE001
        log(f"could not start the keeper: {e}")


def cmd_keeper(ctx: Ctx, args) -> int:
    if args.ensure:
        ensure_keeper(ctx)
        return 0
    if args.stop or args.uninstall:
        if args.uninstall:
            desktop.uninstall_logon_task(TASK)
            log(f"removed the logon task {TASK!r}")
        info = keeper_running(ctx)
        if info:
            desktop.kill(int(str(info["pid"]).split()[0]))
            log("stopped the keeper")
        for name in ("keeper.json", "keeper.lock"):
            (ctx.state / name).unlink(missing_ok=True)
        return 0
    if args.install:
        args_line = " ".join(f'"{a}"' if re.search(r"\s", a) else a for a in ctx.self_argv("keeper")[1:])
        desktop.install_logon_task(TASK, desktop.pythonw_exe(), args_line, str(ctx.ensure_state()),
                                   "Keeps the sessionlift server connection open and its copies of Claude Code "
                                   "sessions current, so moving a session is fast")
        log(f"logon task {TASK!r} registered")
        ensure_keeper(ctx)
        return 0
    return run_keeper(ctx)
