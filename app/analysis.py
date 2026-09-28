"""水下声呐测线覆盖复核 —— 核心几何计算。

坐标约定：输入为平面坐标（单位自定，通常为米），输出的长度/面积与输入单位一致。
所有计算基于 Shapely(GEOS)，对同一输入完全确定（无随机、无时间戳），
修改输入后重新调用即可得到稳定更新的结果。
"""
from __future__ import annotations

from typing import Any

import shapely
from shapely import BufferCapStyle
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .schemas import AnalysisRequest, Options

COORD_NDIGITS = 6  # 坐标输出精度
NUM_NDIGITS = 4    # 面积/比率输出精度

ISSUE_TYPE_LABELS = {"gap": "未覆盖缺口", "outside": "边界外扫掠", "overlap": "过度重叠"}


def r4(v: float) -> float:
    return round(float(v), NUM_NDIGITS)


def r6(v: float) -> float:
    return round(float(v), COORD_NDIGITS)


def _round_coords(coords: Any) -> Any:
    """递归圆整 GeoJSON 坐标；丢弃 Z 值，保证输出稳定可 diff。"""
    if isinstance(coords, (list, tuple)) and coords and isinstance(coords[0], (int, float)):
        return [r6(c) for c in coords[:2]]
    return [_round_coords(c) for c in coords]


def geom_to_geojson(geom: BaseGeometry) -> dict:
    m = mapping(geom)
    return {"type": m["type"], "coordinates": _round_coords(m["coordinates"])}


def _polygons(geom: BaseGeometry) -> list[Polygon]:
    """把任意几何拆成 Polygon 列表（过滤线/点碎片）。"""
    if geom.is_empty:
        return []
    t = geom.geom_type
    if t == "Polygon":
        return [geom]
    if t == "MultiPolygon":
        return list(geom.geoms)
    if t == "GeometryCollection":
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(_polygons(g))
        return out
    return []


def _severity(value: float, high: float, medium: float) -> str:
    if value >= high:
        return "high"
    if value >= medium:
        return "medium"
    return "low"


def _load_boundary(raw: dict) -> BaseGeometry:
    try:
        boundary = shape(raw)
    except Exception as exc:  # noqa: BLE001 - 输入错误统一转为可读信息
        raise ValueError(f"boundary 不是合法的 GeoJSON 几何: {exc}") from exc
    if boundary.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"boundary 必须是 Polygon 或 MultiPolygon，当前为 {boundary.geom_type}")
    if not boundary.is_valid:
        fixed = boundary.buffer(0)
        if fixed.is_empty or fixed.area <= 0:
            raise ValueError("boundary 多边形自相交且无法自动修复，请检查坐标")
        boundary = fixed
    if boundary.area <= 0:
        raise ValueError("boundary 面积为 0")
    return boundary


