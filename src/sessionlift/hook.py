"""The /to-server and /from-server commands inside Claude Code, which involve no model at all.

Each is a user skill (so the command exists and has a description) plus a UserPromptExpansion hook in
~/.claude/settings.json. The hook does the move itself and blocks the command, so Claude is never
called, and the window is left idle with an empty prompt: that is where Ctrl+D (to close it) or
/resume <id> (to switch it to the session brought back) then gets typed. A window opened before the
hook was installed still runs the skill through Claude until it is restarted.

The hook event is read from stdin: command_name, command_args, session_id. CLAUDE_PID in the hook's
environment names the window's process. Both are Claude Code behaviour, not a documented API.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from . import desktop
from .context import log

COMMANDS = ("to-server", "from-server")
MARK = "<!-- written by `sessionlift install`; `sessionlift install --uninstall` removes it -->"


def decision(reason: str) -> str:
    return json.dumps({"decision": "block", "reason": reason})


def run_hook(stdin: bytes, load_ctx, answer) -> int:
    """Handle one hook event. `answer` receives the JSON the hook prints; everything else goes to the
    log, because the hook's stdout is Claude Code's."""
    try:
        event = json.loads(stdin or b"{}")
    except ValueError:
        return 0
    name = event.get("command_name", "")
    if name not in COMMANDS:
        return 0

    def block(reason: str) -> int:
        log(f"/{name}: {reason}")
        answer(decision(reason))
        return 0

    if os.environ.get("SESSIONLIFT_ROLE") == "server":
        return block("This session runs on the server. /to-server and /from-server work from the laptop.")
    if not desktop.WINDOWS_UI:
        return block("sessionlift's slash commands work on Windows only so far. Here: exit Claude Code, then run "
                     "`sessionlift send <id>`; to bring one back, `sessionlift back <id>`, then `claude --resume <id>`.")
    try:
        ctx = load_ctx()
    except SystemExit as e:
        return block(f"/{name} did not run: {e}")
    from .keeper import ensure_keeper
    from .moves import bring_back, server_sessions, start_send, switch_window
    from .sessions import find_session
    ensure_keeper(ctx)  # for the next move, when it is not running: this one goes without it
    sid, pid = event.get("session_id", ""), os.environ.get("CLAUDE_PID", "")
    if not pid.isdigit():
        return block("Claude Code did not say which window this is (no CLAUDE_PID), so nothing moved. "
                     "Exit, then run `sessionlift send <id>`.")
    try:
        if name == "to-server":
            try:
                project, sid = find_session(ctx, sid)
            except SystemExit:
                return block("Nothing to move yet: this session has no conversation in a configured project.")
            title, logfile = start_send(ctx, project, sid, pid, hook_pid=os.getpid())
            return block(f"Moving \"{title}\" to {ctx.cfg.label}. This window closes in a moment and the session "
                         f"continues in the Claude app. Log: {logfile}")
        want = (event.get("command_args") or "").strip()
        if not want:
            rows = server_sessions(ctx)
            if not rows:
                return block(f"No sessions on {ctx.cfg.label}.")
            from .page import pick_session
            want = pick_session(ctx, rows)
            if not want:
                return block("Nothing brought back.")
        _, back, title, note = bring_back(ctx, want)
        switch_window(ctx, pid, back, note, after_hook=os.getpid())
        return block(f"Back from {ctx.cfg.label}. This window switches to \"{title}\" in a moment.")
    except SystemExit as e:
        return block(f"/{name} did not run: {e}")


def cmd_hook(load_ctx, state_dir: Path) -> int:
    answer_stream = sys.stdout
    state_dir.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(state_dir / "hook.log", "a", encoding="utf-8", buffering=1)

    def answer(text: str) -> None:
        answer_stream.write(text)
        answer_stream.flush()

    return run_hook(sys.stdin.buffer.read(), load_ctx, answer)


# ---------------------------------------------------------------- installing the commands

def command_line(config_path: Path) -> str:
    """How Claude Code's hook shell (bash, also on Windows) runs this tool: this interpreter, by an
    absolute path with forward slashes."""
    exe = Path(sys.executable).as_posix()
    return f'"{exe}" -m sessionlift --config "{Path(config_path).as_posix()}"'


def skills(cmd: str) -> dict:
    to_server = f"""---
