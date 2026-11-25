"""请求/响应数据模型。

测值约定：水分、灰分均为 0~10000 的整数万分比（闭区间端点）。
等级 ``None`` 表示"未分级"。
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import AliasChoices, BaseModel, Field, model_validator

# 测值合法范围（万分比整数）
MIN_VALUE = 0
MAX_VALUE = 10000

# 业务上限
MAX_RULES_PER_VERSION = 20
MAX_RECORDS_PER_BATCH = 100


class Interval(BaseModel):
    """闭区间 [lo, hi]，端点均为整数。"""

    lo: int = Field(..., ge=MIN_VALUE, le=MAX_VALUE, description="闭区间下端点（含），万分比整数")
    hi: int = Field(..., ge=MIN_VALUE, le=MAX_VALUE, description="闭区间上端点（含），万分比整数")

    @model_validator(mode="after")
    def _check_order(self) -> "Interval":
        if self.lo > self.hi:
            raise ValueError("区间下端点 lo 不得大于上端点 hi")
        return self


class RuleIn(BaseModel):
    """单条判定规则：水分、灰分同时落在两个闭区间内时命中 grade。"""

    grade: str = Field(..., min_length=1, max_length=64, description="命中后赋予的等级名称")
    moisture: Interval = Field(..., description="水分闭区间")
    ash: Interval = Field(..., description="灰分闭区间")


class VersionCreate(BaseModel):
    """发布一个规则版本（不自动启用）。"""

    code: str = Field(..., min_length=1, max_length=64, description="规则版号，全局唯一，如 v3")
    rules: List[RuleIn] = Field(..., min_length=1, max_length=MAX_RULES_PER_VERSION)
    note: Optional[str] = Field(None, max_length=512)


class SampleRecord(BaseModel):
    """旧档中的一条煤样。"""

    sample_no: str = Field(..., min_length=1, max_length=128, description="样号")
    moisture: Interval = Field(..., description="旧档水分闭区间")
    ash: Interval = Field(..., description="旧档灰分闭区间")
    original_grade: Optional[str] = Field(
        ..., description="原规则判定等级；null/未提供表示原等级为未分级"
    )


class RejudgeRequest(BaseModel):
    """按批提交的重判请求。"""

    batch_no: str = Field(..., min_length=1, max_length=128, description="批号，下游按此查询")
    original_version: str = Field(..., min_length=1, max_length=64, description="旧档原规则版号")
    records: List[SampleRecord] = Field(..., min_length=1, max_length=MAX_RECORDS_PER_BATCH)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "batch_no": "BATCH-DEMO-001",
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
                        {
                            "sample_no": "S-003",
                            "moisture": {"lo": 0, "hi": 10000},
                            "ash": {"lo": 0, "hi": 10000},
                            "original_grade": "普通煤",
                        },
                    ],
                }
            ]
        }
    }


class GradeOption(BaseModel):
    """重判出现多等级时的一个候选：等级 + 一对可行测值（见证点）。"""

    grade: Optional[str] = Field(..., description="候选等级，null 表示未分级")
    witness_moisture: int = Field(..., description="落在旧档区间内、可得出该等级的水分测值")
    witness_ash: int = Field(..., description="落在旧档区间内、可得出该等级的灰分测值")
    feasible_pairs: int = Field(..., ge=0, description="旧档区间内可得出该等级的整数测值对总数")


class RejudgedSample(BaseModel):
    """单条煤样的重判结果。"""

    sample_no: str
    original_grade: Optional[str]
    unique: bool = Field(..., description="现行版下等级是否唯一")
    final_grade: Optional[str] = Field(
        ..., description="唯一时的定级（含未分级 null）；不唯一时为 null"
    )
    options: List[GradeOption] = Field(
        ..., description="全部可行等级；唯一时长度为 1，多等级时长度大于 1"
    )


class RejudgeResult(BaseModel):
    """一批重判成功入仓的结果。"""

    batch_no: str
    original_version: str
    judged_version: str = Field(..., description="本次实际使用的现行规则版号")
    record_count: int
    samples: List[RejudgedSample]


class VersionOut(BaseModel):
    id: int
    code: str
    active: bool
    rule_count: int
    note: Optional[str] = None
    created_at: str


class RuleOut(BaseModel):
    seq: int
    grade: str
    moisture_lo: int
    moisture_hi: int
    ash_lo: int
    ash_hi: int


# --------------------------------------------------------------- 预检 / 受控启用
class PreflightRequest(BaseModel):
    """用一条**已成功入仓**的历史批次验证候选版。"""

    batch_no: str = Field(
        ...,
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("batch_no", "baseline_batch_no"),
        description="已成功批号；取其入仓时保存的原档区间、原等级与原规则重算候选版",
    )

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "examples": [{"batch_no": "BATCH-DEMO-001"}]
        },
    }


class PreflightSample(RejudgedSample):
    """预检中的一条样：与重判结果同构，便于直接比对见证值与候选等级。"""


class PreflightFailure(BaseModel):
    """预检未通过的一个样号。"""

    sample_no: str
    original_grade: Optional[str]
    reason: str = Field(..., description="infeasible_original_grade 或 ambiguous_grade")
    message: str
    options: List[GradeOption] = Field(
        default_factory=list,
        description="候选版下的等级选项及每个等级的见证测值与可行对数；无解时为空",
    )


class PreflightResult(BaseModel):
    """预检通过：全部样号在候选版下唯一，签发一次性启用凭证。"""

    batch_no: str
    original_version: str
    candidate_version: str
    current_version: str
    sample_count: int
    samples: List[PreflightSample]
    ticket: str = Field(..., description="一次性启用凭证，仅本次响应返回明文")
    expires_at: str = Field(..., description="凭证过期时间（UTC ISO8601）")
    preflighted_at: str


class ActivateCheckedRequest(BaseModel):
    """凭预检签发的一次性凭证受控启用。"""

    ticket: str = Field(
        ...,
        min_length=16,
        max_length=256,
        validation_alias=AliasChoices("ticket", "activation_token"),
        description="预检通过后返回的一次性凭证",
    )

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "examples": [{"ticket": "填入 POST /api/versions/{code}/preflight 返回的 ticket"}]
        },
    }


class ActivateCheckedResult(BaseModel):
    code: str
    active: bool
    previous_version: str
    batch_no: str
    consumed_at: str
    sample_count: int


class ErrorResponse(BaseModel):
    detail: str
