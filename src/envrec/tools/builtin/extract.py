"""Extraction helpers a skill (or the model) can call on text it already holds: regex, JSON path, table parse, and the two pure sanitizers."""

from __future__ import annotations

import json
import re

from envrec.discovery.sanitize import clean_text, mask_ids
from envrec.discovery.types import Reason
from envrec.tools.ctx import ToolContext, ToolResult
from envrec.tools.registry import tool


@tool(tags=("extract",))
def regex_extract(ctx: ToolContext, text: str, pattern: str, multiline: bool = True) -> ToolResult:
    """Pull every match of one regular expression out of text.

    Args:
        text: the text to search.
        pattern: a regular expression; capture groups are returned.
        multiline: let ^ and $ match at each line.
    Returns:
        data.matches: a list of matched strings, or of group lists when the pattern has groups.
    Tier: observe
    Platforms: any
    Notes: pure function with a 200 000 character input cap and a 200 match cap.
    """
    try:
        rx = re.compile(pattern, re.M if multiline else 0)
    except re.error as exc:
        return ToolResult.fail(Reason.denied, f"bad pattern: {exc}")
    out = []
    for m in rx.finditer(text[:200_000]):
        out.append(list(m.groups()) if rx.groups else m.group(0))
        if len(out) >= 200:
            break
    return ToolResult(data={"matches": out})


@tool(tags=("extract",))
def json_path(ctx: ToolContext, text: str, path: str) -> ToolResult:
    """Read values out of JSON text with a simple path.

    Args:
        text: JSON text.
        path: a path such as $.a.b[0].c or $.items[*].name.
    Returns:
        data.values: the list of values the path selects.
    Tier: observe
    Platforms: any
    Notes: supports dots, [index] and [*] only; nothing is evaluated.
    """
    from envrec.tools.builtin._jsonpath import select
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return ToolResult.fail(Reason.denied, f"not JSON: {exc}")
    return ToolResult(data={"values": select(doc, path)})


@tool(tags=("extract",))
def parse_table(ctx: ToolContext, text: str, columns: list, separator: str = "") -> ToolResult:
    """Split lines of text into rows with named columns.

    Args:
        text: lines of text, one row per line.
        columns: the column names, in order; the last column takes the rest of the line.
        separator: the text between columns, or empty to split on whitespace.
    Returns:
        data.rows: a list of objects, one per non-empty line.
    Tier: observe
    Platforms: any
    Notes: pure function; at most 500 rows.
    """
    n = len(columns)
    rows = []
    for ln in text.splitlines()[:2000]:
        if not ln.strip():
            continue
        parts = ln.strip().split(separator or None, n - 1)
        if len(parts) == n:
            rows.append(dict(zip(columns, parts)))
        if len(rows) >= 500:
            break
    return ToolResult(data={"rows": rows})


@tool(tags=("extract", "privacy"))
def sanitize_text(ctx: ToolContext, text: str, cap: int = 80) -> ToolResult:
    """Clean an untrusted string: strip control characters and cap length.

    Args:
        text: the untrusted string.
        cap: the maximum length to keep.
    Returns:
        data.text: the cleaned string.
    Tier: observe
    Platforms: any
    Notes: the privacy gate applies the same cleaning to everything written as a fact.
    """
    return ToolResult(data={"text": clean_text(text, int(cap))})


@tool(tags=("extract", "privacy"))
def mask_identifiers(ctx: ToolContext, text: str) -> ToolResult:
    """Replace device identifiers hiding in a name with a placeholder.

    Args:
        text: a name that may contain a MAC address, a UUID or a long hexadecimal id.
    Returns:
        data.text: the text with each identifier replaced by <id>.
    Tier: observe
    Platforms: any
    Notes: the privacy gate applies the same masking to everything written as a fact.
    """
    return ToolResult(data={"text": mask_ids(text)})
