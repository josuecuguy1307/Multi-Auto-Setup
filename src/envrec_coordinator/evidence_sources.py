"""Public evidence from public sources, by code (data: `skills/setup-options/references/evidence_sources.json`). One generic extractor reads every source row, whatever its format (`hub_eval_results`: the Hub's
list of results for a model; `csv_table`: a leaderboard's own CSV; `json_grouped`: per-problem records averaged per model): it fetches the page (a row may raise ITS size cap, under the data's ceiling), keeps the
exact bytes by their hash, takes each number out of the stored page, drops what is vendor-reported or an aggregator (the row's own `drop` rules) and builds an evidence entry only for a benchmark the data
describes and a model the catalog has. An entry counts only while its number is still in its capture (`verify`). A model never writes a value. A source that cannot be read with a plain GET is not a row; it is
listed in `left_out` with the capture that shows why."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from envrec_coordinator.research import jget, real_fetch
from envrec_os import SKILLS_DIR

SOURCES_FILE = SKILLS_DIR / "setup-options" / "references" / "evidence_sources.json"
CAPTURES_DIR = SKILLS_DIR / "setup-options" / "references" / "evidence_captures"
SLUG = re.compile(r"[^a-z0-9]+")
Fetch = Callable[..., tuple[bool, bytes, str]]


def load_sources(path: Path | None = None) -> dict:
    return json.loads((path or SOURCES_FILE).read_text(encoding="utf-8"))


def route_ids(lock: dict, hub_id: str) -> list[str]:
    """The model ids the router sees for a public model: the file names of the catalog's model files whose base model it is."""
    return sorted({m["file"] for m in lock.get("model_files", []) if m.get("base_model") == hub_id})


def skipped(hub_id: str, spec: dict) -> bool:
    """A model used only to build or test this project (the data lists the words): it gets no evidence entry; the owner is told which."""
    return any(w.lower() in hub_id.lower() for w in spec.get("not_for_evidence", []))


def _dropped(row: dict, rules: list[dict], hub_id: str) -> str | None:
    for r in rules:
        v = str(row.get(r["field"]) or "")
        if "startswith" in r and v.startswith(r["startswith"].format(hub_id=hub_id)):
            return r["reason"]
        if "contains" in r and r["contains"].lower() in v.lower():
            return r["reason"]
    return None


def _number(x) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x) if not float(x).is_integer() else x
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*%?\s*", str(x or ""))
    return float(m.group(1)) if m else None


def rows_of(src: dict, hub_ids: list[str], body: bytes) -> tuple[list[dict], dict[str, int], dict]:
    """(rows that survive the drop rules, dropped counts by reason, facts about the page). Each row: hub_id, dataset, task, value, date, source_name, source_url, verified, notes."""
    fmt, keep, dropped, facts = src.get("format", "hub_eval_results"), [], {}, {}

    def drop(why: str) -> None:
        dropped[why] = dropped.get(why, 0) + 1
    if fmt == "hub_eval_results":
        doc = json.loads(body.decode("utf-8", "replace"))
        hub_id = hub_ids[0]
        for raw in jget(doc, src["rows"]) or []:
            row = {k: jget(raw, path) for k, path in src["fields"].items()} | {"hub_id": hub_id}
            row["value"] = row["value"] if isinstance(row.get("value"), (int, float)) and not isinstance(row.get("value"), bool) else None
            if row["value"] is None or not row.get("dataset"):
                drop("no readable number")
                continue
            why = _dropped(row, src["drop"], hub_id)
            if why:
                drop(why)
                continue
            keep.append(row)
    elif fmt == "csv_table":
        table = list(csv.DictReader(io.StringIO(body.decode("utf-8", "replace"))))
        facts["rows"], facts["names"] = len(table), [r.get(src["name_column"], "") for r in table]
        by = {n: h for n, h in src["names"].items() if h in hub_ids}
        for r in table:
            hub = by.get(r.get(src["name_column"], ""))
            v = _number(r.get(src["value_column"]))
            if hub and v is not None:
                keep.append({"hub_id": hub, "dataset": src["dataset"], "task": src["task"], "value": v, "date": src.get("date"), "source_name": src["source_name"], "source_url": src["source_url"], "verified": False,
                             "notes": f"{src['name_column']}: {r[src['name_column']]}; rank {r.get('Rank', '?')}"})
    elif fmt == "json_grouped":
        doc = json.loads(body.decode("utf-8", "replace"))
        recs = jget(doc, src["rows"]) or []
        models = jget(doc, src["models"]) or []
        facts["rows"], facts["names"] = len(recs), [m.get(src["model_name_field"], "") for m in models]
        link_to_hub = {str(m.get(src["link_field"], "")).rstrip("/").split(src["link_prefix"])[-1]: m[src["model_name_field"]] for m in models if src["link_prefix"] in str(m.get(src["link_field"], ""))}
        for hub in hub_ids:
            name = link_to_hub.get(hub)
            vals = [r[src["metric"]] for r in recs if name and r.get(src["group_by"]) == name and isinstance(r.get(src["metric"]), (int, float))]
            if vals:
                keep.append({"hub_id": hub, "dataset": src["dataset"], "task": src["task"], "value": round(sum(vals) / len(vals), 4), "date": src.get("date"), "source_name": src["source_name"], "source_url": src["source_url"],
                             "verified": False, "notes": f"mean of {len(vals)} problems"})
    else:
        raise ValueError(f"unknown source format {fmt!r}")
    return keep, dropped, facts


