"""Turn a ValidationError into one short line a model can act on."""

from __future__ import annotations

from pydantic import ValidationError


def readable_error(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"]) or "(whole object)"
        msg = err["msg"].removeprefix("Value error, ")
        lines.append(f"{where}: {msg}")
    return "; ".join(lines)
