"""Scripted journeys through a real terminal (pty, stdlib only).

SIMULATED-LOCAL: every journey drives `mas preview`, which uses fake
computers and never writes. macOS/Linux pty only; Windows is UNTESTED.
"""
import os
import re
import struct
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not hasattr(os, "openpty"), reason="pty journeys need a POSIX terminal")

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b[()][0-9A-B]")


def drive(keys, *, env_extra=None, cols=80):
    import fcntl
    import pty
    import select
    import termios

    try:
        master, slave = pty.openpty()
    except OSError:
        pytest.skip("this environment cannot open a pty (sandbox); run outside the sandbox")
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, cols, 0, 0))
    env = {**os.environ, "PYTHONPATH": "src", "TERM": "xterm-256color", **(env_extra or {})}
    proc = subprocess.Popen([sys.executable, "-m", "envrec_mas", "preview"],
                            stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
    os.close(slave)
    out = b""
    try:
        for key in keys:
            end = time.monotonic() + 3
            while time.monotonic() < end:
                r, _, _ = select.select([master], [], [], 0.2)
                if r:
                    try:
                        out += os.read(master, 65536)
                    except OSError:
                        break
                    break
            os.write(master, key)
        end = time.monotonic() + 3
        while time.monotonic() < end and proc.poll() is None:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    out += os.read(master, 65536)
                except OSError:
                    break
        if proc.poll() is None:
            os.write(master, b"q")
            proc.wait(timeout=3)
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
    return ANSI.sub("", out.decode("utf-8", "replace"))


def test_journey_first_run_home_is_clear():
    text = drive([b"q"])
    assert "PREVIEW" in text and "Coordinator:" in text and "Running" in text
    assert "This computer:" in text and "Computers connected:" in text


def test_journey_switch_computer_and_help():
    text = drive([b"\r", b"\x1b[B", b"\r", b"\r", b"?", b"\x1b", b"\x1b", b"q"])
    assert "target for the next action" in text and "Help:" in text


def test_journey_add_computer_help():
    text = drive([b"\r", b"\x1b[B", b"\x1b[B", b"\x1b[B", b"\r", b"\x1b", b"\x1b", b"q"])
    assert "Add a computer" in text and "Add a computer to Multi Auto Setup." in text


def test_journey_preview_write_is_refused_by_the_preview_itself():
    # Computers -> Lenovo -> Forget -> Yes, forget it: preview changes nothing.
    text = drive([b"\r", b"\x1b[B", b"\r", b"\x1b[B", b"\x1b[B", b"\r", b"\x1b[B", b"\r", b"q"])
    assert "Preview only: nothing was changed" in text


def test_journey_no_color_keeps_every_word():
    text = drive([b"\r", b"q"], env_extra={"NO_COLOR": "1"})
    assert "Lenovo" in text and "Online" in text and "Offline" in text


def test_journey_80_columns():
    text = drive([b"\r", b"q"], cols=80)
    assert "Computers" in text and "Old PC" in text
