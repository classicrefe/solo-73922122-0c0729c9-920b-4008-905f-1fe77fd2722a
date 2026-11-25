"""预检 + 受控启用：凭证一次性、三方守卫、旧库迁移与 /docs 演示场景。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.db import SCHEMA_VERSION, Database
from app.models import (
    Interval,
    PreflightRequest,
    RejudgeRequest,
    RuleIn,
    SampleRecord,
)
from app.service import (
    ActivationRejected,
    BaselineArchiveUnavailable,
    BaselineBatchNotFound,
    PreflightFailed,
    activate_checked,
    preflight_version,
    rejudge_batch,
)
from app.tickets import expires_at, utc_now

V1_RULES = [
    RuleIn(grade="优质煤", moisture=Interval(lo=0, hi=800), ash=Interval(lo=0, hi=2000)),
    RuleIn(grade="普通煤", moisture=Interval(lo=0, hi=1200), ash=Interval(lo=0, hi=3000)),
    RuleIn(grade="等外煤", moisture=Interval(lo=0, hi=10000), ash=Interval(lo=0, hi=10000)),
]
V2_RULES = [
    RuleIn(grade="优质煤", moisture=Interval(lo=0, hi=700), ash=Interval(lo=0, hi=1500)),
    RuleIn(grade="普通煤", moisture=Interval(lo=0, hi=1000), ash=Interval(lo=0, hi=2500)),
]

# 候选版 A：完整覆盖基准批的旧档区间 [400,600]×[1000,1400]，且只命中一条规则
CAND_UNIQUE = [
    RuleIn(grade="特优煤", moisture=Interval(lo=0, hi=600), ash=Interval(lo=0, hi=1400)),
    RuleIn(grade="未评级", moisture=Interval(lo=0, hi=10000), ash=Interval(lo=0, hi=10000)),
]

# 候选版 B：以水分 500 为界切成两段，基准批区间横跨两者 => 多等级
CAND_AMBIGUOUS = [
    RuleIn(grade="甲", moisture=Interval(lo=0, hi=500), ash=Interval(lo=0, hi=10000)),
    RuleIn(grade="乙", moisture=Interval(lo=501, hi=10000), ash=Interval(lo=0, hi=10000)),
]


def _seed_baseline(db: Database) -> None:
    """入仓一条成功批次：单样、原档区间 [400,600]×[1000,1400]、原等级优质煤。"""
    req = RejudgeRequest(
        batch_no="BASE-1",
        original_version="v1",
        records=[
            SampleRecord(
                sample_no="S-1",
                moisture=Interval(lo=400, hi=600),
                ash=Interval(lo=1000, hi=1400),
                original_grade="优质煤",
            )
        ],
    )
    rejudge_batch(db, req)


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    database.create_version("v1", [r.model_dump() for r in V1_RULES], note="old", activate=False)
    database.create_version("v2", [r.model_dump() for r in V2_RULES], note="new", activate=True)
    return database


def _add_candidate(db: Database, code: str, rules) -> None:
    db.create_version(code, [r.model_dump() for r in rules], note="candidate", activate=False)


# --------------------------------------------------------------------- 回取原档
def test_rejudge_persists_original_archive_for_preflight(db):
    _seed_baseline(db)
    view = db.get_batch_preflight_view("BASE-1")
    assert view["archive_available"] is True
    rec = view["original_archive"]["records"][0]
    assert rec["sample_no"] == "S-1"
    assert rec["moisture"] == {"lo": 400, "hi": 600}
    assert rec["ash"] == {"lo": 1000, "hi": 1400}
    assert rec["original_grade"] == "优质煤"
    assert view["original_rules"][0]["grade"] == "优质煤"
    assert view["original_code"] == "v1"


# ------------------------------------------------------------------- 预检成功路径
def test_preflight_unique_issues_ticket_and_activates(db):
    _seed_baseline(db)
    _add_candidate(db, "v3", CAND_UNIQUE)

    result = preflight_version(db, PreflightRequest(batch_no="BASE-1"), "v3")
    assert result["candidate_version"] == "v3"
    assert result["current_version"] == "v2"
    assert result["sample_count"] == 1
    assert result["samples"][0]["unique"] is True
    assert result["samples"][0]["final_grade"] == "特优煤"
    assert result["ticket"]

    # 凭证只存哈希，明文不落库
    ticket = db.get_activation_ticket(result["ticket"])
    assert ticket is not None and ticket["consumed_at"] is None
    with db.connect() as conn:
        raw = conn.execute("SELECT token_hash FROM activation_tickets").fetchone()[0]
    assert raw != result["ticket"]

    activated = activate_checked(db, "v3", result["ticket"])
    assert activated["code"] == "v3" and activated["previous_version"] == "v2"
    assert db.get_active()[1] == "v3"

    # 一次性：重复使用同一凭证被拒，且现行版仍为 v3
    with pytest.raises(ActivationRejected) as exc:
        activate_checked(db, "v3", result["ticket"])
    assert exc.value.status == "ticket_consumed"
    assert db.get_active()[1] == "v3"


# ------------------------------------------------------------------ 预检失败路径
def test_preflight_ambiguous_returns_options_and_no_ticket(db):
    _seed_baseline(db)
    # 原档 [400,600]×[1000,1400] 在候选版下横跨 甲/乙 两等级
    _add_candidate(db, "v3", CAND_AMBIGUOUS)

    with pytest.raises(PreflightFailed) as exc:
        preflight_version(db, PreflightRequest(batch_no="BASE-1"), "v3")
    failures = exc.value.samples
    assert len(failures) == 1
    failure = failures[0]
    assert failure["sample_no"] == "S-1" and failure["reason"] == "ambiguous_grade"
    grades = {opt["grade"] for opt in failure["options"]}
    assert grades == {"甲", "乙"}
    for opt in failure["options"]:
        # 见证值必须落在原档区间内
        assert 400 <= opt["witness_moisture"] <= 600
        assert 1000 <= opt["witness_ash"] <= 1400
        assert opt["feasible_pairs"] > 0

    # 失败不写任何凭证，现行版不动
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM activation_tickets").fetchone()[0] == 0
    assert db.get_active()[1] == "v2"


def test_preflight_infeasible_original_grade(db):
    _seed_baseline(db)
    # 候选版只覆盖水分 <= 300 的区域，原档区间 [400,600] 内全部未命中且无兜底；
    # 原版在 [400,600] 内定为优质煤 —— 候选版下仍可定为 None（未分级）故唯一。
    # 要构造"无解"需让原规则快照与记录不一致：直接篡改原档等级。
    with db.connect() as conn:
        row = conn.execute(
            "SELECT original_archive_json FROM batch_results WHERE batch_no='BASE-1'"
        ).fetchone()
        archive = json.loads(row[0])
        archive["records"][0]["original_grade"] = "不存在的等级"
        conn.execute(
            "UPDATE batch_results SET original_archive_json=? WHERE batch_no='BASE-1'",
            (json.dumps(archive, ensure_ascii=False),),
        )
        conn.commit()
    _add_candidate(db, "v3", CAND_UNIQUE)

    with pytest.raises(PreflightFailed) as exc:
        preflight_version(db, PreflightRequest(batch_no="BASE-1"), "v3")
    failure = exc.value.samples[0]
    assert failure["reason"] == "infeasible_original_grade"
    assert failure["options"] == []
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM activation_tickets").fetchone()[0] == 0


def test_preflight_missing_baseline_and_candidate(db):
    with pytest.raises(BaselineBatchNotFound):
        preflight_version(db, PreflightRequest(batch_no="NOPE"), "v2")
    _seed_baseline(db)
    from app.service import CandidateVersionNotFound

    with pytest.raises(CandidateVersionNotFound):
        preflight_version(db, PreflightRequest(batch_no="BASE-1"), "v-missing")


# ------------------------------------------------------------------- 受控启用守卫
def _ticket_for(db: Database, candidate="v3") -> str:
    _seed_baseline(db) if db.get_batch_preflight_view("BASE-1") is None else None
    _add_candidate(db, candidate, CAND_UNIQUE)
    return preflight_version(db, PreflightRequest(batch_no="BASE-1"), candidate)["ticket"]


def test_activate_checked_rejects_when_baseline_batch_changed(db):
    ticket = _ticket_for(db)
    # 基准批被重新成功入仓（内容变化 => 修订号/结果变化）
    rejudge_batch(
        db,
        RejudgeRequest(
            batch_no="BASE-1",
            original_version="v1",
            records=[
                SampleRecord(
                    sample_no="S-1",
                    moisture=Interval(lo=410, hi=610),
                    ash=Interval(lo=1000, hi=1400),
                    original_grade="优质煤",
                )
            ],
        ),
    )
    with pytest.raises(ActivationRejected) as exc:
        activate_checked(db, "v3", ticket)
    assert exc.value.status == "baseline_changed"
    assert db.get_active()[1] == "v2"

    # 被拒后凭证仍未被消耗，可再次使用？—— 基准批已变，仍应拒绝
    with pytest.raises(ActivationRejected):
        activate_checked(db, "v3", ticket)
    assert db.get_active()[1] == "v2"


def test_activate_checked_rejects_when_active_version_changed(db):
    ticket = _ticket_for(db)
    # 预检后、启用前管理员手工切到 v1
    assert db.activate("v1")
    with pytest.raises(ActivationRejected) as exc:
        activate_checked(db, "v3", ticket)
    assert exc.value.status == "current_version_changed"
    assert db.get_active()[1] == "v1"  # 原现行版（被手工改过的 v1）保持不变


def test_activate_checked_rejects_when_original_rules_changed(db):
    ticket = _ticket_for(db)
    original_id = db.get_version_id("v1")
    # 直接改写原规则（模拟归档被篡改/回填错误）
    with db.connect() as conn:
        conn.execute(
            "UPDATE rules SET moisture_hi = 900 WHERE version_id = ? AND seq = 0",
            (original_id,),
        )
        conn.commit()
    with pytest.raises(ActivationRejected) as exc:
        activate_checked(db, "v3", ticket)
    assert exc.value.status == "original_rules_changed"
    assert db.get_active()[1] == "v2"


def test_activate_checked_rejects_when_candidate_rules_changed(db):
    ticket = _ticket_for(db)
    candidate_id = db.get_version_id("v3")
    with db.connect() as conn:
        conn.execute(
            "UPDATE rules SET ash_hi = 999 WHERE version_id = ? AND seq = 0",
            (candidate_id,),
        )
        conn.commit()
    with pytest.raises(ActivationRejected) as exc:
        activate_checked(db, "v3", ticket)
    assert exc.value.status == "candidate_changed"
    assert db.get_active()[1] == "v2"


def test_activate_checked_rejects_expired_and_wrong_code_and_unknown(db):
    _seed_baseline(db)
    _add_candidate(db, "v3", CAND_UNIQUE)
    _add_candidate(db, "v4", CAND_UNIQUE)

    # 过期：预检时 TTL 为负
    expired = preflight_version(db, PreflightRequest(batch_no="BASE-1"), "v3", ttl_seconds=-1)
    with pytest.raises(ActivationRejected) as exc:
        activate_checked(db, "v3", expired["ticket"])
    assert exc.value.status == "ticket_expired"

    # 路径版号与凭证不匹配
    good = preflight_version(db, PreflightRequest(batch_no="BASE-1"), "v3")
    with pytest.raises(ActivationRejected) as exc:
        activate_checked(db, "v4", good["ticket"])
    assert exc.value.status == "ticket_version_mismatch"
    assert db.get_active()[1] == "v2"

    # 未知凭证
    with pytest.raises(ActivationRejected) as exc:
        activate_checked(db, "v3", "x" * 32)
    assert exc.value.status == "ticket_not_found"

    # 正确的仍可用
    assert activate_checked(db, "v3", good["ticket"])["code"] == "v3"


def test_activate_checked_ttl_boundary(db):
    _seed_baseline(db)
    _add_candidate(db, "v3", CAND_UNIQUE)
    result = preflight_version(db, PreflightRequest(batch_no="BASE-1"), "v3", ttl_seconds=60)
    # 恰好到期瞬间应视为过期（<=）
    assert db.activate_checked(
        result["ticket"], expected_code="v3", now=result["expires_at"]
    )["status"] == "ticket_expired"
    # 到期前一秒可以
    before = expires_at(-1, result["expires_at"])
    assert db.activate_checked(
        result["ticket"], expected_code="v3", now=before
    )["status"] == "activated"


# --------------------------------------------------------------- 旧库迁移后可预检
LEGACY_SCHEMA = """
CREATE TABLE rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE,
    note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, version_id INTEGER NOT NULL,
    seq INTEGER NOT NULL, grade TEXT NOT NULL,
    moisture_lo INTEGER NOT NULL, moisture_hi INTEGER NOT NULL,
    ash_lo INTEGER NOT NULL, ash_hi INTEGER NOT NULL
);
CREATE TABLE active_version (
    id INTEGER PRIMARY KEY CHECK (id = 1), version_id INTEGER NOT NULL
);
CREATE TABLE batch_results (
    batch_no TEXT PRIMARY KEY, original_code TEXT NOT NULL, judged_code TEXT NOT NULL,
    record_count INTEGER NOT NULL, result_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE batch_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_no TEXT NOT NULL, reason TEXT NOT NULL,
    detail_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def _make_legacy_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO rule_versions(id, code, note, created_at) VALUES (1, 'v1', 'old', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO rule_versions(id, code, note, created_at) VALUES (2, 'v2', 'cur', '2026-01-01T00:00:00+00:00')"
    )
    conn.executemany(
        "INSERT INTO rules(version_id, seq, grade, moisture_lo, moisture_hi, ash_lo, ash_hi) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 0, "优质煤", 0, 800, 0, 2000),
            (1, 1, "普通煤", 0, 1200, 0, 3000),
            (1, 2, "等外煤", 0, 10000, 0, 10000),
            (2, 0, "优质煤", 0, 700, 0, 1500),
            (2, 1, "普通煤", 0, 1000, 0, 2500),
        ],
    )
    conn.execute("INSERT INTO active_version(id, version_id) VALUES (1, 2)")
    result = {
        "batch_no": "LEGACY-1",
        "original_version": "v1",
        "judged_version": "v2",
        "record_count": 1,
        "samples": [
            {
                "sample_no": "S-1",
                "original_grade": "优质煤",
                "unique": True,
                "final_grade": "优质煤",
                "options": [
                    {
                        "grade": "优质煤",
                        "witness_moisture": 400,
                        "witness_ash": 1000,
                        "feasible_pairs": 1,
                    }
                ],
            }
        ],
    }
    conn.execute(
        "INSERT INTO batch_results(batch_no, original_code, judged_code, record_count, "
        "result_json, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("LEGACY-1", "v1", "v2", 1, json.dumps(result, ensure_ascii=False), "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()


def test_legacy_db_migrates_and_reads_rules_but_requires_rearchive(tmp_path):
    path = str(tmp_path / "legacy.db")
    _make_legacy_db(path)

    db = Database(path)  # 触发迁移
    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(batch_results)")}
        assert {"revision", "original_rules_json", "original_archive_json",
                "judged_version_id"} <= columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='activation_tickets'"
        ).fetchone()[0] == 1
        # 原有成功结果保持可查
        assert conn.execute(
            "SELECT COUNT(*) FROM batch_results WHERE batch_no='LEGACY-1'"
        ).fetchone()[0] == 1

    view = db.get_batch_preflight_view("LEGACY-1")
    # 原规则可按原版号回填
    assert view["original_rules"] is not None and len(view["original_rules"]) == 3
    # 但迁移无法凭空还原原档区间 => 必须明确拒绝预检
    assert view["archive_available"] is False

    db.create_version("v3", [r.model_dump() for r in CAND_UNIQUE], note=None, activate=False)
    with pytest.raises(BaselineArchiveUnavailable):
        preflight_version(db, PreflightRequest(batch_no="LEGACY-1"), "v3")

    # 重新成功入仓一次即补齐原档，此后可预检
    rejudge_batch(
        db,
        RejudgeRequest(
            batch_no="LEGACY-1",
            original_version="v1",
            records=[
                SampleRecord(
                    sample_no="S-1",
                    moisture=Interval(lo=400, hi=600),
                    ash=Interval(lo=1000, hi=1400),
                    original_grade="优质煤",
                )
            ],
        ),
    )
    assert db.get_batch_preflight_view("LEGACY-1")["archive_available"] is True
    ok = preflight_version(db, PreflightRequest(batch_no="LEGACY-1"), "v3")
    assert ok["samples"][0]["final_grade"] == "特优煤"


def test_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "legacy.db")
    _make_legacy_db(path)
    Database(path)
    Database(path)  # 再次打开不应重复加列或报错
    db = Database(path)
    assert db.get_batch_preflight_view("LEGACY-1")["batch_no"] == "LEGACY-1"
