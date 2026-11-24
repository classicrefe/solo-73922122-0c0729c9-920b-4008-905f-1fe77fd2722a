"""核心算法单元测试，含小规模域上的暴力枚举对拍。"""

from __future__ import annotations

from typing import List, Optional, Tuple

import pytest

from app import rejudge
from app.rejudge import Rule, build_axis_cells, classify, evaluate_sample

# 小规模规则：用边界 0/3/7/10 构造，对拍时把 MAX_VALUE 压到 10
RULES_V1: List[Rule] = [
    ("优质煤", 0, 3, 0, 3),
    ("普通煤", 0, 7, 0, 7),
]
RULES_V2: List[Rule] = [
    ("优质煤", 0, 2, 0, 2),
    ("普通煤", 0, 5, 0, 5),
]


def _brute_force(
    m_interval: Tuple[int, int],
    a_interval: Tuple[int, int],
    original_grade: Optional[str],
    original_rules: List[Rule],
    current_rules: List[Rule],
):
    """直接枚举区间内全部整数点，返回 {现行等级: 点数} 或 None（原版无解）。"""
    counts = {}
    for m in range(m_interval[0], m_interval[1] + 1):
        for a in range(a_interval[0], a_interval[1] + 1):
            if classify(original_rules, m, a) != original_grade:
                continue
            g = classify(current_rules, m, a)
            counts[g] = counts.get(g, 0) + 1
    return counts or None


@pytest.fixture(autouse=True)
def small_domain(monkeypatch):
    monkeypatch.setattr(rejudge, "MAX_VALUE", 10)
    yield


def test_classify_first_match_wins_and_ungraded():
    rules = [("A", 0, 5, 0, 5), ("B", 0, 10, 0, 10)]
    assert classify(rules, 3, 3) == "A"          # 首条命中
    assert classify(rules, 8, 8) == "B"          # 第二条命中
    assert classify(RULES_V1, 9, 9) is None      # 全部未命中 => 未分级
    # 闭区间端点命中
    assert classify(RULES_V1, 7, 7) == "普通煤"
    assert classify(RULES_V1, 3, 3) == "优质煤"


def test_axis_cells_cover_domain():
    cells = build_axis_cells([(0, 3), (4, 7)])
    # 切点: 0,4,4,8,11 -> 格 [0,3],[4,7],[8,10]
    assert cells == [(0, 3, 0), (4, 7, 4), (8, 10, 8)]
    total_points = sum(hi - lo + 1 for lo, hi, _ in cells)
    assert total_points == 11  # 0..10 全覆盖不重叠


def test_unique_grade_matches_brute_force():
    options = evaluate_sample((0, 2), (0, 2), "优质煤", RULES_V1, RULES_V2)
    expected = _brute_force((0, 2), (0, 2), "优质煤", RULES_V1, RULES_V2)
    assert options is not None and len(options) == 1
    assert options[0].grade == "优质煤"
    assert options[0].feasible_pairs == expected["优质煤"] == 9
    # 见证点确实在旧档区间内且两版定级符合预期
    wm, wa = options[0].witness_moisture, options[0].witness_ash
    assert 0 <= wm <= 2 and 0 <= wa <= 2
    assert classify(RULES_V2, wm, wa) == "优质煤"


def test_multiple_grades_list_each_with_witness():
    # 原版为单条兜底规则（全域均为普通煤），现行版把全域切成 优质煤/普通煤/未分级
    v1 = [("普通煤", 0, 10, 0, 10)]
    options = evaluate_sample((0, 10), (0, 10), "普通煤", v1, RULES_V2)
    expected = _brute_force((0, 10), (0, 10), "普通煤", v1, RULES_V2)
    assert options is not None
    assert {o.grade for o in options} == set(expected.keys()) == {"优质煤", "普通煤", None}
    for opt in options:
        assert opt.feasible_pairs == expected[opt.grade]
        assert classify(RULES_V2, opt.witness_moisture, opt.witness_ash) == opt.grade
        assert classify(v1, opt.witness_moisture, opt.witness_ash) == "普通煤"
    # 总可行对数 = 满足原版原等级的点数
    assert sum(o.feasible_pairs for o in options) == sum(expected.values())
    # 未分级排在最后
    assert options[-1].grade is None


