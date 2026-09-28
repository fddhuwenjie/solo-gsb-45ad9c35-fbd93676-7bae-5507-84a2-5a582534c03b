"""采集连续性检查测试：示例数据、合并规则、边界情形与确定性。"""
import copy
import json

from app.continuity import check_continuity
from app.sample import CONTINUITY_SAMPLE
from app.schemas import ContinuityRequest

L1 = {"id": "L1", "start": [0, 0], "end": [1000, 0], "swath": 50}
L2 = {"id": "L2", "start": [1000, 100], "end": [0, 100], "swath": 50}


def _run(payload=None):
    return check_continuity(ContinuityRequest.model_validate(payload or CONTINUITY_SAMPLE))


def _sample(line_id, t, seq, x, y=0):
    return {"line_id": line_id, "timestamp": t, "ping_seq": seq, "x": x, "y": y}


def test_sample_event_count_and_reason_counts():
    res = _run()
    assert res["summary"]["event_count"] == 7
    assert res["summary"]["reason_counts"] == {
        "time_reversal": 1,
        "seq_duplicate": 1,
        "seq_jump": 1,
        "gap_exceeded": 2,
        "speed_exceeded": 2,
        "offset_exceeded": 1,
    }
    assert [e["id"] for e in res["events"]] == [f"EVT-{i:03d}" for i in range(1, 8)]


def test_sample_events_use_original_indices():
    res = _run()
    first = res["events"][0]
    assert first["reasons"] == ["gap_exceeded"]
    assert (first["start_index"], first["end_index"]) == (4, 5)
    assert first["start_sample"]["ping_seq"] == 5
    assert first["end_sample"]["timestamp"] == 35
    assert first["thresholds"] == {"max_gap_s": 8.0, "max_speed": 15.0, "max_offset": 20.0}


def test_multi_reason_event_merged_with_geometry():
    res = _run()
    ev = next(e for e in res["events"] if set(e["reasons"]) == {"speed_exceeded", "offset_exceeded"})
    # 区间 14、15 相邻且同测线 -> 合并为一个事件，原因全部保留
    assert ev["segment_count"] == 2
    assert (ev["start_index"], ev["end_index"]) == (13, 15)
    assert ev["sample_count"] == 3
    assert ev["geometry"]["type"] == "LineString"
    assert len(ev["geometry"]["coordinates"]) == 3
    assert ev["stats"]["max_speed"] == 18.0278  # hypot(50, 75) / 5
    assert ev["stats"]["max_offset"] == 75.0
    # 每个区间都同时命中两条规则
    for seg in ev["segments"]:
        assert set(seg["reasons"]) == {"speed_exceeded", "offset_exceeded"}
        assert seg["metrics"]["dt"] == 5.0


def test_time_reversal_blocks_merging():
    payload = {
        "lines": [L1],
        "samples": [
            _sample("L1", 0, 1, 0),
            _sample("L1", 100, 2, 10),   # 间隔超限
            _sample("L1", 50, 3, 20),    # 时间倒退
            _sample("L1", 200, 4, 30),   # 间隔超限
        ],
        "options": {"max_gap_s": 8, "max_speed": 1000, "max_offset": 1000},
    }
    res = _run(payload)
    # 倒退点前后同为间隔超限，但不得跨倒退点合并
    assert [e["reasons"] for e in res["events"]] == [
        ["gap_exceeded"], ["time_reversal"], ["gap_exceeded"],
    ]
    # 倒退区间不计算间隔/速度/偏离
    rev = res["events"][1]
    assert rev["segments"][0]["metrics"] is None
    assert rev["stats"] == {"max_dt": None, "max_speed": None, "max_offset": None}


def test_cross_line_segments_not_merged_and_seq_reset_ok():
    payload = {
        "lines": [L1, L2],
        "samples": [
            _sample("L1", 0, 1, 0),
            _sample("L1", 100, 2, 10),        # L1 间隔超限
            _sample("L2", 200, 1, 1000, 100),  # 跨线：seq 重新计数、时间递增 -> 无异常
            _sample("L2", 400, 2, 990, 100),   # L2 间隔超限
        ],
        "options": {"max_gap_s": 8, "max_speed": 1000, "max_offset": 1000},
    }
    res = _run(payload)
    # 两个间隔事件分属两条测线，不得跨测线合并；跨线区间不产生 seq_jump
    assert res["summary"]["event_count"] == 2
    assert [e["line_id"] for e in res["events"]] == ["L1", "L2"]
    assert res["summary"]["reason_counts"]["seq_jump"] == 0


