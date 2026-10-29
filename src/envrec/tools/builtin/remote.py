"""Hands for a computer the owner enrolled and reached over SSH (the `connect` protocol): check the route, run a read-only command, plan the workspace, deploy it, verify it, run one task
through it, undo it.

Nothing here knows an operating system. What differs between computers is a recipe in a skill: the commands to run, their undo, how to check the server. These tools only choose and run
a recipe through the one executor (envrec.recipes), which shows every write with its undo, journals the undo first, and refuses anything outside the recipe the owner approved by hash.
The model chooses the route, the (runtime, model) pair among the candidates, when to verify, and what to do on a failure; the hard constraints and the approvals stay in code.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from envrec.discovery.types import Capture, Reason
from envrec.recipes.executor import RecipeExecutor, RecipeNotApproved, UndoJournal
from envrec.recipes.model import FORBIDDEN, recipe_hash
from envrec.recipes.shell import SHELLS, quote
from envrec.skills.verify import Approvals, approval_text
from envrec.tools.ctx import ToolContext, ToolResult
from envrec.tools.registry import tool

PROBE_TIMEOUT_S = 30.0


def _runner(ctx: ToolContext):
    r, tid = ctx.params.get("remote_runner"), ctx.params.get("remote_target")
    return (r, tid) if r is not None and tid else (None, None)


def _run_argv(ctx: ToolContext):
    """The one way these tools reach the computer: the runner's argv call, quoted for the shell. It returns (code, stdout bytes, stderr bytes); 124 means the command did not answer in time."""
    runner, tid = _runner(ctx)

    def run(argv, shell, stdin=None, timeout=60.0):
        res = runner.run_argv(tid, argv, shell, stdin=stdin, timeout=timeout)
        return res.exit_code, res.stdout.encode(), res.stderr.encode()
    return run


def _no_route(ctx: ToolContext) -> ToolResult | None:
    return None if _runner(ctx)[0] is not None else ToolResult.fail(Reason.not_applicable, "no enrolled computer is set for this run")


def _matching_recipe_skill(ctx: ToolContext):
    """The skill with a recipe whose signature matches what is known about this computer, newest approved first."""
    lib, target = ctx.params.get("library"), ctx.target
    if lib is None or target is None:
        return None
    known = ctx.known()
    return next((s for s in lib.match(target, known, "any") if s.meta.recipe is not None), None)


# ---- the route -----------------------------------------------------------------------------------------------------------------------------------

def _route_text(ctx: ToolContext) -> str:
    return f"run `echo envrec-route-check` on the enrolled computer {ctx.params.get('remote_target')!r} over SSH (read-only: it prints one word)"


@tool(tags=("connect", "route", "active"), kinds=("remote",), ring=2, approval=_route_text, produces=("route.reachable",), timeout_s=60)
def route_check(ctx: ToolContext) -> ToolResult:
    """Check that the enrolled computer answers over its allowed route.

    Args:
        none
    Returns:
        Facts route.reachable (true or false) and route.transport.
    Tier: read
    Platforms: any
    Notes: one read-only command that prints one word; asks the owner first; ends as a typed hang if the computer does not answer.
    """
    if (bad := _no_route(ctx)) is not None:
        return bad
    code, out, err = _run_argv(ctx)(["echo", "envrec-route-check"], "sh", None, PROBE_TIMEOUT_S)
    if code == 124:
        return ToolResult.fail(Reason.timeout, "HANG: the computer did not answer the route check in time")
    ok = code == 0 and b"envrec-route-check" in out
    return ToolResult(facts={"route.reachable": ok, "route.transport": "ssh"}, reasons={}, provider="stdlib",
                      limitations=[] if ok else [(err.decode(errors="replace").strip()[:100] or f"exit {code}")])


_NAME = re.compile(r"[a-z][a-z0-9_]{0,23}")


def _read_text(ctx: ToolContext, shell: str, argv: list, name: str = "read") -> str:
    try:
        return f"run on the enrolled computer {ctx.params.get('remote_target')!r}, read-only: {quote([str(a) for a in argv], shell)}   (you approve this exact command; its output is stored by hash and the model never sees it)"
    except ValueError:
        return "run a read-only command (the arguments are not valid and it will be refused)"


