#!/usr/bin/env bash
# Prepare a Debian or Ubuntu server for sessionlift: what a Claude Code session needs to keep running
# there under tmux with Remote Control. Optional parts are flags.
#
#   ssh myserver 'bash -s' < server/provision.sh                        the base only
#   ssh myserver 'bash -s -- --node --browser' < server/provision.sh    with optional parts
#   sessionlift setup --provision node browser                          the same from the laptop, in the background
#
#   (base)     git, tmux, curl, jq, python3, ripgrep, build tools; Claude Code; ~/.sessionlift-env (PATH)
#   --node     Node.js 22, for MCP servers started with npx
#   --docker   Docker, and this user in the docker group
#   --swap     a 4 GB swap file (small machines run out of memory during installs)
#   --desktop  an Xfce desktop served by Chrome Remote Desktop, to watch the server or clear a login for it
#              (amd64 only; the host is registered once by hand, see the README)
#   --browser  the server's own Chrome, kept running in that desktop with DevTools on 127.0.0.1:9222 for a
#              browser MCP server (implies --node; amd64 only). It keeps site sessions, never passwords
#              or cards: a policy turns the password manager and card autofill off.
#
# Needs sudo without a password prompt (the script runs non-interactively). Idempotent: every step
# checks before it installs, so re-running only fills in what is missing.
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
FAILED=()
NODE=0 DOCKER=0 SWAP=0 DESKTOP=0 BROWSER=0
for arg in "$@"; do
  case "$arg" in
    --node) NODE=1 ;;
    --docker) DOCKER=1 ;;
    --swap) SWAP=1 ;;
    --desktop) DESKTOP=1 ;;
    --browser) BROWSER=1; NODE=1 ;;
    *) echo "unknown option: $arg (known: --node --docker --swap --desktop --browser)"; exit 2 ;;
  esac
done
log() { printf '\n== %s ==\n' "$*"; }
step() { "$@" || { echo "!! FAILED: $*"; FAILED+=("$*"); }; }

amd64() {
  [ "$(dpkg --print-architecture)" = amd64 ] || { echo "this part is published for amd64 only"; return 1; }
}

apt_base() {
  sudo apt-get update -q &&
  sudo apt-get install -yq --no-install-recommends \
    git tmux curl wget jq unzip zip ripgrep build-essential ca-certificates gnupg util-linux \
    python3 python3-venv python3-pip
}

swap() {
  swapon --show | grep -q /swapfile && return 0
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile &&
  sudo mkswap /swapfile && sudo swapon /swapfile &&
  { grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null; }
}

node22() {
  command -v node >/dev/null && [[ "$(node -v)" == v22* ]] && return 0
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -yq nodejs
}

docker_engine() {
  command -v docker >/dev/null || sudo apt-get install -yq docker.io docker-compose-v2 || return 1
  sudo usermod -aG docker "$USER"
}

claude_code() {
  [ -x "$HOME/.local/bin/claude" ] && return 0
  curl -fsSL https://claude.ai/install.sh | bash
}

env_file() {
  # One place for the PATH, sourced by login shells and first thing by interactive bash, so ssh
  # commands, tmux and Claude Code's own shell all find claude in ~/.local/bin.
  cat > "$HOME/.sessionlift-env" <<'EOF'
# sessionlift: claude and the sessionlift shim live here (server/provision.sh)
export PATH="$HOME/.local/bin:$PATH"
EOF
  grep -q sessionlift-env "$HOME/.profile" 2>/dev/null || echo '. "$HOME/.sessionlift-env"' >> "$HOME/.profile"
  # First in .bashrc: Debian's returns early for non-interactive shells. sed inserts nothing into an
  # empty file, so an empty or missing one gets the line appended.
  if [ ! -s "$HOME/.bashrc" ]; then
    echo '. "$HOME/.sessionlift-env"' >> "$HOME/.bashrc"
  elif ! grep -q sessionlift-env "$HOME/.bashrc"; then
    sed -i '1i . "$HOME/.sessionlift-env"' "$HOME/.bashrc"
  fi
}

