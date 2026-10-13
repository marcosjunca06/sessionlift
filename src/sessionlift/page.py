"""The sessions page (Windows): one window for moving sessions. It lists the Claude Code windows open
on this laptop, the sessions closed here today, and those on the server, each moved by one click;
"Move all to the server" before the laptop goes off, and it says when that is safe.

Every move runs as its own process, so closing the page stops nothing; the page follows each by its pid
file and log in the state directory. Also here: the small picker /from-server shows, which always
asks, even when there is only one session, and never brings anything back unasked.
"""

from __future__ import annotations

import contextlib
import os
import queue
import re
import threading
import time

from . import desktop
from .context import Ctx, log
from .moves import server_sessions, start_send
from .sessions import closed_sessions, open_windows, titles

PAGE_TITLE = "Claude sessions"
PICK_TIMEOUT_S = 300  # inside the hook's 600 s, after which Claude Code would hand the command to Claude


class Look:
    BG, CARD, LINE, BORDER, TEXT, MUTED = "#F6F7F9", "#FFFFFF", "#EAECF0", "#D0D5DD", "#101828", "#667085"
    ACCENT, ACCENT_DOWN, ERROR = "#0B7A66", "#096352", "#B42318"
    SMALL, BOLD, TITLE = ("Segoe UI", 9), ("Segoe UI Semibold", 10), ("Segoe UI Semibold", 14)


def short(text: str, n: int = 62) -> str:
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def ago(t: float) -> str:
    s = max(0, time.time() - t)
    return "now" if s < 60 else f"{s // 60:.0f} min ago" if s < 5400 else f"{s // 3600:.0f} h ago"


def _dpi_aware() -> None:
    if os.name == "nt":
        with contextlib.suppress(Exception):
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)


def _icon(ctx: Ctx, root) -> None:
    """The page's icon on a window, once `sessionlift shortcut` has made it."""
    icon = ctx.state / "sessions.ico"
    if icon.exists():
        with contextlib.suppress(Exception):
            root.iconbitmap(str(icon))


def pick_session(ctx: Ctx, rows: list):
    """The session to bring back, picked in a small window; None when cancelled, closed, or left
    unanswered for PICK_TIMEOUT_S."""
    import tkinter as tk
    _dpi_aware()
    p = Look
    chosen = []
    root = tk.Tk()
    root.title("Bring a session back")
    root.configure(bg=p.BG)
    _icon(ctx, root)
    root.attributes("-topmost", True)
    scale = root.winfo_fpixels("1i") / 96

    def px(n: float) -> int:
        return int(round(n * scale))

    def button(parent, text: str, command):
        frame = tk.Frame(parent, bg=p.BORDER, padx=1, pady=1)
        tk.Button(frame, text=text, command=command, font=p.SMALL, fg=p.TEXT, bg=p.CARD, activebackground="#F2F4F7",
                  relief="flat", bd=0, padx=px(10), pady=px(3), cursor="hand2").pack()
        return frame

    def choose(sid: str) -> None:
        chosen.append(sid)
        root.destroy()

    head = tk.Frame(root, bg=p.BG, padx=px(20), pady=px(16))
    head.pack(fill="x")
    tk.Label(head, text="Bring a session back", font=p.TITLE, fg=p.TEXT, bg=p.BG).pack(anchor="w")
    tk.Label(head, text=f"On {ctx.cfg.label}, newest first. The one you pick continues in this window.",
             font=p.SMALL, fg=p.MUTED, bg=p.BG).pack(anchor="w")
    outer = tk.Frame(root, bg=p.LINE, padx=1, pady=1)
    outer.pack(fill="x", padx=px(20))
    card = tk.Frame(outer, bg=p.CARD)
    card.pack(fill="x")
    for i, (sid, state, when, title, project) in enumerate(rows):
        if i:
            tk.Frame(card, bg=p.LINE, height=1).pack(fill="x")
        row = tk.Frame(card, bg=p.CARD, padx=px(14), pady=px(8))
        row.pack(fill="x")
        button(row, "←  Bring back", lambda s=sid: choose(s)).pack(side="right", padx=(px(12), 0))
        tk.Label(row, text=short(title), font=p.BOLD, fg=p.TEXT, bg=p.CARD, anchor="w").pack(fill="x")
        tk.Label(row, text=f"{state} · active {when} · {project} · {sid[:8]}", font=p.SMALL, fg=p.MUTED, bg=p.CARD,
                 anchor="w").pack(fill="x")
    foot = tk.Frame(root, bg=p.BG, padx=px(20), pady=px(14))
    foot.pack(fill="x")
    button(foot, "Cancel", root.destroy).pack(side="right")
    tk.Label(foot, text="Nothing moves until you pick one.", font=p.SMALL, fg=p.MUTED, bg=p.BG).pack(side="left")
    root.bind("<Escape>", lambda e: root.destroy())
    root.after(PICK_TIMEOUT_S * 1000, root.destroy)
    root.update_idletasks()
    w, h = max(root.winfo_reqwidth(), px(560)), root.winfo_reqheight()
    root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
    root.after(150, lambda: (root.lift(), root.focus_force()))
    root.mainloop()
    return chosen[0] if chosen else None