@tool(tags=("connect", "read", "active"), kinds=("remote",), ring=2, approval=_read_text, timeout_s=90)
def remote_read(ctx: ToolContext, shell: str, argv: list, name: str = "read") -> ToolResult:
    """Run one read-only command on the enrolled computer and keep its output.

    Args:
        shell: the shell that runs it: sh, cmd or powershell.
        argv: the program and its arguments as a list of strings, never one shell line with pipes.
        name: a short label for the capture (lower-case letters, digits, underscore).
    Returns:
        A capture with the command's output (stored by hash, never shown to the model) and fact route.read_<name> with the exit code and size.
    Tier: read
    Platforms: any
    Notes: for learning about the computer; commands that change, delete, install, fetch from the network or run privileged are refused; the owner approves each exact command.
    """
    if (bad := _no_route(ctx)) is not None:
        return bad
    if shell not in SHELLS:
        return ToolResult.fail(Reason.denied, f"shell must be one of {SHELLS}")
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) and 0 < len(a) <= 400 for a in argv)):
        return ToolResult.fail(Reason.denied, "argv must be a list of non-empty strings")
    if not _NAME.fullmatch(name):
        return ToolResult.fail(Reason.denied, "name is lower-case letters, digits and underscores")
    text = " ".join(argv).lower()
    if (hit := next((b for b in FORBIDDEN if b in text), None)) or re.search(r"(^|\s)(del|erase|rmdir|rm|move|ren|rename|copy|mkdir|md|echo\s.*>|set-content|out-file|new-item|remove-item|install|msiexec|winget|choco)(\s|$)", text):
        return ToolResult.fail(Reason.denied, f"a read-only command cannot contain `{(hit or 'a command that changes things').strip()}`")
    code, out, err = _run_argv(ctx)(argv, shell, None, PROBE_TIMEOUT_S)
    if code == 124:
        return ToolResult.fail(Reason.timeout, f"HANG: the computer did not answer `{argv[0]}` in time")
    body = out + (b"\n" + err if err else b"")
    facts = {f"route.read_{name}": f"exit {code}, {len(out)} bytes"}
    if code != 0:  # a failed command's first error line (cleaned, at most 80 characters, shown quoted like a banner) is what lets the model learn which shell it is talking to
        first = next((ln.strip() for ln in (err or out).decode(errors="replace").splitlines() if ln.strip()), "")
        if first:
            facts[f"route.read_{name}_error"] = first[:80]
    return ToolResult(captures=[Capture(name, f"remote:{argv[0]}", body, code, "remote", ctx.platform)], facts=facts, provider="stdlib")


# ---- plan --------------------------------------------------------------------------------------------------------------------------------------

@tool(tags=("connect", "plan", "passive"), kinds=("remote",), ring=2, owner_thread=True, produces=("workspace.recipe", "workspace.candidates"), repeatable=True)
def plan_workspace(ctx: ToolContext) -> ToolResult:
    """List the recipe and the (runtime, model) pairs that fit this computer.

    Args:
        none
    Returns:
        Facts workspace.recipe (the skill whose recipe matches, or unknown), workspace.candidates (catalog entries that satisfy every hard constraint) and workspace.recommended (the rule-table hint).
    Tier: observe
    Platforms: any
    Notes: reads what is already known; code lists candidates by RAM, disk, architecture and the target's limits, and does not choose among them. No recipe for this computer is an unknown with its reason.
    """
    sk = _matching_recipe_skill(ctx)
    facts: dict = {}
    reasons: dict = {}
    known = ctx.known()
    if sk is None:
        facts["workspace.recipe"] = None
        reasons["workspace.recipe"] = f"no skill has a workspace recipe for this computer (os {known.get('os.family', 'not discovered yet')}, arch {known.get('os.arch', 'not discovered yet')})"
    else:
        facts["workspace.recipe"] = sk.name
    fn = ctx.params.get("candidates_fn")
    cands = fn(known) if fn is not None else []
    if cands:
        facts["workspace.candidates"] = [c["id"] for c in cands]
        facts["workspace.recommended"] = cands[0]["id"]
    else:
        facts["workspace.candidates"] = None
        reasons["workspace.candidates"] = "no catalog entry satisfies the hard constraints for this computer (platform, backend, RAM, disk, the target's limits): a proposal for a new runtime source is the only way in"
    from envrec.discovery.types import Capture as Cap
    return ToolResult(captures=[Cap("plan", "plan_workspace", json.dumps({"recipe": facts.get("workspace.recipe"), "candidates": facts.get("workspace.candidates") or []}).encode(), 0, "local", ctx.platform)],
                      facts=facts, reasons=reasons, provider="rules")


