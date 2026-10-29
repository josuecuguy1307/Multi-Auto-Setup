"""A large neighbour table (a lab bridge, a VPN) must not push the skill over the sandbox's call cap: lookups and entities are budgeted, the count stays true."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    sys.path.insert(0, str(ROOT / "src" / "envrec" / "skills" / "sbxlib"))
    spec = importlib.util.spec_from_file_location("nn_parse", ROOT / "skills" / "net-neighbors" / "scripts" / "parse.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_table_of_a_thousand_neighbours_asks_for_at_most_the_budget_and_lists_at_most_the_cap():
    m = load()
    asked = []
    m._lk = lambda cid, tool, args: asked.append(cid) or {"name": "Vendor " + cid}
    rows = [{"ip": f"10.9.{i // 250}.{i % 250 + 1}", "mac": f"02:{i // 256:02x}:{i % 256:02x}:00:00:01", "iface": "bridge0"} for i in range(1000)]
    ents = m.neighbor_entities(rows, "arp")
    assert len(set(asked)) == m.LOOKUP_BUDGET == 60 and len(asked) <= m.ENTITY_BUDGET
    assert len(ents) == m.ENTITY_BUDGET == 400
    resolved = [e for e in ents if e.resolved]
    assert 0 < len(resolved) < len(ents) and all(e.attrs["vendor"] is None for e in ents if not e.resolved)


def test_prefixes_already_asked_about_are_reused_after_the_budget_is_reached():
    m = load()
    asked = []
    m._lk = lambda cid, tool, args: asked.append(cid) or {"name": "V"}
    rows = [{"ip": f"192.0.2.{i}", "mac": "aa:bb:cc:00:00:%02x" % i, "iface": "en0"} for i in range(1, 200)] + [{"ip": "192.0.2.250", "mac": "aa:bb:cc:ff:ff:ff", "iface": "en0"}]
    ents = m.neighbor_entities(rows, "arp")
    assert len(ents) == 200 and all(e.resolved for e in ents) and set(asked) == {"oui_aabbcc"}
