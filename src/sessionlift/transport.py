"""The way to the server.

`Transport.call(operation, args, payload)` runs one server operation (sessionlift/server.py) and
returns its answer. `SshTransport` is the real one: it runs the command over the keeper's open
connection when the keeper runs (no new connection), else over this process's own paramiko
connection, else the ssh binary, which also reopens the way through the provider when the server does
not answer. `LocalTransport` runs the same operations in this process against a directory standing in
for the server's home; the tests use it.

The server runs the copy of this package that the laptop keeps there, at
~/.local/share/sessionlift/lib. Every call carries a hash of this package's code; a server holding
other code answers with exit code 87, and the laptop then uploads its own and calls again.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

from . import server as server_ops
from .context import log
from .gitsync import run
from .transcripts import tar_files

LIB = ".local/share/sessionlift/lib"   # under the server's home
STALE_CODE = 87


class Result:
    def __init__(self, code: int, out: bytes, err: bytes) -> None:
        self.code, self.out, self.err = code, out or b"", err or b""

    @property
    def ok(self) -> bool:
        return self.code == 0

    def json(self):
        return json.loads(self.out or b"{}")

    def error(self) -> dict:
        try:
            info = json.loads(self.err)
            if isinstance(info, dict):
                return info
        except ValueError:
            pass
        return {"error": self.err.decode(errors="replace").strip() or f"exit {self.code}"}

    def message(self) -> str:
        return str(self.error().get("error"))

    def value(self, what: str):
        """The answer as JSON, or SystemExit naming what failed."""
        if not self.ok:
            raise SystemExit(f"{what} failed on the server: {self.message()}")
        return self.json()


def frame(args: dict, data: bytes = b"") -> bytes:
    blob = json.dumps(args or {}).encode()
    return len(blob).to_bytes(8, "big") + blob + (data or b"")


class Transport:
    label = "the server"

    def call(self, op: str, args: dict = None, data: bytes = b"", timeout: float = 600) -> Result:
        raise NotImplementedError

    def connect(self) -> None:
        """Make sure the server answers (start it, open its firewall) or SystemExit."""


class LocalTransport(Transport):
    """The server operations, run in this process against `env` (a server.ServerEnv)."""

    def __init__(self, env, label: str = "test-server") -> None:
        self.env, self.label = env, label
        self.calls = []

    def call(self, op: str, args: dict = None, data: bytes = b"", timeout: float = 600) -> Result:
        self.calls.append(op)
        return Result(*server_ops.handle(self.env, op, frame(args or {}, data)))


# ---------------------------------------------------------------- code on the server

_code = None


def package_files() -> dict:
    """{archive name: path} for this package's code, as the server keeps it."""
    here = Path(__file__).resolve().parent
    return {f"sessionlift/{p.name}": p for p in sorted(here.glob("*.py"))}


def code_hash() -> str:
    global _code
    if _code is None:
        h = hashlib.sha256()
        for name, path in package_files().items():
            h.update(name.encode() + b"\0" + path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
        _code = h.hexdigest()[:16]
    return _code


SHIM = """#!/bin/sh
# sessionlift on the server: the copy of the package the laptop keeps here. Only `hook` is useful on
# this side; it tells a session that /to-server and /from-server run from the laptop.
SESSIONLIFT_ROLE=server PYTHONPATH="$HOME/{lib}" exec python3 -m sessionlift "$@"
""".format(lib=LIB)


def remote_command(op: str) -> str:
    """The shell command for one operation. A refusal reads its input first (cat): a large request
    would otherwise still be on its way when the command ends, and the laptop would see a dropped
    connection instead of the exit code."""
    return (f'. "$HOME/.sessionlift-env" 2>/dev/null; H="$HOME/{LIB}"; '
            f'[ "$(cat "$H/CODE_HASH" 2>/dev/null)" = "{code_hash()}" ] || {{ cat >/dev/null; exit {STALE_CODE}; }}; '
            f'command -v python3 >/dev/null || {{ cat >/dev/null; '
            f'echo "{{\\"error\\": \\"python3 is not installed on the server\\"}}" >&2; exit 127; }}; '
            f'PYTHONPATH="$H" exec python3 -m sessionlift.server {shlex.quote(op)}')


def upload_command() -> str:
    """Replace the server's copy of the package, one writer at a time, and install the shim."""
    return (f'H="$HOME/{LIB}"; mkdir -p "$H" "$HOME/.local/bin" && exec 9>"$H/.lock" && flock -w 60 9; '
            f'T=$(mktemp -d "$H/.new.XXXXXX") && tar -xzf - -C "$T" && rm -rf "$H/sessionlift" && '
            f'mv "$T/sessionlift" "$H/sessionlift" && install -m 755 "$T/bin/sessionlift" "$HOME/.local/bin/sessionlift" && '
            f'rm -rf "$T" && printf %s {code_hash()} > "$H/CODE_HASH"')


def code_archive() -> bytes:
    files = dict(package_files())
    files["bin/sessionlift"] = SHIM.encode()
    return tar_files(files)


# ---------------------------------------------------------------- ssh

def _send_frame(sock, payload: bytes) -> None:
    sock.sendall(len(payload).to_bytes(8, "big") + payload)


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(1 << 20, n - len(buf)))
        if not chunk:
            raise ConnectionError("closed mid-message")
        buf += chunk
    return bytes(buf)


