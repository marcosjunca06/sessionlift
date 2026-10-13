"""The configuration file: which server, which projects, what a session may do there.

`sessionlift init` writes it (see `template`); everything private to one setup lives here and nowhere in
the code. Unknown keys are refused, so a typo cannot silently fall back to a default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import paths


def _toml():
    """tomllib, or tomli on Python 3.10. Imported only when a config is read, so the copy of this
    package on the server never needs either."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    return tomllib


class ConfigError(SystemExit):
    """A config that cannot be used; the message says why and where."""


@dataclass
class Project:
    """A working directory that sessions move with: the laptop path Claude Code runs in, and the
    checkout of the same repository on the server."""
    name: str
    local: str
    remote: str
    git_sync: bool = True      # commit and push the moving session's own work, catch the other side up
    sync_memory: bool = True   # two-way sync of this project's auto memory on every move
    copy_files: list[str] = field(default_factory=list)  # git-ignored files `setup` copies (.env, say)
    clone_url: str = ""        # `setup` clones it on the server when the checkout is missing


@dataclass
class Provider:
    """Optional: a cloud machine sessionlift may start, and a firewall rule it keeps open for the
    laptop's current address. kind "none" means the server is simply reachable over ssh."""
    kind: str = "none"         # none | gcp
    instance: str = ""
    zone: str = ""
    project: str = ""          # GCP project id; "" = gcloud's default
    gcloud_configuration: str = ""
    firewall_rule: str = ""    # "" = leave the firewall alone
    keep_addresses: int = 4    # the laptop moves between networks: keep its last few addresses


@dataclass
class Config:
    path: Path
    host: str                  # an ssh host name, or a Host alias in ~/.ssh/config
    projects: list[Project]
    user: str = ""
    port: int = 0
    identity_file: str = ""
    name: str = ""
    bypass_permissions: bool = False
    server_args: list[str] = field(default_factory=list)
    laptop_args: list[str] = field(default_factory=list)
    remote_control_server: bool = False
    browser_mcp: bool = False
    server_note: str = ""
    copy_claude_md: bool = True
    copy_skills: bool = True
    copy_settings: bool = True
    copy_mcp_servers: bool = True
    provider: Provider = field(default_factory=Provider)
    state: str = ""

    @property
    def label(self) -> str:
        """How messages and the page name the server."""
        return self.name or self.host

    @property
    def state_dir(self) -> Path:
        return Path(self.state).expanduser() if self.state else paths.default_state_dir()

    def project_named(self, name: str) -> Project:
        for p in self.projects:
            if p.name == name:
                return p
        raise ConfigError(f"no project named {name!r} in {self.path}")


SECTIONS = {
    "server": {"host": str, "user": str, "port": int, "identity_file": str, "name": str},
    "claude": {"bypass_permissions": bool, "server_args": list, "laptop_args": list,
               "remote_control_server": bool, "browser_mcp": bool, "server_note": str},
    "setup": {"claude_md": bool, "skills": bool, "settings": bool, "mcp_servers": bool},
    "provider": {"kind": str, "instance": str, "zone": str, "project": str, "gcloud_configuration": str,
                 "firewall_rule": str, "keep_addresses": int},
    "state": {"dir": str},
}
PROJECT_KEYS = {"name": str, "local": str, "remote": str, "git_sync": bool, "sync_memory": bool,
                "copy_files": list, "clone_url": str}
NAME = re.compile(r"[A-Za-z0-9._-]+")
# Arguments that would turn bypass mode on behind the back of `bypass_permissions`.
BYPASS_ARGS = re.compile(r"--(allow-)?dangerously-skip-permissions|--permission-mode(=|\s+)bypassPermissions")


def default_name(local: str) -> str:
    """A project's name when the config gives none: the last part of its laptop path, in the characters
    a name may hold."""
    last = re.split(r"[\\/]", local.rstrip("\\/"))[-1]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", last).strip("-")


