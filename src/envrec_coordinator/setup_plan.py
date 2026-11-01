"""Build a `setup_plan` for the option the owner picked: a PURE CODE tool (the model chooses to call it, it never writes the plan). Three inputs decide everything, none of them typed by a model: the closed
recipe (skill `setup-recipes`), the machine's measured facts (which pick the build from the matrix of `model-catalog-rules`) and the catalog's pinned addresses, sizes and SHA-256 (verified-by-code).

Nothing here knows an operating system by name: the commands shown to the owner, the name of the server program, the archive format and how many layers go to the graphics memory are rows of the recipe
data, chosen by the operating-system family and the backend of the matrix cell. A cell the matrix does not support, a build the catalog lacks, a model that does not fit the measured memory or a disk
that is too small ends in a typed `PlanRefused`, never in a guessed plan."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from envrec.catalog import rules as R
from envrec_coordinator import options as O
from envrec_net import setup as P
from envrec_os import SKILLS_DIR

RECIPES_FILE = SKILLS_DIR / "setup-recipes" / "references" / "recipes.json"
GIB = 2 ** 30
KINDS = {"fetch_check": "download", "unpack_archive": "install", "write_server_config": "configure", "start_server": "start", "minimal_call": "verify"}


class PlanRefused(Exception):
    """No plan was built, and why: `reason` is a typed code (unsupported_cell, no_build_in_catalog, no_cell_for_facts, backend_not_supported, does_not_fit, no_disk, build_changed, no_recipe, ...)."""

    def __init__(self, reason: str, text: str) -> None:
        super().__init__(f"{reason}: {text}")
        self.reason, self.text = reason, text


def load_recipes(path: Path | None = None) -> dict[str, dict]:
    p = path or RECIPES_FILE
    return {r["id"]: r for r in json.loads(p.read_text(encoding="utf-8"))["recipes"]} if p.is_file() else {}


def server_argv(server: dict, **v: Any) -> list[str]:
    """The server's arguments from the recipe's template. The owner reads this list joined, and the node runs this list (it builds it from its own copy of the same template): one source."""
    parts = list(server["argv"]) + (list(server["argv_mmproj"]) if v.get("mmproj") else [])
    return [str(x).format(**{k: val for k, val in v.items()}) for x in parts]


def _seg(*parts: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", "-".join(parts)).strip("-.")[:60]


def _say(table: dict[str, str], family: str, what: str) -> str:
    if family not in table:
        raise PlanRefused("no_recipe_for_os", f"the recipe has no {what} for the operating system family {family!r}")
    return table[family]


def build_plan(option: dict, facts: dict, catalog: dict, recipes: dict[str, dict], rules: R.Rules, *, machine_id: str, sensitivity: str = "internal", recipe_id: str = "local-server",
               port: int | None = None) -> P.SetupPlan:
    if option.get("origin") != "catalog_runtime":
        raise PlanRefused("no_recipe", "only a model run on this machine from the catalog has a setup recipe (a command-line tool or an API key is not installed by this)")
    recipe = recipes.get(recipe_id)
    if recipe is None:
        raise PlanRefused("no_recipe", f"there is no recipe named {recipe_id!r}")
    n = O._norm(facts)
    machine = O._machine(n)
    sel = R.select_build(machine, catalog.get("runtime_assets", []), rules)
    if not sel["ok"]:
        raise PlanRefused(sel["reason"], sel["text"])
    asset = sel["asset"]
    if asset["source"] != recipe["runtime_source"]:
        raise PlanRefused("no_recipe", f"the recipe is for {recipe['runtime_source']}, the build is {asset['source']}")
    if (option.get("build") or {}).get("cell") != sel["cell"]:
        raise PlanRefused("build_changed", f"the option was made for another kind of machine ({(option.get('build') or {}).get('cell')}), this one is {sel['cell']}: ask for the options again")
    row = next((r for r in catalog.get("model_files", []) if r.get("id") == option.get("model_id")), None)
    if row is None:
        raise PlanRefused("unknown_model", f"the catalog has no model {option.get('model_id')!r}")
    family = n["os"]
    server = recipe["server"]
    ctx, port = int(server["context"]), int(port or server["port"])
    budget, basis = R.memory_budget(machine, rules)
    est = R.estimate_memory(row, rules, context=ctx)
    if est["total_gib"] > budget:
        raise PlanRefused("does_not_fit", f"needs {est['total_gib']} GiB ({est['label']}) and only {budget} GiB of {basis} may be used for a model")
    need = R.disk_need_gib(row, asset["bytes"], rules)
    if need > n["disk"]:
        raise PlanRefused("no_disk", f"needs {need} GiB of disk and only {n['disk']} GiB is free")

    fmt = next((v for k, v in recipe["archive_formats"].items() if asset["file"].endswith(k)), None)
    if fmt is None:
        raise PlanRefused("unknown_archive", f"the recipe does not know how to unpack {asset['file']}")
    ngl = recipe["backend_args"].get(sel["backend"], {}).get("ngl")
    if ngl is None:
        raise PlanRefused("backend_not_supported", f"the recipe has no server arguments for the {sel['backend']} backend")
    runtime_dir = f"{P.WRITE_ROOT}/runtime/{_seg(asset['tag'], asset['os'], asset['arch'], asset['backend'])}/"
    archive = f"{P.WRITE_ROOT}/downloads/{asset['file']}"
    parts = row.get("shards") or [{"file": row["file"], "url": row["url"], "sha256": row["sha256"], "bytes": row["bytes"]}]
    model_path = f"{P.WRITE_ROOT}/downloads/{parts[0]['file']}"
    projectors = [c for c in row.get("companions", []) if c.get("kind") == "vision-projector"][:1]  # the projector of a vision model (one): downloaded with it, passed to the server
    mmproj_path = f"{P.WRITE_ROOT}/downloads/{projectors[0]['file']}" if projectors else None
    config = f"{P.WRITE_ROOT}/config/server.json"
    binary = _say(server["binary"], family, "server program name")
    cmd, undo = recipe["commands"], recipe["undo"]

    def fetch(src: dict, dest: str, what: str) -> dict:
        return {"title": f"Download {what}", "op": "fetch_check", "args": {"url": src["url"], "sha256": src["sha256"], "bytes": src["bytes"], "dest": dest},
                "exact_command": _say(cmd["fetch_check"], family, "download command").format(url=src["url"], dest=dest, sha256=src["sha256"]), "writes": [dest], "bytes": int(src["bytes"]),
                "undo": _say(undo["fetch_check"], family, "undo").format(path=dest)}
    raw: list[dict] = []
    fetched = 0
    for op in recipe["sequence"]:
        if op == "fetch_check" and fetched == 0:
            raw.append(fetch({"url": asset["url"], "sha256": asset["sha256"], "bytes": asset["bytes"]}, archive, f"the {asset['source']} {sel['backend']} runtime ({asset['file']})"))
            fetched += 1
        elif op == "fetch_check":
            raw += [fetch(p, f"{P.WRITE_ROOT}/downloads/{p['file']}", f"the model file {p['file']}" + (f" (part {i} of {len(parts)})" if len(parts) > 1 else "")) for i, p in enumerate(parts, 1)]
            raw += [fetch(p, f"{P.WRITE_ROOT}/downloads/{p['file']}", f"the vision projector {p['file']}") for p in projectors]
        elif op == "unpack_archive":
            raw.append({"title": "Unpack the runtime", "op": op, "args": {"archive": archive, "dest": runtime_dir, "format": fmt}, "writes": [runtime_dir], "bytes": 0,
                        "exact_command": cmd[op][fmt].format(archive=archive, dest=runtime_dir), "undo": _say(undo[op], family, "undo").format(path=runtime_dir)})
        elif op == "write_server_config":
            raw.append({"title": "Write the server's settings", "op": op, "args": {"path": config, "model": model_path, "host": server["host"], "port": port, "context": ctx, "ngl": ngl, "backend": sel["backend"], "mmproj": mmproj_path},
                        "writes": [config], "bytes": 0, "exact_command": _say(cmd[op], family, "command").format(path=config), "undo": _say(undo[op], family, "undo").format(path=config)})
        elif op == "start_server":
            raw.append({"title": "Start the local server (loopback only)", "op": op, "args": {"runtime_dir": runtime_dir, "binary_name": binary, "config": config, "model": model_path, "host": server["host"], "port": port,
                                                                                               "context": ctx, "ngl": ngl, "mmproj": mmproj_path}, "writes": [f"{P.WRITE_ROOT}/run/"], "bytes": 0,
                        "exact_command": _say(cmd[op], family, "command").format(binary=f"{runtime_dir}{binary}", argv=" ".join(server_argv(server, model=model_path, host=server["host"], port=port, context=ctx, ngl=ngl, mmproj=mmproj_path))), "undo": _say(undo[op], family, "undo")})
        elif op == "minimal_call":
            raw.append({"title": "Ask the model one short question and time the answer", "op": op, "args": {"host": server["host"], "port": port, "prompt": server["prompt"], "max_tokens": server["max_tokens"]},
                        "writes": [], "bytes": 0, "exact_command": _say(cmd[op], family, "command").format(port=port), "undo": _say(undo[op], family, "undo")})
        else:
            raise PlanRefused("no_recipe", f"the recipe has an operation this builder does not know: {op}")
    steps = [P.SetupStep(n=i, kind=KINDS[s["op"]], **s) for i, s in enumerate(raw, 1)]
    pid = hashlib.sha256("|".join([option["id"], asset["sha256"], row["sha256"], str(port)]).encode()).hexdigest()[:12]
    plan = P.SetupPlan(plan_id=f"plan-{pid}", machine_id=machine_id, option_id=option["id"], sensitivity=sensitivity, recipe_id=recipe["id"], recipe_sha256=P.recipe_sha256(recipe),
                       fit=P.Fit(model_bytes=int(row["bytes"]), ram_available_bytes=int(float(n["ram_free"] if n["ram_free"] is not None else n["ram"]) * GIB), fits=True, limited_by=basis),
                       steps=steps, verification=P.Verification(endpoint=f"{server['host']}:{port}", ok=False, latency_ms=0, evidence="untested"))
    return P.seal(plan)


def check_pins(catalog: dict, recipes: dict[str, dict], rules: R.Rules) -> list[str]:
    """Problems with what a plan would download, for every runtime build and every model the catalog lists, against the recipe that downloads it. Empty means every address is on a host the recipe allows,
    over a scheme it allows, with a pinned revision or commit, a 64-hex SHA-256 and a size; every shard and projector too; and every operation of every recipe has what each OS family needs."""
    import re as _re
    from urllib.parse import urlparse
    out: list[str] = []
    hex64, hex40 = _re.compile(r"^[0-9a-f]{64}$"), _re.compile(r"^[0-9a-f]{40}$")
    by_source = {r["runtime_source"]: r for r in recipes.values()}

    def pin(what: str, url: str, sha: str, size: Any, rec: dict | None) -> None:
        u = urlparse(str(url))
        if rec is None:
            out.append(f"{what}: no recipe downloads it")
            return
        dl = rec["download"]
        if u.scheme not in dl["schemes"]:
            out.append(f"{what}: scheme {u.scheme!r} is not allowed")
        if (u.hostname or "") not in dl["hosts"]:
            out.append(f"{what}: host {u.hostname!r} is not one the recipe downloads from")
        if not hex64.match(str(sha)):
            out.append(f"{what}: no pinned SHA-256")
        if not isinstance(size, int) or size <= 0:
            out.append(f"{what}: no size")
    for a in catalog.get("runtime_assets", []):
        rec = by_source.get(a["source"])
        if rec is None:
            continue  # a runtime no recipe sets up is not offered (options say no_recipe_yet), so it carries no download to check
        pin(f"runtime {a['id']}", a["url"], a["sha256"], a["bytes"], rec)
        if not hex40.match(str(a.get("commit") or "")):
            out.append(f"runtime {a['id']}: no pinned commit")
    rec = next(iter(by_source.values()), None)
    for m in catalog.get("model_files", []):
        pin(f"model {m['id']}", m["url"], m["sha256"], m["bytes"], rec)
        if not hex40.match(str(m.get("revision"))) or m.get("revision") not in str(m["url"]):
            out.append(f"model {m['id']}: the address is not pinned to its revision")
        for sh in m.get("shards", []):
            pin(f"shard {sh['file']} of {m['id']}", sh["url"], sh["sha256"], sh["bytes"], rec)
        if m.get("shards") and sum(sh["bytes"] for sh in m["shards"]) != m["bytes"]:
            out.append(f"model {m['id']}: the shards do not add up to its size")
        for c in m.get("companions", []):
            pin(f"projector {c['file']} of {m['id']}", c["url"], c["sha256"], c["bytes"], rec)
    for rid, r in recipes.items():
        ops = set(r["ops"])
        if set(r["sequence"]) - ops:
            out.append(f"recipe {rid}: the sequence names an operation it does not declare")
        for fam in ("macos", "linux", "windows"):
            for op in ops:
                c = r["commands"].get(op, {})
                if not (fam in c or (op == "unpack_archive" and set(c) >= set(r["archive_formats"].values()))):
                    out.append(f"recipe {rid}: no command for {op} on {fam}")
                u = r["undo"].get(op, {})
                if fam not in u:
                    out.append(f"recipe {rid}: no undo for {op} on {fam}")
            if fam not in r["server"]["binary"]:
                out.append(f"recipe {rid}: no server program name for {fam}")
        for cell in rules.data["build_matrix"]["cells"]:
            b = cell.get("backend")
            if b and rules.data["build_matrix"]["backends"][b].get("supported") and b not in r["backend_args"]:
                out.append(f"recipe {rid}: no server arguments for the {b} backend")
    return out
