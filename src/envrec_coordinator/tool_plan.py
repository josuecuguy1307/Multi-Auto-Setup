"""The plan to install a command-line tool from the assets of a GitHub release (recipe `github-release-tool`, data). PURE CODE, like `setup_plan`: the catalog entry says where each file is (address, size,
SHA-256 per operating system and architecture, and the file that answers once unpacked), the recipe says the steps and the commands the owner reads, the machine's measured facts pick the asset. Nothing
here knows a tool by name. An entry that is not verified is not installable: `install_state` says why with a typed state, and the owner is told.

A tool lands in a folder of its own (`<mas>/tools/...`) in the user's profile: no administrator, nothing outside, undone by deleting that folder."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlparse

from envrec_net import setup as P
from envrec_coordinator.setup_plan import PlanRefused, _say, _seg, load_recipes  # noqa: F401 - the same refusal type and helpers as the model plan

GIB = 2 ** 30
SHA64 = re.compile(r"^[0-9a-f]{64}$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
TOOL_KIND = "tool"
STATES = {
    "no_installer": "this entry carries no install data, so the harness cannot install it",
    "unverified_entry": "its install data is not verified by code (pin, SHA-256 per file, size, permissive license)",
}


def load_tool_recipes(path=None) -> dict[str, dict]:
    import json

    from envrec_coordinator.setup_plan import RECIPES_FILE
    doc = json.loads((path or RECIPES_FILE).read_text(encoding="utf-8"))
    return {r["id"]: r for r in doc.get("tool_recipes", [])}


def _pack_state(entry: dict, rec: dict | None, permissive: set[str]) -> tuple[str, str]:
    problems = []
    if rec is None:
        problems.append("the recipe is not one the harness has")
    if (entry.get("license") or {}).get("spdx") not in permissive:
        problems.append(f"its license {(entry.get('license') or {}).get('spdx')!r} is not permissive")
    up = entry.get("upstream") or {}
    commit = str(up.get("commit") or "")
    if not SHA40.match(commit):
        problems.append("no pinned commit")
    files = up.get("files") or {}
    if not files:
        problems.append("no files pinned")
    for name, f in files.items():
        u = urlparse(str(f.get("url", "")))
        if rec and (u.scheme not in rec["download"]["schemes"] or (u.hostname or "") not in rec["download"]["hosts"] or commit not in u.path):
            problems.append(f"file {name}: the address is not on a host the recipe downloads from, pinned to the commit")
        if not SHA64.match(str(f.get("sha256", ""))) or not isinstance(f.get("bytes"), int) or f["bytes"] <= 0:
            problems.append(f"file {name}: no SHA-256 or no size")
    return ("installable", "") if not problems else ("unverified_entry", "; ".join(problems[:4]))


def install_state(entry: dict, permissive: set[str], recipes: dict[str, dict] | None = None) -> tuple[str, str]:
    """('installable', '') or (a typed state, why). Installable only when CODE can verify it: an install block naming a recipe the harness has, a pin (commit), a permissive license and, for every asset, an
    https address on the recipe's host, a size and a 64-hex SHA-256."""
    inst = entry.get("install")
    if not isinstance(inst, dict):
        return "no_installer", STATES["no_installer"]
    recipes = recipes if recipes is not None else load_tool_recipes()
    rec = recipes.get(inst.get("recipe", "github-release-tool"))
    if entry.get("subtype") == "skill_pack":
        return _pack_state(entry, rec, permissive)
    problems: list[str] = []
    if rec is None:
        problems.append("the recipe is not one the harness has")
    if (entry.get("license") or {}).get("spdx") not in permissive:
        problems.append(f"its license {(entry.get('license') or {}).get('spdx')!r} is not permissive")
    up = entry.get("upstream") or {}
    if not (up.get("commit") or entry.get("pin")):
        problems.append("no pinned commit")
    assets = up.get("assets") or {}
    if not assets:
        problems.append("no assets pinned")
    for k, a in assets.items():
        u = urlparse(str(a.get("url", "")))
        if rec and (u.scheme not in rec["download"]["schemes"] or (u.hostname or "") not in rec["download"]["hosts"]):
            problems.append(f"asset {k}: the address is not https on a host the recipe downloads from")
        if not SHA64.match(str(a.get("sha256", ""))) or not isinstance(a.get("bytes"), int) or a["bytes"] <= 0:
            problems.append(f"asset {k}: no SHA-256 or no size")
    for fam, per in (inst.get("asset_keys") or {}).items():
        for arch, key in per.items():
            if key not in assets:
                problems.append(f"{fam}/{arch}: the asset {key!r} is not pinned")
    if not inst.get("binary") or not inst.get("verify", {}).get("argv"):
        problems.append("no program name or no verification")
    return ("installable", "") if not problems else ("unverified_entry", "; ".join(problems[:4]))


