"""采集连续性检查 —— 按采集顺序的样本与计划测线比对。

样本按采集顺序输入，后端不做任何重排，全部检查基于原始顺序的相邻样本对
（区间 k 连接样本 k-1 与样本 k，索引为 0-based 原始输入顺序）：

- 顺序检查：时间倒退（任意相邻样本）；序号重复 / 非 +1 跳变（仅同测线相邻，
  跨测线时 ping_seq 重新计数属正常换线，不报）；
- 区间检查（仅同一测线且时间严格递增的相邻样本间）：采样间隔、implied 速度、
  样本到计划测线（start-end 线段）的偏离距离；
- 事件合并：相邻异常区间合并为一个事件，不得跨测线、不得跨时间倒退点；
  同一区间命中多条规则时，原因全部保留在区间与事件上。

输出确定性：事件按起始样本原始索引升序编号（EVT-001…），无随机、无时间戳，
同一输入输出逐字节一致。
"""
from __future__ import annotations

import math
from typing import Any

from shapely.geometry import LineString, Point

from .analysis import geom_to_geojson, r4, r6
from .schemas import ContinuityRequest

# 原因固定输出顺序（保证同一输入输出稳定）
REASON_ORDER = [
    "time_reversal",
    "seq_duplicate",
    "seq_jump",
    "gap_exceeded",
    "speed_exceeded",
    "offset_exceeded",
]
REASON_LABELS = {
    "time_reversal": "时间倒退",
    "seq_duplicate": "序号重复",
    "seq_jump": "序号非 +1 跳变",
    "gap_exceeded": "采样间隔超限",
    "speed_exceeded": "速度超限",
    "offset_exceeded": "偏离计划线超限",
}

MERGE_RULE = (
    "事件 = 原始顺序上相邻异常区间的最大连续合并（不跨测线、不跨时间倒退点）；"
    "区间 k 连接样本 k-1 → k（0-based 原始索引）；"
    "间隔/速度/偏离仅在同一测线且时间严格递增的区间上计算；"
    "偏离 = 样本点到计划测线（start-end 线段）的距离，区间取两端点最大值"
)


def _sample_view(s: Any, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "line_id": s.line_id,
        "timestamp": s.timestamp,
        "ping_seq": s.ping_seq,
        "x": r6(s.x),
        "y": r6(s.y),
    }


