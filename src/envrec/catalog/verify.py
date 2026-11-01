"""Re-check a catalog lockfile against verifiable metadata, with the rules of `model-catalog-rules`.

For each model file: the hub's listing at the PINNED revision (size, SHA-256), the repo's own info (gated, license on the card, tags), and the GGUF header read by range request (architecture, type, parameter
count from the tensor table, context, layers, license, chat template). The rules decide what the file is, whether it is a catalog entry at all, and what it is labeled. Nothing is downloaded beyond headers.

`verify_lock` returns a new lock dict and a report of every field that changed. It does not write: the caller does (`scripts/catalog_verify.py`)."""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import httpx

from envrec.catalog.gguf import GgufError, facts_from_header, read_header
from envrec.catalog.hub import Hub, HubError
from envrec.catalog.rules import DECLARED, VERIFIED, Rules, derive_row, estimate_memory

_PROJECTOR = re.compile(r"(?i)mmproj")


def rules_digest(rules: Rules) -> str:
    return hashlib.sha256(json.dumps(rules.data, sort_keys=True).encode()).hexdigest()


def fetch_runtime_arches(assets: list[dict[str, Any]], rules: Rules | None = None, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """The architectures each pinned runtime commit can load, read from the runtime's own source (where, and how to read it, is data: `runtime_sources` in rules.json). Verified-by-code."""
    sources = (rules or Rules.load()).data.get("runtime_sources", {})
    c = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
    out: dict[str, Any] = {}
    for a in assets:
        src = sources.get(a["source"])
        if not src or not a.get("commit") or a["tag"] in out.get(a["source"], {}):
            continue
        url = src["table_url"].format(commit=a["commit"])
        if (httpx.URL(url).host or "") != src["host"]:
            raise HubError(f"{url} is not on the host this runtime's rules name ({src['host']})")
        r = c.get(url, follow_redirects=False)
        if r.status_code != 200:
            raise HubError(f"HTTP {r.status_code} for {url}")
        names = sorted(set(re.findall(src["pattern"], r.text)))
        if not names:
            raise HubError("the architecture table was not found in the runtime's source: its layout changed, the parser needs a new row")
        out.setdefault(a["source"], {})[a["tag"]] = {"commit": a["commit"], "architectures": names, "source": url, "label": VERIFIED}
    return out


def check_backend_evidence(assets: list[dict[str, Any]], rules: Rules, *, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """Each build's backend label, checked against the runtime's own release workflow at the pinned commit: the anchor line, then the needle within the lines that follow it. Verified-by-code or a named mismatch."""
    c = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
    out: list[dict[str, Any]] = []
    for name, src in rules.data.get("runtime_sources", {}).items():
        todo = [a for a in assets if a["source"] == name and a.get("commit")]
        if not todo or not src.get("backend_evidence"):
            continue
        url = src["workflow_url"].format(commit=todo[0]["commit"])
        if (httpx.URL(url).host or "") != src["host"]:
            raise HubError(f"{url} is not on the host this runtime's rules name ({src['host']})")
        r = c.get(url, follow_redirects=False)
        if r.status_code != 200:
            raise HubError(f"HTTP {r.status_code} for {url}")
        lines = r.text.splitlines()
        for ev in src["backend_evidence"]:
            at = next((i for i, ln in enumerate(lines) if ev["anchor"] in ln), None)
            ok = at is not None and any(ev["needle"] in ln for ln in lines[at:at + int(ev["within"]) + 1])
            labeled = any(a["os"] == ev["os"] and a["arch"] == ev["arch"] and a["backend"] == ev["backend"] for a in todo)
            out.append({"runtime": name, "os": ev["os"], "arch": ev["arch"], "backend": ev["backend"], "evidence_found": ok, "label_in_catalog": labeled, "source": url, "label": VERIFIED if ok and labeled else "MISMATCH"})
    return out


def _facts(row: dict[str, Any], info: dict[str, Any], tree: dict[str, dict[str, Any]], gg: dict[str, Any], rules: Rules, approved: list[str]) -> dict[str, Any]:
    shards = row.get("shards") or []
    parts = [s["file"] for s in shards] or [row["file"]]
    t = tree.get(row["file"], {})
    sizes = [tree.get(p, {}).get("size") for p in parts]
    # a split model is listed by the sum of its shards; its checksum is the first shard's, unless a later shard's own checksum disagrees with the listing (then that listing value is the one shown)
    bad = next((tree.get(sh["file"], {}).get("sha256") for sh in shards if tree.get(sh["file"], {}).get("sha256") != sh["sha256"]), None)
    ggufs = [v["size"] for p, v in tree.items() if p.lower().endswith(".gguf") and v.get("size")]
    largest = max(ggufs) if ggufs else None
    declared = row.get("params_b_declared", row.get("params_b"))
    f: dict[str, Any] = {
        "file.name": row["file"], "file.repo": row["repo"], "file.bytes": row["bytes"], "file.revision": row["revision"], "file.sha256": row["sha256"], "file.quantization": row.get("quantization"),
        "file.declared_params_b": declared if declared and declared > 0 else None,
        "file.size_ratio_to_repo_largest": (row["bytes"] / largest) if largest else None,
        "hub.author": info.get("author"), "hub.gated": info.get("gated"), "hub.pipeline_tag": info.get("pipeline_tag"), "hub.tags": info.get("tags", []), "hub.card_license": info.get("card_license"),
        "hub.base_models": info.get("base_models", []), "hub.listing_size": sum(sizes) if all(x is not None for x in sizes) else None,
        "hub.listing_sha256": bad if bad else t.get("sha256"),
        "repo.has_companion_projector": any(_PROJECTOR.search(p) and p.lower().endswith(".gguf") for p in tree),
        "repo.largest_gguf_bytes": largest, "policy.approved_publishers": approved,
    }
    f.update(gg)
    return {k: v for k, v in f.items() if v is not None}


def _header_facts(hub: Hub, row: dict[str, Any], markers: dict[str, list[str]], cache: Path | None) -> tuple[dict[str, Any], str | None]:
    out = cache / ("gguf__" + row["id"].replace("/", "__").replace("#", "_") + ".json") if cache else None
    if out and out.is_file():
        return json.loads(out.read_text()), None
    try:
        names = [s["file"] for s in row.get("shards") or []] or [row["file"]]
        heads = [read_header(hub.range_source(row["repo"], row["revision"], n)) for n in names]
    except (GgufError, HubError, httpx.HTTPError) as e:
        return {}, f"{type(e).__name__}: {e}"
    f = facts_from_header(heads[0], markers)  # the metadata is in the first shard; each shard holds its own tensors, so the counts are summed over all of them
    if len(heads) > 1:
        f["gguf.param_count"] = sum(h.param_count() for h in heads)
        f["gguf.tensor_count"] = sum(len(h.tensors) for h in heads)
        f["gguf.file_size"] = sum(h.file_size for h in heads)
        if "gguf.active_param_count" in f:
            f.pop("gguf.active_param_count")  # not derivable from one shard's tensors
    if out:
        out.write_text(json.dumps(f))
    return f, None


def verify_lock(lock: dict[str, Any], hub: Hub, rules: Rules, *, approved_publishers: list[str], runtime_arches: dict[str, Any] | None = None, checked_on: str,
                header_cache: Path | None = None, workers: int = 4, progress: Callable[[str], None] | None = None, max_params_b: float | None = None, backend_evidence: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    rows_in: list[dict[str, Any]] = lock["model_files"]
    markers = rules.data.get("template_markers", {})
    repos = sorted({(r["repo"], r["revision"]) for r in rows_in})
    infos: dict[tuple[str, str], dict[str, Any]] = {}
    trees: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    problems: list[dict[str, str]] = []
    for repo, rev in repos:
        try:
            infos[(repo, rev)], trees[(repo, rev)] = hub.repo_info(repo, rev), hub.tree(repo, rev)
        except HubError as e:
            problems.append({"what": f"{repo}@{rev[:8]}", "problem": str(e)})
    if header_cache:
        header_cache.mkdir(parents=True, exist_ok=True)

    def one(r: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        out = _header_facts(hub, r, markers, header_cache)
        if progress:
            progress(r["id"])
        return out

    todo = [r for r in rows_in if (r["repo"], r["revision"]) in infos]
    with ThreadPoolExecutor(workers) as ex:
        headers = list(ex.map(one, todo))
    kept: list[dict[str, Any]] = []
    r_companions = {r["id"]: r.get("companions", []) for r in rows_in}
    excluded = list(lock.get("excluded", []))
    changes: list[dict[str, Any]] = []
    companions: dict[tuple[str, str], list[dict[str, Any]]] = {}
    removed_ids: set[str] = set()
    for r, (gg, err) in zip(todo, headers):
        if err:
            problems.append({"what": r["id"], "problem": err})
        key = (r["repo"], r["revision"])
        facts = _facts({**r, "params_b_declared": r["params_b"]}, infos[key], trees[key], gg, rules, approved_publishers)
        v = derive_row(facts, rules)
        if max_params_b and v.get("params_b") and v["params_b"] > max_params_b:  # the manifest's cap, judged on the REAL parameter count, not on a number in a name
            v["excluded"] = [*v["excluded"], {"rule": "manifest:max_model_params_b", "reason": f"{v['params_b']:g}B parameters (read from the header) is over the {max_params_b:g}B cap"}]
        if v["excluded"]:
            for x in v["excluded"]:
                excluded.append({"what": r["id"], "reason": x["reason"], "rule": x["rule"]})
            removed_ids.add(r["id"])
            changes.append({"id": r["id"], "change": "excluded", "rules": [x["rule"] for x in v["excluded"]]})
            continue
        if v["companion"]:
            companions.setdefault(key, []).append({"file": r["file"], "url": r["url"], "sha256": r["sha256"], "bytes": v["bytes"] or r["bytes"], "kind": v["kind"], "revision": r["revision"]})
            removed_ids.add(r["id"])
            changes.append({"id": r["id"], "change": "companion", "rules": ["R1.1-projector"]})
            continue
        new = {**r}
        v["conflicts"] = list({(c["field"], c["declared"], c["verified"]): c for c in [*r.get("conflicts", []), *v["conflicts"]]}.values())  # a re-check never erases what an earlier one found
        for fld in ("kind", "params_b", "params_active_b", "bytes", "architecture", "size_label", "dims", "context_trained", "context_extended", "publisher", "base_model", "gated", "license_header",
                    "license_card", "license_verified", "license", "license_name", "license_class", "license_label", "license_basis", "labels", "requires_task", "offer_by_default", "not_default_reasons", "conflicts"):
            if fld in v and v[fld] is not None:
                new[fld] = v[fld]
        new["verified"] = {"label": VERIFIED, "checked": checked_on, "revision": r["revision"], "sources": ["hub listing at the pinned revision", "GGUF header (range request)"], "rules": rules_digest(rules)[:16]}
        for fld in ("params_b", "bytes"):
            if new[fld] != r[fld]:
                changes.append({"id": r["id"], "change": fld, "was": r[fld], "now": new[fld]})
        kept.append(new)
    for new in kept:  # a projector belongs to the vision models of its own repo and revision, never to a text model
        if new["kind"] == "vision-language":
            new["companions"] = companions.get((new["repo"], new["revision"])) or r_companions.get(new["id"], [])  # none found this pass: a lock that was already checked keeps the projectors it attached then
    for key, comps in companions.items():
        if not any(k["kind"] == "vision-language" and (k["repo"], k["revision"]) == key for k in kept):
            for c in comps:
                excluded.append({"what": f"{key[0]}/{c['file']}", "reason": "a vision projector with no model of its repository in the catalog (the model file is over the size cap)", "rule": "R1.1-projector"})
    for k in kept:
        k["min_ram_gib"] = estimate_memory(k, rules)["total_gib"]
    kept.sort(key=lambda m: m["id"])
    ids = {k["id"] for k in kept}
    out = {**lock, "model_files": kept, "excluded": excluded, "pairs": [p for p in lock.get("pairs", []) if p[1] in ids]}
    out["counts"] = {**lock.get("counts", {}), "model_files": len(kept), "combinations": len(out["pairs"]), "excluded": len(excluded)}
    out["verified"] = {"label": VERIFIED, "checked": checked_on, "backend_evidence": backend_evidence or [], "files_checked": len(todo), "rules": rules_digest(rules)[:16], "sources": "hub listing and GGUF headers at the pinned revisions",
                       "runtime_arches": {k: sorted(v) for k, v in (runtime_arches or {}).items()}}
    report = {"checked": checked_on, "files": len(rows_in), "kept": len(kept), "excluded_now": sorted(removed_ids - {c['file'] for cs in companions.values() for c in cs}),
              "companions": {f"{k[0]}@{k[1][:8]}": [c["file"] for c in v] for k, v in companions.items()}, "changes": changes, "problems": problems,
              "not_default": [{"id": k["id"], "why": k["not_default_reasons"], "requires_task": list(k.get("requires_task", {}))} for k in kept if not k["offer_by_default"] or k.get("requires_task")]}
    return out, report
