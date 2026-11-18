#!/usr/bin/env bash
# Build agent-screen in release mode, sign it, install it on PATH, and write
# its launch agent. Loading the launch agent is left to you (see README.md).
set -euo pipefail
cd "$(dirname "$0")"

swift build -c release --product agent-screen
BIN="$(swift build -c release --show-bin-path)/agent-screen"

# An ad-hoc signature changes on every build, which voids the Accessibility and
# Screen Recording grants; a certificate keeps them across rebuilds.
IDENTITY="${AGENT_SCREEN_SIGN_IDENTITY:-Apple Development: Created via API (F6A2298LR5)}"
codesign --force --identifier dev.aktan.agent-screen --sign "$IDENTITY" "$BIN"
codesign --verify --strict "$BIN"

DEST="$HOME/.local/bin"
mkdir -p "$DEST" "$HOME/Library/Logs/agent-screen" "$HOME/Library/LaunchAgents"
install -m 0755 "$BIN" "$DEST/agent-screen"
sed "s|__HOME__|$HOME|g" dev.aktan.agent-screen.plist > "$HOME/Library/LaunchAgents/dev.aktan.agent-screen.plist"
echo "Installed $DEST/agent-screen and ~/Library/LaunchAgents/dev.aktan.agent-screen.plist"
