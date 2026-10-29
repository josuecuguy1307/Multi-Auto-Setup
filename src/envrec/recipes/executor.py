"""The recipe executor: the only code that runs a skill's recipe steps on a target.

Per write step, in this order: the exact command is shown to the owner together with its undo, the owner answers, the undo is written to a durable journal (an fsync'd file, so
it survives a crash of this process), then the command runs with a hard timeout. A step that does not answer in time is a typed `hang`, never a wait. The executor refuses to start
unless the recipe's canonical hash equals the hash the owner approved, so it can run nothing that is not in the approved content.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from envrec.recipes.model import problems, recipe_hash, render
from envrec.recipes.shell import quote

RunArgv = Callable[..., tuple[int, bytes, bytes]]  # (argv, shell, stdin=None, timeout=...) -> (exit code, stdout, stderr)
Ask = Callable[[str], bool]
STEP_TIMEOUT_S, PUT_TIMEOUT_S = 120.0, 900.0


class RecipeNotApproved(PermissionError):
    """The recipe on hand is not the one the owner approved (or is invalid)."""


@dataclass
class Artifact:
    path: Path
    sha256: str


@dataclass
class StepResult:
    id: str
    status: str  # ok | denied | failed | hang | refused
    detail: str = ""


class UndoJournal:
    """A JSONL file next to the run: an entry is written (and flushed to disk) BEFORE the step it reverses runs."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def record(self, target: str, step: str, argv: list[str], shell: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "target": target, "step": step, "argv": argv, "shell": shell, "state": "pending"}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def mark_undone(self, target: str, step: str) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "target": target, "step": step, "state": "undone"}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def pending(self, target: str) -> list[dict]:
        """Entries not yet undone, newest first. An `undone` row cancels the latest earlier pending row of the same step, so a second deploy of the same steps is tracked on its own."""
        rows = [json.loads(ln) for ln in self.path.read_text().splitlines() if ln.strip()] if self.path.exists() else []
        open_: list[dict] = []
        for r in rows:
            if r["target"] != target:
                continue
            if r["state"] == "pending":
                open_.append(r)
            else:
                for i in range(len(open_) - 1, -1, -1):
                    if open_[i]["step"] == r["step"]:
                        del open_[i]
                        break
        return list(reversed(open_))


@dataclass
class RecipeExecutor:
    recipe: dict
    approved_hash: str
    target: str
    run_argv: RunArgv
    ask: Ask
    journal: UndoJournal
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    emit: Callable[[str, dict], None] = lambda kind, info: None

    def __post_init__(self) -> None:
        bad = problems(self.recipe)
        if bad:
            raise RecipeNotApproved("the recipe is not valid: " + "; ".join(bad[:3]))
        if recipe_hash(self.recipe) != self.approved_hash:
            raise RecipeNotApproved("the recipe is not the one the owner approved (its hash differs); nothing was run")
        self.shell = self.recipe["shell"]
        self.values = {"root": self.recipe["root"], "port": str(self.recipe["port"])}

    # ---- helpers -------------------------------------------------------------------------------------------------------------------------

    def _run(self, argv: list[str], stdin=None, timeout: float = STEP_TIMEOUT_S) -> tuple[str, int, str, str]:
        """(status, code, out, err): status `hang` when the transport did not answer in time (exit 124)."""
        code, out, err = self.run_argv(argv, self.shell, stdin=stdin, timeout=timeout)
        status = "hang" if code == 124 else "ok" if code == 0 else "failed"
        return status, code, out.decode(errors="replace"), err.decode(errors="replace")

    def _local_hash(self, role: str) -> str:
        art = self.artifacts.get(role)
        if art is None:
            raise RecipeNotApproved(f"the recipe needs the {role} artifact and none was given")
        h = hashlib.sha256()
        with art.path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != art.sha256:
            raise RecipeNotApproved(f"the local {role} file does not match its pinned sha256; nothing was sent")
        return art.sha256

    # ---- deploy ----------------------------------------------------------------------------------------------------------------------------

    def plan_text(self) -> str:
        """Every command the recipe will run, as the owner will see it: the plan shown with the first approval."""
        from envrec.recipes.model import describe
        return "\n".join([f"recipe {recipe_hash(self.recipe)[:12]} in shell {self.shell}; everything it writes is under {self.recipe['root']}", *describe(self.recipe)])

    def deploy(self) -> list[StepResult]:
        results: list[StepResult] = []
        for s in self.recipe["steps"]:
            sid = s["id"]
            if s.get("write"):
                if "put" in s:
                    try:
                        sha = self._local_hash(s["put"]["artifact"])
                    except RecipeNotApproved as exc:
                        results.append(StepResult(sid, "refused", str(exc)))
                        return results
                    shown = f"send the {s['put']['artifact']} file (sha256 {sha}) to {render([s['put']['dest']], self.values)[0]} through: " + quote(render(s["put"]["receive"], {**self.values, 'dest': render([s['put']['dest']], self.values)[0]}), self.shell)
                else:
                    shown = quote(render(s["run"], self.values), self.shell)
                undo_argv = render(s["undo"], self.values)
                if not self.ask(f"{shown}\n  undo: {quote(undo_argv, self.shell)}"):
                    self.emit("recipe_step_denied", {"step": sid})
                    results.append(StepResult(sid, "denied", "the owner did not approve this step; nothing further ran"))
                    return results
                self.journal.record(self.target, sid, undo_argv, self.shell)  # the way back is on disk BEFORE the step runs
            if "put" in s:
                dest = render([s["put"]["dest"]], self.values)[0]
                status, code, out, err = self._run(render(s["put"]["receive"], {**self.values, "dest": dest}), stdin=self.artifacts[s["put"]["artifact"]].path, timeout=PUT_TIMEOUT_S)
            else:
                status, code, out, err = self._run(render(s["run"], self.values))
            self.emit("recipe_step", {"step": sid, "status": status, "code": code})
            if status != "ok":
                results.append(StepResult(sid, status, (err or out).strip()[:160] or f"exit {code}"))
                return results
            want = s.get("expect_sha256")
            if want:
                if self._local_hash(want) not in out.lower():
                    results.append(StepResult(sid, "failed", f"the {want} file on the target does not have the sha256 that was sent"))
                    return results
            results.append(StepResult(sid, "ok", out.strip()[:80]))
        return results

    # ---- undo ------------------------------------------------------------------------------------------------------------------------------

    def undo(self) -> list[StepResult]:
        results: list[StepResult] = []
        for row in self.journal.pending(self.target):
            shown = quote(row["argv"], row["shell"])
            if not self.ask(f"undo `{row['step']}`: {shown}"):
                results.append(StepResult(row["step"], "denied", "the owner did not approve this undo step; it stays pending"))
                return results
            status, code, out, err = self._run(row["argv"])
            # an undo of something that is already gone may exit non-zero; the proof that nothing is left is the `absent` check below, not this exit code
            self.journal.mark_undone(self.target, row["step"])
            results.append(StepResult(row["step"], "ok" if status != "hang" else "hang", (err or out).strip()[:100]))
        return results

    def nothing_left(self) -> tuple[bool, str]:
        """The recipe's `absent` command, read-only: no output or a failing exit means the workspace folder is gone."""
        status, code, out, err = self._run(render(self.recipe["absent"], self.values))
        if status == "hang":
            return False, "hang"
        gone = code != 0 or not out.strip()
        return gone, "nothing left under the root" if gone else out.strip()[:100]


_HEX64 = re.compile(r"[0-9a-f]{64}")