# ---- deploy / verify / task / undo ----------------------------------------------------------------------------------------------------------------

def _deploy_text(ctx: ToolContext, pair: str) -> str:
    sk = _matching_recipe_skill(ctx)
    plan = "\n".join(__import__("envrec.recipes.model", fromlist=["describe"]).describe(sk.meta.recipe)) if sk else "(no recipe matches: this will be refused)"
    return (f"deploy the workspace {pair!r} on the enrolled computer {ctx.params.get('remote_target')!r} with the recipe of skill {sk.name if sk else '?'!r}. Every command below asks again, one by one, "
            f"before it runs:\n{plan}")


def _deploy_undo(ctx: ToolContext, pair: str) -> dict:
    return {"tool": "undo_workspace", "args": {}, "text": "undo_workspace runs each recorded undo command, newest first, asking you for each, then checks that nothing is left"}


@tool(tags=("connect", "deploy", "active"), kinds=("remote",), ring=2, approval=_deploy_text, undo=_deploy_undo, produces=("host.model_server", "workspace.deployed", "workspace.pair"), timeout_s=2400)
def deploy_workspace(ctx: ToolContext, pair: str) -> ToolResult:
    """Deploy the workspace on the enrolled computer with the matching recipe.

    Args:
        pair: the catalog entry id of the (runtime, model) pair, one of workspace.candidates.
    Returns:
        Facts workspace.deployed and host.model_server (a claim the verifier then checks), with the steps that ran.
    Tier: write
    Platforms: any
    Notes: uses only the recipe of a skill; the recipe's hash must be the one the owner approved (asked once, then remembered by hash); each write shows its exact command and undo; local files are checked against their pins first.
    """
    if (bad := _no_route(ctx)) is not None:
        return bad
    sk = _matching_recipe_skill(ctx)
    if sk is None:
        return ToolResult.fail(Reason.not_applicable, "no skill has a recipe for this computer: research and propose one first")
    known = ctx.known()
    allowed = known.get("workspace.candidates") or []
    if pair not in allowed:
        return ToolResult.fail(Reason.denied, f"pair must be one of workspace.candidates {allowed}")
    arts_fn = ctx.params.get("artifacts_for")
    if arts_fn is None:
        return ToolResult.fail(Reason.unavailable, "no artifact source is configured for this run")
    ask = ctx.recording_approve("recipe_step")
    approvals = Approvals(ctx.params["library"].root)
    if not approvals.approved(sk):  # the one approval of the exact code, by hash; the second run on the same computer type does not ask again
        if not approvals.approve(sk, approval_text(sk), ctx.recording_approve("approve_recipe")):
            return ToolResult.fail(Reason.denied, "the owner did not approve this recipe: nothing was run")
    journal = ctx.params["journal"]
    try:
        ex = RecipeExecutor(sk.meta.recipe, recipe_hash(sk.meta.recipe), ctx.params["remote_target"], _run_argv(ctx), ask, journal, arts_fn(pair))
        results = ex.deploy()
    except RecipeNotApproved as exc:
        return ToolResult.fail(Reason.denied, str(exc)[:200])
    ok = bool(results) and all(r.status == "ok" for r in results)
    hang = next((r for r in results if r.status == "hang"), None)
    if hang is not None:
        return ToolResult.fail(Reason.timeout, f"HANG: step {hang.id} did not answer in time; anything done so far can be undone with undo_workspace")
    facts = {"workspace.deployed": ok, "workspace.pair": pair if ok else None, "workspace.steps": f"{sum(1 for r in results if r.status == 'ok')} of {len(sk.meta.recipe['steps'])} ok"}
    if ok:
        facts["host.model_server"] = "running"
    cap = Capture("deploy", "recipe executor", json.dumps([{"id": r.id, "status": r.status} for r in results]).encode(), 0, "remote", ctx.platform)
    return ToolResult(captures=[cap], facts=facts, provider="stdlib",
                      limitations=[] if ok else [f"stopped at step {results[-1].id}: {results[-1].status}: {results[-1].detail}"])