def analyze(req: AnalysisRequest) -> dict[str, Any]:
    opts: Options = req.options
    boundary = _load_boundary(req.boundary)

    # ---- 1. 测线 -> 中心线 -> 扫掠缓冲（半扫宽、平头端帽） ----
    seen_ids: set[str] = set()
    centerlines: list[LineString] = []
    buffers: list[BaseGeometry] = []
    for ln in req.lines:
        if ln.id in seen_ids:
            raise ValueError(f"测线 id 重复: {ln.id!r}")
        seen_ids.add(ln.id)
        g = LineString([ln.start, ln.end])
        if g.length <= 0:
            raise ValueError(f"测线 {ln.id!r} 起点与终点重合，无法构成测线")
        centerlines.append(g)
        buffers.append(g.buffer(ln.swath / 2.0, cap_style=BufferCapStyle.flat))

    coverage = unary_union(buffers) if buffers else Polygon()
    covered_inside = coverage.intersection(boundary)
    gap_geom = boundary.difference(coverage)        # 界内未覆盖
    outside_geom = coverage.difference(boundary)    # 界外扫掠

    minx, miny, maxx, maxy = boundary.bounds
    extent = max(maxx - minx, maxy - miny, 1.0)
    touch_tol = extent * 1e-6  # “相邻”判定容差（相对坐标尺度）

    issues: list[dict[str, Any]] = []

    # ---- 2. 未覆盖缺口：boundary − ⋃扫掠区 ----
    gap_polys = _polygons(gap_geom)
    gap_polys.sort(key=lambda p: (p.centroid.x, p.centroid.y))  # 固定排序 -> 编号稳定
    n = 0
    for p in gap_polys:
        if p.area < opts.min_issue_area:
            continue
        n += 1
        related = [
            {"line": ln.id, "swath": ln.swath}
            for ln, buf in zip(req.lines, buffers)
            if buf.distance(p) <= touch_tol
        ]
        pct = 100.0 * p.area / boundary.area
        issues.append({
            "id": f"GAP-{n:03d}",
            "type": "gap",
            "type_label": ISSUE_TYPE_LABELS["gap"],
            "severity": _severity(pct, 5.0, 1.0),
            "area": r4(p.area),
            "centroid": [r6(p.centroid.x), r6(p.centroid.y)],
            "geometry": geom_to_geojson(p),
            "lines": [r["line"] for r in related],
            "basis": {
                "method": "gap = boundary − ⋃ buffer(line_i, swath_i/2)，按面积 ≥ min_issue_area 过滤",
                "boundary_area": r4(boundary.area),
                "gap_area": r4(p.area),
                "gap_pct_of_boundary": r4(pct),
                "min_issue_area": opts.min_issue_area,
                "adjacent_lines": related,
            },
        })

    # ---- 3. 边界外扫掠：⋃扫掠区 − boundary ----
    out_polys = _polygons(outside_geom)
    out_polys.sort(key=lambda p: (p.centroid.x, p.centroid.y))
    n = 0
    for p in out_polys:
        if p.area < opts.min_issue_area:
            continue
        n += 1
        per_line = []
        for ln, buf in zip(req.lines, buffers):
            a = buf.intersection(p).area
            if a > opts.min_issue_area * 1e-3:
                per_line.append({"line": ln.id, "swath": ln.swath, "outside_area": r4(a)})
        per_line.sort(key=lambda d: d["outside_area"], reverse=True)
        pct = 100.0 * p.area / coverage.area if coverage.area > 0 else 0.0
        issues.append({
            "id": f"OUT-{n:03d}",
            "type": "outside",
            "type_label": ISSUE_TYPE_LABELS["outside"],
            "severity": _severity(pct, 2.0, 0.5),
            "area": r4(p.area),
            "centroid": [r6(p.centroid.x), r6(p.centroid.y)],
            "geometry": geom_to_geojson(p),
            "lines": [d["line"] for d in per_line],
            "basis": {
                "method": "outside = ⋃ buffer(line_i, swath_i/2) − boundary；"
                          "per_line 按 buffer(line) ∩ 越界区块 计算，测线互相重叠部分会重复计数",
                "outside_area": r4(p.area),
                "outside_pct_of_coverage": r4(pct),
                "coverage_union_area": r4(coverage.area),
                "min_issue_area": opts.min_issue_area,
                "per_line": per_line,
            },
        })

    # ---- 4. 相邻测线过度重叠：buffer(L_i) ∩ buffer(L_{i+1}) ----
    pairs_checked = max(0, len(buffers) - 1)
    n = 0
    for i in range(pairs_checked):
        la, lb = req.lines[i], req.lines[i + 1]
        inter = buffers[i].intersection(buffers[i + 1])
        inter_polys = _polygons(inter)
        inter_area = sum(p.area for p in inter_polys)
        if inter_area < opts.min_issue_area:
            continue
        narrower = min(buffers[i].area, buffers[i + 1].area)
        ratio = inter_area / narrower if narrower > 0 else 0.0
        if ratio <= opts.max_overlap_ratio:
            continue
        n += 1
        geom = unary_union(inter_polys)
        spacing = centerlines[i].distance(centerlines[i + 1])
        issues.append({
            "id": f"OVL-{n:03d}",
            "type": "overlap",
            "type_label": ISSUE_TYPE_LABELS["overlap"],
            "severity": _severity(ratio, 0.5, 0.3),
            "area": r4(inter_area),
            "centroid": [r6(geom.centroid.x), r6(geom.centroid.y)],
            "geometry": geom_to_geojson(geom),
            "lines": [la.id, lb.id],
            "basis": {
                "method": "overlap = buffer(L_i) ∩ buffer(L_{i+1})；"
                          "ratio = overlap_area / min(buffer_area_i, buffer_area_{i+1})，"
                          "ratio > max_overlap_ratio 判为过度重叠",
                "swaths": {la.id: la.swath, lb.id: lb.swath},
                "buffer_areas": {la.id: r4(buffers[i].area), lb.id: r4(buffers[i + 1].area)},
                "centerline_spacing": r4(spacing),
                "overlap_area": r4(inter_area),
                "overlap_ratio": r4(ratio),
                "max_overlap_ratio": opts.max_overlap_ratio,
            },
        })

    # ---- 5. 汇总与逐测线统计 ----
    counts = {"gap": 0, "outside": 0, "overlap": 0}
    for it in issues:
        counts[it["type"]] += 1

    line_stats = []
    for ln, g, buf in zip(req.lines, centerlines, buffers):
        inside = buf.intersection(boundary).area
        line_stats.append({
            "id": ln.id,
            "start": [r6(ln.start[0]), r6(ln.start[1])],
            "end": [r6(ln.end[0]), r6(ln.end[1])],
            "length": r4(g.length),
            "swath": ln.swath,
            "buffer_area": r4(buf.area),
            "inside_area": r4(inside),
            "outside_area": r4(buf.area - inside),
        })

    summary = {
        "boundary_area": r4(boundary.area),
        "line_count": len(req.lines),
        "coverage_union_area": r4(coverage.area),
        "covered_inside_area": r4(covered_inside.area),
        "coverage_pct": r4(100.0 * covered_inside.area / boundary.area),
        "gap_area_total": r4(gap_geom.area),
        "gap_pct_of_boundary": r4(100.0 * gap_geom.area / boundary.area),
        "outside_area_total": r4(outside_geom.area),
        "adjacent_pairs_checked": pairs_checked,
        "issue_counts": counts,
        "issue_total": len(issues),
        "options_used": {
            "min_issue_area": opts.min_issue_area,
            "max_overlap_ratio": opts.max_overlap_ratio,
        },
    }

    return {
        "summary": summary,
        "issues": issues,
        "line_stats": line_stats,
        "geometries": {
            "coverage": geom_to_geojson(coverage) if not coverage.is_empty else None,
        },
        "meta": {
            "engine": "shapely",
            "shapely_version": shapely.__version__,
            "geos_version": ".".join(str(v) for v in shapely.geos_version),
            "cap_style": "flat（平头端帽，缓冲半径 = 扫宽/2）",
            "coordinate_note": "输入视为平面坐标，长度/面积单位与输入一致",
            "ordering": "问题按类型分组、组内按质心坐标升序编号，同一输入输出完全一致",
        },
    }