def _check(where: str, table: dict, keys: dict, path: Path) -> dict:
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [{where}] must be a table")
    for key, value in table.items():
        if key not in keys:
            raise ConfigError(f"{path}: unknown key {key!r} in [{where}] (known: {', '.join(sorted(keys))})")
        want = keys[key]
        if want is int and (isinstance(value, bool) or not isinstance(value, int)):
            raise ConfigError(f"{path}: [{where}] {key} must be a whole number")
        if want is not int and not isinstance(value, want):
            raise ConfigError(f"{path}: [{where}] {key} must be a {want.__name__}")
        if want is list and not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{path}: [{where}] {key} must be a list of strings")
    return table


def is_windows_path(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith("\\\\")


def from_dict(data: dict, path: Path) -> Config:
    for key in data:
        if key not in SECTIONS and key != "project":
            raise ConfigError(f"{path}: unknown section [{key}] (known: {', '.join(sorted([*SECTIONS, 'project']))})")
    tables = {name: _check(name, data.get(name, {}), keys, path) for name, keys in SECTIONS.items()}
    server, claude, setup, prov = tables["server"], tables["claude"], tables["setup"], tables["provider"]
    host = server.get("host", "").strip()
    if not host:
        raise ConfigError(f"{path}: [server] host is required (an ssh host, or a Host alias in ~/.ssh/config)")
    raw_projects = data.get("project", [])
    if not isinstance(raw_projects, list) or not raw_projects:
        raise ConfigError(f"{path}: add at least one [[project]] with local and remote paths")
    projects = []
    for i, raw in enumerate(raw_projects):
        raw = _check(f"project {i + 1}", raw, PROJECT_KEYS, path)
        local, remote = raw.get("local", "").strip(), raw.get("remote", "").strip()
        if not local or not remote:
            raise ConfigError(f"{path}: project {i + 1} needs both local and remote")
        if not (is_windows_path(local) or local.startswith("/")):
            raise ConfigError(f"{path}: project {i + 1}: local must be an absolute path, not {local!r}")
        if not remote.startswith("/"):
            raise ConfigError(f"{path}: project {i + 1}: remote must be an absolute path on the server, not {remote!r}")
        local = local.rstrip("\\/") or local
        remote = remote.rstrip("/") or remote
        name = raw.get("name") or default_name(local) or f"project{i + 1}"
        if not NAME.fullmatch(name):
            raise ConfigError(f"{path}: project name {name!r} may only hold letters, digits, '.', '_' and '-'")
        projects.append(Project(name=name, local=local, remote=remote, git_sync=raw.get("git_sync", True),
                                sync_memory=raw.get("sync_memory", True), copy_files=list(raw.get("copy_files", [])),
                                clone_url=raw.get("clone_url", "")))
    for attr in ("name", "local", "remote"):
        seen = [getattr(p, attr).lower() if attr == "local" else getattr(p, attr) for p in projects]
        if len(set(seen)) != len(seen):
            raise ConfigError(f"{path}: two projects share the same {attr}")
    provider = Provider(**{k: v for k, v in prov.items()})
    if provider.kind not in ("none", "gcp"):
        raise ConfigError(f"{path}: [provider] kind must be \"none\" or \"gcp\", not {provider.kind!r}")
    if provider.kind == "gcp" and not (provider.instance and provider.zone):
        raise ConfigError(f"{path}: [provider] kind \"gcp\" needs instance and zone")
    if provider.keep_addresses < 1:
        raise ConfigError(f"{path}: [provider] keep_addresses must be at least 1")
    if BYPASS_ARGS.search(" ".join(claude.get("server_args", []))):
        raise ConfigError(f"{path}: [claude] server_args may not skip permission prompts; "
                          "set bypass_permissions = true instead (read Security in the README first)")
    return Config(path=path, host=host, projects=projects, user=server.get("user", ""), port=server.get("port", 0),
                  identity_file=server.get("identity_file", ""), name=server.get("name", ""),
                  bypass_permissions=claude.get("bypass_permissions", False),
                  server_args=list(claude.get("server_args", [])), laptop_args=list(claude.get("laptop_args", [])),
                  remote_control_server=claude.get("remote_control_server", False),
                  browser_mcp=claude.get("browser_mcp", False), server_note=claude.get("server_note", ""),
                  copy_claude_md=setup.get("claude_md", True), copy_skills=setup.get("skills", True),
                  copy_settings=setup.get("settings", True), copy_mcp_servers=setup.get("mcp_servers", True),
                  provider=provider, state=tables["state"].get("dir", ""))


def load(path: Path | None = None) -> Config:
    path = Path(path) if path else paths.config_path()
    if not path.exists():
        raise ConfigError(f"no configuration at {path}: run `sessionlift init` first")
    return parse(path.read_text(encoding="utf-8"), path)


def parse(text: str, path: Path) -> Config:
    toml = _toml()
    try:
        data = toml.loads(text)
    except toml.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return from_dict(data, path)


# ---------------------------------------------------------------- writing

def toml_str(text: str) -> str:
    """A TOML string for any text: a literal string ('...', backslashes as they are) when it can be
    one, else a basic string with escapes."""
    if "'" not in text and not any(ch in text for ch in "\n\r\t") and all(ord(ch) >= 32 for ch in text):
        return f"'{text}'"
    out = []
    for ch in text:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def template(host: str, projects: list[tuple[str, str]], *, user: str = "", port: int = 0, identity_file: str = "",
             name: str = "", provider: dict | None = None) -> str:
    """The config `sessionlift init` writes, with every setting shown and explained."""
    provider = provider or {}
    lines = [
        "# sessionlift configuration. `sessionlift init` wrote it; edit it freely.",
        "# Every key is shown with its default. The README explains each one.",
        "",
        "[server]",
        f"host = {toml_str(host)}            # an ssh host, or a Host alias in ~/.ssh/config",
        f"user = {toml_str(user)}                # \"\" = from ~/.ssh/config",
        f"port = {port}                 # 0 = from ~/.ssh/config, else 22",
        f"identity_file = {toml_str(identity_file)}       # \"\" = from ~/.ssh/config",
        f"name = {toml_str(name)}                # how messages and the sessions page name it; \"\" = the host",
        "",
        "[claude]",
        "# true: sessions on the server skip every permission prompt (--dangerously-skip-permissions).",
        "# Read \"Security\" in the README first. Nobody is at the server's keyboard to say no.",
        "bypass_permissions = false",
        "server_args = []                # more arguments for claude on the server, e.g. [\"--chrome\"]",
        "laptop_args = []                # for claude when a session comes back to a new terminal tab",
        "remote_control_server = false   # a systemd service: start new sessions on the server from the Claude app",
        "browser_mcp = false             # setup adds a Playwright MCP server for the server's own Chrome",
        "server_note = \"\"                # text appended to the server's ~/.claude/CLAUDE.md",
        "",
        "[setup]                         # what `sessionlift setup` copies from this laptop's ~/.claude",
        "claude_md = true",
        "skills = true",
        "settings = true",
        "mcp_servers = true",
        "",
        "[provider]",
        f"kind = {toml_str(provider.get('kind', 'none'))}                 # \"none\", or \"gcp\": start the instance and open its firewall",
    ]
    for key in ("instance", "zone", "project", "gcloud_configuration", "firewall_rule"):
        value = provider.get(key, "")
        lines.append(f"{key} = {toml_str(value)}" if value else f"# {key} = \"\"")
    lines.append(f"# keep_addresses = {provider.get('keep_addresses', 4)}")
    for i, (local, remote) in enumerate(projects):
        project_name = default_name(local) or f"project{i + 1}"
        lines += [
            "",
            "[[project]]",
            f"name = {toml_str(project_name)}",
            f"local = {toml_str(local)}       # where Claude Code runs on this laptop",
            f"remote = {toml_str(remote)}      # the same repository, checked out on the server",
            "git_sync = true                 # commit and push the moving session's own uncommitted work",
            "sync_memory = true              # two-way sync of this project's auto memory",
            "copy_files = []                 # git-ignored files `setup` copies, e.g. [\".env\"]",
            "clone_url = \"\"                  # `setup` clones it on the server when the checkout is missing",
        ]
    return "\n".join(lines) + "\n"
