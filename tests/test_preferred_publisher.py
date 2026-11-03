"""The owner's preferred publisher (P6): a profile setting that reorders the local model options. The publishers come from the catalog's verified rows, the owner's choice for this release is DATA (the
profile's default), and no vendor or model name is in the code. SIMULATED-LOCAL."""
from __future__ import annotations

import copy
import pathlib

from envrec_coordinator import options as O
from envrec_coordinator.profile import Profile
from tests.test_options import CAT, WIN

REAL = O.load_catalog()


def titles(r):
    return [o["model"] for o in r["options"]]


def test_a_preferred_publisher_moves_its_models_up_among_level_options_and_nothing_else_moves():
    cat = copy.deepcopy(CAT)
    for row, base in zip(cat["model_files"], ("big-org/x", "small-org/y", "other-org/z", "small-org/w")):
        row["base_model"] = base
    plain = O.build_options(WIN, choice="local", catalog=copy.deepcopy(cat), evidence={"entries": []})
    last = plain["options"][-1]["publisher"]
    assert len(plain["options"]) >= 2 and plain["options"][0]["publisher"] != last
    pref = O.build_options(WIN, choice="local", catalog=copy.deepcopy(cat), evidence={"entries": []}, prefer_publisher=last.upper())  # any case
    assert pref["options"][0]["publisher"] == last and sorted(titles(pref)) == sorted(titles(plain))  # the same options, one moved up
    assert O.build_options(WIN, choice="local", catalog=copy.deepcopy(cat), evidence={"entries": []}, prefer_publisher="nobody")["options"] == plain["options"]  # a name no model has changes nothing
    # it never lifts an option over a better cost class or a measured one
    opts = [{"id": "a", "cost_class": "free_local", "origin": "catalog_runtime", "title": "a", "publisher": "other"}, {"id": "b", "cost_class": "subscription", "origin": "installed_cli", "title": "b", "publisher": "mine"}]
    assert [o["id"] for o in O.order_options(opts, "mine")] == ["a", "b"]
    measured = [{"id": "m", "cost_class": "free_local", "origin": "catalog_runtime", "title": "m", "publisher": "other", "measured": {"pass_rate": 0.9}}, {"id": "p", "cost_class": "free_local", "origin": "catalog_runtime", "title": "p", "publisher": "mine"}]
    assert [o["id"] for o in O.order_options(measured, "mine")] == ["m", "p"]


def test_the_catalogs_publishers_are_read_from_its_verified_rows_and_a_quantizers_name_is_not_one():
    pubs = O.publishers(REAL)
    assert "meta-llama" in pubs and "ibm-granite" in pubs and "bartowski" not in pubs and pubs == sorted(pubs)  # bartowski hosts the file, meta-llama made the model
    assert O.model_publisher({"repo": "bartowski/x", "publisher": "bartowski", "base_model": "meta-llama/Llama"}) == "meta-llama" and O.model_publisher({"repo": "Org/x"}) == "org"


def test_llama_comes_first_among_the_local_options_with_the_releases_default_and_not_without_it():
    facts = {**WIN, "machine.ram_total_gib": 16.0, "machine.ram_available_gib": 12.0, "machine.disk_free_gib": 200.0, "machine.os.name": "Windows 11", "machine.os.arch": "amd64"}
    plain = O.build_options(facts, choice="local", evidence={"entries": []})
    pref = O.build_options(facts, choice="local", evidence={"entries": []}, prefer_publisher="meta-llama")
    pos = lambda r: next(i for i, o in enumerate(r["options"], 1) if o.get("publisher") == "meta-llama")  # noqa: E731
    assert pos(plain) > 3 and pos(pref) == 1  # it was far down the list, and the owner's preference puts it first


def test_the_profile_asks_one_question_whose_options_come_from_the_catalog_and_whose_default_is_data(tmp_path):
    p = Profile(tmp_path / "p.json", dynamic={"model_publishers": lambda: [{"id": x, "label": x} for x in O.publishers(REAL)]})
    assert p.get("preferred_publisher") == "meta-llama"  # the owner's choice for this release, from profile.json
    ids = p.option_ids("preferred_publisher")
    assert "meta-llama" in ids and ids[-1] == "none"
    p.set("preferred_publisher", "ibm-granite")
    assert p.get("preferred_publisher") == "ibm-granite" and "preferred_publisher" in p.summary()["values"]
    for bad in ("not-a-publisher", ""):
        try:
            p.set("preferred_publisher", bad)
            raise AssertionError("accepted " + bad)
        except Exception as exc:  # noqa: BLE001
            assert "is one of" in str(exc)
    p.set("preferred_publisher", "none")
    assert p.get("preferred_publisher") == "none"


def test_no_vendor_or_model_name_is_in_the_code_that_orders_or_asks():
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "envrec_coordinator"
    for f in ("options.py", "profile.py", "profile_tools.py"):
        text = (src / f).read_text().lower()
        assert "meta-llama" not in text and "llama" not in text.replace("llama.cpp", "") and "granite" not in text, f
