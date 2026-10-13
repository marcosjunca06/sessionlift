"""sessionlift: move a live Claude Code session between this laptop and your own server.

    sessionlift init --host H --project LOCAL=REMOTE   write the config file
    sessionlift install [--uninstall]                  Windows: the /to-server and /from-server commands
    sessionlift setup [--provision [STEP ...]]         prepare the server: code, ~/.claude, projects
    sessionlift login [--code C]                       sign Claude Code in on the server
    sessionlift send [SESSION] [--now]                 continue a laptop session on the server
    sessionlift back [SESSION] [--open-tab]            bring a session from the server back here
    sessionlift list | status                          the sessions on the server; what runs there
    sessionlift page | shortcut                        Windows: the sessions page, and its desktop shortcut
    sessionlift keeper [--install|--stop|--uninstall]  the helper that keeps moves fast
    sessionlift sync-memory | open | config            memory both ways; start/open the server; the config

In Claude Code, /to-server and /from-server do the whole move without calling the model. A session lives
on one machine at a time; on the server it runs in tmux with Remote Control, so the Claude app can drive
it while the laptop is shut. Without SESSION, send and back list the sessions and ask; they never pick
one on their own.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import time
from pathlib import Path

from . import __version__, paths

PROVISION_STEPS = ("node", "docker", "swap", "desktop", "browser")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sessionlift", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="Unofficial; not affiliated with Anthropic.")
    parser.add_argument("--config", help=f"the config file (default {paths.config_path()})")
    parser.add_argument("--version", action="version", version=f"sessionlift {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("init", help="write the config file")
    p.add_argument("--host", required=True, help="the server: an ssh host, or a Host alias in ~/.ssh/config")
    p.add_argument("--project", action="append", required=True, metavar="LOCAL=REMOTE",
                   help="a project: its directory here and its checkout on the server (repeat for more)")
    p.add_argument("--user", default="")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--identity-file", default="")
    p.add_argument("--name", default="", help="how messages and the page name the server")
    p.add_argument("--gcp-instance", default="", help="a GCP instance sessionlift may start")
    p.add_argument("--gcp-zone", default="")
    p.add_argument("--gcp-project", default="")
    p.add_argument("--gcloud-configuration", default="")
    p.add_argument("--firewall-rule", default="", help="a GCP firewall rule to keep open for this laptop's address")
    p.add_argument("--force", action="store_true", help="replace an existing config file")

    p = sub.add_parser("install", help="add the /to-server and /from-server commands to Claude Code")
    p.add_argument("--uninstall", action="store_true", help="remove them again")

    p = sub.add_parser("send", help="continue a laptop session on the server")
    p.add_argument("session", nargs="?",
                   help="its id or first characters (default: the session this runs in, else it lists and asks)")
    p.add_argument("--now", action="store_true", help="copy now, even from inside the session being sent")
    p.add_argument("--after-pid", help=argparse.SUPPRESS)
    p.add_argument("--type-exit-after", type=int, help=argparse.SUPPRESS)
    p.add_argument("--after-hook", type=int, help=argparse.SUPPRESS)
    p.add_argument("--close-now", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--group", help=argparse.SUPPRESS)
    p.add_argument("--log", help=argparse.SUPPRESS)

    p = sub.add_parser("back", help="bring a session from the server back to this laptop")
    p.add_argument("session", nargs="?", help="its id or first characters (default: it lists them and asks)")
    p.add_argument("--no-pull", action="store_true", help="leave the laptop checkout alone")
    p.add_argument("--no-switch", action="store_true",
                   help="inside a Claude Code session: do not switch its window to the session brought back")
    p.add_argument("--open-tab", action="store_true", help="Windows: continue it in a new terminal tab")
    p.add_argument("--log", help=argparse.SUPPRESS)

    sub.add_parser("list", help="the sessions on the server")
    sub.add_parser("status", help="what runs on the server")
    p = sub.add_parser("setup", help="prepare the server: sessionlift's code, ~/.claude, the projects")
    p.add_argument("--provision", nargs="*", choices=PROVISION_STEPS, metavar="STEP",
                   help=f"also run server/provision.sh in the background, with these optional parts: {', '.join(PROVISION_STEPS)}")
    sub.add_parser("sync-memory", help="two-way sync of each project's auto memory")
    p = sub.add_parser("login", help="sign Claude Code in on the server")
    p.add_argument("--code", help="the code the sign-in page shows")
    p.add_argument("--email", default="", help="the account to sign in with")
    sub.add_parser("open", help="start the server and let this laptop's address through (provider gcp)")
    sub.add_parser("config", help="where the config is, and what it says")
    sub.add_parser("page", help="Windows: one window for every session here and on the server")
    sub.add_parser("shortcut", help='Windows: put the page on the desktop as "Claude sessions"')
    p = sub.add_parser("keeper", help="the background helper: one connection held open, sessions copied ahead")
    what = p.add_mutually_exclusive_group()
    what.add_argument("--ensure", action="store_true", help="start it unless it runs")
    what.add_argument("--install", action="store_true", help="Windows: start it at every logon, and now")
    what.add_argument("--stop", action="store_true", help="stop the running one")
    what.add_argument("--uninstall", action="store_true", help="remove the logon task and stop it")
    sub.add_parser("hook", help="Claude Code's hook for /to-server and /from-server (reads the event on stdin)")

    p = sub.add_parser("_type")  # internal: type into a Claude Code window (Windows); no help, so not listed
    p.add_argument("--pid", type=int, required=True)
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--text", help="a slash command to type and send")
    what.add_argument("--close", action="store_true", help="close the window (Ctrl+D twice)")
    p.add_argument("--now", action="store_true")
    p.add_argument("--interrupted-file")
    p.add_argument("--draft-file")
    p.add_argument("--after-turn")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--after-exit-of", type=int)
    p.add_argument("--exit-question")
    p.add_argument("--ready-file")
    p.add_argument("--then-file")
    p.add_argument("--then-after", type=float, default=3.0)
    p.add_argument("--log")
    return parser


def cmd_init(path: Path, args) -> int:
    from .config import ConfigError, parse, template
    if path.exists() and not args.force:
        raise SystemExit(f"{path} exists; edit it, or run init again with --force to replace it")
    projects = []
    for spec in args.project:
        local, sep, remote = spec.rpartition("=")
        if not sep or not local or not remote:
            raise SystemExit(f"--project takes LOCAL=REMOTE, not {spec!r}")
        projects.append((local, remote))
    provider = {}
    if args.gcp_instance or args.gcp_zone:
        provider = {"kind": "gcp", "instance": args.gcp_instance, "zone": args.gcp_zone, "project": args.gcp_project,
                    "gcloud_configuration": args.gcloud_configuration, "firewall_rule": args.firewall_rule}
    text = template(args.host, projects, user=args.user, port=args.port, identity_file=args.identity_file,
                    name=args.name, provider=provider)
    try:
        parse(text, path)
    except ConfigError as e:
        raise SystemExit(f"not written: {e}") from e
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {path}\nNext: sessionlift setup, sessionlift login, sessionlift install (see the README).")
    return 0


def pick(options: list, question: str, ask=None, interactive: bool = None) -> str:
    """The session to act on, asked for in the terminal: `options` is [(session id, one line about it)],
    newest first. Always asks, even when there is only one, and never picks on its own: an answer is
    a number from the list or a session id (or its first characters). SystemExit when cancelled, or
    when there is no terminal to ask in."""
    from .context import out
    for i, (_, line) in enumerate(options, 1):
        out(f"  {i}. {line}")
    if interactive is None:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    if not interactive:
        raise SystemExit("say which session: give its id (or its first characters)")
    try:
        answer = (ask or input)(f"{question} (a number or an id; Enter cancels): ").strip().lower()
    except EOFError:
        answer = ""
    if not answer:
        raise SystemExit("cancelled; nothing moved")
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1][0]
    if re.fullmatch(r"[0-9a-f-]{4,36}", answer):
        return answer
    raise SystemExit(f"not a number from the list or a session id: {answer!r}; nothing moved")


def _laptop_choice(ctx) -> str:
    """send without SESSION, outside a Claude Code session: the laptop's recent sessions that no
    window holds, to pick from."""
    from .context import out
    from .moves import ago
    from .sessions import closed_sessions, open_windows
    windows = open_windows(ctx)
    closed = closed_sessions(ctx, {w["sid"] for w in windows}, limit=10, hours=7 * 24)
    if windows:
        out("Open in a window here (move those with /to-server in the window, or from the page):")
        for w in windows:
            out(f"  {w['sid'][:8]}  {w['title']}")
    if not closed:
        raise SystemExit("no closed laptop session from the last 7 days in the configured projects; "
                         "name one: sessionlift send <id>")
    out("Closed on this laptop, newest first:")
    return pick([(c["sid"], f"{c['title']} | {c['project']} | active {ago(time.time() - c['active'])} | "
                            f"{c['sid'][:8]}") for c in closed], "Send which one to the server?")


def _server_choice(ctx) -> str:
    """back without SESSION: the server's sessions, to pick from."""
    from .context import out
    from .moves import server_sessions
    rows = server_sessions(ctx, strict=True)
    if not rows:
        raise SystemExit(f"no sessions on {ctx.cfg.label}")
    out(f"On {ctx.cfg.label}, newest first:")
    return pick([(sid, f"{title} | {state} | {project} | active {when} | {sid[:8]}")
                 for sid, state, when, title, project in rows], "Bring which one back?")


