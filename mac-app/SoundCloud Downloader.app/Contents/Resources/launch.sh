#!/bin/bash
# SoundCloud Downloader launcher (updatable: this file comes from the GitHub repo).
# Sets up a private Python environment on first run, starts the local web page, opens the browser.
set -u
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="$HOME/Library/Application Support/SoundCloud Downloader"
VENV="$HOME_DIR/venv"
PORT=8765
APP_VERSION="$(cat "$APP_DIR/VERSION" 2>/dev/null || echo dev)"
mkdir -p "$HOME_DIR"

have() { command -v "$1" >/dev/null 2>&1; }
notify() { echo "[notify] $1"; have osascript && osascript -e "display notification \"$1\" with title \"SoundCloud Downloader\"" >/dev/null 2>&1; }
fail() { echo "[fail] $1"; have osascript && osascript -e "display dialog \"$1\" with title \"SoundCloud Downloader\" buttons {\"OK\"} with icon stop" >/dev/null 2>&1; exit 1; }
open_page() { have open && open "http://localhost:$PORT"; }

echo "=== $(date) launch.sh version $APP_VERSION from $APP_DIR"

# Already running? Same version: just open the page. Older version: restart it (unless busy).
STATUS="$(curl -s --max-time 2 "http://localhost:$PORT/status" 2>/dev/null || true)"
if [ -n "$STATUS" ]; then
  if echo "$STATUS" | grep -q "\"version\": \"$APP_VERSION\""; then
    echo "already running this version"; open_page; exit 0
  fi
  if echo "$STATUS" | grep -q '"running": true'; then
    notify "A download is in progress in the previous version. Open the app again when it finishes to update."
    open_page; exit 0
  fi
  echo "stopping old version"
  pkill -f "scdl_web.py" 2>/dev/null || true; sleep 1
fi

# Find a Python 3.10+ (python.org, Homebrew, or Apple's Command Line Tools)
PY=""
for c in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$c" ] && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  if have xcode-select && ! xcode-select -p >/dev/null 2>&1; then
    notify "Installing Apple's command line tools (one time). Click Install in the dialog, then open this app again."
    xcode-select --install >/dev/null 2>&1
    exit 0
  fi
  fail "Python 3.10 or newer was not found. Install it from python.org, then open this app again."
fi

# Private environment with yt-dlp, a bundled ffmpeg, and root certificates
DEPS="yt-dlp imageio-ffmpeg certifi"
if [ ! -x "$VENV/bin/python" ]; then
  notify "First-time setup: downloading yt-dlp and ffmpeg. This takes about a minute."
  "$PY" -m venv "$VENV" || fail "Could not create the Python environment. See the log in $HOME_DIR"
fi
if ! "$VENV/bin/python" -c 'import yt_dlp, imageio_ffmpeg, certifi' 2>/dev/null; then
  notify "Installing components…"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip $DEPS || fail "Could not download yt-dlp or ffmpeg. Check your internet connection."
else
  # keep yt-dlp current in the background (SoundCloud changes often)
  ( "$VENV/bin/python" -m pip install --quiet --upgrade $DEPS >/dev/null 2>&1 & )
fi

notify "Starting… your browser will open in a moment."
cd "$APP_DIR"
export SCDL_VERSION="$APP_VERSION"
exec "$VENV/bin/python" scdl_web.py "$@"