def _executor(ctx: ToolContext, artifacts=None) -> RecipeExecutor:
    sk = _matching_recipe_skill(ctx)
    if sk is None:
        raise RecipeNotApproved("no skill has a recipe for this computer")
    return RecipeExecutor(sk.meta.recipe, recipe_hash(sk.meta.recipe), ctx.params["remote_target"], _run_argv(ctx), ctx.recording_approve("recipe_step"), ctx.params["journal"], artifacts or {})


class _RecipeProbes:
    """What verify_model asks of the device, answered by the recipe's own read-only commands: `bind_check` prints one `address:port` per listener, `rss` prints a number of MiB."""

    def __init__(self, ctx: ToolContext, recipe: dict) -> None:
        self.run, self.recipe = _run_argv(ctx), recipe

    def listeners(self) -> list[str]:
        code, out, _ = self.run(self.recipe["bind_check"], self.recipe["shell"], None, PROBE_TIMEOUT_S)
        if code == 124:
            raise ConnectionError("bind check did not answer")
        rows = []
        for line in out.decode(errors="replace").split():
            host = line.rsplit(":", 1)[0].strip("[]")
            rows.append("0100007F:0" if host == "127.0.0.1" else "00000000000000000000000001000000:0" if host == "::1" else f"{host}:0")
        return rows

    def rss_mib(self) -> float | None:
        code, out, _ = self.run(self.recipe["rss"], self.recipe["shell"], None, PROBE_TIMEOUT_S)
        text = out.decode(errors="replace").strip().split()
        return float(text[0]) if code == 0 and text and re.fullmatch(r"\d+(\.\d+)?", text[0]) else None


def _open_tunnel(ctx: ToolContext):
    fn = ctx.params.get("open_tunnel")
    if fn is None:
        raise ConnectionError("no tunnel is configured for this run")
    return fn(int(_matching_recipe_skill(ctx).meta.recipe["port"]), ctx.recording_approve("open_tunnel"))


def _tunnel_text(ctx: ToolContext) -> str:
    return f"open an SSH tunnel from this computer's 127.0.0.1 to the workspace server's 127.0.0.1 on {ctx.params.get('remote_target')!r}, send it fresh random tokens and a short prompt, and read the server's port and memory with the recipe's own read-only commands"


@tool(tags=("connect", "verify", "active"), kinds=("remote",), ring=2, approval=_tunnel_text, owner_thread=True, produces=("host.model_server", "host.model_tokens_per_s", "host.model_rss_mib"), repeatable=True, timeout_s=300)
def verify_workspace(ctx: ToolContext) -> ToolResult:
    """Verify by code that the workspace server is real and loopback-only.

    Args:
        none
    Returns:
        Facts host.model_server (verified only when every condition held), host.model_tokens_per_s, host.model_rss_mib, and the failed conditions when it is not.
    Tier: read
    Platforms: any
    Notes: a fresh-nonce round trip, a non-empty generation, the model identity, nothing listening beyond 127.0.0.1, tokens per second and resident memory; the model's own words about success are never used.
    """
    from envrec.verify import ExpectedModel, TunnelClient, verify_model
    if (bad := _no_route(ctx)) is not None:
        return bad
    sk = _matching_recipe_skill(ctx)
    if sk is None:
        return ToolResult.fail(Reason.not_applicable, "no recipe for this computer")
    pair, expected_fn = ctx.known().get("workspace.pair"), ctx.params.get("expected_model_fn")
    expected = expected_fn(pair) if pair and expected_fn is not None else None
    if expected is None:
        return ToolResult.fail(Reason.unavailable, "the expected model is not known: deploy_workspace first")
    try:
        local, close = _open_tunnel(ctx)
    except (ConnectionError, PermissionError) as exc:
        return ToolResult.fail(Reason.permission if isinstance(exc, PermissionError) else Reason.unavailable, str(exc)[:160])
    try:
        v = verify_model(TunnelClient(local, timeout=60.0), _RecipeProbes(ctx, sk.meta.recipe), ctx.store, ctx.target, ExpectedModel(**expected))
    finally:
        close()
    facts = {"workspace.verified": v.ok, "workspace.verify_detail": v.reason}
    return ToolResult(facts=facts, provider="verifier", limitations=[] if v.ok else [v.reason[:200]])


