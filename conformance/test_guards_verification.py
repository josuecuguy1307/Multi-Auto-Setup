"""GUARDS (G1) and VERIFICATION (V1-V4)."""
import ast
import re
import subprocess
from pathlib import Path

import pytest

from envrec.contracts import CreatedBy, FactStatus, Target, TargetKind
from envrec.contracts.models import Evidence, Fact, RawRef
from envrec.store import Store

from conftest import ROOT

SRC = ROOT / "src"


def test_G1_an_authorized_target_cannot_be_registered_except_from_the_config_file(tmp_path):
    with Store(tmp_path / "s") as st:
        with pytest.raises(PermissionError):
            st.put_target(Target(kind=TargetKind.host, ring=2, handle="x", authorized=True))
        st.put_target(Target(kind=TargetKind.host, ring=2, handle="y", authorized=False))
        again = st.put_target(Target(kind=TargetKind.host, ring=2, handle="y", authorized=True), from_config=True)
        assert again.authorized is True


def test_G1_enrollment_refuses_to_run_inside_the_loop_and_without_a_terminal(tmp_path):
    from envrec.enroll import EnrollmentRefused, enroll
    from envrec.guard import LOOP_ACTIVE
    from envrec.inventory.report import Candidate
    cand = Candidate.__new__(Candidate)
    token = LOOP_ACTIVE.set(True)
    try:
        with pytest.raises(EnrollmentRefused):
            enroll(cand, targets_path=tmp_path / "t.yaml", store=None, check=lambda *a: True, is_tty=lambda: True)
    finally:
        LOOP_ACTIVE.reset(token)
    with pytest.raises(EnrollmentRefused):
        enroll(cand, targets_path=tmp_path / "t.yaml", store=None, check=lambda *a: True, is_tty=lambda: False)


def test_G1_the_only_code_that_writes_targets_yaml_is_enrollment():
    callers, other_writes = [], []
    for p in (SRC / "envrec").rglob("*.py"):
        rel = p.relative_to(SRC).as_posix()
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if "_write_atomic(" in line and rel != "envrec/enroll/enroll.py":
                callers.append(f"{rel}:{n}")
            if re.search(r"(write_text|os\.replace|safe_dump)\(.*targets", line) and "proposals" not in line and "discovered" not in line and rel != "envrec/enroll/enroll.py":
                other_writes.append(f"{rel}:{n}")
    assert callers == [] and other_writes == [], (callers, other_writes)


def test_G1_the_scope_guard_refuses_public_addresses_and_ranges_wider_than_the_cap():
    from envrec.discovery.scope import ScopeError, validate_target
    import ipaddress
    own = lambda: [("en0", ipaddress.IPv4Interface("192.168.50.7/24"))]  # noqa: E731
    for spec in ("8.8.8.8", "192.168.0.0/16", "10.1.2.3"):
        with pytest.raises(ScopeError):
            validate_target(spec, own=own, resolver=lambda *a, **k: [])
    assert [str(a) for a in validate_target("192.168.50.9", own=own, resolver=lambda *a, **k: [])] == ["192.168.50.9"]


def test_G1_the_worker_contains_no_deploy_enroll_or_scan_code():
    forbidden = ("envrec.enroll", "envrec.remote.provision", "envrec.discovery", "envrec.discover", "envrec.cli_fleet", "envrec.helper")
    hits = []
    for p in (SRC / "envrec_worker").rglob("*.py"):
        for n in ast.walk(ast.parse(p.read_text())):
            mods = [n.module] if isinstance(n, ast.ImportFrom) and n.module else [a.name for a in n.names] if isinstance(n, ast.Import) else []
            hits += [f"{p.name}: {m}" for m in mods if m.startswith(forbidden)]
    assert hits == []


def test_G1_no_credential_or_key_material_is_stored_in_the_repository():
    secret = re.compile(r"BEGIN [A-Z ]*PRIVATE KEY|password\s*[:=]\s*\S{4,}|api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9]{16,}", re.I)
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    hits = [f for f in files if f.endswith((".py", ".yaml", ".json", ".md", ".txt", ".swift")) and "tests/" not in f and "conformance/" not in f
            and secret.search((ROOT / f).read_text(errors="replace"))]
    assert hits == [], hits


