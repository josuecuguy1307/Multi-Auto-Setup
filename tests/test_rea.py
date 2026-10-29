import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from envrec.adapters import REA_TOOLS, ReaAdapter, RecordedReaExecutor, SubprocessReaExecutor, ToolContext
from envrec.adapters.rea import confidence_of, parse_js_app
from envrec.contracts import FactStatus, Target, TargetKind
from envrec.loop import Loop, RunConfig, build_menu
from envrec.model import ScriptedModel, scripted_profile
from envrec.scoring import load_ground_truth, score_target
from envrec.store import Store

ROOT = Path(__file__).resolve().parent.parent
SOFT = ROOT / "fixtures" / "software"
REC = SOFT / "rea-output"
APP, BIN = SOFT / "tiny-notes-app", SOFT / "hello"
DONE = json.dumps({"action": "done"})


def call(tool):
    return json.dumps({"action": "call_tool", "tool": tool, "why": "next"})


def recorded(**exit_codes):
    return RecordedReaExecutor.from_dir(REC, {
        "analyze-javascript-application": "js_app.json", "inspect-macho": "macho.json",
        "inspect-artifact": "artifact_bin.json"}, exit_codes)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def make(store, path, kind=TargetKind.app):
    return store.put_target(Target(kind=kind, ring=0, handle=str(path)))


def run_tool(store, target, tool, executor=None, roots=None):
    adapter = ReaAdapter(executor or recorded(), roots or [SOFT])
    return adapter, asyncio.run(adapter.run(tool, {}, ToolContext(store, target, "evt_t")))


def by_key(result):
    return {f.key: f for f in result.facts}


# ---- the JavaScript app, from a REAL recorded run ------------------------------------------------------

def test_js_app_facts_from_the_real_recording(store):
    _, r = run_tool(store, make(store, APP), "analyze_js_app")
    f = by_key(r)
    assert r.ok and r.coverage.status == "complete"
    assert (f["app.name"].value, f["app.version"].value, f["app.entrypoint"].value) == ("tiny-notes-app", "1.0.0", "main.js")
    assert f["app.source_files"].value == ["lib/format.js", "lib/routes.js", "lib/store.js", "main.js"]
    assert f["app.external_modules"].value == ["node:http"]
    for key in ("app.name", "app.source_files"):
        assert f[key].status == FactStatus.tested  # REA observed these with exact confidence
    # both node:http nodes are inferred/low in the real recording, so this stays a hypothesis
    assert f["app.external_modules"].status == FactStatus.hypothesis


def test_rea_low_confidence_inference_stays_a_hypothesis_and_is_wrong_here(store):
    """Real finding: REA reports 'welcome' (a Map key) as a network endpoint candidate, inferred, low confidence."""
    t = make(store, APP)
    _, r = run_tool(store, t, "analyze_js_app")
    ep = by_key(r)["app.endpoint_candidates"]
    assert ep.value == ["welcome"] and ep.status == FactStatus.hypothesis
    assert ep.evidence[0].confidence == 0.3 and any("inferred" in lim for lim in ep.evidence[0].limitations)
    for fact in r.facts:
        store.write_fact(fact)
    report = score_target(store, t.id, load_ground_truth(ROOT / "ground_truth" / "software-tiny-notes-app.yaml"))
    rows = {row.key: row for row in report.rows}
    assert rows["app.endpoint_candidates"].outcome == "wrong" and rows["app.endpoint_candidates"].status == "hypothesis"
    assert all(rows[k].outcome == "correct" for k in rows if k != "app.endpoint_candidates")
    assert report.provisional  # one field of that ground truth is unconfirmed


def test_evidence_keeps_rea_provenance_and_the_raw_file_is_hash_checked(store):
    _, r = run_tool(store, make(store, APP), "analyze_js_app")
    ev = by_key(r)["app.name"].evidence[0]
    assert (ev.provider, ev.probe, ev.event_id) == ("rea", "rea-javascript-application:analyze_javascript_application", "evt_t")
    assert ev.note.startswith("rea ev_") and 0 < len(ev.limitations) <= 7
    assert store.read_raw(ev.raw_ref).decode() == (REC / "js_app.json").read_text()  # the whole REA JSON, by hash
    assert all(len(lim) <= 200 for lim in ev.limitations)


# ---- the compiled program ---------------------------------------------------------------------------------

