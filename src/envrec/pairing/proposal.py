"""Unfamiliar hardware or a runtime outside the manifest: write a proposal for a human, change nothing, fetch nothing.

The research text comes from rung 4 (local documentation only) when a researcher is supplied; otherwise the proposal
says no research was available. The proposal is a plain file under proposals/. Only a human adding a source to the
manifest turns it into something the catalog can contain.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from envrec.catalog import Catalog
from envrec.pairing.profile import DeviceProfile


def write_proposal(p: DeviceProfile, cat: Catalog, reason: str, out_dir: Path | str, *, research: Callable[[str], str] | None = None,
                   requested_runtime: str | None = None) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    question = f"Which small-model runtime and model builds support {p.os}/{p.arch} with accelerator {p.accel}?"
    notes = research(question) if research else "no research function was supplied"
    covered = sorted({f"{a.os}/{a.arch}/{a.backend}" for a in cat.lock.runtime_assets})
    name = f"{p.signature_key}{'-' + requested_runtime if requested_runtime else ''}.md"
    path = out / name
    path.write_text("\n".join([
        f"# Proposal: {p.target_id} ({p.signature_key})", "",
        "**Status: proposal only. Nothing was downloaded, pushed or run for this.**", "",
        f"## Why this exists", reason, "",
        "## Device", *[f"- {k}: {v}" for k, v in {"os": p.os, "arch": p.arch, "ram_gib": p.ram_gib, "free_disk_gb": p.free_disk_gb,
                                                  "accelerator": p.accel, "cpu_threads": p.cpu_threads}.items()], "",
        "## What the catalog covers today", *[f"- {c}" for c in covered], "",
        "## Research (rung 4, local documentation only)", notes.strip() or "(empty)", "",
        "## To act on this, a human does the following",
        "1. Decide whether a source for this platform is acceptable (licence, official release channel).",
        "2. Add it to `config/runtime_manifest.yaml` with `status: approve`; the worker must also support that runtime kind.",
        "3. Regenerate the catalog with `python -m envrec catalog`.",
    ]) + "\n", encoding="utf-8")
    return path
