#!/bin/bash
# One-off measurement: run an allowlisted REA command with an isolated HOME and sample whether the REA
# process tree ever holds a network socket. This OBSERVES; it does not enforce (sandbox-exec cannot run
# the `ps` that REA's cleanup needs). Usage: rea_monitored.sh <out.json> <net.log> <rea args...>
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$1"; NETLOG="$2"; shift 2
HOME_DIR="$ROOT/scout/rea-home"; mkdir -p "$HOME_DIR"
: > "$NETLOG"
cd "$(mktemp -d)"   # empty working directory
HOME="$HOME_DIR" "$ROOT/scout/rea-run/node_modules/.bin/rea" "$@" > "$OUT" 2> "$OUT.err" &
PID=$!
SAMPLES=0
while kill -0 $PID 2>/dev/null; do
  PIDS=$(pgrep -d, -P $PID 2>/dev/null); ALL="$PID${PIDS:+,$PIDS}"
  lsof -nP -a -i -p "$ALL" 2>/dev/null | tail -n +2 >> "$NETLOG"
  SAMPLES=$((SAMPLES+1)); sleep 0.1
done
wait $PID; CODE=$?
echo "exit=$CODE samples=$SAMPLES network_sockets_seen=$(wc -l < "$NETLOG" | tr -d ' ')"
