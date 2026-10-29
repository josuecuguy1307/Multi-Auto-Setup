"""`envrec skills list|show|verify`, `envrec status`, `envrec connect`, `envrec teardown`."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from envrec.skills.casetest import run_cases
from envrec.skills.lib import Library, validate


def _lib(args) -> Library:
    return Library(root=Path(args.skills)).load() if getattr(args, "skills", None) else Library().load()


def _kind(s) -> str:
    return "protocol" if s.meta.states is not None else ("skill" if s.meta.plan is not None else "guidance")


def cmd_skills(args) -> int:
    lib = _lib(args)
    if args.skills_cmd == "list":
        rows = [s for s in lib.all() if (not args.kind or _kind(s) == args.kind) and (not args.generated or s.meta.provenance == "generated")]
        print(f"{'NAME':24} {'KIND':9} {'PROV':9} {'TIER':8} {'VERIFIED':8} DESCRIPTION")
        for s in rows:
            ver = lib.state(s.name).get("verified_runs", s.meta.verified_runs)
            print(f"{s.name:24} {_kind(s):9} {s.meta.provenance:9} {s.meta.tier:8} {ver:<8} {s.description[:70]}")
        print(f"\n{len(rows)} skills ({sum(1 for s in rows if s.meta.provenance == 'generated')} written by a model); {len(lib.errors)} folder(s) failed validation")
        for name, why in lib.errors:
            print(f"  REJECTED {name}: {why[:200]}", file=sys.stderr)
        return 0
    s = lib.get(args.name)
    if s is None:
        print(f"no skill named {args.name!r}", file=sys.stderr)
        return 2
    if args.skills_cmd == "show":
        m = s.meta
        print(f"{s.name} [{_kind(s)}, {m.provenance}, tier {m.tier}, ring {m.ring}, protocol {m.protocol}]\n{s.description}\n")
        if m.plan:
            print("plan:")
            for st in m.plan.steps:
                print(f"  {st.id}: {st.tool} {json.dumps(st.args)} on {','.join(st.platforms)}")
            print(f"  script: {m.plan.script}  produces: {', '.join(m.produces) or '-'}")
        if m.states:
            print("steps: " + " > ".join(m.states.states) + f"  (kinds: {', '.join(m.states.kinds)})")
        if m.baseline:
            print(f"baseline: {m.baseline}")
        tests = sorted((s.path / "tests").glob("*/expect.json")) if (s.path / "tests").is_dir() else []
        print(f"tests: {len(tests)}  references: {', '.join(p.name for p in (s.path / 'references').glob('*')) if (s.path / 'references').is_dir() else '-'}")
        print("\n" + "\n".join(s.body.splitlines()[: args.lines]))
        return 0
    # verify
    problems = validate(s)
    print(f"checks: {'ok' if not problems else '; '.join(problems)}")
    results = run_cases(s) if s.meta.plan and s.meta.plan.script else []
    for r in results:
        print(f"  test {r.case}: {'pass' if r.passed else 'FAIL ' + '; '.join(r.problems)[:200]}")
    ok = not problems and all(r.passed for r in results)
    if ok and args.device:
        print(f"recorded as verified on {args.device}: {lib.record_verified(s.name, args.device)}")
    print("VERIFIED" if ok else "NOT VERIFIED")
    return 0 if ok else 1


def cmd_status(args) -> int:
    from envrec.mission import load_targets
    from envrec.skills.sbx import docker_ready
    lib = Library().load()
    skills = [s for s in lib.runnable()]
    print(f"skills:   {len(skills)} with a plan ({sum(1 for s in skills if s.meta.provenance == 'generated')} written by a model), {len(lib.protocols())} protocols, {len(lib.errors)} rejected")
    print(f"sandbox:  {'ready (Docker and the python:3.12-alpine image)' if docker_ready() else 'NOT READY: skill scripts will not run (fails closed)'}")
    print(f"tools:    {len(__import__('envrec.tools', fromlist=['all_tools']).all_tools())} registered")
    print(f"ffmpeg:   {'present (camera tool can read frames)' if shutil.which('ffmpeg') else 'absent (camera tool unavailable)'}")
    try:
        import httpx
        r = httpx.get("http://localhost:11434/api/tags", timeout=2)
        print("ollama:   " + ", ".join(m["name"] for m in r.json().get("models", [])))
    except Exception:
        print("ollama:   not reachable on localhost:11434")
    p = Path(args.targets)
    if p.is_file():
        cfg = load_targets(p)
        print(f"targets:  {len(cfg.targets)} enrolled: " + ", ".join(f"{t.id} ({'/'.join(x.value for x in t.allowed_transports)})" for t in cfg.targets))
    else:
        print("targets:  no targets.yaml (nothing enrolled)")
    runs = sorted(Path("runs").glob("*"), key=lambda x: x.stat().st_mtime)[-3:] if Path("runs").is_dir() else []
    print("runs:     " + (", ".join(r.name for r in runs) or "none"))
    return 0


def cmd_connect(args) -> int:
    """Guided: one step at a time, each step shown before it happens and each needing a yes."""
    from envrec.mission import load_targets
    from envrec.remote.planner import HINTS, plan_transport, preference
    from envrec.remote.runner import MissingKey, RemoteRunner, TargetNotAllowed
    cfg = load_targets(args.targets)
    entry = cfg.get(args.target)
    if entry is None:
        print(f"{args.target!r} is not an entry of {args.targets}: nothing to connect to. Enroll it first (`envrec enroll`).", file=sys.stderr)
        return 2
    ask = (lambda q: "y") if args.yes else (lambda q: input(q + " [y/N] ").strip().lower())
    print(f"STEP 1 of 4, route. {entry.name} allows: {', '.join(t.value for t in entry.allowed_transports)}.")
    pref, why = preference({})
    order = [t for t in pref if t in entry.allowed_transports] + [t for t in entry.allowed_transports if t not in pref]
    print(f"  order tried, from the route table and what is known ({'; '.join(why)}): {', '.join(t.value for t in order)}")
    runner = RemoteRunner(cfg, host_key_policy="yes")
    reach: dict = {}
    print("STEP 2 of 4, read-only reachability. For each allowed route I will run ONE read-only command (`uname -s`, or a listen/list for serial, adb and docker).")
    for t in order:
        if ask(f"  check {t.value}?") != "y":
            print(f"  skipped {t.value}")
            continue
        try:
            reach[t] = runner.ios_attached(entry.id) if t.value == "ios_usb" else runner.run(entry.id, t, "uname_s", timeout=15).ok if t.value in ("ssh", "docker_exec", "adb_usb") else (runner.serial_listen(entry.id).ok if t.value == "serial" else runner.tcp_read(entry.id).ok)
        except (MissingKey, TargetNotAllowed, OSError) as exc:
            print(f"  {t.value}: cannot ({type(exc).__name__}: {str(exc)[:80]})")
            reach[t] = False
            continue
        print(f"  {t.value}: {'answered' if reach[t] else 'did not answer'}")
    plan = plan_transport(entry, {}, reach)
    if plan.transport is None:
        print(f"STEP 3 of 4, plan. Stopped: {plan.hint}")
        return 3
    print(f"STEP 3 of 4, plan. Route: {plan.transport.value} ({'; '.join(plan.decided_by)}).\n  Next you can run `envrec deploy --target {entry.id}` (every write asks again with its exact command) and `envrec undo --target {entry.id}` takes it all off.")
    print("STEP 4 of 4, verify. After a deploy the harness asks the server for its health and for one real generation; I did not change anything on the device.")
    return 0


def cmd_teardown(args) -> int:
    from envrec import lab
    from envrec.cli_fleet import cmd_undo
    code = 0
    if args.target:
        args.release, args.artifacts = "build/worker/release.json", "build/worker/dist"
        code = cmd_undo(args)
    if args.lab:
        lab.down()
        print("the Docker lab is removed")
    for d in args.remove_runs or []:
        shutil.rmtree(d, ignore_errors=True)
        print(f"removed {d}")
    if not (args.target or args.lab or args.remove_runs):
        print("nothing to tear down: name --target <id>, --lab, or --remove-runs <dir>")
    return code


def add_skills_commands(sub) -> None:
    p = sub.add_parser("skills", help="the skill library: list, show, verify")
    p.add_argument("--skills", default=None, help="a skills directory (default: skills/)")
    ss = p.add_subparsers(dest="skills_cmd", required=True)
    ls = ss.add_parser("list")
    ls.add_argument("--kind", choices=["skill", "protocol", "guidance"], default=None)
    ls.add_argument("--generated", action="store_true")
    sh = ss.add_parser("show")
    sh.add_argument("name")
    sh.add_argument("--lines", type=int, default=40)
    vf = ss.add_parser("verify")
    vf.add_argument("name")
    vf.add_argument("--device", default=None, help="record the skill as verified on this device id when everything passes")
    p.set_defaults(func=cmd_skills)
    st = sub.add_parser("status", help="what the harness has: skills, sandbox, models, enrolled devices")
    st.add_argument("--targets", default="targets.yaml")
    st.set_defaults(func=cmd_status)
    c = sub.add_parser("connect", help="guided: choose a route to one enrolled device, with a yes at every step")
    c.add_argument("--target", required=True)
    c.add_argument("--targets", default="targets.yaml")
    c.add_argument("--yes", action="store_true", help="answer yes to the read-only checks (writes still ask)")
    c.set_defaults(func=cmd_connect)
    t = sub.add_parser("teardown", help="undo a deployment, remove the lab, or remove run folders")
    t.add_argument("--target", default=None)
    t.add_argument("--targets", default="targets.yaml")
    t.add_argument("--store", default="runs/fleet")
    t.add_argument("--lab", action="store_true")
    t.add_argument("--remove-runs", nargs="*", default=None)
    t.set_defaults(func=cmd_teardown)
