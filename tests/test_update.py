"""`mas update` and the release helpers: SHA-256 check, safe unpacking, atomic switch, rollback, and the owner's question. SIMULATED-LOCAL: the release is built from a temporary tree by
scripts/make_release.py and served by a fake fetch; uv and the new interpreter are faked (the real run with uv is the installer test)."""
from __future__ import annotations

import argparse
import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import make_release  # noqa: E402
from envrec_mas import lifecycle, release as rel  # noqa: E402


def make_tree(tmp: Path, marker: str) -> Path:
    t = tmp / f"tree-{marker}"
    (t / "src").mkdir(parents=True)
    (t / "src" / "m.py").write_text(f"V = {marker!r}\n")
    (t / "pyproject.toml").write_text("[project]\nname='x'\n")
    (t / "install.sh").write_text(f"# installer of {marker}\n")
    return t


class World:
    """A fake network with one latest release, and a fake `uv`/interpreter that records what ran."""

    def __init__(self, tmp: Path, tag="v2", notes="Adds things.", tamper=False, smoke_ok=True, install_ok=True):
        self.tag, self.tamper = tag, tamper
        data = make_release.build(make_tree(tmp, tag), tag)
        sha = rel.sha256_hex(data)
        self.manifest = {"tag": tag, "archive": f"mas-{tag}.tar.gz", "sha256": sha, "size": len(data), "notes": notes}
        self.archive = (b"X" + data[1:]) if tamper else data
        self.ran: list[list[str]] = []
        self.smoke_ok, self.install_ok = smoke_ok, install_ok

    def fetch(self, url: str) -> bytes:
        if url.endswith("mas-release.json"):
            return json.dumps(self.manifest).encode()
        assert url.endswith(f"/download/{self.tag}/mas-{self.tag}.tar.gz"), url
        return self.archive

    def run(self, argv, cwd):
        self.ran.append(list(argv))
        if argv[1:2] == ["venv"]:
            Path(argv[-1]).mkdir(parents=True)
            return 0, ""
        if argv[1:3] == ["pip", "install"]:
            return (0, "") if self.install_ok else (1, "no wheel")
        return (0, f"mas 0.1.0 (release {self.tag})") if self.smoke_ok else (1, "ImportError")


@pytest.fixture
def installed(tmp_path, monkeypatch):
    home = tmp_path / "mashome"
    monkeypatch.setenv("MAS_HOME", str(home))
    monkeypatch.setenv("MAS_RELEASE_BASE", "https://example.invalid/releases")
    lay = rel.Layout(home)
    (lay.release_dir("v1") / ".venv").mkdir(parents=True)
    (lay.release_dir("v1") / "RELEASE").write_text(json.dumps({"tag": "v1"}))
    lay.write_atomic("current", "v1")
    return lay


def args(**kw):
    return argparse.Namespace(check=kw.get("check", False), yes=kw.get("yes", True))


def test_update_downloads_verifies_builds_switches_and_keeps_only_current_and_previous(installed, tmp_path, capsys):
    old = installed.release_dir("v0")
    old.mkdir(parents=True)
    w = World(tmp_path)
    assert lifecycle.cmd_update(args(), fetch=w.fetch, run=w.run, smoke_via_pointer=lambda lay: lay.current == "v2") == 0
    assert installed.current == "v2" and installed.previous == "v1"
    assert (installed.release_dir("v2") / "src" / "m.py").read_text() == "V = 'v2'\n"
    assert json.loads((installed.release_dir("v2") / "RELEASE").read_text())["sha256"] == w.manifest["sha256"]
    assert installed.release_dir("v1").is_dir() and not old.exists()  # the previous is kept for one more update, older ones go
    assert any(c[1:3] == ["pip", "install"] and "-e" in c for c in w.ran)
    assert (installed.home / "install.sh").read_text() == "# installer of v2\n"  # the uninstaller next to the program follows the release
    out = capsys.readouterr().out
    assert "v1" in out and "v2" in out and "Adds things." in out and "are not touched" in out


def test_a_sha_mismatch_stops_before_anything_is_written(installed, tmp_path, capsys):
    w = World(tmp_path, tamper=True)
    assert lifecycle.cmd_update(args(), fetch=w.fetch, run=w.run) == 1
    assert installed.current == "v1" and not installed.release_dir("v2").exists() and not any(d.name.startswith(".") for d in installed.releases.iterdir())
    assert w.ran == [] and "sha_mismatch" in capsys.readouterr().err


def test_a_failed_install_removes_the_new_release_and_keeps_the_old_one(installed, tmp_path, capsys):
    w = World(tmp_path, install_ok=False)
    assert lifecycle.cmd_update(args(), fetch=w.fetch, run=w.run) == 1
    assert installed.current == "v1" and not installed.release_dir("v2").exists()
    assert "install_failed" in capsys.readouterr().err


