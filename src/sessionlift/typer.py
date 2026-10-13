"""Typing into a Claude Code window on the laptop (Windows consoles only): closing it with Ctrl+D twice,
switching it to a session with /resume, telling a session what a move stopped.

Rules each learned from a failure:

- Never type into a running turn. Esc on a running turn puts the last prompt back in the input box;
  an /exit typed then landed behind the command that started the move and ran that command again,
  over and over.
- Close with Ctrl+D twice, never a typed /exit: a typed /exit goes through the command menu, and in a
  session with many skills Enter picked a different command from it.
- Ctrl+D does not close a window whose prompt holds text, and a greyed hint in an empty prompt reads
  exactly like typed text. One Backspace tells them apart: typed text loses its last letter.
- A second Esc soon after the first opens the rewind menu: wait between them.
- Type a slash command only into an empty prompt, and press Enter only if the box then reads exactly
  that command.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import desktop
from .context import Ctx, log
from .screen import END_TURN, EXIT_AND_STOP, EXIT_QUESTION, prompt_text, stopped_tasks
from .sessions import window_status


def wait_turn_end(pid: int, transcript: Path, offset: int, timeout_s: float = 3600) -> bool:
    """Wait until the turn running now has ended: an end_turn line past `offset` in its transcript,
    then a moment without writes. False if the session exited first."""
    if transcript is not None:
        deadline = time.time() + timeout_s
        while True:
            with open(transcript, "rb") as f:
                f.seek(offset)
                if END_TURN in f.read():
                    break
            if desktop.wait_for_pid(pid, 0) or time.time() > deadline:
                return False
            time.sleep(0.3)
        size = None
        while size != transcript.stat().st_size:
            size = transcript.stat().st_size
            time.sleep(1.0)
    return not desktop.wait_for_pid(pid, 0)


def answer_exit_question(pid: int, wait_s: float = 6):
    """After Ctrl+D: if Claude Code asks because background work is running, answer "Exit and stop
    tasks" and return the tasks it named (the session on the other side is told, and picks them up).
    None if it did not ask."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if desktop.wait_for_pid(pid, 0):
            return None
        screen = desktop.console_text(pid)
        if EXIT_QUESTION in screen:
            tasks = stopped_tasks(screen)
            if EXIT_AND_STOP.search(screen):
                desktop.console_keys(pid, ["\r"])
            else:
                log("Claude Code's exit question is not on 'Exit and stop tasks'; left for you to answer")
            return tasks
        time.sleep(0.4)
    return None


def settle_window(ctx: Ctx, pid: int, interrupted_file: str = None, draft_file: str = None) -> None:
    """Bring window `pid` to an idle, empty prompt now, whatever it is doing, so Ctrl+D closes it. A
    turn in progress, a permission prompt or a question is interrupted with Esc (the session on the
    other side is told); unsent text in the prompt is saved, then cleared."""
    status = window_status(ctx, pid)
    if status in ("busy", "waiting"):
        desktop.console_keys(pid, ["\x1b"])
        deadline = time.time() + 15
        while window_status(ctx, pid) in ("busy", "waiting") and time.time() < deadline and not desktop.wait_for_pid(pid, 0):
            time.sleep(0.3)
        log(f"interrupted the session ({'mid-turn' if status == 'busy' else 'it was waiting on a prompt'})")
        if interrupted_file:
            Path(interrupted_file).write_text(status, encoding="utf-8")
        time.sleep(1.6)
    else:
        desktop.console_keys(pid, ["\x1b"])  # declines a dialog left after a turn; on an idle prompt it does nothing
        time.sleep(1.0)
    shown = prompt_text(desktop.console_text(pid))
    if not shown:
        return
    desktop.console_keys(pid, ["\b"])
    time.sleep(0.5)
    if prompt_text(desktop.console_text(pid)) == shown:
        return  # a hint, not text
    if draft_file:
        Path(draft_file).write_text(shown, encoding="utf-8")
        log(f"the prompt held unsent text; saved it to {draft_file}")
    desktop.console_keys(pid, ["\b" * (len(shown) + 20)])
    time.sleep(0.8)


def type_into(ctx: Ctx, args) -> int:
    """The `_type` helper: wait for the right moment, then close the window or type a command."""
    if not desktop.WINDOWS_UI:
        log("typing into a Claude Code window is supported on Windows only")
        return 1
    if args.now:
        if desktop.wait_for_pid(args.pid, 0):
            log("the session closed; nothing typed")
            return 1
        settle_window(ctx, args.pid, args.interrupted_file, args.draft_file)
    elif args.after_exit_of:
        # After a hook: the command it blocked left the window idle with an empty prompt. Type only
        # into an empty prompt, never behind something already there.
        desktop.wait_for_pid(args.after_exit_of, 600)
        time.sleep(0.4)
        if desktop.wait_for_pid(args.pid, 0):
            log("the session closed; nothing typed")
            return 1
        if prompt_text(desktop.console_text(args.pid)):
            desktop.console_keys(args.pid, ["\x1b"])  # a greyed-out suggestion reads as text; Esc drops it
            time.sleep(1.5)
            if prompt_text(desktop.console_text(args.pid)):
                log("the prompt is not empty, so nothing typed")
                return 1
    else:
        transcript = Path(args.after_turn) if args.after_turn else None
        if not wait_turn_end(args.pid, transcript, args.offset):
            log("the session closed before its turn ended; nothing typed")
            return 1
        # Esc first: Claude Code can open a dialog when a turn ends, and keys typed into it would
        # answer it. Esc declines it; on an idle prompt it does nothing.
        desktop.console_keys(args.pid, ["\x1b"])
        time.sleep(1.0)
    if args.close:
        if args.ready_file:  # the conversation is final now: the move may start it on the server
            Path(args.ready_file).write_text("closing", encoding="utf-8")
        ok = desktop.console_keys(args.pid, ["\x04", "\x04"], pause=0.25)
        log("pressed Ctrl+D twice to close the window" if ok else "could not close the window; press Ctrl+D twice yourself")
    else:
        ok = desktop.console_keys(args.pid, [args.text])
        time.sleep(0.6)
        shown = prompt_text(desktop.console_text(args.pid))
        if ok and shown != args.text:
            log(f"the prompt reads {shown!r}, not {args.text!r}, so it was not sent; type {args.text!r} yourself")
            return 1
        ok = ok and desktop.console_keys(args.pid, ["\r"])
        log(f"typed {args.text!r} into the session" if ok else f"could not type {args.text!r}; type it yourself")
    if ok and args.exit_question:
        tasks = answer_exit_question(args.pid)
        if tasks is not None:
            log(f"answered the exit question with 'Exit and stop tasks': {tasks or 'tasks not listed'}")
        Path(args.exit_question).write_text(tasks or "", encoding="utf-8")
    if ok and args.then_file:
        time.sleep(args.then_after)
        note = Path(args.then_file).read_text(encoding="utf-8").strip()
        ok = desktop.console_keys(args.pid, ["\x1b", note, "\r"])
        log("told the session what the move stopped" if ok else "could not type the note about stopped work")
    return 0 if ok else 1
