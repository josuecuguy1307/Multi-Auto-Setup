#!/usr/bin/env bash
# Runs scripts/device_hook.py with the project's interpreter (the .venv on Windows or POSIX, else python3).
cd "$(dirname "$0")/.." || exit 0
for py in .venv/Scripts/python.exe .venv/bin/python; do
  [ -x "$py" ] && exec "$py" scripts/device_hook.py "$@"
done
exec python3 scripts/device_hook.py "$@"
