"""Which uncommitted files belong to the moving session: its own edits and the files its foreground
commands wrote and name; never what another session could have written."""

import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sessionlift.gitsync import own_files, write_windows

from fakes import jl

MOVER, OTHER = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


def stamp(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"


class OwnFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.t0 = time.time() - 600
        self.ids = 0
        self.lines = {MOVER: [], OTHER: []}

    def file(self, rel: str, at: float) -> str:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel)
        os.utime(path, (at, at))
        return rel

    def call(self, sid: str, name: str, start: float, end: float = None, **tool_input) -> None:
        """A tool call in a session's transcript; end None means it is still running."""
        self.ids += 1
        call_id = f"toolu_{self.ids}"
        self.lines[sid].append(jl(type="assistant", timestamp=stamp(start), message={"content": [
            {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}]}))
        if end is not None:
            self.lines[sid].append(jl(type="user", timestamp=stamp(end), message={"content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": "done"}]}))

    def own(self, dirty: list, group=frozenset()) -> tuple:
        for sid, lines in self.lines.items():
            (self.project / f"{sid}.jsonl").write_bytes(b"".join(lines))
        return own_files(MOVER, dirty, self.repo, self.project, group)

    def path(self, rel: str) -> str:
        return str(self.repo / rel)

    def test_an_edit_of_its_own(self):
        rel = self.file("a.txt", self.t0 + 1)
        self.call(MOVER, "Edit", self.t0, self.t0 + 2, file_path=self.path(rel))
        self.assertEqual(self.own([rel]), (["a.txt"], []))

    def test_another_sessions_edit_at_that_moment_contests_it(self):
        rel = self.file("a.txt", self.t0 + 1)
        self.call(MOVER, "Write", self.t0, self.t0 + 2, file_path=self.path(rel))
        self.call(OTHER, "Edit", self.t0, self.t0 + 2, file_path=self.path(rel))
        self.assertEqual(self.own([rel]), ([], ["a.txt"]))

    def test_a_command_counts_only_when_the_session_names_the_path(self):
        named = self.file("out/report.json", self.t0 + 3)
        unnamed = self.file("out/other.json", self.t0 + 3)
        self.call(MOVER, "Bash", self.t0, self.t0 + 5, command="python make_report.py --out out/report.json")
        self.assertEqual(self.own([named, unnamed]), (["out/report.json"], ["out/other.json"]))

    def test_another_sessions_command_naming_the_path_contests_a_command(self):
        rel = self.file("out/report.json", self.t0 + 3)
        self.call(MOVER, "Bash", self.t0, self.t0 + 5, command="build out/report.json")
        self.call(OTHER, "Bash", self.t0 + 1, self.t0 + 4, command="cat out/report.json")
        self.assertEqual(self.own([rel]), ([], ["out/report.json"]))

    def test_a_still_running_or_background_command_contests_too(self):
        rel = self.file("out/report.json", self.t0 + 30)
        self.call(MOVER, "Bash", self.t0 + 25, self.t0 + 35, command="build out/report.json")
        self.call(OTHER, "Bash", self.t0, None, command="long_job out/report.json")  # no result yet
        self.assertEqual(self.own([rel]), ([], ["out/report.json"]))
        self.lines[OTHER] = []
        self.call(OTHER, "Bash", self.t0, self.t0 + 1, command="watcher out/report.json", run_in_background=True)
        self.assertEqual(self.own([rel]), ([], ["out/report.json"]))

    def test_its_own_edit_beats_a_command_that_merely_ran(self):
        rel = self.file("a.txt", self.t0 + 1)
        self.call(MOVER, "Edit", self.t0, self.t0 + 2, file_path=self.path(rel))
        self.call(OTHER, "Bash", self.t0, self.t0 + 3, command="git status  # lists a.txt")
        self.assertEqual(self.own([rel]), (["a.txt"], []))

    def test_files_nobody_can_be_shown_to_have_written_stay(self):
        rel = self.file("a.txt", self.t0 + 100)
        self.call(MOVER, "Edit", self.t0, self.t0 + 2, file_path=self.path(rel))  # its edit ended long before
        self.assertEqual(self.own([rel, "deleted.txt"]), ([], ["a.txt", "deleted.txt"]))

    def test_sessions_moving_together_do_not_contest_each_other(self):
        rel = self.file("a.txt", self.t0 + 1)
        self.call(MOVER, "Edit", self.t0, self.t0 + 2, file_path=self.path(rel))
        self.call(OTHER, "Edit", self.t0, self.t0 + 2, file_path=self.path(rel))
        self.assertEqual(self.own([rel], group=frozenset({OTHER})), (["a.txt"], []))

    def test_write_windows_reads_calls_and_results(self):
        self.call(MOVER, "Edit", self.t0, self.t0 + 2, file_path="x")
        self.call(MOVER, "Bash", self.t0 + 3, self.t0 + 4, command="ls")
        self.call(MOVER, "Bash", self.t0 + 5, None, command="sleep 100")
        self.call(MOVER, "Bash", self.t0 + 6, None, command="watch", run_in_background=True)
        (self.project / f"{MOVER}.jsonl").write_bytes(b"".join(self.lines[MOVER]))
        edits, bash, background = write_windows(self.project / f"{MOVER}.jsonl")
        self.assertEqual([e[0] for e in edits], ["x"])
        self.assertEqual(len(bash), 2)
        self.assertEqual(bash[1][1], float("inf"), "a command without a result is still running")
        self.assertEqual(len(background), 1)


if __name__ == "__main__":
    unittest.main()
