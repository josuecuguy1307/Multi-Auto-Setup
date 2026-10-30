"""Read-only probes for the machine analysis, per operating system (skill `os-probes`). This module is part of the per-OS layer: the only place that runs a program or reads the Windows
registry. WHAT to run and WHERE to look is data in `skills/os-probes/references/probes.json`; the code below runs only fixed argument lists named in that table, without a shell, with a time
limit and an output cap, and parses the output with pure functions (so each is tested from a recorded fixture, per operating system).

Every probe returns `{"facts": {...}, "evidence": [...], "limits": [...]}`. A probe never changes the machine, never reads a secret (a login is judged by a file's EXISTENCE, an API key by
the NAME of its environment variable) and talks only to this machine's own loopback address."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import urllib.request
from collections.abc import Callable
from pathlib import Path

from envrec_os import SKILLS_DIR, family

TABLE_FILE = SKILLS_DIR / "os-probes" / "references" / "probes.json"
MAX_OUT = 400_000
Runner = Callable[[list[str]], str | None]


def table() -> dict:
    return json.loads(TABLE_FILE.read_text())


def run_fixed(argv: list[str], *, timeout: float = 12.0) -> str | None:
    """Run one read-only command from the table: no shell, a time limit, an output cap. None when it is not there, fails or is not on the table's allowed list."""
    if not argv or Path(argv[0]).name.lower().removesuffix(".exe") not in table()["allowed_programs"]:
        return None
    exe = shutil.which(argv[0])
    if exe is None:
        return None
    try:
        done = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout[:MAX_OUT] if done.returncode == 0 else None


def _result(facts: dict, evidence: list, limits: list | None = None) -> dict:
    return {"facts": facts, "evidence": evidence, "limits": limits or []}


# ---- parsers (pure; one fixture per operating system) ---------------------------------------------------------------------------------------------------------------

def parse_tab_rows(text: str) -> list[dict]:
    """`name<TAB>version` lines (dpkg-query, rpm)."""
    out = []
    for ln in text.splitlines():
        name, _, ver = ln.partition("\t")
        if name.strip():
            out.append({"name": name.strip()[:80], "version": ver.strip()[:40]})
    return out


def parse_flatpak(text: str) -> list[dict]:
    return [{"name": p[0][:80], "version": (p[1] if len(p) > 1 else "")[:40]} for p in (ln.split("\t") for ln in text.splitlines()) if p and p[0].strip()]


def parse_registry_rows(rows: list[dict]) -> list[dict]:
    """Rows read from the uninstall keys (DisplayName, DisplayVersion, SystemComponent): a program the owner would call installed."""
    out, seen = [], set()
    for r in rows:
        name = str(r.get("DisplayName", "")).strip()
        if not name or r.get("SystemComponent") == 1 or name in seen:
            continue
        seen.add(name)
        out.append({"name": name[:80], "version": str(r.get("DisplayVersion", ""))[:40]})
    return sorted(out, key=lambda x: x["name"].lower())


def parse_nvidia_smi(text: str) -> list[dict]:
    """`name, memory.total (MiB), driver` per GPU."""
    out = []
    for ln in text.strip().splitlines():
        p = [x.strip() for x in ln.split(",")]
        if len(p) >= 2 and p[0]:
            try:
                vram = round(float(p[1]) / 1024, 1)
            except ValueError:
                vram = None
            out.append({"vendor": "NVIDIA", "name": p[0][:80], "vram_gib": vram, "driver": p[2][:30] if len(p) > 2 else "", "memory": "dedicated", "source": "nvidia-smi"})
    return out


