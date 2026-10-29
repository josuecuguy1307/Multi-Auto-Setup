"""Test options. Real-model tests download real artifacts, so they are opt-in and skipped by default."""

import pytest


def pytest_addoption(parser):
    parser.addoption("--live-model", action="store_true", default=False,
                     help="run the tests that download the real runtime and a real small model (needs approved: true in the manifest)")
    parser.addoption("--live-tier", default="qwen2.5-0.5b-q4km",
                     help="which smoke-set model the live tests use; the default is the smallest")


def pytest_configure(config):
    config.addinivalue_line("markers", "live_model: downloads the real runtime and model; needs --live-model")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live-model"):
        return
    skip = pytest.mark.skip(reason="real-model test: pass --live-model (downloads about 0.5 GB; needs approved: true in the manifest)")
    for item in items:
        if "live_model" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _helper_files_stay_in_a_temp_dir(tmp_path, monkeypatch):
    """No test may write to the owner's real helper directory (its approval log lives there)."""
    monkeypatch.setenv("ENVREC_HELPER_DIR", str(tmp_path / "helper-dir"))
