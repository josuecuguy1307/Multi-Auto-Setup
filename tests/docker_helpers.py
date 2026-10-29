"""Helpers for tests that need real containers. They skip cleanly when Docker or the fixture images are absent."""

from __future__ import annotations

import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest


def _sh(*argv: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def docker_ready(*images: str) -> bool:
    try:
        if _sh("docker", "info", timeout=20).returncode != 0:
            return False
        # `docker image inspect` is unreliable on the containerd image store; `docker images -q` is not
        return all(_sh("docker", "images", "-q", i).stdout.strip() != "" for i in images)
    except (OSError, subprocess.SubprocessError):
        return False


def need_docker(*images: str):
    return pytest.mark.skipif(not docker_ready(*images), reason="Docker or the fixture images are not available "
                              "(docker build -f tests/docker/Dockerfile.sshd / Dockerfile.nopy)")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_keypair(directory: Path) -> tuple[str, str]:
    """A throwaway test key pair, generated for the test and never reused."""
    priv = directory / "test_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "envrec-test", "-f", str(priv)], check=True)
    return str(priv), (directory / "test_key.pub").read_text().strip()


@contextmanager
def container(image: str, *docker_args: str, command: list[str] | None = None, env: dict[str, str] | None = None) -> Iterator[str]:
    name = f"envrec-t-{uuid.uuid4().hex[:8]}"
    args = ["docker", "run", "-d", "--rm", "--name", name, *docker_args]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    args += [image, *(command or [])]
    done = _sh(*args, timeout=180)
    if done.returncode != 0:
        raise RuntimeError(f"docker run failed: {done.stderr.strip()[:300]}")
    try:
        yield name
    finally:
        _sh("docker", "rm", "-f", name, timeout=60)


def wait_ssh(port: int, key: str, known_hosts: str, timeout: float = 40) -> None:
    end = time.time() + timeout
    while time.time() < end:
        r = _sh("ssh", "-i", key, "-p", str(port), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={known_hosts}", "-o", "ConnectTimeout=3", "owner@127.0.0.1", "true", timeout=15)
        if r.returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError("sshd in the test container did not come up")


def exec_in(name: str, *cmd: str) -> subprocess.CompletedProcess:
    return _sh("docker", "exec", name, *cmd)