def parse_powershell_gpu(text: str) -> list[dict]:
    """`Get-CimInstance Win32_VideoController | ConvertTo-Json`: a list of objects, or one object. AdapterRAM is a 32-bit field: it cannot say more than 4 GiB, so it is reported as a lower bound."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for g in (data if isinstance(data, list) else [data]):
        name = str(g.get("Name", "")).strip()
        if not name:
            continue
        ram = g.get("AdapterRAM")
        low = name.lower()
        vendor = "NVIDIA" if "nvidia" in low else "AMD" if "amd" in low or "radeon" in low else "Intel" if "intel" in low else "other"
        out.append({"vendor": vendor, "name": name[:80], "vram_gib": round(ram / 2 ** 30, 1) if isinstance(ram, int) and ram > 0 else None,
                    "vram_is_lower_bound": isinstance(ram, int) and ram >= 4 * 2 ** 30 - 2 * 2 ** 20, "driver": str(g.get("DriverVersion", ""))[:30],
                    "memory": "shared" if vendor == "Intel" else "dedicated", "source": "Win32_VideoController"})
    return out


def parse_system_profiler_gpu(text: str) -> list[dict]:
    try:
        data = json.loads(text).get("SPDisplaysDataType", [])
    except ValueError:
        return []
    out = []
    for g in data:
        name = str(g.get("sppci_model") or g.get("_name", "")).strip()
        if not name:
            continue
        vram = g.get("spdisplays_vram") or g.get("spdisplays_vram_shared")
        cores = g.get("sppci_cores")
        apple = str(g.get("spdisplays_vendor", "")).lower().endswith("apple") or name.lower().startswith("apple")
        out.append({"vendor": "Apple" if apple else str(g.get("spdisplays_vendor", "other"))[:30], "name": name[:80], "vram_gib": None if apple else _vram_text(vram), "cores": cores,
                    "memory": "unified" if apple else "dedicated", "source": "system_profiler"})
    return out


def _vram_text(v) -> float | None:
    if not v:
        return None
    parts = str(v).split()
    try:
        n = float(parts[0])
    except (ValueError, IndexError):
        return None
    return round(n / 1024, 1) if len(parts) > 1 and parts[1].upper().startswith("MB") else n


def parse_lspci(text: str) -> list[dict]:
    """`lspci -mm`: the VGA, 3D and display controllers. VRAM is not in this output; it is None."""
    out = []
    for ln in text.splitlines():
        if any(k in ln for k in ('"VGA compatible controller"', '"3D controller"', '"Display controller"')):
            f = [x for x in ln.replace('"', "\u0001").split("\u0001") if x.strip()]
            vendor = f[2].strip() if len(f) > 2 else ""
            name = f[3].strip() if len(f) > 3 else ""
            if name:
                v = "NVIDIA" if "nvidia" in vendor.lower() else "AMD" if "advanced micro" in vendor.lower() or "amd" in vendor.lower() else "Intel" if "intel" in vendor.lower() else vendor[:20]
                out.append({"vendor": v, "name": name[:80], "vram_gib": None, "memory": "shared" if v == "Intel" else "dedicated", "source": "lspci"})
    return out


def parse_models(text: str, fmt: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if fmt == "ollama_tags":
        return [{"name": str(m.get("name", ""))[:60], "size_gib": round(m.get("size", 0) / 2 ** 30, 2)} for m in data.get("models", [])][:50]
    return [{"name": str(m.get("id", ""))[:60]} for m in data.get("data", [])][:50]


# ---- the probes -----------------------------------------------------------------------------------------------------------------------------------------------

def _windows_uninstall_rows() -> list[dict]:
    """Read the uninstall keys (read-only). Only runs on Windows; UNTESTED until a Windows machine verifies it."""
    import winreg  # noqa: PLC0415  (Windows only)
    rows = []
    for path in table()["programs"]["windows"]["registry"]:
        hive_name, _, sub = path.partition("\\")
        hive = winreg.HKEY_LOCAL_MACHINE if hive_name == "HKLM" else winreg.HKEY_CURRENT_USER
        try:
            with winreg.OpenKey(hive, sub) as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                            row = {}
                            for val in ("DisplayName", "DisplayVersion", "SystemComponent"):
                                try:
                                    row[val] = winreg.QueryValueEx(k, val)[0]
                                except OSError:
                                    pass
                            rows.append(row)
                    except OSError:
                        continue
        except OSError:
            continue
    return rows


def installed_programs(*, run: Runner = run_fixed, fam: str | None = None, registry_rows: Callable[[], list[dict]] | None = None, mac_dirs: list[Path] | None = None, limit: int = 400) -> dict:
    fam = fam or family()
    t = table()["programs"].get(fam, {})
    progs: list[dict] = []
    limits: list[str] = []
    source = "none"
    if fam == "windows":
        source = "uninstall registry keys"
        try:
            progs = parse_registry_rows((registry_rows or _windows_uninstall_rows)())
        except Exception as exc:  # no registry on this machine, or no access: said, never guessed
            limits.append(f"registry unreadable: {type(exc).__name__}")
    elif fam == "darwin":
        source = "Applications folders (Info.plist names and versions)"
        for d in (mac_dirs if mac_dirs is not None else [Path(x).expanduser() for x in t.get("dirs", [])]):
            for app in sorted(d.glob("*.app")) if d.is_dir() else []:
                try:
                    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
                    progs.append({"name": str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or app.stem)[:80], "version": str(info.get("CFBundleShortVersionString", ""))[:40]})
                except (OSError, ValueError):
                    progs.append({"name": app.stem[:80], "version": ""})
    else:
        for tool, argv in t.get("commands", {}).items():
            out = run(argv)
            if out:
                source = tool if source == "none" else f"{source}+{tool}"
                progs += parse_flatpak(out) if tool == "flatpak" else parse_tab_rows(out)
        if source == "none":
            limits.append("neither dpkg, rpm nor flatpak answered on this machine")
    return _result({"machine.programs_count": len(progs), "machine.programs": progs[:limit]}, [{"provider": "envrec_os.probes", "tool": "installed_programs", "source": source}], limits)


def gpus(*, run: Runner = run_fixed, fam: str | None = None) -> dict:
    fam = fam or family()
    t = table()["gpu"].get(fam, {})
    found: list[dict] = []
    used = []
    limits = []
    for tool, argv in t.get("commands", {}).items():
        out = run(argv)
        if out:
            used.append(tool)
            found += {"cim": parse_powershell_gpu, "profiler": parse_system_profiler_gpu, "lspci": parse_lspci}[tool](out)
    nv = t.get("nvidia")
    if nv:
        out = run(nv)
        if out:
            used.append("nvidia-smi")
            real = parse_nvidia_smi(out)  # nvidia-smi knows the true VRAM: it replaces the 32-bit-limited or absent figure for NVIDIA entries
            found = [g for g in found if g["vendor"] != "NVIDIA"] + real
    if not found:
        limits.append("no GPU was reported by this machine's tools (or the tools are not installed)")
    for g in found:
        if g.get("vram_is_lower_bound"):
            limits.append(f"{g['name']}: Windows reports at most 4 GiB through its standard query; install the vendor tool (nvidia-smi) for the real figure")
    return _result({"machine.gpu_count": len(found), "machine.gpus": found}, [{"provider": "envrec_os.probes", "tool": "gpus", "used": used}], limits)


def _loopback_get(port: int, path: str, timeout: float = 2.0) -> str | None:
    """The machine's own loopback address only: this probe never contacts anything else."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:  # noqa: S310
            return r.read(200_000).decode("utf-8", "replace")
    except Exception:
        return None


