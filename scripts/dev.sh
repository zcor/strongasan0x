#!/bin/sh
# Local dev server, reachable on both localhost and the LAN (for phone testing).
# Usage: ./scripts/dev.sh [port]   (default port 8001)
set -e
cd "$(dirname "$0")/.."

PORT="${1:-8001}"
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"

HOSTS="localhost,127.0.0.1"
if [ -n "$LAN_IP" ]; then
    HOSTS="$HOSTS,$LAN_IP"
    echo "Phone (same Wi-Fi): http://$LAN_IP:$PORT/"
else
    echo "No LAN IP found; serving on localhost only."
fi
echo "Local:              http://127.0.0.1:$PORT/"

ALLOWED_HOSTS="$HOSTS" exec .venv/bin/python manage.py runserver "0.0.0.0:$PORT"
