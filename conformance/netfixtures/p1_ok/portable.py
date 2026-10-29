import asyncio
import hashlib
from pathlib import Path

import envrec_os


def protect(path: Path):
    envrec_os.current().protect_private(path)  # the per-OS layer does it
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def wait():
    await asyncio.sleep(0)
