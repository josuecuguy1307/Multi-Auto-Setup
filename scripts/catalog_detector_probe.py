"""Run the rules of `model-catalog-rules` on GGUF repositories that are NOT in the catalog (other publishers) and compare each NAME-based detector with signals that do not come from the name:
the hub's pipeline tag and tags, the header's own keys, and the files the repository lists. Read-only: hub listings and the first MiB of one file per repository.

    python scripts/catalog_detector_probe.py --cache DIR --out report.json

A "name-only firing" is a rule that fired while none of its non-name branches did; it is a false positive when an independent signal says the file is not what the name suggested."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from envrec.catalog.gguf import facts_from_header, read_header  # noqa: E402
from envrec.catalog.hub import Hub, HubError  # noqa: E402
from envrec.catalog.rules import Rules, classify, derive_row, holds  # noqa: E402

TERMS = ["embedding", "embed", "rerank", "vision", "vl", "llava", "whisper", "speech", "guard", "shield", "draft", "base", "thinking", "reasoning", "instruct", "coder", "chat", "audio", "omni", "moderation", "classifier",
         "tiny", "mini", "r1", "gemma", "llama", "phi", "smol", "falcon", "mistral", "deepseek", "yi"]
SKIP_AUTHORS = {"Qwen", "HuggingFaceTB", "ibm-granite", "microsoft", "mistralai", "allenai", "TinyLlama"}  # the catalog's own publishers: this probe is about everyone else


def search(client: httpx.Client, term: str, limit: int) -> list[str]:
    r = client.get("https://huggingface.co/api/models", params={"search": term, "filter": "gguf", "sort": "downloads", "direction": -1, "limit": limit})
    r.raise_for_status()
    return [m["id"] for m in r.json() if m["id"].split("/")[0] not in SKIP_AUTHORS]


def pick_file(tree: dict) -> str | None:
    ggufs = [p for p in tree if p.lower().endswith(".gguf") and "mmproj" not in p.lower() and "-of-" not in p.lower()]
    for q in ("q4_k_m", "q4_0", "q8_0", "q4_k_s", "q5_k_m", "iq4_xs", "q6_k", "q3_k_m"):
        hit = next((p for p in ggufs if q in p.lower()), None)
        if hit:
            return hit
    return min(ggufs, key=lambda p: tree[p].get("size") or 1 << 60) if ggufs else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-term", type=int, default=6)
    ap.add_argument("--max-repos", type=int, default=90)
    a = ap.parse_args()
    rules = Rules.load()
    cache = Path(a.cache)
    hub = Hub(cache_dir=cache)
    client = httpx.Client(timeout=30, headers={"user-agent": "mas-detector-probe/1"})
    repos: list[str] = []
    for t in TERMS:
        try:
            for r in search(client, t, a.per_term):
                if r not in repos:
                    repos.append(r)
        except httpx.HTTPError as e:
            print("search failed", t, e, file=sys.stderr)
        time.sleep(0.3)
    repos = repos[: a.max_repos]
    rows = []
    for repo in repos:
        try:
            meta = client.get(f"https://huggingface.co/api/models/{repo}")
            meta.raise_for_status()
            rev = meta.json()["sha"]
            info, tree = hub.repo_info(repo, rev), hub.tree(repo, rev)
            f = pick_file(tree)
            if not f:
                continue
            h = read_header(hub.range_source(repo, rev, f))
        except (HubError, httpx.HTTPError, Exception) as e:  # a repository that cannot be read is listed, not hidden
            rows.append({"repo": repo, "error": f"{type(e).__name__}: {str(e)[:100]}"})
            continue
        gg = facts_from_header(h, rules.data.get("template_markers", {}))
        facts = {"file.name": f, "file.repo": repo, "file.bytes": tree[f]["size"], "file.revision": rev, "file.sha256": tree[f]["sha256"] or "", "hub.author": info["author"], "hub.gated": info["gated"],
                 "hub.pipeline_tag": info["pipeline_tag"], "hub.tags": info["tags"], "hub.card_license": info["card_license"], "hub.base_models": info["base_models"], "hub.listing_size": tree[f]["size"],
                 "hub.listing_sha256": tree[f]["sha256"], "repo.has_companion_projector": any("mmproj" in p.lower() and p.lower().endswith(".gguf") for p in tree),
                 "repo.largest_gguf_bytes": max((v["size"] or 0) for p, v in tree.items() if p.lower().endswith(".gguf")), "file.size_ratio_to_repo_largest": 1.0, **gg}
        facts = {k: v for k, v in facts.items() if v is not None}
        dec = classify({**rules.policy_facts(), **facts}, rules)
        # which branches of each fired rule were NAME branches only
        name_only = []
        for r in rules.rules:
            if r["id"] not in dec.fired or "when" not in r:
                continue
            branches = r["when"].get("any") or r["when"].get("all", [{"x": 0}])[-1].get("any") or []
            names = [b for b in branches if b.get("fact") in ("file.name", "file.repo")]
            others = [b for b in branches if b.get("fact") not in ("file.name", "file.repo")]
            if names and holds({"any": names}, {**rules.policy_facts(), **facts}) and not (others and holds({"any": others}, {**rules.policy_facts(), **facts})):
                name_only.append(r["id"])
        keep = ("file.name", "file.repo", "hub.pipeline_tag", "hub.tags", "hub.card_license", "repo.has_companion_projector", "gguf.general.architecture", "gguf.general.finetune", "gguf.general.license", "gguf.arch.pooling_type",
                "gguf.has_chat_template", "gguf.template.thinking", "gguf.tensor_count", "gguf.param_count", "gguf.arch.vision.block_count")
        rows.append({"repo": repo, "facts": {k: v for k, v in facts.items() if k in keep}, "file": f, "pipeline_tag": info["pipeline_tag"], "tags": info["tags"][:12], "arch": gg.get("gguf.general.architecture"), "pooling": gg.get("gguf.arch.pooling_type"),
                     "has_template": gg.get("gguf.has_chat_template"), "thinking_template": gg.get("gguf.template.thinking"), "finetune": gg.get("gguf.general.finetune"),
                     "has_projector": facts["repo.has_companion_projector"], "vision_keys": any(k.startswith("gguf.arch.vision") or k.startswith("gguf.clip") for k in gg),
                     "fired": dec.fired, "kind": dec.kind, "name_only": name_only, "requires": list(dec.requires_task), "labels": [x["label"][:40] for x in dec.labels]})
    Path(a.out).write_text(json.dumps(rows, indent=1) + "\n")
    print(f"{len(rows)} repositories ({sum(1 for r in rows if 'error' in r)} unreadable)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
