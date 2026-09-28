"""FastAPI 入口：静态页面 + 复核 API。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .analysis import analyze
from .closure import check_closure
from .continuity import check_continuity
from .sample import CLOSURE_SAMPLE, CONTINUITY_SAMPLE, SAMPLE
from .schemas import AnalysisRequest, ClosureRequest, ContinuityRequest

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="水下声呐测线覆盖复核",
    version="1.2.0",
    description="粘贴任务边界与测线 JSON，复核覆盖缺口、越界扫掠与相邻测线过度重叠；"
                "并可对按采集顺序排列的样本做采集连续性检查（时间/序号/间隔/速度/偏离），"
                "以及交叉点水深闭合（交点插值 + Huber IRLS 测线偏置估计）。",
)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/sample")
def api_sample() -> dict:
    """返回示例任务数据（与页面“载入示例”按钮同源）。"""
    return SAMPLE


@app.post("/api/analyze")
def api_analyze(req: AnalysisRequest) -> dict:
    try:
        return analyze(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 几何库对异常输入的报错也转为 400
        raise HTTPException(status_code=400, detail=f"计算失败: {exc}") from exc


@app.get("/api/continuity/sample")
def api_continuity_sample() -> dict:
    """返回采集连续性示例数据（与页面连续性面板“载入示例”按钮同源）。"""
    return CONTINUITY_SAMPLE


@app.post("/api/continuity")
def api_continuity(req: ContinuityRequest) -> dict:
    try:
        return check_continuity(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 几何库对异常输入的报错也转为 400
        raise HTTPException(status_code=400, detail=f"计算失败: {exc}") from exc


@app.get("/api/closure/sample")
def api_closure_sample() -> dict:
    """返回交叉点水深闭合示例数据（task 可载入任务输入，samples/options 填入闭合面板）。"""
    return CLOSURE_SAMPLE


@app.post("/api/closure")
def api_closure(req: ClosureRequest) -> dict:
    try:
        return check_closure(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 几何/平差对异常输入的报错也转为 400
        raise HTTPException(status_code=400, detail=f"计算失败: {exc}") from exc


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
