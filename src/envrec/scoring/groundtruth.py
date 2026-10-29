"""Ground-truth files: hand-written (or hand-confirmed) expected values per fact key."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field, JsonValue, field_validator

from envrec.contracts import check_key
from envrec.contracts.base import Strict


class GTField(Strict):
    value: JsonValue = None
    # True when the honest answer is 'unknown': claiming a value for it is wrong.
    unknown: bool = False
    source: str = ""
    confirmed: bool = False
    rel_tolerance: float | None = Field(default=None, ge=0.0, le=1.0)


class GroundTruth(Strict):
    machine: str
    confirmed: bool = False
    facts: dict[str, GTField]

    @field_validator("facts")
    @classmethod
    def _keys_in_namespace(cls, facts: dict[str, GTField]) -> dict[str, GTField]:
        for key in facts:
            check_key(key)
        return facts

    @property
    def fully_confirmed(self) -> bool:
        return self.confirmed and all(f.confirmed for f in self.facts.values())


def load_ground_truth(path: Path | str) -> GroundTruth:
    return GroundTruth.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
