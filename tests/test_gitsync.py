"""Git on both machines, against real repositories in a temp directory (a bare one stands in for
origin, so nothing leaves the machine): the moving session's own files are committed and pushed and
nothing else, and a checkout takes origin's commits without touching anyone's uncommitted work."""

import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from sessionlift import gitsync, moves

from fakes import SessionCase, _rmtree, new_sid, tool_call, touch, turn

MOVER = "11111111-1111-1111-1111-111111111111"


def git(repo, *args: str) -> str:
    return gitsync.git(repo, *args)


def isolated_git(test: unittest.TestCase, tmp: Path) -> None:
    """Git as on a fresh machine for the rest of the test: no global or system config (no signing, no
    hooks, no templates), a fixed identity, never a prompt."""
    empty = tmp / "gitconfig"
    empty.write_text("", encoding="utf-8")
    patcher = mock.patch.dict(os.environ, {
        "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "sessionlift tests", "GIT_AUTHOR_EMAIL": "tests@example.invalid",
        "GIT_COMMITTER_NAME": "sessionlift tests", "GIT_COMMITTER_EMAIL": "tests@example.invalid"})
    patcher.start()
    test.addCleanup(patcher.stop)


def make_origin(tmp: Path) -> Path:
    """A bare origin with one commit on main."""
    origin = tmp / "origin.git"
    git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp / "seed"
    git(tmp, "init", "-q", "-b", "main", str(seed))
    (seed / "README.md").write_text("readme\n", encoding="utf-8")
    git(seed, "add", "README.md")
    git(seed, "commit", "-q", "-m", "first")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "-u", "origin", "main")
    return origin


def clone(origin: Path, path: Path) -> Path:
    if path.exists():
        _rmtree(path)
    git(origin.parent, "clone", "-q", str(origin), str(path))
    return path


def files_in_last_commit(repo: Path, ref: str = "HEAD") -> list:
    return sorted(git(repo, "show", "--name-only", "--format=", ref).splitlines())


class GitTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="sessionlift-git-"))
        self.addCleanup(_rmtree, self.tmp)
        isolated_git(self, self.tmp)
        self.origin = make_origin(self.tmp)
        self.laptop = clone(self.origin, self.tmp / "laptop")
        self.server = clone(self.origin, self.tmp / "server")
        self.transcripts = self.tmp / "transcripts"  # the laptop's ~/.claude/projects/<project>
        self.transcripts.mkdir()
        self.t0 = time.time() - 600
        self.logs = []

    def session_edits(self, folder: Path, sid: str, *paths: Path) -> None:
        """A session whose Edit calls wrote these files, each at the moment its file was last changed."""
        lines = b"".join(tool_call("Edit", p.stat().st_mtime - 1, p.stat().st_mtime + 1, file_path=str(p)) for p in paths)
        with open(folder / f"{sid}.jsonl", "ab") as f:
            f.write(lines)

    def test_only_the_movers_own_files_are_committed_and_pushed(self):
        mine = touch(self.laptop / "src" / "mine.py", self.t0)
        touch(self.laptop / "theirs.py", self.t0)  # no session can be shown to have written it
        touch(self.laptop / "README.md", self.t0, "edited by someone else\n")
        self.session_edits(self.transcripts, MOVER, mine)
        branch = gitsync.commit_own_and_push(self.laptop, "Snapshot", MOVER, self.transcripts, self.logs.append)
        self.assertEqual(branch, "main")
        self.assertEqual(files_in_last_commit(self.laptop, "origin/main"), ["src/mine.py"])
        self.assertEqual(sorted(gitsync.dirty_paths(self.laptop)), ["README.md", "theirs.py"], "the rest stays as it was")
        self.assertTrue(any("left uncommitted" in line for line in self.logs))
        self.assertTrue(any("pushed 1 commit" in line for line in self.logs))

    def test_the_server_checkout_takes_origins_commits_and_keeps_uncommitted_work(self):
        self.session_edits(self.transcripts, MOVER, touch(self.laptop / "new.txt", self.t0))
        gitsync.commit_own_and_push(self.laptop, "Snapshot", MOVER, self.transcripts, self.logs.append)
        touch(self.server / "notes.txt", self.t0, "another session's work\n")
        markers, ok = gitsync.server_checkout(self.server, branch="main")
        self.assertTrue(ok, markers)
        self.assertTrue(markers[-1].startswith("CHECKOUT "), markers)
        self.assertTrue((self.server / "new.txt").exists())
        self.assertEqual((self.server / "notes.txt").read_text(encoding="utf-8"), "another session's work\n")

    def test_a_checkout_whose_uncommitted_files_origin_changes_stays_behind(self):
        touch(self.laptop / "README.md", self.t0, "changed on the laptop\n")
        git(self.laptop, "commit", "-q", "-am", "laptop change")
        git(self.laptop, "push", "-q")
        touch(self.server / "README.md", self.t0, "uncommitted on the server\n")
        markers, ok = gitsync.server_checkout(self.server, branch="main")
        self.assertIn("BEHIND", markers)
        self.assertTrue(ok)
        self.assertEqual((self.server / "README.md").read_text(encoding="utf-8"), "uncommitted on the server\n")

    def test_a_leaving_session_commits_what_it_wrote_on_the_server(self):
        server_transcripts = self.tmp / "server-transcripts"
        server_transcripts.mkdir()
        made = touch(self.server / "made-there.txt", self.t0)
        touch(self.server / "someone-elses.txt", self.t0)
        self.session_edits(server_transcripts, MOVER, made)
        markers, ok = gitsync.server_checkout(self.server, leaving=MOVER, project_dir=server_transcripts, why="Snapshot")
        self.assertTrue(ok, markers)
        self.assertIn("COMMITTED 1", markers)
        self.assertIn("LEFT 1", markers)
        self.assertIn("PUSHED main", markers)
        self.assertEqual(files_in_last_commit(self.origin, "main"), ["made-there.txt"])
        self.assertTrue((self.server / "someone-elses.txt").exists())
        self.assertTrue(gitsync.rebase_on_origin(self.laptop, self.logs.append))
        self.assertTrue((self.laptop / "made-there.txt").exists(), "the laptop takes the server's commit")

    def test_a_laptop_with_commits_of_its_own_rebases_onto_origin(self):
        touch(self.laptop / "laptop.txt", self.t0)
        git(self.laptop, "add", "laptop.txt")
        git(self.laptop, "commit", "-q", "-m", "on the laptop")
        touch(self.server / "server.txt", self.t0)
        git(self.server, "add", "server.txt")
        git(self.server, "commit", "-q", "-m", "on the server")
        git(self.server, "push", "-q")
        self.assertTrue(gitsync.rebase_on_origin(self.laptop, self.logs.append))
        self.assertEqual(git(self.laptop, "log", "--format=%s", "-3").splitlines(),
                         ["on the laptop", "on the server", "first"])

    def test_a_new_branch_is_pushed_and_checked_out_on_the_server(self):
        git(self.laptop, "checkout", "-q", "-b", "feature")
        self.session_edits(self.transcripts, MOVER, touch(self.laptop / "feature.txt", self.t0))
        branch = gitsync.commit_own_and_push(self.laptop, "Snapshot", MOVER, self.transcripts, self.logs.append)
        self.assertEqual(branch, "feature")
        self.assertIn("pushed feature", self.logs)
        markers, ok = gitsync.server_checkout(self.server, branch="feature")
        self.assertTrue(ok, markers)
        self.assertEqual(git(self.server, "rev-parse", "--abbrev-ref", "HEAD"), "feature")
        self.assertTrue((self.server / "feature.txt").exists())

    def test_dirty_paths_reads_renames_and_spaces(self):
        git(self.laptop, "mv", "README.md", "READ ME.md")
        touch(self.laptop / "dir with space" / "a b.txt", self.t0)
        self.assertEqual(sorted(gitsync.dirty_paths(self.laptop)), ["READ ME.md", "dir with space/a b.txt"])

    def test_big_untracked_files_never_ride_along(self):
        small = touch(self.laptop / "small.txt", self.t0)
        big = self.laptop / "big.bin"
        with open(big, "wb") as f:
            f.truncate(gitsync.BIG_FILE + 1)
        self.assertEqual(gitsync.small_dirty_paths(self.laptop), [small.name])


