"""核心重判算法。

思路（坐标压缩 / 平面矩形划分）：

* 每条规则是水分轴、灰分轴上两个闭区间的笛卡尔积，规则边界把二维整数平面
  切成若干矩形格，同一格内任一点在任一版规则下命中的规则完全相同。
* 把两个版本所有规则的边界（``lo``、``hi+1``）加上 ``0``、``10001`` 排序去重，
  相邻切点之间就是一维均匀格；水分格 × 灰分格 即二维均匀格。
* 只遍历与旧档闭区间矩形相交的格，每格取一个代表点（见证点）分别用原版、
  现行版定级；原版定级不等于原等级的格直接丢弃，其余按现行版等级聚合，
  并把各格覆盖的整数点数累加为该等级的可行测值对数。

复杂度只随**规则边界数**与**记录数**增长，与测值域宽度（10001）无关，
全程不枚举 0~10000 的测值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .models import MAX_VALUE

# 规则的内存表示：(等级, 水分lo, 水分hi, 灰分lo, 灰分hi)；等级 None 不会出现在规则里
Rule = Tuple[str, int, int, int, int]

# 一维均匀格：(lo, hi, 代表点)，lo/hi/代表点均为 [0, MAX_VALUE] 内整数
AxisCell = Tuple[int, int, int]


@dataclass(frozen=True)
class GradeFeasibility:
    """一个可行现行等级及其见证点与可行测值对数。"""

    grade: Optional[str]
    witness_moisture: int
    witness_ash: int
    feasible_pairs: int


def classify(rules: Sequence[Rule], moisture: int, ash: int) -> Optional[str]:
    """按顺序取首条同时命中两个闭区间的规则；未命中返回 None（未分级）。"""
    for grade, m_lo, m_hi, a_lo, a_hi in rules:
        if m_lo <= moisture <= m_hi and a_lo <= ash <= a_hi:
            return grade
    return None


def build_axis_cells(intervals: Sequence[Tuple[int, int]]) -> List[AxisCell]:
    """根据一组闭区间边界构造一维均匀格。

    切点取每个区间的 ``lo`` 与 ``hi+1``，并补全域端点 ``0``、``MAX_VALUE+1``。
    相邻切点 ``[c_i, c_{i+1}-1]`` 为一格，代表点取左端点 ``c_i``。
    """
    cuts = {0, MAX_VALUE + 1}
    for lo, hi in intervals:
        cuts.add(lo)
        cuts.add(hi + 1)
    ordered = sorted(cuts)
    cells: List[AxisCell] = []
    for left, right in zip(ordered, ordered[1:]):
        # 右切点是 hi+1，因此格内整数点为 [left, right-1]
        cells.append((left, right - 1, left))
    return cells


def _overlap(cell_lo: int, cell_hi: int, q_lo: int, q_hi: int) -> Optional[Tuple[int, int, int]]:
    """均匀格与旧档闭区间的交集；返回 (代表点, 交集整数点数)，不相交返回 None。"""
    lo = max(cell_lo, q_lo)
    hi = min(cell_hi, q_hi)
    if lo > hi:
        return None
    # 见证点取交集下端点：仍落在同一均匀格内，定级结果不变且必在旧档区间内
    return lo, hi - lo + 1


def evaluate_sample(
    moisture_interval: Tuple[int, int],
    ash_interval: Tuple[int, int],
    original_grade: Optional[str],
    original_rules: Sequence[Rule],
    current_rules: Sequence[Rule],
) -> Optional[List[GradeFeasibility]]:
    """重判单条煤样。

    返回现行版下全部可行等级（含见证点与可行对数）；若旧档区间内不存在任何
    能按原版得出原等级的测值对，返回 ``None``（调用方应退回整批）。
    """
    # 两个版本的边界一起压缩，保证对两个版本同时均匀
    m_intervals = [(r[1], r[2]) for r in original_rules]
    a_intervals = [(r[3], r[4]) for r in original_rules]
    m_intervals += [(r[1], r[2]) for r in current_rules]
    a_intervals += [(r[3], r[4]) for r in current_rules]

    m_cells = build_axis_cells(m_intervals)
    a_cells = build_axis_cells(a_intervals)

    m_lo0, m_hi0 = moisture_interval
    a_lo0, a_hi0 = ash_interval

    # grade -> [可行对数, 见证水分, 见证灰分]
    aggregated: Dict[Optional[str], List[int]] = {}

    for c_m_lo, c_m_hi, c_m_rep in m_cells:
        m_hit = _overlap(c_m_lo, c_m_hi, m_lo0, m_hi0)
        if m_hit is None:
            continue
        m_witness, m_count = m_hit
        for c_a_lo, c_a_hi, c_a_rep in a_cells:
            a_hit = _overlap(c_a_lo, c_a_hi, a_lo0, a_hi0)
            if a_hit is None:
                continue
            a_witness, a_count = a_hit

            # 原版必须能得出原等级（含原等级为未分级 None 的情况）
            if classify(original_rules, m_witness, a_witness) != original_grade:
                continue

            current_grade = classify(current_rules, m_witness, a_witness)
            pair_count = m_count * a_count
            entry = aggregated.get(current_grade)
            if entry is None:
                aggregated[current_grade] = [pair_count, m_witness, a_witness]
            else:
                entry[0] += pair_count

    if not aggregated:
        return None

    # 排序保证输出确定：有等级名按名字，未分级（None）排最后
    options = [
        GradeFeasibility(grade=grade, witness_moisture=vals[1], witness_ash=vals[2],
                         feasible_pairs=vals[0])
        for grade, vals in sorted(aggregated.items(), key=lambda kv: (kv[0] is None, kv[0] or ""))
    ]
    return options
