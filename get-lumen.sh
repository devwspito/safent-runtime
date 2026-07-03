#!/bin/sh
# Lumen - one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/devwspito/lumen-runtime/main/get-lumen.sh | sh
#
# Installs the `lumen` command on your PATH, then starts Lumen with the security
# cage (loopback) and opens your browser at this boot's unique token. Afterwards,
# control it from the terminal:
#   lumen          open it          lumen stop     stop it
#   lumen update   update it        lumen status   status
# The model, Composio, Brave, agents and skills are all configured IN THE UI.
#
# ASCII-only + POSIX sh on purpose (portable across macOS bash 3.2 / dash / zsh-as-sh).
set -e

LUMEN_CLI_URL="${LUMEN_CLI_URL:-https://raw.githubusercontent.com/devwspito/lumen-runtime/main/lumen}"

command -v curl >/dev/null 2>&1 || { echo "[x] You need curl."; exit 1; }

OS="$(uname -s 2>/dev/null || echo unknown)"

# Install the background update agent so updates can be triggered FROM THE UI (no
# terminal). The container can't recreate itself (sandbox), so `lumen agent` runs on
# the host, watches for a UI-written "update requested" marker, and applies it. Runs
# once at install; idempotent; fail-soft (Lumen works fine without it — you can still
# `lumen update` by hand). Only ever runs the podman the user already runs.
install_agent() {
  mkdir -p "$HOME/.lumen" 2>/dev/null || true
  case "$OS" in
    Darwin)
      _pl="$HOME/Library/LaunchAgents/run.lumen.agent.plist"
      mkdir -p "$HOME/Library/LaunchAgents" 2>/dev/null || return 0
      cat > "$_pl" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>run.lumen.agent</string>
  <key>ProgramArguments</key><array><string>$BIN/lumen</string><string>agent</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.lumen/agent.log</string>
  <key>StandardErrorPath</key><string>$HOME/.lumen/agent.log</string>
</dict></plist>
PLIST
      launchctl unload "$_pl" >/dev/null 2>&1 || true
      launchctl load "$_pl" >/dev/null 2>&1 \
        && echo "[ok] UI-triggered updates enabled (background agent)." || true
      ;;
    Linux)
      if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
        _u="$HOME/.config/systemd/user"; mkdir -p "$_u" 2>/dev/null || true
        cat > "$_u/lumen-agent.service" <<UNIT
[Unit]
Description=Lumen UI-triggered update agent
[Service]
ExecStart=$BIN/lumen agent
Restart=always
RestartSec=10
[Install]
WantedBy=default.target
UNIT
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        systemctl --user enable --now lumen-agent.service >/dev/null 2>&1 \
          && echo "[ok] UI-triggered updates enabled (systemd --user)." || true
      else
        pgrep -f "$BIN/lumen agent" >/dev/null 2>&1 \
          || nohup "$BIN/lumen" agent >"$HOME/.lumen/agent.log" 2>&1 &
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

echo "[*] Installing the 'lumen' command into $BIN ..."
if ! curl -fsSL "$LUMEN_CLI_URL" -o "$BIN/lumen"; then
  echo "[x] Could not download the CLI ($LUMEN_CLI_URL)."
  exit 1
fi
chmod +x "$BIN/lumen"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    echo "[!] $BIN is not on your PATH. Add it (then restart your shell):"
    echo "      export PATH=\"$BIN:\$PATH\""
    ;;
esac

# First run: pull the image, run it with the cage, open the browser.
# (Forwards LUMEN_IMAGE / LUMEN_PORT / LUMEN_SECCOMP_URL if you exported them.)
"$BIN/lumen" update

# Enable UI-triggered updates (background agent). Best-effort; never blocks install.
install_agent || true

# Enterprise pairing: if LUMEN_PAIR_CODE is set, associate after the first run.
# The code is copied to a local variable and the env var is unset immediately
# so it does not persist in the shell or appear in child process environments.
# `lumen pair` internally passes the code to the container via stdin (not argv).
if [ -n "${LUMEN_PAIR_CODE:-}" ]; then
  _pair_code="$LUMEN_PAIR_CODE"
  unset LUMEN_PAIR_CODE
  echo "[*] Pairing with enterprise code (from env)..."
  "$BIN/lumen" pair "$_pair_code"
  unset _pair_code
fi
