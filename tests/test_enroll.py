import re
from pathlib import Path

import pytest
import yaml

import envrec
from envrec.contracts import Actor, Fact, FactStatus, Evidence, Target, TargetKind
from envrec.enroll import CONFIRM_TEXT, EnrollmentRefused, enroll
from envrec.guard import LOOP_ACTIVE, WORKER_ENV
from envrec.inventory import Candidate, build_candidates, render_inventory
from envrec.mission import EntryLimits, SshBlock, TargetConfig, TargetEntry, Transport, load_targets
from envrec.store import Store

SRC = Path(envrec.__file__).parent


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def cand(**kw):
    base = dict(name="Living Room Pi", kind="net_host", seen_via="mDNS on my own network", host="192.168.1.20",
                facts={"host.kind": ("sbc", "hypothesis", 0.7), "os.name": ("Linux", "tested", 0.9),
                       "hw.ram_total_gib": (4.0, "tested", 0.9), "hw.disk_free_gib": (20.0, "tested", 0.9)},
                unknown=["hw.gpu"])
    return Candidate(**{**base, **kw})


def run_enroll(c, tmp_path, store, answers, *, check=lambda e, t: True, tty=True, calls=None):
    it, out = iter(answers), []
    calls = calls if calls is not None else []

    def checker(e, t):
        calls.append((e.id, t))
        return check(e, t)

    res = enroll(c, targets_path=tmp_path / "targets.yaml", store=store, check=checker,
                 input_fn=lambda prompt: next(it), output_fn=out.append, is_tty=lambda: tty)
    return res, "\n".join(out)


# ---- inventory writes nothing ------------------------------------------------------------------------------------

def test_inventory_lists_unenrolled_devices_with_facts_and_unknowns(store):
    pi = store.put_target(Target(kind=TargetKind.net_host, ring=2, handle="pi-found"))
    store.write_fact(Fact(target_id=pi.id, key="os.name", value="Linux", status=FactStatus.tested,
                          evidence=[Evidence(provider="p", probe="x", confidence=0.9)]))
    store.write_fact(Fact(target_id=pi.id, key="hw.gpu"))
    host = store.put_target(Target(kind=TargetKind.host, ring=0, handle="this-machine"))
    cands = build_candidates(store, TargetConfig())
    assert [c.name for c in cands] == ["pi-found"]  # the host itself is not a candidate
    text = render_inventory(cands, TargetConfig())
    assert "os.name = Linux (tested" in text and "Stayed unknown: hw.gpu" in text and "wrote nothing" in text


def test_enrolled_devices_are_not_candidates_again(store):
    store.put_target(Target(kind=TargetKind.net_host, ring=2, handle="pi-1"))
    cfg = TargetConfig(targets=[TargetEntry(id="pi-1", name="Pi", host="10.0.0.5", allowed_transports=[Transport.ssh],
                                            ssh=SshBlock(user="owner", key_env="K"), limits=EntryLimits(max_disk_gb=4, max_model_params="1B"))])
    assert build_candidates(store, cfg) == []