def _task_text(ctx: ToolContext) -> str:
    return "through the SSH tunnel, give the workspace model one small task (extract a number from a short text) and compare its answer with the answer known in advance"


TASK_TEXT, TASK_QUESTION, TASK_ANSWER = "The delivery has 12 boxes and 7 are already unpacked.", "How many boxes are still packed? Reply with the number only.", "5"


@tool(tags=("connect", "task", "active"), kinds=("remote",), ring=2, approval=_task_text, owner_thread=True, produces=("workspace.task_passed",), timeout_s=300)
def run_task_via_tunnel(ctx: ToolContext) -> ToolResult:
    """Run one small task through the workspace and score it.

    Args:
        none
    Returns:
        Fact workspace.task_passed (true when the reply contains the known answer) and the seconds it took.
    Tier: read
    Platforms: any
    Notes: one short question with a known numeric answer; scored by code, never by the model; asks the owner first.
    """
    from envrec.verify import TunnelClient
    if (bad := _no_route(ctx)) is not None:
        return bad
    try:
        local, close = _open_tunnel(ctx)
    except (ConnectionError, PermissionError) as exc:
        return ToolResult.fail(Reason.unavailable, str(exc)[:160])
    try:
        t0 = time.monotonic()
        reply = TunnelClient(local, timeout=120.0).chat(f"{TASK_TEXT}\n{TASK_QUESTION}")
        secs = round(time.monotonic() - t0, 2)
    except Exception as exc:
        return ToolResult(facts={"workspace.task_passed": False}, provider="verifier", limitations=[f"{type(exc).__name__}: {str(exc)[:100]}"])
    finally:
        close()
    return ToolResult(facts={"workspace.task_passed": bool(re.search(rf"\b{TASK_ANSWER}\b", reply)), "workspace.task_seconds": secs}, provider="verifier")


def _undo_text(ctx: ToolContext) -> str:
    return "undo the workspace: run each recorded undo command on the enrolled computer, newest first (you approve each), then check that nothing is left under the workspace folder"


def _undo_undo(ctx: ToolContext) -> dict:
    return {"tool": "undo_workspace", "args": {}, "text": "nothing further: this removes things"}


@tool(tags=("connect", "undo", "active"), kinds=("remote",), ring=2, approval=_undo_text, undo=_undo_undo, produces=("workspace.removed",), timeout_s=1200)
def undo_workspace(ctx: ToolContext) -> ToolResult:
    """Remove what the deploy installed and prove nothing is left.

    Args:
        none
    Returns:
        Fact workspace.removed: true only when the recipe's own `absent` check finds nothing under the workspace folder.
    Tier: write
    Platforms: any
    Notes: replays the undo journal newest first; each step asks the owner; the proof is the read-only absent check, not the exit codes of the undo commands.
    """
    if (bad := _no_route(ctx)) is not None:
        return bad
    try:
        ex = _executor(ctx)
    except RecipeNotApproved as exc:
        return ToolResult.fail(Reason.not_applicable, str(exc)[:160])
    results = ex.undo()
    if any(r.status == "denied" for r in results):
        return ToolResult(facts={"workspace.removed": False}, provider="stdlib", limitations=["the owner did not approve every undo step; the rest stays pending"])
    if any(r.status == "hang" for r in results):
        return ToolResult.fail(Reason.timeout, "HANG: an undo command did not answer in time; the entry stays pending")
    gone, detail = ex.nothing_left()
    if detail == "hang":
        return ToolResult.fail(Reason.timeout, "HANG: the absent check did not answer in time")
    return ToolResult(facts={"workspace.removed": gone}, provider="stdlib", limitations=[] if gone else [detail])
