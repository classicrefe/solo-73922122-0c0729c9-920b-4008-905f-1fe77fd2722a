"""服务编排与 SQLite 持久化测试：整批退回、版本漂移、最后成功结果语义。"""

from __future__ import annotations

import pytest

from app.db import Database
from app.models import (
    Interval,
    RejudgeRequest,
    RuleIn,
    SampleRecord,
    VersionCreate,
)
from app.service import (
    InfeasibleOriginalGrade,
    OriginalVersionNotFound,
    VersionDrift,
    rejudge_batch,
)

V1_RULES = [
    RuleIn(grade="优质煤", moisture=Interval(lo=0, hi=800), ash=Interval(lo=0, hi=2000)),
    RuleIn(grade="普通煤", moisture=Interval(lo=0, hi=1200), ash=Interval(lo=0, hi=3000)),
    RuleIn(grade="等外煤", moisture=Interval(lo=0, hi=10000), ash=Interval(lo=0, hi=10000)),
]
V2_RULES = [
    RuleIn(grade="优质煤", moisture=Interval(lo=0, hi=700), ash=Interval(lo=0, hi=1500)),
    RuleIn(grade="普通煤", moisture=Interval(lo=0, hi=1000), ash=Interval(lo=0, hi=2500)),
]
V3_RULES = [
    RuleIn(grade="精煤", moisture=Interval(lo=0, hi=10000), ash=Interval(lo=0, hi=10000)),
]


def _req(batch_no="B1", original="v1"):
    return RejudgeRequest(
        batch_no=batch_no,
        original_version=original,
        records=[
            SampleRecord(
                sample_no="S-OK",
                moisture=Interval(lo=400, hi=600),
                ash=Interval(lo=1000, hi=1400),
                original_grade="优质煤",
            ),
            SampleRecord(
                sample_no="S-MULTI",
                moisture=Interval(lo=900, hi=1150),
                ash=Interval(lo=2300, hi=2700),
                original_grade="普通煤",
            ),
            SampleRecord(
                sample_no="S-FULL",
                moisture=Interval(lo=0, hi=10000),
                ash=Interval(lo=0, hi=10000),
                original_grade="优质煤",
            ),
        ],
    )


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    database.create_version("v1", [r.model_dump() for r in V1_RULES], note="old", activate=False)
    database.create_version("v2", [r.model_dump() for r in V2_RULES], note="new", activate=True)
    return database


def test_unique_and_multi_grade_payload(db):
    payload = rejudge_batch(db, _req())
    assert payload["judged_version"] == "v2"
    by_no = {s["sample_no"]: s for s in payload["samples"]}

    ok = by_no["S-OK"]
    assert ok["unique"] is True and ok["final_grade"] == "优质煤"
    assert len(ok["options"]) == 1

    multi = by_no["S-MULTI"]
    assert multi["unique"] is False and multi["final_grade"] is None
    grades = {o["grade"] for o in multi["options"]}
    assert "普通煤" in grades and None in grades  # 现行版下部分点未分级
    for opt in multi["options"]:
        # 见证点必须落在旧档闭区间内
        assert 900 <= opt["witness_moisture"] <= 1150
        assert 2300 <= opt["witness_ash"] <= 2700
        assert opt["feasible_pairs"] > 0

    # S-FULL 原等级优质煤：可行点即 v1 优质煤矩形（m≤800,a≤2000），
    # 该矩形在 v2 下被切成 优质煤/普通煤 两档（v2 优质煤是其子矩形）
    full = by_no["S-FULL"]
    assert {o["grade"] for o in full["options"]} == {"优质煤", "普通煤"}
    assert sum(o["feasible_pairs"] for o in full["options"]) == 801 * 2001
    # 优质煤点数 = v2 优质煤矩形 701×1501
    assert next(o for o in full["options"] if o["grade"] == "优质煤")["feasible_pairs"] == 701 * 1501


