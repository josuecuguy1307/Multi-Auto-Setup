"""Process-level flags that human-only code checks. The loop sets LOOP_ACTIVE while it runs."""

from __future__ import annotations

import os
from contextvars import ContextVar

LOOP_ACTIVE: ContextVar[bool] = ContextVar("envrec_loop_active", default=False)
WORKER_ENV = "ENVREC_WORKER_BUILD"


def in_worker_build() -> bool:
    return os.environ.get(WORKER_ENV) == "1"