def cmd_send(ctx, args) -> int:
    from . import desktop
    from .context import out
    from .moves import send_after_exit, send_closed, start_send
    from .sessions import find_session, resolve_session, transcript_path
    from .transcripts import session_title
    found = resolve_session(ctx, args.session)
    project, sid = found if found else find_session(ctx, _laptop_choice(ctx))
    transcript = transcript_path(ctx, project, sid)
    if not transcript.exists():
        raise SystemExit(f"no transcript {transcript}")
    title = session_title(transcript.read_bytes(), sid)
    group = frozenset(s for s in (args.group or "").split(",") if s)
    if args.after_pid:
        return send_after_exit(ctx, project, sid, title, int(args.after_pid), args.type_exit_after, args.after_hook,
                               args.close_now, group)
    pid = (os.environ.get("CLAUDE_PID")
           if os.environ.get("CLAUDE_CODE_SESSION_ID") == sid and not args.now else None)
    if pid:
        title, logfile = start_send(ctx, project, sid, pid)
        how = ("When this reply ends, this window closes by itself" if desktop.WINDOWS_UI else
               "Exit Claude Code now (Ctrl+D twice)")
        out(f"\nMoving \"{title}\" to {ctx.cfg.label}. {how}, and the session continues there in the Claude app "
            f"seconds later.\nProgress: {logfile}")
        return 0
    return send_closed(ctx, project, sid, title, force=args.now)


