"""What the pairing step knows about a device: the numbers that bound the choice, and its signature."""

from __future__ import annotations

from dataclasses import dataclass

ARCH = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64", "x64": "amd64"}  # the names the operating systems use for the same two architectures
OS = {"linux": "linux", "darwin": "macos", "macos": "macos", "windows": "windows"}
KNOWN_ACCEL = {"none", "nvidia", "rocm", "dri", "metal"}
RAM_BRACKETS = [(2.0, "<2"), (4.0, "2-4"), (8.0, "4-8"), (16.0, "8-16")]


def ram_bracket(ram_gib: float) -> str:
    return next((label for top, label in RAM_BRACKETS if ram_gib < top), "16+")


@dataclass(frozen=True)
class DeviceProfile:
    target_id: str
    os: str  # linux | macos | the raw name when unknown
    arch: str  # arm64 | amd64 | the raw name when unknown
    ram_gib: float  # the stricter of reported and cgroup-limited RAM
    free_disk_gb: float
    accel: str  # none | nvidia | rocm | dri | metal | raw
    cpu_threads: int = 1
    max_disk_gb: float = 1e9  # from the target's limits (targets.yaml), the stricter of these and the device wins
    max_params_b: float = 1e9

    @classmethod
    def from_preflight(cls, target_id: str, pf, limits=None) -> DeviceProfile:
        return cls(target_id=target_id, os=OS.get(pf.os_name.lower(), pf.os_name.lower()), arch=ARCH.get(pf.arch.lower(), pf.arch.lower()),
                   ram_gib=pf.ram_effective_gib, free_disk_gb=pf.disk_free_gb, accel=pf.accel, cpu_threads=pf.cpus,
                   max_disk_gb=limits.max_disk_gb if limits else 1e9, max_params_b=limits.params_b if limits else 1e9)

    @property
    def signature(self) -> dict[str, str]:
        """Key for a saved result: a result carries over only to a device with the same signature, and must re-verify anywhere else."""
        return {"arch": self.arch, "os": self.os, "ram_bracket": ram_bracket(self.ram_gib), "accelerator": self.accel}

    @property
    def signature_key(self) -> str:
        s = self.signature
        return f"{s['os']}-{s['arch']}-ram{s['ram_bracket']}-{s['accelerator']}".replace("<", "lt").replace("+", "plus")

    @property
    def unfamiliar_reasons(self) -> list[str]:
        out = []
        if self.os not in OS.values():
            out.append(f"operating system {self.os!r} is not one the catalog knows")
        if self.arch not in ARCH.values():
            out.append(f"architecture {self.arch!r} is not one the catalog knows")
        if self.accel not in KNOWN_ACCEL:
            out.append(f"accelerator {self.accel!r} is not one the rule table knows")
        return out
