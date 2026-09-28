"""核心计算冒烟测试：示例数据应稳定产出预期的三类问题。"""
import json

from app.analysis import analyze
from app.sample import SAMPLE
from app.schemas import AnalysisRequest


def _run():
    return analyze(AnalysisRequest.model_validate(SAMPLE))


def test_sample_issue_counts():
    res = _run()
    counts = res["summary"]["issue_counts"]
    assert counts == {"gap": 3, "outside": 2, "overlap": 1}, counts


def test_sample_coverage_pct():
    res = _run()
    # 边界 540000，缺口 45000+35000+4000=84000 -> 456000/540000
    assert res["summary"]["coverage_pct"] == 84.4444


def test_overlap_basis_verifiable():
    res = _run()
    ovl = next(i for i in res["issues"] if i["type"] == "overlap")
    assert ovl["lines"] == ["L2", "L3"]
    # 重叠带宽 30，扫宽 90 -> ratio = 30000/90000
    assert ovl["basis"]["overlap_ratio"] == 0.3333
    assert ovl["basis"]["overlap_area"] == 30000.0
    assert ovl["basis"]["centerline_spacing"] == 60.0


def test_outside_lines_attributed():
    res = _run()
    outs = [i for i in res["issues"] if i["type"] == "outside"]
    line_sets = {tuple(i["lines"]) for i in outs}
    assert ("L5",) in line_sets and ("L6",) in line_sets


def test_deterministic_output():
    a = json.dumps(_run(), sort_keys=True)
    b = json.dumps(_run(), sort_keys=True)
    assert a == b


def test_gap_ids_stable_and_sorted():
    res = _run()
    gaps = [i for i in res["issues"] if i["type"] == "gap"]
    assert [g["id"] for g in gaps] == ["GAP-001", "GAP-002", "GAP-003"]
    centroids = [(g["centroid"][0], g["centroid"][1]) for g in gaps]
    assert centroids == sorted(centroids)
