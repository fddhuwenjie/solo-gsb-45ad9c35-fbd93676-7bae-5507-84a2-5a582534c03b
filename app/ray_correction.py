"""声速射线改正 —— 分层声速剖面下的 Snell 射线追踪。

每个波束给出换能器位置/朝向（``x, y, heading, transducer_depth``）、双程传播时间
``two_way_travel_time`` 与相对垂直方向的有符号入射角 ``angle_deg``（正角指右舷）。
处理流程：

1. 双程时间取一半得到单程时间 ``t_half``；以入射层（换能器深度所在层）声速 ``c0``
   与入射角 ``θ0`` 计算射线参数（Snell 常量）``p = sin(θ0) / c0``，全程不变。
2. 自换能器深度起逐层向下追踪。在声速 ``c`` 的层内：
   ``sin θ = p·c``、``cos θ = √(1−sin²θ)``，垂向单位深度耗时
   ``dt/dz = 1/(c·cos θ)``，横向单位深度位移 ``dx/dz = tan θ = p·c/√(1−(p·c)²)``。
   每过一层按界面处入射角累计单程耗时与（有符号）横距，累计方式与层的进入深度无关，
   因而换能器位于层中部时首段按部分厚度计算。
3. 时间恰好在某层内耗尽：该层只走部分厚度，给出改正落点；总累计耗时恰为 ``t_half``
   （在数值容差内）到达层底也按成功处理。
4. 失败（禁止外推，已走层段全部保留）：
   - ``refraction_limit``：任一界面（含入射层）出现 ``|p·c| ≥ 1``，发生全反射/临界
     折射，射线无法进入该层；
   - ``profile_gap``：换能器深度不在任何层内，或射线在某层底耗尽时间后下一层顶更深
     （剖面断档）；
   - ``beyond_profile_bottom``：射线穿过剖面最深层底后仍有剩余单程时间。

平面换算：横距以正角指向右舷；航向自正北顺时针，右舷方位 = heading + 90°，
``dx = cross·sin(h+90°) = cross·cos h``，``dy = cross·cos(h+90°) = −cross·sin h``。
对照值（``straight``）始终给出：以入射声速 ``c0`` 沿入射方向做直线传播，
走过距离 ``c0·t_half``。

剖面整体校验（返回 400）：声速为正、层厚为正、层间不得重叠；允许断档（运行时按波束失败）。
层按顶深排序后处理，同一输入输出逐字节一致（无随机、无时间戳）。
"""
from __future__ import annotations

import math
from typing import Any

from .analysis import r4, r6
from .schemas import RayCorrectionRequest

# 临界判定 |p·c| ≥ 1 时为避免浮点误差把合法近临界射线误判为全反射的容差
CRIT_EPS = 1e-12
# 时间“恰好”耗尽在层底的容差（秒）
T_EPS = 1e-12
# 层边界包含判定容差（长度单位）
DEPTH_EPS = 1e-9

FAIL_LABELS = {
    "refraction_limit": "折射极限（|p·c| ≥ 1，全反射）",
    "profile_gap": "剖面断档（换能器不在层内或层间存在空缺，禁止外推）",
    "beyond_profile_bottom": "时间超过剖面底界（禁止外推）",
}
STATUS_LABELS = {"ok": "改正成功", "failed": "改正失败"}

RULE_PROFILE = (
    "剖面由连续且不重叠的 (top_depth, bottom_depth, sound_speed) 层组成："
    "层间不得重叠（整体 400 拒绝），允许断档（射线进入断档则该波束失败，禁止外推）；"
    "层按 top_depth 升序处理"
)
RULE_RAY = (
    "单程时间 t = two_way_travel_time / 2；入射层射线参数 p = sin(θ0)/c0 全程常量；"
    "层内 sin θ = p·c，cos θ = √(1−(p·c)²)，单位垂深耗时 1/(c·cos θ)，"
    "单位垂深横距 tan θ = p·c/√(1−(p·c)²)，逐层累计单程耗时与有符号横距"
)
RULE_FAIL = (
    "|p·c| ≥ 1（含入射层）-> refraction_limit；换能器深度不在任何层内或层底与下一层顶"
    "之间存在深度空缺 -> profile_gap；穿过最深层底仍有剩余时间 -> beyond_profile_bottom；"
    "失败波束只保留已完整走过（含入射部分段）的层段，不给出改正落点，禁止外推"
)
RULE_GEOMETRY = (
    "正角指右舷；heading 自正北顺时针，右舷方位 = heading + 90°，"
    "落点 x = x0 + cross·cos(heading)，y = y0 − cross·sin(heading)；"
    "对照直线 = 以入射声速 c0 沿入射方向直线传播 c0·t"
)


