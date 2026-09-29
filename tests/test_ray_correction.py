"""声速射线改正测试：Snell 逐层追踪、三类失败、段保留、平面换算与确定性。"""
import json

import pytest

from app.ray_correction import ray_correct
from app.sample import RAY_SAMPLE
from app.schemas import RayCorrectionRequest


def _layer(top, bottom, c):
    return {"top_depth": top, "bottom_depth": bottom, "sound_speed": c}


def _beam(**kw):
    twtt = kw.pop("twtt", 0.1)
    base = dict(id=None, x=0.0, y=0.0, heading=0.0, transducer_depth=0.0,
                two_way_travel_time=twtt, angle_deg=0.0)
    base.update(kw)
    return base


def _run(profile, beams):
    return ray_correct(RayCorrectionRequest.model_validate(
        {"profile": profile, "beams": beams}))


def _run_sample():
    return ray_correct(RayCorrectionRequest.model_validate(RAY_SAMPLE))


# ---------------- 示例数据 ----------------
def test_sample_status_counts():
    res = _run_sample()
    s = res["summary"]
    assert s["beam_count"] == 9
    assert s["ok_count"] == 5
    assert s["failed_count"] == 4
    assert s["failure_counts"] == {
        "refraction_limit": 1,
        "profile_gap": 2,
        "beyond_profile_bottom": 1,
    }
    assert s["max_deviation"] == pytest.approx(1.88, abs=1e-3)


def test_sample_ids_and_input_indices_preserved():
    res = _run_sample()
    assert [b["id"] for b in res["beams"]] == [f"B-{i:03d}" for i in range(1, 10)]
    assert [b["input_index"] for b in res["beams"]] == list(range(9))


def test_sample_failure_reasons():
    by_id = {b["id"]: b for b in _run_sample()["beams"]}
    assert by_id["B-006"]["failure_reason"] == "refraction_limit"
    assert by_id["B-007"]["failure_reason"] == "profile_gap"
    assert by_id["B-008"]["failure_reason"] == "beyond_profile_bottom"
    assert by_id["B-009"]["failure_reason"] == "profile_gap"


# ---------------- 基本追踪数值 ----------------
def test_uniform_vertical():
    res = _run([_layer(0, 100, 1500)], [_beam(twtt=0.1)])  # 单程 0.05 s → 75 m
    b = res["beams"][0]
    assert b["status"] == "ok"
    assert b["footprint_depth"] == 75.0
    assert b["cross_track"] == 0.0
    assert b["point"] == [0.0, 0.0]
    assert len(b["segments"]) == 1
    assert b["segments"][0]["angle_deg"] == 0.0
    assert b["segments"][0]["travel_time"] == 0.05
    # 单层垂直：射线改正与直线对照完全一致
    assert b["deviation"] == 0.0
    assert b["straight"]["point"] == b["point"]


def test_uniform_angled_uses_one_way_time_and_heading_north():
    res = _run([_layer(0, 100, 1500)], [_beam(heading=0, twtt=0.1, angle_deg=30)])
    b = res["beams"][0]
    # 单程路径长 75：垂深 75cos30、横距 75sin30（右舷=东侧，航向正北时 x 增大）
    assert b["footprint_depth"] == pytest.approx(64.9519, abs=1e-4)
    assert b["cross_track"] == pytest.approx(37.5, abs=1e-6)
    assert b["point"] == pytest.approx([37.5, 0.0], abs=1e-6)


def test_heading_east_positive_angle_goes_south():
    res = _run([_layer(0, 100, 1500)],
               [_beam(x=10, y=20, heading=90, twtt=0.1, angle_deg=30)])
    b = res["beams"][0]
    # 航向正东，右舷为南：x 不变，y 减小
    assert b["point"] == pytest.approx([10.0, 20.0 - 37.5], abs=1e-6)


def test_negative_angle_is_port_side():
    res = _run([_layer(0, 100, 1500)], [_beam(heading=0, twtt=0.1, angle_deg=-30)])
    b = res["beams"][0]
    assert b["cross_track"] == pytest.approx(-37.5, abs=1e-6)
    assert b["point"] == pytest.approx([-37.5, 0.0], abs=1e-6)


