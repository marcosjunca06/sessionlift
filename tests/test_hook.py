"""The /to-server and /from-server commands: what `install` writes into Claude Code's settings and
skills (and takes out again), and what the hook does with each event. Nothing here moves a session:
the moves are replaced by recorders."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sessionlift import desktop, hook

from fakes import SessionCase, _rmtree, new_sid, turn

OTHER_HOOK = {"matcher": "something-else", "hooks": [{"type": "command", "command": "echo hi"}]}


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sessionlift-install-"))
        self.addCleanup(_rmtree, self.tmp)
        self.claude = self.tmp / "claude"
        self.claude.mkdir()
        self.config = self.tmp / "my config" / "config.toml"
        self.settings = self.claude / "settings.json"
        self.settings.write_text(json.dumps({"model": "opus", "hooks": {"UserPromptExpansion": [OTHER_HOOK],
                                                                        "Stop": [OTHER_HOOK]}}), encoding="utf-8")

    def read(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_install_adds_two_hooks_and_two_skills_and_keeps_the_rest(self):
        hook.install(self.claude, self.config)
        s = self.read()
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["hooks"]["Stop"], [OTHER_HOOK])
        groups = s["hooks"]["UserPromptExpansion"]
        self.assertEqual([g["matcher"] for g in groups], ["something-else", "to-server", "from-server"])
        command = groups[1]["hooks"][0]["command"]
        self.assertTrue(command.startswith(f'"{Path(sys.executable).as_posix()}" -m sessionlift --config "'))
        self.assertTrue(command.endswith(f'{self.config.as_posix()}" hook'))
        for name in ("to-server", "from-server"):
            text = (self.claude / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn(f"name: {name}", text)
            self.assertIn("disable-model-invocation: true", text)
            self.assertIn(hook.MARK, text)
        self.assertTrue((self.claude / "settings.json.before-sessionlift").exists(), "backed up before the first edit")

    def test_install_twice_changes_nothing_more(self):
        hook.install(self.claude, self.config)
        first = self.settings.read_text(encoding="utf-8")
        lines = hook.install(self.claude, self.config)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), first)
        self.assertTrue(any("already" in line for line in lines))

    def test_uninstall_takes_out_only_its_own(self):
        original = self.read()
        (self.claude / "skills" / "mine").mkdir(parents=True)
        (self.claude / "skills" / "mine" / "SKILL.md").write_text("---\nname: mine\n---\n", encoding="utf-8")
        hook.install(self.claude, self.config)
        hook.install(self.claude, self.config, uninstall=True)
        self.assertEqual(self.read(), original)
        self.assertFalse((self.claude / "skills" / "to-server").exists())
        self.assertTrue((self.claude / "skills" / "mine" / "SKILL.md").exists())

    def test_uninstall_leaves_a_skill_of_the_same_name_it_did_not_write(self):
        (self.claude / "skills" / "to-server").mkdir(parents=True)
        (self.claude / "skills" / "to-server" / "SKILL.md").write_text("someone else's\n", encoding="utf-8")
        hook.install(self.claude, self.config, uninstall=True)
        self.assertTrue((self.claude / "skills" / "to-server" / "SKILL.md").exists())

    def test_a_broken_settings_file_is_refused_not_overwritten(self):
        self.settings.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(SystemExit):
            hook.install(self.claude, self.config)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{ not json")

    def test_no_settings_file_yet(self):
        self.settings.unlink()
        hook.install(self.claude, self.config, uninstall=True)
        self.assertFalse(self.settings.exists(), "uninstalling creates nothing")
        hook.install(self.claude, self.config)
        self.assertEqual(len(self.read()["hooks"]["UserPromptExpansion"]), 2)


class RunHookTest(SessionCase):
    """The hook itself, as Claude Code runs it: the event on stdin, the answer on stdout."""

    def setUp(self):
        super().setUp()
        self.answers = []
        self.sent, self.brought, self.switched, self.picked = [], [], [], []
        self.server_rows, self.choice = [], None
        self.use(mock.patch.object(desktop, "WINDOWS_UI", True))
        self.use(mock.patch.dict("os.environ", {"CLAUDE_PID": "4242"}))
        self.use(mock.patch("sessionlift.keeper.ensure_keeper", lambda ctx: None))
        self.use(mock.patch("sessionlift.moves.start_send", lambda ctx, project, sid, pid, hook_pid=None:
                            self.sent.append((sid, pid, hook_pid)) or ("Title", "log")))
        self.use(mock.patch("sessionlift.moves.bring_back", lambda ctx, want:
                            self.brought.append(want) or (self.project, want, "Back title", "")))
        self.use(mock.patch("sessionlift.moves.switch_window", lambda ctx, pid, sid, note, after_hook=None:
                            self.switched.append((pid, sid, after_hook))))
        self.use(mock.patch("sessionlift.moves.server_sessions", lambda ctx: self.server_rows))
        self.use(mock.patch("sessionlift.page.pick_session", lambda ctx, rows:
                            self.picked.append(rows) or self.choice))

    def use(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_hook(self, **event):
        return hook.run_hook(json.dumps(event).encode(), lambda: self.ctx, self.answers.append)

    def answer(self) -> dict:
        self.assertEqual(len(self.answers), 1, self.answers)
        decision = json.loads(self.answers[0])
        self.assertEqual(decision["decision"], "block", "the command never reaches the model")
        return decision

    def test_other_commands_and_bad_input_pass_through(self):
        self.assertEqual(self.run_hook(command_name="compact"), 0)
        self.assertEqual(hook.run_hook(b"not json", lambda: self.ctx, self.answers.append), 0)
        self.assertEqual(self.answers, [])

    def test_to_server_starts_the_move_and_blocks(self):
        sid = new_sid()
        self.write_session(sid, turn(str(self.local)))
        self.run_hook(command_name="to-server", session_id=sid)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][:2], (sid, "4242"))
        self.assertIn('Moving "Title" to test-server', self.answer()["reason"])

    def test_to_server_without_a_conversation_moves_nothing(self):
        self.run_hook(command_name="to-server", session_id=new_sid())
        self.assertEqual(self.sent, [])
        self.assertIn("Nothing to move", self.answer()["reason"])

    def test_without_claude_pid_nothing_moves(self):
        self.use(mock.patch.dict("os.environ", {"CLAUDE_PID": ""}))
        sid = new_sid()
        self.write_session(sid, turn(str(self.local)))
        self.run_hook(command_name="to-server", session_id=sid)
        self.assertEqual(self.sent, [])
        self.assertIn("CLAUDE_PID", self.answer()["reason"])

    def test_from_server_always_asks_even_with_one_session(self):
        self.server_rows = [(new_sid(), "idle", "3 min ago", "The only one", "demo")]
        self.run_hook(command_name="from-server", session_id=new_sid())
        self.assertEqual(len(self.picked), 1, "the picker showed")
        self.assertEqual(self.brought, [], "cancelled: nothing came back")
        self.assertIn("Nothing brought back", self.answer()["reason"])

    def test_from_server_brings_the_picked_one_into_this_window(self):
        sid = new_sid()
        self.server_rows = [(sid, "idle", "3 min ago", "Picked", "demo")]
        self.choice = sid
        self.run_hook(command_name="from-server", session_id=new_sid())
        self.assertEqual(self.brought, [sid])
        self.assertEqual(self.switched[0][:2], ("4242", sid))
        self.assertIn("Back title", self.answer()["reason"])

    def test_from_server_with_an_id_skips_the_picker(self):
        self.run_hook(command_name="from-server", command_args=" abcd1234 ", session_id=new_sid())
        self.assertEqual((self.picked, self.brought), ([], ["abcd1234"]))

    def test_from_server_with_nothing_there(self):
        self.run_hook(command_name="from-server", session_id=new_sid())
        self.assertEqual(self.picked, [])
        self.assertIn("No sessions", self.answer()["reason"])

    def test_on_the_server_the_commands_only_say_where_they_work(self):
        self.use(mock.patch.dict("os.environ", {"SESSIONLIFT_ROLE": "server"}))
        self.run_hook(command_name="to-server", session_id=new_sid())
        self.assertEqual(self.sent, [])
        self.assertIn("runs on the server", self.answer()["reason"])

    def test_not_yet_on_other_laptops(self):
        self.use(mock.patch.object(desktop, "WINDOWS_UI", False))
        self.run_hook(command_name="from-server", session_id=new_sid())
        self.assertEqual((self.picked, self.brought), ([], []))
        self.assertIn("Windows only", self.answer()["reason"])

    def test_a_config_that_does_not_load_is_reported(self):
        def broken():
            raise SystemExit("no configuration at x")
        hook.run_hook(json.dumps({"command_name": "to-server"}).encode(), broken, self.answers.append)
        self.assertIn("no configuration", self.answer()["reason"])


if __name__ == "__main__":
    unittest.main()