def cmd_back(ctx, args) -> int:
    from . import desktop
    from .context import out
    from .moves import bring_back, open_tab, pid_marker, resume_hint, switch_window
    from .sessions import find_session, transcript_path
    if args.open_tab and not desktop.WINDOWS_UI:
        raise SystemExit("--open-tab works on Windows only so far; leave it out and run the command it prints")
    session = args.session or _server_choice(ctx)
    with pid_marker(ctx.ensure_state() / f"back-{session[:8]}.pid"):  # the page follows a move by it
        project, sid, title, note = bring_back(ctx, session, pull=not args.no_pull)
        if args.open_tab:
            open_tab(ctx, project, sid, title, note)
            return 0
        here, pid = os.environ.get("CLAUDE_CODE_SESSION_ID"), os.environ.get("CLAUDE_PID")
        current = None
        if here:
            with contextlib.suppress(SystemExit):
                current = transcript_path(ctx, *find_session(ctx, here))
        if desktop.WINDOWS_UI and pid and current and current.exists() and not args.no_switch:
            switch_window(ctx, pid, sid, note, after_turn=current)
            out(f"\nBack on the laptop. When this reply ends, this window switches to it by typing /resume {sid} "
                f"(if it does not, type that)." + (" Then it is told what the move stopped." if note else ""))
        else:
            out(f"\nContinue here: {resume_hint(ctx, project, sid)}" + (f"\nThen tell it: {note}" if note else ""))
        return 0


