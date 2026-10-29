"""The camera, as a tool: asks every time, shows what it will do, takes one to three still frames, and returns MEASUREMENTS, never the picture.

Why it is shaped this way. A camera is the one sensor that can see a person. So: it is read tier and asks the human on every use, with the exact plan (the camera light comes
on, how many frames, what is computed, where the frames go); the frames are written only to the run's raw store, by hash, where the human can have them deleted; the numbers are
computed by code (brightness, contrast, sharpness, how much changed between frames) and those numbers are all that reaches facts; nothing here detects, counts or identifies a
person; the model never receives an image; and the microphone is never opened (the capture is video-only)."""

from __future__ import annotations

import json
import re
import shutil
import statistics
import subprocess
import tempfile
from pathlib import Path

from envrec.discovery.types import Capture, Reason
from envrec.tools.ctx import ToolContext, ToolResult
from envrec.tools.registry import tool

W, H = 64, 48  # frames are measured at this size: enough for light and focus, too small for anything else


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def gray_pixels(png: bytes) -> bytes | None:
    """The frame as W x H grayscale bytes, decoded by ffmpeg from the PNG on stdin. None if ffmpeg cannot read it."""
    ff = ffmpeg_path()
    if ff is None:
        return None
    done = subprocess.run([ff, "-loglevel", "error", "-i", "pipe:0", "-vf", f"scale={W}:{H},format=gray", "-f", "rawvideo", "pipe:1"], input=png, capture_output=True, timeout=20)
    return done.stdout if done.returncode == 0 and len(done.stdout) == W * H else None


def laplacian_variance(px: bytes) -> float:
    vals = []
    for y in range(1, H - 1):
        for x in range(1, W - 1):
            i = y * W + x
            vals.append(4 * px[i] - px[i - 1] - px[i + 1] - px[i - W] - px[i + W])
    return statistics.pvariance(vals)


def measure(frames_png: list[bytes]) -> dict:
    """Numbers about the frames. No detection of anything in them."""
    grays = [gray_pixels(f) for f in frames_png]
    if not grays or any(g is None for g in grays):
        raise ValueError("a frame could not be decoded")
    out = {"frames": len(grays), "width": W, "height": H,
           "brightness": round(statistics.fmean(statistics.fmean(g) for g in grays), 1),
           "contrast": round(statistics.fmean(statistics.pstdev(g) for g in grays), 1),
           "sharpness": round(statistics.fmean(laplacian_variance(g) for g in grays), 1)}
    if len(grays) > 1:
        out["motion"] = round(statistics.fmean(statistics.fmean(abs(a - b) for a, b in zip(g1, g2, strict=True)) for g1, g2 in zip(grays, grays[1:], strict=False)), 2)
    return out


def _approval(ctx: ToolContext, frames: int = 1, camera: int = 0) -> str:
    return (f"turn on camera {camera} (its light will come on) and take {frames} still frame(s) with the microphone off. The frames are kept only in this run's raw store, by hash, "
            "and you can have them deleted. Code computes brightness, contrast, sharpness and (for more than one frame) how much changed between frames. "
            "Nothing detects or identifies a person, and the model never receives an image.")


@tool(tags=("hardware", "camera", "active", "generic_read"), approval=_approval)
def capture_frame(ctx: ToolContext, frames: int = 1, camera: int = 0) -> ToolResult:
    """Take up to three still frames and report measurements only.

    Args:
        frames: how many frames, 1 to 3.
        camera: the camera index (0 is the first).
    Returns:
        A capture named measures (JSON: frames, brightness, contrast, sharpness, motion) and a capture named frame_1 (and frame_2, frame_3) holding the PNG bytes for the raw store only.
    Tier: read
    Platforms: macos, linux
    Notes: asks on every use; video only; stored by hash in the raw store, deletable on request; no detection or identification of people; the model sees numbers, never an image.
    """
    n = max(1, min(3, int(frames)))
    if ctx.collector.origin == "remote":
        return ToolResult.fail(Reason.not_applicable, "the camera is read on this machine only")
    if ctx.collector.origin == "replay":
        pngs = [c for i in range(1, n + 1) if (c := ctx.collector.read_file(f"frame_{i}", "camera")).ok]
        if not pngs:
            return ToolResult.fail(Reason.unavailable, "no recorded frame")
        raw = [c.data for c in pngs]
        caps = pngs
    else:
        ff = ffmpeg_path()
        if ff is None:
            return ToolResult.fail(Reason.unavailable, "ffmpeg is not installed, so there is no way to read the camera")
        raw, caps = [], []
        with tempfile.TemporaryDirectory(prefix="envrec-cam-") as d:
            for i in range(1, n + 1):
                f = Path(d) / f"f{i}.png"
                argv = ([ff, "-loglevel", "error", "-f", "avfoundation", "-framerate", "30", "-i", f"{camera}", "-frames:v", "1", "-y", str(f)] if ctx.platform == "macos"
                        else [ff, "-loglevel", "error", "-f", "v4l2", "-i", f"/dev/video{camera}", "-frames:v", "1", "-y", str(f)])
                done = subprocess.run(argv, capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
                err = done.stderr.decode("utf-8", "replace")
                if done.returncode != 0 or not f.is_file():
                    if re.search(r"not authori[sz]ed|permission|denied|TCC", err, re.I):
                        return ToolResult.fail(Reason.permission, "System Settings > Privacy & Security > Camera: allow the program that runs this (Terminal or iTerm), then run it again")
                    return ToolResult.fail(Reason.unavailable, (err.strip().splitlines() or ["the camera gave no frame"])[-1][:160])
                data = f.read_bytes()
                raw.append(data)
                caps.append(Capture(f"frame_{i}", f"camera {camera}", data, 0, "local", ctx.platform))
    try:
        m = measure(raw)
    except ValueError as exc:
        return ToolResult.fail(Reason.unavailable, str(exc))
    return ToolResult(captures=[Capture("measures", "camera frames measured", json.dumps(m).encode(), 0, "local", ctx.platform), *caps])
