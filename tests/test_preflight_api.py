"""预检 / 受控启用的 HTTP 端到端测试（/docs 可直接复现的调用序列）。"""

from __future__ import annotations

import os

import pytest

os.environ["COAL_DB_PATH"] = os.path.join(os.path.dirname(__file__), "_api_test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

BASE_BATCH = {
    "batch_no": "BATCH-DEMO-001",
    "original_version": "v1",
    "records": [
        {
            "sample_no": "S-001",
            "moisture": {"lo": 400, "hi": 600},
            "ash": {"lo": 1000, "hi": 1400},
            "original_grade": "优质煤",
        }
    ],
}

# 唯一候选：完整覆盖基准档区间
V3_UNIQUE = {
    "code": "v3",
    "note": "候选版（唯一）",
    "rules": [
        {"grade": "特优煤", "moisture": {"lo": 0, "hi": 600}, "ash": {"lo": 0, "hi": 1400}},
        {"grade": "未评级", "moisture": {"lo": 0, "hi": 10000}, "ash": {"lo": 0, "hi": 10000}},
    ],
}

# 多等级候选：水分 500 把基准档区间切两半
V4_AMBIGUOUS = {
    "code": "v4",
    "note": "候选版（多等级）",
    "rules": [
        {"grade": "甲", "moisture": {"lo": 0, "hi": 500}, "ash": {"lo": 0, "hi": 10000}},
        {"grade": "乙", "moisture": {"lo": 501, "hi": 10000}, "ash": {"lo": 0, "hi": 10000}},
    ],
}

V5_UNIQUE = {
    "code": "v5",
    "note": "候选版（唯一，用于守卫演示）",
    "rules": V3_UNIQUE["rules"],
}


@pytest.fixture(scope="module")
def client():
    db_path = os.environ["COAL_DB_PATH"]
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except FileNotFoundError:
            pass
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except FileNotFoundError:
                pass


@pytest.fixture(scope="module", autouse=True)
def _baseline(client):
    r = client.post("/api/rejudge", json=BASE_BATCH)
    assert r.status_code == 200, r.text


def _create(client, payload):
    r = client.post("/api/versions", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_preflight_failure_then_success_then_activate(client):
    """/docs 演示主线：失败预检（多等级，无凭证）=> 成功预检 => 受控启用。"""
    _create(client, V4_AMBIGUOUS)

    # 1) 多等级：422，返回样号 / 等级选项 / 见证值，无 ticket
    r = client.post("/api/versions/v4/preflight", json={"batch_no": "BATCH-DEMO-001"})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "preflight_failed" and "ticket" not in detail
    assert len(detail["samples"]) == 1
    bad = detail["samples"][0]
    assert bad["sample_no"] == "S-001" and bad["reason"] == "ambiguous_grade"
    assert {o["grade"] for o in bad["options"]} == {"甲", "乙"}
    for opt in bad["options"]:
        assert 400 <= opt["witness_moisture"] <= 600
        assert 1000 <= opt["witness_ash"] <= 1400
        assert opt["feasible_pairs"] > 0

    # 2) 唯一：200 且返回一次性 ticket
    _create(client, V3_UNIQUE)
    r = client.post("/api/versions/v3/preflight", json={"batch_no": "BATCH-DEMO-001"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["candidate_version"] == "v3"
    assert body["current_version"] == "v2"
    assert body["samples"][0]["final_grade"] == "特优煤"
    ticket = body["ticket"]
    assert ticket and len(ticket) >= 16

    # 3) 受控启用
    r = client.post("/api/versions/v3/activate-checked", json={"ticket": ticket})
    assert r.status_code == 200, r.text
    activated = r.json()
    assert activated["active"] is True and activated["code"] == "v3"
    assert activated["previous_version"] == "v2"
    assert client.get("/api/active").json()["code"] == "v3"

    # 4) 重复使用同一凭证 => 409，现行版仍 v3
    r = client.post("/api/versions/v3/activate-checked", json={"ticket": ticket})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "ticket_consumed"
    assert client.get("/api/active").json()["code"] == "v3"


def test_preflight_requires_known_baseline_and_candidate(client):
    r = client.post("/api/versions/v1/preflight", json={"batch_no": "NO-SUCH-BATCH"})
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "baseline_batch_not_found"

    r = client.post("/api/versions/ghost/preflight", json={"batch_no": "BATCH-DEMO-001"})
    assert r.status_code == 404

    # 请求体字段名兼容 baseline_batch_no
    r = client.post(
        "/api/versions/v1/preflight", json={"baseline_batch_no": "NO-SUCH-BATCH"}
    )
    assert r.status_code == 422


def test_activate_checked_guards_baseline_change(client):
    _create(client, V5_UNIQUE)
    r = client.post("/api/versions/v5/preflight", json={"batch_no": "BATCH-DEMO-001"})
    assert r.status_code == 200, r.text
    ticket = r.json()["ticket"]

    # 基准批被重新成功入仓（原档区间变了）=> 409，现行版保持 v3
    changed = {
        "batch_no": "BATCH-DEMO-001",
        "original_version": "v1",
        "records": [
            {
                "sample_no": "S-001",
                "moisture": {"lo": 410, "hi": 610},
                "ash": {"lo": 1000, "hi": 1400},
                "original_grade": "优质煤",
            }
        ],
    }
    assert client.post("/api/rejudge", json=changed).status_code == 200

    r = client.post("/api/versions/v5/activate-checked", json={"ticket": ticket})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "baseline_changed"
    assert client.get("/api/active").json()["code"] == "v3"

    # 即使把原档恢复成预检时内容，重跑也推进了修订号：旧凭证仍严格失效
    assert client.post("/api/rejudge", json=BASE_BATCH).status_code == 200
    r = client.post("/api/versions/v5/activate-checked", json={"ticket": ticket})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "baseline_changed"

    # 重新预检签发新凭证后可正常启用
    r = client.post("/api/versions/v5/preflight", json={"batch_no": "BATCH-DEMO-001"})
    assert r.status_code == 200, r.text
    r = client.post("/api/versions/v5/activate-checked", json={"ticket": r.json()["ticket"]})
    assert r.status_code == 200, r.text
    assert client.get("/api/active").json()["code"] == "v5"


def test_activate_checked_rejects_unknown_or_mismatched_ticket(client):
    _create(client, V3_UNIQUE) if _code_missing(client, "v3") else None
    r = client.post("/api/versions/v3/preflight", json={"batch_no": "BATCH-DEMO-001"})
    assert r.status_code == 200, r.text
    ticket = r.json()["ticket"]

    # 路径版号不符 => 409，不切换
    before = client.get("/api/active").json()["code"]
    r = client.post("/api/versions/v5/activate-checked", json={"ticket": ticket})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "ticket_version_mismatch"
    assert client.get("/api/active").json()["code"] == before

    # 未知凭证 => 404
    r = client.post("/api/versions/v3/activate-checked", json={"ticket": "z" * 32})
    assert r.status_code == 404
    assert r.json()["detail"]["reason"] == "ticket_not_found"


def test_openapi_exposes_new_endpoints(client):
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert "/api/versions/{code}/preflight" in paths
    assert "/api/versions/{code}/activate-checked" in paths
    preflight = paths["/api/versions/{code}/preflight"]["post"]
    ref = preflight["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    schema_name = ref.rsplit("/", 1)[-1]
    assert "ticket" in spec["components"]["schemas"][schema_name]["properties"]
    # 请求体示例可用于 /docs 直接 Execute
    example = spec["components"]["schemas"]["PreflightRequest"]["examples"]
    assert example and "BATCH-DEMO-001" in str(example)


def _code_missing(client, code: str) -> bool:
    codes = {v["code"] for v in client.get("/api/versions").json()}
    return code not in codes
