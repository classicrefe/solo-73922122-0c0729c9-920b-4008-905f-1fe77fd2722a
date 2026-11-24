"""重判编排层：把 API 请求、核心算法与持久层串起来。"""

from __future__ import annotations

from typing import Any, Dict, List

from .db import Database
from .models import RejudgeRequest
from .rejudge import GradeFeasibility, evaluate_sample


class OriginalVersionNotFound(Exception):
    def __init__(self, code: str):
        super().__init__(f"原规则版号不存在: {code}")
        self.code = code


class NoActiveVersion(Exception):
    pass


class VersionDrift(Exception):
    """重判开始到入仓前启用版本被切换。"""


class InfeasibleOriginalGrade(Exception):
    """旧档区间内没有任何测值能按原版得出原等级。"""

    def __init__(self, sample_nos: List[str]):
        super().__init__("部分样号在原版规则下无解")
        self.sample_nos = sample_nos


def rejudge_batch(db: Database, req: RejudgeRequest) -> Dict[str, Any]:
    """执行一批重判并尝试入仓。

    流程：
    1. 取原规则与"重判开始时"的现行规则快照（含版本 id）；
    2. 逐条煤样在两个版本并集的压缩网格上求现行可行等级；
    3. 原等级无解的样号收集后整批退回（HTTP 422 语义，不写结果表）；
    4. 入仓写事务内再次核对启用版本 id，期间换版则整批退回（HTTP 409 语义），
       下游仍可查到该批号上一份成功结果及其所用版号。
    """
    original_id = db.get_version_id(req.original_version)
    if original_id is None:
        raise OriginalVersionNotFound(req.original_version)
    original_rules = db.get_rules(original_id)

    active = db.get_active()
    if active is None:
        raise NoActiveVersion("尚无启用的规则版本")
    active_id, active_code = active
    current_rules = db.get_rules(active_id)

    samples_payload: List[Dict[str, Any]] = []
    infeasible: List[str] = []

    for rec in req.records:
        options = evaluate_sample(
            moisture_interval=(rec.moisture.lo, rec.moisture.hi),
            ash_interval=(rec.ash.lo, rec.ash.hi),
            original_grade=rec.original_grade,
            original_rules=original_rules,
            current_rules=current_rules,
        )
        if options is None:
            infeasible.append(rec.sample_no)
            continue
        samples_payload.append(_build_sample(rec.sample_no, rec.original_grade, options))

    if infeasible:
        db.record_rejection(
            req.batch_no,
            "infeasible_original_grade",
            {"sample_nos": infeasible, "original_version": req.original_version},
        )
        raise InfeasibleOriginalGrade(infeasible)

    payload = {
        "batch_no": req.batch_no,
        "original_version": req.original_version,
        "judged_version": active_code,
        "record_count": len(req.records),
        "samples": samples_payload,
    }

    committed = db.save_batch_result(
        batch_no=req.batch_no,
        original_code=req.original_version,
        judged_code=active_code,
        record_count=len(req.records),
        result_payload=payload,
        expected_active_version_id=active_id,
    )
    if not committed:
        current = db.get_active()
        db.record_rejection(
            req.batch_no,
            "version_drift",
            {
                "snapshot_version": active_code,
                "current_version": current[1] if current else None,
            },
        )
        raise VersionDrift(
            f"重判期间启用版本已由 {active_code} 切换为 "
            f"{current[1] if current else '无'}"
        )
    return payload


def _build_sample(
    sample_no: str, original_grade: str | None, options: List[GradeFeasibility]
) -> Dict[str, Any]:
    unique = len(options) == 1
    return {
        "sample_no": sample_no,
        "original_grade": original_grade,
        "unique": unique,
        "final_grade": options[0].grade if unique else None,
        "options": [
            {
                "grade": opt.grade,
                "witness_moisture": opt.witness_moisture,
                "witness_ash": opt.witness_ash,
                "feasible_pairs": opt.feasible_pairs,
            }
            for opt in options
        ],
    }
