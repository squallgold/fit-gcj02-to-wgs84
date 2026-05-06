"""Regression tests for fit-gcj02-to-wgs84.

Uses real test data:
- ~/Downloads/夏羌拉.gpx — original WGS84 from 两步路 iOS app
- ~/Downloads/夏羌拉.fit — same route after Connect China edit (GCJ-02)

The forward case asserts: converting the FIT (GCJ-02) → WGS84 produces
trackpoints matching the original GPX (WGS84) within ~1cm (FIT semicircle
encoding precision floor).

These tests will be skipped if the test data files are not present.
"""
import math
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "fit-gcj02-to-wgs84.py")

GPX_FIXTURE = os.path.expanduser("~/Downloads/夏羌拉.gpx")
FIT_FIXTURE = os.path.expanduser("~/Downloads/夏羌拉.fit")

requires_fixtures = pytest.mark.skipif(
    not (os.path.isfile(GPX_FIXTURE) and os.path.isfile(FIT_FIXTURE)),
    reason="Test fixtures 夏羌拉.gpx/fit not in ~/Downloads/",
)


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def read_gpx_trackpoints(path):
    tree = ET.parse(path)
    pts = []
    for trkpt in tree.iter("{http://www.topografix.com/GPX/1/1}trkpt"):
        pts.append((float(trkpt.attrib["lat"]), float(trkpt.attrib["lon"])))
    return pts


def read_fit_trackpoints(path):
    from fit_tool.fit_file import FitFile
    from fit_tool.profile.messages.record_message import RecordMessage
    ff = FitFile.from_file(path)
    pts = []
    for record in ff.records:
        msg = getattr(record, "message", None)
        if isinstance(msg, RecordMessage):
            if msg.position_lat is not None and msg.position_long is not None:
                pts.append((msg.position_lat, msg.position_long))
    return pts


def run_cli(*args, expect_exit=0):
    """Run the CLI script and return (stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True,
        text=True,
    )
    assert result.returncode == expect_exit, (
        f"Expected exit {expect_exit}, got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout, result.stderr


# ----- Algorithm-only tests (no fixtures needed) -----

def test_inverse_roundtrip_in_china():
    """Forward + inverse should return original to <1e-9 deg precision."""
    sys.path.insert(0, REPO_ROOT)
    # Hyphenated module name needs importlib
    import importlib.util
    spec = importlib.util.spec_from_file_location("fit_gcj02_to_wgs84", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    test_points = [
        (31.068186, 101.340325),  # 四川甘孜
        (39.908857, 116.397458),  # 北京天安门
        (22.302711, 114.177216),  # 香港
        (43.825592, 87.616848),   # 乌鲁木齐
    ]
    for lat, lon in test_points:
        gcj_lat, gcj_lon = mod.wgs84_to_gcj02(lat, lon)
        back_lat, back_lon = mod.gcj02_to_wgs84(gcj_lat, gcj_lon)
        assert abs(back_lat - lat) < 1e-9
        assert abs(back_lon - lon) < 1e-9


def test_out_of_china_unchanged():
    sys.path.insert(0, REPO_ROOT)
    import importlib.util
    spec = importlib.util.spec_from_file_location("fit_gcj02_to_wgs84", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Tokyo, NYC, London — outside China bounds, must pass through unchanged
    for lat, lon in [(35.6895, 139.6917), (40.7128, -74.0060), (51.5074, -0.1278)]:
        assert mod.wgs84_to_gcj02(lat, lon) == (lat, lon)
        assert mod.gcj02_to_wgs84(lat, lon) == (lat, lon)


# ----- End-to-end CLI tests (require fixtures) -----

@requires_fixtures
def test_e2e_forward_conversion(tmp_path):
    """Convert 夏羌拉.fit (GCJ-02) and confirm output matches 夏羌拉.gpx (WGS84)."""
    output = tmp_path / "out.wgs84.fit"
    stdout, _ = run_cli(FIT_FIXTURE, "-o", str(output))

    assert output.exists(), "Output file not created"
    assert output.stat().st_size == os.path.getsize(FIT_FIXTURE), (
        "Output size should equal input size (in-place byte-level conversion)"
    )
    assert "Converted" in stdout

    gpx_pts = read_gpx_trackpoints(GPX_FIXTURE)
    fit_pts = read_fit_trackpoints(str(output))
    assert len(fit_pts) > 1000, "Sanity: should have many trackpoints"

    # For each FIT trackpoint, find nearest GPX trackpoint. Average distance
    # should be near the FIT semicircle precision floor (~1cm).
    sample_n = min(50, len(fit_pts))
    distances = []
    for i in range(sample_n):
        flat, flon = fit_pts[i]
        d = min(haversine_m(flat, flon, glat, glon) for glat, glon in gpx_pts)
        distances.append(d)
    avg = sum(distances) / len(distances)
    assert avg < 0.05, f"Average residual {avg:.4f}m exceeds 5cm threshold"


@requires_fixtures
def test_e2e_refuses_wgs84_named_input(tmp_path):
    """Filename ending in .wgs84.fit should be refused without --force."""
    # Make a copy with the dangerous name
    import shutil
    bad_input = tmp_path / "already.wgs84.fit"
    shutil.copy(FIT_FIXTURE, bad_input)

    _, stderr = run_cli(str(bad_input), expect_exit=1)
    assert "refusing" in stderr.lower()
    assert "--force" in stderr


@requires_fixtures
def test_e2e_force_overrides_refusal(tmp_path):
    """With --force, processing a .wgs84.fit-named input proceeds."""
    import shutil
    bad_input = tmp_path / "already.wgs84.fit"
    shutil.copy(FIT_FIXTURE, bad_input)
    output = tmp_path / "forced_out.fit"

    stdout, _ = run_cli(str(bad_input), "-o", str(output), "--force")
    assert output.exists()
    assert "Converted" in stdout