def test_snell_two_layers():
    res = _run([_layer(0, 100, 1500), _layer(100, 200, 1480)],
               [_beam(twtt=0.2, angle_deg=30)])
    b = res["beams"][0]
    assert b["status"] == "ok"
    seg0, seg1 = b["segments"]
    # p = sin30/1500；层 1 角 asin(p·1480)
    import math
    theta1 = math.degrees(math.asin(math.sin(math.radians(30)) * 1480 / 1500))
    assert seg0["angle_deg"] == 30.0
    assert seg0["enter_depth"] == 0.0 and seg0["exit_depth"] == 100.0
    assert seg0["complete_layer"] is True
    assert seg0["cross_track"] == pytest.approx(57.7350, abs=1e-4)
    assert seg1["angle_deg"] == pytest.approx(theta1, abs=1e-4)
    assert seg1["enter_depth"] == 100.0
    assert seg1["complete_layer"] is False  # 时间在层内耗尽
    # 总单程耗时恰为输入双程时间的一半
    assert b["elapsed_one_way_time"] == pytest.approx(0.1, abs=1e-9)
    assert b["remaining_one_way_time"] == 0.0
    # 射线累计横距与各层角度一致（慢于对照直线 -> 弯曲射线落点更靠近船、更浅）
    assert b["cross_track"] < b["straight"]["cross_track"]
    assert b["footprint_depth"] < b["straight"]["footprint_depth"]
    assert b["deviation"] == pytest.approx(0.4573, abs=2e-3)


def test_transducer_inside_layer_first_segment_partial():
    # 换能器深 5 m，单程 10/1500 s 恰好走到 15 m 界面
    twtt = 2.0 * 10.0 / 1500.0
    res = _run([_layer(0, 15, 1500), _layer(15, 50, 1480)],
               [_beam(transducer_depth=5, twtt=twtt)])
    b = res["beams"][0]
    assert b["status"] == "ok"
    assert b["footprint_depth"] == 15.0
    assert len(b["segments"]) == 1
    assert b["segments"][0]["enter_depth"] == 5.0
    assert b["segments"][0]["complete_layer"] is False


def test_transducer_at_joining_boundary_enters_lower_layer():
    res = _run([_layer(0, 100, 1500), _layer(100, 200, 1480)],
               [_beam(transducer_depth=100, twtt=2 * 50 / 1480)])
    b = res["beams"][0]
    assert b["entry_layer_index"] == 1
    assert b["status"] == "ok"
    assert b["footprint_depth"] == 150.0


def test_time_exhausted_exactly_at_layer_boundary():
    res = _run([_layer(0, 100, 1500), _layer(100, 200, 1480)],
               [_beam(twtt=2 * 100 / 1500)])
    b = res["beams"][0]
    assert b["status"] == "ok"
    assert b["footprint_depth"] == 100.0
    assert len(b["segments"]) == 1
    assert b["segments"][0]["complete_layer"] is True


# ---------------- 三类失败 ----------------
def test_refraction_limit_keeps_traversed_segments():
    # 入射 30°（sin=0.5），p=0.5/1500；下层 c=3100 -> |p·c|>1
    res = _run([_layer(0, 50, 1500), _layer(50, 100, 3100)],
               [_beam(twtt=1.0, angle_deg=30)])
    b = res["beams"][0]
    assert b["status"] == "failed"
    assert b["failure_reason"] == "refraction_limit"
    assert b["point"] is None and b["footprint_depth"] is None
    # 已走层段保留：在第一层底（50 m）被挡回，包含完整的第一层段
    assert b["reached_depth"] == 50.0
    assert len(b["segments"]) == 1
    assert b["segments"][0]["exit_depth"] == 50.0
    assert b["remaining_one_way_time"] > 0
    # 失败时仍给对照直线
    assert b["straight"]["sound_speed"] == 1500.0
    assert b["ray_path"]["complete"] is False
    # ray_path 首点为换能器，末点为受阻深度
    assert b["ray_path"]["coordinates"][0] == [0.0, 0.0]
    assert b["ray_path"]["coordinates"][-1][1] == 50.0


def test_entry_angle_90_is_refraction_limit():
    res = _run([_layer(0, 50, 1500)], [_beam(twtt=1.0, angle_deg=90)])
    b = res["beams"][0]
    assert b["failure_reason"] == "refraction_limit"
    assert b["segments"] == []
    assert b["reached_depth"] == 0.0


def test_profile_gap_below_stops_without_extrapolation():
    res = _run([_layer(0, 50, 1500), _layer(60, 200, 1500)],
               [_beam(twtt=1.0)])
    b = res["beams"][0]
    assert b["failure_reason"] == "profile_gap"
    assert b["reached_depth"] == 50.0
    assert len(b["segments"]) == 1
    assert b["remaining_one_way_time"] == pytest.approx(0.5 - 50 / 1500, abs=5e-5)
    assert b["point"] is None


