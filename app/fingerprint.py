"""规范化序列化与指纹。

预检生成的启用凭证会记录"基准批 / 原规则 / 预检时现行版 / 候选版规则"的指纹，
``activate-checked`` 在同一写事务内重新计算并逐项比对。所有指纹都基于
**排序键名、紧凑分隔符** 的 JSON，保证跨进程、跨重启稳定，且不受 SQLite
物理行布局或 JSON 空白影响。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, List, Mapping, Optional, Sequence

from .rejudge import Rule


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def rules_payload(rules: Sequence[Rule]) -> List[dict]:
    """把内存规则元组规范成与存储顺序一致的 JSON 结构。"""
    return [
        {
            "seq": seq,
            "grade": grade,
            "moisture_lo": m_lo,
            "moisture_hi": m_hi,
            "ash_lo": a_lo,
            "ash_hi": a_hi,
        }
        for seq, (grade, m_lo, m_hi, a_lo, a_hi) in enumerate(rules)
    ]


def rules_hash(rules: Sequence[Rule]) -> str:
    return sha256_json(rules_payload(rules))


def rules_from_payload(payload: Optional[Iterable[Mapping[str, Any]]]) -> Optional[List[Rule]]:
    """把持久化的规则快照还原成内存规则；结构不合法返回 ``None``。"""
    if payload is None:
        return None
    rules: List[Rule] = []
    try:
        for row in sorted(payload, key=lambda item: item["seq"]):
            rules.append(
                (
                    str(row["grade"]),
                    int(row["moisture_lo"]),
                    int(row["moisture_hi"]),
                    int(row["ash_lo"]),
                    int(row["ash_hi"]),
                )
            )
    except (KeyError, TypeError, ValueError):
        return None
    return rules


def _loads(raw: Optional[str]) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        # 损坏的历史数据也纳入指纹，避免"无法解析 => 看起来没变"
        return {"__raw__": raw}


def batch_row_fingerprint(row: Mapping[str, Any]) -> str:
    """成功批结果整行的稳定指纹（含修订号，任何一次成功重跑都会改变它）。"""
    return sha256_json(
        {
            "batch_no": row["batch_no"],
            "original_code": row["original_code"],
            "judged_code": row["judged_code"],
            "record_count": row["record_count"],
            "revision": row["revision"],
            "updated_at": row["updated_at"],
            "result": _loads(row["result_json"]),
            "original_archive": _loads(row["original_archive_json"]),
            "original_rules": _loads(row["original_rules_json"]),
        }
    )
