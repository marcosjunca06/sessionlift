"""Claude Code's auto memory for a project (~/.claude/projects/<project>/memory), kept the same on both
machines: on every move each file goes the way it is newer, and the MEMORY.md index merges line by
line against the version both sides last agreed on."""

from __future__ import annotations

from .config import Project
from .context import Ctx, log
from .locks import FileLock
from .transcripts import tar_files, untar, write_file


def merge_memory_index(base, mine: str, theirs: str) -> str:
    """Line-level three-way merge of MEMORY.md: keep my order, add their new lines, drop the ones
    they deleted. Without a base it is a union."""
    mine_l, theirs_l = mine.splitlines(), theirs.splitlines()
    base_s = set(base.splitlines()) if base is not None else set()
    theirs_s, mine_s = set(theirs_l), set(mine_l)
    kept = [line for line in mine_l if not (line in base_s and line not in theirs_s)]
    added = [line for line in theirs_l if line not in mine_s and line not in base_s and line.strip()]
    return "\n".join(kept + added) + "\n"


def sync_memory(ctx: Ctx, project: Project) -> None:
    if not project.sync_memory:
        return
    with FileLock(ctx.ensure_state() / f"memory-{project.name}.lock", timeout=120):  # moves side by side
        _sync(ctx, project)


def _sync(ctx: Ctx, project: Project) -> None:
    local = ctx.local_dir(project) / "memory"
    where = {"project": ctx.remote_dir(project)}
    theirs = ctx.server.call("memory_list", where).value("reading the server's memory")
    mine = {p.name: p.stat().st_mtime for p in local.glob("*") if p.is_file()} if local.is_dir() else {}
    push = {n: local / n for n, t in mine.items() if n != "MEMORY.md" and t > theirs.get(n, 0) + 1}
    pull = [n for n, t in theirs.items() if n != "MEMORY.md" and t > mine.get(n, 0) + 1]
    if push:
        ctx.server.call("memory_put", where, tar_files(push)).value("writing memory on the server")
    if pull:
        got = ctx.server.call("memory_get", {**where, "names": pull})
        if not got.ok:
            raise SystemExit(f"reading memory from the server failed: {got.message()}")
        for name, (data, mtime) in untar(got.out).items():
            write_file(local / name, data, mtime)
    base_file = ctx.state / f"memory-base-{project.name}.md"
    base = base_file.read_text(encoding="utf-8") if base_file.exists() else None
    mine_idx = (local / "MEMORY.md").read_text(encoding="utf-8") if (local / "MEMORY.md").exists() else ""
    theirs_idx = ""
    if "MEMORY.md" in theirs:
        got = ctx.server.call("memory_get", {**where, "names": ["MEMORY.md"]})
        theirs_idx = untar(got.out).get("MEMORY.md", (b"", 0))[0].decode("utf-8") if got.ok else ""
    if not mine_idx.strip() and not theirs_idx.strip():  # no index on either side: none to make
        log(f"memory ({project.name}): {len(push)} file(s) to the server, {len(pull)} back")
        return
    merged = merge_memory_index(base, mine_idx, theirs_idx)
    if merged != mine_idx:
        local.mkdir(parents=True, exist_ok=True)
        (local / "MEMORY.md").write_text(merged, encoding="utf-8", newline="\n")
    if merged != theirs_idx:
        ctx.server.call("memory_put", where, tar_files({"MEMORY.md": merged.encode()})).value("writing MEMORY.md")
    base_file.write_text(merged, encoding="utf-8", newline="\n")
    changed = [side for side, before in (("laptop", mine_idx), ("server", theirs_idx)) if merged != before]
    log(f"memory ({project.name}): {len(push)} file(s) to the server, {len(pull)} back, index "
        + (f"updated on the {' and '.join(changed)}" if changed else "in step"))
