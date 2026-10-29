"""Catalog generator: reads official release metadata of the APPROVED sources only and writes the lockfile.

Metadata access is injected (`MetadataSource`), so tests run on fixtures and the live client can be held to a host
allowlist. Nothing is downloaded here: only listings. Every entry that cannot be verified is excluded with a reason.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from envrec.catalog.model import Lock, ModelFile, RuntimeAsset, parse_params_b, parse_quant
from envrec_worker.runtime.spec import SUPPORTED_KINDS
from envrec.runtime.manifest import Manifest

SHARD = re.compile(r"-\d{5}-of-\d{5}", re.I)


class MetadataSource(Protocol):
    def releases(self, repo_url: str) -> list[dict[str, Any]]:
        """Newest first. Each: {tag, commit, prerelease, assets: [{name, url, sha256|None, bytes}]}"""

    def model_repos(self, author: str) -> list[dict[str, Any]]:
        """Each: {repo, license|None}"""

    def repo_files(self, repo: str) -> dict[str, Any]:
        """{revision, license, files: [{path, bytes, sha256|None}]}"""


@dataclass
class Generated:
    lock: Lock
    excluded_md: str


def _gib(n: int) -> float:
    return n / 2**30


def generate(manifest: Manifest, meta: MetadataSource) -> Generated:
    manifest.require_approved()
    raw = manifest.raw
    allow = {x.lower() for x in manifest.license_allowlist}
    max_bytes = int(float(raw.get("max_artifact_gb", 3.0)) * 1e9)
    max_params = float(raw.get("max_model_params_b", 8))
    quants = {q.upper() for q in raw.get("quantizations", [])}
    excluded: list[dict[str, str]] = []

    def exclude(what: str, why: str) -> None:
        excluded.append({"what": what, "reason": why})

    for s in raw["runtime_sources"] + raw["model_sources"]:
        if s.get("status") in ("exclude", "propose-off"):
            exclude(f"source {s['id']}", f"status is {s['status']}: {s.get('reason_off') or s.get('reason') or s.get('seen', '')}"[:300])

    # ---- runtimes ----------------------------------------------------------------------------------
    assets: list[RuntimeAsset] = []
    formats_of: dict[str, list[str]] = {}
    for src in raw["runtime_sources"]:
        if src.get("status") != "approve":
            continue
        if src["license"].lower() not in allow:
            exclude(f"runtime {src['id']}", f"license {src['license']} is not in the allowlist")
            continue
        formats_of[src["id"]] = list(src["formats"])
        tag_re, want_pre = re.compile(src["channel"]["tag_pattern"]), bool(src["channel"].get("include_prerelease"))
        rels = [r for r in meta.releases(src["repo"]) if tag_re.match(r["tag"]) and (want_pre or not r["prerelease"])]
        patterns = [(a, re.compile(a["pattern"])) for a in src["assets"]]
        # newest release that carries at least one wanted asset
        rel = next((r for r in rels if any(p.match(x["name"]) for _, p in patterns for x in r["assets"])), None)
        if rel is None:
            exclude(f"runtime {src['id']}", "no release in the channel carries any of the wanted assets")
            continue
        for spec, pat in patterns:
            hits = [x for x in rel["assets"] if pat.match(x["name"])]
            if len(hits) != 1:
                exclude(f"{src['id']} {spec['os']}/{spec['arch']}/{spec['backend']} @ {rel['tag']}",
                        "not found in the release" if not hits else f"pattern matched {len(hits)} assets, expected exactly 1")
                continue
            a = hits[0]
            if not a.get("sha256"):
                exclude(f"{src['id']} {a['name']}", "the source gives no checksum for this asset")
                continue
            if a["bytes"] > max_bytes:
                exclude(f"{src['id']} {a['name']}", f"{a['bytes'] / 1e9:.2f} GB is over the {max_bytes / 1e9:.1f} GB artifact cap")
                continue
            assets.append(RuntimeAsset(
                id=f"{src['id']}:{rel['tag']}:{spec['os']}/{spec['arch']}:{spec['backend']}", source=src["id"], tag=rel["tag"],
                commit=rel.get("commit"), os=spec["os"], arch=spec["arch"], backend=spec["backend"], file=a["name"], url=a["url"],
                sha256=a["sha256"], bytes=a["bytes"], license=src["license"], license_source=src["license_source"],
                formats=list(src["formats"]), server_binary=src.get("server_binary", ""),
                runnable=src["id"] in SUPPORTED_KINDS))

    # ---- models --------------------------------------------------------------------------------------
    files: list[ModelFile] = []
    for src in raw["model_sources"]:
        if src.get("status") != "approve":
            continue
        for repo in meta.model_repos(src["author"]):
            name = repo["repo"]
            lic = (repo.get("license") or "").lower()
            if not lic:
                exclude(name, "no license stated on the model page")
                continue
            if lic not in allow:
                exclude(name, f"license {lic!r} is not in the allowlist")
                continue
            info = meta.repo_files(name)
            for f in info["files"]:
                path = f["path"]
                if not path.lower().endswith(".gguf"):
                    continue
                q = parse_quant(path)
                if q is None or q not in quants:
                    continue
                if SHARD.search(path):
                    exclude(f"{name}/{path}", "multi-file (sharded) model: not supported")
                    continue
                p = parse_params_b(path) or parse_params_b(name)
                if p is None:
                    exclude(f"{name}/{path}", "parameter count not readable from the name")
                    continue
                if p > max_params:
                    exclude(f"{name}/{path}", f"{p:g}B parameters is over the {max_params:g}B cap")
                    continue
                if not f.get("sha256"):
                    exclude(f"{name}/{path}", "the source gives no checksum for this file")
                    continue
                if f["bytes"] > max_bytes:
                    exclude(f"{name}/{path}", f"{f['bytes'] / 1e9:.2f} GB is over the {max_bytes / 1e9:.1f} GB artifact cap")
                    continue
                files.append(ModelFile(
                    id=f"{name}/{path}", source=src["id"], repo=name, file=path,
                    url=f"https://huggingface.co/{name}/resolve/{info['revision']}/{path}", revision=info["revision"],
                    sha256=f["sha256"], bytes=f["bytes"], license=lic, license_source=f"https://huggingface.co/{name}",
                    format="gguf", quantization=q, params_b=p, min_ram_gib=round(_gib(f["bytes"]) * 1.25 + 0.3, 2)))

    # ---- combinations: computed from the compatibility matrix, never written by hand -------------------
    assets.sort(key=lambda a: a.id)
    files.sort(key=lambda m: m.id)
    pairs = [(a.id, m.id) for a in assets for m in files if m.format in formats_of.get(a.source, [])]
    lock = Lock(manifest_sha256=hashlib.sha256(Path(manifest.path).read_bytes()).hexdigest() if manifest.path else "",
                runtime_assets=assets, model_files=files, pairs=pairs, excluded=excluded,
                counts={"runtime_assets": len(assets), "model_files": len(files), "combinations": len(pairs), "excluded": len(excluded)})
    return Generated(lock, render_excluded(excluded))


def render_excluded(excluded: list[dict[str, str]]) -> str:
    lines = ["# catalog.excluded", "", f"{len(excluded)} things were left out of the catalog automatically. Reason for each:", ""]
    lines += [f"- **{e['what']}**: {e['reason']}" for e in excluded] or ["- nothing"]
    return "\n".join(lines) + "\n"


def write(gen: Generated, lock_path: Path | str, excluded_path: Path | str) -> None:
    # JSON is valid YAML; a guard test keeps YAML writers out of every module but enroll/.
    Path(lock_path).write_text(json.dumps(gen.lock.model_dump(mode="json"), indent=1) + "\n", encoding="utf-8")
    Path(excluded_path).write_text(gen.excluded_md, encoding="utf-8")
