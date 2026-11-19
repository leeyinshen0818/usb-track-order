"""Offscreen layout matrix: every DPR runs in its own process (QT_SCALE_FACTOR
is process-wide). Sizes are logical; see layout_probe.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).with_name("layout_probe.py")
DPRS = ("1.0", "1.25", "1.5", "1.75", "2.0")
MATRIX_SIZES = ("1366x768", "1536x864", "1920x1080")
# Real screens in logical pixels: must fit without clipping; scrolling allowed.
#   1366x768 at 125 % / 150 %, and 1920x1080 at 150 %.
PHYSICAL_SCREENS = {"1.25": ("1093x614",), "1.5": ("911x512", "1280x720")}
GRABS = {"1.25": {"1536x864"}, "1.5": {"1280x720"}}
MIN_ROWS_AT_1366 = 12
MAX_PHYSICAL_SIZE_DIFFERENCE = 0.10  # 150 % vs the 125 % baseline


def run_probe(dpr: str, probe_dir: Path, grab_dir: Path) -> dict[str, dict]:
    sizes = [*MATRIX_SIZES, *PHYSICAL_SCREENS.get(dpr, ())]
    grabbed = GRABS.get(dpr, set())
    args = [f"+{size}" if size in grabbed else size for size in sizes]
    env = dict(os.environ, USB_TRACK_ORDER_PROBE_DIR=str(probe_dir))
    completed = subprocess.run(
        [sys.executable, str(PROBE), dpr, str(grab_dir), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        cwd=PROBE.parents[1],
    )
    assert completed.returncode == 0, completed.stderr
    results = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    return {result["size"]: result for result in results}


@pytest.fixture(scope="module")
def probe_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("layout")


@pytest.fixture(scope="module")
def probe_results(probe_dir: Path) -> dict[str, dict[str, dict]]:
    return {
        dpr: run_probe(dpr, probe_dir / f"dpr-{dpr}", probe_dir / "grabs")
        for dpr in DPRS
    }


@pytest.mark.parametrize("dpr", DPRS)
def test_layout_fits_without_clipping_or_overlap(dpr: str, probe_results) -> None:
    results = probe_results[dpr]
    assert set(MATRIX_SIZES) <= set(results)
    for size, result in results.items():
        assert result["dpr"] == pytest.approx(float(dpr))
        assert result["too_small"] == [], (size, result["too_small"])
        assert result["overlaps"] == [], (size, result["overlaps"])
        assert not result["page_hscroll"], size
        if size in MATRIX_SIZES:
            assert not result["page_vscroll"], size
    assert results["1366x768"]["visible_rows"] >= MIN_ROWS_AT_1366


@pytest.mark.parametrize("key", ["button_height_px", "font_height_px"])
def test_physical_size_at_150_matches_125_baseline(key: str, probe_results) -> None:
    baseline = probe_results["1.25"]["1536x864"][key]
    at_150 = probe_results["1.5"]["1536x864"][key]
    assert abs(at_150 - baseline) / baseline <= MAX_PHYSICAL_SIZE_DIFFERENCE, (
        key, baseline, at_150,
    )


def test_screenshots_saved(probe_results, probe_dir: Path) -> None:
    for dpr, sizes in GRABS.items():
        for size in sizes:
            png = probe_dir / "grabs" / f"window-{size}@{dpr}.png"
            assert png.is_file() and png.stat().st_size > 0, png