def _ev(provider, sha):
    return Evidence(provider=provider, probe="p", confidence=0.9, raw_ref=RawRef(sha256=sha, path="raw/x", size=1) if "size" in RawRef.model_fields else RawRef(sha256=sha, path="raw/x"))


def test_V1_the_store_refuses_a_verified_fact_whose_creator_is_the_verifier_and_who_supersedes_nothing_by_another_creator(tmp_path):
    """created_by must never be the verifier for a claim the verifier made up: verification is a judgment of someone else's claim."""
    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.host, ring=0, handle="x"))
        with pytest.raises(Exception):
            st.write_fact(Fact(target_id=t.id, key="host.model_server", value="running", status=FactStatus.verified, created_by=CreatedBy.verifier, evidence=[_ev("verifier", "a" * 64)]))


def test_V1_two_evidences_from_the_same_raw_capture_are_not_independent(tmp_path):
    same = "b" * 64
    with pytest.raises(Exception):
        Fact(target_id="t", key="host.model_server", value="running", status=FactStatus.verified, created_by=CreatedBy.probe, evidence=[_ev("provider-a", same), _ev("provider-b", same)])


def test_V1_two_evidences_from_distinct_raw_captures_do_count(tmp_path):
    f = Fact(target_id="t", key="host.model_server", value="running", status=FactStatus.verified, created_by=CreatedBy.probe, evidence=[_ev("provider-a", "c" * 64), _ev("provider-b", "d" * 64)])
    assert f.status == FactStatus.verified


def test_V2_the_false_verified_report_states_its_denominator_and_zero_over_zero_is_no_evidence():
    from envrec.scoring.scorer import RowScore, ScoreReport
    none = ScoreReport(rows=[], unscored=[], machine="m", provisional=False)
    assert none.false_verified_rate is None and "nothing verified" in none.render()
    rows = [RowScore("host.os", "linux", "linux", "verified", "correct", ""), RowScore("host.ram", 8, 4, "verified", "wrong", "")]
    out = ScoreReport(rows=rows, unscored=[], machine="m", provisional=False).render()
    assert "1 of 2 verified are wrong" in out


def check_V2_recorded_runs_labeled(root):
    import subprocess as sp

    from recorded_runs import require_recorded_runs
    require_recorded_runs(root)  # no recorded runs in this checkout: "not measured" (a skip), never a pass; with runs the check below runs unchanged
    out = sp.run(["python", str(root / "conformance" / "tools" / "actor_counts.py"), str(root / "runs")], capture_output=True, text=True, cwd=root).stdout
    assert "TOTALS BY LABEL (never summed across labels)" in out and re.search(r"^\s+REAL\s+runs=", out, re.M) and re.search(r"^\s+FIXTURE\s+runs=", out, re.M)


def test_V2_the_recorded_runs_are_labeled_real_or_fixture_and_never_summed():
    check_V2_recorded_runs_labeled(ROOT)


def test_V3_a_server_that_does_not_echo_a_fresh_nonce_is_not_verified(tmp_path):
    from envrec.verify.server_check import verify_server

    class Parrot:
        def chat(self, prompt):
            return "I am up and running."

    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.host, ring=0, handle="x"))
        res = verify_server(Parrot(), st, t)
        assert res.ok is False and st.current_fact(t.id, "host.model_server") is None


