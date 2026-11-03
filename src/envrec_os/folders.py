"""Listing one folder: names, kinds, sizes and modification dates, never contents (skill `os-folders`). The well-known names and the limits are data in `skills/os-folders/references/folders.json`; this is
the only place that touches the file system for a listing. Read-only: it opens no file, follows no link out of the folder and does not recurse."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

from envrec_os import SKILLS_DIR, _expand, family

FOLDERS_FILE = SKILLS_DIR / "os-folders" / "references" / "folders.json"


class FolderRefused(Exception):
    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code, self.text = code, text


def load_table(path: Path | None = None) -> dict:
    return json.loads((path or FOLDERS_FILE).read_text(encoding="utf-8"))


def resolve(arg: str, *, fam: str | None = None, env: dict[str, str] | None = None, table: dict | None = None) -> Path:
    """A well-known name or an absolute path, as a path on this computer. Anything else is refused with a typed reason."""
    table = table or load_table()
    fam = fam or family()
    name = str(arg).strip()
    if not name or len(name) > 400 or "\x00" in name:
        raise FolderRefused("bad_path", "give a well-known folder name (the os-folders skill lists them) or an absolute path")
    row = table["folders"].get(name.lower())
    if row is not None:
        template = row.get(fam)
        if not template:
            raise FolderRefused("no_such_folder_name", f"{name} has no known place on a {fam} computer; give an absolute path")
        if env is not None and template.startswith("~") and env.get("HOME"):
            template = env["HOME"] + template[1:]  # the home folder of the environment given, not of this process
        return _expand(template, env)
    absolute = PureWindowsPath(name).is_absolute() if fam == "windows" else PurePosixPath(name).is_absolute()
    if not absolute:
        raise FolderRefused("not_absolute", f"{name!r} is not a folder name this computer knows ({', '.join(sorted(table['folders']))}) and not an absolute path")
    parts = PureWindowsPath(name).parts if fam == "windows" else PurePosixPath(name).parts
    if ".." in parts:
        raise FolderRefused("bad_path", "a path with .. is not accepted")
    return Path(name)


def list_dir(arg: str, *, limit: int | None = None, fam: str | None = None, env: dict[str, str] | None = None, table: dict | None = None) -> dict:
    table = table or load_table()
    cap = min(int(limit or table["limits"]["max_entries"]), int(table["limits"]["max_entries"]))
    name_cap = int(table["limits"]["max_name_chars"])
    path = resolve(arg, fam=fam, env=env, table=table)
    try:
        if not path.is_dir():
            raise FolderRefused("not_a_folder", "that is not a folder on this computer")
        it = sorted(os.scandir(path), key=lambda e: e.name.lower())
    except FolderRefused:
        raise
    except PermissionError:
        raise FolderRefused("permission", "this computer did not let the software read that folder") from None
    except OSError as exc:
        raise FolderRefused("unreadable", f"the folder could not be read ({type(exc).__name__})") from None
    entries, total_bytes = [], 0
    for e in it[:cap]:
        try:
            st = e.stat(follow_symlinks=False)
            kind = "folder" if e.is_dir(follow_symlinks=False) else "file" if e.is_file(follow_symlinks=False) else "other"
            size, mtime = (st.st_size if kind == "file" else 0), time.strftime("%Y-%m-%d", time.gmtime(st.st_mtime))
        except OSError:
            kind, size, mtime = "other", 0, ""
        total_bytes += size
        entries.append({"name": "".join(c if c.isprintable() else " " for c in e.name)[:name_cap], "kind": kind, "bytes": size, "modified": mtime})
    return {"path": str(path), "entries": entries, "count": len(it), "cut": max(0, len(it) - cap), "total_bytes": total_bytes}