class _Layer:
    __slots__ = ("top", "bottom", "c", "input_index")

    def __init__(self, top: float, bottom: float, c: float, input_index: int):
        self.top = top
        self.bottom = bottom
        self.c = c
        self.input_index = input_index


def _validate_profile(layers_in: list[Any]) -> list[_Layer]:
    """校验并排序剖面。重叠/零厚度/非正声速为整体输入错误（ValueError -> 400）。"""
    layers = [
        _Layer(float(ly.top_depth), float(ly.bottom_depth), float(ly.sound_speed), i)
        for i, ly in enumerate(layers_in)
    ]
    # 零厚度 / 负厚度
    for ly in layers:
        if ly.bottom <= ly.top:
            raise ValueError(
                f"声速层 #{ly.input_index}（{ly.top} → {ly.bottom}）厚度必须为正"
            )
    order = sorted(range(len(layers)), key=lambda i: (layers[i].top, layers[i].bottom))
    sorted_layers = [layers[i] for i in order]
    # 重叠判定（端点相接不算重叠）
    for k in range(1, len(sorted_layers)):
        prev, cur = sorted_layers[k - 1], sorted_layers[k]
        if cur.top < prev.bottom - DEPTH_EPS:
            raise ValueError(
                f"声速层 #{cur.input_index}（顶深 {cur.top}）与层 "
                f"#{prev.input_index}（底深 {prev.bottom}）重叠；剖面层不得重叠"
            )
    return sorted_layers


def _layer_containing(layers: list[_Layer], depth: float) -> int:
    """换能器深度所在层。层界面优先归属下方层（要求上下相接）；断档上沿留在本层。"""
    for k, ly in enumerate(layers):
        if ly.top - DEPTH_EPS <= depth < ly.bottom - DEPTH_EPS:
            return k
        if abs(depth - ly.bottom) <= DEPTH_EPS:
            below_joins = (
                k + 1 < len(layers)
                and abs(layers[k + 1].top - ly.bottom) <= DEPTH_EPS
            )
            return k + 1 if below_joins else k
    return -1


def _segment_view(
    layer: _Layer, z_enter: float, z_exit: float,
    angle: float, sin_v: float, cos_v: float,
    dt: float, vert: float, cross: float, cumulative_t: float,
) -> dict[str, Any]:
    dist = layer.c * dt
    complete = (
        abs(z_enter - layer.top) <= DEPTH_EPS
        and abs(z_exit - layer.bottom) <= DEPTH_EPS
    )
    return {
        "layer_index": layer.input_index,
        "top_depth": r4(layer.top),
        "bottom_depth": r4(layer.bottom),
        "sound_speed": r4(layer.c),
        "angle_deg": r4(angle),
        "sin_angle": r6(sin_v),
        "cos_angle": r6(cos_v),
        "enter_depth": r4(z_enter),
        "exit_depth": r4(z_exit),
        "vertical": r4(vert),
        "cross_track": r4(cross),
        "path_length": r4(dist),
        "travel_time": r4(dt),
        "elapsed_one_way_time": r4(cumulative_t),
        "complete_layer": complete,
    }


