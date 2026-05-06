"""fit-gcj02-to-wgs84 的回归测试。

使用真实数据 fixture：
- ~/Downloads/夏羌拉.gpx —— 来自两步路 iOS app 的原始 WGS84 GPX
- ~/Downloads/夏羌拉.fit —— 同一条路线在 Connect 中国区编辑后的 GCJ-02 FIT

正向用例断言：把 FIT（GCJ-02）转换为 WGS84 后，轨迹点跟原始 GPX
（WGS84）的距离 ~1cm（FIT semicircle 编码精度地板）。

如果测试 fixture 文件不存在，相关用例会自动跳过。
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
    reason="测试 fixture 夏羌拉.gpx/fit 不在 ~/Downloads/ 下",
)


def haversine_m(lat1, lon1, lat2, lon2):
    """两个经纬度之间的球面距离，单位：米。"""
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
    """运行 CLI 脚本，返回 (stdout, stderr)。"""
    result = subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True,
        text=True,
    )
    assert result.returncode == expect_exit, (
        f"期望 exit={expect_exit}，实际={result.returncode}。\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout, result.stderr


def _load_module():
    """以模块形式加载主脚本（文件名带连字符，要走 importlib 这条路）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("fit_gcj02_to_wgs84", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----- 算法层用例（不需要 fixture 文件） -----

def test_inverse_roundtrip_in_china():
    """正向 + 反向应在 1e-9 度内还原原值。"""
    mod = _load_module()
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
    """境外坐标必须原样返回（GCJ-02 仅在中国大陆有定义）。"""
    mod = _load_module()
    # 东京、纽约、伦敦
    for lat, lon in [(35.6895, 139.6917), (40.7128, -74.0060), (51.5074, -0.1278)]:
        assert mod.wgs84_to_gcj02(lat, lon) == (lat, lon)
        assert mod.gcj02_to_wgs84(lat, lon) == (lat, lon)


# ----- 端到端 CLI 用例（依赖 fixture） -----

@requires_fixtures
def test_e2e_forward_conversion(tmp_path):
    """转换 夏羌拉.fit（GCJ-02），断言输出跟 夏羌拉.gpx（WGS84）逐点对齐。"""
    output = tmp_path / "out.wgs84.fit"
    stdout, _ = run_cli(FIT_FIXTURE, "-o", str(output))

    assert output.exists(), "输出文件没有生成"
    assert output.stat().st_size == os.path.getsize(FIT_FIXTURE), (
        "输出文件大小应当等于输入大小（字节级原地转换）"
    )
    assert "已转换" in stdout

    gpx_pts = read_gpx_trackpoints(GPX_FIXTURE)
    fit_pts = read_fit_trackpoints(str(output))
    assert len(fit_pts) > 1000, "Sanity 检查：轨迹点数应当很多"

    # 对每个 FIT 轨迹点，在 GPX 中找最近邻。平均距离应当在
    # FIT semicircle 编码精度地板（~1cm）附近。
    sample_n = min(50, len(fit_pts))
    distances = []
    for i in range(sample_n):
        flat, flon = fit_pts[i]
        d = min(haversine_m(flat, flon, glat, glon) for glat, glon in gpx_pts)
        distances.append(d)
    avg = sum(distances) / len(distances)
    assert avg < 0.05, f"平均残差 {avg:.4f}m 超过 5cm 阈值"


@requires_fixtures
def test_e2e_refuses_wgs84_named_input(tmp_path):
    """输入文件名以 .wgs84.fit 结尾时应当拒绝（除非 --force）。"""
    import shutil
    bad_input = tmp_path / "already.wgs84.fit"
    shutil.copy(FIT_FIXTURE, bad_input)

    _, stderr = run_cli(str(bad_input), expect_exit=1)
    assert "拒绝" in stderr
    assert "--force" in stderr


@requires_fixtures
def test_e2e_force_overrides_refusal(tmp_path):
    """加 --force 参数时，对 .wgs84.fit 命名的输入也能正常转换。"""
    import shutil
    bad_input = tmp_path / "already.wgs84.fit"
    shutil.copy(FIT_FIXTURE, bad_input)
    output = tmp_path / "forced_out.fit"

    stdout, _ = run_cli(str(bad_input), "-o", str(output), "--force")
    assert output.exists()
    assert "已转换" in stdout
