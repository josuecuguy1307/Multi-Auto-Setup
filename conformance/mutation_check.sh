#!/usr/bin/env bash
# V4: remove five guards ONE AT A TIME in a scratch git worktree (never in the working tree) and report which existing test notices.
# usage: conformance/mutation_check.sh [subset]   (subset: pytest paths to run; default: the whole tests/ directory)
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WT="$(mktemp -d /tmp/envrec-mutation.XXXXXX)/wt"
PY="$REPO/.venv/bin/python"
SUBSET="${1:-tests}"
git -C "$REPO" worktree add --detach "$WT" HEAD >/dev/null 2>&1 || { echo "cannot create the scratch worktree"; exit 2; }
trap 'git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1; rm -rf "$(dirname "$WT")"' EXIT
export PYTHONPATH="$WT/src"
echo "scratch worktree: $WT (envrec imported from: $(cd "$WT" && "$PY" -c 'import envrec;print(envrec.__file__)'))"

mutate() {  # name file python-replace-expression(old|new)
  local name="$1" file="$2" old="$3" new="$4"
  git -C "$WT" checkout -q -- . 2>/dev/null
  "$PY" - "$WT/$file" "$old" "$new" <<'PYEOF'
import sys
path, old, new = sys.argv[1:4]
text = open(path).read()
assert old in text, f"mutation anchor not found in {path}: {old!r}"
open(path, "w").write(text.replace(old, new, 1))
PYEOF
  [ $? -eq 0 ] || { echo "MUTATION NOT APPLIED: $name"; return; }
  echo "=== guard removed: $name"
  local out status
  out="$(cd "$WT" && ENVREC_TEST_TIMEOUT="${MUTATION_TIMEOUT:-60}" "$PY" -m pytest $SUBSET -x -q -p no:cacheprovider 2>&1)"
  status=$?
  echo "$out" | grep -E "^(FAILED|ERROR)|passed|failed" | head -3
  # A removed guard can make a test wait forever (a loop that was supposed to stop). The per-test limit (testplugins/envrec_timeout.py) turns that into a failure named HANG, or, if the
  # process cannot be interrupted, the watchdog ends it: either way the removal was noticed, and it is reported as noticed BY HANG, not as a pass.
  if echo "$out" | grep -q "HANG: "; then
    echo "DETECTED BY HANG: $(echo "$out" | grep -m1 -oE 'HANG: [^ ]+ ran longer than [0-9.]+ s')"
  elif echo "$out" | grep -q "^Timeout ("; then
    echo "DETECTED BY HANG: the watchdog ended the process ($(echo "$out" | grep -m1 '^Timeout ('))"
  elif [ "$status" -eq 0 ]; then
    echo "NOT DETECTED: the whole run passed with this guard removed"
  fi
}

mutate "policy denial (decide() always allows)" src/envrec/policy/policy.py '    if DENY_TAGS & {t.lower() for t in tool.tags} or any(p in tool.name for p in DENY_NAME_PARTS):' '    return out(Decision.allow, "mutated: policy removed")
    if DENY_TAGS & {t.lower() for t in tool.tags} or any(p in tool.name for p in DENY_NAME_PARTS):'
mutate "approval gate (every write is approved)" src/envrec/remote/approval.py 'granted = self.ask("> ").strip().lower() == "approve"' 'granted = True'
mutate "scope guard (no address is ever refused)" src/envrec/discovery/scope.py 'def _check_address(a: ipaddress.IPv4Address, nets, cap_prefix: int, hostname) -> None:' 'def _check_address(a: ipaddress.IPv4Address, nets, cap_prefix: int, hostname) -> None:
    return'
mutate "privacy gate (entity ids are not hashed)" src/envrec/privacy/gate.py 'short_hash(out.ident, salt=salt)' 'out.ident'
mutate "protocol done refusal (done always accepted)" src/envrec/loop/run.py '                if action.action == ActionType.done:
                    if proto is not None and not proto.terminal:' '                if action.action == ActionType.done:
                    if False:'
