"""V2: the mechanical checks on researched leads. Each reason is typed and found by CODE from the fields of the page and the README, never by the model; the thresholds are data the owner can change; the
queries have a cap, carry the qualifiers, and a repeated address is read from its stored capture. SIMULATED-LOCAL: pages are fake bytes behind an injected fetch (the REAL fetches are in the measurement)."""
from __future__ import annotations

import json
import shutil

import pytest
from test_research import SEARCH, TASK_TEXT, readme, refused, researcher

from envrec_coordinator import research as RS

OLD, NEW = "2023-01-01T00:00:00Z", "2026-10-01T00:00:00Z"


def repos(*rows):
    """rows: (name, license, archived, pushed, stars, topics)."""
    return json.dumps({"items": [{"full_name": n, "description": "d", "license": {"spdx_id": lic}, "html_url": f"https://github.com/{n}", "pushed_at": pushed, "archived": arch, "stargazers_count": stars, "topics": topics}
                                 for n, lic, arch, pushed, stars, topics in rows]}).encode()


PAGE = repos(("a/good", "MIT", False, NEW, 40, ["finance"]), ("a/noassert", "NOASSERTION", False, NEW, 40, []), ("a/gpl", "GPL-3.0", False, NEW, 40, []), ("a/archived", "MIT", True, NEW, 40, []),
             ("a/stale", "MIT", False, OLD, 40, []), ("a/unloved", "MIT", False, NEW, 2, []), ("a/offtopic", "Apache-2.0", False, NEW, 40, ["cooking"]))
URL = SEARCH + "finance+agent"


def fetched(tmp_path, pages=None, **kw):
    r, sent = researcher(tmp_path, pages=pages if pages is not None else {URL: PAGE}, **kw)
    return r, sent, r.fetch_source(URL)


def test_every_reason_is_typed_and_found_by_code_from_the_page(tmp_path):
    r, _, d = fetched(tmp_path, pages={URL: PAGE, "https://api.github.com/repos/a/offtopic/readme": readme("A recipe book for cooks.")})
    assert [i["checks"] for i in d["items"]] == [[], ["license_missing"], ["license_not_permissive"], ["archived"], ["stale"], ["below_min_stars"], []]  # what the view shows the model, from the same function
    for n, code in ((2, "license_missing"), (3, "license_not_permissive"), (4, "archived"), (5, "stale"), (6, "below_min_stars"), (7, "no_domain_in_readme")):
        assert refused(r.record_candidate, d["capture_ref"], n, "own_harness", "x").code == code
    ok = r.record_candidate(d["capture_ref"], 1, "own_harness", "fits")
    assert ok["checks"]["license"] == "MIT" and ok["checks"]["stars"] == 40 and ok["checks"]["domain_in_readme"] is True and len(ok["checks"]["readme_capture_sha256"]) == 64
    assert r.summary() == {"searches": 1, "requests": 3, "reused": 0, "attempted": 7, "recorded": 1, "rejected": {"license_missing": 1, "license_not_permissive": 1, "archived": 1, "stale": 1, "below_min_stars": 1, "no_domain_in_readme": 1}}
    assert r.pending()[d["capture_ref"]]["items_passing_checks"] == [1]  # the off-topic item passed what the page shows; its README failed it, and the state now says so
    lead = json.loads((tmp_path / "researched" / "environments.json").read_text())["environments"][0]
    assert lead["evidence_state"] == "hypothesis" and lead["research_checks"]["thresholds"] == {"max_age_days": 730, "min_stars": 10}  # recorded with the checks it passed, never the default


def test_a_readme_that_cannot_be_read_is_no_verdict_on_the_lead(tmp_path):
    r, sent = researcher(tmp_path, pages={URL: PAGE})
    d = r.fetch_source(URL)
    r.fetch = lambda url: (False, b"", "HTTP 404") if url.endswith("/readme") else (True, PAGE, "")
    assert refused(r.record_candidate, d["capture_ref"], 1, "own_harness", "x").code == "readme_unreadable"
    assert r.summary()["attempted"] == 0  # not counted as an invalid lead


def test_a_package_page_is_checked_on_what_it_carries(tmp_path):
    pypi = "https://pypi.org/pypi/fin-tool/json"
    body = json.dumps({"info": {"name": "fin-tool", "summary": "Tools for cooking", "license_expression": "MIT", "package_url": "https://pypi.org/project/fin-tool/", "version": "1"}}).encode()
    r, _ = researcher(tmp_path, pages={pypi: body})
    d = r.fetch_source(pypi)
    assert d["items"][0]["checks"] == []  # a package page has no star count and no push date: those checks do not apply to it
    assert refused(r.record_candidate, d["capture_ref"], 1, "provider", "x").code == "no_domain_in_description"


