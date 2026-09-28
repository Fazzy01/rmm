#!/bin/bash
# macOS installer for the RMM agent (agent.py). Run from a Terminal with:
#   chmod +x install-macos.sh && ./install-macos.sh
#
# What it does:
#   1. Copies agent.py + requirements.txt into ~/Library/Application
#      Support/RmmAgent, creates a venv there, installs dependencies.
#   2. Registers a LaunchAgent (~/Library/LaunchAgents/com.rmm.agent.plist)
#      so the agent starts automatically at login and restarts if it
#      crashes (KeepAlive) - the closest macOS equivalent of the Windows
#      installer's "ONLOGON" Scheduled Task.
#   3. Opens the two System Settings panes the agent needs access granted
#      in by hand: Screen Recording (so screen capture works at all) and
#      Accessibility (so remote mouse/keyboard control works). macOS's TCC
#      privacy subsystem deliberately does not let any script grant these
#      to itself - same one-time-consent model as Android's permission
#      prompts elsewhere in this project. Until both are checked, the
#      agent will connect and accept shell commands fine, but screen
#      frames and remote input silently won't work.
#
# Edit SERVER_URL / TOKEN below (or pass them as the first two arguments)
# before running. Works two ways:
#   - run locally from a repo checkout (finds agent.py next to this script)
#   - piped straight from the server: curl -fsSL <server>/install-macos.sh | bash
#     (no local files, so it downloads agent.py + requirements.txt itself,
#     from DOWNLOAD_BASE below - same host as SERVER_URL, over plain http).

set -euo pipefail

SERVER_URL="${1:-${SERVER_URL:-ws://192.227.246.68:8000}}"
TOKEN="${2:-${TOKEN:-}}"
DOWNLOAD_BASE="${DOWNLOAD_BASE:-http://192.227.246.68:8000}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
AGENT_SRC_DIR="${SCRIPT_DIR:+$(dirname "$SCRIPT_DIR")}"   # agents/python-agent, when run locally
APP_DIR="$HOME/Library/Application Support/RmmAgent"
PLIST_PATH="$HOME/Library/LaunchAgents/com.rmm.agent.plist"
DEVICE_NAME="$(scutil --get ComputerName 2>/dev/null || hostname)"

echo "Installing RMM agent to: $APP_DIR"
mkdir -p "$APP_DIR"
if [ -n "$AGENT_SRC_DIR" ] && [ -f "$AGENT_SRC_DIR/agent.py" ]; then
    cp "$AGENT_SRC_DIR/agent.py" "$APP_DIR/agent.py"
    cp "$AGENT_SRC_DIR/requirements.txt" "$APP_DIR/requirements.txt"
else
    echo "No local checkout found - downloading agent.py from $DOWNLOAD_BASE ..."
    curl -fsSL "$DOWNLOAD_BASE/agent.py" -o "$APP_DIR/agent.py"
    curl -fsSL "$DOWNLOAD_BASE/requirements.txt" -o "$APP_DIR/requirements.txt"
fi

echo "Setting up Python virtual environment..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

PYTHON_BIN="$APP_DIR/venv/bin/python3"

echo "Registering LaunchAgent (auto-start at login)..."
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.rmm.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_BIN</string>
        <string>$APP_DIR/agent.py</string>
        <string>--server</string>
        <string>$SERVER_URL</string>
        <string>--device</string>
        <string>$DEVICE_NAME</string>
        <string>--token</string>
        <string>$TOKEN</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$APP_DIR/agent.log</string>
    <key>StandardErrorPath</key>
    <string>$APP_DIR/agent.log</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load -w "$PLIST_PATH"

echo ""
echo "Agent installed and started as device '$DEVICE_NAME' -> $SERVER_URL"
echo ""
echo "IMPORTANT - one-time manual step required by macOS:"
echo "  Two System Settings panes are about to open. In each, find and"
echo "  enable the entry for 'python3' (or add it with the + button if"
echo "  it isn't listed yet, pointing at: $PYTHON_BIN):"
echo "    - Screen Recording  (needed for the remote screen view)"
echo "    - Accessibility     (needed for remote mouse/keyboard control)"
echo ""
if [ -t 0 ]; then
    read -p "Press Enter to open those panes now..." _
else
    echo "(running non-interactively - opening System Settings panes now)"
fi

open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
sleep 1
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"

echo ""
echo "After granting both, restart the agent so the grants take effect:"
echo "  launchctl kickstart -k gui/\$(id -u)/com.rmm.agent"
