"""Graders: programs that decide pass or fail. No model grades anything. Text graders are exact or structured checks over the reply; code tasks run in the sandbox gateway
(`envrec.skills.sbx.run_exam_code`), which refuses before starting anything when no sandbox backend is ready."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from envrec.skills import sbx

from envrec_coordinator.admission.suite import CODE_GRADERS, Task

FENCE = re.compile(r"```[ \t]*(?:python|py|json)?[ \t]*\r?\n(.*?)```", re.S | re.I)
ANSWER = re.compile(r"^ANSWER:\s*(-?\d+(?:\.\d+)?)\s*$")
BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")


class SandboxMissing(RuntimeError):
    """Model-written code was about to run and no sandbox backend is ready. The exam aborts; nothing ran."""


@dataclass(frozen=True)
class Grade:
    passed: bool
    grader: str  # the kind of the grader, always one of CODE_GRADERS for a counted result
    reason: str  # "passed" or a key of strings.TASK_FAIL
    detail: str = ""


CodeRunner = Callable[[str, str, int, float], dict]


def _default_runner(solution: str, checks: str, n_checks: int, timeout_s: float) -> dict:
    return sbx.run_exam_code(solution, checks, n_checks, timeout_s=timeout_s)


def extract_code(reply: str) -> str | None:
    m = FENCE.search(reply)
    text = (m.group(1) if m else reply).strip("\n")
    return text if text.strip() else None


def _json_object(reply: str):
    m = FENCE.search(reply)
    text = (m.group(1) if m else reply).strip()
    for cand in (text, text[text.find("{"):text.rfind("}") + 1] if "{" in text and "}" in text else ""):
        try:
            v = json.loads(cand)
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    return None


def _lines(reply: str, bullet: str, normalize: str) -> list[str]:
    out = []
    for ln in reply.strip().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if bullet == "strip":
            ln = BULLET.sub("", ln)
        out.append(ln.lower() if normalize == "lower" else ln)
    return out


def _num_equal(a, b, tol: float) -> bool:
    return abs(float(a) - float(b)) <= tol


def _text_checks(g: dict, reply: str) -> Grade:
    """Free text that cannot be compared whole (a summary): the strings it must carry as written, the strings it must not, the number of non-empty lines, the number of words. All data in the task."""
    low = g.get("normalize") == "lower"
    fold = (lambda s: str(s).lower()) if low else str
    lines = [ln.strip() for ln in reply.strip().splitlines() if ln.strip()]
    if g.get("bullets") == "strip":
        lines = [BULLET.sub("", ln) for ln in lines]
    body = fold("\n".join(lines))
    want = g.get("lines")
    if isinstance(want, dict) and not (int(want.get("min", 0)) <= len(lines) <= int(want.get("max", 10**6))):
        return Grade(False, "text_checks", "wrong_lines", f"{len(lines)} lines")
    if "max_words" in g and len(body.split()) > int(g["max_words"]):
        return Grade(False, "text_checks", "too_long", f"{len(body.split())} words")
    missing = [s for s in g.get("contains_all", []) if fold(s) not in body]
    if missing:
        return Grade(False, "text_checks", "missing_content", f"{len(missing)} missing")
    if any(fold(s) in body for s in g.get("contains_none", [])):
        return Grade(False, "text_checks", "forbidden_content")
    return Grade(True, "text_checks", "passed")


def grade(task: Task, reply: str, *, code_runner: CodeRunner | None = None) -> Grade:
    """Grade one reply. `code_runner` is injected by tests; the default is the sandbox gateway. Raises SandboxMissing before any model-written code runs without a sandbox."""
    kind, g = task.grader_kind, task.grader
    if kind not in CODE_GRADERS:
        raise ValueError(f"grader {kind!r} is not a code grader and cannot grade an admission task")
    if not reply or not reply.strip():
        return Grade(False, kind, "empty")
    if kind == "sandbox_tests":
        code = extract_code(reply)
        if code is None:
            return Grade(False, kind, "no_code")
        try:
            res = (code_runner or _default_runner)(code, task.checks, int(g["n_checks"]), task.timeout_s)
        except sbx.SandboxUnavailable as exc:
            raise SandboxMissing(str(exc)) from None
        if res.get("ok") is True and res.get("kind") == "passed":
            return Grade(True, kind, "passed")
        r = res.get("kind", "no_result")
        return Grade(False, kind, r if r in ("wrong_answer", "syntax_error", "exception", "timeout", "no_result", "wrong_check_count") else "no_result", str(res.get("detail", ""))[:120])
    if kind == "json":
        obj = _json_object(reply)
        if obj is None:
            return Grade(False, kind, "no_json")
        ok = obj == g["equals"] and json.dumps(obj, sort_keys=True) == json.dumps(g["equals"], sort_keys=True)  # equal, and no 1 == True mix-ups
        return Grade(ok, kind, "passed" if ok else "not_equal")
    if kind == "answer_line":
        last = next((ln.strip() for ln in reversed(reply.strip().splitlines()) if ln.strip()), "")
        m = ANSWER.match(last)
        if not m:
            return Grade(False, kind, "no_answer_line")
        ok = _num_equal(m.group(1), g["expect"], float(g.get("tolerance", 0)))
        return Grade(ok, kind, "passed" if ok else "wrong_answer")
    if kind == "exact":
        ok = reply.strip() == str(g["expect"])
        return Grade(ok, kind, "passed" if ok else "not_equal")
    if kind == "text_checks":
        return _text_checks(g, reply)
    got = _lines(reply, g.get("bullet", "none"), g.get("normalize", "none"))  # kind == "lines"
    want = [str(x).lower() if g.get("normalize") == "lower" else str(x) for x in g["expect"]]
    ok = got == want if g.get("ordered", True) else (set(got) == set(want) and len(got) == len(set(got)))
    return Grade(ok, kind, "passed" if ok else "wrong_lines")
