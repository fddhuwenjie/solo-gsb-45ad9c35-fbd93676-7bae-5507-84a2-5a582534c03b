"""示例任务数据：L 形边界 + 6 条往复测线。

故意包含三类问题，便于演示：
- L2/L3 间距 45 < 扫宽 90，重叠率约 33% -> 过度重叠
- L3 与 L4 间距过大 -> 未覆盖缺口
- L5 右端越出边界、L6 大部分落在边界凹口之外 -> 边界外扫掠
"""

SAMPLE = {
    "boundary": {
        "type": "Polygon",
        "coordinates": [[
            [0, 0], [1000, 0], [1000, 500], [400, 500], [400, 600], [0, 600], [0, 0],
        ]],
    },
    "lines": [
        {"id": "L1", "start": [0, 45], "end": [1000, 45], "swath": 90},
        {"id": "L2", "start": [1000, 135], "end": [0, 135], "swath": 90},
        {"id": "L3", "start": [0, 195], "end": [1000, 195], "swath": 90},
        {"id": "L4", "start": [1000, 330], "end": [0, 330], "swath": 90},
        {"id": "L5", "start": [0, 420], "end": [1060, 420], "swath": 90},
        {"id": "L6", "start": [1000, 545], "end": [0, 545], "swath": 90},
    ],
    "options": {"min_issue_area": 1.0, "max_overlap_ratio": 0.2},
}


def _build_continuity_samples() -> list[dict]:
    """沿 L1/L2 计划线构造采集样本（正常步进 50 m / 5 s，速度 10）。

    故意包含六类异常，并演示事件合并与多原因共存：
    - 间隔超限（dt=15 > 8）、速度超限（30 > 15）
    - ping_seq 重复、非 +1 跳变
    - 偏离超限（75 > 20）与速度超限同时命中、相邻区间合并为一个事件
    - 时间倒退（95 -> 90）；换线后 ping_seq 重新计数属正常
    """
    samples = []

    def add(line_id: str, t: float, seq: int, x: float, y: float) -> None:
        samples.append(
            {"line_id": line_id, "timestamp": t, "ping_seq": seq, "x": x, "y": y}
        )

    # L1：[0,45] -> [1000,45]
    add("L1", 0, 1, 0, 45)
    add("L1", 5, 2, 50, 45)
    add("L1", 10, 3, 100, 45)
    add("L1", 15, 4, 150, 45)
    add("L1", 20, 5, 200, 45)
    add("L1", 35, 6, 250, 45)      # 间隔 15 s > 8 -> 采样间隔超限
    add("L1", 40, 7, 300, 45)
    add("L1", 45, 8, 350, 45)
    add("L1", 50, 9, 500, 45)      # 5 s 移动 150 m，速度 30 > 15 -> 速度超限
    add("L1", 55, 10, 550, 45)
    add("L1", 60, 10, 600, 45)     # ping_seq 10 重复
    add("L1", 65, 11, 650, 45)
    add("L1", 70, 15, 700, 45)     # ping_seq 11 -> 15 非 +1 跳变
    add("L1", 75, 16, 750, 45)
    add("L1", 80, 17, 800, 120)    # 偏离 75 > 20 且区间速度约 18 > 15（多原因）
    add("L1", 85, 18, 850, 45)     # 回到测线，区间仍速度+偏离超限（与上一区间合并）
    add("L1", 90, 19, 900, 45)
    add("L1", 95, 20, 950, 45)
    add("L1", 90, 21, 1000, 45)    # 时间倒退 95 -> 90
    add("L1", 95, 22, 1000, 45)
    # L2：[1000,135] -> [0,135]（换线后 ping_seq 重新计数属正常）
    add("L2", 100, 1, 1000, 135)
    add("L2", 105, 2, 950, 135)
    add("L2", 120, 3, 900, 135)    # 间隔 15 s > 8 -> 采样间隔超限
    add("L2", 125, 4, 850, 135)
    add("L2", 130, 5, 800, 135)
    return samples


CONTINUITY_SAMPLE = {
    "lines": SAMPLE["lines"],
    "samples": _build_continuity_samples(),
    "options": {"max_gap_s": 8.0, "max_speed": 15.0, "max_offset": 20.0},
}


