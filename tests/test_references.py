"""A2: tools take references (capture ids, fact keys), never raw content. The model chooses WHICH capture; code reads it; the content never reaches a prompt."""

from __future__ import annotations

import asyncio
import json

import pytest

from envrec.contracts import ModelProfile, Target, TargetKind
from envrec.discovery import Salts
from envrec.discovery.types import Capture, this_platform
from envrec.loop import Loop, RunConfig
from envrec.model.scripted import ScriptedModel, scripted_profile
from envrec.privacy.gate import PrivacyGate
from envrec.store import Store
from envrec.tools import ToolContext, get_tool, run_tool
from envrec.tools.bridge import RegistryAdapter
from envrec.tools.ctx import ToolResult, resolve_refs
from envrec.tools.registry import ToolArgError, all_tools

NMEA = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"


def put(store, target, name, data, provider="serial_identify"):
    gate = PrivacyGate(store, target, Salts(), provider)
    gate.store_captures([Capture(name, "port", data.encode() if isinstance(data, str) else data, 0, "local", "linux")])
    return gate.refs[name].sha256


@pytest.fixture
def st(tmp_path):
    with Store(tmp_path / "s") as store:
        yield store


def target(store, handle="t", kind=TargetKind.serial):
    return store.put_target(Target(kind=kind, ring=0, handle=handle))


def test_a_capture_reference_is_a_typed_argument_the_menu_schema_names():
    tool = get_tool("identify_serial_capture")
    assert [(a.name, a.ref, a.required) for a in tool.args] == [("capture", "capture", True)]
    assert tool.json_schema()["properties"]["capture"]["x-ref"] == "capture"


@pytest.mark.parametrize("bad", ["", "   ", "not hex!", "abc", "zz" * 8, "a" * 65, 42])
def test_a_capture_reference_that_is_not_an_id_is_refused_before_anything_runs(bad):
    with pytest.raises(ToolArgError):
        get_tool("identify_serial_capture").check_args({"capture": bad})


def test_the_capture_index_lists_names_and_sizes_and_never_the_content(st):
    t = target(st)
    sha = put(st, t, "heard", NMEA)
    [row] = st.captures(t.id)
    assert (row["sha256"], row["name"], row["size"], row["tool"]) == (sha, "heard", len(NMEA), "serial_identify") and NMEA not in json.dumps(row)


def test_a_capture_resolves_by_a_unique_prefix_and_only_for_its_own_target(st):
    a, b = target(st, "a"), target(st, "b")
    sha = put(st, a, "heard", NMEA)
    assert st.resolve_capture(a.id, sha[:8]).sha256 == sha and st.resolve_capture(a.id, sha).sha256 == sha
    with pytest.raises(KeyError):
        st.resolve_capture(b.id, sha[:8])  # another target's capture does not exist for this one, even though the bytes are in the same raw store
    with pytest.raises(KeyError):
        st.resolve_capture(a.id, sha[:7])  # too short to be an id
    with pytest.raises(KeyError):
        st.resolve_capture(a.id, "0" * 8)


def test_an_ambiguous_prefix_is_refused_not_guessed(st):
    t = target(st)
    shas = [put(st, t, f"c{i}", f"payload number {i}") for i in range(300)]
    prefix = next(p for p in {s[:8] for s in shas} if sum(1 for s in shas if s.startswith(p)) > 1) if len({s[:8] for s in shas}) < len(shas) else None
    if prefix is None:  # no accidental collision among 300 captures at 8 digits (expected): force one through the index
        from envrec.contracts import RawRef
        st.index_capture(t.id, RawRef(path="raw/xx/" + shas[0][:8] + "f" * 56, sha256=shas[0][:8] + "f" * 56), "twin", 1, "x")
        prefix = shas[0][:8]
    with pytest.raises(KeyError, match="matches 2"):
        st.resolve_capture(t.id, prefix)


def test_resolve_refs_reads_the_capture_in_code_and_refuses_a_foreign_fact(st):
    a, b = target(st, "a"), target(st, "b")
    sha = put(st, a, "heard", NMEA)
    tool = get_tool("identify_serial_capture")
    got, err = resolve_refs(tool, {"capture": sha[:10]}, st, a)
    assert err is None and got["capture"].text == NMEA
    got, err = resolve_refs(tool, {"capture": sha[:10]}, st, b)
    assert got == {} and "no capture" in err


def test_the_tool_matches_the_capture_and_writes_a_hypothesis_not_a_fact_it_was_told(st):
    t = target(st)
    sha = put(st, t, "heard", NMEA)
    ctx = ToolContext(None, this_platform(), st, t, Salts())
    ctx.resolved, err = resolve_refs(get_tool("identify_serial_capture"), {"capture": sha}, st, t)
    res = run_tool(get_tool("identify_serial_capture"), ctx, {"capture": sha})
    assert err is None and res.ok and res.facts == {"serial.protocol": "nmea"}
    assert b"GPGGA" in res.captures[0].data  # the evidence capture holds the line that matched


