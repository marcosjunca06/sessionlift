"""Sessions on the laptop: the windows Claude Code's registry lists, the sessions closed recently, and
finding one by the first characters of its id."""

import json
import os
import time
import unittest
from unittest import mock

from sessionlift import sessions

from fakes import SessionCase, jl, new_sid, turn


class SessionsTest(SessionCase):
    def register(self, pid: int, sid: str, cwd: str = None, **extra) -> None:
        """A window in Claude Code's registry, ~/.claude/sessions/<pid>.json."""
        registry = self.ctx.claude / "sessions"
        registry.mkdir(parents=True, exist_ok=True)
        (registry / f"{pid}.json").write_text(json.dumps(
            {"pid": pid, "sessionId": sid, "cwd": self.cwd if cwd is None else cwd, "kind": "interactive",
             "status": "idle", **extra}), encoding="utf-8")

    def setUp(self):
        super().setUp()
        self.cwd = str(self.local)
        self.alive = {101, 102, 103, 104}

    def windows(self):
        return sessions.open_windows(self.ctx, alive=lambda pid, started=None: pid in self.alive)

    def test_open_windows_come_from_the_registry(self):
        busy, idle, elsewhere, background, gone = (new_sid() for _ in range(5))
        for sid in (busy, idle, elsewhere, background, gone):
            self.write_session(sid, turn(self.cwd) + jl(aiTitle=f"title {sid[:4]}"))
        os.utime(self.folder / f"{idle}.jsonl", (time.time() - 60, time.time() - 60))
        self.register(101, busy, status="busy")
        self.register(102, idle)
        self.register(103, elsewhere, cwd=str(self.tmp / "some" / "other" / "project"))
        self.register(104, background, kind="bg")
        self.register(105, gone)  # its process has exited
        self.register(106, "not-a-session-id")
        rows = self.windows()
        self.assertEqual([(r["pid"], r["sid"], r["status"]) for r in rows], [(101, busy, "busy"), (102, idle, "idle")])
        self.assertEqual(rows[0]["title"], f"title {busy[:4]}")
        self.assertEqual(rows[0]["project"], "demo")
        self.assertEqual(sessions.window_status(self.ctx, 101), "busy")
        self.assertIsNone(sessions.window_status(self.ctx, 999))

    def test_closed_sessions_are_those_a_conversation_happened_in(self):
        talked, typed_only, open_one, old = (new_sid() for _ in range(4))
        self.write_session(talked, turn(self.cwd) + jl(customTitle="Talked"))
        self.write_session(typed_only, jl(type="user", cwd=self.cwd, message={"content": "/model"}))
        self.write_session(open_one, turn(self.cwd))
        self.write_session(old, turn(self.cwd))
        os.utime(self.folder / f"{old}.jsonl", (time.time() - 3 * 86400,) * 2)
        rows = sessions.closed_sessions(self.ctx, skip={open_one})
        self.assertEqual([(r["sid"], r["title"]) for r in rows], [(talked, "Talked")])
        self.assertEqual(len(sessions.closed_sessions(self.ctx, skip=set(), hours=24 * 7)), 3)

    def test_find_session_by_its_first_characters(self):
        a, b = "abcd0000-0000-4000-8000-000000000001", "abcd0000-0000-4000-8000-000000000002"
        for sid in (a, b):
            self.write_session(sid, turn(self.cwd))
        self.assertEqual(sessions.find_session(self.ctx, a[:-1] + "1")[1], a)
        for ambiguous_or_missing_or_bad in ("abcd", "ffff", "../x"):
            with self.subTest(ambiguous_or_missing_or_bad), self.assertRaises(SystemExit):
                sessions.find_session(self.ctx, ambiguous_or_missing_or_bad)

    def test_without_a_session_named_none_is_picked(self):
        self.write_session(new_sid(), turn(self.cwd))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            self.assertIsNone(sessions.resolve_session(self.ctx, None), "the newest transcript is never taken unasked")
        sid = new_sid()
        self.write_session(sid, turn(self.cwd))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": sid}):
            self.assertEqual(sessions.resolve_session(self.ctx, None), (self.project, sid))

    def test_same_dir(self):
        self.assertTrue(sessions.same_dir(self.cwd, self.cwd + os.sep))
        self.assertFalse(sessions.same_dir(self.cwd, ""))
        if os.name == "nt":
            self.assertTrue(sessions.same_dir(self.cwd.upper(), self.cwd.lower()))


if __name__ == "__main__":
    unittest.main()