def cmd_sync_memory(ctx, args) -> int:
    from .memory import sync_memory
    ctx.server.connect()
    for project in ctx.cfg.projects:
        sync_memory(ctx, project)
    return 0


def cmd_config(ctx, args) -> int:
    cfg = ctx.cfg
    print(f"config:   {cfg.path}\nserver:   {cfg.host}" + (f" ({cfg.name})" if cfg.name else "")
          + f"\nprovider: {cfg.provider.kind}\nstate:    {ctx.state}\nbypass permissions on the server: "
          + ("yes" if cfg.bypass_permissions else "no"))
    for p in cfg.projects:
        print(f"project {p.name}: {p.local} -> {p.remote} (git sync {'on' if p.git_sync else 'off'}, "
              f"memory sync {'on' if p.sync_memory else 'off'})")
    return 0


def main(argv: list = None) -> int:
    args = _parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if getattr(args, "log", None):
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(args.log, "a", encoding="utf-8", buffering=1)
    # Absolute: helpers and hooks run it from other directories.
    config_path = Path(args.config).expanduser().resolve() if args.config else paths.config_path()
    if args.command == "init":
        return cmd_init(config_path, args)
    server_side = os.environ.get("SESSIONLIFT_ROLE") == "server"
    if args.command == "hook":
        from .hook import cmd_hook

        def load_ctx():
            from . import config
            from .context import Ctx
            return Ctx(config.load(config_path))

        if server_side:
            state = Path.home() / ".local" / "state" / "sessionlift"
        else:
            from . import config
            try:
                state = config.load(config_path).state_dir
            except SystemExit:  # the hook says what is wrong with the config itself
                state = paths.default_state_dir()
        return cmd_hook(load_ctx, state)
    if server_side:
        print("This is the server's copy of sessionlift: run sessionlift on the laptop.", file=sys.stderr)
        return 2
    from . import config
    from .context import Ctx
    ctx = Ctx(config.load(config_path))
    if args.command == "install":
        from . import desktop
        from .hook import install
        if not args.uninstall and not desktop.WINDOWS_UI:
            raise SystemExit("/to-server and /from-server work on Windows laptops only so far; use "
                             "`sessionlift send` and `sessionlift back` here (see the README)")
        for line in install(ctx.claude, config_path, uninstall=args.uninstall):
            print(line)
        if not args.uninstall:
            print("Restart open Claude Code windows to get the hooks; /to-server and /from-server then move sessions "
                  "without calling the model.")
        return 0
    if args.command == "_type":
        from .typer import type_into
        return type_into(ctx, args)
    if args.command == "keeper":
        from .keeper import cmd_keeper
        return cmd_keeper(ctx, args)
    if args.command in ("page", "shortcut"):
        from . import page
        return (page.cmd_page if args.command == "page" else page.cmd_shortcut)(ctx, args)
    if args.command in ("list", "status", "setup", "login", "open"):
        from . import serverctl
        return getattr(serverctl, f"cmd_{args.command}")(ctx, args)
    commands = {"send": cmd_send, "back": cmd_back, "sync-memory": cmd_sync_memory, "config": cmd_config}
    return commands[args.command](ctx, args)