desktop() {
  # Google's recipe for Chrome Remote Desktop on Compute Engine, without its screen locker: a user with
  # no password could not unlock it. Registering the host takes your Google sign-in: see the README.
  amd64 || return 1
  if ! dpkg -s chrome-remote-desktop >/dev/null 2>&1; then
    wget -q -O /tmp/crd.deb https://dl.google.com/linux/direct/chrome-remote-desktop_current_amd64.deb &&
    sudo apt-get install -yq /tmp/crd.deb && rm -f /tmp/crd.deb || return 1
  fi
  sudo apt-get install -yq --no-install-recommends xfce4 xfce4-terminal desktop-base dbus-x11 &&
  { ! systemctl is-enabled lightdm >/dev/null 2>&1 || sudo systemctl disable lightdm; } &&
  echo 'exec /etc/X11/Xsession /usr/bin/xfce4-session' > "$HOME/.chrome-remote-desktop-session"
}

browser() {
  # The server's own Chrome, kept running inside the remote desktop so sessions have a signed-in browser
  # while the laptop is shut. Chrome ignores --remote-debugging-port on its default profile, hence a
  # profile of its own. Every session on the server can use this browser and read its profile.
  amd64 || return 1
  if ! command -v google-chrome >/dev/null; then
    wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb &&
    sudo apt-get install -yq /tmp/chrome.deb && rm -f /tmp/chrome.deb || return 1
  fi
  sudo mkdir -p /etc/opt/chrome/policies/managed &&
  sudo tee /etc/opt/chrome/policies/managed/sessionlift.json >/dev/null <<'EOF' || return 1
{
  "PasswordManagerEnabled": false,
  "AutofillCreditCardEnabled": false,
  "SyncTypesListDisabled": ["passwords", "autofill"]
}
EOF
  mkdir -p "$HOME/.local/bin" "$HOME/.config/autostart" &&
  cat > "$HOME/.local/bin/sessionlift-chrome" <<'EOF' &&
#!/usr/bin/env bash
# Keeps the server's Chrome running in the remote desktop session (server/provision.sh).
# The profile's SingletonLock names the pid of the Chrome that holds it.
profile="$HOME/.config/sessionlift-chrome"
xset s off -dpms 2>/dev/null
while true; do
  lock=$(readlink "$profile/SingletonLock" 2>/dev/null)
  if ! { [ -n "$lock" ] && kill -0 "${lock##*-}" 2>/dev/null; }; then
    google-chrome --user-data-dir="$profile" --remote-debugging-port=9222 --password-store=basic >/dev/null 2>&1 &
  fi
  sleep 15
done
EOF
  chmod +x "$HOME/.local/bin/sessionlift-chrome" &&
  printf '[Desktop Entry]\nType=Application\nName=Chrome, kept running\nExec=%s\n' \
    "$HOME/.local/bin/sessionlift-chrome" > "$HOME/.config/autostart/sessionlift-chrome.desktop" &&
  # Fetch the pinned Playwright MCP now, so a session's first start does not wait for npm.
  npx -y @playwright/mcp@0.0.83 --help >/dev/null
}

log "apt packages";     step apt_base
[ "$SWAP" = 1 ] &&      { log "swap"; step swap; }
[ "$NODE" = 1 ] &&      { log "Node 22"; step node22; }
[ "$DOCKER" = 1 ] &&    { log "Docker"; step docker_engine; }
log "Claude Code";      step claude_code
log "PATH";             step env_file
[ "$DESKTOP" = 1 ] &&   { log "remote desktop"; step desktop; }
[ "$BROWSER" = 1 ] &&   { log "the server's Chrome"; step browser; }

log "summary"
echo "claude:  $("$HOME/.local/bin/claude" --version 2>/dev/null || echo missing)"
echo "python3: $(python3 --version 2>/dev/null || echo missing)"
echo "tmux:    $(tmux -V 2>/dev/null || echo missing)"
[ "$NODE" = 1 ] && echo "node:    $(node -v 2>/dev/null || echo missing)"
[ -f /var/run/reboot-required ] && echo "reboot required (kernel update)"
if [ ${#FAILED[@]} -gt 0 ]; then printf 'FAILED: %s\n' "${FAILED[@]}"; exit 1; fi
echo "provision OK"
