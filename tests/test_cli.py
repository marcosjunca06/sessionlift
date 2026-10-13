"""The command line: it loads, init writes a config that parses, and a session to move is always
asked for, never picked on its own."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sessionlift import cli, config, desktop

from fakes import SANDBOX, SessionCase, _rmtree, jl, new_sid, turn


def run(*argv: str) -> tuple:
    """(exit code, what it printed) for one command line."""
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else str(e.code))
    return code, printed.getvalue()


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sessionlift-cli-"))
        self.addCleanup(_rmtree, self.tmp)
        self.config = self.tmp / "config.toml"

    def init(self, *extra: str) -> tuple:
        return run("--config", str(self.config), "init", "--host", "devbox", "--project",
                   f"{self.tmp / 'my project'}=/home/dev/my-project", *extra)

    def test_help_lists_the_commands_and_hides_the_internal_one(self):
        code, text = run("--help")
        self.assertEqual(code, 0)
        for command in ("init", "install", "setup", "send", "back", "list", "page", "keeper", "hook"):
            self.assertIn(command, text)
        self.assertNotIn("_type", text)
        self.assertNotIn("SUPPRESS", text)
        self.assertEqual(run("--version"), (0, f"sessionlift {cli.__version__}\n"))

    def test_init_writes_a_config_that_parses_with_bypass_off(self):
        code, text = self.init("--name", "Dev box", "--user", "dev")
        self.assertEqual(code, 0, text)
        cfg = config.load(self.config)
        self.assertEqual((cfg.host, cfg.user, cfg.label), ("devbox", "dev", "Dev box"))
        self.assertEqual([(p.name, p.remote) for p in cfg.projects], [("my-project", "/home/dev/my-project")])
        self.assertFalse(cfg.bypass_permissions)
        self.assertEqual(cfg.provider.kind, "none")
        code, text = run("--config", str(self.config), "config")
        self.assertIn("bypass permissions on the server: no", text)

    def test_init_does_not_overwrite_without_force(self):
        self.init()
        before = self.config.read_text(encoding="utf-8")
        code, text = self.init("--name", "other")
        self.assertIn("exists", str(code))
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)
        self.assertEqual(self.init("--force", "--name", "other")[0], 0)
        self.assertEqual(config.load(self.config).name, "other")

    def test_init_refuses_what_would_not_parse(self):
        code, _ = run("--config", str(self.config), "init", "--host", "devbox", "--project", "relative=/srv/x")
        self.assertIn("not written", str(code))
        code, _ = run("--config", str(self.config), "init", "--host", "devbox", "--project", "no-equals-sign")
        self.assertIn("LOCAL=REMOTE", str(code))
        self.assertFalse(self.config.exists())

    def test_gcp_provider_from_init(self):
        self.init("--gcp-instance", "dev-1", "--gcp-zone", "europe-west1-b", "--firewall-rule", "dev-ssh")
        p = config.load(self.config).provider
        self.assertEqual((p.kind, p.instance, p.zone, p.firewall_rule), ("gcp", "dev-1", "europe-west1-b", "dev-ssh"))

    def test_install_writes_into_claude_codes_directory_on_windows_only(self):
        self.init()
        claude = SANDBOX / "claude"
        with mock.patch.object(desktop, "WINDOWS_UI", False):
            code, _ = run("--config", str(self.config), "install")
        self.assertIn("Windows laptops only", str(code))
        self.assertFalse((claude / "skills" / "to-server").exists())
        with mock.patch.object(desktop, "WINDOWS_UI", True):
            code, text = run("--config", str(self.config), "install")
            self.assertEqual(code, 0, text)
            self.assertTrue((claude / "skills" / "to-server" / "SKILL.md").exists())
            self.assertIn(self.config.resolve().as_posix(), (claude / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(run("--config", str(self.config), "install", "--uninstall")[0], 0)
        self.assertFalse((claude / "skills" / "to-server").exists())

    def test_the_servers_copy_only_runs_the_hook(self):
        self.init()
        with mock.patch.dict("os.environ", {"SESSIONLIFT_ROLE": "server"}):
            code, text = run("--config", str(self.config), "list")
        self.assertEqual(code, 2)
        self.assertIn("server's copy", text)

    def test_a_missing_config_says_to_run_init(self):
        code, _ = run("--config", str(self.tmp / "none.toml"), "list")
        self.assertIn("sessionlift init", str(code))


class PickTest(unittest.TestCase):
    OPTIONS = [("aaaaaaaa-0000-4000-8000-000000000001", "newest"), ("bbbbbbbb-0000-4000-8000-000000000002", "older")]

    def pick(self, answer, options=None, interactive=True):
        asked = []

        def ask(question):
            asked.append(question)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        with contextlib.redirect_stdout(io.StringIO()) as printed:
            result = cli.pick(options or self.OPTIONS, "Which?", ask=ask, interactive=interactive)
        return result, asked, printed.getvalue()

    def test_a_number_or_an_id(self):
        self.assertEqual(self.pick("2")[0], self.OPTIONS[1][0])
        self.assertEqual(self.pick(" BBBB ")[0], "bbbb", "an id or its first characters, for the move to look up")

    def test_one_session_is_still_asked_about(self):
        result, asked, printed = self.pick("1", options=self.OPTIONS[:1])
        self.assertEqual(len(asked), 1)
        self.assertIn("1. newest", printed)
        self.assertEqual(result, self.OPTIONS[0][0])

    def test_cancel_and_nonsense_move_nothing(self):
        for answer in ("", EOFError(), "3", "x; rm -rf"):
            with self.subTest(answer=answer), self.assertRaises(SystemExit):
                self.pick(answer)

    def test_without_a_terminal_it_lists_and_refuses(self):
        with self.assertRaises(SystemExit) as caught:
            self.pick("1", interactive=False)
        self.assertIn("say which session", str(caught.exception))


class ChoiceTest(SessionCase):
    """send and back without a session named, against the in-process server."""

    def test_back_lists_the_servers_sessions_and_asks(self):
        sid = new_sid()
        self.server_folder.mkdir(parents=True)
        (self.server_folder / f"{sid}.jsonl").write_bytes(jl(aiTitle="On the server"))
        with mock.patch("builtins.input", lambda question: "1"), mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = True
            self.assertEqual(cli._server_choice(self.ctx), sid)
        self.assertIn("On the server", self.output.getvalue())

    def test_back_with_nothing_on_the_server(self):
        with self.assertRaises(SystemExit) as caught:
            cli._server_choice(self.ctx)
        self.assertIn("no sessions on test-server", str(caught.exception))

    def test_send_lists_the_closed_sessions_and_asks(self):
        closed = new_sid()
        self.write_session(closed, turn(str(self.local)) + jl(customTitle="Closed one"))
        with mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = False
            with self.assertRaises(SystemExit):
                cli._laptop_choice(self.ctx)
        self.assertIn("Closed one", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