# ---------------------------------------------------------------------------
# 交叉点水深闭合示例：矩形边界 + 3 横（H）× 3 竖（V）网格 + 1 条与 H2 共线重叠
# 的 H2D。样本按“先横后竖”的采集顺序排列，每条线在 0..1000 里程上每 100 m 一个
# 样本，横向偏移 ±3 m，水深 = 20 + 测线偏置 + 平缓水深场（线性，交点处双方插值
# 结果一致，闭合差只剩偏置差）。
#
# 故意覆盖三种跳过：
# - H2D 与 H2 共线重叠 -> overlapping；H2D 自身不放任何样本
# - H3 在 V3 交点（里程 870）两侧的 800/900 里程样本缺失：最近的包围对是
#   (700, 1000)，两端沿线距离 170/130 均超包围距离 100 -> no_bracketing_samples
# - 两个 LX 样本属于任务输入未定义的测线 -> unknown_line
#
# 水深沿计划线里程位置取值（横向偏移不引入水深场误差），干净交点的闭合差只剩
# 测线偏置差；另在 V2 里程 400/500 的样本上制造 +5.0 粗差，只污染 V2×H2 一个
# 交点，Huber IRLS（δ=0.3）会把该观测权重压到约 0.06，其校正残差远超 0.5 判为异常。
_CLOSURE_LINES = [
    {"id": "H1", "start": [0, 120], "end": [1000, 120], "swath": 200},
    {"id": "H2", "start": [0, 480], "end": [1000, 480], "swath": 200},
    {"id": "H2D", "start": [100, 480], "end": [900, 480], "swath": 100},  # 与 H2 共线重叠
    {"id": "H3", "start": [0, 880], "end": [1000, 880], "swath": 200},
    {"id": "V1", "start": [150, 0], "end": [150, 1000], "swath": 200},
    {"id": "V2", "start": [520, 0], "end": [520, 1000], "swath": 200},
    {"id": "V3", "start": [870, 0], "end": [870, 1000], "swath": 200},
]

_CLOSURE_BIAS = {"H1": 0.0, "H2": 0.4, "H3": -0.3, "V1": -0.2, "V2": 0.25, "V3": 0.15}
_CLOSURE_TASK = {
    "boundary": {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1000, 0], [1000, 1000], [0, 1000], [0, 0]]],
    },
    "lines": _CLOSURE_LINES,
    "options": {"min_issue_area": 1.0, "max_overlap_ratio": 0.2},
}


def _depth_field(x: float, y: float) -> float:
    """平缓的真实水深场（线性）：交点处两条线插值得到同值，闭合差仅反映测线偏置。"""
    return 20.0 - 0.008 * x + 0.004 * y


def _build_closure_samples() -> list[dict]:
    samples: list[dict] = []

    def add(line_id: str, x: float, y: float, depth: float) -> None:
        samples.append({"line_id": line_id, "x": x, "y": y, "depth": round(depth, 4)})

    def add_line(line_id: str, horizontal: bool, fixed: float) -> None:
        for k in range(11):  # 里程 0,100,…,1000
            s = k * 100
            # H3 在 V3 交点（里程 870）两侧的 800/900 里程样本故意缺失：
            # 最近的包围对 (700, 1000) 两端沿线距离 170/130 均超包围距离
            if line_id == "H3" and k in (8, 9):
                continue
            off = 3.0 if k % 2 == 0 else -3.0
            blip = 0.0
            # V2 里程 400/500 粗差 +5.0，仅污染 V2×H2（里程 480）一个交点
            if line_id == "V2" and k in (4, 5):
                blip = 5.0
            if horizontal:
                x, y = float(s), fixed + off
            else:
                x, y = fixed + off, float(s)
            # 水深沿计划线里程位置（fixed 处）取值：横向偏移不引入水深场误差
            fx, fy = (float(s), fixed) if horizontal else (fixed, float(s))
            add(line_id, x, y, _depth_field(fx, fy) + _CLOSURE_BIAS[line_id] + blip)

    add_line("H1", True, 120)
    add_line("H2", True, 480)
    # H2D 不放样本（与 H2 共线重叠，按 overlapping 跳过）
    add_line("H3", True, 880)
    add_line("V1", False, 150)
    add_line("V2", False, 520)
    add_line("V3", False, 870)
    # 未知测线样本：任务输入中没有 LX
    add("LX", 10, 10, _depth_field(10, 10) + 0.1)
    add("LX", 20, 20, _depth_field(20, 20) + 0.1)
    return samples


