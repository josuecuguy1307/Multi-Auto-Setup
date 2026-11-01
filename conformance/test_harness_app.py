"""HARNESS APP conformance (docs/HARNESS_SPEC.md, section HA). Each check that can run has a fixture that violates it and is shown to fail."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

import harnessrules as R

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIX = Path(__file__).parent / "harnessfixtures"
sys.path.append(str(ROOT / "tests"))
ADAPTERS = [SRC / "envrec_harness" / n for n in ("mcp_server.py", "openwebui_pipe.py", "client.py")]


@pytest.mark.parametrize("f", ADAPTERS, ids=lambda p: p.name)
def test_HA1_adapters_contain_no_decision_logic(f):
    assert R.adapter_decision_sites(f) == []


def test_HA1_fails_on_an_adapter_that_branches_on_sensitivity_mints_an_owner_or_runs_a_program():
    sites = R.adapter_decision_sites(FIX / "adapter_bad" / "adapter.py")
    joined = "\n".join(sites)
    for must in ("import subprocess", "from envrec_coordinator", "'confidential'", "sensitivity", "'OWNER'", "'approved'", ".cloud"):
        assert must in joined, must


def test_HA1_passes_an_adapter_that_only_translates():
    assert R.adapter_decision_sites(FIX / "adapter_ok" / "adapter.py") == []


def test_HA2_a_channel_with_a_model_in_the_middle_never_lists_a_mechanism_a_model_could_press():
    assert R.channel_registry_violations(SRC / "envrec_coordinator" / "harness.py") == []


def test_HA2_fails_on_a_cloud_or_model_mediated_channel_that_lists_the_panels_click_or_the_chats_own_dialog():
    v = R.channel_registry_violations(FIX / "channels_bad" / "channels.py")
    assert len(v) == 3 and all("model in the middle" in x for x in v)


def test_HA2_passes_the_good_registry():
    assert R.channel_registry_violations(FIX / "channels_ok" / "channels.py") == []


def test_HA2_runtime_a_tool_call_from_every_channel_fails_to_approve_a_write(tmp_path):
    from mas_world import coordinator
    from test_harness_core import ask_write
    from envrec_coordinator.harness import CHANNELS, Refused

    async def go():
        async with coordinator(tmp_path) as c:
            p = ask_write(c)
            for ch in CHANNELS.values():
                with pytest.raises(Refused):
                    c.harness.decide(ch, p.intent.id, "approve")
            assert not p.future.done()
    asyncio.run(go())


def test_HA3_runtime_nothing_confidential_reaches_a_cloud_channel_in_any_intent_kind(tmp_path):
    from mas_world import coordinator
    from envrec_coordinator.harness import CHANNELS

    async def go():
        async with coordinator(tmp_path) as c:
            secret = "S3CRET-PAYLOAD"
            for kind, payload in (("text", {}), ("progress", {"step": 1}), ("show_data", {"data_kind": "machine", "data": {"x": secret}}),
                                  ("ask_choice", {"writes": False, "options": [{"id": "a", "label": secret}]}), ("confirm", {"writes": True, "summary": secret, "command": secret, "undo": secret, "plan_hash": "sha256:" + "ab" * 32})):
                text = f"{secret}\n1. {secret}" if kind == "ask_choice" else secret
                i = c.harness._add("r", kind, "GUARD", "confidential", payload, text, f"r:{kind}")
                seen = c.harness.for_channel(i, CHANNELS["claude-desktop"])
                assert secret not in str(seen), kind
                assert secret in str(c.harness.for_channel(i, CHANNELS["openwebui"])), kind  # a local chat does see it: the filter is about the cloud, not a blanket redaction
    asyncio.run(go())


def test_HA4_every_intent_the_core_can_make_has_a_text_fallback(tmp_path):
    from envrec_harness.intents import Intent, IntentInvalid, validate
    for kind, payload in (("text", {}), ("progress", {}), ("show_data", {"data_kind": "network"}), ("ask_choice", {"writes": False, "options": [{"id": "a", "label": "A"}]}),
                          ("confirm", {"writes": False})):
        with pytest.raises(IntentInvalid):
            validate(Intent(id="i", kind=kind, run_id=None, actor="GUARD", sensitivity="internal", payload=payload, text_fallback=""))


def test_HA4_the_core_builds_intents_only_through_the_validating_door():
    import ast
    tree = ast.parse((SRC / "envrec_coordinator" / "harness.py").read_text())
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name not in ("_add", "network_intent"):
            for n in ast.walk(fn):
                assert not (isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Intent"), f"{fn.name} builds an Intent without validate()"


CARD_FILES = [SRC / "envrec_harness" / n for n in ("cards.py", "icons.py", "host.py", "strings.py")]


@pytest.mark.parametrize("f", CARD_FILES, ids=lambda p: p.name)
def test_HA5_the_card_layer_names_no_url_and_makes_no_network_call(f):
    assert R.card_external_sites(f) == []


def test_HA5_fails_on_a_card_that_loads_a_font_an_image_a_stylesheet_or_calls_the_network():
    sites = "\n".join(R.card_external_sites(FIX / "cards_bad" / "card.html"))
    for must in ("https://fonts.example", "//cdn.example.com", "@import", "url(http", "fetch(", "<iframe"):
        assert must in sites, must


def test_HA5_passes_a_card_that_draws_inline():
    assert R.card_external_sites(FIX / "cards_ok" / "card.html") == []


def test_HA6_capability_detection_degrades_cleanly(tmp_path):
    from mas_world import coordinator
    from envrec_coordinator.harness import CHANNELS

    async def go():
        async with coordinator(tmp_path) as c:
            ch = CHANNELS["claude-desktop"]
            assert c.harness.mechanisms(ch) in (["os_dialog"], [])  # nothing declared: no elicitation is assumed
            c.harness.declare(ch, {"elicitation": True, "something_new": "yes", "nested": {"x": 1}})  # an unknown or malformed capability is ignored, never an error
            assert c.harness.caps["claude-desktop"] == {"elicitation": True, "something_new": "yes"}
            assert c.harness.capabilities(ch)["fallback"] == "panel_click"  # the owner panel is always the last resort
    asyncio.run(go())


def test_HA7_the_views_javascript_is_executed_not_only_read(tmp_path):
    """The MCP view runs in a host simulator that follows the published protocol (Node): the handshake completes, a non-zero height is reported, the card is painted, and nothing throws. A shell whose
    script cannot run was invisible in the first real chat session and passed every string-matching test."""
    import json
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("not measured: node is not installed")
    from envrec_harness import host
    js = tmp_path / "mcp.js"
    js.write_text("\n".join(re.findall(r"<script>(.*?)</script>", host.document("", "mcp"), flags=re.S)))
    done = subprocess.run([node, str(ROOT / "tests" / "js" / "host_sim.mjs"), str(js), "normal"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and not done.stderr.strip(), done.stderr[:400]
    r = json.loads(done.stdout.strip().splitlines()[-1])
    assert r["rendered"] == "MAP" and r["tools"] == ["mas_view"] and all(json.loads(x)["height"] > 0 for x in r["sizes"])


def test_HA8_a_setup_approval_counts_as_the_owners_only_with_a_human_gesture():
    """Direct calls cannot mint the owner: the coordinator's front door for a run's answers refuses an approval of a writing step unless the human gesture that made it comes with it."""
    from types import SimpleNamespace
    from envrec_coordinator.runs import AgentRuns
    seen, logs = [], []
    adapter = SimpleNamespace(pending=SimpleNamespace(qid="approve_run_1", data={"kind": "approve_step"}), answer=lambda qid, opt, by, ch: seen.append((qid, by)) or True)
    runs = AgentRuns.__new__(AgentRuns)
    runs.runs = {"r": SimpleNamespace(adapter=adapter)}
    runs.coord = SimpleNamespace(log=SimpleNamespace(add=lambda *a, **k: logs.append(a)))
    assert runs.answer("r", "approve_run_1", "approve") is False
    assert runs.answer("r", "approve_run_1", "approve", "owner", "claude-desktop", "something-else") is False
    assert seen == [] and [x[0] for x in logs] == ["owner_authority_refused"] * 2
    assert runs.answer("r", "approve_run_1", "approve", "owner", None, "os_dialog") is True and seen == [("approve_run_1", "owner")]
    assert runs.answer("r", "approve_run_1", "approve", "claude-code") is True and seen[-1] == ("approve_run_1", "claude-code")  # the operator is recorded as the operator, and the task desk will not run the step


def test_HA9_the_text_a_human_approves_cannot_be_changed_by_a_host_that_strips_tags():
    """The plan's `<mas>` is shown as `MAS_HOME` (reversible); a write confirmation holding angle brackets is refused, whatever the field."""
    import re
    from envrec_harness.intents import Intent, IntentInvalid, for_display, undisplay, validate
    raw = "rm -rf <mas>/downloads/x"
    assert re.sub(r"<[^>]*>", "", raw) == "rm -rf /downloads/x"  # what an owner saw before
    assert re.sub(r"<[^>]*>", "", for_display(raw)) == for_display(raw) and undisplay(for_display(raw)) == raw
    base = {"writes": True, "summary": "Undo step 1", "command": for_display(raw), "undo": "nothing", "plan_hash": "sha256:" + "cd" * 32}
    validate(Intent(id="i", kind="confirm", run_id=None, actor="GUARD", sensitivity="internal", payload=base, text_fallback="Undo step 1"))
    for field in ("command", "undo", "summary"):
        with pytest.raises(IntentInvalid):
            validate(Intent(id="i", kind="confirm", run_id=None, actor="GUARD", sensitivity="internal", payload={**base, field: raw}, text_fallback="Undo step 1"))


def test_HA10_every_connection_recipe_cites_its_documentation_and_a_cell_is_documented_or_refuses():
    """The recipes are data and every step has a builder, a runner and an undo; a host cites the tool's own documentation; an operating system the documentation does not cover is a typed refusal, never a guess."""
    import json
    from envrec_coordinator import connect as C
    r = C.load_recipes()
    assert set(r["hosts"]) >= {"openwebui", "claude-desktop", "claude-code", "codex", "opencode", "muse-code"}
    for host, h in r["hosts"].items():
        assert h["sources"] and all(src.startswith("http") for src in h["sources"]), host
        for spec in h["steps"]:
            assert spec["op"] in C._BUILD and spec["op"] in C._EXEC, (host, spec["op"])
        for os_name, cell in h["cells"].items():
            assert cell.get("status") or cell.get("refuse") == "os_not_documented", (host, os_name)
    assert "WEBUI_AUTH" not in json.dumps(r["hosts"]["openwebui"]["steps"])  # single-user mode is not used: any web page could sign in as its administrator
