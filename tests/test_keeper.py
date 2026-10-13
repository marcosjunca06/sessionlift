"""The keeper: copies ahead in chunks, and its door for other processes on 127.0.0.1."""

import json
import os
import socket
import stat
import subprocess
import unittest

from sessionlift import keeper, transport
from sessionlift.transport import SshTransport

from fakes import SessionCase, jl, new_sid, turn


class StagingTest(SessionCase):
    def archive_copy(self, sid):
        return (self.env.archive / f"{sid}.jsonl").read_bytes()

    def test_a_long_transcript_goes_in_chunks(self):
        sid = new_sid()
        data = b"".join(turn(str(self.local), f"message {i}") for i in range(10))
        self.write_session(sid, data)
        rec, more, rounds = None, True, 0
        while more:
            rec, more = keeper.stage_session(self.ctx, self.project, sid, rec, chunk=600)
            self.assertIsNotNone(rec)
            rounds += 1
        self.assertGreater(rounds, 2)
        self.assertEqual(len(self.archive_copy(sid).splitlines()), 20)
        self.assertNotIn(str(self.local).encode().replace(b"\\", b"\\\\"), self.archive_copy(sid),
                         "copies are in the server's form")
        self.assertEqual(keeper.stage_session(self.ctx, self.project, sid, rec), (rec, False), "nothing new, nothing sent")

    def test_new_lines_are_appended_and_a_changed_copy_starts_over(self):
        sid = new_sid()
        first, second, third = (turn(str(self.local), text) for text in ("first", "second", "third"))
        self.write_session(sid, first)
        rec, _ = keeper.stage_session(self.ctx, self.project, sid, None)
        self.write_session(sid, first + second)
        rec, _ = keeper.stage_session(self.ctx, self.project, sid, rec)
        self.assertEqual(len(self.archive_copy(sid).splitlines()), 4)
        self.assertEqual(self.archive_copy(sid), self.ctx_form(first + second), "only the new end was appended")
        (self.env.archive / f"{sid}.jsonl").write_bytes(b"changed elsewhere\n")
        self.write_session(sid, first + second + third)
        self.assertEqual(keeper.stage_session(self.ctx, self.project, sid, rec)[0], None, "a mismatch is worked out afresh")
        rec, _ = keeper.stage_session(self.ctx, self.project, sid, None)
        self.assertEqual(self.archive_copy(sid), self.ctx_form(first + second + third))

    def test_a_transcript_that_no_longer_begins_the_same_is_copied_whole(self):
        sid = new_sid()
        self.write_session(sid, turn(str(self.local), "first"))
        rec, _ = keeper.stage_session(self.ctx, self.project, sid, None)
        replaced = turn(str(self.local), "came back from the server") + turn(str(self.local), "and went on")
        self.write_session(sid, replaced)
        rec, more = keeper.stage_session(self.ctx, self.project, sid, rec)
        self.assertEqual((self.archive_copy(sid), more), (self.ctx_form(replaced), False))

    def ctx_form(self, data):
        from sessionlift.moves import _to_server
        return _to_server(self.project, data)

    def test_a_session_live_on_the_server_is_left_alone(self):
        sid = new_sid()
        self.write_session(sid, turn(str(self.local)))
        self.server_folder.mkdir(parents=True)
        (self.server_folder / f"{sid}.jsonl").write_bytes(jl(live=True))
        self.assertEqual(keeper.stage_session(self.ctx, self.project, sid, None), (None, False))
        self.assertFalse((self.env.archive / f"{sid}.jsonl").exists())


class CycleTest(SessionCase):
    """One round of the keeper against the in-process server."""

    def test_a_round_copies_the_recent_sessions_ahead_and_records_the_server(self):
        closed, live_there = new_sid(), new_sid()
        self.write_session(closed, turn(str(self.local), "worked on here"))
        self.write_session(live_there, turn(str(self.local)))
        self.server_folder.mkdir(parents=True)
        (self.server_folder / f"{live_there}.jsonl").write_bytes(turn(self.project.remote))  # it runs there now
        staged = {"0123abcd-0000-4000-8000-000000000000": {"cut": 1}}  # a session gone from the laptop
        pending = keeper.keeper_cycle(self.ctx, staged)
        self.assertFalse(pending)
        self.assertEqual(set(staged), {closed}, "copied ahead; the live one and the vanished one are not")
        self.assertTrue((self.env.archive / f"{closed}.jsonl").exists())
        self.assertFalse((self.env.archive / f"{live_there}.jsonl").exists())
        status = json.loads((self.ctx.state / "keeper-status.json").read_text())
        self.assertEqual((status["ok"], status["logged_in"]), (True, True))
        self.assertEqual(json.loads((self.ctx.state / "staged.json").read_text()), staged)

    def test_a_session_being_moved_is_left_alone(self):
        sid = new_sid()
        self.write_session(sid, turn(str(self.local)))
        from sessionlift.desktop import pid_stamp
        self.ctx.ensure_state()
        (self.ctx.state / f"send-{sid[:8]}.pid").write_text(pid_stamp())
        staged = {}
        keeper.keeper_cycle(self.ctx, staged)
        self.assertEqual(staged, {})
        self.assertFalse((self.env.archive / f"{sid}.jsonl").exists())

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_the_token_file_is_private(self):
        path = self.ctx.ensure_state() / "keeper.json"
        keeper.write_private(path, '{"token": "x"}')
        keeper.write_private(path, '{"token": "y"}')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text())["token"], "y")


class DoorTest(SessionCase):
    """A process asks the keeper to run a command; the keeper runs it on its connection."""

    def test_round_trip_and_token(self):
        ran = []

        def fake_run(client, remote, data, timeout):
            ran.append((remote, data))
            return subprocess.CompletedProcess(["ssh"], 3, b"out:" + (data or b""), b"err")

        original = keeper.paramiko_run
        keeper.paramiko_run = fake_run
        holder = SshTransport(self.cfg, self.ctx.ensure_state(), use_keeper=False)
        holder.client = lambda: object()
        server = keeper.serve(holder, "secret-token")
        try:
            from sessionlift.desktop import pid_stamp
            (self.ctx.state / "keeper.json").write_text(json.dumps(
                {"pid": pid_stamp(), "port": server.server_address[1], "token": "secret-token"}))
            asker = SshTransport(self.cfg, self.ctx.state)
            r = asker.keeper_run("echo hi", b"payload", 30)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (3, b"out:payload", b"err"))
            self.assertEqual(ran, [("echo hi", b"payload")])
            with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as sock:
                transport.send_frame(sock, json.dumps({"token": "wrong", "remote": "x"}).encode())
                transport.send_frame(sock, b"")
                sock.settimeout(5)
                self.assertEqual(sock.recv(1), b"", "a wrong token gets nothing and runs nothing")
            self.assertEqual(len(ran), 1)
        finally:
            keeper.paramiko_run = original
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
