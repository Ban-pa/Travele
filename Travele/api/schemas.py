"""HTTP 接口自己的数据结构 —— 前端发上来什么、接口层回什么。

一句话：请求、确认、进度、错误、回执都在这里；**行程本身的定义不在这里**，
在 planning/schemas.py（接口层引用它，不重复定义同一个概念）。

分工与方向：
- 本文件 → 依赖 planning/schemas.py（要复用基类、Slot、TripStatus）
- planning 不反向依赖本文件：行程是它产出的东西，不该反过来认识 HTTP

谁调我：api/main.py（当请求/响应模型）
我调谁：planning/schemas.py（复用 WireModel 基类与几个枚举）、pydantic
"""
from __future__ import annotations

import datetime
from enum import Enum
from typing import Any

from pydantic import Field, field_validator, model_validator

from Travele.planning.schemas import Slot, TransportMethod, TripStatus, WireModel


# ==========================================================================
# 枚举：值就是 wire 上的字符串
# ==========================================================================
class TripMode(str, Enum):
    """两种入口：explicit=自己填表，discover=从随机发现那边挑的。"""

    explicit = "explicit"
    discover = "discover"


class BudgetTier(str, Enum):
    low = "low"
    mid = "mid"
    high = "high"


class ProgressEventType(str, Enum):
    """SSE 事件名。前端靠它判断进度条怎么显示、以及何时收工。

    排不出行程时不发"降级"这类事件，直接发 failed 带上给用户看的话。
    """

    searching = "searching"
    drafting = "drafting"
    validating = "validating"
    revising = "revising"
    done = "done"
    failed = "failed"


class ErrorCode(str, Enum):
    llm_timeout = "LLM_TIMEOUT"
    llm_rate_limit = "LLM_RATE_LIMIT"
    validation_failed = "VALIDATION_FAILED"
    trip_not_found = "TRIP_NOT_FOUND"
    city_not_found = "CITY_NOT_FOUND"
    internal_error = "INTERNAL_ERROR"


# ==========================================================================
# 前端提交① POST /api/trips —— 出行需求
# ==========================================================================
class Schedule(WireModel):
    trip_start: datetime.date
    trip_end: datetime.date

    @model_validator(mode="after")
    def trip_end_not_before_start(self):
        if self.trip_end < self.trip_start:
            raise ValueError("返回日期不能早于出发日期")
        return self

    @model_validator(mode="after")
    def trip_start_cnt_tolong(self):
        # timedelta 不能直接和 int 比（会比出 TypeError → 提交接口 500），要比 .days
        if (self.trip_end - self.trip_start).days > 7:
            raise ValueError("旅行时间不能超过7天")
        return self



class UserInfo(WireModel):
    adults: int = Field(ge=1)
    children: int = Field(default=0, ge=0)
    departure_city: str = Field(min_length=1) #触发地点
    budget_tier: BudgetTier | None = None   # 预算


class Destination(WireModel):
    province: str = Field(min_length=1)
    city: str = Field(min_length=1)
    center_place: str | None = None         #中心地点
    must_visit: list[str] = Field(default_factory=list)   # 必去地点

    @field_validator("must_visit")
    @classmethod
    def drop_blank_names(cls, names: list[str]) -> list[str]:
        """前端按逗号/顿号切分，可能切出空串，这里丢掉。"""
        return [name.strip() for name in names if name.strip()]


class TripPreferences(WireModel):
    themes: list[str] = Field(default_factory=list) #主题
    avoid: list[str] = Field(default_factory=list) #避免


class PlanningRequest(WireModel):
    """前端表单的完整请求体。必填最小集：schedule / adults / departureCity / 省市 / isSelfDriving。"""
    mode: TripMode = TripMode.explicit
    schedule: Schedule
    user: UserInfo
    destination: Destination
    is_self_driving: bool
    prefs: TripPreferences = Field(default_factory=TripPreferences)
    remarks: str = ""                       # 没填就是空串，不是省略


# ==========================================================================
# 前端提交② POST /api/trips/{tripId}/confirm —— 决策表
# ==========================================================================
class ConfirmDecision(WireModel):
    """一条"用户改过的节点"。前端只把动过的节点传上来，没动的一律不传。"""

    day_index: int = Field(ge=1)
    slot: Slot
    seq: int = Field(ge=1)
    chosen_poi_id: str | None = None        # 只换交通时可以没有这一项
    chosen_transport: TransportMethod | None = None   # 只换地点时可以没有这一项

    @model_validator(mode="after")
    def at_least_one_choice(self):
        if self.chosen_poi_id is None and self.chosen_transport is None:
            raise ValueError("每条决策至少要改一样东西（换地点或换交通），没改就不该传上来")
        return self


class ConfirmRequest(WireModel):
    """decisions 为空数组是合法的，意思就是"照生成的行程确认"。"""

    decisions: list[ConfirmDecision] = Field(default_factory=list)


# ==========================================================================
# 后端返回② POST /api/trips/{tripId}/confirm —— 各点介绍与出行建议
# ==========================================================================
class SpotIntro(WireModel):
    poi_id: str
    name: str
    intro: str
    source_doc: str | None = None


class ConfirmResponse(WireModel):
    trip_id: str
    spot_intros: list[SpotIntro] = Field(default_factory=list)
    travel_tips: list[str] = Field(default_factory=list)


class StoredPlan(WireModel):
    """历史旅游方案列表里的一条（用户确认过的行程）。

    列表页只要这几列；点进去看整份行程时，后端直接把存下来的那份 TripDetail 还回去。
    """

    plan_id: str
    title: str
    province: str
    city: str
    start_date: datetime.date | None = None
    end_date: datetime.date | None = None
    saved_at: str = ""


# ==========================================================================
# 其它：提交回执 / SSE 进度 / 统一错误体
# ==========================================================================
class TripReceipt(WireModel):
    """POST /api/trips 的 202 回执。前端只读 tripId。"""

    trip_id: str
    status: TripStatus = TripStatus.planning
    created_at: datetime.datetime | None = None


class ProgressEvent(WireModel):
    """SSE 每条 data: 的载荷：前端读 event（决定怎么显示）和 message（显示的文字）。

    约定：一条流的最后必须是 done 或 failed，否则前端会一直等到 210 秒超时。
    （后端总预算 180 秒，超时会发 failed「创建超时」，正常情况下前端等不到 210 秒。）
    """

    event: ProgressEventType
    message: str = ""


class ApiErrorBody(WireModel):
    """统一错误体：直接顶层返回，不套 HTTPException 的 {"detail": ...}。

    code 用 ErrorCode 的已知值；未知情况允许直接给字符串，方便排错时不被枚举卡住。
    """

    code: ErrorCode | str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
