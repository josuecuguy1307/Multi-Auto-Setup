"""TOOLS (T1-T6)."""
import ast
import json
import re
from pathlib import Path

import pytest
import yaml

from envrec.contracts import Actor, Origin, Target, TargetKind, Tier
from envrec.discovery import Salts
from envrec.discovery.types import Capture, this_platform
from envrec.mission import EntryLimits, SshBlock, TargetConfig, TargetEntry, Transport
from envrec.privacy.gate import PrivacyGate
from envrec.remote.approval import ApprovalGate
from envrec.store import Store
from envrec.tools import ToolContext, get_tool, run_tool
from envrec.tools.registry import all_tools

from conftest import ROOT, TOOL_JSON

SRC = ROOT / "src" / "envrec"
# product / family names that must not appear in generic code. Derived from the names of the devices this project has fixtures or runs for.
DEVICE_TOKENS = re.compile(r"(?<![A-Za-z0-9])(zxq|krl-?3|iphones?|ipads?|airpods|echo show|alexa|jbl|jlab|redmi)(?![A-Za-z0-9])", re.I)  # not \b: `_zxq-lab` must match too
ALLOWED_TABLES = ("skills/data/",)  # generic public tables (usb.ids, OUI, Bluetooth numbers, IANA)


def source_files(base: Path, suffixes=(".py", ".json", ".yaml", ".swift")):
    return [p for p in base.rglob("*") if p.suffix in suffixes and "__pycache__" not in p.parts]


def device_name_hits(base: Path) -> list[str]:
    hits = []
    for p in source_files(base):
        rel = p.relative_to(ROOT).as_posix()
        if any(a in rel for a in ALLOWED_TABLES):
            continue
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            m = DEVICE_TOKENS.search(line)
            if m:
                hits.append(f"{rel}:{n}: {m.group(0)}")
    return hits


def test_T1_no_device_or_fixture_name_appears_in_src_outside_generic_tables():
    hits = device_name_hits(SRC)
    assert hits == [], "\n" + "\n".join(hits)


def test_T1_tools_are_registered_in_one_place_only():
    outside = []
    for p in source_files(SRC, (".py",)):
        rel = p.relative_to(SRC).as_posix()
        if rel.startswith("tools/builtin/") or rel in ("tools/registry.py",):
            continue
        if re.search(r"^\s*@tool\(", p.read_text(errors="replace"), re.M):
            outside.append(rel)
    assert outside == []


def test_T1_every_registered_tool_has_the_fixed_docstring_fields_and_a_provenance():
    tools = all_tools()
    assert len(tools) >= 25
    for t in tools:
        assert t.provenance in ("builtin", "generated") and t.tier in ("observe", "read", "write") and len(t.summary.split()) <= 12, t.name
        assert t.tier != "write" or (t.approval is not None and t.undo is not None), f"{t.name}: a write tool shows its exact action and records its undo first"
        assert t.returns and t.notes and t.platforms, t.name


def test_T2_arguments_are_validated_before_anything_runs():
    calls = []
    tool = get_tool("lookup_oui")
    ctx = ToolContext(None, this_platform(), None, None, Salts(), {}, lambda q: True)
    bad = run_tool(tool, ctx, {})
    assert not bad.ok and bad.error is not None and "mac_prefix" in bad.detail
    wrong_type = run_tool(get_tool("ble_scan_passive"), ctx, {"seconds": "soon"})
    assert not wrong_type.ok and calls == []


def test_T2_raw_output_never_enters_the_prompt(tmp_path):
    from envrec.contracts import ModelProfile
    from envrec.loop.view import build_view, expand_fact_text
    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.sensor, ring=0, handle="x"))
        PrivacyGate(st, t, Salts(), "p").store_captures([Capture("c", "src", b"RAWSECRET-9f3a open-port banner", 0, "local", "linux")])
        view = build_view(st, t, ModelProfile(name="p", kind="scripted", model_id="m", endpoint="e", context_tokens=1000, view_budget_facts=8, menu_max=6), [], "none", {})
    assert "RAWSECRET" not in view


def test_T2_evidence_has_provider_a_sha256_raw_reference_and_limitations():
    from envrec.contracts.models import Evidence, RawRef
    assert {"provider", "raw_ref", "limitations"} <= set(Evidence.model_fields) and "sha256" in RawRef.model_fields