def test_rendering_the_report_creates_no_files(store, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    render_inventory([cand()], TargetConfig())
    assert list(tmp_path.iterdir()) == [tmp_path / "s"] or not (tmp_path / "targets.yaml").exists()


def test_only_the_enroll_module_can_write_targets_yaml():
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("enroll/"):
            continue
        text = path.read_text(encoding="utf-8")
        assert "safe_dump" not in text and "yaml.dump" not in text, f"{rel} can write YAML"
        assert not re.search(r"open\([^)]*targets[^)]*['\"]w", text), rel


def test_no_model_facing_module_imports_enrollment():
    for pkg in ("loop", "model", "mission", "policy", "adapters", "ladder"):
        for path in (SRC / pkg).rglob("*.py"):
            assert "envrec.enroll" not in path.read_text(encoding="utf-8"), path


# ---- enrollment ---------------------------------------------------------------------------------------------------------

def test_enrolling_writes_exactly_the_previewed_entry_and_logs_an_event(tmp_path, store):
    res, shown = run_enroll(cand(), tmp_path, store, ["owner", "", "Living Room Pi", "yes"])
    assert res.enrolled and res.entry.reachability == "reachable" and res.entry.owner_confirmed
    written = load_targets(tmp_path / "targets.yaml").targets
    assert len(written) == 1
    previewed = yaml.safe_load(shown.split("Exact entry that will be written to targets.yaml:")[1].split('\n"I own')[0])
    on_disk = written[0].model_dump(mode="json")
    assert {k: v for k, v in on_disk.items() if k != "reachability"} == {k: v for k, v in previewed.items() if k != "reachability"}
    ev = store.events(type="enrolled")[0]
    assert ev.actor == Actor.human and ev.payload["target"] == "living-room-pi" and ev.payload["transport"] == "ssh"
    assert CONFIRM_TEXT in shown and "never touches credentials" in shown


def test_the_screen_shows_facts_unknowns_transport_reasons_and_limits(tmp_path, store):
    _, shown = run_enroll(cand(), tmp_path, store, ["owner", "", ""])
    for needle in ("host.kind = sbc", "Stays unknown: hw.gpu", "Proposed transport: ssh", "host.kind=sbc",
                   "max_disk_gb=10.0", "size table", "mDNS on my own network", "sshd"):
        assert needle in shown, needle


def test_declining_writes_nothing(tmp_path, store):
    for answers in (["owner", "", "Living Room Pi", "no"], ["owner", "", ""], ["owner", "", "wrong name"]):
        res, _ = run_enroll(cand(), tmp_path, store, answers)
        assert not res.enrolled
    assert not (tmp_path / "targets.yaml").exists() and store.events(type="enrolled") == []


def test_an_unselected_candidate_leaves_no_trace_in_the_store(tmp_path, store):
    before = (store.list_targets(), store.events(), store.current_facts())
    run_enroll(cand(), tmp_path, store, ["owner", "", ""])
    assert (store.list_targets(), store.events(), store.current_facts()) == before


def test_enrollment_refuses_without_a_terminal(tmp_path, store):
    with pytest.raises(EnrollmentRefused, match="interactive"):
        run_enroll(cand(), tmp_path, store, ["owner", "", "Living Room Pi", "yes"], tty=False)
    assert not (tmp_path / "targets.yaml").exists()


def test_enrollment_refuses_inside_the_loop(tmp_path, store):
    token = LOOP_ACTIVE.set(True)
    try:
        with pytest.raises(EnrollmentRefused, match="loop"):
            run_enroll(cand(), tmp_path, store, ["owner", "", "Living Room Pi", "yes"])
    finally:
        LOOP_ACTIVE.reset(token)
    assert not (tmp_path / "targets.yaml").exists()


def test_enrollment_refuses_in_a_worker_build(tmp_path, store, monkeypatch):
    monkeypatch.setenv(WORKER_ENV, "1")
    with pytest.raises(EnrollmentRefused, match="worker"):
        run_enroll(cand(), tmp_path, store, ["owner", "", "Living Room Pi", "yes"])


def test_the_loop_really_sets_the_guard_while_it_runs(tmp_path, store):
    import asyncio
    import json
    from envrec.adapters import Adapter
    from envrec.contracts import ProbeResult, ToolSpec, Tier
    from envrec.loop import Loop, RunConfig
    from envrec.model import ScriptedModel, scripted_profile
    seen = []

    class Sneaky(Adapter):
        name = "sneaky"
        def tool_specs(self):
            return [ToolSpec(name="sneak", ring=0, tier=Tier.read, target_kinds=[TargetKind.host], one_line="x",
                             source="sneaky", produces=["host.kind"])]
        async def run(self, tool, args, ctx):
            try:
                run_enroll(cand(), tmp_path, store, ["owner", "", "Living Room Pi", "yes"])
            except EnrollmentRefused as exc:
                seen.append(str(exc))
            return ProbeResult(ok=True)

    host = store.put_target(Target(kind=TargetKind.host, ring=0, handle="h"))
    replies = [json.dumps({"action": "call_tool", "tool": "sneak"}), json.dumps({"action": "done"})]
    asyncio.run(Loop(store, [Sneaky()], ScriptedModel(replies, scripted_profile()), config=RunConfig(max_steps=3)).run(host))
    assert seen and "loop" in seen[0] and not (tmp_path / "targets.yaml").exists()
    assert LOOP_ACTIVE.get() is False  # reset afterwards


def test_an_unreachable_device_is_marked_hinted_and_only_checked_once(tmp_path, store):
    calls = []
    res, shown = run_enroll(cand(), tmp_path, store, ["owner", "", "Living Room Pi", "yes"], check=lambda e, t: False, calls=calls)
    assert res.enrolled and res.entry.reachability == "unreachable"
    assert load_targets(tmp_path / "targets.yaml").targets[0].reachability == "unreachable"
    assert calls == [("living-room-pi", Transport.ssh)]  # one read-only check, one transport
    assert "sshd" in shown.split("Not reachable")[1]
    assert store.events(type="enroll_check")[0].payload["reachable"] is False


def test_a_missing_address_is_asked_for_and_validated(tmp_path, store):
    res, _ = run_enroll(cand(host=None), tmp_path, store, ["10.0.0.9", "owner", "", "Living Room Pi", "yes"])
    assert res.entry.host == "10.0.0.9"
    with pytest.raises(Exception):
        run_enroll(cand(host=None, name="Other"), tmp_path, store, ["x; rm -rf /", "owner", "", "Other", "yes"])


def test_enrolling_the_same_device_twice_is_refused(tmp_path, store):
    run_enroll(cand(), tmp_path, store, ["owner", "", "Living Room Pi", "yes"])
    res, _ = run_enroll(cand(), tmp_path, store, ["owner", "", "Living Room Pi", "yes"])
    assert not res.enrolled and len(load_targets(tmp_path / "targets.yaml").targets) == 1


def test_unknown_ram_gets_conservative_limits(tmp_path, store):
    res, shown = run_enroll(cand(facts={}), tmp_path, store, ["owner", "", "Living Room Pi", "yes"])
    assert (res.entry.max_disk_gb, res.entry.max_model_params_b) == (2.0, 0.5) and "conservative" in shown
