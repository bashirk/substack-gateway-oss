#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
ENV_FILE="$PROJECT_DIR/.env.autopilot"
UV_BIN=${UV_BIN:-/usr/local/bin/uv}

if [ ! -x "$UV_BIN" ]; then
    echo "uv is not executable at $UV_BIN" >&2
    echo "Reinstall the LaunchAgent with scripts/install-launchd-agent.sh." >&2
    exit 1
fi

if [ ! -r "$ENV_FILE" ]; then
    echo "Missing or unreadable environment file: $ENV_FILE" >&2
    exit 1
fi

mkdir -p "$PROJECT_DIR/data/artifacts" "$PROJECT_DIR/data/.uv-cache"
cd "$PROJECT_DIR"

export UV_CACHE_DIR="$PROJECT_DIR/data/.uv-cache"
exec "$UV_BIN" run --env-file "$ENV_FILE" substack-autopilot