def test_ungraded_is_a_valid_unique_grade():
    # 原版兜底规则让所有点都是普通煤；现行版全部落空 => 唯一结果是未分级
    v1 = [("普通煤", 0, 10, 0, 10)]
    v2: List[Rule] = [("优质煤", 0, 0, 0, 0)]
    options = evaluate_sample((5, 9), (5, 9), "普通煤", v1, v2)
    assert len(options) == 1 and options[0].grade is None
    assert options[0].feasible_pairs == 25


def test_infeasible_original_grade_returns_none():
    # 旧档区间在 v1 下根本没有 优质煤，却声称原等级优质煤
    assert evaluate_sample((8, 10), (8, 10), "优质煤", RULES_V1, RULES_V2) is None
    # 原等级为未分级但区间内全有等级 => 同样无解
    assert evaluate_sample((0, 2), (0, 2), None, RULES_V1, RULES_V2) is None


def test_original_ungraded_feasible():
    # 区间 [8,10]×[8,10] 在 v1 下确实未分级
    options = evaluate_sample((8, 10), (8, 10), None, RULES_V1, RULES_V2)
    assert options is not None and len(options) == 1 and options[0].grade is None
    assert options[0].feasible_pairs == 9


def test_closed_intervals_endpoints_are_included():
    rules = [("A", 3, 7, 3, 7)]
    # 旧档仅包含两个边界点；它们必须被算作可行点
    options = evaluate_sample((3, 7), (3, 7), "A", rules, rules)
    assert len(options) == 1 and options[0].grade == "A"
    assert options[0].feasible_pairs == 25  # 5×5 闭区间整数点


def test_brute_force_parity_many_cases():
    """随机化边界生成大量场景，与暴力枚举逐点对拍（含可行对数与等级集合）。"""
    import random

    rng = random.Random(20261124)

    def random_rules():
        rules = []
        for _ in range(rng.randint(1, 5)):
            mlo, mhi = sorted(rng.sample(range(11), 2))
            alo, ahi = sorted(rng.sample(range(11), 2))
            rules.append((f"G{rng.randint(0, 2)}", mlo, mhi, alo, ahi))
        return rules

    for _ in range(300):
        v1, v2 = random_rules(), random_rules()
        mlo, mhi = sorted(rng.sample(range(11), 2))
        alo, ahi = sorted(rng.sample(range(11), 2))
        # 原等级从 v1 在该区间的实际结果集合里挑，也故意挑不存在的
        possible = {classify(v1, m, a) for m in range(mlo, mhi + 1)
                    for a in range(alo, ahi + 1)}
        original = rng.choice(list(possible) + ["幽灵等级"])
        options = evaluate_sample((mlo, mhi), (alo, ahi), original, v1, v2)
        expected = _brute_force((mlo, mhi), (alo, ahi), original, v1, v2)
        if expected is None:
            assert options is None
            continue
        assert {o.grade for o in options} == set(expected)
        for opt in options:
            assert opt.feasible_pairs == expected[opt.grade]
            assert mlo <= opt.witness_moisture <= mhi
            assert alo <= opt.witness_ash <= ahi
            assert classify(v1, opt.witness_moisture, opt.witness_ash) == original
            assert classify(v2, opt.witness_moisture, opt.witness_ash) == opt.grade


def test_complexity_independent_of_domain_width(monkeypatch):
    """同样的规则与区间，域宽 10 与 10000 下结果结构一致，且不随域宽枚举。"""
    monkeypatch.setattr(rejudge, "MAX_VALUE", 10000)
    current = [("A", 100, 900, 200, 800), ("B", 0, 2000, 0, 3000)]
    # 原版只有一条全域规则，原等级在全域可行；现行版把全域切成 A/B/未分级
    original = [("兜底", 0, 10000, 0, 10000)]
    options = evaluate_sample((0, 10000), (0, 10000), "兜底", original, current)
    grades = {o.grade for o in options}
    assert grades == {"A", "B", None}
    # 手算 A 区域: 水分 [100,900]×灰分[200,800] = 801×601
    a_opt = next(o for o in options if o.grade == "A")
    assert a_opt.feasible_pairs == 801 * 601
    # 三等级可行对数之和 = 全域点数
    b_count = 2001 * 3001 - 801 * 601
    assert sum(o.feasible_pairs for o in options) == 10001**2
    assert next(o for o in options if o.grade == "B").feasible_pairs == b_count
