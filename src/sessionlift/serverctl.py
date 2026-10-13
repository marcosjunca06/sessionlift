"""Setting the server up and looking at it: `setup`, `status`, `login`, `open`, `list`."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import paths
from .config import Config
from .context import Ctx, log, out
from .gitsync import run
from .memory import sync_memory
from .moves import server_sessions
from .providers import provider_for
from .transcripts import tar_files

BROWSER_MCP_NAME = "server-chrome"
WINDOWS_COMMAND = re.compile(r"(\.(exe|cmd|bat|ps1)$|^(cmd|powershell|pwsh)$|^[A-Za-z]:[\\/])", re.I)
WINDOWS_PATH = re.compile(r"(^|[\s\"'=])[A-Za-z]:[\\/]")
OWN_HOOK = re.compile(r"-m sessionlift\b.*\bhook\s*$")
SERVER_HOOK = '"$HOME/.local/bin/sessionlift" hook'


def _windows_hook(command: str) -> bool:
    """A hook command that only a Windows machine can run: a Windows shell or program first, or a
    path with a drive letter anywhere."""
    words = command.strip().split()
    first = words[0].strip("\"'") if words else ""
    return bool(WINDOWS_COMMAND.search(first) or WINDOWS_PATH.search(command))


def adapted_settings(settings: dict, cfg: Config, dropped: list = None) -> dict:
    """The laptop's ~/.claude/settings.json as the server should have it: no status line or voice
    settings (they call laptop programs, and the server has no microphone), Remote Control on at start,
    bypass mode only when the config asks for it (also when the laptop itself runs in it), sessionlift's
    own hooks pointing at the server's copy (which tells a session there that the commands run from the
    laptop), and no hooks that run Windows programs (their commands go into `dropped`)."""
    s = json.loads(json.dumps(settings))
    for key in ("statusLine", "voice", "voiceEnabled"):
        s.pop(key, None)
    s["remoteControlAtStartup"] = True
    permissions = s.get("permissions") if isinstance(s.get("permissions"), dict) else None
    if cfg.bypass_permissions:
        s.setdefault("permissions", {})["defaultMode"] = "bypassPermissions"
        s["skipDangerousModePermissionPrompt"] = True
    else:
        if permissions is not None and permissions.get("defaultMode") == "bypassPermissions":
            del permissions["defaultMode"]
        s.pop("skipDangerousModePermissionPrompt", None)
    hooks = s.get("hooks") if isinstance(s.get("hooks"), dict) else {}
    for event in list(hooks):
        groups = [g for g in hooks[event] if isinstance(g, dict)] if isinstance(hooks[event], list) else []
        for group in groups:
            kept = []
            for hook in group.get("hooks") or []:
                command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
                if OWN_HOOK.search(command):
                    hook["command"] = SERVER_HOOK
                elif _windows_hook(command):
                    if dropped is not None:
                        dropped.append(command)
                    continue
                kept.append(hook)
            group["hooks"] = kept
        hooks[event] = [g for g in groups if g["hooks"]]
        if not hooks[event]:
            del hooks[event]
    if "hooks" in s and not hooks:
        del s["hooks"]
    return s


def _windows_only(server: dict) -> bool:
    return bool(WINDOWS_COMMAND.search(str(server.get("command", ""))))


def claude_json_patch(ctx: Ctx, server_home: str) -> dict:
    """What `setup` merges into the server's ~/.claude.json: the laptop's MCP servers (user-level, and
    each project's under its server path) minus those that start Windows programs, the browser MCP
    server when configured, onboarding done and every project's folder trusted."""
    path = paths.claude_json()
    d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    skipped = []

    def usable(servers: dict) -> dict:
        kept = {}
        for name, server in (servers or {}).items():
            if _windows_only(server):
                skipped.append(name)
            else:
                kept[name] = server
        return kept

    servers = usable(d.get("mcpServers")) if ctx.cfg.copy_mcp_servers else {}
    if ctx.cfg.browser_mcp:
        servers[BROWSER_MCP_NAME] = {"type": "stdio", "command": "npx", "args": [
            "-y", "@playwright/mcp@0.0.83", "--cdp-endpoint", "http://127.0.0.1:9222",
            # By default Playwright MCP writes snapshots into the session's working directory, which
            # leaves the checkout dirty and makes every move commit them.
            "--output-dir", f"{server_home}/.cache/playwright-mcp"]}
    projects = {}
    for project in ctx.cfg.projects:
        mine = next((v for k, v in (d.get("projects") or {}).items() if _same(k, project.local)), {})
        projects[project.remote] = {"hasTrustDialogAccepted": True,
                                    "mcpServers": usable(mine.get("mcpServers")) if ctx.cfg.copy_mcp_servers else {}}
    if skipped:
        log(f"MCP servers left on the laptop (they start Windows programs): {', '.join(sorted(set(skipped)))}")
    return {"hasCompletedOnboarding": True, "mcpServers": servers, "projects": projects}


def _same(a: str, b: str) -> bool:
    norm = lambda p: p.replace("\\", "/").rstrip("/").lower()  # noqa: E731
    return norm(a) == norm(b)


def rc_unit(ctx: Ctx, user: str, home: str) -> str:
    mode = " --permission-mode bypassPermissions" if ctx.cfg.bypass_permissions else ""
    chrome = " --chrome" if "--chrome" in ctx.cfg.server_args else ""
    loop = (f'export PATH="{home}/.local/bin:$PATH"; while true; do claude remote-control --spawn worktree{mode}{chrome}; '
            'echo "[remote-control exited $?; restarting in 30 s]"; sleep 30; done')
    return f"""[Unit]
Description=Claude Code Remote Control server (sessionlift): start sessions on {ctx.cfg.label} from the Claude app
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
User={user}
WorkingDirectory={ctx.cfg.projects[0].remote}
ExecStart=/usr/bin/tmux -L sessionlift-rc new-session -d -s rc -x 200 -y 50 bash -lc '{loop}'
ExecStop=/usr/bin/tmux -L sessionlift-rc kill-server
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""


def provision_script() -> bytes:
    """server/provision.sh, from the installed package or from a checkout."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "provision.sh", here.parents[1] / "server" / "provision.sh"):
        if candidate.exists():
            return candidate.read_bytes()
    raise SystemExit("provision.sh is missing from this installation")


def _is_ignored(repo: str, rel: str) -> bool:
    return run(["git", "-C", repo, "check-ignore", "-q", rel]).returncode == 0


def _is_tracked(repo: str, rel: str) -> bool:
    return run(["git", "-C", repo, "ls-files", "--error-unmatch", rel]).returncode == 0


def cmd_setup(ctx: Ctx, args) -> int:
    t = ctx.server
    t.connect()
    if hasattr(t, "upload_code"):
        t.upload_code()
    info = t.call("ping").value("reaching sessionlift on the server")
    home, user = info["home"], info["user"]
    log(f"{ctx.cfg.label}: {user}@{home}, Python {info['python']}, sessionlift {info['version']}")
    for project in ctx.cfg.projects:
        r = t.call("clone", {"url": project.clone_url, "path": project.remote}, timeout=1800)
        if r.ok:
            log(f"{project.name}: {'cloned into' if r.json().get('cloned') else 'checkout present at'} {project.remote}")
        else:
            log(f"{project.name}: {r.message()}")
            continue
        files = {}
        in_git = (Path(project.local) / ".git").exists()
        for rel in project.copy_files:
            path = Path(project.local) / rel
            if not path.is_file():
                log(f"{project.name}: {rel} does not exist here; skipped")
            elif in_git and _is_tracked(project.local, rel):
                log(f"{project.name}: {rel} is tracked by git, so it travels with the repository; skipped")
            elif in_git and not _is_ignored(project.local, rel):
                log(f"{project.name}: {rel} is not ignored by git, so on the server it would show as untracked "
                    f"and a move could commit it; add it to .gitignore first. Skipped")
            else:
                files[rel.replace("\\", "/")] = path
        if files:
            t.call("put_files", {"dir": project.remote, "private": True}, tar_files(files)).value("copying files")
            log(f"{project.name}: copied {', '.join(files)}")
        exclude = Path(project.local) / ".git" / "info" / "exclude"
        if exclude.is_file():
            # Local excludes never travel with a clone; without them, worktrees and the like show up
            # as untracked on the server and every move commits them.
            t.call("put_files", {"dir": f"{project.remote}/.git/info"}, tar_files({"exclude": exclude})).value(
                "copying .git/info/exclude")
    claude_files = {}
    if ctx.cfg.copy_claude_md:
        laptop_md = ctx.claude / "CLAUDE.md"
        text = laptop_md.read_bytes() if laptop_md.exists() else b""
        if ctx.cfg.server_note:
            text += b"\n\n" + ctx.cfg.server_note.encode() + b"\n"
        if text:
            claude_files["CLAUDE.md"] = text
    if ctx.cfg.copy_settings and (ctx.claude / "settings.json").exists():
        settings = json.loads((ctx.claude / "settings.json").read_text(encoding="utf-8"))
        dropped = []
        claude_files["settings.json"] = json.dumps(adapted_settings(settings, ctx.cfg, dropped), indent=2).encode()
        if dropped:
            log(f"hooks left on the laptop (they run Windows programs): {'; '.join(c[:80] for c in dropped)}")
    if ctx.cfg.copy_skills and (ctx.claude / "skills").is_dir():
        for p in (ctx.claude / "skills").rglob("*"):
            rel = p.relative_to(ctx.claude).as_posix()
            if p.is_file() and not rel.startswith("skills/synced/"):  # claude.ai syncs those itself
                claude_files[rel] = p
    if claude_files:
        t.call("put_files", {"dir": ".claude"}, tar_files(claude_files)).value("copying ~/.claude")
    t.call("merge_claude_json", {"patch": claude_json_patch(ctx, home)}).value("updating ~/.claude.json")
    top = sorted(n for n in claude_files if "/" not in n)
    skills = sum(1 for n in claude_files if n.startswith("skills/"))
    log(f"~/.claude: {', '.join(top) or 'no files'}, {skills} skill file(s), MCP servers")
    for project in ctx.cfg.projects:
        try:
            sync_memory(ctx, project)
        except SystemExit as e:
            log(f"memory ({project.name}): {e}")
    if ctx.cfg.remote_control_server:
        logged_in = t.call("keeper_status", {"projects": []}).value("checking the sign-in")
        r = t.call("install_rc", {"unit": rc_unit(ctx, user, home), "enable": bool(logged_in.get("logged_in"))})
        if not r.ok:
            log(f"Remote Control server: {r.message()}")
        elif logged_in.get("logged_in"):
            log(f"Remote Control server: {r.json().get('active')} (start new sessions on the server from the Claude app)")
        else:
            log("Remote Control server installed; not signed in on the server yet: sessionlift login, then setup again")
    if args.provision is not None:
        r = t.call("provision", {"flags": [f"--{f}" for f in args.provision]}, provision_script())
        log(f"provisioning in the background on the server; follow it with: ssh {ctx.cfg.host} tail -f "
            f"{r.value('provisioning').get('log')}")
    return 0


def cmd_status(ctx: Ctx, args) -> int:
    ctx.server.connect()
    answer = ctx.server.call("status", {"projects": [ctx.remote_dir(p) for p in ctx.cfg.projects],
                                        "repos": [p.remote for p in ctx.cfg.projects],
                                        "remote_control_server": ctx.cfg.remote_control_server,
                                        "browser": ctx.cfg.browser_mcp}, timeout=180).value("status")
    out(answer["text"])
    return 0


def cmd_login(ctx: Ctx, args) -> int:
    ctx.server.connect()
    if args.code:
        answer = ctx.server.call("login_code", {"code": args.code}, timeout=120).value("signing in")
        if answer.get("logged_in"):
            log("signed in on the server")
            return 0
        out(answer.get("screen", ""))
        return 1
    answer = ctx.server.call("login_start", {"email": args.email or ""}, timeout=120).value("starting the sign-in")
    if answer.get("logged_in"):
        log("already signed in on the server")
        return 0
    if answer.get("url"):
        out(f"Open this, approve, then: sessionlift login --code <the code it shows>\n\n{answer['url']}")
        return 0
    out(answer.get("screen", ""))
    return 1


def cmd_open(ctx: Ctx, args) -> int:
    provider_for(ctx.cfg).open()
    log("reachable" if ctx.server.reachable() else "still unreachable")
    return 0


def cmd_list(ctx: Ctx, args) -> int:
    rows = server_sessions(ctx, strict=True)
    if not rows:
        out(f"No sessions on {ctx.cfg.label}.")
        return 0
    out(f"Sessions on {ctx.cfg.label}, newest first (id | state | last active | project | title):")
    for sid, state, ago, title, project in rows:
        out(f"{sid} | {state} | {ago} | {project} | {title}")
    return 0