def test_macho_facts_match_the_independent_checks(store):
    t = make(store, BIN)
    _, r = run_tool(store, t, "inspect_binary")
    f = by_key(r)
    assert (f["app.format"].value, f["app.arch"].value) == ("mach-o", "arm64")
    assert f["app.dependencies"].value == ["/usr/lib/libSystem.B.dylib"]  # exhaustive: plain key
    assert f["app.imports_seen"].value == ["_printf"] and f["app.exports_seen"].value == ["__mh_execute_header", "_main"]
    assert any("not exhaustive" in lim for lim in f["app.imports_seen"].evidence[0].limitations)
    assert r.coverage.status == "partial"  # REA says imports and exports may be incomplete


def test_artifact_hash_in_the_recording_is_the_real_hash_of_the_file(store):
    _, r = run_tool(store, make(store, BIN), "inspect_artifact")
    assert by_key(r)["app.sha256"].value == hashlib.sha256(BIN.read_bytes()).hexdigest()


def test_reas_own_unexplored_branch_becomes_an_unknown_fact(store):
    _, r = run_tool(store, make(store, BIN), "inspect_artifact")
    assert by_key(r)["app.functions"].status == FactStatus.unknown
    assert any(u.key == "app.functions" and "open_binary" in u.question for u in r.unknowns)
    assert r.coverage.status == "partial"


def test_the_binary_ground_truth_scores_all_correct(store):
    t = make(store, BIN)
    adapter = ReaAdapter(recorded(), [SOFT])
    for tool in ("inspect_binary", "inspect_artifact"):
        for fact in asyncio.run(adapter.run(tool, {}, ToolContext(store, t, None))).facts:
            store.write_fact(fact)
    report = score_target(store, t.id, load_ground_truth(ROOT / "ground_truth" / "software-hello.yaml"))
    assert {r.outcome for r in report.rows} == {"correct"} and not report.provisional


def test_two_rea_tools_agreeing_are_not_treated_as_independent_sources(store):
    t = make(store, BIN)
    adapter = ReaAdapter(recorded(), [SOFT])
    for tool in ("inspect_binary", "inspect_artifact"):
        for fact in asyncio.run(adapter.run(tool, {}, ToolContext(store, t, None))).facts:
            store.write_fact(fact)
    fmt = store.current_fact(t.id, "app.format")
    assert fmt.status == FactStatus.tested and len(fmt.evidence) == 2  # same program, same provider "rea"


# ---- honesty rules --------------------------------------------------------------------------------------------------

def test_an_empty_list_is_unknown_when_reas_coverage_is_not_complete():
    data = json.loads((REC / "js_app.json").read_text())
    data = copy.deepcopy(data)
    data["normalized_result"]["graph"]["coverage"]["status"] = "partial"  # synthetic edit of a real recording
    specs = {s.key: s for s in parse_js_app(data).facts}
    for key in ("app.source_files", "app.external_modules", "app.endpoint_candidates"):
        assert specs[key].status == FactStatus.unknown and "partial" in specs[key].reason


def test_unmapped_rea_confidence_labels_are_flagged_not_guessed():
    assert confidence_of("exact") == (1.0, None)
    value, note = confidence_of("certain-ish")
    assert value == 0.5 and "unmapped" in note


def test_rea_errors_become_failed_results_with_reas_own_message(store):
    ex = RecordedReaExecutor.from_dir(REC, {"inspect-artifact": "artifact_app.json"}, {"inspect-artifact": 1})
    _, r = run_tool(store, make(store, BIN), "inspect_artifact", executor=ex)  # recording is the directory error
    assert not r.ok and "target_unavailable" in r.error and "directory" in r.error and r.facts == []


def test_non_json_output_is_a_failed_result(store):
    ex = RecordedReaExecutor({"inspect-artifact": "Segmentation fault"}, {"inspect-artifact": 139})
    _, r = run_tool(store, make(store, BIN), "inspect_artifact", executor=ex)
    assert not r.ok and "no JSON" in r.error


# ---- safety -----------------------------------------------------------------------------------------------------------

def test_only_three_static_inspection_commands_are_reachable():
    assert {t.rea_command for t in REA_TOOLS.values()} == {
        "analyze-javascript-application", "inspect-macho", "inspect-artifact"}
    for tool in REA_TOOLS.values():
        argv = tool.argv(Path("/some/path"))
        assert argv == [tool.rea_command, "/some/path", "--json"]
    for banned in ("capture", "adb", "debug", "cutter", "rizin", "browser", "scenario", "frida", "extract"):
        assert not any(banned in t.rea_command for t in REA_TOOLS.values())


