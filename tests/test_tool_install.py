"""P1: install a command-line tool from the assets of a GitHub release. The plan is built by code from the catalog entry (pins taken from REAL release captures) and the machine's facts; the node's new
`verify_program` step and the finder of self-installed tools; and the services flow: ONE chat question for the whole plan, steps sent only after the owner's answer, the install recorded per computer, the router
seeing the tool as installed, the undo. SIMULATED-LOCAL: the origin of the files is a loopback server and the program is a small script; the REAL download, SHA-256 check and safe unpacking of the three shipped
entries' macOS arm64 assets were done on 2026-11-02 (see docs/KNOWN_GAPS.md and the PROGRESS entry)."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import os
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from test_executors import Origin
from acceptance_world import build
from test_acceptance_run import sc, text
from test_owner_profile import CH

from envrec_coordinator import tool_plan as TP
from envrec_node import executors as X
from envrec_node.envrun import finder
from envrec_node.setup import WriteRefused, WriteScope
from envrec_net import envrun as E
from envrec_os import procs

CAPS = Path(__file__).parent / "fixtures" / "github_releases"
PERMISSIVE = set(E.load_policy()["permissive_licenses"])
ENVS = E.load_environments()
SHIPPED = {"opencode": "opencode", "codex": "codex", "goose": "goose"}
CELLS = [("macos", "arm64"), ("macos", "amd64"), ("linux", "arm64"), ("linux", "amd64"), ("windows", "amd64")]
POSIX = os.name != "nt"


def run(coro):
    return asyncio.run(coro)


def capture(name):
    d = json.loads((CAPS / f"{name}.json").read_text())
    return {a["name"]: a for a in d["assets"]}


# ---- the plan, from pins that match the real release captures ----------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("env", sorted(SHIPPED))
def test_every_pin_of_a_shipped_entry_is_what_its_release_capture_reports(env):
    assets, caps = ENVS[env]["upstream"]["assets"], capture(env)
    assert assets and TP.install_state(ENVS[env], PERMISSIVE) == ("installable", "")
    for key, a in assets.items():
        c = caps[a["name"]]
        assert (a["sha256"], a["bytes"], a["url"]) == (c["digest"].split(":", 1)[1], c["size"], c["browser_download_url"]), key


@pytest.mark.parametrize("env", sorted(SHIPPED))
@pytest.mark.parametrize("cell", CELLS)
def test_a_plan_is_built_for_every_cell_the_entry_has_and_says_everything_the_owner_approves(env, cell):
    n = {"os": cell[0], "arch": cell[1], "disk": 500}
    try:
        plan = TP.build_tool_plan(ENVS[env], n, TP.load_tool_recipes(), PERMISSIVE, machine_id="m")
    except TP.PlanRefused as exc:
        assert exc.reason == "unsupported_cell"  # only a cell the release has no build for
        return
    fetch, unpack, verify = plan.steps
    inst = ENVS[env]["install"]
    key = next(k for k in ENVS[env]["upstream"]["assets"] if ENVS[env]["upstream"]["assets"][k]["url"] == fetch.args["url"])
    assert verify.args["binary_path"].endswith("/" + (inst["binary"].get(key) or inst["binary"][cell[0]])) and fetch.bytes == ENVS[env]["upstream"]["assets"][key]["bytes"]
    assert fetch.args["dest"] in fetch.writes and unpack.args["dest"] in unpack.writes and all(w.startswith("<mas>/") for s in plan.steps for w in s.writes)
    assert all(s.exact_command and s.undo for s in plan.steps) and plan.plan_hash == TP.P.plan_hash(plan)


def test_a_different_asset_is_a_different_plan_and_a_changed_pin_changes_the_hash():
    a = TP.build_tool_plan(ENVS["opencode"], {"os": "macos", "arch": "arm64", "disk": 500}, TP.load_tool_recipes(), PERMISSIVE, machine_id="m")
    b = TP.build_tool_plan(ENVS["opencode"], {"os": "linux", "arch": "amd64", "disk": 500}, TP.load_tool_recipes(), PERMISSIVE, machine_id="m")
    c = copy.deepcopy(ENVS["opencode"])
    c["upstream"]["assets"]["darwin-arm64"]["sha256"] = "0" * 64
    d = TP.build_tool_plan(c, {"os": "macos", "arch": "arm64", "disk": 500}, TP.load_tool_recipes(), PERMISSIVE, machine_id="m")
    assert len({a.plan_hash, b.plan_hash, d.plan_hash}) == 3


def refusal(entry, n=None):
    with pytest.raises(TP.PlanRefused) as e:
        TP.build_tool_plan(entry, n or {"os": "macos", "arch": "arm64", "disk": 500}, TP.load_tool_recipes(), PERMISSIVE, machine_id="m")
    return e.value


def test_what_code_cannot_verify_is_not_installed_and_the_refusal_is_typed():
    assert refusal(ENVS["claude-code"]).reason == "no_installer"  # no install data at all
    bad = copy.deepcopy(ENVS["opencode"])
    del bad["upstream"]["assets"]["darwin-arm64"]["sha256"]
    assert refusal(bad).reason == "unverified_entry"
    lic = copy.deepcopy(ENVS["opencode"])
    lic["license"]["spdx"] = "GPL-3.0"
    assert refusal(lic).reason == "unverified_entry" and "not permissive" in refusal(lic).text
    nopin = copy.deepcopy(ENVS["opencode"])
    nopin["upstream"].pop("commit")
    nopin.pop("pin", None)
    assert refusal(nopin).reason == "unverified_entry" and "pinned commit" in refusal(nopin).text
    host = copy.deepcopy(ENVS["opencode"])
    host["upstream"]["assets"]["darwin-arm64"]["url"] = "https://example.org/x.zip"
    assert refusal(host).reason == "unverified_entry"
    assert refusal(ENVS["opencode"], {"os": "macos", "arch": "arm64", "disk": 0.05}).reason == "no_disk"
    assert refusal(ENVS["opencode"], {"os": "macos", "arch": "riscv", "disk": 500}).reason == "unsupported_cell"
    assert TP.check_tool_pins(ENVS, PERMISSIVE) == []  # every shipped entry that carries install data verifies


# ---- the node: verify_program, the record, the finder -------------------------------------------------------------------------------------------------------------------------------------

SCRIPT = f"#!{sys.executable}\nimport sys\nprint('faketool 1.2.3' if sys.argv[1:] == ['--version'] else 'bad')\nsys.exit(0 if sys.argv[1:] == ['--version'] else 2)\n"


def tool_archive(fmt: str, name="faketool") -> bytes:
    buf = io.BytesIO()
    if fmt == "zip":
        with zipfile.ZipFile(buf, "w") as zf:
            zi = zipfile.ZipInfo(name)
            zi.external_attr = (0o755 | stat.S_IFREG) << 16
            zf.writestr(zi, SCRIPT)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode = len(SCRIPT.encode()), 0o755
            tf.addfile(ti, io.BytesIO(SCRIPT.encode()))
    return buf.getvalue()


REAL_RECIPE = TP.load_tool_recipes()["github-release-tool"]


def tool_recipe():
    r = copy.deepcopy(REAL_RECIPE)
    r["download"] = {"schemes": ["http"], "hosts": {"127.0.0.1": ["127.0.0.1"]}, "max_redirects": 3, "disk_margin": 1.0}  # a test origin on loopback
    return r


def fake_entry(origin, body: bytes, fmt: str, **over):
    e = copy.deepcopy(ENVS["opencode"])
    e.update({"id": "faketool", "title": "Fake tool"})
    e["adapter"]["exe"] = {"darwin": "faketool", "linux": "faketool", "windows": "faketool"}
    name = "faketool." + ("zip" if fmt == "zip" else "tar.gz")
    sha = hashlib.sha256(body).hexdigest()
    e["upstream"] = {**e["upstream"], "assets": {k: {"name": name, "sha256": sha, "bytes": len(body), "url": origin.url("/" + name)} for k in e["upstream"]["assets"]}}
    e["install"]["binary"] = {k: "faketool" for k in e["install"]["binary"]} | {k: "faketool" for k in e["upstream"]["assets"]}
    e["install"]["verify"] = {"argv": ["--version"], "timeout_s": 20, "expect_exit": 0, "expect": "faketool 1.2.3"}
    e.update(over)
    return e, name


@pytest.fixture
def node(tmp_path):
    home = tmp_path / "mas"
    home.mkdir()
    origins = []
    yield type("N", (), {"home": home, "recipe": staticmethod(tool_recipe), "origins": origins})
    for o in origins:
        o.close()


def exec_plan(node, entry, plan, *, ctx=None):
    """Run the plan's steps with the node's own functions, each under the scope of its declared paths; returns the results."""
    recipe = tool_recipe()
    c = {"recipe": recipe, "mas_home": node.home, **(ctx or {})}
    out = []
    for s in plan.steps:
        out.append(getattr(X, s.op)(dict(s.args), WriteScope(node.home, list(s.writes)), c)[0])
    return out


@pytest.mark.skipif(not POSIX, reason="the fake program is a POSIX script")
@pytest.mark.parametrize("fmt", ["tar.gz", "zip"])
def test_a_tool_is_downloaded_checked_unpacked_run_once_recorded_found_and_undone(node, fmt):
    body = tool_archive(fmt)
    o = Origin({f"/faketool.{'zip' if fmt == 'zip' else 'tar.gz'}": body})
    node.origins.append(o)
    entry, _ = fake_entry(o, body, fmt)
    plan = TP.build_tool_plan(entry, {"os": "linux", "arch": "amd64", "disk": 500}, {"github-release-tool": tool_recipe()}, PERMISSIVE, machine_id="m")
    got = exec_plan(node, entry, plan)
    assert got[0]["sha256"] == hashlib.sha256(body).hexdigest() and got[2]["ok"] is True and "faketool 1.2.3" in got[2]["output"]
    find = finder({"mas_home": node.home, "find_exe": lambda n: None})
    path = find("faketool")
    assert path and Path(path).is_file() and str(node.home / "tools") in path and find("another") is None  # found by its name, from the record
    undo = {s.n: s for s in plan.steps}
    for n in (3, 2, 1):
        getattr(X, "undo_" + undo[n].op)(dict(undo[n].args), WriteScope(node.home, list(undo[n].writes)), {"recipe": tool_recipe(), "mas_home": node.home})
    assert find("faketool") is None and not (node.home / "tools").exists() and not (node.home / "downloads").exists()


@pytest.mark.skipif(not POSIX, reason="the fake program is a POSIX script")
def test_the_node_refuses_a_wrong_hash_flags_it_does_not_know_a_program_that_fails_and_a_path_outside(node):
    body = tool_archive("tar.gz")
    o = Origin({"/faketool.tar.gz": body})
    node.origins.append(o)
    entry, _ = fake_entry(o, body, "tar.gz")
    good = TP.build_tool_plan(entry, {"os": "linux", "arch": "amd64", "disk": 500}, {"github-release-tool": tool_recipe()}, PERMISSIVE, machine_id="m")
    bad = copy.deepcopy(entry)
    for a in bad["upstream"]["assets"].values():
        a["sha256"] = "1" * 64
    plan = TP.build_tool_plan(bad, {"os": "linux", "arch": "amd64", "disk": 500}, {"github-release-tool": tool_recipe()}, PERMISSIVE, machine_id="m")
    with pytest.raises(X.StepFailed) as e:
        X.fetch_check(dict(plan.steps[0].args), WriteScope(node.home, list(plan.steps[0].writes)), {"recipe": tool_recipe(), "mas_home": node.home})
    assert e.value.code == "checksum_mismatch" and not list((node.home / "downloads").glob("*"))  # nothing wrong stays
    exec_plan(node, entry, good)
    v = good.steps[2]
    scope = WriteScope(node.home, list(v.writes))
    ctx = {"recipe": tool_recipe(), "mas_home": node.home}
    for argv, code in ((["--version", "; rm -rf /"], "bad_arguments"), (["--help"], "program_failed"), ([], "bad_arguments")):
        with pytest.raises(X.StepFailed) as e2:
            X.verify_program({**v.args, "argv": argv}, scope, ctx)
        assert e2.value.code == code
    with pytest.raises(X.StepFailed) as e3:
        X.verify_program({**v.args, "expect": "something it never prints"}, scope, ctx)
    assert e3.value.code == "program_unexpected"
    with pytest.raises(WriteRefused):
        X.verify_program({**v.args, "binary_path": "<mas>/downloads/x"}, scope, ctx)  # outside the declared folder


# ---- the services flow in the acceptance world --------------------------------------------------------------------------------------------------------------------------------------------

def harness_analysis(c, nid):
    """What a finished harness analysis run stores (runs store only, never the registry store): the record mas_prepare reads."""
    import time as _t
    facts = {"machine.os.family": "linux", "machine.os.name": "Linux", "machine.os.arch": "x86_64", "machine.disk_free_gib": 200, "machine.ram_total_gib": 16,
             "machine.ram_available_gib": 10, "machine.vram_gib": 0, "machine.gpus": [], "machine.model_path": "cpu", "machine.runtimes": [],
             "machine.ai_clis": [], "machine.api_key_variable_names": [], "analysis.finished": True}
    c.runs.analyses.mkdir(parents=True, exist_ok=True)
    (c.runs.analyses / f"{nid}.json").write_text(json.dumps({"run": "seeded", "node": nid, "stored_at": _t.time(), "facts": {k: {"value": v, "status": "tested"} for k, v in facts.items()}}))


class StubOrigin:
    """Only the address a plan is built from (no server is started: reaching the plan never downloads)."""
    @staticmethod
    def url(p):
        return "http://127.0.0.1:9" + p


def empty_registry(c, nid):
    (c.registry.dir / f"analysis-{nid}.json").unlink(missing_ok=True)  # build() seeds a registry analysis for every world (acceptance_world.py): remove the file in this test's own tmp dir, so that store is genuinely empty (the reader itself is not patched)
    assert c.registry.load_analysis(nid) is None


@pytest.mark.skipif(not POSIX, reason="the fake program is a POSIX script")
def test_install_tool_reads_the_analysis_a_harness_run_stored(tmp_path, monkeypatch):
    """Regression (real failure on the Lenovo): the owner analysed with mas_start, mas_prepare used that analysis, yet mas_install_tool refused 'no stored analysis' for every tool."""
    async def go():
        body = tool_archive("tar.gz")
        entry, _ = fake_entry(StubOrigin, body, "tar.gz")
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            harness_analysis(c, nid)  # only the runs store holds an analysis; the registry store is emptied below
            empty_registry(c, nid)
            monkeypatch.setattr(TP, "load_tool_recipes", lambda path=None: {"github-release-tool": tool_recipe()})
            c.services.catalog["faketool"] = entry
            r = c.services.install_tool(CH, environment="faketool")
            assert r["ok"] and "Install Fake tool" in r["text"], r["text"]
        finally:
            await teardown()
    run(go())


def test_install_tool_refuses_when_no_analysis_exists_anywhere(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            empty_registry(c, nid)  # and no harness analysis was ever stored for this machine
            r = c.services.install_tool(CH, environment="opencode")
            assert not r["ok"] and "no stored analysis" in r["text"], r["text"]
        finally:
            await teardown()
    run(go())


def test_install_tool_says_the_true_reason_for_an_incomplete_stored_analysis(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            empty_registry(c, nid)
            c.runs.analyses.mkdir(parents=True, exist_ok=True)  # stored under an older, narrower spec: most facts the current one needs are missing
            (c.runs.analyses / f"{nid}.json").write_text(json.dumps({"run": "old", "node": nid, "stored_at": 1.0, "facts": {"machine.os.family": {"value": "linux", "status": "tested"}}}))
            r = c.services.install_tool(CH, environment="opencode")
            assert not r["ok"] and "no longer serves" in r["text"] and "no stored analysis" not in r["text"], r["text"]
        finally:
            await teardown()
    run(go())


def test_install_tool_does_not_borrow_another_machines_analysis(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            empty_registry(c, nid)
            harness_analysis(c, "another-machine")  # a complete analysis, but of a different computer
            r = c.services.install_tool(CH, environment="opencode")
            assert not r["ok"] and "no stored analysis" in r["text"], r["text"]
        finally:
            await teardown()
    run(go())


@pytest.mark.parametrize("env", ["opencode", "codex", "goose"])
def test_every_shipped_tool_reaches_the_plan_through_the_same_lookup(tmp_path, env):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            empty_registry(c, nid)
            harness_analysis(c, nid)
            r = c.services.install_tool(CH, environment=env)
            assert r["ok"] and "step(s)" in r["text"], (env, r["text"])
        finally:
            await teardown()
    run(go())


@pytest.mark.skipif(not POSIX, reason="the fake program is a POSIX script")
def test_one_chat_question_then_the_steps_then_the_router_sees_the_tool_and_the_node_finds_it_and_it_can_be_undone(tmp_path, monkeypatch):
    async def go():
        body = tool_archive("tar.gz")
        o = Origin({"/faketool.tar.gz": body})
        entry, _ = fake_entry(o, body, "tar.gz")
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            c.registry.save_analysis(nid, {"facts": {"machine.os.family": {"value": "linux", "status": "tested"}, "machine.os.arch": {"value": "x86_64", "status": "tested"}, "machine.disk_free_gib": {"value": 200, "status": "tested"},
                                                     "machine.ai_clis": {"value": [], "status": "tested"}, "machine.api_key_variable_names": {"value": [], "status": "tested"}}}, 1.0)
            monkeypatch.setattr(TP, "load_tool_recipes", lambda path=None: {"github-release-tool": tool_recipe()})  # the test origin is on loopback
            c.services.catalog["faketool"] = entry
            ctx = w.agent.runner.ctx
            ctx["recipes"] = {**ctx["recipes"], "github-release-tool": tool_recipe()}
            ctx.pop("find_exe", None)
            ctx["run"] = procs.run_capture  # the world plays the models; the verification really runs the little program
            r = c.services.install_tool(CH, environment="faketool")
            assert r["ok"] and "Install Fake tool" in r["text"] and "1." in r["text"] and "3." in r["text"] and "MB" in r["text"]
            assert not [t for t in c.tasks.list() if t.task_type == "setup_step"]  # nothing is sent before his answer
            assert c.harness.decide(CH, r["intent_id"], "approve")["ok"]
            job = c.services.jobs[r["job_id"]]
            for _ in range(300):
                if job.state in ("done", "failed"):
                    break
                await asyncio.sleep(0.1)
            assert job.state == "done", job.text
            assert "faketool" in c.runs.stored_tools(nid) and "faketool" in next(m for m in c.services.facts.machines() if m.machine_id == nid).installed
            steps = [t for t in c.tasks.list() if t.task_type == "setup_step"]
            assert len(steps) == 3 and all(t.status == "done" for t in steps)
            assert len([e for e in c.log.events("setup_step_approved") if e.effective_tag == "OWNER"]) == 3
            where = lambda: finder({"mas_home": ctx["mas_home"], "find_exe": lambda n: None})("faketool")  # noqa: E731
            assert where() and "/tools/" in where()
            c.services.envs["faketool"] = entry  # the catalog the router reads (this world builds it once)
            from envrec_coordinator.router import EntryChat, Task
            from envrec_coordinator.router.catalog import build_candidates
            probe = Task(task_id="p", task_type="text", sensitivity="public", entry_chat=EntryChat(kind="local", name="x"), created_by="OWNER", payload_ref="p")
            cands, left = build_candidates(probe, c.services.envs, c.services.facts, c.services.policy, {})
            assert any(x.route.environment_id == "faketool" and x.route.machine_id == nid for x in cands) and "faketool@" + nid not in left  # an install somebody can route to
            adm = c.services.run_admission(CH, environment="faketool", task_type="text", access=entry["accesses"][0]["id"])
            assert adm["ok"] and "faketool" in adm["combo"]  # and the admission exam can be proposed for it
            r2 = c.services.install_tool(CH, environment="faketool", action="undo")
            assert r2["ok"] and "Remove Fake tool" in r2["text"]
            c.harness.decide(CH, r2["intent_id"], "approve")
            job2 = c.services.jobs[r2["job_id"]]
            for _ in range(300):
                if job2.state in ("done", "failed"):
                    break
                await asyncio.sleep(0.1)
            assert job2.state == "done" and c.runs.stored_tools(nid) == {} and where() is None
            declined = c.services.install_tool(CH, environment="faketool")
            c.harness.decide(CH, declined["intent_id"], "reject")
            await asyncio.sleep(0.2)
            assert c.services.jobs[declined["job_id"]].state == "declined" and not c.runs.stored_tools(nid)
        finally:
            await teardown()
            o.close()
    run(go())
