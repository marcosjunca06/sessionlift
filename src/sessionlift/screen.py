"""Reading Claude Code's terminal screen and transcript: what is in the input box, which tasks its exit
question names, whether a turn has ended. Pure text; the tests feed it screens."""

from __future__ import annotations

import re

END_TURN = b'"stop_reason":"end_turn"'           # the transcript line that closes a turn
EXIT_QUESTION = "Background work is running"     # what Claude Code asks when exiting would stop tasks
EXIT_AND_STOP = re.compile(r"❯\s*1\.\s*Exit and stop tasks")


def prompt_text(screen: str):
    """What sits in Claude Code's input box: the lines between the last two ──── rules framing it (a
    menu or dialog below it marks its own choice with ❯ too), or else after the last ❯. None if there
    is no prompt on screen."""
    lines = screen.splitlines()
    rules = [i for i, line in enumerate(lines) if line.strip() and set(line.strip()) <= {"─", "▔", "▁"}]
    if len(rules) >= 2 and rules[-1] - rules[-2] > 1:
        box = " ".join(line.strip() for line in lines[rules[-2] + 1:rules[-1]] if line.strip())
        return box[1:].strip() if box.startswith("❯") else box.strip()
    prompts = [line.strip() for line in lines if line.strip().startswith("❯")]
    return prompts[-1][1:].strip() if prompts else None


def stopped_tasks(screen: str) -> str:
    """The tasks Claude Code's exit question lists ("subagent · ...", "shell · ..."), joined by "; "."""
    lines = screen.splitlines()
    start = next((i for i, line in enumerate(lines) if "will stop when you exit" in line), None)
    if start is None:
        return ""
    tasks = []
    for line in lines[start + 1:]:
        line = line.strip()
        if re.match(r"^(❯\s*)?\d\.", line):
            break
        if line:
            tasks.append(line)
    return "; ".join(tasks)
