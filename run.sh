#!/usr/bin/env bash
# The continuity loop: fresh `claude -p` sessions, one after another, until every acceptance criterion is PASS or AGENT_STOP exists.
#
#   bash run.sh                          # on this machine (bypass permissions: only inside a sandbox/VM you trust)
#   MAS_LOOP_MODE=container bash run.sh  # inside the device harness container (recommended)
#
# Env: CLAUDE_BIN (a Claude Code >= 2.1.321, for Fable 5.2), MAS_ADVISOR (default fable; "off" disables), MAS_MODEL (default claude-opus-5-5),
#      MAS_MAX_SESSIONS (default 200), MAS_EFFORT (default high).
set -u
cd "$(dirname "$0")" || exit 1
ROOT=$(pwd)
LOGDIR=runs/loop
mkdir -p "$LOGDIR"

py() { for p in .venv/Scripts/python.exe .venv/bin/python; do [ -x "$p" ] && { "$p" "$@"; return; }; done; python3 "$@"; }

pick_claude() {
  [ -n "${CLAUDE_BIN:-}" ] && { echo "$CLAUDE_BIN"; return; }
  # newest bundled desktop build first, then PATH
  local newest
  newest=$(ls -d "$LOCALAPPDATA"/Claude-Data/*/claude-code/*/*/claude.exe 2>/dev/null | sort -V | tail -1)
  [ -n "$newest" ] && { echo "$newest"; return; }
  command -v claude
}

if [ "${MAS_LOOP_MODE:-host}" = container ]; then
  echo "ERROR container mode is not built yet (device/harness/Dockerfile is the next harness unit)"; exit 2
fi

CLAUDE=$(pick_claude)
[ -z "$CLAUDE" ] && { echo "ERROR no claude binary found"; exit 1; }
echo "claude: $CLAUDE ($("$CLAUDE" --version 2>/dev/null))"

ADVISOR_ARGS=()
[ "${MAS_ADVISOR:-fable}" != "off" ] && ADVISOR_ARGS=(--advisor "${MAS_ADVISOR:-fable}")

PROMPT='You are continuing the MAS portable device mission in this repo. Read MISSION.md, STEER.md, the last entry of PROGRESS.md, DECISIONS.md and NEEDS_HUMAN.md.
If AGENT_STOP exists: commit, note it in PROGRESS.md, stop.
Pick the next unit: the lowest-phase criterion in ACCEPTANCE.md that is not PASS. Work it through the cycle: test first (show it red), build,
`python scripts/device_accept.py run --id <ID>` until PASS, then the evaluator subagent (save its report to device/evals/evaluator/<ID>.md); fix until its verdict is PASS.
Commit small, on device/v0 only; push only device/v0. Consult the advisor at the decision points listed in MISSION.md section 5.
Before ending: append a dated PROGRESS.md entry (what you did, what failed, what is next). Anything that needs Josué goes to NEEDS_HUMAN.md.'

for i in $(seq 1 "${MAS_MAX_SESSIONS:-200}"); do
  [ -f AGENT_STOP ] && { echo "AGENT_STOP found; stopping."; break; }
  if py scripts/device_accept.py status > "$LOGDIR/status.txt" 2>&1; then
    echo "every criterion PASS; stopping."; break
  fi
  stamp=$(date +%Y%m%dT%H%M%S)
  echo "session $i ($stamp): $(tail -1 "$LOGDIR/status.txt")"
  "$CLAUDE" -p "$PROMPT" --model "${MAS_MODEL:-claude-opus-5-5}" --effort "${MAS_EFFORT:-high}" "${ADVISOR_ARGS[@]}" \
    --permission-mode bypassPermissions --output-format stream-json --verbose \
    > "$LOGDIR/session-$stamp.jsonl" 2> "$LOGDIR/session-$stamp.err" < /dev/null
  rc=$?
  [ $rc -ne 0 ] && echo "ERROR session $i exited $rc (see $LOGDIR/session-$stamp.err)"
  py scripts/device_accept.py verify > "$LOGDIR/verify-$stamp.txt" 2>&1 || echo "ERROR verify failed after session $i"
done