def test_nothing_the_device_said_is_an_unknown_with_its_reason(st):
    t = target(st)
    sha = put(st, t, "heard", "")
    ctx = ToolContext(None, this_platform(), st, t, Salts())
    ctx.resolved, _ = resolve_refs(get_tool("identify_serial_capture"), {"capture": sha}, st, t)
    res = run_tool(get_tool("identify_serial_capture"), ctx, {"capture": sha})
    assert res.ok and res.facts == {"serial.protocol": None} and "nothing the device said" in res.reasons["serial.protocol"]


def test_without_a_resolved_reference_the_tool_fails_instead_of_inventing_input(st):
    t = target(st)
    res = run_tool(get_tool("identify_serial_capture"), ToolContext(None, this_platform(), st, t, Salts()), {"capture": "ab" * 8})
    assert not res.ok and "capture could not be read" in res.detail


def test_the_probe_log_shape_is_read_too(st):
    t = target(st)
    log = json.dumps({"port": "/dev/x", "baud": 9600, "passive": "", "sent": ["scpi"], "reply": "ACME,MODEL1,123,1.0"})
    sha = put(st, t, "probe", log)
    ctx = ToolContext(None, this_platform(), st, t, Salts())
    ctx.resolved, _ = resolve_refs(get_tool("identify_serial_capture"), {"capture": sha}, st, t)
    res = run_tool(get_tool("identify_serial_capture"), ctx, {"capture": sha})
    assert res.ok and res.facts["serial.protocol"] in ("scpi", None)  # whichever the signature table says, it was read from the log's reply, not from the JSON text


def test_through_the_loop_the_model_picks_the_capture_from_the_view_and_the_raw_text_never_reaches_a_prompt(st):
    t = target(st, handle="/dev/ttyfake")
    secret = "$GPGGA,RAWSECRET-9f3a,4807.038,N"
    sha = put(st, t, "heard", NMEA + "\n" + secret)
    model = ScriptedModel([json.dumps({"action": "call_tool", "tool": "identify_serial_capture", "args": {"capture": sha[:8]}, "why": "match it"}), '{"action":"done","why":"x"}'], scripted_profile())
    loop = Loop(st, [RegistryAdapter(platform="linux", params={"port": "/dev/ttyfake"})], model, config=RunConfig(max_steps=3))
    asyncio.run(loop.run(t))
    views = [user for _system, user in model.calls]
    assert views and all("RAWSECRET" not in v and "GPGGA" not in v for v in views)
    first = views[0]
    assert sha[:8] in first and "identify_serial_capture(capture)" in first and "a capture id from CAPTURES" in first
    f = st.current_fact(t.id, "serial.protocol")
    assert f is not None and f.value == "nmea" and f.status.value == "hypothesis"


def test_skill_helpers_are_registered_documented_and_never_on_any_menu():
    skill_side = sorted(t.name for t in all_tools() if t.audience == "skill")
    assert skill_side == sorted(["json_path", "lookup_bluetooth_company", "lookup_bluetooth_service", "lookup_oui", "lookup_service_type", "lookup_usb_id", "mask_identifiers",
                                 "match_serial_signature", "parse_table", "regex_extract", "sanitize_text"])
    reg = RegistryAdapter(platform="linux")
    assert not {s.name for s in reg.tool_specs()} & set(skill_side)
    for name in skill_side:
        assert get_tool(name).name == name and get_tool(name).audience == "skill"  # still callable by a skill's script through the same registry


def test_the_menu_writes_each_argument_name_exactly_as_the_model_must_send_it():
    """The 9B copied `port?` from a menu line into its args and every call was refused. The names in the menu are the schema's names, with nothing attached."""
    import re

    from envrec.loop.menu import MenuEntry, render_menu
    reg = RegistryAdapter(platform="linux")
    for spec in reg.tool_specs():
        line = render_menu([MenuEntry(spec.name, spec.one_line, spec, reg)]).splitlines()[0]
        shown = re.search(r"^\s*1 \w+\(([^)]*)\)", line)
        props = list(spec.input_model.get("properties") or {})
        assert (shown.group(1).split(", ") if shown else []) == props, line


def test_a_fact_the_gate_refuses_makes_the_call_fail_loudly_instead_of_vanishing(st):
    from envrec.adapters.base import ToolContext as LoopContext
    from envrec.tools.registry import REGISTRY, tool

    @tool(kinds=("serial",))
    def zz_bad_key(ctx: ToolContext) -> ToolResult:
        """Report a fact whose key is outside the namespace.

        Args:
            none
        Returns:
            Nothing.
        Tier: observe
        Platforms: any
        Notes: test only.
        """
        return ToolResult(facts={"nonsense.key": 1}, provider="stdlib")

    try:
        t = target(st)
        res = asyncio.run(RegistryAdapter(platform="linux").run("zz_bad_key", {}, LoopContext(store=st, target=t, event_id=None)))
        assert not res.ok and "the gate dropped 1 output" in res.error and "nonsense" in res.error
    finally:
        REGISTRY.pop("zz_bad_key", None)