def check_continuity(req: ContinuityRequest) -> dict[str, Any]:
    opts = req.options
    samples = req.samples
    n = len(samples)

    # ---- 0. 计划测线（沿用覆盖复核的 lines 输入） ----
    planned: dict[str, LineString] = {}
    for ln in req.lines:
        if ln.id in planned:
            raise ValueError(f"测线 id 重复: {ln.id!r}")
        g = LineString([ln.start, ln.end])
        if g.length <= 0:
            raise ValueError(f"测线 {ln.id!r} 起点与终点重合，无法构成测线")
        planned[ln.id] = g

    # 每个样本到其计划测线的距离；未知测线 -> None（不参与偏离判定，在 summary 中列出）
    offsets: list[float | None] = []
    unknown_line_ids: list[str] = []
    for s in samples:
        g = planned.get(s.line_id)
        if g is None:
            if s.line_id not in unknown_line_ids:
                unknown_line_ids.append(s.line_id)
            offsets.append(None)
        else:
            offsets.append(g.distance(Point(s.x, s.y)))

    # ---- 1. 逐区间检查（区间 k 连接样本 k-1 与 k） ----
    seg_reasons: list[list[str]] = [[] for _ in range(n)]
    seg_metrics: list[dict[str, Any] | None] = [None] * n
    for k in range(1, n):
        a, b = samples[k - 1], samples[k]
        reasons: list[str] = []
        dt = b.timestamp - a.timestamp
        if dt < 0:
            reasons.append("time_reversal")
        if a.line_id == b.line_id:
            if b.ping_seq == a.ping_seq:
                reasons.append("seq_duplicate")
            elif b.ping_seq != a.ping_seq + 1:
                reasons.append("seq_jump")
            if dt > 0:
                # 只在同一测线且时间严格递增的相邻样本间计算间隔/速度/偏离
                dist = math.hypot(b.x - a.x, b.y - a.y)
                speed = dist / dt
                off_from, off_to = offsets[k - 1], offsets[k]
                off_max = (
                    max(off_from, off_to)
                    if off_from is not None and off_to is not None
                    else None
                )
                seg_metrics[k] = {
                    "dt": r4(dt),
                    "dist": r4(dist),
                    "speed": r4(speed),
                    "offset_from": r4(off_from) if off_from is not None else None,
                    "offset_to": r4(off_to) if off_to is not None else None,
                    "offset_max": r4(off_max) if off_max is not None else None,
                }
                if dt > opts.max_gap_s:
                    reasons.append("gap_exceeded")
                if speed > opts.max_speed:
                    reasons.append("speed_exceeded")
                if off_max is not None and off_max > opts.max_offset:
                    reasons.append("offset_exceeded")
        seg_reasons[k] = reasons

    # ---- 2. 相邻异常区间合并为事件（不跨测线、不跨倒退点） ----
    raw_events: list[list[int]] = []  # 每个事件是一串连续区间编号
    cur: list[int] | None = None
    for k in range(1, n):
        reasons = seg_reasons[k]
        if not reasons:
            cur = None  # 正常区间断开事件
            continue
        if samples[k - 1].line_id != samples[k].line_id:
            # 跨线区间只可能命中时间倒退；独立成事件，且作为合并边界
            raw_events.append([k])
            cur = None
            continue
        if (
            cur is not None
            and samples[cur[-1]].line_id == samples[k].line_id
            and "time_reversal" not in reasons
            and "time_reversal" not in seg_reasons[cur[-1]]
        ):
            cur.append(k)
        else:
            cur = [k]
            raw_events.append(cur)

    # ---- 3. 事件输出（起止样本、原始索引、轨迹几何、阈值与计算量） ----
    thresholds = {
        "max_gap_s": opts.max_gap_s,
        "max_speed": opts.max_speed,
        "max_offset": opts.max_offset,
    }
    events: list[dict[str, Any]] = []
    for idx, segs in enumerate(raw_events, 1):
        start_i, end_i = segs[0] - 1, segs[-1]
        ev_reasons = sorted({r for k in segs for r in seg_reasons[k]}, key=REASON_ORDER.index)
        geom = LineString([(samples[i].x, samples[i].y) for i in range(start_i, end_i + 1)])
        mets = [seg_metrics[k] for k in segs if seg_metrics[k] is not None]
        events.append({
            "id": f"EVT-{idx:03d}",
            "line_id": samples[end_i].line_id,
            "reasons": ev_reasons,
            "reason_labels": [REASON_LABELS[r] for r in ev_reasons],
            "start_index": start_i,
            "end_index": end_i,
            "start_sample": _sample_view(samples[start_i], start_i),
            "end_sample": _sample_view(samples[end_i], end_i),
            "sample_count": end_i - start_i + 1,
            "segment_count": len(segs),
            "geometry": geom_to_geojson(geom),
            "centroid": [r6(geom.centroid.x), r6(geom.centroid.y)],
            "stats": {
                "max_dt": max((m["dt"] for m in mets), default=None),
                "max_speed": max((m["speed"] for m in mets), default=None),
                "max_offset": max(
                    (m["offset_max"] for m in mets if m["offset_max"] is not None),
                    default=None,
                ),
            },
            "segments": [
                {
                    "seg": k,
                    "from_index": k - 1,
                    "to_index": k,
                    "reasons": seg_reasons[k],
                    "reason_labels": [REASON_LABELS[r] for r in seg_reasons[k]],
                    "metrics": seg_metrics[k],
                }
                for k in segs
            ],
            "thresholds": thresholds,
            "basis": {
                "method": MERGE_RULE,
                "segment_span": [segs[0], segs[-1]],
            },
        })

    # ---- 4. 汇总与逐测线统计 ----
    reason_counts = {r: 0 for r in REASON_ORDER}
    for ev in events:
        for r in ev["reasons"]:
            reason_counts[r] += 1

    line_agg: dict[str, dict[str, int]] = {}
    for i, s in enumerate(samples):
        agg = line_agg.setdefault(s.line_id, {"count": 0, "first": i, "last": i})
        agg["count"] += 1
        agg["last"] = i
    events_per_line: dict[str, int] = {}
    for ev in events:
        events_per_line[ev["line_id"]] = events_per_line.get(ev["line_id"], 0) + 1
    line_stats = [
        {
            "line_id": lid,
            "planned": lid in planned,
            "sample_count": agg["count"],
            "event_count": events_per_line.get(lid, 0),
            "first_index": agg["first"],
            "last_index": agg["last"],
            "time_range": [samples[agg["first"]].timestamp, samples[agg["last"]].timestamp],
            "seq_range": [samples[agg["first"]].ping_seq, samples[agg["last"]].ping_seq],
        }
        for lid, agg in line_agg.items()
    ]

    summary = {
        "sample_count": n,
        "planned_line_count": len(planned),
        "sample_line_count": len(line_agg),
        "event_count": len(events),
        "anomaly_segment_count": sum(1 for k in range(1, n) if seg_reasons[k]),
        "reason_counts": reason_counts,
        "unknown_line_ids": unknown_line_ids,
        "time_range": [samples[0].timestamp, samples[-1].timestamp] if n else None,
        "options_used": thresholds,
    }

    return {
        "summary": summary,
        "events": events,
        "line_stats": line_stats,
        "meta": {
            "engine": "shapely",
            "index_note": "start_index/end_index/seg 均为 0-based 原始输入顺序，样本不做任何重排",
            "segment_rule": MERGE_RULE,
            "seq_rule": "ping_seq 重复 / 非 +1 跳变仅在同一测线的相邻样本间判定，跨测线重新计数属正常",
            "reversal_rule": "时间倒退按原始顺序的任意相邻样本判定；倒退区间不参与间隔/速度/偏离计算，且作为事件合并边界",
            "unknown_line_rule": "样本 line_id 不在计划测线中时，该样本不参与偏离判定，其 id 列入 summary.unknown_line_ids",
            "coordinate_note": "输入视为平面坐标，距离/速度单位与输入坐标及秒一致",
            "ordering": "事件按起始样本原始索引升序编号（EVT-001…），同一输入输出完全一致",
        },
    }
