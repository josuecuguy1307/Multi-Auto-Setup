"""Task types are DATA: the catalog (`task_types` of each environment, and the mode that serves each) and the suites (`data/admission/*/suite.yaml`). No list of type names lives in code, a type the code never
heard of loads, and the shipped data is consistent. SIMULATED-LOCAL: no process, no network."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from envrec_coordinator.admission.suite import DATA_ROOT, SuiteRefused, load_suite, parse_suite
from envrec_coordinator.admission.graders import grade
from envrec_net import envrun as E
from envrec_net import task as T

SRC = Path(__file__).resolve().parents[1] / "src"
SUITES = sorted(p.parent.name for p in DATA_ROOT.glob("*/suite.yaml"))
ENVS = E.load_environments()


def test_no_list_of_task_type_names_lives_in_code():
    hits = []
    for p in SRC.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"^\s*TASK_TYPES\s*=|Literal\[\"code\",\s*\"text\"", text, re.M):
            hits.append(f"{p.relative_to(SRC)}:{text[:m.start()].count(chr(10)) + 1}")
    assert hits == []


def _doc(task_type: str) -> dict:
    return {"suite_id": f"{task_type}-v1", "task_type": task_type, "version": 1, "tasks": [
        {"id": "n-01", "prompt": "say hi", "reference": "hi", "grader": {"kind": "exact", "expect": "hi"}, "source": {"author": "project", "license": "project license (pending)"}}]}


def test_a_suite_for_a_type_the_code_never_heard_of_loads(tmp_path):
    d = tmp_path / "novel-v1"
    d.mkdir()
    (d / "suite.yaml").write_text(yaml.safe_dump(_doc("zz-novel-type")), encoding="utf-8")
    s = load_suite("zz-novel-type", root=tmp_path)
    assert s.task_type == "zz-novel-type" and s.tasks[0].id == "n-01"


@pytest.mark.parametrize("bad", ["", "Has Space", "UPPER", "a" * 40, "x/y", "-lead"])
def test_a_task_type_name_that_is_not_a_plain_slug_is_refused(bad):
    with pytest.raises(SuiteRefused) as e:
        parse_suite(_doc(bad))
    assert e.value.code == "suite_invalid"


def test_environment_run_parameters_take_any_slug_type_and_refuse_a_malformed_one():
    base = {"environment_id": "llamacpp-local", "task_type": "summarize", "sensitivity": "public", "mode": "chat", "workspace_id": "w1", "recipe_sha256": "0" * 64, "input_text": "x"}
    assert T.validate_params("environment_run", base).task_type == "summarize"
    with pytest.raises(ValueError):
        T.validate_params("environment_run", {**base, "task_type": "Not A Slug"})


def test_the_shipped_data_is_consistent_every_catalog_type_has_a_suite_and_a_mode_and_every_suite_is_served():
    served = {t for e in ENVS.values() for t in e["task_types"]}
    suite_types = {yaml.safe_load((DATA_ROOT / s / "suite.yaml").read_text(encoding="utf-8"))["task_type"] for s in SUITES}
    assert served == suite_types, f"catalog types without a suite or suites nobody serves: {served ^ suite_types}"
    for e in ENVS.values():
        for t in e["task_types"]:
            assert E.mode_of(e, t) is not None, f"{e['id']} declares {t} but has no mode for it"


@pytest.mark.parametrize("name", SUITES)
def test_every_suite_has_ten_code_graded_tasks_and_every_non_sandbox_reference_passes_its_grader(name):
    s = load_suite(yaml.safe_load((DATA_ROOT / name / "suite.yaml").read_text(encoding="utf-8"))["task_type"])
    assert len(s.tasks) == 10 and s.counted == 10
    for t in s.tasks:
        if t.grader_kind != "sandbox_tests":
            assert grade(t, t.reference).passed, f"{t.id}: the reference does not pass its own grader"
            assert not grade(t, "no").passed, f"{t.id}: a wrong reply passes"


def test_every_sandbox_reference_of_every_suite_passes_in_the_real_sandbox_and_a_stub_fails():  # LINUX-CONTAINER
    from envrec.skills import sbx
    if not sbx.docker_ready():
        pytest.skip("no sandbox backend ready here: not measured")
    ran = 0
    for name in SUITES:
        s = load_suite(yaml.safe_load((DATA_ROOT / name / "suite.yaml").read_text(encoding="utf-8"))["task_type"])
        for t in (t for t in s.tasks if t.grader_kind == "sandbox_tests"):
            assert grade(t, "```python\n" + t.reference + "\n```").passed, t.id
            fn = re.search(r"def (\w+)", t.reference).group(1)
            assert not grade(t, f"```python\ndef {fn}(*a, **k):\n    return None\n```").passed, f"{t.id}: a stub passes"
            ran += 1
    assert ran >= 16  # code-v1 (10) and numeric-analysis-v1 (6)


def test_the_router_routes_each_shipped_new_type_and_a_type_added_only_to_the_catalog_data():
    import copy

    from envrec_coordinator.router import DictFacts, Router
    from tests.test_router import machine, policy, task
    envs = copy.deepcopy(ENVS)
    for e in envs.values():  # a type that exists nowhere in this repository: one catalog row per environment, nothing in code
        e["task_types"].append("zz-novel-type")
        e["adapter"]["modes"]["zz-novel-type"] = copy.deepcopy(e["adapter"]["modes"]["text"])
    for t in ("summarize", "extraction", "data-cleaning", "numeric-analysis", "zz-novel-type"):
        d = Router(envs=envs, facts=DictFacts([machine()]), policy=policy()).route(task(ttype=t, sens="public", chat="local"))
        assert d.route.environment_id == "llamacpp-local" and d.rule_trace[0].step == "sensitivity", t
    from envrec_coordinator.router import Refusal
    out = Router(envs=envs, facts=DictFacts([machine()]), policy=policy()).route(task(ttype="transcribe-audio", sens="public", chat="local"))
    assert isinstance(out, Refusal) and out.code == "no_candidate" and out.details["why"] == "unknown_task_type"
