"""A throwaway generated skill folder for the approval-by-hash check."""
import json
from pathlib import Path

from envrec.skills.skillmd import load_skill


def generated_skill(root: Path, script: str, rewrite: bool = True):
    d = root / "lib" / "g-approved"
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\nname: g-approved\ndescription: Use when a conformance check needs a throwaway skill.\n---\n\n# g\n")
    (d / "skill.meta.json").write_text(json.dumps({"provenance": "generated", "platforms": ["any"], "tier": "observe", "ring": 0, "produces": [],
                                                  "plan": {"steps": [{"id": "r", "tool": "read_file", "args": {"path": "/tmp/x"}}], "script": "scripts/parse.py"}}))
    if rewrite:
        (d / "scripts" / "parse.py").write_text(script)
    return load_skill(d)