def _bound_beyond_loopback(port: int) -> bool | None:
    """Is a listener on this port reachable from the network (bound to every interface or a LAN address)? Read from this machine's own socket table. None when it cannot be told."""
    try:
        import psutil
        listeners = [c.laddr.ip for c in psutil.net_connections(kind="tcp") if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port]
    except Exception:
        return None
    if not listeners:
        return None
    return any(ip not in ("127.0.0.1", "::1") for ip in listeners)


def model_runtimes(*, which: Callable[[str], str | None] = shutil.which, processes: Callable[[], set[str]] | None = None, get: Callable[[int, str], str | None] = _loopback_get,
                   exposed: Callable[[int], bool | None] = _bound_beyond_loopback) -> dict:
    """Local model runtimes (Ollama, llama.cpp, LM Studio, ...): installed (a binary on the path), running (a process), reachable on loopback, bound to loopback only or not, models present."""
    if processes is None:
        def processes() -> set[str]:
            import psutil
            return {p.info["name"].lower() for p in psutil.process_iter(["name"]) if p.info.get("name")}
    running = processes()
    rows = []
    for rt in table()["runtimes"]:
        binary = next((b for b in rt["binaries"] if which(b)), None)
        proc = next((p for p in rt["processes"] if p.lower() in running), None)
        models_text = get(rt["default_port"], rt["models_path"]) if (proc or binary) else None
        models = parse_models(models_text, rt["models_format"]) if models_text else []
        if binary or proc or models_text:
            rows.append({"id": rt["id"], "name": rt["name"], "installed": bool(binary), "running": bool(proc) or models_text is not None, "port": rt["default_port"],
                         "answers_on_loopback": models_text is not None, "exposed_beyond_loopback": exposed(rt["default_port"]) if models_text is not None else None, "models": models})
    return _result({"machine.runtime_count": len(rows), "machine.runtimes": rows}, [{"provider": "envrec_os.probes", "tool": "model_runtimes", "contacted": "127.0.0.1 only"}],
                   [] if rows else ["no local model runtime was found (none of the table's binaries, processes or default ports)"])


def ai_clis(*, which: Callable[[str], str | None] = shutil.which, exists: Callable[[Path], bool] = lambda p: p.exists(), env: dict | None = None) -> dict:
    """Official AI command-line tools installed, whether they look set up (a file or folder EXISTS: its content is never read), and the NAMES of environment variables that look like an AI key."""
    rows = []
    for c in table()["ai_clis"]:
        binary = next((b for b in c["binaries"] if which(b)), None)
        if binary:
            rows.append({"id": c["id"], "name": c["name"], "kind": c["kind"], "installed": True, "set_up": any(exists(Path(m).expanduser()) for m in c["setup_marks"]),
                         "note": "'set up' means its configuration folder or file exists; no login or token was read"})
    names = key_names(env)
    return _result({"machine.ai_cli_count": len(rows), "machine.ai_clis": rows, "machine.api_key_variable_names": names}, [{"provider": "envrec_os.probes", "tool": "ai_clis"}],
                   [] if rows else ["no official AI command-line tool was found on the path"])