class Page:
    # What each move's log says, in order, and how the page puts it.
    STEPS = {"server": [("interrupted the session", "Interrupted what it was doing"),
                        ("saved it to", "Saved its unsent text"),
                        ("pressed Ctrl+D", "Closing its window"),
                        ("copied ahead", "Copying to the server"),
                        ("starting on the server while", "Starting on the server"),
                        ("has closed", "Window closed, starting on the server"),
                        ("server checkout", "Starting on the server"),
                        ("running on the server in tmux", "Live on the server")],
             "laptop": [("stopped sl-", "Stopped on the server"),
                        ("transcript back", "Copied back"),
                        ("in a new tab", "Opened in a new tab")]}
    DONE = {"server": "running on the server in tmux", "laptop": "in a new tab"}

    def __init__(self, ctx: Ctx) -> None:
        import tkinter as tk
        _dpi_aware()
        self.ctx, self.tk, p = ctx, tk, Look
        self.root = tk.Tk()
        self.root.title(PAGE_TITLE)
        self.root.configure(bg=p.BG)
        _icon(ctx, self.root)
        self.scale = self.root.winfo_fpixels("1i") / 96
        self.root.minsize(self.px(600), self.px(300))
        self.data = None
        self.moves = {}
        self.inbox = queue.Queue()
        self.server_wanted = threading.Event()
        self.layout, self.foot, self.rows = None, None, {}
        head = tk.Frame(self.root, bg=p.BG, padx=self.px(20), pady=self.px(16))
        head.pack(fill="x")
        tk.Label(head, text=PAGE_TITLE, font=p.TITLE, fg=p.TEXT, bg=p.BG).pack(anchor="w")
        tk.Label(head, text=f"This laptop and {ctx.cfg.label}. A move takes the session as it is.", font=p.SMALL,
                 fg=p.MUTED, bg=p.BG).pack(anchor="w")
        self.body = tk.Frame(self.root, bg=p.BG, padx=self.px(20))
        self.body.pack(fill="both", expand=True)
        tk.Frame(self.root, bg=p.LINE, height=1).pack(fill="x", pady=(self.px(16), 0))
        self.footer = tk.Frame(self.root, bg=p.CARD, padx=self.px(20), pady=self.px(12))
        self.footer.pack(fill="x")
        tk.Label(self.body, text="Looking for sessions…", font=p.SMALL, fg=p.MUTED, bg=p.BG).pack(anchor="w")
        self.root.bind("<Escape>", lambda e: self.root.destroy())
        threading.Thread(target=self.poll, daemon=True).start()
        self.root.after(150, self.drain)

    def px(self, n: float) -> int:
        return int(round(n * self.scale))

    # -- data, gathered off the window's thread

    def poll(self) -> None:
        rows, error, at = None, "", 0.0
        while True:
            try:
                if self.server_wanted.is_set() or time.time() - at > 8:
                    self.server_wanted.clear()
                    try:
                        rows, error = server_sessions(self.ctx, strict=True), ""
                    except BaseException as e:  # noqa: BLE001 - SystemExit from ssh included: show it, go on
                        error = str(e) or e.__class__.__name__
                    at = time.time()
                windows = open_windows(self.ctx)
                skip = {w["sid"] for w in windows} | {r[0] for r in rows or []}
                closed = closed_sessions(self.ctx, skip)
                moving = {}
                for direction, prefix in (("server", "send-"), ("laptop", "back-")):
                    for f in self.ctx.state.glob(f"{prefix}*.pid"):
                        try:
                            alive = desktop.stamp_alive(f.read_text())
                        except OSError:
                            alive = False
                        if alive:
                            moving[f.stem[len(prefix):]] = direction
                self.inbox.put({"windows": windows, "closed": closed, "server": rows, "error": error, "moving": moving})
            except Exception as e:  # noqa: BLE001 - the page keeps looking
                log(f"page: {e!r}")
            time.sleep(1.5)

    def drain(self) -> None:
        with contextlib.suppress(queue.Empty):
            while True:
                self.data = self.inbox.get_nowait()
        if self.data is not None:
            self.render()
        self.root.after(400, self.drain)

    # -- moves

    def project(self, name: str):
        return self.ctx.cfg.project_named(name)

    def start(self, row: dict, direction: str, group=frozenset()) -> None:
        sid = row["sid"]
        if sid in self.moves and not self.moves[sid]["done"]:
            return
        kind = "send" if direction == "server" else "back"
        state = self.ctx.ensure_state()
        logfile = state / f"{kind}-{sid[:8]}.log"
        self.moves[sid] = {"dir": direction, "title": row["title"], "log": logfile, "t": time.time(),
                           "offset": logfile.stat().st_size if logfile.exists() else 0, "done": None,
                           "label": "Starting", "pid_file": state / f"{kind}-{sid[:8]}.pid"}

        def run_it() -> None:
            try:
                if direction == "server" and row.get("pid"):
                    start_send(self.ctx, self.project(row["project"]), sid, str(row["pid"]), now=True, group=group)
                elif direction == "server":
                    desktop.spawn_detached(self.ctx.self_argv("send", sid, "--log", str(logfile)), state)
                else:
                    desktop.spawn_detached(self.ctx.self_argv("back", sid, "--open-tab", "--log", str(logfile)), state)
            except BaseException as e:  # noqa: BLE001 - a refusal shows on the row
                self.moves[sid].update(done="failed", done_at=time.time(), label=str(e) or "could not start")

        threading.Thread(target=run_it, daemon=True).start()
        self.render()

    def move_all(self) -> None:
        windows = self.laptop_rows()
        group = frozenset(w["sid"] for w in windows)
        for w in windows:
            self.start(w, "server", group)

    def follow(self, move: dict, now: float) -> None:
        """Bring a move up to date from its log: its latest step, and whether it has ended."""
        if move["done"]:
            return
        try:
            with open(move["log"], "rb") as f:
                f.seek(move["offset"])
                text = f.read().decode("utf-8", "replace")
        except OSError:
            text = ""
        lines = [s for s in (re.sub(r"^\[\d\d:\d\d:\d\d\] ", "", line).strip() for line in text.splitlines()) if s]
        for line in lines:
            for marker, label in self.STEPS[move["dir"]]:
                if marker in line:
                    move["label"] = label
        alive = False
        with contextlib.suppress(OSError):
            alive = desktop.stamp_alive(move["pid_file"].read_text())
        if any(self.DONE[move["dir"]] in line for line in lines):
            move.update(done="ok", done_at=now)
            self.server_wanted.set()
        elif now - move["t"] > 12 and not alive:
            move.update(done="failed", done_at=now, label=lines[-1] if lines else f"the move did not start; see {move['log']}")
            self.server_wanted.set()

    def moving_prefixes(self) -> set:
        return {sid[:8] for sid in self.moves} | set((self.data or {}).get("moving", {}))

    def laptop_rows(self) -> list:
        busy = self.moving_prefixes()
        return [w for w in (self.data or {}).get("windows", []) if w["sid"][:8] not in busy]

    # -- drawing

    def render(self) -> None:
        d, now, p = self.data, time.time(), Look
        for sid, move in list(self.moves.items()):
            self.follow(move, now)
            if move["done"] == "ok" and now - move["done_at"] > 6:
                del self.moves[sid]
        busy = self.moving_prefixes()
        state_word = {"idle": "idle", "busy": "working", "waiting": "waiting for you"}
        moving = []
        for sid, m in self.moves.items():
            where = "To the server" if m["dir"] == "server" else "To this laptop"
            if m["done"] == "failed":
                moving.append({"key": sid, "title": m["title"], "meta": f"{where}: {m['label']}", "color": p.ERROR,
                               "button": ("Dismiss", lambda s=sid: (self.moves.pop(s, None), self.render()))})
            elif m["done"] == "ok":
                moving.append({"key": sid, "title": m["title"], "color": p.ACCENT, "meta":
                               f"{'Live on the server' if m['dir'] == 'server' else 'Back on this laptop'} after "
                               f"{m['done_at'] - m['t']:.0f} s"})
            else:
                moving.append({"key": sid, "title": m["title"], "color": p.ACCENT,
                               "meta": f"{where} · {m['label']} · {now - m['t']:.0f} s"})
        for sid8, direction in d["moving"].items():
            if not any(sid.startswith(sid8) for sid in self.moves):
                path = next((f for proj in self.ctx.cfg.projects for f in self.ctx.local_dir(proj).glob(f"{sid8}*.jsonl")), None)
                moving.append({"key": sid8, "title": titles.title(path, path.stem) if path else sid8, "color": p.ACCENT,
                               "meta": f"{'To the server' if direction == 'server' else 'To this laptop'} · started from its window"})
        laptop = [{"key": w["sid"], "title": w["title"],
                   "meta": f"{state_word.get(w['status'], w['status'])} · active {ago(w['active'])} · {w['project']} · {w['sid'][:8]}",
                   "button": ("To the server  →", lambda w=w: self.start(w, "server"))} for w in self.laptop_rows()]
        closed = [{"key": c["sid"], "title": c["title"],
                   "meta": f"closed · active {ago(c['active'])} · {c['project']} · {c['sid'][:8]}",
                   "button": ("To the server  →", lambda c=c: self.start(c, "server"))}
                  for c in d["closed"] if c["sid"][:8] not in busy]
        server_rows = [{"key": sid, "title": title, "meta": f"{state} · active {when} · {project} · {sid[:8]}",
                        "button": ("←  To this laptop", lambda r={"sid": sid, "title": title}: self.start(r, "laptop"))}
                       for sid, state, when, title, project in (d["server"] or []) if sid[:8] not in busy]
        server_empty = (f"Cannot reach the server: {d['error']}" if d["error"] else
                        "Looking…" if d["server"] is None else "No sessions on the server.")
        sections = [("moving", "Moving", moving, ""),
                    ("laptop", "Open on this laptop", laptop, "No Claude Code windows open in the configured projects."),
                    ("closed", "Closed on this laptop today", closed, ""),
                    ("server", f"On {self.ctx.cfg.label}", server_rows, server_empty)]
        layout = tuple((name, tuple((r["key"], (r.get("button") or ("",))[0]) for r in rows), bool(rows) or empty)
                       for name, _, rows, empty in sections)
        if layout != self.layout:
            self.layout = layout
            self.draw_sections(sections)
        else:
            for name, _, rows, empty in sections:
                for r in rows:
                    title, meta = self.rows.get((name, r["key"]), (None, None))
                    if title is not None:
                        title.configure(text=short(r["title"]))
                        meta.configure(text=r["meta"], fg=r.get("color", p.MUTED))
                if not rows and (name, "") in self.rows:
                    self.rows[(name, "")][1].configure(text=empty, fg=p.ERROR if d["error"] and name == "server" else p.MUTED)
        self.draw_footer(laptop, d)

    def button(self, parent, text: str, command, primary: bool = False):
        tk, p = self.tk, Look
        if primary:
            return tk.Button(parent, text=text, command=command, font=p.BOLD, fg="white", bg=p.ACCENT,
                             activeforeground="white", activebackground=p.ACCENT_DOWN, relief="flat", bd=0,
                             padx=self.px(14), pady=self.px(6), cursor="hand2")
        frame = tk.Frame(parent, bg=p.BORDER, padx=1, pady=1)
        tk.Button(frame, text=text, command=command, font=p.SMALL, fg=p.TEXT, bg=p.CARD, activebackground="#F2F4F7",
                  relief="flat", bd=0, padx=self.px(10), pady=self.px(3), cursor="hand2").pack()
        return frame

    def draw_sections(self, sections) -> None:
        tk, p = self.tk, Look
        for child in self.body.winfo_children():
            child.destroy()
        self.rows = {}
        for name, header, rows, empty in sections:
            if not rows and not empty:
                continue
            tk.Label(self.body, text=header, font=p.BOLD, fg=p.TEXT, bg=p.BG).pack(anchor="w", pady=(self.px(12), self.px(6)))
            outer = tk.Frame(self.body, bg=p.LINE, padx=1, pady=1)
            outer.pack(fill="x")
            card = tk.Frame(outer, bg=p.CARD)
            card.pack(fill="x")
            if not rows:
                note = tk.Label(card, text=empty, font=p.SMALL, bg=p.CARD, anchor="w", justify="left",
                                fg=p.ERROR if empty.startswith("Cannot") else p.MUTED,
                                padx=self.px(14), pady=self.px(10), wraplength=self.px(540))
                note.pack(fill="x")
                self.rows[(name, "")] = (None, note)
                continue
            for i, r in enumerate(rows):
                if i:
                    tk.Frame(card, bg=p.LINE, height=1).pack(fill="x")
                row = tk.Frame(card, bg=p.CARD, padx=self.px(14), pady=self.px(8))
                row.pack(fill="x")
                if r.get("button"):
                    self.button(row, *r["button"]).pack(side="right", padx=(self.px(12), 0))
                title = tk.Label(row, text=short(r["title"]), font=p.BOLD, fg=p.TEXT, bg=p.CARD, anchor="w")
                title.pack(fill="x")
                meta = tk.Label(row, text=r["meta"], font=p.SMALL, fg=r.get("color", p.MUTED), bg=p.CARD,
                                anchor="w", justify="left", wraplength=self.px(400))
                meta.pack(fill="x")
                self.rows[(name, r["key"])] = (title, meta)

    def draw_footer(self, laptop: list, d: dict) -> None:
        p = Look
        unfollowed = [s8 for s8 in d["moving"] if not any(sid.startswith(s8) for sid in self.moves)]
        in_flight = sum(1 for m in self.moves.values() if not m["done"]) + len(unfollowed)
        if in_flight:
            state = ("moving", f"Moving {in_flight} session{'s' * (in_flight != 1)}. Keep the laptop on until this "
                               "says it is safe.", p.TEXT, None)
        elif laptop:
            state = ("open", f"{len(laptop)} session{'s' * (len(laptop) != 1)} open on this laptop.", p.TEXT,
                     ("Move all to the server", self.move_all))
        elif d["error"]:
            state = ("unknown", "Nothing is open on this laptop, but the server cannot be reached.", p.ERROR, None)
        else:
            state = ("safe", "Nothing is open on this laptop. Safe to shut down.", p.ACCENT,
                     ("Shut down the laptop", desktop.shut_down))
        if state[:2] == (self.foot or (None, None))[:2]:
            return
        self.foot = state
        for child in self.footer.winfo_children():
            child.destroy()
        if state[3]:
            self.button(self.footer, *state[3], primary=state[0] == "open").pack(side="right")
        self.tk.Label(self.footer, text=state[1], font=p.BOLD if state[0] == "safe" else ("Segoe UI", 10), fg=state[2],
                      bg=p.CARD, anchor="w", justify="left", wraplength=self.px(380)).pack(side="left", fill="x")