def test_cross_line_reversal_isolated_event():
    payload = {
        "lines": [L1, L2],
        "samples": [
            _sample("L1", 100, 1, 0),
            _sample("L2", 50, 1, 1000, 100),   # 跨线 + 时间倒退 -> 独立事件
            _sample("L2", 200, 2, 990, 100),   # L2 间隔超限 -> 不得与倒退事件合并
        ],
        "options": {"max_gap_s": 8, "max_speed": 1000, "max_offset": 1000},
    }
    res = _run(payload)
    assert [e["reasons"] for e in res["events"]] == [["time_reversal"], ["gap_exceeded"]]
    assert res["events"][0]["line_id"] == "L2"  # 跨线事件归入到达测线


def test_seq_duplicate_and_jump_only_same_line():
    payload = {
        "lines": [L1],
        "samples": [
            _sample("L1", 0, 5, 0),
            _sample("L1", 1, 5, 10),   # 重复
            _sample("L1", 2, 6, 20),   # +1 正常
            _sample("L1", 3, 9, 30),   # 非 +1 跳变
        ],
        "options": {"max_gap_s": 100, "max_speed": 1000, "max_offset": 1000},
    }
    res = _run(payload)
    assert [e["reasons"] for e in res["events"]] == [["seq_duplicate"], ["seq_jump"]]


def test_zero_dt_not_evaluated():
    payload = {
        "lines": [L1],
        "samples": [
            _sample("L1", 5, 1, 0),
            _sample("L1", 5, 2, 1000),  # dt=0 非严格递增：不计算速度（避免除零），也不算倒退
        ],
        "options": {"max_gap_s": 8, "max_speed": 1, "max_offset": 1000},
    }
    res = _run(payload)
    assert res["summary"]["event_count"] == 0


def test_unknown_line_skips_offset_but_kept_in_summary():
    payload = {
        "lines": [L1],
        "samples": [
            _sample("L9", 0, 1, 0, 99999),   # 未知测线：不参与偏离判定
            _sample("L9", 1, 2, 10, 99999),
        ],
        "options": {"max_gap_s": 8, "max_speed": 1000, "max_offset": 1},
    }
    res = _run(payload)
    assert res["summary"]["unknown_line_ids"] == ["L9"]
    assert res["summary"]["event_count"] == 0
    assert res["line_stats"][0]["planned"] is False


def test_offset_uses_distance_to_planned_segment():
    payload = {
        "lines": [L1],  # y=0 的线段
        "samples": [
            _sample("L1", 0, 1, 0, 30),   # 偏离 30 > 20
            _sample("L1", 1, 2, 10, 0),
        ],
        "options": {"max_gap_s": 8, "max_speed": 1000, "max_offset": 20},
    }
    res = _run(payload)
    assert [e["reasons"] for e in res["events"]] == [["offset_exceeded"]]
    seg = res["events"][0]["segments"][0]
    assert seg["metrics"]["offset_from"] == 30.0
    assert seg["metrics"]["offset_to"] == 0.0
    assert seg["metrics"]["offset_max"] == 30.0


def test_thresholds_respected():
    payload = copy.deepcopy(CONTINUITY_SAMPLE)
    payload["options"]["max_gap_s"] = 20.0  # 放宽间隔阈值 -> 两个间隔事件消失
    res = _run(payload)
    assert res["summary"]["reason_counts"]["gap_exceeded"] == 0
    assert res["summary"]["event_count"] == 5


def test_empty_samples():
    res = _run({"lines": [L1], "samples": [], "options": {}})
    assert res["summary"]["event_count"] == 0
    assert res["summary"]["time_range"] is None
    assert res["events"] == []


def test_deterministic_output():
    a = json.dumps(_run(), sort_keys=True)
    b = json.dumps(_run(), sort_keys=True)
    assert a == b
