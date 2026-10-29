"""Move the old per-run SQLite skills (a code blob and a stored test per signature) into the library as standard skill folders.

The signature and the verified-run count are kept. The old code runs unchanged inside the sandbox through a thin script: it was written against one recorded line
(`{"chunk": ...}` on stdin, facts as JSON on stdout), and the wrapper hands it exactly that."""

from __future__ import annotations

import json
import re
from pathlib import Path

from envrec.contracts import RawRef
from envrec.skills.lib import Library

SCRIPT = '''"""Migrated from a per-run skill. The original code is kept verbatim in _LEGACY and runs here, in the sandbox, over one recorded line."""
import io
import json
import re
import sys

from envrec_sbx import Caps, Out

_LEGACY = %s


def _banner(cap):
    try:
        d = json.loads(cap.text)
        if isinstance(d, dict) and isinstance(d.get("banner"), str):
            return d["banner"]
    except ValueError:
        pass
    return cap.text


def _quote(text):
    for seg in re.split(r"[^\\x20-\\x7e]+", text):
        seg = seg.replace('"', "").replace("\\\\", "").strip()
        if len(seg) >= 3:
            return seg[:120]
    return None


def parse(inp):
    caps = Caps(inp)
    out = Out(caps)
    cap = caps.get("probe")
    if cap is None or not cap.ok or not cap.text.strip():
        return out.lost(cap, *%s).result()
    text = _banner(cap)
    saved = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps({"chunk": text})), io.StringIO()
    try:
        exec(compile(_LEGACY, "legacy", "exec"), {"__name__": "__main__"})
        printed = sys.stdout.getvalue()
    except Exception:
        printed = ""
    finally:
        sys.stdin, sys.stdout = saved
    line = next((ln for ln in reversed(printed.splitlines()) if ln.strip().startswith("{")), None)
    try:
        facts = json.loads(line or "{}").get("facts", [])
    except ValueError:
        facts = []
    quote = _quote(text)
    for f in facts:
        if isinstance(f, dict) and isinstance(f.get("key"), str):
            out.fact(f["key"], f.get("value"), src="probe", quote=quote)
    return out.result()
'''


def slug(signature: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", signature.lower()).strip("-")[:48] or "skill"


def migrate_store_skills(store, library: Library) -> list[Path]:
    """One folder per stored skill: `legacy-<signature>`. Skills already migrated are left alone. Returns the folders written."""
    written: list[Path] = []
    for sk in store.all_skills():
        name = f"legacy-{slug(sk.signature)}"
        dest = library.root / name
        if dest.exists():
            continue
        code = store.read_raw(RawRef(path=sk.code_ref, sha256=sk.code_ref.rsplit("/", 1)[-1])).decode()
        try:
            tests = json.loads(store.read_raw(RawRef(path=sk.tests_ref, sha256=sk.tests_ref.rsplit("/", 1)[-1])))
        except Exception:
            tests = {"chunks": [], "expected_keys": []}
        keys = sorted(tests.get("expected_keys", []))
        (dest / "scripts").mkdir(parents=True)
        (dest / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Use when a device with the signature {sk.signature} appears again and a probe for it was learned earlier.\n---\n\n"
            f"# {sk.signature}, learned earlier\n\nThis skill was learned on a previous run and moved here from the per-run store with its signature and "
            f"{sk.verified_runs} verified run(s). Its script reads one recorded line from the `probe` capture and reports {', '.join(keys) or 'its facts'}.\n\n"
            "On a device it has not passed its tests on, treat everything it reports as a hypothesis until `skills verify` has passed it there.\n", encoding="utf-8")
        (dest / "scripts" / "parse.py").write_text(SCRIPT % (json.dumps(code), json.dumps(keys)), encoding="utf-8")
        meta = {"provenance": "generated", "protocol": "serial", "tier": "read", "ring": 1, "platforms": ["macos", "linux"], "signature": {"signature": sk.signature},
                "produces": keys, "verified_runs": sk.verified_runs, "legacy_id": None,
                "plan": {"steps": [{"id": "probe", "tool": "serial_identify", "args": {"port": "{param.port}"}}], "script": "scripts/parse.py"}}
        (dest / "skill.meta.json").write_text(json.dumps(meta, indent=1))
        for i, chunk in enumerate(tests.get("chunks", [])[:3]):
            d = dest / "tests" / f"recorded-{i}"
            (d / "captures").mkdir(parents=True)
            (d / "captures" / "probe.txt").write_text(chunk)
            (d / "expect.json").write_text(json.dumps({"platform": "linux", "match": True, "fact_keys": keys}))
        neg = dest / "tests" / "unrelated"
        (neg / "captures").mkdir(parents=True)
        (neg / "captures" / "probe.txt").write_text("no such device\n")
        (neg / "expect.json").write_text(json.dumps({"platform": "linux", "match": False}))
        written.append(dest)
    return written