def cmd_page(ctx: Ctx, args) -> int:
    if not desktop.WINDOWS_UI:
        raise desktop.Unsupported("the sessions page is Windows-only so far; use `sessionlift list`, `send` and `back`")
    import sys
    state = ctx.ensure_state()
    if sys.stdout is None:  # pythonw, from the desktop shortcut: what it says goes to a log
        sys.stdout = sys.stderr = open(state / "page.log", "a", encoding="utf-8", buffering=1)
    if desktop.focus_window(PAGE_TITLE):
        return 0
    from .keeper import ensure_keeper
    ensure_keeper(ctx)
    page = Page(ctx)
    page.root.update_idletasks()
    w, h = page.root.winfo_width(), page.root.winfo_height()
    page.root.geometry(f"+{(page.root.winfo_screenwidth() - w) // 2}+{max(0, (page.root.winfo_screenheight() - h) // 3)}")
    page.root.mainloop()
    return 0


def make_icon(path) -> bool:
    """The page's icon (two arrows, laptop and server), when Pillow is installed."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((12, 12, 244, 244), radius=36, fill=Look.ACCENT)
    d.line((58, 98, 168, 98), fill="white", width=18)
    d.polygon([(198, 98), (158, 60), (158, 136)], fill="white")
    d.line((88, 160, 198, 160), fill="white", width=18)
    d.polygon([(58, 160), (98, 122), (98, 198)], fill="white")
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return True


def cmd_shortcut(ctx: Ctx, args) -> int:
    """A desktop shortcut that opens the page (pythonw: no console window)."""
    if not desktop.WINDOWS_UI:
        raise desktop.Unsupported("the sessions page and its shortcut are Windows-only so far")
    state = ctx.ensure_state()
    icon = state / "sessions.ico"
    has_icon = make_icon(icon)
    argv = ctx.self_argv("page")
    arguments = " ".join(f'"{a}"' if re.search(r"\s", a) else a for a in argv[1:])
    path = desktop.make_shortcut(PAGE_TITLE, desktop.pythonw_exe(), arguments, str(state),
                                 "Move Claude Code sessions between this laptop and your server",
                                 str(icon) if has_icon else "")
    log(f"shortcut: {path}")
    return 0
