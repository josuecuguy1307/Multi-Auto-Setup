"""install.sh and install.ps1: static checks on both, and the shell installer's flow with a stand-in `uv` and a release served from this computer. SIMULATED-LOCAL.
The real-`uv` run, the launchd run and the container runs are recorded in PROGRESS.md; the Windows script is checked statically here (UNTESTED on Windows)."""
from __future__ import annotations

import functools
import hashlib
import http.server
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SH = ROOT / "install.sh"
PS = ROOT / "install.ps1"
sys.path.insert(0, str(ROOT / "scripts"))
import make_release  # noqa: E402
import pin_installers  # noqa: E402

needs_sh = pytest.mark.skipif(shutil.which("sh") is None or sys.platform == "win32", reason="needs a POSIX shell")


# ---- static -------------------------------------------------------------------------------------------------------------------------------------

def pins(text: str) -> tuple[str, str, str]:
    g = lambda pat: re.search(pat, text, re.M).group(1)  # noqa: E731
    if "DEFAULT_TAG" in text:
        return g(r'^DEFAULT_TAG="(.*)"$'), g(r'^DEFAULT_SHA256="(.*)"$'), g(r'^DEFAULT_BASE="(.*)"$')
    return g(r"^\$DefaultTag = '(.*)'$"), g(r"^\$DefaultSha256 = '(.*)'$"), g(r"^\$DefaultBase = '(.*)'$")


def test_both_installers_pin_the_same_release_and_the_hash_is_well_formed():
    a, b = pins(SH.read_text(encoding="utf-8")), pins(PS.read_text(encoding="utf-8"))
    assert a == b
    assert re.fullmatch(r"[0-9a-f]{64}", a[1]) and a[2].startswith("https://github.com/")


