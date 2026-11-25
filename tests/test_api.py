"""FastAPI 端到端测试：/docs 所需接口、HTTP 状态码、下游查询语义。"""

from __future__ import annotations

import os

import pytest

# 必须在导入 app.main 之前指定独立测试库
os.environ["COAL_DB_PATH"] = os.path.join(os.path.dirname(__file__), "_api_test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # 每个测试模块使用全新临时库：先清掉播种文件，结束后再清理
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
        # 关闭残留连接后清理临时库文件（WAL 模式下还有 -wal/-shm）
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except FileNotFoundError:
                pass


def test_health_and_seed(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["active_version"] == "v2"
    assert body["versions"] == 2

    r = client.get("/api/versions")
    codes = {v["code"]: v for v in r.json()}
    assert set(codes) == {"v1", "v2"}
    assert codes["v2"]["active"] is True
    assert codes["v1"]["rule_count"] == 3

    r = client.get("/api/versions/v2/rules")
    assert r.status_code == 200 and len(r.json()) == 2


def test_openapi_docs_available(client):
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_rejudge_unique_multi_and_persistence(client):
    payload = {
        "batch_no": "B-API-1",
        "original_version": "v1",
        "records": [
            {
                "sample_no": "S-001",
                "moisture": {"lo": 400, "hi": 600},
                "ash": {"lo": 1000, "hi": 1400},
                "original_grade": "优质煤",
            },
            {
                "sample_no": "S-002",
                "moisture": {"lo": 900, "hi": 1150},
                "ash": {"lo": 2300, "hi": 2700},
                "original_grade": "普通煤",
            },
        ],
    }
    r = client.post("/api/rejudge", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["judged_version"] == "v2"
    s1, s2 = body["samples"]
    assert s1["unique"] is True and s1["final_grade"] == "优质煤"
    assert s2["unique"] is False and s2["final_grade"] is None
    assert len(s2["options"]) >= 2

    # 下游按批号查询
    r = client.get("/api/batches/B-API-1")
    assert r.status_code == 200
    assert r.json()["judged_version"] == "v2"

    # 回取预检所用原档
    r = client.get("/api/batches/B-API-1/original-archive")
    assert r.status_code == 200
    archive = r.json()
    assert archive["archive_available"] is True
    assert archive["original_archive"]["records"][1]["moisture"] == {"lo": 900, "hi": 1150}
    assert [x["grade"] for x in archive["original_rules"]][0] == "优质煤"


def test_rejudge_infeasible_returns_422_and_no_result(client):
    payload = {
        "batch_no": "B-API-2",
        "original_version": "v1",
        "records": [
            {
                "sample_no": "S-BAD",
                "moisture": {"lo": 9000, "hi": 9500},
                "ash": {"lo": 9000, "hi": 9500},
                "original_grade": "优质煤",
            }
        ],
    }
    r = client.post("/api/rejudge", json=payload)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["reason"] == "infeasible_original_grade"
    assert detail["sample_nos"] == ["S-BAD" ]
    assert client.get("/api/batches/B-API-2").status_code == 404
    rejs = client.get("/api/batches/B-API-2/rejections").json()
    assert rejs[0]["reason"] == "infeasible_original_grade"


def test_rejudge_drift_409_then_last_success_remains(client):
    # 先成功入仓
    good = {
        "batch_no": "B-API-3",
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
    assert client.post("/api/rejudge", json=good).status_code == 200

    # 发布并启用 v3
    new_ver = {
        "code": "v3",
        "note": "重判期间发布",
        "rules": [
            {"grade": "精煤", "moisture": {"lo": 0, "hi": 10000}, "ash": {"lo": 0, "hi": 10000}}
        ],
    }
    r = client.post("/api/versions?activate=true", json=new_ver)
    assert r.status_code == 201 and r.json()["active"] is True
    assert client.get("/api/active").json()["code"] == "v3"

    # 旧请求重提 => 成功（按新快照 v3 计算），结果覆盖为 v3
    r = client.post("/api/rejudge", json=good)
    assert r.status_code == 200 and r.json()["judged_version"] == "v3"

    # 切回 v2，再提交 => judged_version 跟随启用版
    assert client.post("/api/versions/v2/activate").status_code == 200
    r = client.post("/api/rejudge", json=good)
    assert r.status_code == 200 and r.json()["judged_version"] == "v2"
    assert client.get("/api/batches/B-API-3").json()["judged_version"] == "v2"


def test_validation_errors(client):
    # 区间反向
    bad_interval = {
        "batch_no": "B-BAD",
        "original_version": "v1",
        "records": [
            {
                "sample_no": "S",
                "moisture": {"lo": 9, "hi": 1},
                "ash": {"lo": 0, "hi": 1},
                "original_grade": "优质煤",
            }
        ],
    }
    assert client.post("/api/rejudge", json=bad_interval).status_code == 422

    # 超过 100 条
    too_many = {
        "batch_no": "B-BIG",
        "original_version": "v1",
        "records": [
            {
                "sample_no": f"S{i}",
                "moisture": {"lo": 0, "hi": 1},
                "ash": {"lo": 0, "hi": 1},
                "original_grade": "等外煤",
            }
            for i in range(101)
        ],
    }
    assert client.post("/api/rejudge", json=too_many).status_code == 422

    # 原规则版号不存在
    ghost = {
        "batch_no": "B-GHOST",
        "original_version": "nope",
        "records": [
            {
                "sample_no": "S",
                "moisture": {"lo": 0, "hi": 1},
                "ash": {"lo": 0, "hi": 1},
                "original_grade": "等外煤",
            }
        ],
    }
    assert client.post("/api/rejudge", json=ghost).status_code == 404


def test_duplicate_version_and_unknown_activate(client):
    payload = {
        "code": "v1",
        "rules": [
            {"grade": "X", "moisture": {"lo": 0, "hi": 1}, "ash": {"lo": 0, "hi": 1}}
        ],
    }
    assert client.post("/api/versions", json=payload).status_code == 409
    assert client.post("/api/versions/ghost/activate").status_code == 404