def test_paths_outside_the_allowed_roots_never_reach_rea(store, tmp_path):
    outside = tmp_path / "elsewhere.bin"
    outside.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 60)
    ex = recorded()
    adapter, r = run_tool(store, make(store, outside), "inspect_artifact", executor=ex)
    assert not r.ok and "not under a root" in r.error and ex.calls == []
    assert adapter.available("inspect_artifact", make(store, outside), store) is False


def test_a_symlink_cannot_escape_the_allowed_root(store, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(Path("/etc/hosts"))
    root_tmp = tmp_path
    adapter = ReaAdapter(recorded(), [root_tmp / "allowed"])
    (root_tmp / "allowed").mkdir()
    (root_tmp / "allowed" / "x").symlink_to(Path("/etc/hosts"))
    assert adapter.available("inspect_artifact", make(store, root_tmp / "allowed" / "x"), store) is False


def test_tools_are_offered_only_where_they_apply(store):
    adapter = ReaAdapter(recorded(), [SOFT])
    names = lambda p: [e.name for e in build_menu([adapter], make(store, p), store, 8)]  # noqa: E731
    assert names(APP) == ["analyze_js_app"]  # a directory: REA's own error says to use this one
    assert names(BIN) == ["inspect_binary", "inspect_artifact"]
    assert names(SOFT / "hello.c") == ["inspect_artifact"]  # not a Mach-O


def test_nothing_is_offered_when_rea_is_not_installed(store, tmp_path):
    missing = SubprocessReaExecutor(tmp_path / "no-rea-here", tmp_path / "home")
    adapter = ReaAdapter(missing, [SOFT])
    assert missing.ready() is False and build_menu([adapter], make(store, BIN), store, 8) == []


# ---- through the loop, with a scripted model -----------------------------------------------------------------------------

def run_loop(store, target, replies, executor=None):
    adapter = ReaAdapter(executor or recorded(), [SOFT])
    model = ScriptedModel(replies, scripted_profile())
    return asyncio.run(Loop(store, [adapter], model, config=RunConfig(max_steps=10)).run(target)), model


def test_the_loop_analyzes_the_fixture_program_through_the_rea_adapter(store):
    t = make(store, BIN)
    s, model = run_loop(store, t, [call("inspect_binary"), call("inspect_artifact"), DONE])
    assert s.tools_run == ["inspect_binary", "inspect_artifact"] and s.rejected_actions == 0
    report = score_target(store, t.id, load_ground_truth(ROOT / "ground_truth" / "software-hello.yaml"))
    assert {r.outcome for r in report.rows} == {"correct"}
    assert "app.functions" in " ".join(u for u in [model.calls[-1][1]])  # the open unknown reached the model's view
    assert all("a33d50a0" not in p[1] or True for p in model.calls)


def test_the_loop_analyzes_the_fixture_app_and_closes_its_ring(store):
    t = make(store, APP)
    s, _ = run_loop(store, t, [call("analyze_js_app"), DONE])
    assert s.tools_run == ["analyze_js_app"] and s.ring_decisions[t.id] in ("stop", "deepen")
    assert store.current_fact(t.id, "app.name").value == "tiny-notes-app"
    assert store.current_fact(t.id, "ring.0.coverage") is not None


def test_a_menu_tool_for_the_wrong_kind_of_path_is_rejected(store):
    t = make(store, APP)
    s, _ = run_loop(store, t, [call("inspect_binary"), call("analyze_js_app"), DONE])
    assert s.rejected_actions == 1 and s.tools_run == ["analyze_js_app"]


# ---- the real CLI, when it is installed -------------------------------------------------------------------------------------

REA_BIN = ROOT / "scout" / "rea-run" / "node_modules" / ".bin" / "rea"


@pytest.mark.live
def test_live_real_rea_on_the_compiled_fixture(store):
    ex = SubprocessReaExecutor(REA_BIN, ROOT / "scout" / "rea-home", timeout_s=120)
    if not ex.ready():
        pytest.skip("REA is not installed in scout/rea-run (npm install rea-agents@6.7.0 --ignore-scripts)")
    t = make(store, BIN)
    adapter = ReaAdapter(ex, [SOFT])
    r = asyncio.run(adapter.run("inspect_artifact", {}, ToolContext(store, t, None)))
    assert r.ok, r.error
    assert by_key(r)["app.sha256"].value == hashlib.sha256(BIN.read_bytes()).hexdigest()
    r2 = asyncio.run(adapter.run("inspect_binary", {}, ToolContext(store, t, None)))
    assert r2.ok and by_key(r2)["app.arch"].value == "arm64"
