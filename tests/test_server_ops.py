"""The server operations, run in-process against a temp home with a fake tmux."""

import json
import unittest

from sessionlift import server
from sessionlift.transcripts import tar_files, untar
from sessionlift.transport import frame

from fakes import FakeMux, LINK, SessionCase, jl, new_sid, turn


class ServerOpsTest(SessionCase):
    def call(self, op, args=None, data=b""):
        code, out, err = server.handle(self.env, op, frame(args or {}, data))
        return code, out, json.loads(err) if err else {}

    def test_receive_writes_then_appends(self):
        sid, project = new_sid(), self.ctx.remote_dir(self.project)
        payload = tar_files({"arriving.jsonl": b"one\n", f"{sid}/subagents/a.jsonl": b"side\n"})
        self.assertEqual(self.call("receive", {"sid": sid, "project": project}, payload)[0], 0)
        self.call("receive", {"sid": sid, "project": project, "append": True}, tar_files({"arriving.jsonl": b"two\n"}))
        self.assertEqual((self.server_folder / f"{sid}.jsonl").read_bytes(), b"one\ntwo\n")
        self.assertEqual((self.server_folder / sid / "subagents" / "a.jsonl").read_bytes(), b"side\n")

    def test_side_files_must_belong_to_the_session(self):
        sid = new_sid()
        code, _, err = self.call("receive", {"sid": sid, "project": self.ctx.remote_dir(self.project)},
                                 tar_files({"arriving.jsonl": b"x\n", "someone-else/a.txt": b"x"}))
        self.assertNotEqual(code, 0)
        self.assertIn("outside the session", err["error"])

    def test_arguments_are_checked(self):
        self.assertEqual(self.call("copy_state", {"sid": "not-an-id", "project": "x"})[0], 2)
        self.assertEqual(self.call("copy_state", {"sid": new_sid(), "project": "a/b"})[0], 2)
        self.assertEqual(self.call("no_such_operation")[0], 2)

    def test_copy_state_takes_the_archive_copy_back(self):
        sid, project = new_sid(), self.ctx.remote_dir(self.project)
        self.env.archive.mkdir(parents=True)
        (self.env.archive / f"{sid}.jsonl").write_bytes(b"archived\n")
        (self.env.archive / sid).mkdir()
        (self.env.archive / sid / "tool.txt").write_bytes(b"12345")
        code, out, _ = self.call("copy_state", {"sid": sid, "project": project})
        state = json.loads(out)
        self.assertEqual((code, state["size"], state["sides"]), (0, 9, {f"{sid}/tool.txt": 5}))
        self.assertFalse((self.env.archive / f"{sid}.jsonl").exists())
        self.assertTrue((self.server_folder / f"{sid}.jsonl").exists())

    def test_start_runs_claude_in_tmux_with_remote_control(self):
        sid = new_sid()
        args = {"sid": sid, "project": self.ctx.remote_dir(self.project), "cwd": self.project.remote,
                "title": "Fix the build", "args": ["--chrome"]}
        code, out, _ = self.call("start", args, tar_files({"arriving.jsonl": b"t\n"}))
        self.assertEqual(code, 0)
        _, name, cwd, command = self.env.mux.log[0]
        self.assertEqual((name, cwd), (f"sl-{sid[:8]}", self.project.remote))
        self.assertIn(f"claude --resume {sid} --chrome --remote-control 'Fix the build'", command)
        self.assertNotIn("dangerously", command)
        self.assertEqual(self.call("start", args, tar_files({"arriving.jsonl": b"t\n"}))[0], 3, "already running")
        self.assertEqual(json.loads(self.call("await_link", {"mux": name})[1])["url"], LINK)

    def test_start_refuses_a_missing_checkout(self):
        args = {"sid": new_sid(), "project": self.ctx.remote_dir(self.project), "cwd": self.project.remote + "/nope"}
        code, _, err = self.call("start", args, tar_files({"arriving.jsonl": b"t\n"}))
        self.assertEqual(code, 3)
        self.assertIn("sessionlift setup", err["error"])

    def test_await_link_reports_an_exit_and_a_timeout(self):
        self.env.mux = FakeMux(link=False)
        self.env.mux.new("sl-0000aaaa", "/", "x")
        answer = json.loads(self.call("await_link", {"mux": "sl-0000aaaa", "timeout": 2})[1])
        self.assertIsNone(answer["url"])
        self.env.mux.sessions["sl-0000aaaa"]["screen"] += "[claude exited 1]\n"
        self.assertIn("exited", json.loads(self.call("await_link", {"mux": "sl-0000aaaa"})[1]))

    def test_list_marks_running_sessions_and_reads_titles(self):
        a, b = new_sid(), new_sid()
        self.server_folder.mkdir(parents=True)
        (self.server_folder / f"{a}.jsonl").write_bytes(jl(aiTitle="Idle one"))
        (self.server_folder / f"{b}.jsonl").write_bytes(jl(aiTitle="Auto") + jl(customTitle="Running one"))
        (self.server_folder / "notes.jsonl").write_bytes(b"{}\n")
        self.env.mux.new(f"sl-{b[:8]}", "/", "x")
        rows = json.loads(self.call("list", {"projects": [self.ctx.remote_dir(self.project)]})[1])["rows"]
        got = {r["sid"]: (r["state"], r["title"]) for r in rows}
        self.assertEqual(got, {a: ("idle", "Idle one"), b: ("running", "Running one")})

    def test_bring_back_stops_the_session_and_sends_only_the_new_end(self):
        sid = new_sid()
        self.env.mux = FakeMux(ask=True)
        self.server_folder.mkdir(parents=True)
        start = turn("/x", "first")
        (self.server_folder / f"{sid}.jsonl").write_bytes(start + turn("/x", "second"))
        (self.server_folder / sid).mkdir()
        (self.server_folder / sid / "same.txt").write_bytes(b"same")
        (self.server_folder / sid / "new.txt").write_bytes(b"new")
        self.env.mux.new(f"sl-{sid[:8]}", "/", "x")
        import hashlib
        have = f"HAVE {sid} {len(start)} {hashlib.sha256(start).hexdigest()}\nSIDE 4 {sid}/same.txt"
        code, out, err = self.call("bring_back", {"want": sid[:8], "have": have, "projects": [
            {"dir": self.ctx.remote_dir(self.project), "repo": "", "git": False}]})
        self.assertEqual(code, 0, err)
        files = untar(out)
        meta = json.loads(files.pop(server.META)[0])
        self.assertEqual((meta["sid"], meta["stopped"], meta["delta"]), (sid, True, len(start)))
        self.assertIn("Exit and stop tasks", meta["question"])
        self.assertEqual(files[f"{sid}.tail"][0], (self.server_folder / f"{sid}.jsonl").read_bytes()[len(start):])
        self.assertEqual(set(files), {f"{sid}.tail", f"{sid}/new.txt"})
        self.assertFalse(self.env.mux.has(f"sl-{sid[:8]}"))
        keys = [entry[2] for entry in self.env.mux.log if entry[0] == "send"]
        self.assertIn(("Escape",), keys)
        self.assertEqual(sum(1 for k in keys if k == ("C-d",)), 2, "closed with Ctrl+D twice, never a typed /exit")

    def test_bring_back_needs_one_match(self):
        a, b = "aaaa0000-0000-0000-0000-000000000001", "aaaa0000-0000-0000-0000-000000000002"
        self.server_folder.mkdir(parents=True)
        for sid in (a, b):
            (self.server_folder / f"{sid}.jsonl").write_bytes(b"x\n")
        projects = [{"dir": self.ctx.remote_dir(self.project), "repo": "", "git": False}]
        code, _, err = self.call("bring_back", {"want": "aaaa", "projects": projects})
        self.assertEqual((code, len(err["found"])), (5, 2))
        code, _, err = self.call("bring_back", {"projects": projects})
        self.assertEqual((code, err["running"]), (4, []), "with nothing named and nothing running, say which")

    def test_unstage_stops_and_archives(self):
        sid = new_sid()
        self.server_folder.mkdir(parents=True)
        (self.server_folder / f"{sid}.jsonl").write_bytes(b"x\n")
        self.env.mux.new(f"sl-{sid[:8]}", "/", "x")
        self.call("unstage", {"sid": sid, "project": self.ctx.remote_dir(self.project), "stop": True})
        self.assertFalse(self.env.mux.has(f"sl-{sid[:8]}"))
        self.assertTrue((self.env.archive / f"{sid}.jsonl").exists())
        self.assertFalse((self.server_folder / f"{sid}.jsonl").exists())

    def test_stage_append_checks_the_size_it_expects(self):
        sid, project = new_sid(), self.ctx.remote_dir(self.project)
        first = self.call("stage_append", {"sid": sid, "project": project, "fresh": True},
                          tar_files({"arriving.jsonl": b"abc\n"}))
        self.assertEqual(json.loads(first[1]), {"result": "ok", "size": 4})
        stale = self.call("stage_append", {"sid": sid, "project": project, "fresh": False, "expected": 99},
                          tar_files({"arriving.jsonl": b"def\n"}))
        self.assertEqual(json.loads(stale[1])["result"], "mismatch")
        self.server_folder.mkdir(parents=True)
        (self.server_folder / f"{sid}.jsonl").write_bytes(b"live\n")
        live = self.call("stage_append", {"sid": sid, "project": project, "fresh": False, "expected": 4},
                         tar_files({"arriving.jsonl": b"def\n"}))
        self.assertEqual(json.loads(live[1])["result"], "onserver", "a session live on the server is never staged over")

    def test_memory_files_stay_in_the_memory_folder(self):
        project = self.ctx.remote_dir(self.project)
        self.assertEqual(self.call("memory_put", {"project": project}, tar_files({"a.md": b"x"}))[0], 0)
        self.assertEqual(json.loads(self.call("memory_list", {"project": project})[1]).keys(), {"a.md"})
        self.assertNotEqual(self.call("memory_put", {"project": project}, tar_files({"sub/a.md": b"x"}))[0], 0)
        self.assertNotEqual(self.call("memory_get", {"project": project, "names": ["sub/a.md"]})[0], 0)

    def test_merge_claude_json(self):
        (self.env.home).mkdir(parents=True, exist_ok=True)
        (self.env.home / ".claude.json").write_text(json.dumps({"keep": 1, "projects": {"/p": {"a": 1}}}))
        self.call("merge_claude_json", {"patch": {"mcpServers": {"x": {}}, "projects": {"/p": {"b": 2}}}})
        merged = json.loads((self.env.home / ".claude.json").read_text())
        self.assertEqual(merged, {"keep": 1, "mcpServers": {"x": {}}, "projects": {"/p": {"a": 1, "b": 2}}})


if __name__ == "__main__":
    unittest.main()
