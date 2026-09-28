"""交叉点水深闭合 —— 计划线交点处的水深偏置网平差。

计划测线沿用覆盖复核的 ``lines``，采集样本沿用连续性面板“按原始采集顺序”的
输入约定并增加 ``depth``。处理流程：

1. 仅处理 **两条计划线的点状交点**：线段相交结果为单点（含端点相接）；共线/重叠
   的线段相交结果为线性几何，整体跳过并说明原因 ``overlapping``。
2. 对每条参与交点的测线，按 **原始采集顺序** 在该线样本中寻找相邻样本对 (a, b)：
   - 两样本的投影里程跨过交点里程（``min(s_a,s_b) ≤ s_x ≤ max(s_a,s_b)``，允许
     采集方向与计划线 start→end 反向）；
   - 两端样本的投影里程到交点里程的绝对差（沿计划线里程）均 ≤ 包围距离
     ``max_enclosure_distance``——端点可能横向贴近测线但沿线离交点很远，
     横向投影距离不作为判限。
   候选超过一组时取两端沿线距离 |s−sₓ| 之和最小者，再以 a（再 b）的原始索引破同值；
   水深按里程线性插值 ``d = d_a + (d_b - d_a) * t``
   （t 为交点在 a→b 上的里程比例），采集方向反向时同样适用。
3. 用每个交点的水深差建立偏置方程：``b_li - b_lj = d_j - d_i``（字典序小者为左），
   交点残差 ``r = (d_i + b_i) - (d_j + b_j)``。按连通分量求解，每个分量固定
   **字典序最小测线偏置为零**（基准）；NumPy IRLS + Huber 权（阈值 ``huber_delta``）。
   观测数 = 节点数 - 1（一棵树）的分量无冗余，标为 ``indeterminate``（不可判异常）；
   其余按 |校正残差| 是否超过 ``residual_threshold`` 判定 ``ok`` / ``anomaly``。

样本不重排，所有排序均有确定键（字典序、交点坐标、原始索引），无随机数，
同一输入输出逐字节一致。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
from shapely.geometry import LineString

from .analysis import r4, r6
from .schemas import ClosureRequest

SKIP_LABELS = {
    "overlapping": "重叠线（交点不是唯一一点）",
    "no_bracketing_samples": "缺少有效包围样本",
    "unknown_line": "未知测线（该计划线无采集样本）",
}
STATUS_LABELS = {
    "ok": "闭合正常",
    "anomaly": "闭合异常",
    "indeterminate": "无冗余不可判",
}

IRLS_MAX_ITER = 50
IRLS_TOL = 1e-10

RULE_INTERSECTION = (
    "仅处理两条计划线的点状交点：两线段相交结果为唯一一点（含端点相接）；"
    "相交结果为线性几何（共线/重叠）或多点时按 overlapping 整体跳过"
)
RULE_ENCLOSURE = (
    "样本按原始采集顺序取同线相邻对 (a,b)：投影里程跨过交点 "
    "(min(s_a,s_b) ≤ s_x ≤ max(s_a,s_b)，允许采集方向反向)，"
    "且两端投影里程到交点里程的绝对差（沿线距离 |s-s_x|）均 ≤ max_enclosure_distance"
    "（端点贴近测线但沿线离交点过远同样超距拒绝；横向投影距离不参与判限）；"
    "候选多组时取两端沿线距离之和最小者，再以 a（再 b）的原始索引破同值；"
    "水深按里程线性插值 d = d_a + (d_b-d_a)·t，t=(s_x-s_a)/(s_b-s_a)"
)
RULE_ADJUSTMENT = (
    "偏置方程 b_li - b_lj = d_j - d_i（li < lj 字典序），交点残差 "
    "r = (d_i + b_i) - (d_j + b_j)；每连通分量固定字典序最小测线偏置为零，"
    "NumPy IRLS + Huber 权（|r|≤δ 权 1，否则 δ/|r|）；"
    "观测数 = 节点数-1 的分量无冗余标 indeterminate，"
    "其余按 |r| > residual_threshold 判 anomaly"
)


class _PlannedLine:
    __slots__ = ("id", "x0", "y0", "dx", "dy", "length")

    def __init__(self, lid: str, start: list[float], end: list[float]):
        self.id = lid
        self.x0, self.y0 = start[0], start[1]
        self.dx, self.dy = end[0] - start[0], end[1] - start[1]
        self.length = math.hypot(self.dx, self.dy)

    @property
    def geom(self) -> LineString:
        return LineString([(self.x0, self.y0), (self.x0 + self.dx, self.y0 + self.dy)])

    def project(self, x: float, y: float) -> tuple[float, float]:
        """返回样本在计划线上的 (投影里程 s, 投影距离 dist)。

        s 为沿 start→end 的有向里程（可能落到 [0, length] 之外）；
        dist 为到计划线所在无限直线的垂直距离。
        """
        s = ((x - self.x0) * self.dx + (y - self.y0) * self.dy) / (self.length * self.length)
        px = self.x0 + s * self.dx
        py = self.y0 + s * self.dy
        return s * self.length, math.hypot(x - px, y - py)


def check_closure(req: ClosureRequest) -> dict[str, Any]:
    opts = req.options
    max_d = opts.max_enclosure_distance

    # ---- 0. 计划测线（沿用任务输入中的 lines；样本按原始顺序，不重排） ----
    lines: dict[str, _PlannedLine] = {}
    line_order: list[str] = []
    for ln in req.lines:
        if ln.id in lines:
            raise ValueError(f"测线 id 重复: {ln.id!r}")
        if math.hypot(ln.end[0] - ln.start[0], ln.end[1] - ln.start[1]) <= 0:
            raise ValueError(f"测线 {ln.id!r} 起点与终点重合，无法构成测线")
        lines[ln.id] = _PlannedLine(ln.id, ln.start, ln.end)
        line_order.append(ln.id)

    # 样本按测线分组，保持原始采集顺序；未知测线样本单列
    by_line: dict[str, list[dict[str, Any]]] = {lid: [] for lid in line_order}
    unknown_line_ids: list[str] = []
    unknown_counts: dict[str, int] = {}
    for idx, s in enumerate(req.samples):
        if s.line_id in lines:
            by_line[s.line_id].append({"i": idx, "x": s.x, "y": s.y, "depth": s.depth})
        else:
            unknown_counts[s.line_id] = unknown_counts.get(s.line_id, 0) + 1
            if s.line_id not in unknown_line_ids:
                unknown_line_ids.append(s.line_id)

    # 每条计划线样本的投影预算（里程 / 投影距离）
    proj: dict[str, list[tuple[float, float]]] = {}
    for lid, ss in by_line.items():
        pl = lines[lid]
        proj[lid] = [pl.project(s["x"], s["y"]) for s in ss]

    # ---- 1. 两两计划线求交，只处理唯一的点状交点 ----
    point_pairs: list[tuple[str, str, float, float]] = []
    skips: list[dict[str, Any]] = []
    for a in range(len(line_order)):
        for b in range(a + 1, len(line_order)):
            la, lb = line_order[a], line_order[b]
            inter = lines[la].geom.intersection(lines[lb].geom)
            gtype = inter.geom_type
            if gtype == "Point":
                point_pairs.append((la, lb, inter.x, inter.y))
            elif inter.is_empty:
                continue  # 两线段不相交（含平行错开、共线但无重叠——shapely 返回零长度 LineString）
            elif gtype == "MultiPoint":
                # 退化情形：多点相接，交点不唯一，按重叠处理
                skips.append({
                    "kind": "overlapping",
                    "kind_label": SKIP_LABELS["overlapping"],
                    "lines": [la, lb],
                    "intersection_type": gtype,
                    "reason": f"{la} 与 {lb} 相交结果为 {gtype}，交点不唯一，无法做单点闭合",
                })
            elif gtype in ("LineString", "MultiLineString"):
                if inter.length <= 0:
                    continue  # 共线但互不重叠，相交退化为零长度线
                skips.append({
                    "kind": "overlapping",
                    "kind_label": SKIP_LABELS["overlapping"],
                    "lines": [la, lb],
                    "intersection_type": gtype,
                    "overlap_length": r4(inter.length),
                    "reason": f"{la} 与 {lb} 共线重叠（相交结果为 {gtype}，长度 {r4(inter.length)}），非点状交点",
                })
            else:
                # GeometryCollection 等其他非点状相交结果：交点不唯一，按重叠处理
                skips.append({
                    "kind": "overlapping",
                    "kind_label": SKIP_LABELS["overlapping"],
                    "lines": [la, lb],
                    "intersection_type": gtype,
                    "reason": f"{la} 与 {lb} 相交结果为 {gtype}，不是唯一点状交点，无法做单点闭合",
                })

    # 点状交点先按坐标、再按测线名稳定排序，编号 XPT-001…
    point_pairs.sort(key=lambda p: (r6(p[2]), r6(p[3]), p[0], p[1]))

    # ---- 2. 逐交点、逐线找有效包围样本并按里程线性插值水深 ----
    intersections: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []  # 偏置方程（有效交点），边交点按 XPT 序
    for n, (lid_a, lid_b, ix, iy) in enumerate(point_pairs, 1):
        xpt_id = f"XPT-{n:03d}"
        missing: list[str] = []
        interp: dict[str, dict[str, Any]] = {}
        bracket_counts: dict[str, tuple[int, int]] = {}
        for lid in (lid_a, lid_b):
            pl = lines[lid]
            ss = by_line[lid]
            if not ss:
                # 该计划线没有任何采集样本 -> 未知测线（该线未采集）
                missing.append(lid)
                continue
            s_x = pl.project(ix, iy)[0]  # 交点在该线 start→end 上的里程（投影距离≈0）
            ps = proj[lid]
            best: tuple[float, int, int, int] | None = None
            n_bracket = 0  # 投影里程跨过交点的相邻对数（不论是否超距）
            n_eligible = 0  # 其中两端沿线距离均 ≤ 包围距离的候选数
            # 原始顺序扫描相邻样本对（样本不重排）；判限用端点投影里程与交点里程
            # 的绝对差（沿线距离），候选排序键 =（两端沿线距离和, a 原始索引, b 原始索引）
            for k in range(1, len(ss)):
                i_a, i_b = ss[k - 1]["i"], ss[k]["i"]
                s_a, _d_a = ps[k - 1]
                s_b, _d_b = ps[k]
                d_along_a, d_along_b = abs(s_a - s_x), abs(s_b - s_x)
                if min(s_a, s_b) <= s_x <= max(s_a, s_b):
                    n_bracket += 1
                    if d_along_a <= max_d and d_along_b <= max_d:
                        n_eligible += 1
                        key = (d_along_a + d_along_b, i_a, i_b)
                        if best is None or key < best[:3]:
                            best = (key[0], i_a, i_b, k)
            if best is None:
                missing.append(lid)
                continue
            bracket_counts[lid] = (n_bracket, n_eligible)
            _sum_d, i_a, i_b, k = best
            s_a, d_a = ps[k - 1]
            s_b, d_b = ps[k]
            along_a, along_b = abs(s_a - s_x), abs(s_b - s_x)
            sa, sb = ss[k - 1], ss[k]
            t = (s_x - s_a) / (s_b - s_a) if s_b != s_a else 0.0
            depth = sa["depth"] + (sb["depth"] - sa["depth"]) * t
            interp[lid] = {
                "station_m": r4(s_x),
                "max_enclosure_distance": max_d,
                "bracketing_pair_count": bracket_counts[lid][0],
                "eligible_pair_count": bracket_counts[lid][1],
                "endpoints": [
                    {
                        "index": i_a,
                        "x": r6(sa["x"]), "y": r6(sa["y"]),
                        "station_m": r4(s_a),
                        "alongtrack_distance_m": r4(along_a),
                        "projection_distance": r4(d_a),
                        "distance_to_intersection": r4(math.hypot(sa["x"] - ix, sa["y"] - iy)),
                        "depth": r4(sa["depth"]),
                    },
                    {
                        "index": i_b,
                        "x": r6(sb["x"]), "y": r6(sb["y"]),
                        "station_m": r4(s_b),
                        "alongtrack_distance_m": r4(along_b),
                        "projection_distance": r4(d_b),
                        "distance_to_intersection": r4(math.hypot(sb["x"] - ix, sb["y"] - iy)),
                        "depth": r4(sb["depth"]),
                    },
                ],
                "ratio_t": r6(t),
                "interpolated_depth": r4(depth),
                "_depth_raw": depth,  # 原始精度参与平差（输出前移除）
            }

        if missing:
            # 每个无法插值的线各给一条跳过说明：无线样本 -> unknown_line，否则 -> no_bracketing
            for lid in missing:
                kind = "unknown_line" if not by_line[lid] else "no_bracketing_samples"
                if kind == "unknown_line":
                    reason = (f"交点 {xpt_id}（{lid_a} × {lid_b} @ [{r6(ix)}, {r6(iy)}]）处"
                              f"测线 {lid} 没有任何采集样本（未知测线/未采集），无法插值水深")
                else:
                    reason = (f"交点 {xpt_id}（{lid_a} × {lid_b} @ [{r6(ix)}, {r6(iy)}]）处"
                              f"测线 {lid} 缺少有效包围样本：没有投影里程跨过交点"
                              f"（里程 {r4(lines[lid].project(ix, iy)[0])}）且两端投影里程"
                              f"到交点里程的绝对差（沿线距离）均 ≤ {max_d} 的同线相邻样本对"
                              f"（端点可能横向贴近测线，但沿线离交点过远同样超距拒绝）")
                skips.append({
                    "kind": kind,
                    "kind_label": SKIP_LABELS[kind],
                    "lines": sorted([lid_a, lid_b]),
                    "intersection_id": xpt_id,
                    "line": lid,
                    "point": [r6(ix), r6(iy)],
                    "reason": reason,
                })
            continue

        li, lj = sorted([lid_a, lid_b])
        di = interp[li]["_depth_raw"]
        dj = interp[lj]["_depth_raw"]
        raw_misclosure = di - dj  # 原始闭合差 = 校正前水深差
        edges.append({
            "xpt_id": xpt_id, "li": li, "lj": lj,
            "di": di, "dj": dj, "raw": raw_misclosure,
        })
        intersections.append({
            "id": xpt_id,
            "lines": sorted([lid_a, lid_b]),
            "point": [r6(ix), r6(iy)],
            "interpolations": interp,
            "raw_misclosure": r4(raw_misclosure),
        })

    # ---- 3. 偏置方程网：连通分量 + 字典序最小测线固定为零 ----
    parent: dict[str, str] = {lid: lid for lid in line_order}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        parent[find(e["li"])] = find(e["lj"])

    comp_members: dict[str, list[str]] = {}
    for lid in line_order:
        comp_members.setdefault(find(lid), []).append(lid)
    # 分量按其字典序最小成员排序，编号 CMP-1…；只保留含观测的分量
    comps = sorted((sorted(m) for m in comp_members.values() if len(m) > 1),
                   key=lambda m: m[0])

    biases: dict[str, float] = {}
    component_by_line: dict[str, str] = {}
    component_records: list[dict[str, Any]] = []
    edge_component: dict[str, str] = {}
    for ci, members in enumerate(comps, 1):
        cid = f"CMP-{ci}"
        mset = set(members)
        anchor = members[0]  # 已排序，字典序最小
        for lid in members:
            component_by_line[lid] = cid
        comp_edges = [e for e in edges if e["li"] in mset]
        for e in comp_edges:
            edge_component[e["xpt_id"]] = cid
        idx = {lid: j for j, lid in enumerate(members)}
        n_nodes = len(members)
        m_obs = len(comp_edges)
        # 设计矩阵：方程 r = b_li - b_lj - (dj - di)，即 A b = y，y = dj - di
        A = np.zeros((m_obs, n_nodes), dtype=np.float64)
        y = np.zeros(m_obs, dtype=np.float64)
        for r_i, e in enumerate(comp_edges):
            A[r_i, idx[e["li"]]] = 1.0
            A[r_i, idx[e["lj"]]] = -1.0
            y[r_i] = e["dj"] - e["di"]

        redundant = m_obs >= n_nodes  # n-1 为树（恰好确定基准，无冗余）
        # Huber IRLS（初始权全 1）；基准列消去：anchor 固定为零
        free_cols = [j for j in range(n_nodes) if members[j] != anchor]
        Af = A[:, free_cols]
        prev_w = np.zeros(m_obs, dtype=np.float64)
        w = np.ones(m_obs, dtype=np.float64)
        beta = np.zeros(n_nodes, dtype=np.float64)
        iterations = 0
        converged = False
        for it in range(1, IRLS_MAX_ITER + 1):
            # 加权法方程 (AᵀWA) x = AᵀWy，W 为对角阵，逐行缩放避免显式构造
            Aw = Af * w[:, None]
            N = Aw.T @ Af
            rhs = Aw.T @ y
            x_hat = np.linalg.solve(N, rhs)
            beta = np.zeros(n_nodes, dtype=np.float64)
            beta[free_cols] = x_hat
            resid = A @ beta - y
            absw = np.abs(resid)
            w = np.where(absw <= opts.huber_delta, 1.0,
                         opts.huber_delta / np.where(absw > 0, absw, 1e-300))
            iterations = it
            if it > 1 and float(np.max(np.abs(w - prev_w))) < IRLS_TOL:
                converged = True
                break
            prev_w = w.copy()
        residuals = A @ beta - y
        for lid, bval in zip(members, beta):
            biases[lid] = float(bval)
        component_records.append({
            "id": cid,
            "lines": members,
            "anchor_line": anchor,
            "redundancy": m_obs - (n_nodes - 1),
            "observation_count": m_obs,
            "node_count": n_nodes,
            "redundant": redundant,
            "huber_delta": opts.huber_delta,
            "iterations": iterations,
            "converged": converged,
            "residual_rms": r4(float(np.sqrt(np.mean(residuals ** 2)))) if m_obs else None,
        })

    # ---- 4. 交点判定：原始闭合差 / 最终权重 / 偏置 / 校正残差 ----
    comp_index = {c["id"]: c for c in component_records}
    anomaly_count = 0
    ok_count = 0
    indeterminate_count = 0
    for xpt, e in zip(intersections, edges):
        cid = edge_component[xpt["id"]]
        comp = comp_index[cid]
        bi = biases[e["li"]]
        bj = biases[e["lj"]]
        r = (e["di"] + bi) - (e["dj"] + bj)
        # 最终 Huber 权（用最终校正残差重算一次，与 IRLS 末次更新一致）
        weight = 1.0 if abs(r) <= opts.huber_delta else opts.huber_delta / abs(r)
        if not comp["redundant"]:
            status = "indeterminate"
            indeterminate_count += 1
        elif abs(r) > opts.residual_threshold:
            status = "anomaly"
            anomaly_count += 1
        else:
            status = "ok"
            ok_count += 1
        xpt.update({
            "component_id": cid,
            "status": status,
            "status_label": STATUS_LABELS[status],
            "raw_misclosure": r4(e["raw"]),
            "depth_left": r4(e["di"]),
            "depth_right": r4(e["dj"]),
            "final_weight": r6(weight),
            "biases": {e["li"]: r4(bi), e["lj"]: r4(bj)},
            "corrected_residual": r4(r),
        })

    # 插值明细去掉内部字段，只保留圆整后的可复核输出
    for xpt in intersections:
        for v in xpt["interpolations"].values():
            v.pop("_depth_raw", None)

    # ---- 5. 跳过记录稳定排序，编号 SKP-001… ----
    # 交点级跳过：overlapping / unknown_line / no_bracketing
    # 样本中出现任务输入未定义的未知测线：交点无关，排在最后
    for lid in unknown_line_ids:
        skips.append({
            "kind": "unknown_line",
            "kind_label": SKIP_LABELS["unknown_line"],
            "lines": [],
            "line": lid,
            "sample_count": unknown_counts[lid],
            "reason": f"样本中出现任务输入未定义的测线 {lid}（{unknown_counts[lid]} 个样本），"
                      "该测线不参与任何交点插值",
        })
    kind_rank = {"overlapping": 0, "unknown_line": 1, "no_bracketing_samples": 2}
    skips.sort(key=lambda d: (kind_rank[d["kind"]],
                              d.get("intersection_id") or "",
                              d.get("line") or "",
                              d["lines"][0] if d["lines"] else "",
                              d["lines"][1] if len(d["lines"]) > 1 else ""))
    for k, sk in enumerate(skips, 1):
        sk["id"] = f"SKP-{k:03d}"

    # ---- 6. 逐测线偏置（按输入顺序，含无观测线） ----
    line_biases = []
    for lid in line_order:
        cid = component_by_line.get(lid)
        line_biases.append({
            "line_id": lid,
            "component_id": cid,
            "bias": r4(biases[lid]) if lid in biases else None,
            "is_anchor": cid is not None and comp_index[cid]["anchor_line"] == lid,
            "sample_count": len(by_line[lid]),
            "intersection_count": sum(1 for x in intersections if lid in x["lines"]),
        })

    summary = {
        "planned_line_count": len(line_order),
        "sample_count": len(req.samples),
        "point_intersection_count": len(point_pairs),
        "intersection_count": len(intersections),
        "ok_count": ok_count,
        "anomaly_count": anomaly_count,
        "indeterminate_count": indeterminate_count,
        "skip_count": len(skips),
        "skip_counts": {
            "overlapping": sum(1 for s in skips if s["kind"] == "overlapping"),
            "no_bracketing_samples": sum(1 for s in skips if s["kind"] == "no_bracketing_samples"),
            "unknown_line": sum(1 for s in skips if s["kind"] == "unknown_line"),
        },
        "component_count": len(component_records),
        "unknown_line_ids": unknown_line_ids,
        "options_used": {
            "max_enclosure_distance": opts.max_enclosure_distance,
            "huber_delta": opts.huber_delta,
            "residual_threshold": opts.residual_threshold,
        },
    }

    return {
        "summary": summary,
        "intersections": intersections,
        "skips": skips,
        "components": component_records,
        "line_biases": line_biases,
        "meta": {
            "engine": "numpy+shapely",
            "numpy_version": np.__version__,
            "index_note": "endpoints[].index 为 0-based 原始输入顺序，样本不做任何重排",
            "intersection_rule": RULE_INTERSECTION,
            "enclosure_rule": RULE_ENCLOSURE,
            "adjustment_rule": RULE_ADJUSTMENT,
            "projection_note": "投影里程沿计划线 start→end（可越出 [0,length]）；"
                               "投影距离为样本到计划线所在无限直线的垂直距离，仅作横向质量参考、不参与判限；"
                               "包围判限为端点投影里程到交点里程的绝对差（沿线距离），"
                               "两端均 ≤ max_enclosure_distance 的相邻对才是有效候选",
            "skip_reasons": SKIP_LABELS,
            "status_labels": STATUS_LABELS,
            "coordinate_note": "输入视为平面坐标，水深与坐标长度单位一致即可",
            "ordering": "点状交点按 (x,y,线名) 升序编号 XPT-001…；跳过记录按类型/交点/测线排序编号 SKP-001…；"
                        "连通分量按字典序最小成员排序编号 CMP-1…，同一输入输出完全一致",
        },
    }