def test_V3_a_server_that_echoes_the_fresh_nonce_is_verified_by_code(tmp_path):
    from envrec.verify.server_check import verify_server

    class Echo:
        def chat(self, prompt):
            return prompt.rsplit(" ", 1)[1] + " ok"

    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.host, ring=0, handle="x"))
        res_without_claim = verify_server(Echo(), st, t)  # nothing claims the server is up: a verifier never creates the claim it verifies
        assert res_without_claim.ok is False and "nothing claims" in res_without_claim.reason and st.current_fact(t.id, "host.model_server") is None
        st.write_fact(Fact(target_id=t.id, key="host.model_server", value="running", status=FactStatus.hypothesis, created_by=CreatedBy.loop, evidence=[Evidence(provider="deploy", probe="start", confidence=0.6)]))
        assert verify_server(Echo(), st, t).ok is True
        verified = st.current_fact(t.id, "host.model_server")
        assert verified.status == FactStatus.verified and verified.created_by == CreatedBy.verifier


VACUOUS = [re.compile(r"^\s*assert\s+[\w\.\[\]\(\)'\", ]+\s+is not None\s*(#.*)?$"), re.compile(r"^\s*assert\b.*\bor True\b"), re.compile(r"^\s*assert\s+True\b"), re.compile(r"^\s*assert\s+1\s*$")]


def test_V4_no_test_file_contains_a_vacuous_assertion():
    hits = []
    for base in (ROOT / "tests", ROOT / "conformance"):
        for p in sorted(base.rglob("test_*.py")):
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if any(v.search(line) for v in VACUOUS):
                    hits.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()}")
    assert hits == [], "\n" + "\n".join(hits)


def test_V1_two_captures_over_one_channel_are_not_independent():
    with pytest.raises(Exception):
        Fact(target_id="t", key="host.model_server", value="running", status=FactStatus.verified, created_by=CreatedBy.probe,
             evidence=[_ev("a", "e" * 64).model_copy(update={"channel": "ssh"}), _ev("b", "f" * 64).model_copy(update={"channel": "ssh"})])


def test_V1_two_captures_over_two_channels_are_independent_even_with_the_same_provider_name():
    f = Fact(target_id="t", key="host.model_server", value="running", status=FactStatus.verified, created_by=CreatedBy.probe,
             evidence=[_ev("probe", "1" * 64).model_copy(update={"channel": "ssh"}), _ev("probe", "2" * 64).model_copy(update={"channel": "usb"})])
    assert f.status == FactStatus.verified


def test_V1_a_fact_stored_under_the_old_rule_still_loads(tmp_path):
    """Old data: two provider names, no channel, no raw reference. It was accepted when written; reading it back must not fail."""
    import sqlite3
    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.host, ring=0, handle="x"))
        legacy = Fact.model_construct(id="fact_old", target_id=t.id, key="host.os", value="linux", status=FactStatus.verified, evidence=[Evidence(provider="a", probe="p", confidence=0.9), Evidence(provider="b", probe="q", confidence=0.9)],
                                      supersedes=None, created_by=CreatedBy.probe, ts=__import__("datetime").datetime(2026, 1, 1), alt_values=[])
        st.db.execute("INSERT INTO facts(id, target_id, key, status, is_current, supersedes, json) VALUES(?,?,?,?,1,NULL,?)", (legacy.id, t.id, legacy.key, "verified", legacy.model_dump_json()))
        st.db.commit()
        loaded = st.current_fact(t.id, "host.os")
    assert loaded.status == FactStatus.verified and [e.provider for e in loaded.evidence] == ["a", "b"] and loaded.evidence[0].channel == ""


def test_T3_every_approval_of_a_read_tier_tool_is_an_event_and_a_refusal_too(tmp_path):
    from envrec.discovery import Salts
    from envrec.discovery.types import this_platform
    from envrec.tools import ToolContext, get_tool, run_tool
    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.serial, ring=0, handle="/dev/ttys999"))
        for answer in (False, True):
            ctx = ToolContext(None, this_platform(), st, t, Salts(), {}, lambda plan, a=answer: a)
            run_tool(get_tool("serial_identify"), ctx, {"port": "/dev/ttys999"})
        ev = st.events(type="approval")
        assert [(e.payload["tool"], e.payload["granted"], e.payload["tier"]) for e in ev] == [("serial_identify", False, "read"), ("serial_identify", True, "read")]
        assert "/dev/ttys999" in ev[0].payload["plan"] and {e.tag for e in ev} == {"OWNER"}
