#!/bin/sh
set -eu

LABEL=com.substack-gateway.autopilot
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "Stopped and removed $LABEL. Local state and artifacts were preserved."
