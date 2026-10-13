"""Whole moves, laptop to server and back, against the in-process server: what travels, what is
rewritten, what is kept."""

import json
import os
import unittest

from sessionlift import desktop, moves
from sessionlift.transport import LocalTransport

from fakes import FakeMux, SessionCase, jl, new_sid, turn


class Recording(LocalTransport):
    """LocalTransport that also records each call's arguments and payload size."""

    def __init__(self, env):
        super().__init__(env)
        self.sent = []

    def call(self, op, args=None, data=b"", timeout=600):
        self.sent.append((op, dict(args or {}), len(data or b"")))
        return super().call(op, args, data, timeout)


class MovesTest(SessionCase):
    def setUp(self):
        super().setUp()
        self.ctx.transport = Recording(self.env)
        self.cwd = str(self.local)

    def server_lines(self, sid):
        return (self.server_folder / f"{sid}.jsonl").read_bytes().splitlines()

    def send(self, sid):
        title = "Demo session"
        with open(os.devnull, "w") as quiet:
            import contextlib
            with contextlib.redirect_stdout(quiet):
                return moves.send_closed(self.ctx, self.project, sid, title)

    def test_send_moves_the_session_and_rewrites_its_directory(self):
        sid = new_sid()
        self.write_session(sid, turn(self.cwd, "first") + jl(aiTitle="Demo"), sides={"subagents/a.jsonl": jl(cwd=self.cwd)})
        self.assertEqual(self.send(sid), 0)
        cwds = {json.loads(line).get("cwd") for line in self.server_lines(sid)} - {None}
        self.assertEqual(cwds, {self.project.remote})
        side = json.loads((self.server_folder / sid / "subagents" / "a.jsonl").read_bytes())
        self.assertEqual(side["cwd"], self.project.remote)
        self.assertTrue(self.env.mux.has(f"sl-{sid[:8]}"))
        ops = [op for op, _, _ in self.ctx.transport.sent]
        self.assertEqual(ops[:4], ["can_take", "copy_state", "receive", "start"])

    def test_a_second_trip_sends_only_the_new_end(self):
        sid = new_sid()
        first = turn(self.cwd, "first")
        self.write_session(sid, first)
        self.send(sid)
        self.env.mux.kill(f"sl-{sid[:8]}")
        self.ctx.transport.call("archive", {"sid": sid, "project": self.ctx.remote_dir(self.project)})
        self.write_session(sid, first + turn(self.cwd, "second"))
        self.ctx.transport.sent.clear()
        self.send(sid)
        receive = next(args for op, args, _ in self.ctx.transport.sent if op == "receive")
        self.assertTrue(receive["append"], "the archive copy was the beginning: only the new end went")
        self.assertEqual(len(self.server_lines(sid)), 4)

    def test_a_diverged_copy_is_replaced_whole(self):
        sid = new_sid()
        self.server_folder.mkdir(parents=True)
        (self.server_folder / f"{sid}.jsonl").write_bytes(b'{"something":"else"}\n')
        self.write_session(sid, turn(self.cwd))
        self.send(sid)
        receive = next(args for op, args, _ in self.ctx.transport.sent if op == "receive")
        self.assertFalse(receive["append"])
        self.assertEqual(len(self.server_lines(sid)), 2)

    def test_send_refuses_without_a_signed_in_server_or_with_an_open_window(self):
        sid = new_sid()
        self.write_session(sid, turn(self.cwd))
        self.env.logged = False
        with self.assertRaises(SystemExit) as caught:
            self.send(sid)
        self.assertIn("not signed in", str(caught.exception))
        registry = self.ctx.claude / "sessions"
        registry.mkdir(parents=True)
        (registry / f"{os.getpid()}.json").write_text(json.dumps(
            {"pid": os.getpid(), "sessionId": sid, "cwd": self.cwd, "kind": "interactive", "status": "idle"}))
        with self.assertRaises(SystemExit) as caught:
            self.send(sid)
        self.assertIn("open in a window", str(caught.exception))

    def test_back_brings_the_new_turns_home(self):
        sid = new_sid()
        self.env.mux = FakeMux(ask=True)
        self.write_session(sid, turn(self.cwd, "first"))
        self.send(sid)
        with open(self.server_folder / f"{sid}.jsonl", "ab") as f:  # the session worked on the server
            f.write(turn(self.project.remote + "/web", "on the server"))
        project, back, title, note = moves.bring_back(self.ctx, sid[:8], pull=False)
        self.assertEqual((project.name, back), ("demo", sid))
        lines = [json.loads(x) for x in (self.folder / f"{sid}.jsonl").read_bytes().splitlines()]
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[-1]["cwd"], os.path.join(self.cwd, "web") if os.name == "nt" else self.cwd + "/web")
        self.assertIn("subagent · reviewing the board", note, "the session is told which work stopped")
        self.assertTrue((self.env.archive / f"{sid}.jsonl").exists(), "the server's copy goes to the archive")
        self.assertFalse((self.server_folder / f"{sid}.jsonl").exists())
        bring = next(args for op, args, _ in self.ctx.transport.sent if op == "bring_back")
        self.assertIn(f"HAVE {sid} ", bring["have"], "the laptop says what it holds, so only the new end comes back")

    def test_back_never_picks_a_session_by_itself(self):
        sid = new_sid()
        self.write_session(sid, turn(self.cwd))
        self.send(sid)  # the only session on the server
        self.ctx.transport.sent.clear()
        for nothing_named in (None, "", "zz"):
            with self.subTest(nothing_named), self.assertRaises(SystemExit):
                moves.bring_back(self.ctx, nothing_named, pull=False)
        self.assertEqual(self.ctx.transport.sent, [], "the server was not even asked")
        self.assertTrue(self.env.mux.has(f"sl-{sid[:8]}"), "it still runs there")

    def test_a_move_that_gives_up_takes_its_copy_back(self):
        sid = new_sid()
        self.write_session(sid, turn(self.cwd))
        moves.copy_ahead(self.ctx, self.project, sid)
        self.assertTrue((self.server_folder / f"{sid}.jsonl").exists())
        moves.unstage(self.ctx, self.project, sid)
        self.assertFalse((self.server_folder / f"{sid}.jsonl").exists())
        self.assertEqual(moves.server_sessions(self.ctx), [], "the list never shows a session that stayed here")

    def test_back_keeps_a_laptop_copy_the_server_never_saw(self):
        sid = new_sid()
        self.write_session(sid, turn(self.cwd, "first"))
        self.send(sid)
        self.write_session(sid, turn(self.cwd, "first") + turn(self.cwd, "typed on the laptop meanwhile"))
        moves.bring_back(self.ctx, sid, pull=False)
        backups = list(self.folder.glob(f"{sid}.laptop-*.jsonl.bak"))
        self.assertEqual(len(backups), 1)

    def test_the_cut_leaves_out_what_the_closing_window_wrote(self):
        sid = new_sid()
        start = turn(self.cwd, "first")
        self.write_session(sid, start)
        ahead = moves.copy_ahead(self.ctx, self.project, sid)
        git = moves.start_on_server(self.ctx, self.project, sid, "Demo", ahead, early=True)
        git.join()
        self.assertEqual(moves.server_cut(self.ctx, sid), len(start))
        self.write_session(sid, start + jl(type="system", content="exit records the server never got"))
        moves.bring_back(self.ctx, sid, pull=False)
        data = (self.folder / f"{sid}.jsonl").read_bytes()
        self.assertNotIn(b"exit records", data)
        self.assertIsNone(moves.server_cut(self.ctx, sid), "the cut is forgotten once the session is back")

    def test_the_list_hides_a_stale_copy(self):
        stale, live = new_sid(), new_sid()
        self.server_folder.mkdir(parents=True)
        for sid in (stale, live):
            (self.server_folder / f"{sid}.jsonl").write_bytes(jl(aiTitle=sid[:4]))
            os.utime(self.server_folder / f"{sid}.jsonl", (1_000_000_000, 1_000_000_000))
        self.write_session(stale, turn(self.cwd))  # the laptop's copy went on after the server's
        rows = moves.server_sessions(self.ctx)
        self.assertEqual([r[0] for r in rows], [live])

    def test_start_send_hands_the_move_to_a_background_helper(self):
        sid = new_sid()
        self.write_session(sid, turn(self.cwd) + jl(aiTitle="Background"))
        started = []
        original = desktop.spawn_detached
        desktop.spawn_detached = lambda argv, cwd: started.append(argv)
        try:
            title, log = moves.start_send(self.ctx, self.project, sid, 4242, hook_pid=77)
        finally:
            desktop.spawn_detached = original
        self.assertEqual(title, "Background")
        argv = started[0]
        self.assertEqual(argv[1:5], ["-m", "sessionlift", "--config", str(self.cfg.path)])
        self.assertEqual(argv[5:8], ["send", sid, "--after-pid"])
        self.assertIn("--after-hook", argv)

    def test_notes(self):
        self.assertEqual(moves.note_for("Moved.", "", False), "")
        note = moves.note_for("Moved.", "shell · npm test", True, behind=True)
        self.assertTrue(note.startswith("[Moved.] it interrupted your turn"))
        self.assertIn("shell · npm test", note)
        self.assertIn("could not take", note)


if __name__ == "__main__":
    unittest.main()