def build_pack_plan(entry: dict, n: dict, recipe: dict, *, machine_id: str, installed: set[str] | None, sensitivity: str) -> P.SetupPlan:
    """The plan for a file-based skill pack: one download-and-check step per file into the pack's own folder. The pack is for the environments it names: with `installed` given, at least one of them must be on the machine."""
    if installed is not None and not (set(entry["pack_for"]) & installed):
        raise PlanRefused("no_environment_for_pack", f"{entry['title']} is for {', '.join(entry['pack_for'])}, and none of them is installed on this computer")
    family = n["os"]
    folder = f"{P.WRITE_ROOT}/packs/{_seg(entry['id'])}/"
    cmd, undo = recipe["commands"]["fetch_check"], recipe["undo"]["fetch_check"]
    steps = []
    for name, f in sorted(entry["upstream"]["files"].items()):
        dest = f"{folder}{name}"
        steps.append({"title": f"Download {name} of {entry['title']}", "kind": "download", "op": "fetch_check", "args": {"url": f["url"], "sha256": f["sha256"], "bytes": f["bytes"], "dest": dest},
                      "exact_command": _say(cmd, family, "download command").format(url=f["url"], dest=dest, sha256=f["sha256"]), "writes": [dest], "bytes": int(f["bytes"]), "undo": _say(undo, family, "undo").format(path=dest)})
    seq = [P.SetupStep(n=i, **s) for i, s in enumerate(steps, 1)]
    pid = hashlib.sha256("|".join([entry["id"], entry["upstream"]["commit"], *(f["sha256"] for _, f in sorted(entry["upstream"]["files"].items()))]).encode()).hexdigest()[:12]
    plan = P.SetupPlan(plan_id=f"pack-{pid}", machine_id=machine_id, option_id=f"pack-{entry['id']}", sensitivity=sensitivity, recipe_id=recipe["id"], recipe_sha256=P.recipe_sha256(recipe),
                       fit=P.Fit(model_bytes=0, ram_available_bytes=0, fits=True, limited_by="files only: no model memory"), steps=seq, verification=P.Verification())
    return P.seal(plan)


