"""请求数据模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SurveyLine(BaseModel):
    """一条测线：按记录顺序排列，start/end 为平面坐标，swath 为全扫宽。"""

    id: str = Field(min_length=1, max_length=64, description="测线编号，需唯一")
    start: list[float] = Field(min_length=2, max_length=2, description="起点 [x, y]")
    end: list[float] = Field(min_length=2, max_length=2, description="终点 [x, y]")
    swath: float = Field(gt=0, description="扫宽（全宽），缓冲半径取 swath/2")


class Options(BaseModel):
    """可调阈值。"""

    min_issue_area: float = Field(
        default=1.0, ge=0,
        description="问题最小上报面积（坐标单位平方），用于过滤碎斑",
    )
    max_overlap_ratio: float = Field(
        default=0.2, ge=0, le=1,
        description="相邻测线重叠面积 / 较窄扫掠面积 超过该值判为过度重叠",
    )


class AnalysisRequest(BaseModel):
    boundary: dict[str, Any] = Field(description="任务边界，GeoJSON Polygon/MultiPolygon 几何")
    lines: list[SurveyLine] = Field(default_factory=list, max_length=5000)
    options: Options = Field(default_factory=Options)


class ContinuitySample(BaseModel):
    """一个采集样本：按采集顺序排列，timestamp 单位秒（epoch 或相对秒均可）。"""

    line_id: str = Field(min_length=1, max_length=64, description="所属测线编号，应对应计划测线 lines[].id")
    timestamp: float = Field(description="采样时间戳（秒），采集顺序下应非递减")
    ping_seq: int = Field(description="ping 序号，同一测线内应逐个 +1")
    x: float = Field(description="平面坐标 x")
    y: float = Field(description="平面坐标 y")


class ContinuityOptions(BaseModel):
    """采集连续性阈值。"""

    max_gap_s: float = Field(
        default=8.0, gt=0,
        description="最大采样间隔（秒），相邻样本时间差超过该值判为间隔超限",
    )
    max_speed: float = Field(
        default=15.0, gt=0,
        description="最大速度（坐标单位/秒），相邻样本 implied 速度超过该值判为速度超限",
    )
    max_offset: float = Field(
        default=20.0, ge=0,
        description="偏离容差（坐标单位），样本到计划测线（start-end 线段）的距离超过该值判为偏离超限",
    )


class ContinuityRequest(BaseModel):
    lines: list[SurveyLine] = Field(
        default_factory=list, max_length=5000,
        description="计划测线（与覆盖复核同一格式，页面沿用任务输入中的 lines）",
    )
    samples: list[ContinuitySample] = Field(
        default_factory=list, max_length=200000,
        description="采集样本，必须按采集顺序排列，后端不做任何重排",
    )
    options: ContinuityOptions = Field(default_factory=ContinuityOptions)


class ClosureSample(BaseModel):
    """一个水深采集样本：数组顺序即原始采集顺序（后端不做任何重排）。"""

    line_id: str = Field(min_length=1, max_length=64, description="所属测线编号，应对应计划测线 lines[].id")
    x: float = Field(description="平面坐标 x")
    y: float = Field(description="平面坐标 y")
    depth: float = Field(description="该样本处水深（与坐标同一长度单位即可，参与交点插值与闭合差）")


class ClosureOptions(BaseModel):
    """交叉点水深闭合参数。"""

    max_enclosure_distance: float = Field(
        default=15.0, gt=0,
        description="包围距离：包围交点的相邻样本对两端投影里程到交点里程的绝对差"
                    "（沿计划线里程）均不得超过该值；横向投影距离不参与判限",
    )
    huber_delta: float = Field(
        default=0.3, gt=0,
        description="Huber 阈值 δ：|r|≤δ 权 1，否则权 δ/|r|（IRLS 迭代加权最小二乘）",
    )
    residual_threshold: float = Field(
        default=0.5, ge=0,
        description="校正残差阈值：有冗余分量中 |校正残差| 超过该值判为闭合异常",
    )


class ClosureRequest(BaseModel):
    lines: list[SurveyLine] = Field(
        default_factory=list, max_length=5000,
        description="计划测线（与覆盖复核同一格式，页面沿用任务输入中的 lines）",
    )
    samples: list[ClosureSample] = Field(
        default_factory=list, max_length=200000,
        description="水深采集样本（含 depth），必须按采集顺序排列，后端不做任何重排",
    )
    options: ClosureOptions = Field(default_factory=ClosureOptions)
