"""The README's images are generated from the style tokens and every claim that can be checked from the tree is."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_images_match_the_generator():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "make_readme_assets.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_every_local_link_and_image_exists():
    targets = re.findall(r'(?:src|srcset|href)="([^"#:]+)"', README) + re.findall(r"\]\(([^)#:]+)\)", README)
    assert len(targets) > 20
    missing = [t for t in targets if not (ROOT / t).exists()]
    assert not missing


def test_no_license_badge_until_the_owner_chooses():
    assert "img.shields.io/badge/license" not in README and "img.shields.io/github/license" not in README
    assert "has not been chosen" in README


def test_public_readme_has_no_ai_assisted_or_not_tested_claims():
    assert "AI-assisted" not in README and "AI assisted" not in README
    assert "UNTESTED" not in README and "SIMULATED-LOCAL" not in README
    assert "Tests with Meta's models are planned." not in README


def test_windows_installer_has_no_status_claim():
    assert "| Installer" not in README and "never run on real Windows" not in README


def test_third_party_tiles_are_only_for_verified_things():
    tiles = sorted(p.name for p in (ROOT / "assets" / "readme").glob("tile-*-dark.svg"))
    assert tiles == ["tile-claude-desktop-dark.svg", "tile-linux-dark.svg", "tile-macos-dark.svg", "tile-open-webui-dark.svg", "tile-windows-dark.svg"]
    assert "tile-meta" not in README and "tile-codex" not in README
    assert "WORKS" not in (ROOT / "assets" / "readme" / "tile-open-webui-dark.svg").read_text().replace("CONNECTED ONCE", "")


def test_the_public_export_does_not_need_private_files():
    for private in ("CLAUDE.md", "PROGRESS.md", "PLAN.md", "ENVIRONMENT.md", "AUDIT_HARNESS_TRUTH"):
        assert private not in README


def test_meta_sentence_is_the_sourced_one():
    assert "Confidential never leaves your network" in README
    assert "improve its products" not in README