def test_the_thresholds_are_data_the_owner_can_change(tmp_path):
    th = RS.load_thresholds(RS.load_sources())
    assert th["min_stars"] == 10 and th["max_queries"] == 4 and th["max_age_days"] == 730 and "_note" not in th
    d = tmp_path / "researched"
    d.mkdir()
    (d / "thresholds.json").write_text(json.dumps({"min_stars": 1, "max_age_days": 2000, "bogus": 5, "max_queries": -3, "readme_min_tags": "x"}))
    mine = RS.load_thresholds(RS.load_sources(), d)
    assert (mine["min_stars"], mine["max_age_days"], mine["max_queries"], mine["readme_min_tags"]) == (1, 2000, 4, 1) and "bogus" not in mine  # a bad value is ignored, a known one wins
    r, _, page = fetched(tmp_path)
    assert [i["checks"] for i in page["items"]][4:6] == [[], []]  # with those numbers an old repository and an unloved one pass


def test_the_code_adds_the_qualifiers_the_model_did_not_state_and_does_not_double_them(tmp_path):
    r, sent, _ = fetched(tmp_path)
    assert sent[0].split("?q=")[1] == "finance+agent+archived%3Afalse+pushed%3A%3E%3D2024-11-02+stars%3A%3E%3D10"
    r2, sent2 = researcher(tmp_path / "b", pages={SEARCH + "finance+stars%3A%3E5": PAGE})
    r2.fetch_source(SEARCH + "finance+stars%3A%3E5")
    assert "stars%3A%3E5" in sent2[0] and sent2[0].count("stars") == 1 and "archived%3Afalse" in sent2[0]  # the model's own stars qualifier stays


def test_searches_have_a_cap_and_a_repeated_address_is_read_from_its_capture(tmp_path):
    pages = {SEARCH + f"finance+agent+{w}": PAGE for w in ("tool", "cli", "mcp", "skill", "harness")}
    r, sent = researcher(tmp_path, pages=pages)
    for w in ("tool", "cli", "mcp", "skill"):
        r.fetch_source(SEARCH + f"finance+agent+{w}")
    assert r.fetch_source(SEARCH + "finance+agent+mcp")["reused"] is True  # the same address again: answered from its capture, not counted
    assert r.reused and len(sent) == 4 and r.queries == 4
    assert refused(r.fetch_source, SEARCH + "finance+agent+harness").code == "query_limit" and len(sent) == 4
    r2, sent2 = researcher(tmp_path, pages=pages)  # another run, same state folder, the same day: nothing is asked
    r2.fetch_source(SEARCH + "finance+agent+tool")
    assert sent2 == [] and r2.queries == 0 and len(r2.reused) == 1
    r3, sent3 = researcher(tmp_path, pages=pages)
    r3.today = "2026-11-05"  # older than thresholds.reuse_days: asked again
    r3.fetch_source(SEARCH + "finance+agent+tool")
    assert len(sent3) == 1


def test_the_search_shapes_come_from_the_data_and_each_passes_the_query_guard(tmp_path):
    r, _ = researcher(tmp_path, tags=("grant-writing", "finance"))
    shapes = r.query_shapes()
    assert shapes[:3] == ["topic:grant-writing", "\"awesome grant writing\"", "\"grant writing agent skills\" in:readme"] and len(shapes) == 6
    for q in shapes:
        r._check_query(q)  # raises Refused if the guard would turn it away
    assert "readme" not in r.page_forms() and set(r.page_forms()) == {"search", "repo", "pypi", "npm"}
    assert TASK_TEXT  # the task text is never part of a shape


def test_a_refusal_names_the_items_that_still_pass_and_a_rejected_item_is_marked(tmp_path):
    r, _, d = fetched(tmp_path, pages={URL: PAGE, "https://api.github.com/repos/a/offtopic/readme": readme("A recipe book for cooks.")})
    e = refused(r.record_candidate, d["capture_ref"], 7, "own_harness", "x")
    assert e.code == "no_domain_in_readme" and "pass the checks so far: [1]" in e.text
    assert r.pending()[d["capture_ref"]]["items_passing_checks"] == [1] and "no_domain_in_readme" in r.captures[d["capture_ref"]]["items"][6]["checks"]
    r.record_candidate(d["capture_ref"], 1, "own_harness", "fits")
    assert "pass the checks so far: [1]" in refused(r.record_candidate, d["capture_ref"], 2, "own_harness", "x").text  # a recorded one still passes; the listed numbers are facts of the state
