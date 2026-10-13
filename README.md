# sessionlift

Move a live Claude Code session from your laptop to your own always-on Linux server, and back, as the
same session: the same id and the same transcript.

On the server the session runs in tmux with Claude Code's Remote Control on, so the Claude app on your
phone can drive it while the laptop is shut. When you are back at the laptop, you bring the session home
and carry on in the same window. In Claude Code it takes one command each way, `/to-server` and
`/from-server`, and no model is called to do it.

> **Unofficial.** Not affiliated with, endorsed by or supported by Anthropic. sessionlift relies on parts
> of Claude Code that are not a public API (see [What it relies on](#what-it-relies-on)), and a Claude
> Code update can break it. Read [Security](#security) before you give it a server.

## Why

Anthropic's cloud sessions reach the network only through an HTTP(S) proxy, so a session there cannot
SSH into your machines, and running them on your own hardware (self-hosted runners) needs a Team or
Enterprise plan. sessionlift takes the other route: your own server runs the session, with your
repository, your `~/.claude` (CLAUDE.md, skills, settings, MCP servers) and whatever access you chose to
give that server, and Remote Control connects it to the Claude app.

It was extracted from a private tool that moved sessions between one laptop and one cloud VM, and
generalised: everything specific to one setup is now configuration.
[claude-roam](https://github.com/bclement14/claude-roam) also moves Claude Code sessions between machines
over ssh.

## How it works

A Claude Code session is a transcript, `~/.claude/projects/<project>/<session id>.jsonl`, plus a folder
of side files (subagent transcripts, large tool results). It lives on one machine at a time.

- **To the server** (`/to-server`, `sessionlift send`): the laptop window is closed with Ctrl+D twice,
  never a typed `/exit`, which goes through the command menu. Moved from the sessions page, a turn in
  progress is interrupted with Esc first, and text left unsent in the prompt is saved to a draft file
  before the prompt is cleared. If Claude Code asks because background work is running, the answer is
  "Exit and stop tasks". Meanwhile the transcript is copied with its working-directory paths
  rewritten for the server, and `claude --resume <id> --remote-control <title>` starts in tmux there.
  Once its Remote Control link is up, the session is told what the move stopped, so it picks that up.
- **Back** (`/from-server`, `sessionlift back`): one trip to the server stops the session the same way,
  commits there and sends back only the end of the transcript the laptop lacks. The laptop takes the
  server's commits, and the window switches to the session with `/resume <id>`.
- **Only what is missing travels.** Each side compares its copy of a transcript with the other's by size
  and sha256, so a session that was on the server before sends only its new end.
- **Git: only the moving session's own work.** Uncommitted files are committed and pushed on both
  machines, but only those the moving session wrote: a file's last change has to fall inside one of the
  session's own Edit or Write calls on that file, or inside one of its foreground Bash commands that names
  the file, and no other session of the project can have written it then. Everything else stays
  uncommitted for whoever wrote it. The other checkout then takes origin's commits without touching
  anyone's uncommitted work: git refuses a fast-forward that would overwrite it, and a rebase runs only
  when no uncommitted file is one that origin changed.
- **Memory**: each project's auto memory is synced both ways on every move. The newer copy of a file
  wins, and `MEMORY.md` merges line by line against the version both sides last agreed on.
- **No model involved.** `/to-server` and `/from-server` are `UserPromptExpansion` hooks: the hook does
  the move and blocks the command, so Claude is never called and the window is left idle with an empty
  prompt, which is where Ctrl+D or `/resume` then goes.
- **The keeper** (optional, recommended): a background process on the laptop that holds one ssh
  connection open, so other commands skip the connection setup, and every 15 seconds copies ahead the
  sessions open on the laptop and the five closed most recently, so a move only has the last few seconds
  left to send. Without it, moves work the same, only slower.
- **The sessions page** (`sessionlift page`, or the "Claude sessions" desktop shortcut): one window
  listing the Claude Code windows open on the laptop, the sessions closed there today and those on the
  server, each moved by one click. "Move all to the server" moves every open window before the laptop
  goes off; the page then says when it is safe to shut down.

## Platforms

| | |
|---|---|
| Laptop: Windows 11, Claude Code in Windows Terminal | Works as built. sessionlift was developed and used here. |
| Laptop: macOS or Linux | Not supported yet. Parts of the code have POSIX versions (process checks, starting a background helper), but none of it has been run there. The slash commands, the sessions page, typing into windows and starting the keeper at logon exist only on Windows. |
| Server: Ubuntu 24.04, x86-64 | Works as built. |
| Server: other Linux | Not tested. It needs Python 3.8 or newer, tmux, git, bash and flock; `server/provision.sh` is written for Debian and Ubuntu. |

sessionlift's own tests run offline (see [Development](#development)). Moves against a real server and
real Claude Code windows were run with the tool sessionlift was extracted from, on the Windows 11 laptop
and Ubuntu 24.04 server above.

## Requirements

On the laptop:

- Windows 11, Python 3.10 or newer, git, and the OpenSSH client (`ssh`, part of Windows).
- Claude Code, in Windows Terminal or a console window. sessionlift was built against Claude Code
  2.1.296.
- Key-based ssh to the server. An entry in `~/.ssh/config` is the easiest; the server's host key must be
  in `~/.ssh/known_hosts`, so connect once with `ssh` first.

On the server:

- Linux with Python 3.8 or newer (standard library only), tmux, git, bash and flock, and a POSIX login
  shell (bash, dash or zsh). `server/provision.sh` installs all of it, and Claude Code.
- Claude Code signed in with a claude.ai account: `sessionlift login` does it from the laptop. Remote
  Control needs that full login; a `claude setup-token` token cannot run it.
- Push access to your repositories' remote (see [Server setup](#server-setup)).

## Server setup

1. **A machine that stays on**, reachable over ssh with your key: your own box or a small cloud VM. The
   numbers below come from a 2-vCPU, 8 GB VM with 4 GB of swap.
2. **Provision it.** The script needs sudo without a password prompt, and only fills in what is missing,
   so it is safe to run again:

   ```
   ssh myserver 'bash -s' < server/provision.sh
   ssh myserver 'bash -s -- --node --swap' < server/provision.sh     # with optional parts
   ```

   The base installs git, tmux, curl, jq, python3, ripgrep and build tools, then Claude Code, and puts
   `~/.local/bin` on the PATH (`~/.sessionlift-env`). Optional parts: `--node` (Node.js 22, for MCP
   servers started with npx), `--docker`, `--swap` (4 GB), `--desktop` (an Xfce desktop served by Chrome
   Remote Desktop) and `--browser` (the server's own Chrome, kept running in that desktop with DevTools
   on 127.0.0.1:9222; implies `--node`). The last two are amd64 only. `sessionlift setup --provision
   [STEP ...]` runs the same script from the laptop, in the background.
3. **Let the server push to your repositories.** Moves commit and push on the server too, so it needs its
   own key. Give it one that reaches only these repositories (on GitHub, a deploy key with write access
   per repository), not your personal key:

   ```
   ssh myserver 'ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -C sessionlift-server && cat ~/.ssh/id_ed25519.pub'
   ssh myserver 'ssh -o StrictHostKeyChecking=accept-new -T git@github.com'
   ```

4. **The server's desktop and Chrome** (only with `--desktop` and `--browser`): registering Chrome Remote
   Desktop takes your Google sign-in and a PIN, so it is done once by hand. On the laptop, open
   remotedesktop.google.com/headless and copy the Debian Linux command it gives; run it on the server in
   `ssh -t myserver`, and choose a PIN. The code expires after a few minutes. Then open the desktop from
   remotedesktop.google.com/access and, in the Chrome there, sign in to the sites sessions should use.
   With `browser_mcp = true`, `sessionlift setup` gives every session on the server a Playwright MCP
   server for that Chrome. The Chrome policy that `--browser` installs turns off its password manager and
   card autofill: it keeps site sessions, never passwords.

## Laptop setup

Install it (or `pip install .` from a checkout of this repository):

```
pip install "sessionlift @ git+https://github.com/marcosjunca06/sessionlift"
sessionlift init --host myserver --project "C:\code\myapp=/home/me/myapp"
sessionlift setup
sessionlift login
sessionlift login --code <the code the sign-in page shows>
sessionlift install
```

- `init` writes the config file, `%APPDATA%\sessionlift\config.toml`, with every setting shown and
  explained. A project is a directory where you run Claude Code on the laptop (`local`) and the same
  repository checked out on the server (`remote`); repeat `--project` for more. `--name` sets how
  messages and the page name the server, and `--gcp-instance`, `--gcp-zone` and `--firewall-rule` set up
  the optional Google Cloud provider (see [Configuration](#configuration)).
- `setup` puts sessionlift's server half on the server, clones each project there when its checkout is
  missing (set `clone_url`), copies `CLAUDE.md`, your skills, your settings and your MCP servers from
  `~/.claude`, the files listed in `copy_files`, and syncs memory. Run it again after adding a skill or an
  MCP server. Settings that only work on the laptop stay behind: the status line, voice, hooks that run
  Windows programs, MCP servers that start Windows programs, and bypass mode (see [Security](#security)).
- `login` signs Claude Code in on the server: it prints a link; approve, then pass the code it shows.
- `install` adds the `/to-server` and `/from-server` skills and their hooks to your user
  `~/.claude/settings.json`, backing the file up first (`settings.json.before-sessionlift`). Restart the
  Claude Code windows that are already open. `install --uninstall` takes them out again.

Optional:

```
sessionlift keeper --install      start the keeper at every logon (a scheduled task), and now
sessionlift shortcut              "Claude sessions" on the desktop: the sessions page
```

## Use

In Claude Code, in a configured project:

```
/to-server             this session moves to the server; its window closes and it continues in the Claude app
/from-server           pick a session on the server; it continues in this window
/from-server <id>      that one, without the list
```

`/from-server` always shows the list first, even when there is only one session there, and brings nothing
back unless you pick it; Cancel, Esc or five minutes without a click bring nothing back. It works from any
window in the session's project directory, a fresh `claude` included.

From a terminal:

```
sessionlift send [SESSION] [--now]       continue a laptop session on the server
sessionlift back [SESSION] [--open-tab]  bring one back; --open-tab continues it in a new terminal tab
sessionlift list                         the sessions on the server
sessionlift status                       what runs there: sessions and their links, disk, memory, git
sessionlift page                         the sessions page
sessionlift sync-memory                  sync each project's auto memory both ways
sessionlift open                         start the server and open its firewall (provider gcp)
sessionlift config                       where the config is, and what it says
```

SESSION is a session id or its first characters. Without one, `send` and `back` list the sessions and ask
which; they never pick one on their own. (Run by Claude inside a session, `send` without SESSION means
that session.) `send` refuses a session that is open in a window: move that one with `/to-server` in its
window, or from the page.

To watch a session on the server, `ssh -t myserver tmux attach -t sl-<first 8 characters of its id>`, and
detach with Ctrl-b d. Each move logs to `send-<first 8 characters>.log` or `back-<first 8 characters>.log`
in the state directory (`%LOCALAPPDATA%\sessionlift`); the hook, the keeper and the page log there too.

## Timings

Measured in October 2026 with the private tool sessionlift was extracted from, which ran the same moves:
a Windows 11 laptop and an Ubuntu 24.04 VM on Google Compute Engine (e2-standard-2), about 97 ms apart.

| | |
|---|---|
| `/to-server` until the session is live in the Claude app | 9.6 s. The floor is Claude Code's own start on the server, about 6 s, plus Remote Control connecting. |
| `/from-server` until the window has switched to the session | about 9 s |
| A command on the server over the keeper's open connection | 0.3-0.6 s, against 2.5 s over a fresh connection (1.5 s to connect, 1.0 s to open the first channel) |
| Uploads over paramiko | 0.56 MB/s with Windows' default 64 KB socket send buffer; 2.8 MB/s with the 4 MB buffer sessionlift sets |
| A 180 MB transcript sent whole, without the keeper's copy ahead | 4 minutes each way |

## Configuration

`sessionlift init` writes every key with its default, and refuses keys it does not know.

| Section | Key | Default | |
|---|---|---|---|
| `[server]` | `host` | (required) | an ssh host, or a Host alias in `~/.ssh/config` |
| | `user`, `port`, `identity_file` | from `~/.ssh/config` | |
| | `name` | the host | how messages and the page name the server |
| `[claude]` | `bypass_permissions` | `false` | see [Security](#security) |
| | `server_args` | `[]` | more arguments for `claude` on the server, such as `["--chrome"]` |
| | `laptop_args` | `[]` | arguments for `claude` when a session comes back in a new tab |
| | `remote_control_server` | `false` | a systemd service running `claude remote-control`, to start new sessions on the server from the Claude app (needs sudo without a password) |
| | `browser_mcp` | `false` | a Playwright MCP server for the server's own Chrome (`--browser`) |
| | `server_note` | `""` | text added to the server's `~/.claude/CLAUDE.md` |
| `[setup]` | `claude_md`, `skills`, `settings`, `mcp_servers` | `true` | what `setup` copies from `~/.claude` |
| `[provider]` | `kind` | `"none"` | `"gcp"`: sessionlift may start the instance and keep a firewall rule open for the laptop |
| | `instance`, `zone`, `project`, `gcloud_configuration` | | the instance, and how to call `gcloud` |
| | `firewall_rule` | `""` | a rule whose source ranges follow the laptop's public address; `""` leaves the firewall alone |
| | `keep_addresses` | `4` | how many of the laptop's recent addresses the rule keeps |
| `[[project]]` | `local`, `remote` | (required) | the laptop directory and the server checkout |
| | `name` | the last part of `local` | |
| | `git_sync`, `sync_memory` | `true` | commit and push the session's own work; sync its auto memory |
| | `copy_files` | `[]` | git-ignored files `setup` copies, such as `[".env"]` |
| | `clone_url` | `""` | `setup` clones it on the server when the checkout is missing |
| `[state]` | `dir` | `%LOCALAPPDATA%\sessionlift` | logs, pid files, the keeper's address, copy records |

| Variable | |
|---|---|
| `SESSIONLIFT_CONFIG` | the config file (or pass `--config`) |
| `SESSIONLIFT_HOME` | the state directory |
| `SESSIONLIFT_CLAUDE_DIR`, `SESSIONLIFT_CLAUDE_JSON` | Claude Code's `~/.claude` and `~/.claude.json` |

After changing the server in the config, stop the keeper (`sessionlift keeper --stop`); the next move
or hook starts it again.

## Security

sessionlift acts with your accounts on two machines. Know what it does before you run it.

**On the laptop** it reads the transcripts of the configured projects and Claude Code's window registry;
types into the Claude Code window a move is for, through the Windows console (Esc, Backspace, Ctrl+D,
`/resume <id>`, and a note about stopped work; a slash command only into an empty prompt); saves unsent
prompt text to the state directory before clearing it; commits and pushes the moving session's files in
the configured checkouts; writes two skills and two hooks into `~/.claude`; and starts background helpers
(through WMI, so they outlive the window they came from). On request it also registers a logon task and
makes a desktop shortcut. The sessions page has a button that shuts the laptop down; it shows only when
nothing is open or moving, and does nothing until clicked. With provider `gcp` it starts the instance
and edits the firewall rule with your gcloud credentials, and asks api.ipify.org for the laptop's public
address.

**On the server**, as the user you ssh in as, it:

- keeps its own copy of sessionlift's code (`~/.local/share/sessionlift/lib`, and `~/.local/bin/sessionlift`)
  and replaces it with the laptop's whenever the two differ: whoever can change the copy installed on
  the laptop can run code on the server;
- runs Claude Code in tmux, and with `remote_control_server` installs a systemd service through sudo;
- writes `~/.claude` (CLAUDE.md, skills, settings) and merges your MCP servers into `~/.claude.json`,
  with any keys in their arguments or environment;
- copies the `copy_files` (such as `.env`) into the checkouts, and commits and pushes there;
- keeps a copy of every session that went there, and the keeper's copies of the laptop's open and
  recently closed sessions in the configured projects, in `~/.claude/sessionlift-archive`. A transcript
  holds the whole conversation, including whatever the tools printed.

**Keys on the server.** The server needs its own way to push to your repositories. Give it a key that
reaches only those (a deploy key per repository, or a separate account), never your personal key: every
session on the server can use it, and so can anyone who gets into the server. Claude Code's login there
lives in `~/.claude/.credentials.json`; anyone signed in to the same Claude account can drive the
server's sessions from the Claude app. Your laptop's ssh key never leaves the laptop. sessionlift's own
connections trust only host keys already in `~/.ssh/known_hosts`, and it runs the ssh binary with
`BatchMode=yes`, so it never answers a prompt about an unknown host.

**The keeper's token.** The keeper listens on 127.0.0.1, on a port of its choosing, and runs any command
a local process sends it, over its open connection, if the request carries the token from `keeper.json`
in the state directory. A program running as you that can read that file can run commands on the server
as your server user. It could do the same with your ssh key, but this way no ssh-agent confirmation or
key passphrase stands in between, since the connection is already open. The file is readable only by you
(the state directory is in your Windows profile; on other systems the file is written 0600).
`sessionlift keeper --stop` closes the door, and `keeper --uninstall` also removes the logon task.

**Bypass mode is off by default.** With `bypass_permissions = true`, sessions on the server start with
`--dangerously-skip-permissions`, the server's `settings.json` makes `bypassPermissions` the default
mode, and the Remote Control service runs in that mode. No session there then asks before running a
command or editing a file, and nobody is at the server's keyboard to say no: whatever the server's user
can reach (the git key, the copied secrets, the server's Chrome with its logins) a session can use
unasked. Turn it on only for a server that holds nothing you would not hand to the model unsupervised.
With it off, a session that needs permission asks in the Claude app. The laptop's own bypass setting is
never copied to the server, and `server_args` cannot turn bypass mode on.

## What it relies on

sessionlift reads and drives parts of Claude Code that are not a public API. It was built against Claude
Code 2.1.296, and a later version may change any of these:

- **The window registry**, `~/.claude/sessions/<pid>.json`: which windows are open, the session each holds
  (it follows `/resume`), its working directory, and whether it is busy, waiting or idle.
- **The transcript layout**: `~/.claude/projects/<working directory, each other character a dash>/<id>.jsonl`
  and the folder beside it; the `cwd`, `uuid`, `timestamp`, `customTitle` and `aiTitle` fields, `tool_use`
  and `tool_result` records, and `"stop_reason":"end_turn"`.
- **UserPromptExpansion hooks**: the event's `command_name`, `command_args` and `session_id`, `CLAUDE_PID`
  in the hook's environment, and that a `block` decision ends the command without calling the model.
- **The terminal screen**: the input box between two rules and its `❯`, Esc to interrupt, Ctrl+D twice to
  exit, and the exit question "Background work is running" with "Exit and stop tasks".
- **Command-line flags and settings**: `claude --resume`, `--remote-control <name>`, `claude auth status`,
  `claude auth login --claudeai`, `claude remote-control --spawn worktree`; `remoteControlAtStartup` and
  `skipDangerousModePermissionPrompt` in `settings.json`; `hasCompletedOnboarding` and per-project
  `hasTrustDialogAccepted` in `~/.claude.json`.

## Limitations

- Windows laptops only so far (see [Platforms](#platforms)).
- Typing into a window needs a console. In VS Code's Claude Code panel or the desktop app, `/to-server`
  cannot close the window and moves nothing: exit there and run `sessionlift send <id>`. After
  `/from-server`, type `/resume <id>` yourself.
- Start Claude Code in the configured project directory itself. A session started in a subdirectory is
  kept under another project name in `~/.claude` and is not seen.
- Sessions started on the server from the Claude app (the Remote Control service and its worktrees) are
  not listed and cannot come back this way.
- A move takes a session as it is: a turn in progress is interrupted, and background agents and
  commands are stopped. The session on the other side is told which, but running processes do not
  travel.
- Only the `cwd` fields of a transcript are rewritten; paths inside older messages keep the other
  machine's form.
- A move never commits deleted files, untracked files over 20 MB, or files no session can be shown to
  have written; they stay uncommitted where they are.
- Memory files go by modification time: a file edited on both machines between two moves keeps only the
  newer edit (`MEMORY.md` itself merges by line).
- With `ProxyJump` or `ProxyCommand` in `~/.ssh/config`, every command goes through the ssh binary, and
  the keeper has no connection to hold.

## Development

```
pip install -e .
python -m unittest discover -s tests -v
```

Besides the package's own dependencies the tests need git, and nothing else: no server, no network, no
Claude Code. The server's operations run
in-process against a temporary home with a fake tmux, git runs against local repositories with a bare
one standing in for origin, and every location sessionlift would default to points into a temporary
directory. Anything that would open a window, start a background process or type into a console is
refused by the tests' fakes. The moves themselves, the typing into windows and the sessions page are
exercised by hand.

## License

MIT. Claude and Claude Code are trademarks of Anthropic.
