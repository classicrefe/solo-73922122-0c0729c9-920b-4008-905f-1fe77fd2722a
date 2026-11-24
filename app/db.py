"""SQLite 持久层。

表结构：
* ``rule_versions``  各版规则头（版号唯一）
* ``rules``          每版最多 20 条规则，seq 即判定顺序
* ``active_version`` 单行表，保存当前启用版本（重判期间可随时切换）
* ``batch_results``  每批最后一次成功结果（同批号重跑成功即覆盖）
* ``batch_rejections`` 退回记录（原等级无解 / 期间换版），便于审计，不影响下游查询
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .rejudge import Rule

SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE,
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id  INTEGER NOT NULL REFERENCES rule_versions(id),
    seq         INTEGER NOT NULL,
    grade       TEXT NOT NULL,
    moisture_lo INTEGER NOT NULL,
    moisture_hi INTEGER NOT NULL,
    ash_lo      INTEGER NOT NULL,
    ash_hi      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_version ON rules(version_id, seq);

CREATE TABLE IF NOT EXISTS active_version (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    version_id INTEGER NOT NULL REFERENCES rule_versions(id)
);

CREATE TABLE IF NOT EXISTS batch_results (
    batch_no       TEXT PRIMARY KEY,
    original_code  TEXT NOT NULL,
    judged_code    TEXT NOT NULL,
    record_count   INTEGER NOT NULL,
    result_json    TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_rejections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no       TEXT NOT NULL,
    reason         TEXT NOT NULL,
    detail_json    TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejections_batch ON batch_rejections(batch_no);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ versions
    def create_version(
        self, code: str, rules: Sequence[Dict[str, Any]], note: Optional[str], activate: bool
    ) -> int:
        """发布新版本；activate=True 时在同一事务内启用。返回版本 id。"""
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO rule_versions(code, note, created_at) VALUES (?, ?, ?)",
                    (code, note, _utc_now()),
                )
            except sqlite3.IntegrityError as exc:  # 版号重复
                raise VersionConflict(code) from exc
            version_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO rules(version_id, seq, grade, moisture_lo, moisture_hi, "
                "ash_lo, ash_hi) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        version_id,
                        seq,
                        r["grade"],
                        r["moisture"]["lo"],
                        r["moisture"]["hi"],
                        r["ash"]["lo"],
                        r["ash"]["hi"],
                    )
                    for seq, r in enumerate(rules)
                ],
            )
            if activate:
                conn.execute(
                    "INSERT INTO active_version(id, version_id) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET version_id = excluded.version_id",
                    (version_id,),
                )
            conn.commit()
        return version_id

    def get_version_id(self, code: str) -> Optional[int]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM rule_versions WHERE code = ?", (code,)
            ).fetchone()
            return row["id"] if row else None

    def get_rules(self, version_id: int) -> List[Rule]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT grade, moisture_lo, moisture_hi, ash_lo, ash_hi "
                "FROM rules WHERE version_id = ? ORDER BY seq",
                (version_id,),
            ).fetchall()
        return [
            (r["grade"], r["moisture_lo"], r["moisture_hi"], r["ash_lo"], r["ash_hi"])
            for r in rows
        ]

    def get_active(self) -> Optional[Tuple[int, str]]:
        """返回 (version_id, code)；尚无启用版本时返回 None。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT v.id AS id, v.code AS code FROM active_version a "
                "JOIN rule_versions v ON v.id = a.version_id WHERE a.id = 1"
            ).fetchone()
            return (row["id"], row["code"]) if row else None

    def activate(self, code: str) -> bool:
        """启用指定版号；版号不存在返回 False。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM rule_versions WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "INSERT INTO active_version(id, version_id) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET version_id = excluded.version_id",
                (row["id"],),
            )
            conn.commit()
        return True

    def list_versions(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT v.id, v.code, v.note, v.created_at, "
                "  (SELECT COUNT(*) FROM rules r WHERE r.version_id = v.id) AS rule_count, "
                "  (SELECT version_id FROM active_version WHERE id = 1) AS active_id "
                "FROM rule_versions v ORDER BY v.id"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "code": r["code"],
                "active": r["active_id"] == r["id"],
                "rule_count": r["rule_count"],
                "note": r["note"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def list_rules(self, code: str) -> Optional[List[Dict[str, Any]]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM rule_versions WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                return None
            rules = conn.execute(
                "SELECT seq, grade, moisture_lo, moisture_hi, ash_lo, ash_hi "
                "FROM rules WHERE version_id = ? ORDER BY seq",
                (row["id"],),
            ).fetchall()
        return [dict(r) for r in rules]

    # ------------------------------------------------------------------- batches
    def save_batch_result(
        self,
        batch_no: str,
        original_code: str,
        judged_code: str,
        record_count: int,
        result_payload: Dict[str, Any],
        expected_active_version_id: int,
    ) -> bool:
        """在同一写事务内复核启用版本未变，再覆盖入仓最后一次成功结果。

        返回 False 表示重判期间启用版本被切换（版本漂移），调用方按退回处理。
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT version_id FROM active_version WHERE id = 1"
            ).fetchone()
            if row is None or row["version_id"] != expected_active_version_id:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO batch_results(batch_no, original_code, judged_code, "
                "record_count, result_json, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(batch_no) DO UPDATE SET "
                "original_code = excluded.original_code, judged_code = excluded.judged_code, "
                "record_count = excluded.record_count, result_json = excluded.result_json, "
                "updated_at = excluded.updated_at",
                (
                    batch_no,
                    original_code,
                    judged_code,
                    record_count,
                    json.dumps(result_payload, ensure_ascii=False),
                    _utc_now(),
                ),
            )
            conn.commit()
        return True

    def get_batch_result(self, batch_no: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batch_results WHERE batch_no = ?", (batch_no,)
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["result_json"])
        return {
            "batch_no": row["batch_no"],
            "original_version": row["original_code"],
            "judged_version": row["judged_code"],
            "record_count": row["record_count"],
            "updated_at": row["updated_at"],
            "result": payload,
        }

    def record_rejection(
        self, batch_no: str, reason: str, detail: Dict[str, Any]
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO batch_rejections(batch_no, reason, detail_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (batch_no, reason, json.dumps(detail, ensure_ascii=False), _utc_now()),
            )
            conn.commit()

    def list_rejections(self, batch_no: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            if batch_no is None:
                rows = conn.execute(
                    "SELECT * FROM batch_rejections ORDER BY id DESC LIMIT 100"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM batch_rejections WHERE batch_no = ? ORDER BY id DESC",
                    (batch_no,),
                ).fetchall()
        return [
            {
                "id": r["id"],
                "batch_no": r["batch_no"],
                "reason": r["reason"],
                "detail": json.loads(r["detail_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


class VersionConflict(Exception):
    def __init__(self, code: str):
        super().__init__(f"规则版号已存在: {code}")
        self.code = code