def build_tool_plan(entry: dict, n: dict, recipes: dict[str, dict], permissive: set[str], *, machine_id: str, sensitivity: str = "internal", installed: set[str] | None = None) -> P.SetupPlan:
    """The plan for this entry on a machine whose normalised facts are `n` (`os` family, `arch`, free disk in GiB). A refusal is typed: unverified_entry / no_installer / unsupported_cell / no_disk / no_recipe."""
    state, why = install_state(entry, permissive, recipes)
    if state != "installable":
        raise PlanRefused(state, why)
    inst = entry["install"]
    recipe = recipes[inst.get("recipe", "github-release-tool")]
    if entry.get("subtype") == "skill_pack":
        return build_pack_plan(entry, n, recipe, machine_id=machine_id, installed=installed, sensitivity=sensitivity)
    family, arch = n["os"], n["arch"]
    per = inst["asset_keys"].get(family) or {}
    key = next((per[a] for a in [arch, *recipe.get("arch_aliases", {}).get(arch, [])] if a in per), None)
    if key is None:
        raise PlanRefused("unsupported_cell", f"{entry['id']} has no build for {family} on {arch or 'an unknown architecture'}")
    asset = entry["upstream"]["assets"][key]
    if float(n.get("disk") or 0) and float(n["disk"]) * GIB < asset["bytes"] * float(recipe["download"]["disk_margin"]) * 2:
        raise PlanRefused("no_disk", f"needs about {round(asset['bytes'] * 2 / GIB, 2)} GiB of disk and only {n['disk']} GiB is free")
    fmt = next((v for k, v in recipe["archive_formats"].items() if asset["name"].endswith(k)), None)
    if fmt is None:
        raise PlanRefused("unknown_archive", f"the recipe does not know how to unpack {asset['name']}")
    release = (entry["upstream"].get("release") or entry["upstream"].get("seen_release") or "pinned")
    folder = f"{P.WRITE_ROOT}/tools/{_seg(entry['id'], release, key)}/"
    archive = f"{P.WRITE_ROOT}/downloads/{_seg(entry['id'], release)}-{asset['name']}"
    binary_name = inst["binary"].get(key) or inst["binary"].get(family)
    if not binary_name:
        raise PlanRefused("unverified_entry", f"the entry names no program for {family} / {key}")
    binary_path = f"{folder}{binary_name}"
    cmd, undo = recipe["commands"], recipe["undo"]
    argv = list(inst["verify"]["argv"])
    steps: list[dict[str, Any]] = []
    for op in recipe["sequence"]:
        if op == "fetch_check":
            steps.append({"title": f"Download {asset['name']} ({entry['title']} {release})", "kind": "download", "op": op, "args": {"url": asset["url"], "sha256": asset["sha256"], "bytes": asset["bytes"], "dest": archive},
                          "exact_command": _say(cmd[op], family, "download command").format(url=asset["url"], dest=archive, sha256=asset["sha256"]), "writes": [archive], "bytes": int(asset["bytes"]),
                          "undo": _say(undo[op], family, "undo").format(path=archive)})
        elif op == "unpack_archive":
            steps.append({"title": f"Unpack {entry['title']} into its own folder", "kind": "install", "op": op, "args": {"archive": archive, "dest": folder, "format": fmt}, "writes": [folder], "bytes": 0,
                          "exact_command": _say(cmd[op][fmt], family, "unpack command").format(archive=archive, dest=folder), "undo": _say(undo[op], family, "undo").format(path=folder)})
        elif op == "verify_program":
            steps.append({"title": f"Run {entry['title']} once to see that it starts", "kind": "verify", "op": op,
                          "args": {"tool_dir": folder, "binary_path": binary_path, "argv": argv, "exe": entry["adapter"]["exe"][n_family(family)] if entry.get("adapter") else binary_name, **({"expect": inst["verify"]["expect"]} if inst["verify"].get("expect") else {})},
                          "writes": [folder], "bytes": 0, "exact_command": _say(cmd[op], family, "command").format(binary=binary_path, argv=" ".join(argv)), "undo": _say(undo[op], family, "undo")})
        else:
            raise PlanRefused("no_recipe", f"the recipe has an operation this builder does not know: {op}")
    seq = [P.SetupStep(n=i, **s) for i, s in enumerate(steps, 1)]
    pid = hashlib.sha256("|".join([entry["id"], asset["sha256"], key, release]).encode()).hexdigest()[:12]
    plan = P.SetupPlan(plan_id=f"tool-{pid}", machine_id=machine_id, option_id=f"tool-{entry['id']}", sensitivity=sensitivity, recipe_id=recipe["id"], recipe_sha256=P.recipe_sha256(recipe),
                       fit=P.Fit(model_bytes=0, ram_available_bytes=0, fits=True, limited_by="a command-line tool: no model memory"), steps=seq, verification=P.Verification())
    return P.seal(plan)


def n_family(family: str) -> str:
    """The adapter's table is keyed darwin/linux/windows; the setup facts say macos/linux/windows."""
    return {"macos": "darwin"}.get(family, family)


def check_tool_pins(entries: dict[str, dict], permissive: set[str], recipes: dict[str, dict] | None = None) -> list[str]:
    """For every entry that carries an install block: the problems `install_state` finds (empty when all verify). A shipped entry with an install block must verify."""
    out = []
    for k, e in sorted(entries.items()):
        if "install" in e:
            st, why = install_state(e, permissive, recipes)
            if st != "installable":
                out.append(f"{k}: {st}: {why}")
    return out