def recv_frame(sock) -> bytes:
    return _recv_exact(sock, int.from_bytes(_recv_exact(sock, 8), "big"))


send_frame = _send_frame


class NotStarted(Exception):
    """The command never reached the server, so running it another way cannot run it twice."""


def paramiko_run(client, remote: str, data: bytes, timeout: float) -> subprocess.CompletedProcess:
    try:
        chan = client.get_transport().open_session(timeout=20)
        chan.settimeout(timeout)
        chan.exec_command(remote)
    except Exception as e:  # noqa: BLE001 - whatever stopped it, nothing ran
        raise NotStarted(str(e)) from e
    if data:
        chan.sendall(data)
    chan.shutdown_write()
    out, err = bytearray(), bytearray()
    deadline = time.time() + timeout
    while True:
        if chan.recv_ready():
            out += chan.recv(1 << 20)
        elif chan.recv_stderr_ready():
            err += chan.recv_stderr(1 << 20)
        elif chan.exit_status_ready():
            break
        elif time.time() > deadline:
            chan.close()
            raise TimeoutError(f"{remote[:60]} took over {timeout} s on the server")
        else:
            time.sleep(0.005)
    code = chan.recv_exit_status()
    while chan.recv_ready():
        out += chan.recv(1 << 20)
    while chan.recv_stderr_ready():
        err += chan.recv_stderr(1 << 20)
    chan.close()
    return subprocess.CompletedProcess(["ssh", remote], code, bytes(out), bytes(err))


