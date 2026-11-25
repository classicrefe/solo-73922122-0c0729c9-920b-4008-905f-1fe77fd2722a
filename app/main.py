"""FastAPI 入口：规则版本管理 + 旧煤样重判入仓 + 下游批号查询。

启动时若库内没有任何规则版本，会自动播种两个演示版本并启用 v2，
打开 /docs 即可直接试跑（请求体已带示例）。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .db import Database, VersionConflict
from .models import (
    ActivateCheckedRequest,
    ActivateCheckedResult,
    PreflightRequest,
    PreflightResult,
    RejudgeRequest,
    RejudgeResult,
    RuleOut,
    VersionCreate,
    VersionOut,
)
from .service import (
    ActivationRejected,
    BaselineArchiveUnavailable,
    BaselineBatchNotFound,
    CandidateVersionNotFound,
    InfeasibleOriginalGrade,
    NoActiveVersion,
    OriginalVersionNotFound,
    PreflightFailed,
    VersionDrift,
    activate_checked,
    preflight_version,
    rejudge_batch,
)

DB_PATH = os.environ.get("COAL_DB_PATH", os.path.join("data", "coal.db"))

# --------------------------------------------------------------------- 演示种子
SEED_VERSIONS = [
    {
        "code": "v1",
        "note": "旧版规则（含等外煤兜底，全部有等级）",
        "rules": [
            {"grade": "优质煤", "moisture": {"lo": 0, "hi": 800}, "ash": {"lo": 0, "hi": 2000}},
            {"grade": "普通煤", "moisture": {"lo": 0, "hi": 1200}, "ash": {"lo": 0, "hi": 3000}},
            {"grade": "等外煤", "moisture": {"lo": 0, "hi": 10000}, "ash": {"lo": 0, "hi": 10000}},
        ],
    },
    {
        "code": "v2",
        "note": "现行版规则（收紧阈值，去掉兜底，区间外为未分级）",
        "rules": [
            {"grade": "优质煤", "moisture": {"lo": 0, "hi": 700}, "ash": {"lo": 0, "hi": 1500}},
            {"grade": "普通煤", "moisture": {"lo": 0, "hi": 1000}, "ash": {"lo": 0, "hi": 2500}},
        ],
    },
]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _app.state.db = Database(DB_PATH)
    db = _app.state.db
    if not db.list_versions():
        for idx, ver in enumerate(SEED_VERSIONS):
            db.create_version(
                code=ver["code"], rules=ver["rules"], note=ver["note"], activate=(idx == 1)
            )
    yield


app = FastAPI(
    title="煤质数据仓重判入仓 API",
    description=(
        "按现行规则重判旧煤样：旧档按批提交（带原规则版号），"
        "坐标压缩算法只遍历规则边界形成的网格，不枚举测值域；"
        "重判期间可发布/启用新版本，入仓前发现换版则整批退回。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_db() -> Database:
    return app.state.db


# --------------------------------------------------------------------- 异常映射
@app.exception_handler(VersionConflict)
def _version_conflict_handler(_request: Request, exc: VersionConflict):
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


@app.exception_handler(InfeasibleOriginalGrade)
def _infeasible_handler(_request: Request, exc: InfeasibleOriginalGrade):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": {
                "reason": "infeasible_original_grade",
                "message": "以下样号在旧档区间内不存在按原版得出原等级的测值对，整批退回",
                "sample_nos": exc.sample_nos,
            }
        },
    )


@app.exception_handler(VersionDrift)
def _drift_handler(_request: Request, exc: VersionDrift):
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": {"reason": "version_drift", "message": str(exc)}},
    )


@app.exception_handler(OriginalVersionNotFound)
def _orig_not_found_handler(_request: Request, exc: OriginalVersionNotFound):
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


@app.exception_handler(CandidateVersionNotFound)
def _candidate_not_found_handler(_request: Request, exc: CandidateVersionNotFound):
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


@app.exception_handler(BaselineBatchNotFound)
def _baseline_not_found_handler(_request: Request, exc: BaselineBatchNotFound):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": {
                "reason": "baseline_batch_not_found",
                "message": str(exc),
                "batch_no": exc.batch_no,
            }
        },
    )


@app.exception_handler(BaselineArchiveUnavailable)
def _baseline_archive_handler(_request: Request, exc: BaselineArchiveUnavailable):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": {
                "reason": "baseline_archive_unavailable",
                "message": str(exc),
                "batch_no": exc.batch_no,
            }
        },
    )


@app.exception_handler(PreflightFailed)
def _preflight_failed_handler(_request: Request, exc: PreflightFailed):
    """预检未通过：返回样号、候选等级选项与见证值，绝不签发凭证。"""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": {
                "reason": "preflight_failed",
                "message": "原档无解或候选等级不唯一，未生成启用凭证",
                "samples": exc.samples,
            }
        },
    )


@app.exception_handler(ActivationRejected)
def _activation_rejected_handler(_request: Request, exc: ActivationRejected):
    status_code = (
        status.HTTP_404_NOT_FOUND
        if exc.status in {"ticket_not_found", "candidate_missing"}
        else status.HTTP_409_CONFLICT
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": {
                "reason": exc.status,
                "message": "受控启用被拒绝，原现行版保持不变",
                "state": exc.detail,
            }
        },
    )


@app.exception_handler(NoActiveVersion)
def _no_active_handler(_request: Request, exc: NoActiveVersion):
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


# --------------------------------------------------------------------- 基础接口
@app.get("/health", tags=["meta"])
def health():
    db = get_db()
    active = db.get_active()
    return {
        "status": "ok",
        "active_version": active[1] if active else None,
        "versions": len(db.list_versions()),
    }


# --------------------------------------------------------------------- 规则版本
class ActivateResponse(BaseModel):
    code: str
    active: bool


@app.post("/api/versions", response_model=VersionOut, status_code=status.HTTP_201_CREATED,
          tags=["rules"])
def create_version(body: VersionCreate, activate: bool = Query(False, description="发布后立即启用")):
    """发布新版本（每版最多 20 条规则，按 seq 顺序取首条命中）。不影响进行中的重判。"""
    db = get_db()
    version_id = db.create_version(
        code=body.code,
        rules=[r.model_dump() for r in body.rules],
        note=body.note,
        activate=activate,
    )
    return next(v for v in db.list_versions() if v["id"] == version_id)


@app.get("/api/versions", response_model=List[VersionOut], tags=["rules"])
def list_versions():
    return get_db().list_versions()


@app.get("/api/versions/{code}/rules", response_model=List[RuleOut], tags=["rules"])
def get_rules(code: str):
    rules = get_db().list_rules(code)
    if rules is None:
        raise HTTPException(status_code=404, detail=f"规则版号不存在: {code}")
    return rules


@app.post("/api/versions/{code}/activate", response_model=ActivateResponse, tags=["rules"])
def activate_version(code: str):
    """启用新版本。重判开始到入仓前调用本接口会使该批重判在入仓时退回。"""
    if not get_db().activate(code):
        raise HTTPException(status_code=404, detail=f"规则版号不存在: {code}")
    return ActivateResponse(code=code, active=True)


@app.post("/api/versions/{code}/preflight", response_model=PreflightResult, tags=["rules"])
def preflight(code: str, body: PreflightRequest):
    """用一条**已成功入仓**的历史批次验证待启用版本；通过才签发一次性凭证。

    * 基准数据取自该批入仓时保存的原档区间、原等级与**原规则**，按候选版重算；
    * 任一原档无解或候选等级不唯一 => 422，返回样号、等级选项与见证值，不签发凭证；
    * 全部唯一 => 返回各样的定级与 ``ticket``（仅此一次明文）。
    """
    return preflight_version(get_db(), body, code)


@app.post(
    "/api/versions/{code}/activate-checked",
    response_model=ActivateCheckedResult,
    tags=["rules"],
)
def activate_checked_version(code: str, body: ActivateCheckedRequest):
    """凭预检凭证受控启用；基准批 / 原规则 / 预检时现行版任一变化都拒绝。

    失败返回 409（凭证不存在为 404），**原现行版保持不变**；凭证一次性，成功后失效。
    """
    return activate_checked(get_db(), code, body.ticket)


@app.get("/api/active", tags=["rules"])
def get_active_version():
    active = get_db().get_active()
    if active is None:
        return {"version_id": None, "code": None}
    return {"version_id": active[0], "code": active[1]}


# --------------------------------------------------------------------- 重判入仓
@app.post("/api/rejudge", response_model=RejudgeResult, tags=["rejudge"])
def rejudge(body: RejudgeRequest):
    """按批重判旧煤样并把**最后一次成功结果**入仓。

    * 现行版下等级唯一：``unique=true``，直接给出 ``final_grade``；
    * 出现多个等级：``unique=false``，``options`` 列出各等级、一对可行测值（见证点）
      及该等级的可行测值对总数；
    * 原等级无解：422，报样号并退回整批，不覆盖历史成功结果；
    * 重判期间换版：409，整批退回，下游继续查到上一份成功结果及其所用版号。
    """
    return rejudge_batch(get_db(), body)


@app.get("/api/batches/{batch_no}", tags=["rejudge"])
def query_batch(batch_no: str):
    """下游按批号查询最后一次成功结果（含所用现行版号 judged_version）。"""
    row = get_db().get_batch_result(batch_no)
    if row is None:
        raise HTTPException(status_code=404, detail=f"批号无成功入仓结果: {batch_no}")
    return row


@app.get("/api/batches/{batch_no}/rejections", tags=["rejudge"])
def batch_rejections(batch_no: str):
    """查询该批号的退回记录（原等级无解 / 期间换版）。"""
    return get_db().list_rejections(batch_no)


@app.get("/api/batches/{batch_no}/original-archive", tags=["preflight"])
def batch_original_archive(batch_no: str):
    """回取预检所用原档：入仓时保存的样号、原档区间、原等级与原规则快照。

    旧库迁移前入仓的历史行 ``archive_available=false``：原规则已回填，但原档
    区间当时未保存，需重新成功重判该批一次后才可预检。
    """
    view = get_db().get_batch_preflight_view(batch_no)
    if view is None:
        raise HTTPException(status_code=404, detail=f"批号无成功入仓结果: {batch_no}")
    return {
        "batch_no": view["batch_no"],
        "original_version": view["original_code"],
        "judged_version": view["judged_code"],
        "record_count": view["record_count"],
        "updated_at": view["updated_at"],
        "archive_available": view["archive_available"],
        "original_archive": view["original_archive"],
        "original_rules": view["original_rules"],
    }


@app.get("/api/rejections", tags=["rejudge"])
def all_rejections(limit: int = Query(100, ge=1, le=500)):
    rows = get_db().list_rejections()
    return rows[:limit]