def _trace_beam(b: Any, layers: list[_Layer]) -> dict[str, Any]:
    """对单个波束做单程 Snell 射线追踪。"""
    t_half = float(b.two_way_travel_time) * 0.5
    theta0 = math.radians(float(b.angle_deg))
    z0 = float(b.transducer_depth)
    heading = math.radians(float(b.heading))

    # 对照：入射声速直线传播（与剖面结构无关，始终可算）
    straight: dict[str, Any] | None = None
    entry_layer_idx = _layer_containing(layers, z0)

    result: dict[str, Any] = {
        "id": b.id,
        "input_index": 0,  # 由调用方填写
        "x": r6(b.x),
        "y": r6(b.y),
        "heading": r4(b.heading),
        "transducer_depth": r4(z0),
        "angle_deg": r4(b.angle_deg),
        "two_way_travel_time": r4(b.two_way_travel_time),
        "one_way_travel_time": r4(t_half),
        "segments": [],
    }

    if entry_layer_idx < 0:
        result.update(_failed(
            "profile_gap",
            f"换能器深度 {r4(z0)} 不在任何声速层内（剖面范围"
            f"{r4(layers[0].top)}…{r4(layers[-1].bottom)}，且不允许外推）",
            z0, z0, 0.0, 0.0, 0.0, None, straight, t_half,
        ))
        return result

    l0 = layers[entry_layer_idx]
    c0 = l0.c
    p = math.sin(theta0) / c0
    result["ray_parameter_p"] = r6(p)
    result["entry_layer_index"] = l0.input_index
    result["entry_sound_speed"] = r4(c0)

    # 对照直线
    s0 = abs(math.sin(theta0))
    cos0 = math.sqrt(max(0.0, 1.0 - s0 * s0))
    straight_vert = c0 * t_half * cos0
    straight_cross = math.copysign(c0 * t_half * s0, b.angle_deg) if b.angle_deg != 0 else 0.0
    straight_x = float(b.x) + straight_cross * math.cos(heading)
    straight_y = float(b.y) - straight_cross * math.sin(heading)
    straight = {
        "sound_speed": r4(c0),
        "path_length": r4(c0 * t_half),
        "vertical": r4(straight_vert),
        "cross_track": r4(straight_cross),
        "footprint_depth": r4(z0 + straight_vert),
        "point": [r6(straight_x), r6(straight_y)],
    }
    result["straight"] = straight

    # 入射层临界判定（θ0=±90° 或声速不允许该 p）
    if abs(p * c0) >= 1.0 - CRIT_EPS:
        result.update(_failed(
            "refraction_limit",
            f"入射层（层 #{l0.input_index}，c={r4(c0)}）处 |p·c| = "
            f"{r6(abs(p * c0))} ≥ 1，入射角已达临界，射线不能向下传播",
            z0, z0, 0.0, 0.0, 0.0, 0.0, straight, t_half,
        ))
        return result

    z = z0
    elapsed = 0.0
    vert_total = 0.0
    cross_total = 0.0
    segments: list[dict[str, Any]] = []
    ki = entry_layer_idx
    while True:
        ly = layers[ki]
        c = ly.c
        pc = p * c
        if abs(pc) >= 1.0 - CRIT_EPS:
            result.update(_failed(
                "refraction_limit",
                f"进入层 #{ly.input_index}（深度 {r4(ly.top)}，c={r4(c)}）时 "
                f"|p·c| = {r6(abs(pc))} ≥ 1，发生全反射/临界折射，射线不能进入该层",
                z0, z, elapsed, vert_total, cross_total, p, straight, t_half, segments,
            ))
            return result
        cos_v = math.sqrt(max(0.0, 1.0 - pc * pc))
        sin_v = pc  # 保留 p 的符号
        angle = math.degrees(math.asin(pc))
        # 从当前深度走到层底所需的单程时间与横距（临界已在上一步排除，cos_v > 0）
        dz_full = ly.bottom - z
        dt_full = dz_full / (c * cos_v)
        cross_full = dz_full * sin_v / cos_v
        remain = t_half - elapsed
        if remain < dt_full - T_EPS:
            # 时间在本层内耗尽：部分厚度，成功
            dz = c * cos_v * remain
            d_cross = dz * sin_v / cos_v
            z_new = z + dz
            elapsed = t_half
            vert_total += dz
            cross_total += d_cross
            segments.append(_segment_view(
                ly, z, z_new, angle, sin_v, cos_v, remain, dz, d_cross, elapsed))
            result.update(_success(
                z_new, elapsed, vert_total, cross_total,
                segments, p, straight, b, heading, t_half,
            ))
            return result
        # 完整走过本层（含恰在层底耗尽时间的情形）
        segments.append(_segment_view(
            ly, z, ly.bottom, angle, sin_v, cos_v, dt_full,
            dz_full, cross_full, elapsed + dt_full))
        elapsed += dt_full
        vert_total += dz_full
        cross_total += cross_full
        z = ly.bottom
        # 恰好在层底耗尽：若下方还有层则成功落点即在界面处；本层即最深层同样成功
        if t_half - elapsed <= T_EPS:
            result.update(_success(
                z, elapsed, vert_total, cross_total,
                segments, p, straight, b, heading, t_half,
            ))
            return result
        # 还有剩余时间：需要进入下一层
        if ki + 1 >= len(layers):
            result.update(_failed(
                "beyond_profile_bottom",
                f"射线穿过最深层 #{ly.input_index} 底界 {r4(ly.bottom)} 后仍有 "
                f"{r4(t_half - elapsed)} s 单程时间剩余，剖面以下禁止外推",
                z0, z, elapsed, vert_total, cross_total, p, straight, t_half, segments,
            ))
            return result
        nxt = layers[ki + 1]
        if nxt.top > ly.bottom + DEPTH_EPS:
            result.update(_failed(
                "profile_gap",
                f"射线在深度 {r4(ly.bottom)}（层 #{ly.input_index} 底）耗尽该层后，"
                f"下一层 #{nxt.input_index} 顶深为 {r4(nxt.top)}，存在 "
                f"{r4(nxt.top - ly.bottom)} 的断档，剩余 {r4(t_half - elapsed)} s，禁止外推",
                z0, z, elapsed, vert_total, cross_total, p, straight, t_half, segments,
            ))
            return result
        ki += 1


