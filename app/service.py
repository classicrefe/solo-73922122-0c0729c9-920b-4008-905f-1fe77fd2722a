"""重判编排层：把 API 请求、核心算法与持久层串起来。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .db import Database
from .fingerprint import rules_hash
from .models import PreflightRequest, RejudgeRequest
from .rejudge import GradeFeasibility, evaluate_sample
from .tickets import DEFAULT_TTL_SECONDS, expires_at, new_ticket, utc_now


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


class CandidateVersionNotFound(OriginalVersionNotFound):
    """候选（待启用）规则版号不存在。"""


class BaselineBatchNotFound(Exception):
    def __init__(self, batch_no: str):
        super().__init__(f"批号无成功入仓结果: {batch_no}")
        self.batch_no = batch_no


class BaselineArchiveUnavailable(Exception):
    """旧库迁移后的历史成功结果没有原档快照，无法据以预检。"""

    def __init__(self, batch_no: str):
        super().__init__(
            f"批号 {batch_no} 的历史成功结果缺少原档快照（旧库迁移前入仓），"
            "请用该批重新 POST /api/rejudge 一次成功后再预检"
        )
        self.batch_no = batch_no


class PreflightFailed(Exception):
    """预检发现原档无解或候选等级不唯一，不签发凭证。"""

    def __init__(self, samples: List[Dict[str, Any]]):
        super().__init__("预检未通过")
        self.samples = samples


class ActivationRejected(Exception):
    """受控启用复核失败：现行版保持不变。"""

    def __init__(self, status: str, detail: Dict[str, Any]):
        super().__init__(status)
        self.status = status
        self.detail = detail


def rejudge_batch(db: Database, req: RejudgeRequest) -> Dict[str, Any]:
    """执行一批重判并尝试入仓。

    流程：
    1. 取原规则与"重判开始时"的现行规则快照（含版本 id）；
    2. 逐条煤样在两个版本并集的压缩网格上求现行可行等级；
    3. 原等级无解的样号收集后整批退回（HTTP 422 语义，不写结果表）；
    4. 入仓写事务内再次核对启用版本 id，期间换版则整批退回（HTTP 409 语义），
       下游仍可查到该批号上一份成功结果及其所用版号。

    成功入仓时同时保存本次请求的**原档快照**（样号、区间、原等级）和原规则
    快照，供后续 ``preflight`` 从入仓结果原样回取。
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

    original_archive = {
        "batch_no": req.batch_no,
        "original_version": req.original_version,
        "original_version_id": original_id,
        "records": [
            {
                "sample_no": rec.sample_no,
                "moisture": {"lo": rec.moisture.lo, "hi": rec.moisture.hi},
                "ash": {"lo": rec.ash.lo, "hi": rec.ash.hi},
                "original_grade": rec.original_grade,
            }
            for rec in req.records
        ],
    }

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
        original_archive=original_archive,
        original_version_id=original_id,
        original_rules=original_rules,
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


def preflight_version(
    db: Database,
    req: PreflightRequest,
    candidate_code: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> Dict[str, Any]:
    """用一条已成功历史批次验证候选版，全部样号唯一才签发一次性凭证。

    基准数据全部取自**入仓时保存的原档快照与原规则**，而不是当前规则表，
    因此原规则后来被替换也仍按"当时"重算；候选版规则取当前已发布内容。
    任一确认项不满足即抛 :class:`PreflightFailed`，不写凭证。
    """
    candidate_id = db.get_version_id(candidate_code)
    if candidate_id is None:
        raise CandidateVersionNotFound(candidate_code)
    candidate_rules = db.get_rules(candidate_id)

    baseline = db.get_batch_preflight_view(req.batch_no)
    if baseline is None:
        raise BaselineBatchNotFound(req.batch_no)
    if not baseline["archive_available"]:
        raise BaselineArchiveUnavailable(req.batch_no)

    original_rules = [
        (
            row["grade"],
            row["moisture_lo"],
            row["moisture_hi"],
            row["ash_lo"],
            row["ash_hi"],
        )
        for row in baseline["original_rules"]
    ]
    original_rules_hash = rules_hash(original_rules)

    active = db.get_active()
    if active is None:
        raise NoActiveVersion("尚无启用的规则版本")
    current_id, current_code = active
    current_rules = db.get_rules(current_id)

    samples: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for rec in baseline["original_archive"]["records"]:
        options = evaluate_sample(
            moisture_interval=(rec["moisture"]["lo"], rec["moisture"]["hi"]),
            ash_interval=(rec["ash"]["lo"], rec["ash"]["hi"]),
            original_grade=rec.get("original_grade"),
            original_rules=original_rules,
            current_rules=candidate_rules,
        )
        if options is None:
            failures.append(
                {
                    "sample_no": rec["sample_no"],
                    "original_grade": rec.get("original_grade"),
                    "reason": "infeasible_original_grade",
                    "message": "旧档区间内没有测值能在候选版下按原版得出原等级",
                    "options": [],
                }
            )
            continue
        built = _build_sample(rec["sample_no"], rec.get("original_grade"), options)
        samples.append(built)
        if not built["unique"]:
            failures.append(
                {
                    "sample_no": rec["sample_no"],
                    "original_grade": rec.get("original_grade"),
                    "reason": "ambiguous_grade",
                    "message": "候选版下候选等级不唯一，需人工确认后再启用",
                    "options": built["options"],
                }
            )

    if failures:
        raise PreflightFailed(failures)

    now = utc_now()
    deadline = expires_at(ttl_seconds, now)
    token, token_hash = new_ticket()
    preflight_summary = {
        "batch_no": req.batch_no,
        "candidate_version": candidate_code,
        "current_version": current_code,
        "sample_count": len(samples),
        "samples": samples,
    }
    baseline_fingerprint = baseline["fingerprint"]
    db.create_activation_ticket(
        {
            "token_hash": token_hash,
            "version_id": candidate_id,
            "version_code": candidate_code,
            "baseline": {
                "batch_no": req.batch_no,
                "fingerprint": baseline_fingerprint,
                "revision": baseline["revision"],
                "judged_version_id": baseline["judged_version_id"],
            },
            "original_code": baseline["original_code"],
            "original_version_id": (
                baseline["original_archive"].get("original_version_id")
                or db.get_version_id(baseline["original_code"])
                or -1
            ),
            "original_rules_hash": original_rules_hash,
            "current_version_id": current_id,
            "current_code": current_code,
            "current_rules_hash": rules_hash(current_rules),
            "candidate_rules_hash": rules_hash(candidate_rules),
            "preflight": preflight_summary,
            "created_at": now,
            "expires_at": deadline,
        }
    )
    return {
        "batch_no": req.batch_no,
        "original_version": baseline["original_code"],
        "candidate_version": candidate_code,
        "current_version": current_code,
        "sample_count": len(samples),
        "samples": samples,
        "ticket": token,
        "expires_at": deadline,
        "preflighted_at": now,
    }


def activate_checked(db: Database, candidate_code: str, ticket: str) -> Dict[str, Any]:
    """凭凭证受控启用；任何一项变化都拒绝并保留原现行版。"""
    result = db.activate_checked(ticket, expected_code=candidate_code)
    status = result["status"]
    if status != "activated":
        raise ActivationRejected(status, result)
    return {
        "code": result["code"],
        "active": True,
        "previous_version": result["previous_code"],
        "batch_no": result["batch_no"],
        "consumed_at": utc_now(),
        "sample_count": result["preflight"]["sample_count"],
    }


def _build_sample(
    sample_no: str, original_grade: Optional[str], options: Sequence[GradeFeasibility]
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
