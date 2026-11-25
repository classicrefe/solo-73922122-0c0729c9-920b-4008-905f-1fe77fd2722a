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
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .fingerprint import (
    batch_row_fingerprint,
    rules_from_payload,
    rules_hash,
    rules_payload,
)
from .rejudge import Rule
from .tickets import hash_token, is_expired, utc_now

# 当前库结构版本；旧库在启动时原位迁移（只加列/建表，不删数据）
SCHEMA_VERSION = 2

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
    updated_at     TEXT NOT NULL,
    revision       INTEGER NOT NULL DEFAULT 1,
    original_archive_json TEXT,
    original_rules_json   TEXT,
    judged_version_id     INTEGER
);

CREATE TABLE IF NOT EXISTS batch_rejections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no       TEXT NOT NULL,
    reason         TEXT NOT NULL,
    detail_json    TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejections_batch ON batch_rejections(batch_no);

-- 预检通过后签发的一次性启用凭证。只存指纹/摘要，不存明文 token；
-- 启用时在写事务内逐项复核基准批、原规则、现行版与候选规则。
CREATE TABLE IF NOT EXISTS activation_tickets (
    token_hash           TEXT PRIMARY KEY,
    version_id           INTEGER NOT NULL REFERENCES rule_versions(id),
    version_code         TEXT NOT NULL,
    baseline_json        TEXT NOT NULL,
    original_code        TEXT NOT NULL,
    original_version_id  INTEGER NOT NULL,
    original_rules_hash  TEXT NOT NULL,
    current_version_id   INTEGER NOT NULL,
    current_code         TEXT NOT NULL,
    current_rules_hash   TEXT NOT NULL,
    candidate_rules_hash TEXT NOT NULL,
    preflight_json       TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    expires_at           TEXT NOT NULL,
    consumed_at          TEXT,
    consumed_status      TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_version ON activation_tickets(version_id);
"""

# 旧库迁移：v1（无 revision / 原档快照 / 凭证表）=> v2。只增不删。
MIGRATIONS = {
    1: [
        "ALTER TABLE batch_results ADD COLUMN revision INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE batch_results ADD COLUMN original_archive_json TEXT",
        "ALTER TABLE batch_results ADD COLUMN original_rules_json TEXT",
        "ALTER TABLE batch_results ADD COLUMN judged_version_id INTEGER",
    ],
}


def _utc_now() -> str:
    return utc_now()


def _archive_complete(
    archive: Optional[Dict[str, Any]], original_rules: Optional[Sequence[Any]]
) -> bool:
    """原档快照是否足以重算：旧库迁移占位行（区间缺失）不算完整。"""
    if not archive or not original_rules:
        return False
    if archive.get("archive_available") is False:
        return False
    records = archive.get("records")
    if not records:
        return False
    for rec in records:
        if not isinstance(rec.get("moisture"), dict) or not isinstance(rec.get("ash"), dict):
            return False
    return True


class Database:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with self.connect() as conn:
            # 先按最新结构补齐缺失的表（CREATE IF NOT EXISTS 幂等），
            # 再在单个写事务里给旧表补列并推进 user_version；任何一步失败都回滚。
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            self._migrate(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """把旧库原位升级到 ``SCHEMA_VERSION``。

        仅对已存在的库补列/补表；空库（``user_version == 0`` 且无业务表）
        直接按最新 SCHEMA 建表。迁移完成前不提交，失败不会留下半升级库。

        除补列外，还对旧行的 ``original_rules_json`` 做可恢复回填：按行内
        ``original_code`` 找到原规则版本并写入当时的规则快照。旧行**从未保存**
        原档区间，无法凭空还原，因此 ``original_archive_json`` 写入带
        ``archive_available=false`` 的占位结构；重新提交该批成功后即补齐。
        """
        has_batch = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'batch_results'"
        ).fetchone()
        if not has_batch:
            return
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            # 基线归档库没有 user_version，但表已存在 => 视为 v1
            version = 1
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(batch_results)").fetchall()
        }
        for from_version in range(version, SCHEMA_VERSION):
            for statement in MIGRATIONS.get(from_version, []):
                column = statement.split(" ADD COLUMN ", 1)[-1].split(" ", 1)[0]
                if column in existing:
                    continue
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
                existing.add(column)
        if version < SCHEMA_VERSION:
            Database._backfill_legacy_rows(conn)

    @staticmethod
    def _backfill_legacy_rows(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT batch_no, original_code, result_json FROM batch_results "
            "WHERE original_rules_json IS NULL OR original_archive_json IS NULL"
        ).fetchall()
        for row in rows:
            rules_row = conn.execute(
                "SELECT r.grade, r.moisture_lo, r.moisture_hi, r.ash_lo, r.ash_hi "
                "FROM rules r JOIN rule_versions v ON v.id = r.version_id "
                "WHERE v.code = ? ORDER BY r.seq",
                (row["original_code"],),
            ).fetchall()
            original_rules = [
                (r["grade"], r["moisture_lo"], r["moisture_hi"], r["ash_lo"], r["ash_hi"])
                for r in rules_row
            ]
            try:
                result = json.loads(row["result_json"])
                samples = result.get("samples", [])
            except (TypeError, ValueError):
                samples = []
            archive = {
                "batch_no": row["batch_no"],
                "original_version": row["original_code"],
                "archive_available": False,
                "legacy": True,
                "records": [
                    {
                        "sample_no": s.get("sample_no"),
                        "moisture": None,
                        "ash": None,
                        "original_grade": s.get("original_grade"),
                    }
                    for s in samples
                ],
            }
            conn.execute(
                "UPDATE batch_results SET original_rules_json = ?, "
                "original_archive_json = ? WHERE batch_no = ?",
                (
                    json.dumps(rules_payload(original_rules), ensure_ascii=False)
                    if original_rules
                    else None,
                    json.dumps(archive, ensure_ascii=False),
                    row["batch_no"],
                ),
            )

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
        original_archive: Optional[Dict[str, Any]] = None,
        original_version_id: Optional[int] = None,
        original_rules: Optional[Sequence[Rule]] = None,
    ) -> bool:
        """在同一写事务内复核启用版本未变，再覆盖入仓最后一次成功结果。

        除版本守卫外，同时把**重判所用原档**（本次入参记录）与**原规则快照**
        随结果一起落库：预检必须能从入仓结果回取当时所用的原档区间、
        原等级与原规则，即使规则版本后来被改名/删除也不受影响。

        返回 False 表示重判期间启用版本被切换（版本漂移），调用方按退回处理。
        """
        if original_rules is None and original_version_id is not None:
            # 调用方未显式传规则 => 在写事务外读取快照并由调用方负责一致性；
            # 这里只做便捷兜底，正式路径由 service 层显式传入。
            original_rules = self.get_rules(original_version_id)
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
                "record_count, result_json, updated_at, revision, original_archive_json, "
                "original_rules_json, judged_version_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(batch_no) DO UPDATE SET "
                "original_code = excluded.original_code, judged_code = excluded.judged_code, "
                "record_count = excluded.record_count, result_json = excluded.result_json, "
                "updated_at = excluded.updated_at, "
                "revision = batch_results.revision + 1, "
                "original_archive_json = excluded.original_archive_json, "
                "original_rules_json = excluded.original_rules_json, "
                "judged_version_id = excluded.judged_version_id",
                (
                    batch_no,
                    original_code,
                    judged_code,
                    record_count,
                    json.dumps(result_payload, ensure_ascii=False),
                    _utc_now(),
                    json.dumps(original_archive, ensure_ascii=False)
                    if original_archive is not None
                    else None,
                    json.dumps(rules_payload(original_rules), ensure_ascii=False)
                    if original_rules is not None
                    else None,
                    expected_active_version_id,
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
        original_archive = (
            json.loads(row["original_archive_json"])
            if row["original_archive_json"] is not None
            else None
        )
        return {
            "batch_no": row["batch_no"],
            "original_version": row["original_code"],
            "judged_version": row["judged_code"],
            "record_count": row["record_count"],
            "updated_at": row["updated_at"],
            "result": payload,
            "original_archive": original_archive,
        }

    def get_batch_preflight_view(self, batch_no: str) -> Optional[Dict[str, Any]]:
        """回取预检所需原档：成功结果的入参记录快照 + 当时所用原规则。

        旧库迁移后历史行没有原档快照，返回 ``original_archive: None`` 并给出
        ``archive_available: False``，由服务层明确拒绝预检而非悄悄用现行规则顶替。
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batch_results WHERE batch_no = ?", (batch_no,)
            ).fetchone()
            if row is None:
                return None
            original_rules = (
                json.loads(row["original_rules_json"])
                if row["original_rules_json"] is not None
                else None
            )
            archive = (
                json.loads(row["original_archive_json"])
                if row["original_archive_json"] is not None
                else None
            )
            judged_version_id = row["judged_version_id"]
            if judged_version_id is None:
                found = conn.execute(
                    "SELECT id FROM rule_versions WHERE code = ?", (row["judged_code"],)
                ).fetchone()
                judged_version_id = found["id"] if found else None
            fingerprint = batch_row_fingerprint(dict(row))
        return {
            "batch_no": row["batch_no"],
            "original_code": row["original_code"],
            "judged_code": row["judged_code"],
            "judged_version_id": judged_version_id,
            "record_count": row["record_count"],
            "updated_at": row["updated_at"],
            "revision": row["revision"],
            "result": json.loads(row["result_json"]),
            "original_archive": archive,
            "original_rules": original_rules,
            "archive_available": _archive_complete(archive, original_rules),
            "fingerprint": fingerprint,
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

    # ------------------------------------------------------------------ tickets
    def create_activation_ticket(self, record: Mapping[str, Any]) -> None:
        """保存预检签发的一次性凭证。``token_hash`` 为明文凭证的 sha256。"""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO activation_tickets(token_hash, version_id, version_code, "
                "baseline_json, original_code, original_version_id, original_rules_hash, "
                "current_version_id, current_code, current_rules_hash, "
                "candidate_rules_hash, preflight_json, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["token_hash"],
                    record["version_id"],
                    record["version_code"],
                    json.dumps(record["baseline"], ensure_ascii=False, sort_keys=True),
                    record["original_code"],
                    record["original_version_id"],
                    record["original_rules_hash"],
                    record["current_version_id"],
                    record["current_code"],
                    record["current_rules_hash"],
                    record["candidate_rules_hash"],
                    json.dumps(record["preflight"], ensure_ascii=False, sort_keys=True),
                    record["created_at"],
                    record["expires_at"],
                ),
            )
            conn.commit()

    def get_activation_ticket(self, token: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM activation_tickets WHERE token_hash = ?",
                (hash_token(token),),
            ).fetchone()
        if row is None:
            return None
        return {
            "token_hash": row["token_hash"],
            "version_id": row["version_id"],
            "version_code": row["version_code"],
            "baseline": json.loads(row["baseline_json"]),
            "original_code": row["original_code"],
            "original_version_id": row["original_version_id"],
            "original_rules_hash": row["original_rules_hash"],
            "current_version_id": row["current_version_id"],
            "current_code": row["current_code"],
            "current_rules_hash": row["current_rules_hash"],
            "candidate_rules_hash": row["candidate_rules_hash"],
            "preflight": json.loads(row["preflight_json"]),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "consumed_at": row["consumed_at"],
            "consumed_status": row["consumed_status"],
        }

    def activate_checked(
        self,
        token: str,
        *,
        expected_code: Optional[str] = None,
        now: Optional[str] = None,
    ) -> Dict[str, Any]:
        """受控启用：唯一写事务内复核凭证与三方状态，然后切换现行版。

        复核项（任一不符即整体回滚，原现行版保持不变）：

        1. 凭证存在、未使用、未过期，且绑定版本仍存在；
        2. 基准批当前成功结果与签发凭证时**完全一致**（内容 + 修订号）；
        3. 基准批所用原规则快照未被改动（sha256 与签发时一致）；
        4. 预检时的现行版仍是**当前**现行版，且其规则未变；
        5. 候选版规则未变。

        返回 ``{"status": "activated"|"ticket_not_found"|..., ...}``；只有
        ``activated`` 会真正切换，其余状态调用方映射为 HTTP 错误，现行版不动。
        """
        current_time = now or _utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM activation_tickets WHERE token_hash = ?",
                (hash_token(token),),
            ).fetchone()
            if row is None:
                conn.rollback()
                return {"status": "ticket_not_found"}
            if row["consumed_at"] is not None:
                conn.rollback()
                return {"status": "ticket_consumed", "consumed_at": row["consumed_at"],
                        "consumed_status": row["consumed_status"]}
            if expected_code is not None and row["version_code"] != expected_code:
                conn.rollback()
                return {
                    "status": "ticket_version_mismatch",
                    "ticket_code": row["version_code"],
                    "requested_code": expected_code,
                }
            if is_expired(row["expires_at"], current_time):
                conn.rollback()
                return {"status": "ticket_expired", "expires_at": row["expires_at"]}

            version = conn.execute(
                "SELECT id, code FROM rule_versions WHERE id = ?", (row["version_id"],)
            ).fetchone()
            if version is None:
                conn.rollback()
                return {"status": "candidate_missing", "code": row["version_code"]}

            candidate_rules = conn.execute(
                "SELECT grade, moisture_lo, moisture_hi, ash_lo, ash_hi "
                "FROM rules WHERE version_id = ? ORDER BY seq",
                (version["id"],),
            ).fetchall()
            candidate_rule_tuples: List[Rule] = [
                (r["grade"], r["moisture_lo"], r["moisture_hi"], r["ash_lo"], r["ash_hi"])
                for r in candidate_rules
            ]
            if rules_hash(candidate_rule_tuples) != row["candidate_rules_hash"]:
                conn.rollback()
                return {"status": "candidate_changed", "code": version["code"]}

            active = conn.execute(
                "SELECT v.id, v.code FROM active_version a "
                "JOIN rule_versions v ON v.id = a.version_id WHERE a.id = 1"
            ).fetchone()
            if active is None:
                conn.rollback()
                return {"status": "no_active_version"}
            if active["id"] != row["current_version_id"]:
                conn.rollback()
                return {
                    "status": "current_version_changed",
                    "expected_code": row["current_code"],
                    "actual_code": active["code"],
                }
            active_rules_rows = conn.execute(
                "SELECT grade, moisture_lo, moisture_hi, ash_lo, ash_hi "
                "FROM rules WHERE version_id = ? ORDER BY seq",
                (active["id"],),
            ).fetchall()
            active_rule_tuples: List[Rule] = [
                (r["grade"], r["moisture_lo"], r["moisture_hi"], r["ash_lo"], r["ash_hi"])
                for r in active_rules_rows
            ]
            if rules_hash(active_rule_tuples) != row["current_rules_hash"]:
                conn.rollback()
                return {"status": "current_rules_changed", "code": active["code"]}

            baseline = json.loads(row["baseline_json"])
            batch_row = conn.execute(
                "SELECT * FROM batch_results WHERE batch_no = ?",
                (baseline["batch_no"],),
            ).fetchone()
            if batch_row is None:
                conn.rollback()
                return {"status": "baseline_missing", "batch_no": baseline["batch_no"]}
            if batch_row_fingerprint(dict(batch_row)) != baseline["fingerprint"]:
                conn.rollback()
                return {"status": "baseline_changed", "batch_no": baseline["batch_no"]}

            # 原规则复核：既比对基准批保存的原规则快照，也比对原版号当前
            # 登记的规则，防止规则表被改写而快照"看起来没变"。
            original_rules_rows = conn.execute(
                "SELECT r.grade, r.moisture_lo, r.moisture_hi, r.ash_lo, r.ash_hi "
                "FROM rules r WHERE r.version_id = ? ORDER BY r.seq",
                (row["original_version_id"],),
            ).fetchall()
            current_original_rules: List[Rule] = [
                (r["grade"], r["moisture_lo"], r["moisture_hi"], r["ash_lo"], r["ash_hi"])
                for r in original_rules_rows
            ]
            snapshot_original_rules = rules_from_payload(
                json.loads(batch_row["original_rules_json"])
                if batch_row["original_rules_json"] is not None
                else None
            )
            if (
                not current_original_rules
                or rules_hash(current_original_rules) != row["original_rules_hash"]
                or snapshot_original_rules is None
                or rules_hash(snapshot_original_rules) != row["original_rules_hash"]
            ):
                conn.rollback()
                return {
                    "status": "original_rules_changed",
                    "original_code": row["original_code"],
                }

            conn.execute(
                "INSERT INTO active_version(id, version_id) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET version_id = excluded.version_id",
                (version["id"],),
            )
            conn.execute(
                "UPDATE activation_tickets SET consumed_at = ?, consumed_status = ? "
                "WHERE token_hash = ?",
                (current_time, "activated", row["token_hash"]),
            )
            conn.commit()
        return {
            "status": "activated",
            "code": version["code"],
            "version_id": version["id"],
            "previous_code": row["current_code"],
            "batch_no": baseline["batch_no"],
            "preflight": json.loads(row["preflight_json"]),
        }


class VersionConflict(Exception):
    def __init__(self, code: str):
        super().__init__(f"规则版号已存在: {code}")
        self.code = code