class MoveWithGitTest(SessionCase):
    """A whole move with git on: the laptop's commit reaches the server checkout, and the server's
    commit comes back with the session."""

    git_sync = True

    def setUp(self):
        super().setUp()
        isolated_git(self, self.tmp)
        origin = make_origin(self.tmp)
        clone(origin, self.local)
        clone(origin, self.remote)
        self.cwd = str(self.local)
        self.t0 = time.time() - 600

    def test_there_and_back(self):
        sid = new_sid()
        work = touch(self.local / "feature.txt", self.t0)
        self.write_session(sid, turn(self.cwd, "add a feature")
                           + tool_call("Write", self.t0 - 1, self.t0 + 1, file_path=str(work)))
        touch(self.local / "unrelated.txt", self.t0)
        self.assertEqual(moves.send_closed(self.ctx, self.project, sid, "Feature work"), 0)
        self.assertTrue((self.remote / "feature.txt").exists(), "the server checkout has the session's work")
        self.assertFalse((self.remote / "unrelated.txt").exists())
        self.assertIn("unrelated.txt", gitsync.dirty_paths(self.local))

        made = touch(self.remote / "made-on-server.txt", self.t0 + 100)
        with open(self.server_folder / f"{sid}.jsonl", "ab") as f:  # the session's work on the server
            f.write(tool_call("Edit", self.t0 + 99, self.t0 + 101, file_path=str(made)))
        self.assertTrue(self.env.mux.has(f"sl-{sid[:8]}"))
        project, back, title, note = moves.bring_back(self.ctx, sid)
        self.assertEqual(back, sid)
        self.assertFalse(self.env.mux.has(f"sl-{sid[:8]}"), "it was stopped on the server")
        self.assertTrue((self.local / "made-on-server.txt").exists(), "its commit came back with it")
        log = self.output.getvalue()
        self.assertIn("committed 1 file(s) the session wrote on the server", log)
        self.assertIn("took origin's new commits", log)
        self.assertTrue((self.env.archive / f"{sid}.jsonl").exists(), "the server's copy went to its archive")
        lines = [json.loads(x) for x in (self.folder / f"{sid}.jsonl").read_bytes().splitlines()]
        self.assertEqual(lines[0]["cwd"], self.cwd, "the transcript is in the laptop's form again")


if __name__ == "__main__":
    unittest.main()
