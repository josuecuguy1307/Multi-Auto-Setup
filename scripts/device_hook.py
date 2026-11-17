"""Claude Code hooks for the device mission (wired in .claude/settings.json through scripts/device_hook.sh).

    session-start   inject STEER.md, the AGENT_STOP state and the acceptance summary into the new session's context
    pre-bash        the gate: a `git commit` is refused while any recorded PASS is stale or the results were hand-edited
    stop            commit work in progress at the end of a turn, but only when the device tests pass; never pushes, never blocks

Reads the hook's JSON from stdin. Prints little; details go to runs/hooks.log.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
LOG = ROOT / "runs" / "hooks.log"


def _log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def _steer() -> str:
    p = ROOT / "STEER.md"
    if not p.exists():
        return ""
    body = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith(("#", ">", "<!--"))]
    return "\n".join(body)


def session_start() -> int:
    parts = ["Device mission: read MISSION.md, PROGRESS.md (last entry), DECISIONS.md, NEEDS_HUMAN.md before working."]
    if (ROOT / "AGENT_STOP").exists():
        parts.append("AGENT_STOP EXISTS: close cleanly now (commit, note in PROGRESS.md) and end.")
    s = _steer()
    parts.append("STEER.md (Josué's direction changes; they override earlier plans):\n" + s if s else "STEER.md: no new direction.")
    st = _run([PY, "scripts/device_accept.py", "status"], timeout=60)
    summary = [l for l in st.stdout.splitlines() if "**" in l and "PASS**" not in l][:8]
    parts.append("Acceptance (not yet PASS):\n" + "\n".join(summary) + "\n" + (st.stdout.strip().splitlines() or [""])[-1])
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n\n".join(parts)}}))
    return 0


def pre_bash(event: dict) -> int:
    cmd = event.get("tool_input", {}).get("command", "")
    if not re.search(r"\bgit\b[^\n|;&]*\bcommit\b", cmd):
        return 0
    v = _run([PY, "scripts/device_accept.py", "verify"], timeout=120)
    if v.returncode == 0:
        return 0
    reason = "Commit gate: " + "; ".join(l for l in v.stdout.splitlines() if l.startswith("ERROR"))[:600] + ". Re-run `python scripts/device_accept.py run --id <ID>` first."
    _log("pre-bash blocked: " + reason)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    return 0


def stop(event: dict) -> int:
    if event.get("stop_hook_active"):
        return 0
    dirty = _run(["git", "status", "--porcelain"]).stdout.strip()
    if not dirty:
        return 0
    branch = _run(["git", "branch", "--show-current"]).stdout.strip()
    if branch in ("main", "master", ""):
        _log(f"stop: not committing on branch {branch!r}")
        return 0
    t = _run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "device/tests"], timeout=900)
    v = _run([PY, "scripts/device_accept.py", "verify"], timeout=120)
    if t.returncode not in (0, 5) or v.returncode != 0:  # 5 = nothing collected
        _log(f"stop: tests or verify not green (pytest {t.returncode}, verify {v.returncode}); left uncommitted")
        return 0
    _run(["git", "add", "-A"])
    msg = "wip(device): auto-commit at stop\n\nDevice tests and acceptance verify were green.\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
    c = _run(["git", "commit", "-q", "-m", msg])
    _log(f"stop: commit exit {c.returncode} {c.stderr.strip()[:200]}")
    return 0


def main() -> int:
    what = sys.argv[1]
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}
    try:
        return {"session-start": lambda: session_start(), "pre-bash": lambda: pre_bash(event), "stop": lambda: stop(event)}[what]()
    except Exception as exc:  # a broken hook must never wedge the session
        _log(f"ERROR hook {what}: {exc!r}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
