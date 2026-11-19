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
MATRIX_SIZES = ("1366x768", "1536x864", "1920x1080")
GRAB_AT_DPR = "1.25"
GRAB_SIZES = {"1366x768", "1920x1080"}
# A physical 1366x768 screen at 125 % / 150 %: must fit, scrolling allowed.
PHYSICAL_1366 = {"1.25": "1093x614", "1.5": "911x512"}
MIN_ROWS_AT_1366 = 12


def run_probe(dpr: str, probe_dir: Path, grab_dir: Path | None) -> list[dict]:
    sizes = [
        f"+{size}" if dpr == GRAB_AT_DPR and size in GRAB_SIZES else size
        for size in MATRIX_SIZES
    ]
    if dpr in PHYSICAL_1366:
        sizes.append(PHYSICAL_1366[dpr])
    env = dict(os.environ, USB_TRACK_ORDER_PROBE_DIR=str(probe_dir))
    completed = subprocess.run(
        [sys.executable, str(PROBE), dpr, str(grab_dir) if grab_dir else "-", *sizes],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        cwd=PROBE.parents[1],
    )
    assert completed.returncode == 0, completed.stderr
    return [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]


@pytest.mark.parametrize("dpr", ["1.0", "1.25", "1.5"])
def test_layout_fits_without_clipping_or_overlap(dpr: str, tmp_path: Path) -> None:
    grab_dir = tmp_path / "grabs" if dpr == GRAB_AT_DPR else None
    results = {r["size"]: r for r in run_probe(dpr, tmp_path, grab_dir)}
    assert set(MATRIX_SIZES) <= set(results)
    for size, result in results.items():
        assert result["dpr"] == pytest.approx(float(dpr))
        assert result["too_small"] == [], (size, result["too_small"])
        assert result["overlaps"] == [], (size, result["overlaps"])
        assert not result["page_hscroll"], size
        if size in MATRIX_SIZES:
            assert not result["page_vscroll"], size
    assert results["1366x768"]["visible_rows"] >= MIN_ROWS_AT_1366
    if grab_dir is not None:
        for size in GRAB_SIZES:
            png = grab_dir / f"window-{size}@{dpr}.png"
            assert png.is_file() and png.stat().st_size > 0
