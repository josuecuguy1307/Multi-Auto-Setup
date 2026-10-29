"""The worker's model runtime: launches an external, pinned inference server as a child process.

Standard library only. The inference server is the only external binary. Nothing here downloads anything,
chooses a binary, or accepts a bind address other than 127.0.0.1.
"""

from envrec_worker.runtime.spec import RuntimeSpec, SpecError, build_command
from envrec_worker.runtime.supervisor import RuntimeFailed, Status, Supervisor

__all__ = ["RuntimeFailed", "RuntimeSpec", "SpecError", "Status", "Supervisor", "build_command"]
