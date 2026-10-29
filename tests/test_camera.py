"""The camera tool: asks every time, returns measurements, keeps frames only in the raw store (deletable), never an image to the model, microphone never opened."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from envrec.contracts import Target, TargetKind
from envrec.discovery import ReplayCollector, Salts
from envrec.store import Store
from envrec.tools import ToolContext, get_tool, run_tool
from envrec.tools.builtin import camera

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg decodes the frames")


def replay(tmp_path, names):
    d = tmp_path / "rec"
    d.mkdir()
    for i, n in enumerate(names, 1):
        (d / f"frame_{i}.out").write_bytes((ROOT / "fixtures" / "camera" / n).read_bytes())
    return d


def ctx(d, approve):
    return ToolContext(collector=ReplayCollector(d, "macos"), platform="macos", salts=Salts(), approve=approve)


def test_it_is_read_tier_and_asks_before_touching_the_camera_with_the_exact_plan(tmp_path):
    from envrec.discovery import LocalCollector
    t = get_tool("capture_frame")
    assert t.tier == "read" and "camera" in t.tags
    shown = []
    c = ToolContext(collector=LocalCollector("macos"), platform="macos", approve=lambda text: (shown.append(text), False)[1])
    r = run_tool(t, c, {"frames": 2})  # a real (local) collector: the human is asked first, and a no means the camera is never opened
    assert r.error.value == "denied" and len(shown) == 1
    assert "microphone off" in shown[0] and "2 still frame" in shown[0] and "never receives an image" in shown[0] and "light will come on" in shown[0]
    assert run_tool(t, ToolContext(collector=LocalCollector("macos"), platform="macos"), {}).error.value == "denied"  # no approver at all: refused


def test_it_returns_numbers_and_keeps_the_pictures_only_as_captures_for_the_raw_store(tmp_path):
    d = replay(tmp_path, ["frame_1.png", "frame_2.png"])
    res = run_tool(get_tool("capture_frame"), ctx(d, lambda t: True), {"frames": 2})
    assert res.ok and [c.name for c in res.captures] == ["measures", "frame_1", "frame_2"]
    m = json.loads(res.captures[0].data)
    assert set(m) >= {"frames", "brightness", "contrast", "sharpness", "motion"} and m["frames"] == 2 and 0 <= m["brightness"] <= 255
    assert res.data is None  # nothing structured but the measures capture: no picture travels as data


def test_measurements_tell_a_flat_gray_frame_from_a_detailed_one():
    flat = camera.measure([(ROOT / "fixtures" / "camera" / "flat.png").read_bytes()])
    busy = camera.measure([(ROOT / "fixtures" / "camera" / "frame_1.png").read_bytes()])
    assert flat["contrast"] < 1 and flat["sharpness"] < 1 and busy["contrast"] > 20 and busy["sharpness"] > flat["sharpness"]
    two = camera.measure([(ROOT / "fixtures" / "camera" / "frame_1.png").read_bytes()] * 2)
    assert two["motion"] == 0.0  # the same frame twice: nothing changed


def test_frames_go_through_the_gate_into_the_raw_store_and_can_be_deleted(tmp_path):
    from envrec.privacy.gate import PrivacyGate
    d = replay(tmp_path, ["frame_1.png"])
    res = run_tool(get_tool("capture_frame"), ctx(d, lambda t: True), {"frames": 1})
    with Store(tmp_path / "s") as st:
        host = st.put_target(Target(kind=TargetKind.host, ring=0, handle="h"))
        gate = PrivacyGate(st, host, Salts(), "camera")
        gate.store_captures(res.captures)
        ref = gate.refs["frame_1"]
        assert st.read_raw(ref)[:4] == b"\x89PNG"
        assert st.delete_raw(ref) and not st.delete_raw(ref)  # deleted on request; deleting twice finds nothing
        with pytest.raises(Exception):
            st.read_raw(ref)


def test_a_missing_camera_or_permission_is_a_typed_unknown_not_a_crash(tmp_path):
    empty = tmp_path / "none"
    empty.mkdir()
    r = run_tool(get_tool("capture_frame"), ctx(empty, lambda t: True), {"frames": 1})
    assert not r.ok and r.error.value == "unavailable"
    assert run_tool(get_tool("capture_frame"), ToolContext(collector=ReplayCollector(empty, "macos"), platform="macos", approve=lambda t: True), {"frames": 9}).ok is False  # asked for 9: clamped to 3, still no recording
    import inspect
    src = inspect.getsource(camera)
    assert "-an" in src or "audio" not in src.lower().replace("microphone", "")  # video only


def test_the_tool_has_no_way_to_find_or_recognize_a_person():
    import inspect
    code = "\n".join(ln for ln in inspect.getsource(camera).splitlines() if not ln.strip().startswith(('"', "#")) and '"""' not in ln).lower()
    for word in ("cascade", "haar", "opencv", "cv2", "recogni", "landmark", "embedding", "face_"):
        assert word not in code, word