def _provisioner(tmp_path, ask):
    from envrec.remote.provision import Provisioner
    from envrec.remote.runner import RemoteResult
    store = Store(tmp_path / "fleet")
    entry = TargetEntry(id="laptop-b", name="Laptop B", host="192.0.2.50", allowed_transports=[Transport.ssh], ssh=SshBlock(user="owner", key_env="CONF_KEY"),
                        limits=EntryLimits(max_disk_gb=5, max_model_params="1B"), owner_confirmed=True)
    cfg = TargetConfig(targets=[entry])

    class Runner:
        def __init__(self):
            self.calls = []

        def run(self, tid, transport, template, values=None, **kw):
            from envrec.remote.approval import UndoLog
            self.calls.append((template, [e["step"] for e in UndoLog(store, tid).pending()]))
            return RemoteResult(0, "", "", "ssh", tid, template, [])

    runner = Runner()
    return Provisioner(runner, ApprovalGate(store, ask=ask, show=lambda s: None), store, cfg, None), runner, store


def test_T3_a_write_records_its_undo_before_it_runs_and_asks_a_fresh_approval(tmp_path):
    answers = iter(["approve", "approve"])
    p, runner, store = _provisioner(tmp_path, lambda prompt: next(answers))
    p._write("laptop-b", Transport.ssh, "install_runtime", "mkdir_install", {"dir": "/opt/envrec/a"}, undo=("remove_tree", {"dir": "/opt/envrec/a"}))
    p._write("laptop-b", Transport.ssh, "install_runtime", "mkdir_install", {"dir": "/opt/envrec/b"}, undo=("remove_tree", {"dir": "/opt/envrec/b"}))
    assert p.gate.asked == 2  # nothing is carried over: each write asked again
    assert runner.calls[0][1] == ["install_runtime"] and len(runner.calls[1][1]) == 2  # the undo entry was already in the log when the command ran
    store.close()


def test_T3_a_denied_write_changes_nothing(tmp_path):
    p, runner, store = _provisioner(tmp_path, lambda prompt: "no")
    res = p._write("laptop-b", Transport.ssh, "install_runtime", "mkdir_install", {"dir": "/opt/envrec/a"}, undo=("remove_tree", {"dir": "/opt/envrec/a"}))
    assert res is None and runner.calls == [] and [e for e in store.events() if e.type == "undo_entry"] == []
    assert [e.payload["granted"] for e in store.events() if e.type == "approval"] == [False]
    store.close()


def test_T3_a_write_tier_tool_does_not_run_without_approval_in_the_loop(run_loop, monkeypatch):
    import asyncio
    from conftest import FakeAdapter
    from envrec.contracts import ToolSpec
    from envrec.loop import Loop, RunConfig
    from envrec.model.scripted import ScriptedModel, scripted_profile

    class Writer(FakeAdapter):
        def tool_specs(self):
            return [ToolSpec(name="push_file", ring=0, tier=Tier.write_approval, target_kinds=[TargetKind.sensor], one_line="write a file", source="fake")]

    import tempfile
    with tempfile.TemporaryDirectory() as d, Store(d) as st:
        t = st.put_target(Target(kind=TargetKind.sensor, ring=0, handle="x", authorized=False), from_config=False)
        w = Writer({"push_file": {}})
        # unauthorized target: policy denies a write outright; and an authorized one still needs the owner's yes (asserted on the policy function)
        loop = Loop(st, [w], ScriptedModel([TOOL_JSON % "push_file"], scripted_profile()), config=RunConfig(max_steps=1), approve=lambda spec, tgt: True)
        asyncio.run(loop.run(t))
        assert w.executed == []
        from envrec.policy import decide
        auth = t.model_copy(update={"authorized": True})
        assert decide(w.tool_specs()[0], auth).decision.value == "needs_approval"


def test_T3_every_tool_that_sends_or_opens_something_shows_its_exact_action_before_it_runs():
    missing = [t.name for t in all_tools() if "active" in t.tags and t.approval is None]
    assert missing == [], f"active tools with no approval text: {missing}"


def test_T4_no_tool_builds_a_fact_itself_so_every_tool_result_reaches_the_store_through_the_gate():
    offenders = []
    for p in source_files(SRC / "tools", (".py",)):
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if re.search(r"(?<![A-Za-z_])Fact\(", line) and "FactOut(" not in line:
                offenders.append(f"{p.relative_to(ROOT)}:{n}")
    assert offenders == [], offenders


def test_T4_a_skill_run_builds_its_facts_with_the_gate_before_anything_is_written():
    text = (SRC / "skills" / "runner.py").read_text()
    assert "PrivacyGate(" in text and re.search(r"gate\.build\(outs\)", text)
    # every write_fact in the file takes what the gate built
    for n, line in enumerate(text.splitlines(), 1):
        if "store.write_fact(" in line:
            assert "for f in built" in text.splitlines()[n - 2], f"skills/runner.py:{n} writes something the gate did not build"


MISSION_TOOLS = yaml.safe_load((Path(__file__).parent / "mission_tools.yaml").read_text())["capabilities"]