def key_names(env: dict | None = None) -> list[dict]:
    """NAMES (never values) of environment variables that look like an AI provider's key, from the table in the machine-analysis skill."""
    tbl = json.loads((SKILLS_DIR / "machine-analysis" / "references" / "ai-env-names.json").read_text())
    env = os.environ if env is None else env
    out = [{"provider": prov, "name": n} for prov, names in tbl["providers"].items() for n in names if n in env]
    known = {o["name"] for o in out}
    suffixes = tuple(tbl.get("generic_suffixes", []))
    return out + [{"provider": "other", "name": n} for n in sorted(env) if n.endswith(suffixes) and n not in known]


def os_details() -> dict:
    """The operating system as the owner knows it, with the real build: on Windows the kernel is the build number (10.0.26100), not a repeat of the version."""
    import platform
    fam = family()
    if fam == "windows":
        ver = platform.version()
        return _result({"machine.os.name": f"Windows {platform.release()}", "machine.os.version": platform.win32_edition() if hasattr(platform, "win32_edition") else "", "machine.os.build": ver,
                        "machine.os.arch": platform.machine().lower()}, [{"provider": "platform", "tool": "os_details"}])
    name = {"darwin": "macOS", "linux": "Linux"}[fam]
    ver = platform.mac_ver()[0] if fam == "darwin" else ""
    if fam == "linux":
        try:
            rel = dict(ln.split("=", 1) for ln in Path("/etc/os-release").read_text().splitlines() if "=" in ln)
            name, ver = rel.get("NAME", name).strip('"'), rel.get("VERSION_ID", "").strip('"')
        except OSError:
            pass
    return _result({"machine.os.name": name, "machine.os.version": ver, "machine.os.build": platform.release(), "machine.os.arch": platform.machine().lower()}, [{"provider": "platform", "tool": "os_details"}])


def memory_for_models(gpu_list: list[dict] | None = None, *, ram_total_gib: float | None = None, ram_available_gib: float | None = None, disk_free_gib: float | None = None) -> dict:
    """RAM and VRAM available for models, and the largest model that fits WITH the real limit named: vram, ram, disk, or the top of our catalog. A machine with no discrete GPU is said to run on
    its CPU and RAM ("no_gpu_detected"); Apple silicon shares one memory ("unified")."""
    import psutil

    from envrec.sizing import CATALOG_PARAMS_B, disk_needed_gb, ram_cap_params_b
    vm = psutil.virtual_memory()
    ram_total = ram_total_gib if ram_total_gib is not None else round(vm.total / 2 ** 30, 1)
    ram_avail = ram_available_gib if ram_available_gib is not None else round(vm.available / 2 ** 30, 1)
    if disk_free_gib is None:
        disk_free_gib = round(shutil.disk_usage(Path.home()).free / 2 ** 30, 1)
    gpu_list = gpu_list if gpu_list is not None else gpus()["facts"]["machine.gpus"]
    dedicated = [g for g in gpu_list if g.get("memory") == "dedicated" and g.get("vram_gib")]
    unified = any(g.get("memory") == "unified" for g in gpu_list)
    vram = max((g["vram_gib"] for g in dedicated), default=None)
    caps = {"ram": ram_cap_params_b(ram_total), "disk": max((p for p in CATALOG_PARAMS_B if disk_needed_gb(p) <= disk_free_gib), default=0.0)}
    path = "unified" if unified else "gpu" if vram else "cpu"
    if vram:
        caps["vram"] = ram_cap_params_b(vram)
    top = max(CATALOG_PARAMS_B)
    limit_name = min(caps, key=lambda k: caps[k])
    limit = caps[limit_name]
    fit = max((p for p in CATALOG_PARAMS_B if p <= limit), default=0.0)
    if limit >= top:
        limit_name = "catalog_top"  # nothing on this machine stops a bigger model: the biggest we list is the limit
    elif path == "cpu" and limit_name == "ram":
        limit_name = "ram_no_gpu_detected"
    return _result({"machine.ram_total_gib": ram_total, "machine.ram_available_gib": ram_avail, "machine.vram_gib": vram, "machine.model_path": path, "machine.disk_free_gib": disk_free_gib,
                    "machine.largest_model_params_b": fit, "machine.largest_model_limited_by": limit_name},
                   [{"provider": "envrec_os.probes", "tool": "memory_for_models", "table": "workspace-sizing"}],
                   ["on a machine without a discrete GPU, models run on the CPU: slower, limited by RAM"] if path == "cpu" else [])