CLOSURE_SAMPLE = {
    "task": _CLOSURE_TASK,
    "lines": _CLOSURE_LINES,
    "samples": _build_closure_samples(),
    "options": {
        "max_enclosure_distance": 100.0,
        "huber_delta": 0.3,
        "residual_threshold": 0.5,
    },
}


# ---------------------------------------------------------------------------
# 声速射线改正式例：5 层声速剖面（米 / m·s⁻¹），层 300→320 之间故意留 20 m 断档；
# 120→300 深层声速回升到 1520（高于入射层 1500），为大角度波束制造折射极限。
# 波束换能器位于 (500,500)、深 5 m；覆盖成功与三类失败：
# - B-001 垂直、B-002 右舷 30°、B-003 左舷 45°、B-004 右舷 60°：正常落点
# - B-005 垂直且单程时间恰好走完入射层（落点在层界面 15 m）
# - B-006 右舷 82°：在 120 m 界面处 |p·c|>1 -> refraction_limit（保留前 3 层段）
# - B-007 垂直长传播时间：在 300 m 处遇到 300→320 断档 -> profile_gap
# - B-008 垂直更长传播时间：穿过 500 m 剖面底界 -> beyond_profile_bottom
# - B-009 换能器深 520 m，位于剖面之外 -> profile_gap（无对照直线）
def _build_ray_sample() -> dict:
    profile = [
        {"top_depth": 0, "bottom_depth": 15, "sound_speed": 1500.0},    # 表层混合层
        {"top_depth": 15, "bottom_depth": 50, "sound_speed": 1480.0},   # 温跃层
        {"top_depth": 50, "bottom_depth": 120, "sound_speed": 1470.0},
        {"top_depth": 120, "bottom_depth": 300, "sound_speed": 1520.0},  # 深层回升（制造折射极限）
        # 300 → 320 故意断档（声速剖面缺失，射线到此失败，禁止外推）
        {"top_depth": 320, "bottom_depth": 500, "sound_speed": 1525.0},
    ]

    def beam(bid, x, y, heading, td, twtt, ang):
        return {
            "id": bid, "x": x, "y": y, "heading": heading,
            "transducer_depth": td,
            "two_way_travel_time": round(twtt, 9),
            "angle_deg": ang,
        }

    beams = [
        beam("B-001", 500, 500, 90, 5, 0.10, 0.0),        # 垂直：落在 1470 层
        beam("B-002", 500, 500, 90, 5, 0.16, 30.0),       # 右舷 30°（航向正东 -> 落点在南侧）
        beam("B-003", 500, 500, 0, 5, 0.12, -45.0),       # 左舷 45°（航向正北 -> 落点在西侧）
        beam("B-004", 500, 500, 90, 5, 0.06, 60.0),       # 右舷 60°：落在温跃层以下
        beam("B-005", 500, 500, 90, 5, 2.0 * 10.0 / 1500.0, 0.0),  # 恰好走到层界面 15 m
        beam("B-006", 500, 500, 90, 5, 1.0, 82.0),        # 82° > 临界角 75.3°：折射极限
        beam("B-007", 500, 500, 90, 5, 0.42, 0.0),        # 单程 0.21 s 止于 300 m 断档
        # 换能器位于断档下方 340 m（断档在其上方不经过），垂直下行超出 500 m 底界
        beam("B-008", 620, 500, 90, 340, 0.25, 0.0),       # 160 m 至底界后仍有剩余 -> 超界
        beam("B-009", 500, 500, 90, 520, 0.05, 0.0),      # 换能器在剖面之外
    ]
    return {"profile": profile, "beams": beams}


RAY_SAMPLE = _build_ray_sample()