def test_transducer_outside_profile_is_gap_without_straight():
    res = _run([_layer(0, 50, 1500), _layer(60, 200, 1500)],
               [_beam(transducer_depth=55, twtt=0.1)])
    b = res["beams"][0]
    assert b["failure_reason"] == "profile_gap"
    assert b["straight"] is None
    assert b["ray_path"]["coordinates"] == [[0.0, 55.0]]
    assert b["segments"] == []


def test_beyond_profile_bottom_keeps_full_layer():
    res = _run([_layer(0, 50, 1500)], [_beam(twtt=1.0)])
    b = res["beams"][0]
    assert b["failure_reason"] == "beyond_profile_bottom"
    assert b["reached_depth"] == 50.0
    assert b["remaining_one_way_time"] == pytest.approx(0.5 - 50 / 1500, abs=5e-5)
    assert len(b["segments"]) == 1
    assert b["segments"][0]["complete_layer"] is True


def test_sample_refraction_failure_preserves_three_segments():
    by_id = {b["id"]: b for b in _run_sample()["beams"]}
    b = by_id["B-006"]
    # 换能器深 5 m：首段为层 0 的部分段，其后层 1、层 2 为完整层，在 120 m 界面受阻
    assert [s["layer_index"] for s in b["segments"]] == [0, 1, 2]
    assert b["segments"][0]["enter_depth"] == 5.0
    assert b["segments"][0]["complete_layer"] is False
    assert b["segments"][1]["complete_layer"] is True
    assert b["reached_depth"] == 120.0
    assert b["straight"]["cross_track"] > 0


def test_sample_bottom_failure_from_below_gap_transducer():
    by_id = {b["id"]: b for b in _run_sample()["beams"]}
    b = by_id["B-008"]
    # 换能器 340 m（断档 300–320 在其上方，不经过），下行到 500 m 底界后仍有剩余
    assert b["transducer_depth"] == 340.0
    assert b["reached_depth"] == 500.0
    assert len(b["segments"]) == 1


# ---------------- 剖面校验 ----------------
def test_overlapping_layers_rejected():
    with pytest.raises(ValueError, match="重叠"):
        _run([_layer(0, 60, 1500), _layer(50, 100, 1480)], [_beam()])


def test_zero_thickness_layer_rejected():
    with pytest.raises(ValueError, match="厚度必须为正"):
        _run([_layer(10, 10, 1500)], [_beam()])


def test_unsorted_layers_sorted_by_top_depth():
    res = _run([_layer(100, 200, 1480), _layer(0, 100, 1500)],
               [_beam(twtt=0.05)])
    assert [ly["top_depth"] for ly in res["profile"]] == [0.0, 100.0]
    assert res["beams"][0]["status"] == "ok"


def test_input_layer_index_refers_to_original_order():
    res = _run([_layer(100, 200, 1480), _layer(0, 100, 1500)],
               [_beam(twtt=0.2, angle_deg=30)])
    b = res["beams"][0]
    # 输入乱序（深→浅），追踪按深度向下：先入射输入中的第 2 层（index 1），再到 index 0
    assert b["segments"][0]["layer_index"] == 1
    assert b["entry_layer_index"] == 1
    if len(b["segments"]) > 1:
        assert b["segments"][-1]["layer_index"] == 0


# ---------------- 其他 ----------------
def test_default_ids_generated_in_input_order():
    res = _run([_layer(0, 50, 1500)],
               [_beam(), _beam(id="X"), _beam()])
    assert [b["id"] for b in res["beams"]] == ["B-001", "X", "B-003"]


def test_empty_beams():
    res = _run([_layer(0, 50, 1500)], [])
    assert res["beams"] == []
    assert res["summary"]["beam_count"] == 0
    assert res["summary"]["max_deviation"] is None


def test_straight_control_always_present_when_entry_layer_known():
    # 成功与失败（换能器在层内）波束都带直线对照
    for b in _run_sample()["beams"]:
        if b["id"] != "B-009":
            assert b["straight"] is not None
            st = b["straight"]
            assert st["path_length"] > 0
            assert st["footprint_depth"] >= b["transducer_depth"]


def test_deterministic_output():
    a = json.dumps(_run_sample(), sort_keys=True)
    b = json.dumps(_run_sample(), sort_keys=True)
    assert a == b
