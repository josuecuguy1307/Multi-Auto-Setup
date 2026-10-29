"""What to launch and with which caps. The flag set per runtime kind lives here, in code, and nowhere else."""

from __future__ import annotations

from dataclasses import dataclass

LOCALHOST = "127.0.0.1"
# Only runtime kinds whose flags have been checked against a real binary are listed. A kind that is in the
# manifest but not here cannot be launched: the worker refuses rather than guess flags.
SUPPORTED_KINDS = ("llama.cpp",)


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeSpec:
    kind: str  # runtime project id from the manifest, e.g. "llama.cpp"
    binary: str  # absolute path of the pinned server binary
    model: str  # absolute path of the model file
    port: int
    threads: int
    context: int
    host: str = LOCALHOST
    max_rss_mib: int | None = None  # the supervisor stops the server if its resident memory crosses this
    min_free_disk_mb: int | None = None  # ... or if free disk on the model's volume drops below this
    startup_timeout_s: float = 120.0

    def validate(self) -> None:
        if self.kind not in SUPPORTED_KINDS:
            raise SpecError(f"runtime kind {self.kind!r} has no verified flag set; supported: {SUPPORTED_KINDS}")
        if self.host != LOCALHOST:
            raise SpecError(f"refusing to bind {self.host!r}: the server listens on {LOCALHOST} only")
        if not 1024 <= self.port <= 65535:
            raise SpecError("port must be 1024 to 65535")
        if self.threads < 1 or self.context < 128:
            raise SpecError("threads must be at least 1 and context at least 128")
        for name in ("binary", "model"):
            if not getattr(self, name).startswith("/"):
                raise SpecError(f"{name} must be an absolute path")


def build_command(spec: RuntimeSpec) -> list[str]:
    """argv for the server. A list, never a shell string; the host is always 127.0.0.1."""
    spec.validate()
    return [spec.binary, "-m", spec.model, "--host", LOCALHOST, "--port", str(spec.port),
            "-c", str(spec.context), "-t", str(spec.threads), "--parallel", "1"]
