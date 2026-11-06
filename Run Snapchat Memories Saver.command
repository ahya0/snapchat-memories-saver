#!/bin/zsh
set -u
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)" || exit 2
PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
if [[ -z "$PYTHON_BIN" ]] || ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  /usr/bin/osascript -e 'display dialog "Snapchat Memories Saver needs Python 3.9 or newer. Install it from python.org/downloads/macos, then try again." with title "Snapchat Memories Saver" buttons {"OK"} default button "OK" with icon caution' >/dev/null 2>&1 || true
  exit 69
fi
exec "$PYTHON_BIN" "$SCRIPT_DIR/scripts/guided_audit.py" "$@"
