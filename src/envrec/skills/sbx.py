"""The Docker sandbox for a skill's `scripts/`. Fails closed.

No network, a read-only root filesystem, every capability dropped, no new privileges, no device mounts, memory/CPU/process limits, an unprivileged user. Only two
directories are mounted, both read-only: the skill's own folder (`/skill`) and the helper library (`/sbxlib`). The harness does all I/O; the script only turns the text
it is handed into typed output, and that output is validated here before anything else sees it."""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path

IMAGE = "python:3.12-alpine"
LIB_DIR = Path(__file__).resolve().parent / "sbxlib"
MAX_OUTPUT = 4 * 1024 * 1024
MAX_INPUT = 12 * 1024 * 1024
MAX_FACTS, MAX_ENTITIES, MAX_CALLS = 300, 600, 90
_KEY = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_\-]+)+$")
WRAPPER = (
    "import sys, json\n"
    "sys.path.insert(0, '/sbxlib')\n"
    "d = json.load(sys.stdin)\n"
    "import signal\n"
    "signal.alarm(max(1, int(d.get('limit_s', 10))))\n"  # the script's own time starts here, after the container and the interpreter are up
    "ns = {'__name__': 'skill_script', '__file__': '/skill/' + d['script']}\n"
    "exec(compile(open('/skill/' + d['script']).read(), d['script'], 'exec'), ns)\n"
    "sys.stdout.write(json.dumps(ns['parse'](d['input'])))\n"
)
START_SLACK_S = 45  # container start under load (a busy Docker VM) is not the script's time; the script's own limit is enforced inside by the alarm above
SCRIPT_KEYS = {"facts", "entities", "unknowns", "next"}


class SandboxUnavailable(RuntimeError):
    pass


class SandboxRefused(ValueError):
    pass


class ScriptFailed(SandboxRefused):
    """The script ran and crashed (exit code non-zero): a bug in the script, reported as an error rather than a refusal."""


def docker_argv(name: str, skill_dir: Path, image: str = IMAGE) -> list[str]:
    return ["docker", "run", "--rm", "-i", "--name", name, "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=4m", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "24", "--memory", "256m", "--memory-swap", "256m", "--cpus", "1", "--user", "65534:65534",
            "-v", f"{skill_dir.resolve()}:/skill:ro", "-v", f"{LIB_DIR}:/sbxlib:ro", image, "python", "-I", "-c", WRAPPER]


_READY: dict[str, bool] = {}


def docker_ready(image: str = IMAGE) -> bool:
    if image in _READY:
        return _READY[image]
    try:
        ok = subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0 and bool(
            subprocess.run(["docker", "images", "-q", image], capture_output=True, text=True, timeout=20).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        ok = False
    _READY[image] = ok
    return ok


def validate_output(raw: bytes) -> dict:
    if len(raw) > MAX_OUTPUT:
        raise SandboxRefused(f"output over {MAX_OUTPUT} bytes rejected")
    try:
        out = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise SandboxRefused("the script did not print JSON") from None
    if not isinstance(out, dict) or set(out) - SCRIPT_KEYS:
        raise SandboxRefused(f"the script must return an object with only {sorted(SCRIPT_KEYS)}")
    for k, cap in (("facts", MAX_FACTS), ("entities", MAX_ENTITIES), ("next", MAX_CALLS), ("unknowns", MAX_FACTS)):
        v = out.get(k, [])
        if not isinstance(v, list) or len(v) > cap or not all(isinstance(x, dict) for x in v):
            raise SandboxRefused(f"`{k}` must be a list of at most {cap} objects")
        out[k] = v
    for f in out["facts"]:
        if not isinstance(f.get("key"), str) or not _KEY.match(f["key"]):
            raise SandboxRefused(f"bad fact key {str(f.get('key'))[:60]!r}: keys look like prefix.name")
    for e in out["entities"]:
        if not isinstance(e.get("kind"), str) or not isinstance(e.get("ident"), str) or not isinstance(e.get("attrs", {}), dict):
            raise SandboxRefused("each entity needs a kind, an ident and attrs")
    for u in out["unknowns"]:
        if not isinstance(u.get("key"), str) or not _KEY.match(u["key"]):
            raise SandboxRefused(f"bad unknown key {str(u.get('key'))[:60]!r}")
    for c in out["next"]:
        if not isinstance(c.get("id"), str) or not isinstance(c.get("tool"), str) or not isinstance(c.get("args", {}), dict):
            raise SandboxRefused("each requested call needs an id, a tool and args")
    return out


def run_script(skill_dir: Path, script: str, payload: dict, *, timeout_s: float = 10.0, image: str = IMAGE) -> dict:
    """Run `<skill_dir>/<script>` over `payload` (its `parse(input)`), and return the validated output. Raises SandboxUnavailable (fail closed) or SandboxRefused."""
    if not (skill_dir / script).is_file():
        raise SandboxRefused(f"the script {script} does not exist")
    body = json.dumps({"script": script, "input": payload, "limit_s": timeout_s}).encode()
    if len(body) > MAX_INPUT:
        raise SandboxRefused("input too large for the sandbox")
    if not docker_ready(image):
        raise SandboxUnavailable("Docker (or the sandbox image) is not available: skill scripts do not run without it")
    name = f"envrec-sbx-{uuid.uuid4().hex[:10]}"
    try:
        done = subprocess.run(docker_argv(name, skill_dir, image), input=body, capture_output=True, timeout=timeout_s + START_SLACK_S)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=15)
        raise SandboxRefused(f"the script ran over {timeout_s:g} s and was killed") from None
    if done.returncode in (142, -14):  # SIGALRM: the in-container limit fired
        raise SandboxRefused(f"the script ran over {timeout_s:g} s and was killed")
    if done.returncode != 0:
        last = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ScriptFailed(f"the script failed (exit {done.returncode}): {last[-1][:160] if last else 'no message'}")
    return validate_output(done.stdout)


