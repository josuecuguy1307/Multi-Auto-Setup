"""S3: every builtin skill must reproduce, on the same recorded fixtures, exactly what the YAML skill and the adapter before it produced (golden outputs captured
before any migration, commit 42ed3b7). Same status, counts, unknown reasons, fact keys, statuses and values, entity attributes included."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import golden_tables
from envrec.contracts import Target, TargetKind
from envrec.discovery import ReplayCollector, Salts
from envrec.skills.discover import library
from envrec.skills.sbx import docker_ready
from envrec.store import Store
from envrec.tools.ctx import ToolContext

pytestmark = pytest.mark.skipif(not docker_ready(), reason="the skill scripts run in the Docker sandbox")
ROOT = Path(__file__).resolve().parent.parent
GOLD = sorted((ROOT / "tests" / "golden" / "discovery").glob("*.json"))


@pytest.fixture(scope="module", autouse=True)
def tables():
    golden_tables.install()
    yield
    golden_tables.uninstall()


def fixed_salts() -> Salts:
    s = Salts()
    s.run, s._stable = "GOLDEN-RUN-SALT", "GOLDEN-STABLE-SALT"
    return s


@pytest.mark.parametrize("gold", GOLD, ids=[g.stem for g in GOLD])
def test_skill_reproduces_the_golden_output_of_the_adapter_it_replaced(gold, tmp_path):
    from envrec.skills.runner import run_skill
    g = json.loads(gold.read_text())
    skill = library().get(g["adapter"])
    if skill is None:
        pytest.skip("not migrated yet")
    with Store(tmp_path / "s") as st:
        host = st.put_target(Target(kind=TargetKind.host, ring=skill.meta.ring, handle="golden-host"))
        d = ROOT / "fixtures" / "discovery" / g["adapter"] / g["fixture"]
        ctx = ToolContext(collector=ReplayCollector(d, g["platform"]), platform=g["platform"], store=st, target=host, salts=fixed_salts(), approve=lambda text: True)
        run = run_skill(skill, ctx, params={})
        facts = sorted(({"key": f.key, "status": f.status.value, "value": f.value} for f in st.current_facts(host.id) if not f.key.startswith("ring.adapter.")), key=lambda x: x["key"])
    assert run.status == g["status"], (run.status, run.reason)
    assert {"entities": run.entities, "resolved": run.resolved, "unknowns": run.unknowns, "facts": run.facts} == g["counts"]
    assert run.unknown_reasons == g["unknown_reasons"]
    assert json.loads(json.dumps(facts, default=str)) == json.loads(json.dumps(g["facts"], default=str))


def test_every_adapter_has_been_migrated_to_a_skill_folder():
    adapters = {json.loads(g.read_text())["adapter"] for g in GOLD}
    lib = library()
    assert [e for e in lib.errors if not e[0].startswith("legacy")] == [], lib.errors
    assert adapters <= {s.id for s in lib.skills.values()}, sorted(adapters - {s.id for s in lib.skills.values()})