@needs_sh
@pytest.mark.parametrize("shell", ["sh", "dash", "bash"])
def test_install_sh_parses_in_each_shell_that_exists(shell):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not installed")
    assert subprocess.run([shell, "-n", str(SH)], capture_output=True, text=True).returncode == 0


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_install_sh_has_no_shellcheck_warnings_or_errors():
    done = subprocess.run(["shellcheck", "-s", "sh", "-S", "warning", str(SH)], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout


def test_install_sh_is_wrapped_so_a_truncated_download_runs_nothing():
    lines = [ln for ln in SH.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines[-2:] == ['main "$@"', "exit $?"]
    assert "main() {" in lines


FORBIDDEN_EVERYWHERE = ["sudo", "Set-ExecutionPolicy", "RunAs", "HKLM", "New-NetFirewallRule", "netsh", "iptables", "ufw", "firewall-cmd", "pfctl", "Set-Date", "timedatectl", "-Scope LocalMachine", "'Machine'"]


@pytest.mark.parametrize("path", [SH, PS])
def test_neither_installer_elevates_or_changes_the_firewall_network_or_clock(path):
    text = path.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for word in FORBIDDEN_EVERYWHERE:
        assert word not in code, (path.name, word)


@pytest.mark.parametrize("path", [SH, PS])
def test_both_check_the_sha256_before_they_unpack_and_before_they_build(path):
    t = path.read_text(encoding="utf-8")
    sha = t.index("does NOT match")
    unpack = t.index("tar -xzf") if path == SH else t.index("tar.exe -xzf")
    build = t.index(' venv ') if path == SH else t.index("& $uv venv")
    assert sha < unpack < build


@pytest.mark.parametrize("path", [SH, PS])
def test_both_ask_before_running_the_official_uv_command_and_show_it(path):
    t = path.read_text(encoding="utf-8")
    cmd = "astral.sh/uv/install.sh" if path == SH else "astral.sh/uv/install.ps1"
    assert t.count(cmd) >= 2 and "UV_NO_MODIFY_PATH" in t
    assert t.index("Run that command now?") < t.rindex(cmd)  # the question comes before the line that actually runs it


def test_install_ps1_has_a_bom_changes_only_the_user_path_and_asks_before_it():
    raw = PS.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"  # Windows PowerShell 5.1 reads a file without one as ANSI and garbles non-ASCII text
    t = raw.decode("utf-8-sig")
    assert "SetEnvironmentVariable('Path', $new, 'User')" in t and "'Machine'" not in t
    assert t.index("Add that folder to your user PATH?") < t.index("SetEnvironmentVariable('Path', $new, 'User')")


def _functions(text: str) -> list[tuple[str, str]]:
    """(name, body) of each top-level `function Name { ... }` of the script, by brace counting (the script keeps its braces out of strings)."""
    out = []
    for m in re.finditer(r"^function ([A-Za-z-]+)[^{]*\{", text, flags=re.M):
        depth, i = 1, m.end()
        while depth and i < len(text):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        out.append((m.group(1), text[m.end():i]))
    return out


def test_install_ps1_never_reads_its_own_path_from_inside_a_function():
    """Found on the first real Windows run: `$MyInvocation.MyCommand.Path` inside Invoke-Install is the FUNCTION's invocation, which has no Path property, so strict mode stopped the install with
    "The property 'Path' cannot be found on this object" after the environment was built. The script's own path is read once, at script level, from $PSCommandPath (empty when it arrived through a pipe)."""
    t = PS.read_bytes().decode("utf-8-sig")
    assert "MyInvocation.MyCommand" not in t
    first_function = re.search(r"^function ", t, flags=re.M).start()
    assert t.index("$ScriptPath = ") < first_function and "$PSCommandPath" in t[:first_function] and "Set-StrictMode -Version 2" in t[:first_function]
    inside = dict(_functions(t))
    assert "$ScriptPath" in inside["Invoke-Install"] and "$PSCommandPath" not in inside["Invoke-Install"]
    install = inside["Invoke-Install"]
    assert re.search(r"elseif \(\$mine -and \(Test-Path \$mine\)\)", install)  # a pipe leaves it empty: then there is nothing to copy, and nothing fails


def test_install_ps1_copes_with_a_user_path_that_does_not_exist_and_asks_before_creating_it():
    """A user with no PATH variable of their own is normal on Windows (the value is absent, so the read gives $null). Reading it, testing for the folder and building the new value must all be
    null-safe, the value is created only after the owner's yes, and only the user's variable is ever written."""
    t = PS.read_bytes().decode("utf-8-sig")
    funcs = dict(_functions(t))
    assert "GetEnvironmentVariable('Path', 'User')" in funcs["Get-UserPath"] and ".Path" not in funcs["Get-UserPath"]
    assert re.search(r"if \(-not \$p\) \{ return \$false \}", funcs["Test-OnUserPath"]) and funcs["Test-OnUserPath"].index("-not $p") < funcs["Test-OnUserPath"].index("$p.Split")
    assert "$new = if ($p) { $p.TrimEnd(';') + ';' + $BinDir } else { $BinDir }" in t  # nothing to extend: the new value is the folder alone
    assert t.count("SetEnvironmentVariable('Path'") == 2 and "'Machine'" not in t and "HKLM" not in t  # the install and the uninstall, both 'User'
    assert all("'User')" in line for line in t.splitlines() if "SetEnvironmentVariable('Path'" in line)
    assert t.index("Add that folder to your user PATH?") < t.index("SetEnvironmentVariable('Path', $new, 'User')")
def test_install_ps1_tells_the_window_that_started_it_how_to_run_mas_before_a_new_window_exists():
    """On the first real Windows run the installer added .mas\\bin to the user PATH and then told the owner to type `mas join` in the SAME window, which cannot find it (the script is a child process: its
    PATH change reaches new windows only). The session PATH is set after the consent for the script's own later steps, and every hint the owner is given uses the full path unless the command is already found."""
    t = PS.read_bytes().decode("utf-8-sig")
    install = dict(_functions(t))["Invoke-Install"]
    consent, user_write = install.index("Add that folder to your user PATH?"), install.index("SetEnvironmentVariable('Path', $new, 'User')")
    session_write = install.index("$env:Path = $env:Path.TrimEnd(';') + ';' + $BinDir")
    assert consent < user_write < session_write  # the session is touched only after the consent and the lasting change
    assert "if (-not $inSession)" in install[session_write - 30:session_write] and "'Machine'" not in t
    assert install.index("$inSession =") < consent  # asked BEFORE the PATH changes
    assert "$masName = if ($inSession) { 'mas' } else { \"& '$masCmd'\" }" in install
    hints = [line for line in install.splitlines() if "Say" in line and re.search(r"\bmas (join|service|update|coordinator)\b", line)]
    assert hints == []  # no hint says a bare `mas ...`: they all go through $masName
    assert install.count("$masName join") >= 2 and "$masName service install" in install and "$masName update" in install


def test_the_uninstaller_touches_the_user_path_only_when_the_folder_is_on_it():
    """`Test-OnUserPath` is null-safe (a missing user Path answers no), and the uninstaller writes the variable only inside `if (Test-OnUserPath ...)`: a missing user Path is left missing."""
    undo = dict(_functions(PS.read_bytes().decode("utf-8-sig")))["Invoke-Uninstall"]
    guard = re.search(r"if \(Test-OnUserPath \$BinDir\) \{.*?SetEnvironmentVariable\('Path', \(\$kept -join ';'\), 'User'\)", undo, flags=re.S)
    assert guard and undo.count("SetEnvironmentVariable") == 1




@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell (pwsh); on Windows the script has never been run by this suite")
def test_install_ps1_parses_with_the_powershell_parser():
    code = f"$e=$null; [System.Management.Automation.Language.Parser]::ParseFile('{PS}', [ref]$null, [ref]$e) | Out-Null; if ($e) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}"
    assert subprocess.run(["pwsh", "-NoProfile", "-Command", code], capture_output=True, text=True).returncode == 0


def test_pinning_rewrites_both_files_and_nothing_else(tmp_path):
    for f in (SH, PS):
        shutil.copy(f, tmp_path / f.name)
    changed = pin_installers.pin(tmp_path, "v9", "ab" * 32, "https://example.com/r")
    assert sorted(changed) == ["install.ps1", "install.sh"]
    assert pins((tmp_path / "install.sh").read_text(encoding="utf-8")) == ("v9", "ab" * 32, "https://example.com/r") == pins((tmp_path / "install.ps1").read_text(encoding="utf-8"))
    assert (tmp_path / "install.ps1").read_bytes()[:3] == b"\xef\xbb\xbf"
    a = SH.read_text(encoding="utf-8").splitlines()
    b = (tmp_path / "install.sh").read_text(encoding="utf-8").splitlines()
    assert len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 3
    with pytest.raises(SystemExit):
        pin_installers.pin(tmp_path, "../x", "ab" * 32, None)


# ---- the flow, with a stand-in uv --------------------------------------------------------------------------------------------------------------------

STUB_UV = """#!/bin/sh
# stand-in for uv: `venv --python V DIR` makes DIR/bin/python (the real interpreter) and `pip install` does nothing
case "$1" in
  venv) dir="$4"; mkdir -p "$dir/bin"; printf '#!/bin/sh\\nexec "%s" "$@"\\n' "$REAL_PY" >"$dir/bin/python"; chmod +x "$dir/bin/python"
        printf '#!/bin/sh\\necho mas-stub\\n' >"$dir/bin/mas"; chmod +x "$dir/bin/mas" ;;
  pip) exit 0 ;;
esac
"""


@pytest.fixture
def lab(tmp_path):
    """A fake release server on loopback, a temp HOME with a stand-in uv first on PATH, and a way to run install.sh there."""
    tree = tmp_path / "tree"
    (tree / "src" / "envrec_mas").mkdir(parents=True)
    (tree / "src" / "envrec_mas" / "__init__.py").write_text("")
    (tree / "src" / "envrec_mas" / "cli.py").write_text("")
    data = make_release.build(tree, "t1")
    sha = hashlib.sha256(data).hexdigest()
    srv = tmp_path / "srv"
    (srv / "download" / "t1").mkdir(parents=True)
    (srv / "download" / "t1" / "mas-t1.tar.gz").write_bytes(data)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(srv))
    handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "uv").write_text(STUB_UV)
    (home / "bin" / "uv").chmod(0o755)

    class Lab:
        pass

    L = Lab()
    L.home, L.sha, L.srv, L.data = home, sha, srv, data
    L.base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def run(*args, extra_env=None, stdin="", detached=False):
        env = {"HOME": str(home), "PATH": f"{home}/bin:/usr/bin:/bin:/usr/sbin:/sbin", "SHELL": "/bin/sh", "MAS_RELEASE_BASE": L.base, "REAL_PY": sys.executable,
               "PYTHONPATH": str(tree / "src"), "TMPDIR": str(tmp_path)}
        env.update(extra_env or {})
        # detached: a new session with no controlling terminal (POSIX), for a test whose precondition is "no terminal": stdin="" only pipes stdin, and the installer asks on /dev/tty, which answers through the controlling terminal this child would otherwise inherit
        return subprocess.run(["sh", str(SH), *args], capture_output=True, text=True, env=env, input=stdin, timeout=120, start_new_session=detached)

    L.run = run
    yield L
    httpd.shutdown()


