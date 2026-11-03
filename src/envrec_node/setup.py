"""The node's side of the one task that writes (docs/NETWORK_CONTRACT.md 3.9): `authorize` decides whether a `setup_step` may run, `run_step` runs it, and nothing runs before `authorize` passes.

`authorize` checks, in this order, with a typed reason for each refusal: the plan is well formed; its hash recomputed here is the hash it carries and the hash the signed token was made for
(`plan_altered`); the token is scoped to this recipe, step, action and these paths and carries an OWNER approval of exactly that (`bad_scope`, `no_owner_approval`); this computer has the recipe
(`unknown_recipe`) and its copy is the one the plan was made from (`stale_recipe`); the plan's operations are the recipe's, in the recipe's order (`plan_not_from_recipe`), and every path the step writes lies inside what the recipe
declares for its operation (`outside_declared_writes`). `run_step` then gives the operation's function a `WriteScope`: every path it opens is resolved and must lie inside the step's declared paths, under
`<mas>`, the node's own folder in the user's profile.

The operations themselves (download and check, unpack, write a configuration, start or stop the loopback server, remove) are data of the recipes plus small functions registered in `ctx["executors"]`
by the layer that knows the operating system; a node with no function for an operation refuses (`no_executor`). Nothing here starts a program from text a task carries."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from envrec_net import setup as P
from envrec_net import task as T
from envrec_os import SKILLS_DIR

RECIPES_FILE = SKILLS_DIR / "setup-recipes" / "references" / "recipes.json"
Executor = Callable[[dict, "WriteScope", dict], tuple[dict, list[dict]]]


class WriteRefused(T.TokenRejected):
    """A setup step that was refused before it ran, or a path an operation tried to write outside its declared paths. `reason` is one of T.REJECTIONS."""


def load_recipes(path: Path | None = None) -> dict[str, dict]:
    """The closed recipes this computer has: id -> {id, ops: {op: {writes: [<mas>/... prefixes]}}, sequence: [op...], repeat: [op...]}. A computer with none refuses every setup step."""
    p = path or RECIPES_FILE
    if not p.is_file():
        return {}
    doc = json.loads(p.read_text(encoding="utf-8"))
    return {r["id"]: r for r in [*doc.get("recipes", []), *doc.get("tool_recipes", [])]}


def _covered(path: str, declared: list[str]) -> bool:
    """`path` is one of the declared paths or inside one of them (a declared path ending in `/` is a folder)."""
    return any(path == d.rstrip("/") or path.startswith(d.rstrip("/") + "/") for d in declared)


def authorize(params: T.SetupStepParams, token: dict, recipes: Mapping[str, dict]) -> tuple[P.SetupPlan, P.SetupStep]:
    try:
        plan = P.parse_plan(params.plan)
        step = plan.step(params.step)
    except P.PlanError as exc:
        raise WriteRefused("bad_plan") from exc
    T.check_write_scope(token, plan_hash=P.plan_hash(plan), plan_declared_hash=plan.plan_hash, recipe=plan.recipe_id, step=step.n, action=params.action, writes=step.writes)
    recipe = recipes.get(plan.recipe_id)
    if recipe is None:
        raise WriteRefused("unknown_recipe")
    if P.recipe_sha256(recipe) != plan.recipe_sha256:
        raise WriteRefused("stale_recipe")
    ops, repeat = recipe.get("ops", {}), set(recipe.get("repeat", []))

    def shape(seq: list[str]) -> list[str]:  # an operation the recipe lets repeat (one download per file) counts once
        return [o for i, o in enumerate(seq) if not (i and o == seq[i - 1] and o in repeat)]
    if step.op not in ops or shape([x.op for x in plan.steps]) != shape(list(recipe.get("sequence", []))):
        raise WriteRefused("plan_not_from_recipe")  # an operation the recipe does not have, or the steps are not in the recipe's order
    declared = [P.clean_write_path(d) for d in ops[step.op].get("writes", [])]
    if not all(_covered(w, declared) for w in step.writes):
        raise WriteRefused("outside_declared_writes")
    return plan, step


class WriteScope:
    """What an operation may write: the step's declared paths, resolved under the node's own folder. `path()` is the only way an operation gets a place to write."""

    def __init__(self, mas_home: Path, declared: list[str]) -> None:
        self.home = Path(mas_home).resolve()
        self.declared = list(declared)

    def path(self, spec: str) -> Path:
        spec = P.clean_write_path(spec)
        if not _covered(spec, self.declared):
            raise WriteRefused("outside_declared_writes")
        target = (self.home / spec[len(P.WRITE_ROOT) + 1:].rstrip("/")).resolve()  # resolve() follows links: a link inside the folder that points out is caught here
        if self.home != target and self.home not in target.parents:
            raise WriteRefused("outside_declared_writes")
        return target

    def read_path(self, spec: str) -> Path:
        """A place inside the node's own folder to READ from (a file an earlier step wrote): it must still lie inside `<mas>`, but it need not be one of this step's declared writes."""
        return _read_inside(self.home, spec)


def _read_inside(home: Path, spec: str) -> Path:
    target = (home / P.clean_write_path(spec)[len(P.WRITE_ROOT) + 1:].rstrip("/")).resolve()
    if home != target and home not in target.parents:
        raise WriteRefused("outside_declared_writes")
    return target


def run_step(plan: P.SetupPlan, step: P.SetupStep, action: str, ctx: dict) -> tuple[dict, list[dict]]:
    """Run one authorized step (or its undo) with the operation's registered function. The result is facts and evidence like any task's."""
    executors: Mapping[str, Executor] = ctx.get("executors") or {}
    op = step.op if action == "run" else "undo_" + step.op
    fn = executors.get(op)
    if fn is None or ctx.get("mas_home") is None:
        raise WriteRefused("no_executor")
    scope = WriteScope(ctx["mas_home"], step.writes)
    result, evidence = fn(dict(step.args), scope, {**ctx, "recipe": (ctx.get("recipes") or {}).get(plan.recipe_id)})
    return {"plan_hash": plan.plan_hash, "step": step.n, "action": action, "op": step.op, **result}, evidence