def _failed(reason: str, message: str, z0: float, depth: float, elapsed: float,
            vert: float, cross: float, p: float | None,
            straight: dict[str, Any] | None, t_half: float,
            segments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "status": "failed",
        "status_label": STATUS_LABELS["failed"],
        "failure_reason": reason,
        "failure_label": FAIL_LABELS[reason],
        "failure_message": message,
        "segments": segments or [],
        "reached_depth": r4(depth),
        "elapsed_one_way_time": r4(elapsed),
        "remaining_one_way_time": r4(max(0.0, t_half - elapsed)),
        "vertical_traveled": r4(vert),
        "cross_track_traveled": r4(cross),
        "ray_parameter_p": r6(p) if p is not None else None,
        "footprint_depth": None,
        "cross_track": None,
        "point": None,
        "ray_path": _ray_path(z0, segments or [], reached_only=True),
        "straight": straight,
        "deviation": None,
    }


def _success(depth: float, elapsed: float,
             vert: float, cross: float, segments: list[dict[str, Any]],
             p: float, straight: dict[str, Any], b: Any,
             heading: float, t_half: float) -> dict[str, Any]:
    fx = float(b.x) + cross * math.cos(heading)
    fy = float(b.y) - cross * math.sin(heading)
    deviation = math.hypot(fx - straight["point"][0], fy - straight["point"][1])
    return {
        "status": "ok",
        "status_label": STATUS_LABELS["ok"],
        "failure_reason": None,
        "failure_label": None,
        "failure_message": None,
        "reached_depth": r4(depth),
        "elapsed_one_way_time": r4(elapsed),
        "remaining_one_way_time": r4(max(0.0, t_half - elapsed)),
        "vertical_traveled": r4(vert),
        "cross_track_traveled": r4(cross),
        "ray_parameter_p": r6(p),
        "segments": segments,
        "footprint_depth": r4(depth),
        "cross_track": r4(cross),
        "point": [r6(fx), r6(fy)],
        "ray_path": _ray_path(float(b.transducer_depth), segments, reached_only=False),
        "straight": straight,
        "deviation": r4(deviation),
    }


def _ray_path(z0: float, segments: list[dict[str, Any]],
              reached_only: bool) -> dict[str, Any]:
    """给出剖面视图（横轴=有符号横距，纵轴=向下垂深）的折线路径，供页面绘制。

    首点为换能器 (0, transducer_depth)，其后逐段累加单段横距。
    """
    points: list[list[float]] = [[r6(0.0), r4(z0)]]
    acc = 0.0
    for seg in segments:
        acc += seg["cross_track"]
        points.append([r6(acc), r4(seg["exit_depth"])])
    return {
        "coordinates": points,  # [cross_track, depth]，depth 向下为正
        "complete": not reached_only,
    }


def ray_correct(req: RayCorrectionRequest) -> dict[str, Any]:
    layers = _validate_profile(req.profile)
    profile_view = [
        {
            "layer_index": ly.input_index,
            "top_depth": r4(ly.top),
            "bottom_depth": r4(ly.bottom),
            "sound_speed": r4(ly.c),
        }
        for ly in layers
    ]

    beams_out: list[dict[str, Any]] = []
    fail_counts = {k: 0 for k in FAIL_LABELS}
    ok_count = 0
    for i, b in enumerate(req.beams):
        beam_id = b.id if b.id else f"B-{i + 1:03d}"
        view = _trace_beam(b, layers)
        view["id"] = beam_id
        view["input_index"] = i
        beams_out.append(view)
        if view["status"] == "ok":
            ok_count += 1
        else:
            fail_counts[view["failure_reason"]] += 1

    n = len(beams_out)
    summary = {
        "beam_count": n,
        "ok_count": ok_count,
        "failed_count": n - ok_count,
        "failure_counts": fail_counts,
        "layer_count": len(layers),
        "profile_top": r4(layers[0].top),
        "profile_bottom": r4(layers[-1].bottom),
        "max_deviation": max((bm["deviation"] for bm in beams_out if bm["deviation"] is not None),
                             default=None),
    }

    return {
        "summary": summary,
        "profile": profile_view,
        "beams": beams_out,
        "meta": {
            "engine": "analytic-snell",
            "profile_rule": RULE_PROFILE,
            "ray_rule": RULE_RAY,
            "failure_rule": RULE_FAIL,
            "geometry_rule": RULE_GEOMETRY,
            "failure_reasons": FAIL_LABELS,
            "status_labels": STATUS_LABELS,
            "coordinate_note": "heading 自正北顺时针（度），正角指右舷；深度与横距单位与输入一致",
            "ordering": "波束按 0-based 原始输入顺序处理，缺省 id 依次为 B-001…；同一输入输出完全一致",
        },
    }
