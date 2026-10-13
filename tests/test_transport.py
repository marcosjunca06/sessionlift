"""The way to the server, without a server: the commands sent over ssh, the code kept there, how the
ssh binary is called, and answers read back."""

import io
import json
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from sessionlift import transport
from sessionlift.transport import LocalTransport, Result, SshTransport

from fakes import SessionCase, _rmtree


class CommandsTest(unittest.TestCase):
    def test_every_call_checks_the_code_and_refuses_cleanly(self):
        command = transport.remote_command("list")
        self.assertIn(transport.code_hash(), command)
        self.assertIn(f"cat >/dev/null; exit {transport.STALE_CODE}", command,
                      "a refused request is read to its end, so the laptop sees the exit code")
        self.assertTrue(command.endswith("python3 -m sessionlift.server list"))
        self.assertIn("python3 -m sessionlift.server 'a b'", transport.remote_command("a b"), "quoted for the shell")

    def test_the_code_archive_holds_the_package_and_the_server_shim(self):
        names = set(tarfile.open(fileobj=io.BytesIO(transport.code_archive()), mode="r:gz").getnames())
        modules = {f"sessionlift/{p.name}" for p in Path(transport.__file__).parent.glob("*.py")}
        self.assertEqual(names, modules | {"bin/sessionlift"})
        self.assertIn("sessionlift/server.py", names)
        self.assertIn("SESSIONLIFT_ROLE=server", transport.SHIM)
        upload = transport.upload_command()
        self.assertIn(f"printf %s {transport.code_hash()}", upload)
        self.assertIn('install -m 755 "$T/bin/sessionlift" "$HOME/.local/bin/sessionlift"', upload)

    def test_the_hash_ignores_line_endings(self):
        """A Windows checkout (CRLF) and the server's copy (LF) are the same code."""
        tmp = Path(tempfile.mkdtemp(prefix="sessionlift-hash-"))
        self.addCleanup(_rmtree, tmp)
        hashes = []
        for name, body in (("crlf.py", b"x = 1\r\ny = 2\r\n"), ("lf.py", b"x = 1\ny = 2\n"), ("other.py", b"x = 2\n")):
            (tmp / name).write_bytes(body)
            with mock.patch.object(transport, "package_files", lambda p=tmp / name: {"sessionlift/m.py": p}), \
                    mock.patch.object(transport, "_code", None):
                hashes.append(transport.code_hash())
        self.assertEqual(hashes[0], hashes[1])
        self.assertNotEqual(hashes[1], hashes[2])
        self.assertEqual(len(hashes[0]), 16)

    def test_frames(self):
        framed = transport.frame({"a": 1}, b"payload")
        n = int.from_bytes(framed[:8], "big")
        self.assertEqual((json.loads(framed[8:8 + n]), framed[8 + n:]), ({"a": 1}, b"payload"))


class ResultTest(unittest.TestCase):
    def test_answers_and_errors(self):
        self.assertEqual(Result(0, b'{"x": 1}', b"").value("reading"), {"x": 1})
        failed = Result(3, b"", b'{"error": "no checkout"}')
        self.assertFalse(failed.ok)
        self.assertEqual(failed.message(), "no checkout")
        with self.assertRaises(SystemExit) as caught:
            failed.value("starting")
        self.assertIn("starting failed on the server: no checkout", str(caught.exception))
        self.assertEqual(Result(255, b"", b"ssh: connect refused\n").message(), "ssh: connect refused")
        self.assertEqual(Result(9, None, None).message(), "exit 9")


class SshTest(SessionCase):
    def test_ssh_argv(self):
        t = SshTransport(self.cfg, self.ctx.state)
        self.assertEqual(t.ssh_argv("true", timeout=8),
                         ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "example-server", "true"])
        t = SshTransport(replace(self.cfg, user="dev", port=2222, identity_file="~/.ssh/id_dev"), self.ctx.state)
        argv = t.ssh_argv("true")
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertEqual(argv[argv.index("-l") + 1], "dev")
        self.assertTrue(argv[argv.index("-i") + 1].endswith("id_dev"))
        self.assertNotIn("~", argv[argv.index("-i") + 1])

    def test_a_proxy_in_ssh_config_leaves_the_connection_to_the_ssh_binary(self):
        home = Path(tempfile.mkdtemp(prefix="sessionlift-home-"))
        self.addCleanup(_rmtree, home)
        (home / ".ssh").mkdir()
        (home / ".ssh" / "config").write_text("Host example-server\n  HostName 192.0.2.1\n  ProxyJump bastion\n",
                                             encoding="utf-8")
        with mock.patch.object(Path, "home", lambda: home), \
                mock.patch("socket.create_connection", side_effect=OSError("no connection may be opened")) as connect:
            self.assertIsNone(SshTransport(self.cfg, self.ctx.state).client())
        connect.assert_not_called()

    def test_no_keeper_means_no_keeper_run(self):
        t = SshTransport(self.cfg, self.ctx.ensure_state())
        self.assertIsNone(t.keeper_info(fresh=True))
        self.assertIsNone(t.keeper_run("true", b"", 5))
        (self.ctx.state / "keeper.json").write_text(json.dumps({"pid": "999999999 0", "port": 1, "token": "x"}))
        self.assertIsNone(t.keeper_info(fresh=True), "a keeper whose process is gone is not used")


class LocalTest(SessionCase):
    def test_the_in_process_server_answers(self):
        r = LocalTransport(self.env).call("ping")
        self.assertTrue(r.ok, r.err)
        self.assertEqual(r.json()["home"], str(self.env.home))


if __name__ == "__main__":
    unittest.main()