def tier_of(rules: list[dict], *, verified: bool, official: bool, source_url: str) -> str:
    host = (urlsplit(source_url).hostname or "").lower()
    facts = {"verified": verified, "official_benchmark": official}
    for r in rules:
        w = r["when"]
        if all(facts.get(k) == v for k, v in w.items() if k != "source_host_not") and ("source_host_not" not in w or host != w["source_host_not"]):
            return r["tier"]
    return "3"


def _store(body: bytes, captures: Path) -> str:
    sha = hashlib.sha256(body).hexdigest()
    captures.mkdir(parents=True, exist_ok=True)
    (captures / f"{sha}.json").write_bytes(body)
    return sha


def _date_from(src: dict, fetch: Fetch, requests: list[str]) -> str | None:
    d = src.get("date_from")
    if not d:
        return src.get("date")
    requests.append(d["url"])
    ok, body, _ = fetch(d["url"])
    m = re.search(d["regex"], body.decode("utf-8", "replace")) if ok else None
    return m.group(1) if m else None


def _get(fetch: Fetch, url: str, src: dict):
    return fetch(url, src["max_bytes"]) if src.get("max_bytes") else fetch(url)


def build(spec: dict, lock: dict, *, fetch: Fetch = real_fetch, today: str, captures: Path = CAPTURES_DIR, keep_big: Path | None = None) -> dict:
    """{entries, dropped, skipped, coverage, requests, models_skipped}: evidence entries from the data's sources for the catalog's models. `coverage` has, per source, what the capture held and which catalog
    models it matched (an empty match is a result: the capture proves the source has nothing for the catalog's models). `keep_big` is where a capture too large for the repository is stored."""
    hubs = {m["file"]: m["base_model"] for m in lock.get("model_files", []) if m.get("base_model")}
    all_hubs = sorted(set(hubs.values()))
    use = [h for h in all_hubs if not skipped(h, spec)]
    entries, dropped, notes, requests, cards, coverage, cards_body = [], {}, [], [], {}, [], {}

    def add_dropped(d: dict) -> None:
        for k, v in d.items():
            dropped[k] = dropped.get(k, 0) + v

    def entry_from(row: dict, src: dict, page_sha: str, card_sha: str | None, official: bool) -> dict:
        ids = route_ids(lock, row["hub_id"])
        tier = tier_of(spec["tier_rules"], verified=row.get("verified") is True, official=official, source_url=str(row.get("source_url") or ""))
        bench = spec["benchmarks"][row["dataset"]]
        slug = SLUG.sub("-", f"{row['dataset']}-{row.get('task') or 'all'}-{row['hub_id']}".lower()).strip("-")[:90]
        return {"id": slug, "applies": {"model": row["hub_id"], "models": ids}, "hub_id": row["hub_id"], "task_types": list(bench["task_types"]), "domains": list(bench["domains"]), "benchmark": f"{bench['label']} / {row.get('task') or 'all'}",
                "dataset": row["dataset"], "task": row.get("task"), "value": row["value"], "result": f"{row['value']}", "source": f"{row.get('source_name') or 'a source named on the Hub'} ({src['label'].format(hub_id=row['hub_id'])})", "source_url": str(row.get("source_url") or ""),
                "source_tier": tier, "official_benchmark": official, "hub_verified": row.get("verified") is True, "retrieved": today, "source_date": str(row.get("date") or "")[:10], "source_row": src["id"],
                "capture_sha256": page_sha, "card_capture_sha256": card_sha, "note": str(row.get("notes") or "")[:160]}
    for src in spec["sources"]:
        fmt = src.get("format", "hub_eval_results")
        if fmt == "hub_eval_results":
            for hub in use:
                url = src["url"].format(hub_id=hub)
                requests.append(url)
                ok, body, detail = _get(fetch, url, src)
                if not ok:
                    notes.append({"model": hub, "source": src["id"], "reason": f"the page could not be read ({detail or 'no answer'})"})
                    continue
                page_sha = hashlib.sha256(body).hexdigest()  # stored only if an entry comes from it (below)
                rows, drop, _ = rows_of(src, [hub], body)
                add_dropped(drop)
                for row in rows:
                    bench = spec["benchmarks"].get(row["dataset"])
                    if bench is None:
                        dropped[f"no benchmark described in the data: {row['dataset']}"] = dropped.get(f"no benchmark described in the data: {row['dataset']}", 0) + 1
                        continue
                    card_sha, tags = cards.get(row["dataset"], (None, []))
                    if card_sha is None:
                        curl = src["benchmark_card"]["url"].format(dataset=row["dataset"])
                        requests.append(curl)
                        cok, cbody, cdetail = _get(fetch, curl, src)
                        if not cok:
                            notes.append({"model": hub, "source": src["id"], "reason": f"the card of {row['dataset']} could not be read ({cdetail or 'no answer'})"})
                            continue
                        card_sha, tags = hashlib.sha256(cbody).hexdigest(), [t for t in (jget(json.loads(cbody.decode("utf-8", "replace")), src["benchmark_card"]["tags"]) or []) if isinstance(t, str)]
                        cards[row["dataset"]] = (card_sha, tags)
                        cards_body[row["dataset"]] = cbody
                    _store(body, captures)
                    (captures / f"{card_sha}.json").parent.mkdir(parents=True, exist_ok=True)
                    (captures / f"{card_sha}.json").write_bytes(cards_body[row["dataset"]])
                    entries.append(entry_from(row, src, page_sha, card_sha, src["benchmark_card"]["official_tag"] in tags))
            continue
        requests.append(src["url"])
        ok, body, detail = _get(fetch, src["url"], src)
        if not ok:
            notes.append({"source": src["id"], "reason": f"the page could not be read ({detail or 'no answer'})"})
            continue
        s2 = {**src, "date": _date_from(src, fetch, requests)}
        sha = hashlib.sha256(body).hexdigest()
        big = len(body) > int(spec.get("repo_capture_max_bytes", 1_000_000))
        if big:
            if keep_big is not None:
                _store(body, keep_big)
        else:
            _store(body, captures)
        rows, drop, facts = rows_of(s2, use, body)
        add_dropped(drop)
        for row in rows:
            entries.append(entry_from(row, s2, sha, None, bool(src.get("official_benchmark", True))))
        coverage.append({"source": src["id"], "url": src["url"], "capture_sha256": sha, "bytes": len(body), "kept_in_repository": not big, "rows": facts.get("rows"), "models_in_capture": facts.get("names"),
                         "catalog_models_matched": sorted({r["hub_id"] for r in rows}), "retrieved": today, "source_date": s2.get("date")})
    for lo in spec.get("left_out", []):
        for url in lo.get("proof_urls", []):
            requests.append(url)
            ok, body, detail = fetch(url)
            if ok:
                coverage.append({"source": lo["source"], "kind": "unavailable", "url": url, "capture_sha256": _store(body, captures), "bytes": len(body), "retrieved": today})
            else:
                notes.append({"source": lo["source"], "reason": f"the proof page could not be read ({detail or 'no answer'})"})
    return {"entries": entries, "dropped": dropped, "skipped": notes, "coverage": coverage, "requests": requests, "models_skipped": sorted(h for h in all_hubs if skipped(h, spec)), "models_used": use}


def verify(entry: dict, spec: dict, captures: Path = CAPTURES_DIR) -> tuple[bool, str]:
    """Is this entry's number in its capture? The page is read from the stored bytes (checked against their hash) and the row is found again by dataset, task and value."""
    try:
        body = (captures / f"{entry['capture_sha256']}.json").read_bytes()
    except OSError:
        return False, "the capture is not stored"
    if hashlib.sha256(body).hexdigest() != entry["capture_sha256"]:
        return False, "the stored page does not match its hash"
    src = next((s for s in spec["sources"] if s["id"] == entry.get("source_row", "hub_model_eval_results")), None)
    if src is None:
        return False, "the source row is gone from the data"
    rows, _, _ = rows_of({**src, "date": entry.get("source_date")}, [entry["hub_id"]], body)
    found = any(r["dataset"] == entry["dataset"] and r.get("task") == entry.get("task") and r["value"] == entry["value"] and r["hub_id"] == entry["hub_id"] and str(r.get("source_url") or "") == entry["source_url"] for r in rows)
    return (True, "") if found else (False, "the number is not in the capture")
