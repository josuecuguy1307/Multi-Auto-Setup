#!/bin/bash
# Run the REA CLI with the network denied and file writes limited to its private home and temp dirs.
# Usage: scripts/rea_sandboxed.sh <rea args...>
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="$ROOT/scout/rea-home"
mkdir -p "$HOME_DIR"
PROFILE="(version 1)(allow default)(deny network*)(deny file-write*)(allow file-write* (subpath \"$HOME_DIR\") (subpath \"/private/tmp\") (subpath \"/private/var/folders\") (subpath \"/dev\"))"
exec env HOME="$HOME_DIR" sandbox-exec -p "$PROFILE" "$ROOT/scout/rea-run/node_modules/.bin/rea" "$@"
