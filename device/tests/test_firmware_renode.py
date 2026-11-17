"""P4-FW-RENODE (EMULATED): the same firmware in Renode, the second emulator of D-007. Not built yet: Renode is not installed on this host
(not available through winget; NEEDS_HUMAN #9). Until it is, this criterion stays FAIL on purpose: no skip, no stand-in."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


def renode_path() -> Path | None:
    for c in (os.environ.get("MAS_RENODE", ""), r"C:\Program Files\Renode\bin\Renode.exe", shutil.which("renode") or ""):
        if c and Path(c).exists():
            return Path(c)
    return None


def test_renode_is_installed():
    assert renode_path() is not None, "Renode not installed: P4-FW-RENODE is not done (NEEDS_HUMAN #9)"


def test_renode_transcripts_match_the_host_build():
    pytest.fail("not built: the Renode platform script and harness come after Renode is installed (D-018)")
