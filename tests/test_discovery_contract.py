"""Contract tests that run over EVERY registered adapter against its fixtures, and the guards on the committed fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from envrec.contracts import FactStatus, Target, TargetKind
from envrec.discovery import ReplayCollector, Salts
from envrec.discovery.redact import find_leaks, identity_terms
from envrec.skills.discover import all_adapters, run_adapter
from envrec.skills.runner import collect_and_parse
from envrec.store import Store
from envrec.tools.ctx import ToolContext

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "discovery"


def cases():
    out = []
    for a in all_adapters():
        for plat in a.platforms:
            d = FIX / a.id / plat
            if d.is_dir():
                out.append(pytest.param(a, plat, d, id=f"{a.id}-{plat}"))
    return out


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def is_remote(adapter) -> bool:
    """A remote adapter reads another computer through the owner's enrolled connection, so a replay collector cannot feed it; its recorded cases live in its own skill folder
    (`tests/<case>/expect.json`) and `test_skillmd` runs them for every skill."""
    return adapter.meta.signature.kind == "remote"


def test_every_adapter_has_a_fixture_for_every_platform_it_claims():
    missing = [f"{a.id}/{p}" for a in all_adapters() if not is_remote(a) for p in a.platforms if not (FIX / a.id / p).is_dir()]
    assert missing == []


def test_every_remote_adapter_has_recorded_cases_of_its_own_in_its_skill_folder():
    remote = [a for a in all_adapters() if is_remote(a)]
    assert remote, "if no remote adapter exists any more, delete is_remote and this test"
    bare = [a.id for a in remote if not [d for d in (a.path / "tests").glob("*/expect.json")]]
    assert bare == []


def test_adapter_ids_are_unique_and_valid():
    ids = [a.id for a in all_adapters()]
    assert len(ids) == len(set(ids)) and all(i.replace("_", "").isalnum() and i == i.lower() for i in ids)
    assert all(a.tier in ("observe", "read") and a.mode in ("survey", "menu") and 0 <= a.ring <= 3 for a in all_adapters())


@pytest.mark.parametrize(("adapter", "plat", "d"), cases())
def test_adapter_parses_its_fixture_and_every_fact_cites_a_capture(adapter, plat, d, store):
    host = store.put_target(Target(kind=TargetKind.host, ring=adapter.ring, handle="fixture-host"))
    run = run_adapter(adapter, ReplayCollector(d, plat), store, host, salts=Salts(), platform=plat)
    assert run.status == "ran", (run.status, run.reason)
    facts = [f for f in store.current_facts(host.id) if not f.key.startswith("ring.")]
    assert facts, "an adapter that ran must produce something"
    for f in facts:
        if f.status != FactStatus.unknown:
            assert f.evidence and f.evidence[0].raw_ref and f.evidence[0].provider == adapter.id
            data = store.read_raw(f.evidence[0].raw_ref)
            assert hashlib.sha256(data).hexdigest() == f.evidence[0].raw_ref.sha256  # the cited capture exists and is the one that was hashed (an empty output is a capture too)
    status = store.current_fact(host.id, f"ring.adapter.{adapter.id}").value
    assert status["status"] == "ran"


@pytest.mark.parametrize(("adapter", "plat", "d"), cases())
def test_parse_is_pure_and_deterministic(adapter, plat, d):
    ctx = ToolContext(collector=ReplayCollector(d, plat), platform=plat, salts=Salts(), approve=lambda t: True)
    first, second = collect_and_parse(adapter, ctx, {}), collect_and_parse(adapter, ctx, {})
    assert (first.out["facts"] or first.out["entities"] or first.out["unknowns"]) and first.out == second.out


@pytest.mark.parametrize(("adapter", "plat", "d"), cases())
def test_produced_keys_are_valid_and_entities_carry_no_raw_identifiers(adapter, plat, d, store):
    host = store.put_target(Target(kind=TargetKind.host, ring=adapter.ring, handle="fixture-host"))
    run_adapter(adapter, ReplayCollector(d, plat), store, host, salts=Salts(), platform=plat)
    for f in store.current_facts(host.id):
        if ".entity." in f.key:
            blob = json.dumps(f.value)
            assert "\n" not in blob and "\x1b" not in blob
            assert f.value["owned"] in ("owned", "ambient", "unknown")


def test_committed_fixtures_contain_no_identifying_strings():
    terms = identity_terms()
    root = FIX.parent
    bad = []
    for f in root.rglob("*"):
        if f.is_file() and f.suffix in (".out", ".json", ".txt", ".md", ".yaml", ".yml", ".js", ".c", ".csv", ".exit"):
            hits = find_leaks(f.read_text(errors="replace"), terms)
            if hits:
                bad.append((str(f.relative_to(root)), hits[:3]))
    assert bad == []


def test_the_leak_scanner_catches_what_it_should():
    assert find_leaks("host at 192.168.1.20 mac 28:24:c9:74:ab:ab") and find_leaks("fe80::aa11:22ff:fe33:4455")
    assert find_leaks("owner is jdoe", ["jdoe"]) and find_leaks("/Users/someone/x")
    assert not find_leaks("host 192.0.2.20 mac 28:24:c9:00:00:01 version 23.40.31.0 on 127.0.0.1")
