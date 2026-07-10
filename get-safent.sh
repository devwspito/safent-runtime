#!/bin/sh
# Safent - one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/devwspito/safent-runtime/main/get-safent.sh | sh
#
# Installs the `safent` command on your PATH, then starts Safent with the security
# cage (loopback) and opens your browser at this boot's unique token. Afterwards,
# control it from the terminal:
#   safent          open it          safent stop     stop it
#   safent update   update it        safent status   status
# The model, Composio, Brave, agents and skills are all configured IN THE UI.
#
# ASCII-only + POSIX sh on purpose (portable across macOS bash 3.2 / dash / zsh-as-sh).
set -e

SAFENT_CLI_URL="${SAFENT_CLI_URL:-https://raw.githubusercontent.com/devwspito/safent-runtime/main/safent}"

command -v curl >/dev/null 2>&1 || { echo "[x] You need curl."; exit 1; }

OS="$(uname -s 2>/dev/null || echo unknown)"

# Install the background update agent so updates can be triggered FROM THE UI (no
# terminal). The container can't recreate itself (sandbox), so `safent agent` runs on
# the host, watches for a UI-written "update requested" marker, and applies it. Runs
# once at install; idempotent; fail-soft (Safent works fine without it — you can still
# `safent update` by hand). Only ever runs the podman the user already runs.
install_agent() {
  mkdir -p "$HOME/.safent" 2>/dev/null || true
  case "$OS" in
    Darwin)
      _pl="$HOME/Library/LaunchAgents/run.safent.agent.plist"
      mkdir -p "$HOME/Library/LaunchAgents" 2>/dev/null || return 0
      cat > "$_pl" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>run.safent.agent</string>
  <key>ProgramArguments</key><array><string>$BIN/safent</string><string>agent</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/opt/podman/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.safent/agent.log</string>
  <key>StandardErrorPath</key><string>$HOME/.safent/agent.log</string>
</dict></plist>
PLIST
      launchctl unload "$_pl" >/dev/null 2>&1 || true
      launchctl load "$_pl" >/dev/null 2>&1 \
        && echo "[ok] UI-triggered updates enabled (background agent)." || true
      ;;
    Linux)
      if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
        _u="$HOME/.config/systemd/user"; mkdir -p "$_u" 2>/dev/null || true
        cat > "$_u/safent-agent.service" <<UNIT
[Unit]
Description=Safent UI-triggered update agent
[Service]
ExecStart=$BIN/safent agent
Restart=always
RestartSec=10
[Install]
WantedBy=default.target
UNIT
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        systemctl --user enable --now safent-agent.service >/dev/null 2>&1 \
          && echo "[ok] UI-triggered updates enabled (systemd --user)." || true
      else
        pgrep -f "$BIN/safent agent" >/dev/null 2>&1 \
          || nohup "$BIN/safent" agent >"$HOME/.safent/agent.log" 2>&1 &
      fi
      ;;
  esac
}

# Pick a writable PATH dir (no sudo). Prefer one already on PATH; else ~/.local/bin.
BIN=""
for d in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin" "$HOME/bin"; do
  case ":$PATH:" in
    *":$d:"*)
      if mkdir -p "$d" 2>/dev/null && [ -w "$d" ]; then BIN="$d"; break; fi
      ;;
  esac
done
[ -n "$BIN" ] || BIN="$HOME/.local/bin"
mkdir -p "$BIN" 2>/dev/null || { echo "[x] Could not create $BIN."; exit 1; }

echo "[*] Installing the 'safent' command into $BIN ..."
if ! curl -fsSL "$SAFENT_CLI_URL" -o "$BIN/safent"; then
  echo "[x] Could not download the CLI ($SAFENT_CLI_URL)."
  exit 1
fi
chmod +x "$BIN/safent"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    echo "[!] $BIN is not on your PATH. Add it (then restart your shell):"
    echo "      export PATH=\"$BIN:\$PATH\""
    ;;
esac

# First run: pull the image, run it with the cage, open the browser.
# (Forwards SAFENT_IMAGE / SAFENT_PORT / SAFENT_SECCOMP_URL if you exported them.)
"$BIN/safent" update

# Enable UI-triggered updates (background agent). Best-effort; never blocks install.
install_agent || true

# Enterprise pairing: if SAFENT_PAIR_CODE is set, associate after the first run.
# The code is copied to a local variable and the env var is unset immediately
# so it does not persist in the shell or appear in child process environments.
# `safent pair` internally passes the code to the container via stdin (not argv).
if [ -n "${SAFENT_PAIR_CODE:-}" ]; then
  _pair_code="$SAFENT_PAIR_CODE"
  unset SAFENT_PAIR_CODE
  echo "[*] Pairing with enterprise code (from env)..."
  "$BIN/safent" pair "$_pair_code"
  unset _pair_code
fi
