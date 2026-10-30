"""The read-only probes of the machine analysis, per operating system (skill os-probes). Parsers and probes are tested from recorded fixtures under fixtures/probes (the Windows ones are UNTESTED against a real Windows machine, see the README there)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import envrec_os
from envrec_os import probes as P

FX = Path(__file__).resolve().parent.parent / "fixtures" / "probes"


def text(*parts):
    return FX.joinpath(*parts).read_text()


def test_dpkg_and_rpm_rows_become_installed_programs():
    deb = P.parse_tab_rows(text("linux-debian", "dpkg.tsv"))
    assert {"name": "firefox-esr", "version": "128.4.0esr-1~deb12u1"} in deb and len(deb) == 5
    rpm = P.parse_tab_rows(text("linux-fedora", "rpm.tsv"))
    assert {"name": "firefox", "version": "131.0"} in rpm
    assert P.parse_flatpak(text("linux-debian", "flatpak.tsv")) == [{"name": "org.gimp.GIMP", "version": "2.10.38"}, {"name": "org.videolan.VLC", "version": "3.0.21"}]


def test_installed_programs_on_debian_and_fedora_read_only_the_tables_commands():
    asked = []

    def run(argv):
        asked.append(argv[0])
        return {"dpkg-query": text("linux-debian", "dpkg.tsv"), "rpm": None, "flatpak": text("linux-debian", "flatpak.tsv")}.get(argv[0])
    deb = P.installed_programs(run=run, fam="linux")
    assert deb["facts"]["machine.programs_count"] == 7 and asked == ["dpkg-query", "rpm", "flatpak"] and deb["evidence"][0]["source"] == "dpkg+flatpak" and deb["limits"] == []
    fed = P.installed_programs(run=lambda argv: text("linux-fedora", "rpm.tsv") if argv[0] == "rpm" else None, fam="linux")
    assert fed["facts"]["machine.programs_count"] == 5 and fed["evidence"][0]["source"] == "rpm"
    none = P.installed_programs(run=lambda argv: None, fam="linux")
    assert none["facts"]["machine.programs_count"] == 0 and none["limits"]  # said, not guessed


def test_windows_registry_rows_drop_system_components_duplicates_and_nameless_entries():
    rows = json.loads(text("windows", "registry_rows.json"))
    progs = P.parse_registry_rows(rows)
    assert [p["name"] for p in progs] == ["Mozilla Firefox (x64 en-US)", "Ollama", "Python 3.13.1 (64-bit)"]
    res = P.installed_programs(fam="windows", registry_rows=lambda: rows)
    assert res["facts"]["machine.programs_count"] == 3 and res["evidence"][0]["source"] == "uninstall registry keys"
    def broken():
        raise OSError("no registry")
    bad = P.installed_programs(fam="windows", registry_rows=broken)
    assert bad["facts"]["machine.programs_count"] == 0 and "registry unreadable" in bad["limits"][0]


def test_macos_applications_are_read_from_their_info_plists(tmp_path):
    import plistlib
    app = tmp_path / "Example.app" / "Contents"
    app.mkdir(parents=True)
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleName": "Example", "CFBundleShortVersionString": "2.3"}))
    (tmp_path / "Broken.app").mkdir()
    res = P.installed_programs(fam="darwin", mac_dirs=[tmp_path])
    assert {"name": "Example", "version": "2.3"} in res["facts"]["machine.programs"] and {"name": "Broken", "version": ""} in res["facts"]["machine.programs"]


def test_gpus_per_operating_system_from_their_own_tools_output():
    win = P.parse_powershell_gpu(text("windows", "gpu_cim_list.json"))
    nv = next(g for g in win if g["vendor"] == "NVIDIA")
    assert nv["vram_is_lower_bound"] is True and nv["vram_gib"] == 4.0 and next(g for g in win if g["vendor"] == "Intel")["memory"] == "shared"
    assert P.parse_powershell_gpu(text("windows", "gpu_cim_single.json"))[0]["name"].startswith("Intel(R) UHD")
    assert P.parse_powershell_gpu("not json") == []
    assert P.parse_nvidia_smi(text("windows", "nvidia_smi.csv"))[0] == {"vendor": "NVIDIA", "name": "NVIDIA GeForce RTX 3050 Laptop GPU", "vram_gib": 4.0, "driver": "552.22", "memory": "dedicated", "source": "nvidia-smi"}
    mac = P.parse_system_profiler_gpu(text("darwin", "system_profiler_gpu.json"))
    assert mac and mac[0]["memory"] == "unified" and mac[0]["vendor"] == "Apple"
    lin = P.parse_lspci(text("linux-debian", "lspci_nvidia.txt"))
    assert [(g["vendor"], g["memory"]) for g in lin] == [("Intel", "shared"), ("NVIDIA", "dedicated")] and "RTX 3050" in lin[1]["name"]
    assert P.parse_lspci(text("linux-fedora", "lspci_amd.txt"))[0]["vendor"] == "AMD"


def test_the_nvidia_tool_replaces_the_32_bit_limited_windows_figure_and_a_missing_gpu_is_said():
    def run(argv):
        return {"powershell": text("windows", "gpu_cim_list.json"), "nvidia-smi": "NVIDIA GeForce RTX 3050 Laptop GPU, 6144, 552.22"}.get(argv[0])
    res = P.gpus(run=run, fam="windows")
    nv = [g for g in res["facts"]["machine.gpus"] if g["vendor"] == "NVIDIA"]
    assert len(nv) == 1 and nv[0]["vram_gib"] == 6.0 and not any("4 GiB" in x for x in res["limits"])
    only_cim = P.gpus(run=lambda argv: text("windows", "gpu_cim_list.json") if argv[0] == "powershell" else None, fam="windows")
    assert any("at most 4 GiB" in x for x in only_cim["limits"])
    nothing = P.gpus(run=lambda argv: None, fam="linux")
    assert nothing["facts"]["machine.gpu_count"] == 0 and "no GPU" in nothing["limits"][0]


def test_model_runtimes_installed_running_loopback_exposure_and_models():
    running = {"ollama", "llama-server"}
    answers = {11434: text("runtimes", "ollama_tags.json"), 8080: text("runtimes", "openai_models.json")}
    res = P.model_runtimes(which=lambda b: "/usr/bin/" + b if b in ("ollama",) else None, processes=lambda: running, get=lambda port, path: answers.get(port), exposed=lambda port: port == 8080)
    by = {r["id"]: r for r in res["facts"]["machine.runtimes"]}
    assert by["ollama"]["installed"] and by["ollama"]["running"] and [m["name"] for m in by["ollama"]["models"]] == ["llama3.2:3b", "qwen2.5:7b"] and by["ollama"]["exposed_beyond_loopback"] is False
    assert by["llama.cpp"]["installed"] is False and by["llama.cpp"]["running"] and by["llama.cpp"]["exposed_beyond_loopback"] is True  # bound beyond loopback: said
    assert "lm-studio" not in by
    nothing = P.model_runtimes(which=lambda b: None, processes=lambda: set(), get=lambda p, q: None)
    assert nothing["facts"]["machine.runtime_count"] == 0 and nothing["limits"]


def test_the_runtime_probe_only_ever_contacts_loopback(monkeypatch):
    seen = []

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"{}"
    monkeypatch.setattr(P.urllib.request, "urlopen", lambda url, timeout=0: seen.append(url) or R())
    P._loopback_get(11434, "/api/tags")
    assert seen == ["http://127.0.0.1:11434/api/tags"]


def test_ai_clis_are_installed_or_not_set_up_by_file_existence_and_a_key_value_never_appears():
    res = P.ai_clis(which=lambda b: "/bin/" + b if b in ("claude", "codex") else None, exists=lambda p: p.name == ".claude",
                    env={"OPENAI_API_KEY": "sk-secret-value-9999", "HOME": "/x", "MYCO_API_KEY": "another-secret", "PATH": "/bin"})
    rows = {r["id"]: r for r in res["facts"]["machine.ai_clis"]}
    assert rows["claude"]["set_up"] is True and rows["codex"]["set_up"] is False and "gemini" not in rows
    names = res["facts"]["machine.api_key_variable_names"]
    assert {"provider": "openai", "name": "OPENAI_API_KEY"} in names and {"provider": "other", "name": "MYCO_API_KEY"} in names
    assert "sk-secret-value-9999" not in repr(res) and "another-secret" not in repr(res)


def test_the_ai_cli_probe_never_opens_a_login_file():
    import inspect
    src = inspect.getsource(P.ai_clis)
    assert "read_text" not in src and "open(" not in src and "read_bytes" not in src


@pytest.mark.parametrize("name,kw,limited_by,path", [
    ("nvidia 6 GiB", dict(gpu_list=[{"vendor": "NVIDIA", "vram_gib": 6.0, "memory": "dedicated"}], ram_total_gib=32, disk_free_gib=500), "vram", "gpu"),
    ("no gpu 8 GiB", dict(gpu_list=[], ram_total_gib=8, disk_free_gib=500), "ram_no_gpu_detected", "cpu"),
    ("apple unified 16", dict(gpu_list=[{"vendor": "Apple", "vram_gib": None, "memory": "unified"}], ram_total_gib=16, disk_free_gib=500), "catalog_top", "unified"),
    ("big machine", dict(gpu_list=[{"vendor": "NVIDIA", "vram_gib": 48.0, "memory": "dedicated"}], ram_total_gib=128, disk_free_gib=2000), "catalog_top", "gpu"),
    ("small disk", dict(gpu_list=[], ram_total_gib=64, disk_free_gib=3), "disk", "cpu"),
])
def test_the_largest_model_that_fits_names_the_real_limit_never_none(name, kw, limited_by, path):
    f = P.memory_for_models(**kw)["facts"]
    assert f["machine.largest_model_limited_by"] == limited_by and f["machine.model_path"] == path and f["machine.largest_model_limited_by"] != "none", name


def test_a_command_that_is_not_on_the_tables_list_is_never_run():
    assert P.run_fixed(["rm", "-rf", "/tmp/x"]) is None
    assert P.run_fixed(["sh", "-c", "id"]) is None
    assert P.run_fixed([]) is None


def test_os_details_on_this_machine_gives_a_real_build_and_windows_kernel_is_not_the_release(monkeypatch):
    d = P.os_details()["facts"]
    assert d["machine.os.name"] and d["machine.os.arch"] and d["machine.os.build"]
    import platform
    monkeypatch.setattr(P, "family", lambda: "windows")
    monkeypatch.setattr(platform, "release", lambda: "11")
    monkeypatch.setattr(platform, "version", lambda: "10.0.26100")
    w = P.os_details()["facts"]
    assert w["machine.os.name"] == "Windows 11" and w["machine.os.build"] == "10.0.26100" and w["machine.os.build"] != "11"
    from envrec.probes import ring0
    monkeypatch.setattr(ring0.platform, "system", lambda: "Windows")
    assert ring0.read_os().values["os.kernel"] == "10.0.26100" and ring0.read_os().values["os.version"] == "11"  # the real build, not a repeat of the version


def test_every_table_row_is_complete():
    t = P.table()
    assert set(t["programs"]) == {"windows", "darwin", "linux"} and set(t["gpu"]) == {"windows", "darwin", "linux"}
    assert all({"id", "binaries", "processes", "default_port", "models_path", "models_format"} <= set(r) for r in t["runtimes"])
    assert all({"id", "binaries", "setup_marks", "kind"} <= set(c) for c in t["ai_clis"])
    for gpu in t["gpu"].values():
        for argv in gpu.get("commands", {}).values():
            assert P.Path(argv[0]).name in t["allowed_programs"]