def test_a_new_release_that_does_not_start_is_never_made_current(installed, tmp_path, capsys):
    w = World(tmp_path, smoke_ok=False)
    assert lifecycle.cmd_update(args(), fetch=w.fetch, run=w.run) == 1
    assert installed.current == "v1" and not installed.release_dir("v2").exists()
    assert "smoke_failed" in capsys.readouterr().err


def test_if_the_switch_cannot_be_confirmed_the_old_tag_is_put_back(installed, tmp_path, capsys):
    w = World(tmp_path)
    assert lifecycle.cmd_update(args(), fetch=w.fetch, run=w.run, smoke_via_pointer=lambda lay: False) == 1
    assert installed.current == "v1"  # rolled back
    assert "switch_failed" in capsys.readouterr().err


def test_without_a_yes_nothing_happens(installed, tmp_path, monkeypatch, capsys):
    w = World(tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert lifecycle.cmd_update(args(yes=False), fetch=w.fetch, run=w.run) == 1
    assert installed.current == "v1" and not installed.release_dir("v2").exists() and w.ran == []


def test_check_shows_what_would_change_and_changes_nothing(installed, tmp_path, capsys):
    w = World(tmp_path)
    assert lifecycle.cmd_update(args(check=True, yes=False), fetch=w.fetch, run=w.run) == 0
    assert installed.current == "v1" and w.ran == [] and "Latest version" in capsys.readouterr().out


def test_already_current_says_so(installed, tmp_path, capsys):
    w = World(tmp_path, tag="v1")
    assert lifecycle.cmd_update(args(), fetch=w.fetch, run=w.run) == 0
    assert "You already have the latest version" in capsys.readouterr().out and w.ran == []


def test_a_copy_not_made_by_the_installer_is_not_managed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MAS_HOME", str(tmp_path / "nothing"))
    assert lifecycle.cmd_update(args(), fetch=lambda u: b"", run=None) == 1
    assert "was not installed by the installer" in capsys.readouterr().err


@pytest.mark.parametrize("manifest", [{"tag": "../x", "archive": "a.tar.gz", "sha256": "0" * 64}, {"tag": "v3", "archive": "a.tar.gz", "sha256": "xyz"}, {"tag": "v3", "archive": "../a.tar.gz", "sha256": "0" * 64}, ["x"]])
def test_a_malformed_manifest_is_refused(manifest):
    with pytest.raises(rel.UpdateRefused) as e:
        rel.fetch_manifest("https://h", lambda u: json.dumps(manifest).encode())
    assert e.value.reason == "bad_manifest"


def test_only_https_or_this_computer_is_ever_fetched():
    for url in ("http://example.com/x", "file:///etc/passwd", "ftp://h/x"):
        with pytest.raises(rel.UpdateRefused) as e:
            rel.http_get(url)
        assert e.value.reason == "insecure_url"


def _tar(entries) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, kind in entries:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                ti.size = 1
                tf.addfile(ti, io.BytesIO(b"x"))
            else:
                ti.type = {"sym": tarfile.SYMTYPE, "hard": tarfile.LNKTYPE}[kind]
                ti.linkname = "/etc/passwd"
                tf.addfile(ti)
    return buf.getvalue()


@pytest.mark.parametrize("entries", [[("top/../../evil", "file")], [("/abs/evil", "file")], [("top/link", "sym")], [("top/hard", "hard")], [("a/x", "file"), ("b/y", "file")], [("top/a:b", "file"), ("top/..", "file")]])
def test_unpacking_refuses_escapes_links_and_two_roots(tmp_path, entries):
    with pytest.raises(rel.UpdateRefused) as e:
        rel.safe_extract(_tar(entries), tmp_path / "dest")
    assert e.value.reason == "bad_archive" and not (tmp_path / "evil").exists()


def test_unpacking_strips_the_single_top_folder(tmp_path):
    rel.safe_extract(_tar([("top/a/b.txt", "file")]), tmp_path / "dest")
    assert (tmp_path / "dest" / "a" / "b.txt").read_text() == "x"


def test_release_packing_is_deterministic_and_leaves_out_caches(tmp_path):
    t = make_tree(tmp_path, "z")
    (t / "__pycache__").mkdir()
    (t / "__pycache__" / "c.pyc").write_text("junk")
    a, b = make_release.build(t, "v9"), make_release.build(t, "v9")
    assert a == b
    names = tarfile.open(fileobj=io.BytesIO(a)).getnames()
    assert "multi-auto-setup-v9/src/m.py" in names and not any("pycache" in n for n in names)


def test_notes_from_the_network_lose_control_characters():
    m = rel.fetch_manifest("https://h", lambda u: json.dumps({"tag": "v3", "archive": "a.tar.gz", "sha256": "0" * 64, "notes": "ok\x1b[31mred\x00"}).encode())
    assert "\x1b" not in m["notes"] and "\x00" not in m["notes"]