# ---- the admission exam's code tasks ------------------------------------------------------------------------------------------------------------
# A model's answer to a "code" task is model-written code. It runs HERE and nowhere else: the same container limits as a skill script, but nothing is mounted at all (the solution and the
# project's hidden checks travel on stdin, so the solution cannot read the checks from disk). Output is one JSON line the wrapper writes after the solution's own prints are discarded.
EXAM_WRAPPER = (
    "import sys, json, io, signal\n"
    "d = json.load(sys.stdin)\n"
    "real = sys.stdout\n"
    "signal.alarm(max(1, int(d.get('limit_s', 10))))\n"
    "res = {'ok': False, 'kind': 'error', 'checked': 0}\n"
    "class Failed(Exception):\n"
    "    pass\n"
    "def check(actual, expected):\n"
    "    res['checked'] += 1\n"
    "    if actual != expected or type(actual) is not type(expected):\n"
    "        raise Failed(repr(actual)[:80] + ' != ' + repr(expected)[:80])\n"
    "def raises(exc, fn, *a):\n"
    "    res['checked'] += 1\n"
    "    try:\n"
    "        fn(*a)\n"
    "    except exc:\n"
    "        return\n"
    "    except BaseException as e:\n"
    "        raise Failed('raised ' + type(e).__name__)\n"
    "    raise Failed('did not raise ' + exc.__name__)\n"
    "ns = {'__name__': 'solution', 'check': check, 'raises': raises}\n"
    "sys.stdout = io.StringIO()\n"
    "try:\n"
    "    exec(compile(d['solution'], 'solution', 'exec'), ns)\n"
    "    res['checked'] = 0\n"
    "    ns2 = dict(ns)\n"
    "    ns2['check'], ns2['raises'] = check, raises\n"  # the solution cannot swap the project's checkers for its own
    "    exec(compile(d['checks'], 'checks', 'exec'), ns2)\n"
    "    res.update(ok=res['checked'] == d['n_checks'], kind='passed' if res['checked'] == d['n_checks'] else 'wrong_check_count')\n"
    "except Failed as e:\n"
    "    res.update(ok=False, kind='wrong_answer', detail=str(e))\n"
    "except SyntaxError as e:\n"
    "    res.update(ok=False, kind='syntax_error', detail=str(e)[:80])\n"
    "except BaseException as e:\n"
    "    res.update(ok=False, kind='exception', detail=type(e).__name__ + ': ' + str(e)[:80])\n"
    "sys.stdout = real\n"
    "sys.stdout.write(json.dumps({'ok': bool(res['ok']), 'kind': res['kind'], 'checked': res['checked'], 'detail': res.get('detail', '')}))\n"
)
EXAM_KINDS = {"passed", "wrong_answer", "syntax_error", "exception", "wrong_check_count", "error"}


def exam_argv(name: str, image: str = IMAGE) -> list[str]:
    return ["docker", "run", "--rm", "-i", "--name", name, "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=4m", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "24", "--memory", "256m", "--memory-swap", "256m", "--cpus", "1", "--user", "65534:65534",
            image, "python", "-I", "-c", EXAM_WRAPPER]


def run_exam_code(solution: str, checks: str, n_checks: int, *, timeout_s: float = 10.0, image: str = IMAGE) -> dict:
    """Run a model's solution followed by the project's hidden checks. Returns {ok, kind, checked, detail}. Raises SandboxUnavailable (fail closed, before anything starts) when no sandbox
    backend is ready: model-written code never runs without one. A solution that runs over the limit is a result (`timeout`), not an error."""
    if not docker_ready(image):
        raise SandboxUnavailable("Docker (or the sandbox image) is not available: model-written code does not run without it")
    body = json.dumps({"solution": solution, "checks": checks, "n_checks": n_checks, "limit_s": timeout_s}).encode()
    if len(body) > MAX_INPUT:
        raise SandboxRefused("input too large for the sandbox")
    name = f"envrec-exam-{uuid.uuid4().hex[:10]}"
    try:
        done = subprocess.run(exam_argv(name, image), input=body, capture_output=True, timeout=timeout_s + START_SLACK_S)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=15)
        return {"ok": False, "kind": "timeout", "checked": 0, "detail": f"over {timeout_s:g} s"}
    if done.returncode in (142, -14):
        return {"ok": False, "kind": "timeout", "checked": 0, "detail": f"over {timeout_s:g} s"}
    try:
        out = json.loads(done.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "kind": "no_result", "checked": 0, "detail": "the solution ended the process without the checks finishing"}
    if not isinstance(out, dict) or out.get("kind") not in EXAM_KINDS or not isinstance(out.get("ok"), bool) or (out["ok"] and out["kind"] != "passed"):
        return {"ok": False, "kind": "no_result", "checked": 0, "detail": "the sandbox result was not understood"}
    return {"ok": out["ok"], "kind": out["kind"], "checked": int(out.get("checked") or 0), "detail": str(out.get("detail", ""))[:120]}
