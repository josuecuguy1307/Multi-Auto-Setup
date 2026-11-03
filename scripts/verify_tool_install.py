"""REAL check of the tool installer on this computer, in a throwaway folder (never the user's profile): for each catalog entry that carries install data, build the plan for THIS machine, run its steps with the
node's own functions (a real download from the pinned address, the SHA-256 check, the safe unpacking, the program started once with --version), then undo and see that nothing is left.

    python3 scripts/verify_tool_install.py [ENTRY_ID ...]      writes live_results/recommender-v3/tool_install.json"""
from __future__ import annotations

import json
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from envrec_coordinator import options as O  # noqa: E402
from envrec_coordinator import tool_plan as TP  # noqa: E402
from envrec_net import envrun as E  # noqa: E402
from envrec_node import executors as X  # noqa: E402
from envrec_node.envrun import finder  # noqa: E402
from envrec_node.setup import WriteScope  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "live_results" / "recommender-v3"


def main(argv: list[str]) -> int:
    envs, permissive, recipes = E.load_environments(), set(E.load_policy()["permissive_licenses"]), TP.load_tool_recipes()
    fam = {"darwin": "macos", "linux": "linux", "windows": "windows"}[platform.system().lower()]
    n = {"os": fam, "arch": O.ARCHES.get(platform.machine().lower(), platform.machine().lower()), "disk": shutil.disk_usage("/").free / 2 ** 30}
    rows = []
    for env in argv or [k for k, e in sorted(envs.items()) if "install" in e]:
        row: dict = {"environment": env, "machine": f"{fam}/{n['arch']}", "label": "REAL (download, SHA-256, unpack, run) in a throwaway folder"}
        home = Path(tempfile.mkdtemp(prefix=f"toolverify-{env}-"))
        try:
            plan = TP.build_tool_plan(envs[env], n, recipes, permissive, machine_id="this-mac")
            ctx = {"recipe": recipes[plan.recipe_id], "mas_home": home}
            t0, results = time.monotonic(), []
            for s in plan.steps:
                res, _ = getattr(X, s.op)(dict(s.args), WriteScope(home, list(s.writes)), ctx)
                results.append({"step": s.n, "op": s.op, "result": {k: v for k, v in res.items() if k in ("bytes", "sha256", "skipped", "files", "ok", "output", "binary")}})
            row.update(ok=True, seconds=round(time.monotonic() - t0, 1), pinned_sha256=plan.steps[0].args["sha256"], steps=results, found_by_name=bool(finder({"mas_home": home, "find_exe": lambda x: None})(envs[env]["adapter"]["exe"][{"macos": "darwin"}.get(fam, fam)])))
            for s in reversed(plan.steps):
                getattr(X, "undo_" + s.op)(dict(s.args), WriteScope(home, list(s.writes)), ctx)
            row["left_after_undo"] = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
        except Exception as exc:  # noqa: BLE001 - recorded, not hidden
            row.update(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            shutil.rmtree(home, ignore_errors=True)
        rows.append(row)
        print(json.dumps(row)[:400], flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tool_install.json").write_text(json.dumps({"measured_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + "Z", "rows": rows}, indent=1), encoding="utf-8")
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
