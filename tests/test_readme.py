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


def test_meta_models_claim_stays_planned_and_ai_assisted_line_is_kept():
    assert "Tests with Meta's models are planned." in README
    assert "AI-assisted development." in README
    assert "UNTESTED: no real call has been made" in README


def test_windows_installer_is_not_claimed_to_have_run():
    row = next(l for l in README.splitlines() if l.startswith("| Installer"))
    assert "never run on real Windows" in row


def test_third_party_tiles_are_only_for_verified_things():
    tiles = sorted(p.name for p in (ROOT / "assets" / "readme").glob("tile-*-dark.svg"))
    assert tiles == ["tile-claude-desktop-dark.svg", "tile-codex-dark.svg", "tile-linux-dark.svg", "tile-macos-dark.svg", "tile-meta-dark.svg", "tile-open-webui-dark.svg", "tile-windows-dark.svg"]
    for untested in ("meta", "codex"):
        assert "NOT TESTED YET" in (ROOT / "assets" / "readme" / f"tile-{untested}-dark.svg").read_text()
    assert "WORKS" not in (ROOT / "assets" / "readme" / "tile-open-webui-dark.svg").read_text().replace("CONNECTED ONCE", "")


def test_the_public_export_does_not_need_private_files():
    for private in ("CLAUDE.md", "PROGRESS.md", "PLAN.md", "ENVIRONMENT.md", "AUDIT_HARNESS_TRUTH"):
        assert private not in README


def test_meta_sentence_is_the_sourced_one():
    assert "the router never sends confidential tasks there" in README
    assert "improve its products" not in README
