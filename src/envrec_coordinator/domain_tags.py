"""Domain tags of the shipped catalog entries, by code from what each entry's OWN source states (data: `skills/router-environments/references/domain_sources.json`). The source (a repository's API page, or a
documentation page) is fetched, kept by its hash, and its description and topics are read; a tag is written only when a data rule matches that text. An entry whose sources state no work domain keeps `domains: []`
and says so. `verify` re-reads the stored captures and checks that every tag of the entry follows from them."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Callable
from pathlib import Path

from envrec_coordinator.research import real_fetch
from envrec_os import SKILLS_DIR

SOURCES_FILE = SKILLS_DIR / "router-environments" / "references" / "domain_sources.json"
CAPTURES_DIR = SKILLS_DIR / "router-environments" / "references" / "domain_captures"


def load(path: Path | None = None) -> dict:
    return json.loads((path or SOURCES_FILE).read_text(encoding="utf-8"))


def page_text(body: bytes) -> dict[str, str]:
    """{'description': ..., 'topics': ...} from a repository API page (JSON) or the meta description of a documentation page (HTML)."""
    raw = body.decode("utf-8", "replace")
    if raw.lstrip().startswith("{"):
        d = json.loads(raw)
        return {"description": str(d.get("description") or ""), "topics": " ".join(t for t in d.get("topics", []) if isinstance(t, str))}
    m = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', raw) or re.search(r'<meta[^>]+content="([^"]*)"[^>]+name="description"', raw)
    return {"description": html.unescape(m.group(1)) if m else "", "topics": ""}


def tags_from(texts: list[dict[str, str]], rules: list[dict]) -> list[dict]:
    """The tags the rules give for these texts, each with the rule and where it matched. Order is the rules'."""
    out: dict[str, dict] = {}
    for r in rules:
        for t in texts:
            where = [k for k in ("description", "topics") if r.get("in", k) == k and r["contains"].lower() in t[k].lower()]
            if where and r["tag"] not in out:
                out[r["tag"]] = {"tag": r["tag"], "rule": r["contains"], "in": where[0]}
    return list(out.values())


def build(entry_id: str, spec: dict, *, fetch: Callable[[str], tuple[bool, bytes, str]] = real_fetch, today: str, captures: Path = CAPTURES_DIR) -> dict:
    srcs, texts = [], []
    for url in spec["entries"][entry_id]:
        ok, body, detail = fetch(url)
        if not ok:
            raise RuntimeError(f"{entry_id}: {url} could not be read ({detail})")
        sha = hashlib.sha256(body).hexdigest()
        captures.mkdir(parents=True, exist_ok=True)
        (captures / f"{sha}.json").write_bytes(body)
        t = page_text(body)
        srcs.append({"url": url, "capture_sha256": sha, "description": t["description"][:240], "topics": t["topics"]})
        texts.append(t)
    got = tags_from(texts, spec["text_rules"])
    return {"domains": [g["tag"] for g in got], "domains_source": {"sources": srcs, "matched": got, "retrieved": today, **({} if got else {"states_no_domain": "its sources state no work domain that a rule of the data names"})}}


def verify(entry: dict, spec: dict, captures: Path = CAPTURES_DIR) -> tuple[bool, str]:
    ds = entry.get("domains_source")
    if not isinstance(ds, dict) or not ds.get("sources"):
        return False, "no domains_source"
    texts = []
    for s in ds["sources"]:
        try:
            body = (captures / f"{s['capture_sha256']}.json").read_bytes()
        except OSError:
            return False, "a capture is not stored"
        if hashlib.sha256(body).hexdigest() != s["capture_sha256"]:
            return False, "a stored page does not match its hash"
        texts.append(page_text(body))
    want = [g["tag"] for g in tags_from(texts, spec["text_rules"])]
    return (True, "") if want == entry.get("domains", []) else (False, f"the sources give {want}, the entry says {entry.get('domains')}")
