"""The dependency list must install on a fresh machine: every entry is valid PEP 508, and `cryptography` resolves to exactly ONE series on each platform we name."""
from __future__ import annotations

import tomllib

import pytest
from packaging.requirements import Requirement

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

PLATFORMS = [  # (sys_platform, platform_machine, the series that platform must get)
    ("darwin", "arm64", ">=50,<51"), ("darwin", "x86_64", ">=48,<49"),
    ("win32", "AMD64", ">=50,<51"), ("win32", "ARM64", ">=46,<47"),
    ("linux", "x86_64", ">=50,<51"), ("linux", "aarch64", ">=50,<51"),
]


def test_every_dependency_and_extra_is_a_valid_pep_508_requirement():
    entries = PROJECT["dependencies"] + [r for extra in PROJECT["optional-dependencies"].values() for r in extra]
    assert len(entries) >= 8
    for entry in entries:
        Requirement(entry)  # raises InvalidRequirement on a marker such as `not (...)`, which a fresh install rejects


@pytest.mark.parametrize("system,machine,series", PLATFORMS)
def test_cryptography_resolves_to_exactly_one_series_on_each_platform(system, machine, series):
    env = {"sys_platform": system, "platform_machine": machine, "python_version": "3.13", "platform_system": {"darwin": "Darwin", "win32": "Windows", "linux": "Linux"}[system]}
    chosen = [r for r in map(Requirement, PROJECT["dependencies"]) if r.name == "cryptography" and (r.marker is None or r.marker.evaluate(env))]
    assert len(chosen) == 1, (system, machine, [str(r) for r in chosen])
    assert sorted(str(chosen[0].specifier).split(",")) == sorted(series.split(",")), (system, machine, str(chosen[0].specifier))


def test_the_markers_are_not_vacuous_a_wrong_expectation_would_fail():
    reqs = [r for r in map(Requirement, PROJECT["dependencies"]) if r.name == "cryptography"]
    assert len(reqs) == 3 and all(r.marker is not None for r in reqs)
    intel = {"sys_platform": "darwin", "platform_machine": "x86_64"}
    assert [r.marker.evaluate(intel) for r in reqs] == [False, True, False]