class SshTransport(Transport):
    def __init__(self, cfg, state: Path, use_keeper: bool = True) -> None:
        self.cfg, self.state, self.use_keeper = cfg, Path(state), use_keeper
        self.label = cfg.label
        self._client = None
        self._lock = threading.Lock()
        self._keeper_seen = (0.0, None)

    # -- the ssh binary

    def ssh_argv(self, *remote: str, timeout: int = 10) -> list:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}"]
        if self.cfg.port:
            argv += ["-p", str(self.cfg.port)]
        if self.cfg.identity_file:
            argv += ["-i", os.path.expanduser(self.cfg.identity_file)]
        if self.cfg.user:
            argv += ["-l", self.cfg.user]
        return [*argv, self.cfg.host, *remote]

    def reachable(self) -> bool:
        return run(self.ssh_argv("true", timeout=8), timeout=60).returncode == 0

    def connect(self) -> None:
        if self.reachable():
            return
        from .providers import provider_for
        provider = provider_for(self.cfg)
        if not provider.can_open:
            raise SystemExit(f"cannot reach {self.cfg.host} over ssh (try: ssh {self.cfg.host})")
        log(f"{self.label} does not answer; checking the instance and this laptop's address")
        provider.open()
        for _ in range(12):
            if self.reachable():
                return
            time.sleep(5)
        raise SystemExit(f"still cannot reach {self.cfg.host}")

    # -- paramiko: one connection per process

    def client(self):
        """This process's connection, opened on first use; None without paramiko or when it cannot
        connect (the ssh binary then takes over, and reopens the way if it must)."""
        with self._lock:
            if self._client is not None and self._client.get_transport() and self._client.get_transport().is_active():
                return self._client
            try:
                import socket
                import paramiko
                ssh_config = Path.home() / ".ssh" / "config"
                found = paramiko.SSHConfig.from_path(str(ssh_config)).lookup(self.cfg.host) if ssh_config.exists() else {}
                if found.get("proxycommand") or found.get("proxyjump"):
                    return None  # only the ssh binary goes the way ~/.ssh/config says
                host = found.get("hostname", self.cfg.host)
                port = self.cfg.port or int(found.get("port", 22))
                user = self.cfg.user or found.get("user")
                keys = ([os.path.expanduser(self.cfg.identity_file)] if self.cfg.identity_file else
                        [os.path.expanduser(k) for k in found.get("identityfile", [])])
                sock = socket.create_connection((host, port), timeout=10)
                # Windows' default 64 KB send buffer held uploads to about 0.6 MB/s at 97 ms; with 4 MB
                # they ran at 2.8 MB/s.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
                client = paramiko.SSHClient()
                known = Path.home() / ".ssh" / "known_hosts"
                if known.exists():
                    client.load_host_keys(str(known))  # the server's key, or refuse: no auto-add
                client.connect(host, port=port, username=user, sock=sock, key_filename=keys or None, timeout=10,
                               banner_timeout=20, auth_timeout=20, look_for_keys=not keys, allow_agent=True)
                client.get_transport().set_keepalive(20)
                self._client = client
            except Exception:  # noqa: BLE001 - any failure: the ssh binary takes over
                self._client = None
            return self._client

    def forget(self) -> None:
        self._client = None

    # -- the keeper's connection

    def keeper_info(self, fresh: bool = False):
        """The running keeper's address, or None; looked up at most every 5 s."""
        if not self.use_keeper:
            return None
        from .desktop import stamp_alive
        at, info = self._keeper_seen
        if not fresh and time.time() - at < 5:
            return info
        try:
            info = json.loads((self.state / "keeper.json").read_text(encoding="utf-8"))
            if not stamp_alive(str(info.get("pid", ""))):
                info = None
        except (OSError, ValueError, AttributeError):
            info = None
        self._keeper_seen = (time.time(), info)
        return info

    def keeper_run(self, remote: str, data: bytes, timeout: float):
        """Run a command over the keeper's connection. None when no keeper takes it, and then nothing
        ran: the request goes in whole before the keeper starts it."""
        info = self.keeper_info()
        if not info:
            return None
        import socket
        try:
            sock = socket.create_connection(("127.0.0.1", int(info["port"])), timeout=3)
        except OSError:
            return None
        with sock:
            try:
                sock.settimeout(30)
                _send_frame(sock, json.dumps({"token": info["token"], "remote": remote, "timeout": timeout}).encode())
                _send_frame(sock, data or b"")
            except OSError:
                return None
            try:
                sock.settimeout(timeout + 30)
                meta = json.loads(recv_frame(sock))
                out, err = recv_frame(sock), recv_frame(sock)
            except (OSError, ValueError) as e:
                return subprocess.CompletedProcess(["keeper", remote], 254, b"", f"lost the keeper during the command: {e}".encode())
        if meta.get("not_started"):
            return None
        if meta.get("timeout"):
            raise TimeoutError(err.decode(errors="replace"))
        return subprocess.CompletedProcess(["keeper", remote], int(meta["code"]), out, err)

    # -- one command

    def run(self, remote: str, data: bytes = None, timeout: float = 600) -> subprocess.CompletedProcess:
        """Over the keeper's connection, else this process's own, else the ssh binary (which reopens the
        way and tries once more when its connection itself fails, exit 255). A command whose connection
        dropped once it was running is not run again: it may have done its work, so it returns 254."""
        r = self.keeper_run(remote, data, timeout)
        if r is not None:
            return r
        client = self.client()
        if client is not None:
            try:
                return paramiko_run(client, remote, data, timeout)
            except TimeoutError:
                raise
            except NotStarted:
                self.forget()
            except Exception as e:  # noqa: BLE001 - it dropped mid-command
                self.forget()
                return subprocess.CompletedProcess(["ssh", remote], 254, b"", f"connection lost during the command: {e}".encode())
        argv = self.ssh_argv(remote)
        r = run(argv, data, timeout=timeout)
        if r.returncode == 255:
            self.connect()
            r = run(argv, data, timeout=timeout)
        return r

    def upload_code(self) -> None:
        r = self.run(upload_command(), code_archive(), timeout=120)
        if r.returncode:
            raise SystemExit(f"could not put sessionlift's code on the server: {r.stderr.decode(errors='replace').strip()}")
        log("updated sessionlift's code on the server")

    def call(self, op: str, args: dict = None, data: bytes = b"", timeout: float = 600) -> Result:
        request = frame(args or {}, data)
        r = self.run(remote_command(op), request, timeout)
        if r.returncode == STALE_CODE:
            self.upload_code()
            r = self.run(remote_command(op), request, timeout)
        return Result(r.returncode, r.stdout, r.stderr)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if self._client is not None:
                self._client.close()
        self._client = None
