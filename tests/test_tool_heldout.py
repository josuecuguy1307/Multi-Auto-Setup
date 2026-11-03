"""Held-out check of the tool installer (P1), FIXED BEFORE the third harness's data was written: a harness added as DATA ONLY (one catalog entry, zero code changes) must produce, for every operating
system and architecture, a plan whose first step downloads exactly the asset the release capture names with exactly the size and SHA-256 that capture reports, unpacks it into its own folder, runs it once, and
can be undone; an architecture the release has no asset for is refused with `unsupported_cell`. The expectations below come from the REAL release capture in fixtures/github_releases/goose.json
(fetched 2026-11-02 from api.github.com), not from the entry under test."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from envrec_coordinator import tool_plan as TP
from envrec_net import envrun as E

CAPTURE = json.loads((Path(__file__).parent / "fixtures" / "github_releases" / "goose.json").read_text())
ASSETS = {a["name"]: a for a in CAPTURE["assets"]}
PERMISSIVE = set(E.load_policy()["permissive_licenses"])
# (operating system family, architecture as the analysis states it) -> (asset name the capture holds, the program inside it)
CELLS = {
    ("macos", "arm64"): ("goose-aarch64-apple-darwin.tar.gz", "goose"),
    ("macos", "amd64"): ("goose-x86_64-apple-darwin.tar.gz", "goose"),
    ("linux", "arm64"): ("goose-aarch64-unknown-linux-musl.tar.gz", "goose"),
    ("linux", "amd64"): ("goose-x86_64-unknown-linux-musl.tar.gz", "goose"),
    ("windows", "amd64"): ("goose-x86_64-pc-windows-msvc.zip", "goose-package/goose.exe"),
}


def entry():
    return E.load_environments()["goose"]


@pytest.mark.parametrize("cell", sorted(CELLS))
def test_every_cell_plans_the_asset_the_release_capture_names(cell):
    asset, program = CELLS[cell]
    plan = TP.build_tool_plan(entry(), {"os": cell[0], "arch": cell[1], "disk": 200}, TP.load_tool_recipes(), PERMISSIVE, machine_id="m")
    first, second, third = plan.steps
    got = ASSETS[asset]
    assert (first.args["sha256"], first.args["bytes"], first.args["url"]) == (got["digest"].split(":", 1)[1], got["size"], got["browser_download_url"]) and first.bytes == got["size"]
    assert [s.op for s in plan.steps] == ["fetch_check", "unpack_archive", "verify_program"] and second.args["dest"].startswith("<mas>/tools/") and third.args["binary_path"].endswith("/" + program)
    assert all(s.undo and s.exact_command for s in plan.steps) and plan.plan_hash.startswith("sha256:") and third.args["argv"] == ["--version"]


def test_an_architecture_the_release_has_no_asset_for_is_refused_with_its_reason():
    with pytest.raises(TP.PlanRefused) as e:
        TP.build_tool_plan(entry(), {"os": "windows", "arch": "arm64", "disk": 200}, TP.load_tool_recipes(), PERMISSIVE, machine_id="m")
    assert e.value.reason == "unsupported_cell"


def test_the_entry_is_installable_licensed_and_routable_by_data_alone():
    e = entry()
    assert TP.install_state(e, PERMISSIVE) == ("installable", "") and e["license"]["spdx"] == "Apache-2.0" and e["upstream"]["release"] == CAPTURE["tag_name"]
    assert E.is_runnable(e) and set(e["task_types"]) <= {"code", "text", "summarize", "extraction", "data-cleaning", "numeric-analysis"} and e["adapter"]["modes"]["text"]["confinement"] == "unverified"
    assert all(m["argv"] and "{prompt}" in " ".join(m["argv"]) or e["adapter"]["prompt_via"] == "stdin" for m in e["adapter"]["modes"].values())