@pytest.mark.parametrize("cap", MISSION_TOOLS, ids=lambda c: f"{c['mission']}:{c['capability']}")
def test_T5_the_mission_tool_exists_and_is_in_the_menu_in_its_protocol_state(cap, tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "conformance" / "tools"))
    import menu_truth
    state_menu = menu_truth.menus(cap["kind"], cap.get("ring", 0), True)  # the menu of an enrolled target
    offered = {n for v in state_menu.values() for n in (v[0] if isinstance(v, tuple) else v)}
    assert any(t in offered for t in cap["tools"]), f"none of {cap['tools']} is offered to a {cap['kind']} target in any state; offered: {sorted(offered)[:8]}"


# ---- T6: tools take references (fact ids, capture ids), never raw content ---------------------------------------------------------------------------
# The model never sees raw output (C1). An argument that can only be filled with raw output (a banner, a capture's text, a command's output) is an argument nobody can fill.
RAW_NAME = re.compile(r"^(text|raw|raw_text|output|stdout|stderr|banner|payload|content|contents|body|data|blob|reply|frame|buffer|capture_text|response)$", re.I)
RAW_DOC = re.compile(r"\b(what the device said|raw (output|text|bytes|data)|output of|text to (search|parse)|json text|lines of text|untrusted string|file contents?|banner)\b", re.I)


def _model_tools():
    return [t for t in all_tools() if t.audience == "model"]


def test_T6_no_model_facing_tool_has_an_argument_that_is_raw_output():
    offenders = []
    for t in _model_tools():
        for a in t.args:
            if a.ref:
                continue  # a reference IS the answer: its doc may talk about what the capture holds
            if RAW_NAME.match(a.name) or RAW_DOC.search(a.doc):
                offenders.append(f"{t.name}({a.name}): {a.doc[:60]}")
    assert offenders == [], "arguments only raw content can fill; take a CaptureRef or FactRef instead:\n" + "\n".join(offenders)


def test_T6_every_reference_is_typed_and_a_value_argument_is_something_the_model_can_write():
    for t in _model_tools():
        for a in t.args:
            assert a.ref in (None, "capture", "fact")
            if a.ref is None:
                assert a.type_name.split("[")[0] in ("str", "int", "float", "bool", "list", "dict"), (t.name, a.name, a.type_name)


def test_T6_a_helper_that_takes_values_from_a_sandboxed_script_is_never_on_a_menu():
    from envrec.tools.bridge import RegistryAdapter
    from envrec.contracts import TargetKind
    skill_side = {t.name for t in all_tools() if t.audience != "model"}
    assert skill_side, "the table/extract helpers are the skill side; none registered means the audit moved"
    assert not skill_side & {s.name for s in RegistryAdapter(platform="linux").tool_specs()}
    import sys
    sys.path.insert(0, str(ROOT / "conformance" / "tools"))
    import menu_truth
    for kind in (k.value for k in TargetKind):
        for ring in (0, 1, 2, 3):
            offered = {n for v in menu_truth.menus(kind, ring, False).values() for n in (v[0] if isinstance(v, tuple) else v)}
            assert not offered & skill_side, (kind, ring, sorted(offered & skill_side))


def test_T6_every_argument_of_every_tool_on_the_menu_is_satisfiable_from_what_the_view_shows(tmp_path):
    """The view names each argument (and says where its value comes from); for a reference it lists the ids that exist; the content is not there."""
    from envrec.contracts import ModelProfile
    from envrec.loop.menu import MenuEntry, render_menu
    from envrec.loop.view import build_view
    from envrec.tools.bridge import RegistryAdapter
    SECRET = "RAWSECRET-c0ffee open-port banner"
    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.serial, ring=0, handle="x"))
        gate = PrivacyGate(st, t, Salts(), "serial_identify")
        gate.store_captures([Capture("heard", "port", SECRET.encode(), 0, "local", "linux")])
        sha = gate.refs["heard"].sha256
        reg = RegistryAdapter(platform=this_platform())
        specs = [s for s in reg.tool_specs() if any(p.get("x-ref") for p in (s.input_model.get("properties") or {}).values())]
        assert specs, "no tool takes a reference: the check has nothing to look at"
        for spec in specs:
            menu = [MenuEntry(spec.name, spec.one_line, spec, reg)]
            view = build_view(st, t, ModelProfile(name="p", kind="scripted", model_id="m", endpoint="e", context_tokens=4000, view_budget_facts=8, menu_max=6), menu, "none", {})
            for name, prop in spec.input_model["properties"].items():
                assert name in view and prop["description"][:40] in render_menu(menu), (spec.name, name)
                if prop.get("x-ref") == "capture":
                    assert sha[:8] in view, f"{spec.name} takes a capture id and the view does not list it"
            assert "RAWSECRET" not in view and SECRET not in view
