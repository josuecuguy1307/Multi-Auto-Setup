"""Write the brand assets in assets/brand/ from src/envrec_style/tokens.json: SVGs, PNGs (when rsvg-convert is installed) and gallery.html. The files are committed; tests/test_style.py fails if they drift from the tokens.

    python scripts/make_brand.py [--check]
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from envrec_style import gallery, svg  # noqa: E402

OUT = ROOT / "assets" / "brand"
LOGOS = {"logo-dark": "dark", "logo-transparent": "transparent", "logo-light": "light", "logo-light-alt": "light-b"}
PNGS = {"logo-dark": [1, 2], "logo-light": [1, 2], "logo-transparent": [1, 2]}  # 1x = 1000 px wide, 2x = 2000


def svgs() -> dict[str, str]:
    return {f"{name}.svg": svg.logo_svg(variant) for name, variant in LOGOS.items()} | {"gallery.html": gallery.gallery_html()}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    files = svgs()
    if "--check" in sys.argv:
        bad = [n for n, text in files.items() if not (OUT / n).is_file() or (OUT / n).read_text(encoding="utf-8") != text]
        print("drift: " + ", ".join(bad) if bad else "assets match the tokens")
        return 1 if bad else 0
    for name, text in files.items():
        (OUT / name).write_text(text, encoding="utf-8")
    rsvg = shutil.which("rsvg-convert")
    if not rsvg:
        print("rsvg-convert not found: SVGs and gallery written, PNGs not")
        return 0
    for name, scales in PNGS.items():
        for sc in scales:
            png = OUT / (f"{name}.png" if sc == 1 else f"{name}@{sc}x.png")
            subprocess.run([rsvg, "-z", str(sc), "-o", str(png), str(OUT / f"{name}.svg")], check=True)
    print("written:", ", ".join(sorted(p.name for p in OUT.iterdir())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