name: to-server
description: Move this Claude Code session to your own server right now, exactly as it is; it keeps running there with Remote Control, so you can continue it from the Claude app with the laptop shut. Run on the laptop.
disable-model-invocation: true
effort: low
allowed-tools: Bash({cmd} send)
---
{MARK}

!`{cmd} send`

sessionlift has started moving this session to the server. Do not run anything. Reply with exactly
one short line and nothing else: "Moving to the server: this window closes by itself, and the session
continues in the Claude app as <the title the output names>."
"""
    from_server = f"""---
name: from-server
description: Show the Claude Code sessions on your server and bring the one you pick back to this laptop, continuing it in this window. Run on the laptop.
disable-model-invocation: true
effort: low
argument-hint: "[session id or its first characters]"
allowed-tools: Bash({cmd} *)
---
{MARK}

!`{cmd} list`

Session named, if any: "$ARGUMENTS"

1. If a session was named, go straight to step 3 with it.
2. If the list says there are no sessions, say so in one line and stop. Otherwise ask which one with
   AskUserQuestion, at once and without any other text: one option per session, newest first, at
   most four. Label: its title cut to five words. Description: running or idle, how long ago it was
   active, and the first 8 characters of its id. With only one session, add a second option,
   "Cancel". If cancelled, stop.
3. Run `{cmd} back <the full id from the list>`.
4. If it succeeded, reply with exactly one short line: "Back on the laptop: this window switches to
   it in a moment." Write nothing after it. If it failed, show the error.
"""
    return {"to-server": to_server, "from-server": from_server}


def install(claude: Path, config_path: Path, uninstall: bool = False) -> list:
    """Write (or remove) the two user skills and the two hooks in ~/.claude/settings.json. Returns
    what changed, for the person running it. settings.json is backed up once, before the first edit."""
    cmd = command_line(config_path)
    changed = []
    for name, text in skills(cmd).items():
        folder = claude / "skills" / name
        path = folder / "SKILL.md"
        if uninstall:
            if path.exists() and MARK in path.read_text(encoding="utf-8"):
                shutil.rmtree(folder)
                changed.append(f"removed {path}")
        else:
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            changed.append(f"wrote {path}")
    settings_path = claude / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except ValueError as e:
        raise SystemExit(f"{settings_path} is not valid JSON ({e}); fix it, then run this again") from e
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict):
        raise SystemExit(f"{settings_path} does not look like Claude Code's settings; nothing changed there")
    before = json.dumps(settings, sort_keys=True)
    hooks = settings.setdefault("hooks", {})
    groups = [g for g in hooks.get("UserPromptExpansion", [])
              if not (isinstance(g, dict) and g.get("matcher") in COMMANDS)]
    if not uninstall:
        groups += [{"matcher": "to-server", "hooks": [{"type": "command", "command": f"{cmd} hook", "timeout": 120}]},
                   {"matcher": "from-server", "hooks": [{"type": "command", "command": f"{cmd} hook", "timeout": 600}]}]
    if groups:
        hooks["UserPromptExpansion"] = groups
    else:
        hooks.pop("UserPromptExpansion", None)
    if not hooks:
        settings.pop("hooks", None)
    if json.dumps(settings, sort_keys=True) == before:
        changed.append(f"{settings_path} already as it should be")
        return changed
    backup = settings_path.with_name("settings.json.before-sessionlift")
    if settings_path.exists() and not backup.exists():
        shutil.copy2(settings_path, backup)
        changed.append(f"backed up {settings_path} to {backup.name}")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    changed.append(f"{'removed the hooks from' if uninstall else 'added the hooks to'} {settings_path}")
    return changed
