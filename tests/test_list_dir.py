"""The node's list_dir task (P1): names, kinds, sizes and dates of one folder, never contents; read-only; typed refusals; a task round trip through a real coordinator and node (SIMULATED-LOCAL)."""
from __future__ import annotations

import asyncio
import os

import pytest
from mas_world import coordinator
from test_mas_tasks import agent_up, down, finished

from envrec_net import task as T
from envrec_os import folders as F

SECRET = "TOP-SECRET-CONTENT-DO-NOT-READ"


def make(tmp_path):
    d = tmp_path / "Desk"
    d.mkdir()
    (d / "notes.txt").write_text(SECRET)
    (d / "photo.png").write_bytes(b"\x89PNG" + b"0" * 2000)
    (d / "Projects").mkdir()
    (d / "Projects" / "inner.txt").write_text(SECRET)
    return d


def test_it_lists_names_kinds_sizes_and_dates_and_never_opens_a_file_or_recurses(tmp_path):
    d = make(tmp_path)
    out = F.list_dir(str(d), fam="darwin")
    by = {e["name"]: e for e in out["entries"]}
    assert set(by) == {"notes.txt", "photo.png", "Projects"} and by["Projects"]["kind"] == "folder" and by["photo.png"]["kind"] == "file" and by["photo.png"]["bytes"] == 2004
    assert by["notes.txt"]["bytes"] == len(SECRET) and len(by["notes.txt"]["modified"]) == 10 and out["count"] == 3 and out["cut"] == 0
    assert SECRET not in str(out) and "inner.txt" not in str(out)  # contents were not read, and the folder was not entered
    assert set(by["notes.txt"]) == {"name", "kind", "bytes", "modified"}


def test_a_link_out_of_the_folder_is_listed_as_a_name_and_not_followed(tmp_path):
    d = make(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "private.txt").write_text(SECRET)
    os.symlink(outside, d / "link")
    by = {e["name"]: e for e in F.list_dir(str(d), fam="darwin")["entries"]}
    assert by["link"]["kind"] == "other" and by["link"]["bytes"] == 0 and "private.txt" not in str(by)


def test_a_long_folder_is_cut_and_says_how_many(tmp_path):
    d = tmp_path / "many"
    d.mkdir()
    for i in range(30):
        (d / f"f{i:02}.txt").write_text("x")
    out = F.list_dir(str(d), limit=10, fam="darwin")
    assert len(out["entries"]) == 10 and out["cut"] == 20 and out["count"] == 30


@pytest.mark.parametrize("arg,code", [("", "bad_path"), ("relative/dir", "not_absolute"), ("/a/../b", "bad_path"), ("Nowhere", "not_absolute"), ("/definitely/not/here", "not_a_folder")])
def test_a_bad_folder_is_a_typed_refusal_that_tells_what_to_say(arg, code):
    with pytest.raises(F.FolderRefused) as e:
        F.list_dir(arg, fam="darwin")
    assert e.value.code == code


def test_a_well_known_name_is_looked_up_in_the_data_for_this_family(tmp_path):
    env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    (tmp_path / "Desktop").mkdir()
    assert F.resolve("Desktop", fam="darwin", env=env) == tmp_path / "Desktop"
    assert str(F.resolve("DESKTOP", fam="windows", env=env)).endswith("Desktop")
    with pytest.raises(F.FolderRefused) as e:
        F.resolve("Desktop", fam="plan9", env=env)
    assert e.value.code == "no_such_folder_name"


def test_the_task_has_no_parameter_that_writes_or_runs_anything():
    assert set(T.ListDirParams.model_fields) == {"folder", "limit"} and "list_dir" not in T.WRITE_TYPES
    for bad in ({"folder": "/x", "command": "ls"}, {"folder": "/x", "limit": 9999}, {}):
        with pytest.raises(ValueError):
            T.validate_params("list_dir", bad)


def test_a_real_node_lists_a_folder_for_its_coordinator_and_reports_a_refusal_with_its_reason(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                d = make(tmp_path)
                rec = await finished(c.tasks.start(nid, "list_dir", {"folder": str(d)}, by="GUARD"))
                assert rec.status == "done" and {e["name"] for e in rec.result["entries"]} == {"notes.txt", "photo.png", "Projects"} and SECRET not in str(rec.result)
                bad = await finished(c.tasks.start(nid, "list_dir", {"folder": "/definitely/not/here"}, by="GUARD"))
                assert bad.status == "failed" and bad.reason == "not_a_folder"
                worse = await finished(c.tasks.start(nid, "list_dir", {"folder": str(d), "command": "rm -rf /"}, by="GUARD"))
                assert worse.status == "rejected"
            finally:
                await down(agent, tsk)
    asyncio.run(go())
