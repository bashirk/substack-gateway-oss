#!/bin/sh
set -eu

LABEL=com.substack-gateway.autopilot
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
RUNNER="$SCRIPT_DIR/run-autopilot-macos.sh"
ENV_FILE="$PROJECT_DIR/.env.autopilot"
LOG_DIR="$PROJECT_DIR/data/logs"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"

case "$PROJECT_DIR/" in
    "$HOME/Documents/"*|"$HOME/Desktop/"*|"$HOME/Downloads/"*)
        echo "The project is inside a macOS privacy-protected folder:" >&2
        echo "  $PROJECT_DIR" >&2
        echo "A background LaunchAgent may receive 'Operation not permitted'." >&2
        echo "Move or clone it under $HOME/Developer (or another non-protected" >&2
        echo "directory), update the absolute data paths in .env.autopilot, and" >&2
        echo "run this installer from the new location." >&2
        exit 1
        ;;
esac

UV_BIN=$(command -v uv || true)
if [ -z "$UV_BIN" ] || [ ! -x "$UV_BIN" ]; then
    echo "uv was not found. Install it and ensure command -v uv succeeds." >&2
    exit 1
fi
if [ ! -r "$ENV_FILE" ]; then
    echo "Missing $ENV_FILE. Copy autopilot.env.example and configure it first." >&2
    exit 1
fi
if [ ! -x "$RUNNER" ]; then
    echo "$RUNNER is not executable. Run: chmod +x scripts/*.sh" >&2
    exit 1
fi

mkdir -p "$PLIST_DIR" "$LOG_DIR" "$PROJECT_DIR/data/artifacts"
chmod 700 "$PROJECT_DIR/data" "$LOG_DIR" "$PROJECT_DIR/data/artifacts"
chmod 600 "$ENV_FILE"

xml_escape() {
    printf '%s' "$1" | sed \
        -e 's/&/\&amp;/g' \
        -e 's/</\&lt;/g' \
        -e 's/>/\&gt;/g' \
        -e 's/"/\&quot;/g' \
        -e "s/'/\&apos;/g"
}

RUNNER_XML=$(xml_escape "$RUNNER")
PROJECT_XML=$(xml_escape "$PROJECT_DIR")
UV_XML=$(xml_escape "$UV_BIN")
STDOUT_XML=$(xml_escape "$LOG_DIR/autopilot.log")
STDERR_XML=$(xml_escape "$LOG_DIR/autopilot.error.log")

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$RUNNER_XML</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_XML</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>UV_BIN</key>
        <string>$UV_XML</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>$STDOUT_XML</string>
    <key>StandardErrorPath</key>
    <string>$STDERR_XML</string>
</dict>
</plist>
EOF

chmod 600 "$PLIST"
plutil -lint "$PLIST"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "Installed and started $LABEL"
echo "Status: launchctl print $DOMAIN/$LABEL"
echo "Logs:   tail -f '$LOG_DIR/autopilot.log' '$LOG_DIR/autopilot.error.log'"
