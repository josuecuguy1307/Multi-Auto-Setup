import os
import subprocess
import sys
import winreg
from fcntl import flock


def write_key(path):
    os.chmod(path, 0o600)
    if sys.platform == "win32":
        subprocess.run(["icacls", path, "/inheritance:r"])
    return os.getuid()
