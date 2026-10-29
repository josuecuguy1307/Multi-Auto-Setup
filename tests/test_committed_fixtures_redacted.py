"""Committed files must not carry the real name of a device (or the owner's username). The list holds only HASHES of those names, so the list itself leaks nothing."""
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIST = json.loads((ROOT / "tests" / "redaction_list.json").read_text())["entries"]
SKIP_SUFFIX = {".png", ".pyz", ".gz", ".db", ".pyc", ".sha256"}


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def hits_in(text: str, entries=LIST) -> list[int]:
    ws = words(text)
    found = []
    for e in entries:
        n = e["words"]
        for i in range(len(ws) - n + 1):
            if hashlib.sha256(" ".join(ws[i:i + n]).encode()).hexdigest() == e["sha256"]:
                found.append(n)
                break
    return found


def entries_for(*names: str) -> list[dict]:
    return [{"words": len(words(n)), "sha256": hashlib.sha256(" ".join(words(n)).encode()).hexdigest()} for n in names]


def test_the_detector_finds_a_listed_name_wherever_it_hides():
    """Uses invented names: the real ones must not appear in this file either."""
    listed = entries_for("Gadget Nine 5 (7th edition)-Q7Z", "Q7Z")
    assert hits_in('x "name": "Gadget Nine 5 (7th edition)-Q7Z", y', listed) == [6, 1]  # the whole name, and its unit suffix, which is listed on its own
    assert hits_in("lowercase gadget nine 5 7th edition q7z inside", listed) == [6, 1]
    assert hits_in("Example Speaker 1 and a Gadget Nine 5 (7th edition) without a unit suffix", listed) == []


def test_the_shipped_list_holds_hashes_only_and_is_not_empty():
    assert len(LIST) >= 3 and all(set(e) == {"words", "sha256"} and len(e["sha256"]) == 64 for e in LIST)


def test_no_committed_file_contains_a_listed_real_name():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    offenders = []
    for f in files:
        p = ROOT / f
        if p.suffix in SKIP_SUFFIX or f == "tests/redaction_list.json" or not p.is_file():
            continue
        try:
            text = p.read_text()
        except UnicodeDecodeError:
            continue
        if hits_in(text):
            offenders.append(f)
    assert offenders == [], offenders