def tree_of(path: Path) -> set[str]:
    return {str(p.relative_to(path)) for p in path.rglob("*")} if path.exists() else set()


@needs_sh
def test_a_wrong_sha_stops_before_anything_is_written(lab):
    r = lab.run("--yes", "--role", "node", "--no-join", "--tag", "t1", "--sha256", "0" * 64)
    assert r.returncode != 0 and "does NOT match" in r.stderr and "nothing was installed" in r.stderr
    assert not (lab.home / ".mas").exists() and not (lab.home / ".local").exists()


@needs_sh
def test_another_tag_without_its_own_sha_is_refused_and_the_embedded_hash_is_never_applied_to_it(lab):
    r = lab.run("--yes", "--tag", "t1")
    assert r.returncode != 0 and "--sha256" in r.stderr and not (lab.home / ".mas").exists()


@needs_sh
def test_an_archive_with_a_path_that_escapes_is_refused_even_if_its_hash_matches(lab, tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("top/", "top/../../evil"):
            ti = tarfile.TarInfo(name)
            ti.type = tarfile.DIRTYPE if name.endswith("/") else tarfile.REGTYPE
            tf.addfile(ti, io.BytesIO(b"") if ti.type == tarfile.REGTYPE else None)
    bad = buf.getvalue()
    (lab.srv / "download" / "t1" / "mas-t1.tar.gz").write_bytes(bad)
    r = lab.run("--yes", "--role", "node", "--no-join", "--tag", "t1", "--sha256", hashlib.sha256(bad).hexdigest())
    assert r.returncode != 0 and "paths that are not allowed" in r.stderr
    assert not (lab.home / ".mas" / "releases" / "t1").exists() and not (tmp_path / "evil").exists()


@needs_sh
def test_install_then_reinstall_then_uninstall_leaves_the_keys_and_nothing_else(lab):
    args = ("--yes", "--role", "node", "--no-join", "--tag", "t1", "--sha256", lab.sha)
    r = lab.run(*args)
    assert r.returncode == 0, r.stderr
    mas = lab.home / ".mas"
    assert (mas / "current").read_text().strip() == "t1" and (mas / "role").read_text().strip() == "node"
    assert (mas / "install.sh").is_file() and (mas / "releases" / "t1" / "RELEASE").is_file()
    shim = lab.home / ".local" / "bin" / "mas"
    assert shim.is_file() and os.access(shim, os.X_OK)
    assert "multi-auto-setup" in (lab.home / ".profile").read_text()  # the PATH block was shown, asked (--yes) and written
    out = subprocess.run([str(shim)], capture_output=True, text=True, env={"HOME": str(lab.home), "PATH": "/usr/bin:/bin"})
    assert out.stdout.strip() == "mas-stub"
    # a second run repairs in place: no download, nothing duplicated
    before = tree_of(lab.home)
    r2 = lab.run(*args, extra_env={"PATH": f"{lab.home}/.local/bin:{lab.home}/bin:/usr/bin:/bin"})
    assert r2.returncode == 0 and "is already installed" in r2.stdout and tree_of(lab.home) == before
    assert (lab.home / ".profile").read_text().count(">>> multi-auto-setup") == 1
    # keys live outside ~/.mas and survive an uninstall without --purge
    state = lab.home / ".local" / "share" / "mas" / "node" / "identity"
    state.mkdir(parents=True)
    (state / "key.pem").write_text("k")
    u = lab.run("--uninstall", "--yes")
    assert u.returncode == 0, u.stderr
    assert not mas.exists() and not shim.exists() and not (lab.home / ".profile").exists()
    assert (state / "key.pem").read_text() == "k"


@needs_sh
def test_uninstall_refuses_unsafe_or_foreign_folders_and_a_declined_question_changes_nothing(lab, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "precious.txt").write_text("x")
    r = lab.run("--uninstall", "--yes", "--home", str(foreign))
    assert r.returncode != 0 and "I cannot find an installation" in r.stderr and (foreign / "precious.txt").exists()
    assert lab.run("--uninstall", "--yes", "--home", "/").returncode != 0
    assert lab.run("--uninstall", "--yes", "--home", str(lab.home)).returncode != 0  # the home folder itself
    args = ("--yes", "--role", "node", "--no-join", "--tag", "t1", "--sha256", lab.sha)
    assert lab.run(*args).returncode == 0
    n = lab.run("--uninstall", stdin="", detached=True)  # no --yes and no terminal (detached, so /dev/tty cannot answer): the answer is no
    assert n.returncode != 0 and (lab.home / ".mas" / "current").exists()


@needs_sh
def test_a_foreign_mas_command_is_not_overwritten(lab):
    (lab.home / ".local" / "bin").mkdir(parents=True)
    (lab.home / ".local" / "bin" / "mas").write_text("#!/bin/sh\necho theirs\n")
    r = lab.run("--yes", "--role", "node", "--no-join", "--tag", "t1", "--sha256", lab.sha)
    assert r.returncode == 0 and "it is not ours" in r.stdout
    assert (lab.home / ".local" / "bin" / "mas").read_text().endswith("echo theirs\n")


@needs_sh
def test_through_a_pipe_the_installer_still_asks_on_the_terminal_and_a_no_changes_nothing(lab):
    """`cat install.sh | sh`: stdin is the script, so the questions must come from /dev/tty. A pseudo-terminal stands in for the person."""
    import pty
    import select
    import time
    env = {"HOME": str(lab.home), "PATH": f"{lab.home}/bin:/usr/bin:/bin", "SHELL": "/bin/sh", "MAS_RELEASE_BASE": lab.base}
    pid, fd = pty.fork()
    if pid == 0:
        os.execve("/bin/sh", ["sh", "-c", f"cat {SH} | sh -s -- --role node --no-join --tag t1 --sha256 {lab.sha}"], env)
    seen, answered, end = b"", False, time.time() + 30
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            seen += chunk
            if b"build version" in seen and b"[y/N]: " in seen and not answered:
                os.write(fd, b"n\n")
                answered = True
    _, status = os.waitpid(pid, 0)
    text = seen.decode("utf-8", "replace")
    assert answered, text
    assert os.waitstatus_to_exitcode(status) != 0 and "Nothing was changed" in text
    assert not (lab.home / ".mas").exists()


def test_both_installers_are_english_only_no_spanish_words_reach_the_owner():
    import re
    spanish = re.compile(r"\b(inesperado|error inesperado|instalaci[oó]n|usuario|ejecutar|aprobar|cancelar|siguiente|computadora|equipo)\b", re.I)
    for path in (SH, PS):
        hits = [ln for ln in path.read_text(encoding="utf-8-sig").splitlines() if spanish.search(ln)]
        assert hits == [], (path.name, hits[:3])
