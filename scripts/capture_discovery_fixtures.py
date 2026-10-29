#!/usr/bin/env python3
"""Capture real output of the discovery skills on THIS machine, redact it, and write committed fixtures.

  python scripts/capture_discovery_fixtures.py [adapter_id ...]

Raw captures go to runs/fixture_raw/ (gitignored). Redacted, size-limited copies go to fixtures/discovery/<skill>/<platform>/.
The redaction map is consistent across adapters in one run. A leak scan runs on every file written; a finding aborts the write.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from envrec.discovery import LocalCollector, Salts  # noqa: E402
from envrec.discovery.redact import Redactor, find_leaks  # noqa: E402
from envrec.discovery.types import Capture  # noqa: E402
from envrec.skills.discover import all_adapters  # noqa: E402
from envrec.skills.runner import collect_and_parse  # noqa: E402
from envrec.tools.ctx import ToolContext  # noqa: E402

RAW = ROOT / "runs" / "fixture_raw"
OUT = ROOT / "fixtures" / "discovery"


class Recording(LocalCollector):
    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.dir = directory
        directory.mkdir(parents=True, exist_ok=True)

    def _save(self, c: Capture) -> Capture:
        (self.dir / f"{c.name}.out").write_bytes(c.data)
        if c.exit_code not in (None, 0):
            (self.dir / f"{c.name}.exit").write_text(str(c.exit_code))
        elif c.error is not None:
            (self.dir / f"{c.name}.exit").write_text("1")
        return c

    def run(self, cmd):
        return self._save(super().run(cmd))

    def read_file(self, name, path):
        c = super().read_file(name, path)
        return c if name.startswith("mcp_") else self._save(c)  # raw MCP configs hold tokens: never written, only the reduced capture is

    def http_get(self, name, url, timeout_s=3.0):
        return self._save(super().http_get(name, url, timeout_s))


def shrink(name: str, text: str) -> str:
    if name == "sp_apps":
        d = json.loads(text)
        d["SPApplicationsDataType"] = d["SPApplicationsDataType"][:30]
        return json.dumps(d, indent=1, ensure_ascii=False)
    if name == "dev_list":
        return "\n".join(ln for ln in text.splitlines() if ln.startswith(("cu.", "tty.", "ttyUSB", "ttyACM"))) + "\n"
    if name in ("ps_comm", "lsof_tcp", "brew", "ls_apps") or name.startswith("ls_"):
        return "\n".join(text.splitlines()[:120]) + "\n"
    return text


def main() -> int:
    wanted = set(sys.argv[1:])
    red = Redactor()
    for a in all_adapters("macos"):
        if a.meta.ring not in (0, 1, 2) or a.meta.tier != "observe" or (wanted and a.id not in wanted):
            continue
        rec = Recording(RAW / a.id / "macos")
        for cap in collect_and_parse(a, ToolContext(collector=rec, platform="macos", salts=Salts(), params={"salt": "capture"}), {}).caps.values():
            if cap.name.startswith("mcp_") or a.id == "net_mdns":  # the tool's own (reduced or synthesized) capture is what gets recorded
                (rec.dir / f"{cap.name}.out").write_bytes(cap.data)
                (rec.dir / f"{cap.name}.src").write_text(cap.source)
        dest = OUT / a.id / "macos"
        dest.mkdir(parents=True, exist_ok=True)
        for f in sorted(rec.dir.iterdir()):
            if f.suffix == ".src":
                continue
            if f.suffix == ".exit":
                (dest / f.name).write_text(f.read_text())
                continue
            text = shrink(f.stem, f.read_text(errors="replace"))
            clean = red.text(text)
            bad = find_leaks(clean, red.terms)
            if bad:
                print(f"REFUSED {a.id}/{f.name}: {bad[:3]}")
                continue
            (dest / f.name).write_text(clean)
        (dest / "meta.json").write_text(json.dumps({"adapter": a.id, "platform": "macos", "synthetic": False, "source": "captured on a real Mac, redacted"}, indent=1))
        print("captured", a.id, len(list(dest.iterdir())), "files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