def test_unique_ungraded_when_current_has_no_match(db):
    """原版兜底区域在现行版全部落空 => 唯一定级为未分级（grade=null）。"""
    req = RejudgeRequest(
        batch_no="B-UNG",
        original_version="v1",
        records=[
            SampleRecord(
                sample_no="S-ONLY-CATCHALL",
                moisture=Interval(lo=5000, hi=5010),
                ash=Interval(lo=5000, hi=5010),
                original_grade="等外煤",  # v1 兜底，v2 完全不覆盖该区域
            )
        ],
    )
    payload = rejudge_batch(db, req)
    sample = payload["samples"][0]
    assert sample["unique"] is True and sample["final_grade"] is None
    assert sample["options"][0]["grade"] is None
    assert sample["options"][0]["feasible_pairs"] == 121  # 11×11


def test_infeasible_original_grade_rejects_whole_batch(db):
    req = _req()
    req.records.append(
        SampleRecord(
            sample_no="S-BAD",
            moisture=Interval(lo=9000, hi=9500),
            ash=Interval(lo=9000, hi=9500),
            original_grade="优质煤",  # v1 下该区域只有等外煤，无解
        )
    )
    with pytest.raises(InfeasibleOriginalGrade) as exc:
        rejudge_batch(db, req)
    assert exc.value.sample_nos == ["S-BAD"]
    # 整批未入仓
    assert db.get_batch_result("B1") is None
    rejections = db.list_rejections("B1")
    assert len(rejections) == 1 and rejections[0]["reason"] == "infeasible_original_grade"


def test_original_version_missing(db):
    req = _req(original="vX")
    with pytest.raises(OriginalVersionNotFound):
        rejudge_batch(db, req)


def test_version_drift_keeps_last_success(db):
    # 第一次成功入仓 v2 结果
    first = rejudge_batch(db, _req("B2"))
    assert first["judged_version"] == "v2"

    # 发布并启用 v3（重判期间允许换版）
    db.create_version("v3", [r.model_dump() for r in V3_RULES], note="v3", activate=True)
    assert db.get_active()[1] == "v3"

    # 模拟一次"开始于 v2、入仓时已换版"的重判：直接写库必然被版本守卫拒绝
    active_id_v2 = db.get_version_id("v2")
    committed = db.save_batch_result(
        batch_no="B2",
        original_code="v1",
        judged_code="v2",
        record_count=3,
        result_payload={"stale": True},
        expected_active_version_id=active_id_v2,
    )
    assert committed is False

    # 服务层同样以当前启用版（v3）为快照，提交成功，不会产生漂移异常
    payload = rejudge_batch(db, _req("B2"))
    assert payload["judged_version"] == "v3"

    # 用 monkeypatch 锁定一个旧快照，验证漂移时服务层确实抛 VersionDrift
    original_get_active = db.get_active

    def stale_snapshot():
        return active_id_v2, "v2"

    db.get_active = stale_snapshot  # type: ignore[method-assign]
    try:
        with pytest.raises(VersionDrift):
            rejudge_batch(db, _req("B2"))
    finally:
        db.get_active = original_get_active  # type: ignore[method-assign]

    # 漂移退回不覆盖：下游仍查到此前 v3 的成功结果及其所用版号
    row = db.get_batch_result("B2")
    assert row["judged_version"] == "v3"
    assert db.list_rejections("B2")[0]["reason"] == "version_drift"

    # 在 v3 下重新提交成功 => 覆盖为最后一次成功结果
    ok = rejudge_batch(db, _req("B2"))
    assert ok["judged_version"] == "v3"
    row = db.get_batch_result("B2")
    assert row["judged_version"] == "v3" and len(row["result"]["samples"]) == 3


def test_duplicate_version_conflict(db):
    from app.db import VersionConflict

    with pytest.raises(VersionConflict):
        db.create_version("v1", [r.model_dump() for r in V3_RULES], note=None, activate=False)


def test_rules_capped_at_twenty(db):
    rules = [
        RuleIn(grade="G", moisture=Interval(lo=0, hi=1), ash=Interval(lo=0, hi=1))
        for _ in range(21)
    ]
    with pytest.raises(Exception):
        VersionCreate(code="v-bad", rules=rules)


def test_interval_validation(db):
    with pytest.raises(Exception):
        Interval(lo=10, hi=5)
    with pytest.raises(Exception):
        Interval(lo=-1, hi=5)
    with pytest.raises(Exception):
        Interval(lo=0, hi=10001)
