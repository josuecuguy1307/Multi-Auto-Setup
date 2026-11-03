"""The lifecycle commands: `mas service`, `mas update`, `mas unpair`, `mas forget`, `mas coordinator rotate-identity`.

Owner-facing text is English. Every command that changes the machine shows what it will do and how to undo it, then asks; `--yes` is the owner's answer given in advance for exactly what
was shown (it never widens anything). Where the machine is touched (services, programs) the per-OS layer and its skill data do it; this module only asks and reports."""

from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import envrec_os
from envrec_os import service as svc
from envrec_mas import coordconfig
from envrec_mas import release as rel

EXIT_USAGE = 2
UNPAIR_WAIT_S = 5.0  # how long `mas unpair` waits for a running coordinator before it concludes there is none
ROLE_COMMAND = {"node": ["node", "run"], "coordinator": ["coordinator", "start"]}
PYTHON_VERSION = "3.13"


def ask_yes(question: str, *, yes: bool = False, input_fn: Callable[[str], str] | None = None) -> bool:
    """The owner's answer. Anything but an explicit yes is a no; no terminal is a no."""
    if yes:
        print(f"{question} y  (--yes)")
        return True
    try:
        return (input_fn or input)(f"{question} [y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---- mas service -------------------------------------------------------------------------------------------------------------------------------

def service_argv(role: str, state: Path, *, python: str | None = None) -> list[str]:
    """What the service runs. Nothing about HOW the coordinator runs is in the command: its settings (address, port, chat bridge, model profile...) are in <state>/coordinator.json, so changing
    one is editing data (or `mas service install` again), never a new command line."""
    log = state / f"service-{role}.log"
    return [python or svc.current_python(), "-m", "envrec_mas", "--state-dir", str(state), "--log-file", str(log)] + ROLE_COMMAND[role]


def _state(a) -> Path:
    try:
        return Path(a.state_dir).expanduser() if a.state_dir else envrec_os.default_state_dir()
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from None


COORDINATOR_NOTE = ("A coordinator listens on ONE port of your network (7443 by default, and optionally UDP 7444 for discovery). If your firewall asks, you decide: this program never changes the firewall, "
                    "the network profile, the clock or the router. Also, --bind is this computer's address on your network: if the router gives it another one, the service will not start; it is best to reserve a fixed one for it.")


def _coordinator_config(state: Path, given: dict) -> dict:
    """The settings the coordinator service will start with: what is in the file now, plus what this command gave. A NEW file also gets the schema's `service_defaults` (a service has no terminal, so
    the chat bridge is on unless the owner says --no-web). Refused before anything is written when a value does not have its type."""
    schema = coordconfig.load_schema()
    filed = coordconfig.read(state, schema)
    base = filed if filed else dict(schema.get("service_defaults", {}))
    return coordconfig.check({**base, **given}, schema)


def cmd_service(a, *, runner: svc.Runner = svc.default_runner) -> int:
    state = _state(a)
    role = a.role
    if a.action != "status" and role == "node" and a.action == "install":
        from envrec_node.state import NodeState
        if NodeState(state).pin() is None:
            print("This node is not paired yet, so a service would have nobody to call. Run `mas join` first. Nothing was changed.", file=sys.stderr)
            return 1
    new_cfg: dict | None = None
    if role == "coordinator" and a.action == "install":
        try:
            new_cfg = _coordinator_config(state, coordconfig.from_namespace(a))
        except coordconfig.ConfigRefused as exc:
            print(f"Cannot do that: {exc.text} [{exc.reason}]. Nothing was changed.", file=sys.stderr)
            return EXIT_USAGE
        if not new_cfg.get("bind"):
            print("A coordinator needs `--bind ADDRESS` (this computer's address on your network). Nothing was changed.", file=sys.stderr)
            return EXIT_USAGE
    argv = service_argv(role, state)
    try:
        plan = svc.plan(role, argv, runner=runner)
    except svc.ServiceRefused as exc:
        print(f"Cannot do that: {exc.text} [{exc.reason}]", file=sys.stderr)
        return 1
    if a.action == "status":
        here = svc.installed(plan, runner=runner)
        up = svc.running(plan, runner=runner)
        print(f"{role} service: " + ("installed" if here else "not installed") + (f" ({plan.kind})" if here else "") + ("" if up is None or not here else ", running" if up else ", stopped"))
        print(f"log: {state / f'service-{role}.log'}")
        return 0 if here else 1
    if a.action == "install":
        print("This is what will be done on this computer:\n" + plan.describe())
        if role == "coordinator":
            print(f"\nSettings it will start with, written to {coordconfig.path_of(state)} (edit that file, or run this command again, to change them; undo: delete the file):")
            print(json.dumps(new_cfg, indent=1, sort_keys=True))
            print("\n" + COORDINATOR_NOTE)
        print(f"\nService log: {state / f'service-{role}.log'}")
        if not ask_yes("Install the service?", yes=a.yes):
            print("Nothing was changed.")
            return 1
        if new_cfg is not None:
            coordconfig.write(state, new_cfg)
            print(f"  wrote {coordconfig.path_of(state)}")
        try:
            for line in svc.install(plan, runner=runner):
                print("  " + line)
        except svc.ServiceRefused as exc:
            print(exc.text, file=sys.stderr)
            return 1
        _remember_service(state, role, argv)
        print(f"Done. To remove it: mas service uninstall --role {role}")
        return 0
    # uninstall
    if not svc.installed(plan, runner=runner):
        print(f"The {role} service is not installed. Nothing was changed.")
        return 0
    print("This will be removed:\n" + plan.describe())
    if not ask_yes("Remove the service?", yes=a.yes):
        print("Nothing was changed.")
        return 1
    for line in svc.uninstall(plan, runner=runner):
        print("  " + line)
    _forget_service(state, role)
    print("Done. Your keys and pairing are untouched." + (f" The settings file {coordconfig.path_of(state)} stays; delete it if you want." if role == "coordinator" else ""))
    return 0


def _service_note(role: str) -> Path:
    return rel.mas_home() / f"service-{role}.json"


def _remember_service(state: Path, role: str, argv: list[str]) -> None:
    """`mas update` re-registers a service with the new release's interpreter; it needs the arguments the owner approved."""
    try:
        _service_note(role).parent.mkdir(parents=True, exist_ok=True)
        _service_note(role).write_text(json.dumps({"argv": argv}), encoding="utf-8")
    except OSError:
        pass


def _forget_service(state: Path, role: str) -> None:
    try:
        _service_note(role).unlink()
    except OSError:
        pass


# ---- mas update --------------------------------------------------------------------------------------------------------------------------------

def _run(argv, cwd):
    return svc.default_runner(list(argv))


def cmd_update(a, *, fetch: rel.Fetch = rel.http_get, run=_run, runner: svc.Runner = svc.default_runner, smoke_via_pointer: Callable[[rel.Layout], bool] | None = None) -> int:
    layout = rel.Layout(rel.mas_home())
    if not layout.installed():
        print("This copy was not installed by the installer (there is no installation in "
              f"{layout.home}), so `mas update` does not manage it. If you work from a git clone use `git pull`. Nothing was changed.", file=sys.stderr)
        return 1
    base = rel.release_base()
    try:
        m = rel.fetch_manifest(base, fetch)
    except rel.UpdateRefused as exc:
        print(f"Could not check the latest version: {exc.text} [{exc.reason}]", file=sys.stderr)
        return 1
    cur = layout.current
    if m["tag"] == cur:
        print(f"You already have the latest version ({cur}).")
        return 0
    old_services = [r for r in ("node", "coordinator") if _service_note(r).is_file()]
    chat_env, stale = _chat_env(a)
    print(f"Installed version: {cur}\nLatest version:    {m['tag']}  (SHA-256 {m['sha256'][:16]}...)")
    if m["notes"]:
        print("\nWhat it brings (text from the release author):\n" + "\n".join("  " + ln for ln in m["notes"].splitlines()))
    print(f"\nWhat changes on this computer:\n  - {m['archive']} is downloaded from {base} and its SHA-256 is checked\n  - {layout.release_dir(m['tag'])} is created with its own environment\n"
          f"  - `current` moves from {cur} to {m['tag']} (atomic switch; if anything fails it goes back to {cur})\n  - versions older than {layout.previous or cur} are deleted")
    for r in old_services:
        print(f"  - the {r} service is registered again with the new program and restarted")
    for row in stale:
        print(f"  - the entry of {row['label']} in its settings is rewritten to run the stable command (a copy of its settings file is kept): {row['detail'].split('; ')[-1]}")
    print("  - your keys, pairings and logs are not touched")
    if a.check:
        return 0
    if not ask_yes("Update now?", yes=a.yes):
        print("Nothing was changed.")
        return 1
    uv = layout.read("uv") or "uv"
    try:
        data = fetch(f"{base}/download/{m['tag']}/{m['archive']}")
        rel.verify(data, m["sha256"])
        rel.build_release(layout, m["tag"], data, m["sha256"], uv=uv, python_version=PYTHON_VERSION, run=run)
        rel.switch(layout, m["tag"], smoke=lambda: (smoke_via_pointer(layout) if smoke_via_pointer else _smoke_current(layout, run)))
    except rel.UpdateRefused as exc:
        print(f"Not updated: {exc.text} [{exc.reason}]. You remain on {layout.current}.", file=sys.stderr)
        return 1
    for r in old_services:
        _reregister(r, layout, runner)
    for name in ("install.sh", "install.ps1"):  # the uninstaller next to the program follows the release
        src = layout.release_dir(m["tag"]) / name
        if src.is_file():
            shutil.copy2(src, layout.home / name)
    for row in (_repair_chats(chat_env) if stale and chat_env is not None else []):
        print("  " + row["text"], file=sys.stderr if row["state"] != "repaired" else sys.stdout)
    gone = rel.prune(layout)
    print(f"Updated to {m['tag']}." + (f" Old versions deleted: {', '.join(gone)}." if gone else ""))
    if not old_services:
        print("If you have a `mas node run` or a coordinator running by hand, restart it to use the new version.")
    return 0


def _chat_env(a):
    """(the chat-connection environment, the chats whose entry of ours is dead or names a release folder). Nothing is read from a settings file but our entry's command."""
    try:
        from envrec_coordinator import connect as C
        env = C.Env.system(Path(a.state_dir).expanduser() if getattr(a, "state_dir", None) else envrec_os.default_state_dir())
        return env, [r for r in C.detect(env) if r["state"] == "repair"]
    except Exception:  # noqa: BLE001 - a chat check must never stop an update
        return None, []


def _repair_chats(env) -> list[dict]:
    from envrec_coordinator import connect as C
    try:
        return C.repair_entries(env)
    except Exception as exc:  # noqa: BLE001
        return [{"state": "not_repaired", "text": f"Could not repair a chat's entry ({type(exc).__name__}). Run `mas connect` for it."}]


def _smoke_current(layout: rel.Layout, run) -> bool:
    py = rel.release_python(layout, layout.current or "")
    rc, out = run([str(py), "-m", "envrec_mas", "--version"], None)
    return rc == 0 and (layout.current or "") in out


def _reregister(role: str, layout: rel.Layout, runner: svc.Runner) -> None:
    try:
        argv = json.loads(_service_note(role).read_text(encoding="utf-8"))["argv"]
        argv[0] = str(rel.release_python(layout, layout.current or ""))
        plan = svc.plan(role, argv, runner=runner)
        svc.uninstall(plan, runner=runner)
        svc.install(plan, runner=runner)
        _service_note(role).write_text(json.dumps({"argv": argv}), encoding="utf-8")
        print(f"  {role} service registered again.")
    except (svc.ServiceRefused, OSError, ValueError, KeyError) as exc:
        print(f"  Could not register the {role} service again: {exc}. Run `mas service install --role {role}`.", file=sys.stderr)


# ---- mas unpair / forget / rotate-identity -----------------------------------------------------------------------------------------------------

def cmd_unpair(a) -> int:
    """On the coordinator. If it is running it unpairs (and tells the node if it is connected); if it is not, the pin is removed from its registry directly and the node learns
    the next time it dials (it is refused, and its message says to run `mas join`)."""
    from envrec_coordinator.control import control_dir, read_json, write_json
    from envrec_coordinator.registry import Registry
    state = _state(a)
    reg = Registry(state / "coordinator", envrec_os.current())
    hits = [n for n in reg.all() if n.node_id == a.node or n.name == a.node or n.node_id.startswith(a.node)]
    if len(hits) != 1:
        print(f"No paired node matches {a.node!r}. See `mas nodes`." if not hits else "Several nodes match; use the full id (`mas nodes`).", file=sys.stderr)
        return EXIT_USAGE
    n = hits[0]
    print(f"You are about to unpair {n.name} ({n.node_id}).\n  - the coordinator deletes its pinned key: that node is no longer accepted from now on\n"
          "  - if it is connected it is told and forgets this coordinator; if not, it will find out when it tries to connect (it is refused)\n  - to join it again: `mas join` on the node and `mas pair` here")
    if not ask_yes("Unpair?", yes=a.yes):
        print("Nothing was changed.")
        return 1
    ctl = control_dir(state / "coordinator")
    rid = uuid.uuid4().hex[:10]
    write_json(envrec_os.current(), ctl / f"unpair-req-{rid}.json", {"id": rid, "node": n.node_id})
    res_file = ctl / f"unpair-res-{rid}.json"
    end = time.monotonic() + UNPAIR_WAIT_S
    while time.monotonic() < end:
        res = read_json(res_file)
        if res is not None:
            res_file.unlink(missing_ok=True)
            if res.get("status") == "unpaired":
                print(f"{n.name} unpaired." + (" It was told and has forgotten this coordinator." if res.get("notified") else " It was not connected: when it tries to connect it will be refused."))
                return 0
            print(f"Could not: {res.get('status')}", file=sys.stderr)
            return 1
        time.sleep(0.1)
    for f in ctl.glob(f"unpair-req-{rid}.json"):
        f.unlink(missing_ok=True)  # nobody picked it up: the coordinator is not running, so this process does it
    from envrec_net.log import EventLog
    reg.remove(n.node_id)
    log = EventLog(state / "coordinator" / "log")
    try:
        log.add("node_unpaired", {"node": n.node_id, "name": n.name, "notified": False, "via": "cli_offline"}, owner=True)
    finally:
        log.close()
    print(f"The coordinator is not running here; {n.name} was removed from the registry. When it tries to connect it will be refused.")
    return 0


def cmd_forget(a) -> int:
    """On a node: forget the coordinator locally (when the coordinator is gone, or the owner changed their mind). The node's own identity stays."""
    from envrec_node.state import NodeState
    st = NodeState(_state(a))
    pin = st.pin()
    if pin is None:
        print("This node is not paired with any coordinator. Nothing was changed.")
        return 0
    print(f"This node will forget the coordinator at {pin.address}. Its pinned key is deleted here; the coordinator is not told (if it is still alive, remove the node there with `mas unpair`). "
          "To join this computer again: `mas join`.")
    if not ask_yes("Forget the coordinator?", yes=a.yes):
        print("Nothing was changed.")
        return 1
    st.forget_pin()
    print("Done. This node no longer has a coordinator.")
    return 0


ROTATE_WORDS = ("Changing the coordinator's identity invalidates ALL pairings: every node has the current key pinned and will refuse a coordinator with another one. After this, every node "
                "will have to join again (`mas join` on the node and `mas pair` here).")


def cmd_rotate_identity(a) -> int:
    from envrec_coordinator.registry import Registry
    state = _state(a) / "coordinator"
    ident_dir = state / "identity"
    if not ident_dir.is_dir():
        print("This coordinator has no identity yet (it never started). There is nothing to change.")
        return 0
    reg = Registry(state, envrec_os.current())
    nodes = reg.all()
    print(ROTATE_WORDS)
    print(f"Nodes that will have to join again ({len(nodes)}): " + (", ".join(n.name for n in nodes) or "none"))
    stamp = str(int(time.time()))
    print(f"What is done: the identity folder becomes identity.rotated-{stamp} and nodes.json becomes nodes.rotated-{stamp}.json (nothing is deleted; to undo, rename them back with the coordinator stopped).\n"
          "The coordinator must be STOPPED now.")
    if not ask_yes("Is the coordinator stopped, and do you want to change its identity?", yes=a.yes):
        print("Nothing was changed.")
        return 1
    ident_dir.rename(state / f"identity.rotated-{stamp}")
    if reg.path.is_file():
        reg.path.rename(state / f"nodes.rotated-{stamp}.json")
    print("Done. The next time it starts, the coordinator creates a new identity. Join each node again.")
    return 0
