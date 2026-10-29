#!/usr/bin/env python3
"""Print the exact menu a model is offered at every state of the protocol for each kind of target, built by the loop's own code (nothing is run).

  python conformance/tools/menu_truth.py [kind ...]
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from envrec.contracts import Target, TargetKind  # noqa: E402
from envrec.discovery import LocalCollector, Salts  # noqa: E402
from envrec.discovery.types import this_platform  # noqa: E402
from envrec.loop.menu import build_menu  # noqa: E402
from envrec.protocols import ProtocolSet  # noqa: E402
from envrec.skills.lib import Library  # noqa: E402
from envrec.store import Store  # noqa: E402
from envrec.tools.bridge import RegistryAdapter  # noqa: E402

MENU_MAX = 6  # config/models.yaml muse-glimmer (the small profile)


def menus(kind: str, ring: int, authorized: bool) -> dict[str, list[str]]:
    lib = Library(root=ROOT / "skills").load()
    ps = ProtocolSet.from_library(lib)
    out: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory() as d, Store(Path(d) / "s") as st:
        t = st.put_target(Target(kind=TargetKind(kind), ring=ring, handle="audit", authorized=authorized), from_config=authorized)
        reg = RegistryAdapter(platform=this_platform(), collector=LocalCollector(), salts=Salts(), library=lib)
        run = ps.start(kind)
        if run is None:
            return {"(no protocol for this kind)": [e.name for e in build_menu([reg], t, st, 10_000)]}
        for name, state in run.defn.states.items():
            run.state = name
            allow = (lambda e, p=run: p.allows_entry(e.name, list(e.spec.tags), (lib.get(e.name[6:]).meta.protocol if e.name.startswith("skill.") and lib.get(e.name[6:]) else None)))
            full = build_menu([reg], t, st, 10_000, None, allow)
            out[name] = ([e.name for e in full[:MENU_MAX]], len(full))
    return out


def main(kinds: list[str]) -> int:
    lib = Library(root=ROOT / "skills").load()
    valid = {k.value for k in TargetKind}
    for p in lib.protocols():
        for k in p.meta.states.kinds:
            if k not in valid:
                print(f"NOTE protocol {p.name} is for kind {k!r}, which is not a TargetKind: the loop can never start it")
    for kind in kinds or ["host", "net_host", "ble", "serial", "usb", "phone", "sensor", "media", "remote", "door"]:
        for ring, auth in ((0, False),) if kind not in ("host", "ble", "media", "remote", "door") else (((0, False), (3, False)) if kind == "host" else ((1, True),) if kind in ("media", "door") else ((2, True),) if kind == "remote" else ((1, False),)):
            print(f"\n=== target kind={kind} ring={ring} authorized={auth} (menu_max={MENU_MAX})")
            for state, val in menus(kind, ring, auth).items():
                names, total = val if isinstance(val, tuple) else (val, len(val))
                print(f"  {state:10} -> {len(names)} shown of {total}: {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
