"""What is known about a combination for a task, and where it comes from (docs/RECOMMENDATION_SPEC.md sections 4 and 6). Pure: it reads the admission facts and the public evidence DATA
(`skills/setup-options/references/evidence.json`), and never invents a number.

Order, the most specific first: measured here (an admission fact valid for this exact combination) > public for the combination (the same environment and model on a public benchmark) > public for the
model only > none. Vendor-reported numbers and benchmarks on the data's excluded list are never used, and different levels are never averaged: the most specific one speaks alone."""

from __future__ import annotations

import datetime
import json
import time

from envrec_coordinator.router import strings
from envrec_os import SKILLS_DIR

EVIDENCE_FILE = SKILLS_DIR / "setup-options" / "references" / "evidence.json"
RANK = {"measured_here": 0, "public_combination": 1, "public_model_only": 2, "none": 3}
SOURCE_TIERS = ("1", "2", "3")  # vendor-reported ("vendor") is shown by the data and never used


def load(path=None) -> dict:
    p = path or EVIDENCE_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"entries": [], "excluded_benchmarks": []}


def _usable(e: dict, doc: dict) -> bool:
    excluded = {str(b.get("benchmark") if isinstance(b, dict) else b) for b in doc.get("excluded_benchmarks", [])}
    return str(e.get("source_tier")) in SOURCE_TIERS and e.get("benchmark") not in excluded and bool(e.get("source")) and bool(e.get("retrieved")) and bool(e.get("applies"))


def public_for(route, task_type: str, domains: list[str], doc: dict) -> dict | None:
    """The most specific usable public entry for this combination and this kind of work, else None. A combination match beats a model-only match; within a level the better source tier (1 before 3) wins."""
    best = None
    for e in doc.get("entries", []):
        if not _usable(e, doc):
            continue
        ap = e["applies"]
        if route.model_id != ap.get("model") and route.model_id not in (ap.get("models") or []):
            continue
        if not ({task_type} & set(e.get("task_types", [])) or set(domains) & set(e.get("domains", []))):
            continue
        level = "public_combination" if ap.get("environment") == route.environment_id and (not ap.get("access") or ap.get("access") == route.access_id) else "public_model_only" if not ap.get("environment") else None
        if level is None:
            continue
        key = (RANK[level], int(e["source_tier"]))
        if best is None or key < best[0]:
            best = (key, level, e)
    return None if best is None else {"tier": best[1], "entry": best[2]}


def age_warning(source_date: str, doc: dict, now: float | None = None) -> str:
    """The warning for a result whose source date is older than the data's `warn_after_days`; '' when it is not, or when the data states no age or the date cannot be read."""
    limit = doc.get("warn_after_days")
    try:
        today = datetime.date.fromtimestamp(now if now is not None else time.time())
        days = (today - datetime.date.fromisoformat(str(source_date)[:10])).days
    except (ValueError, TypeError):
        return ""
    return strings.EVIDENCE_OLD.format(date=str(source_date)[:10], days=days) if isinstance(limit, int) and days > limit else ""


def describe(route, task_type: str, domains: list[str], fact: dict | None, passed: tuple[int, int] | None, doc: dict, now: float | None = None) -> dict:
    """The evidence for the chosen combination as data: tier id, the owner-facing line `evidence: <tier>, <source>, <date>`, the source and the date. `fact` is the valid admission fact (or None)."""
    if passed is not None and fact is not None:
        when = time.strftime("%Y-%m-%d", time.gmtime(float(fact["measured_at"]))) if fact.get("measured_at") else "date not recorded"
        src = f"admission suite {fact.get('suite_id', 'unknown')}"
        tier = "measured_here"
        what = strings.EVIDENCE_MEASURED.format(passed=passed[0], of=passed[1])
        return {"tier": tier, "source": src, "date": when, "text": strings.EVIDENCE_LINE.format(tier=what, source=src, date=when)}
    pub = public_for(route, task_type, domains, doc)
    if pub is not None:
        e = pub["entry"]
        what = strings.EVIDENCE_TIER_TEXT[pub["tier"]] + f" (source tier {e['source_tier']}, {e.get('benchmark', 'benchmark')}: {e.get('result', 'no result stated')})"
        when = e.get("source_date") or e["retrieved"]  # the date the SOURCE published it, when the entry has one
        warn = age_warning(e["source_date"], doc, now) if e.get("source_date") else ""
        return {"tier": pub["tier"], "source": e["source"], "date": when, "warning": warn, "text": strings.EVIDENCE_LINE.format(tier=what, source=e["source"], date=when) + (" " + warn if warn else "")}
    return {"tier": "none", "source": "", "date": "", "text": strings.EVIDENCE_NONE_LINE}
