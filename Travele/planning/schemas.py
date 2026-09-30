"""规划功能的数据结构 —— 一张行程长什么样（天 → 时段 → 活动节点 → 候选方案 → 地点/交通）。


"""
from __future__ import annotations

import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from Travele.retrieval.schemas import DocChunk


class WireModel(BaseModel):
    """所有对外数据结构的基类：出去是 camelCase，进来 camelCase / snake_case 都认。

    extra="forbid" 是故意的：字段名拼错要立刻报错，不能被静默忽略。
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


# ==========================================================================
# 枚举：值就是 wire 上的字符串，改动会直接打到前端
# ==========================================================================
class Slot(str, Enum):
    """一天分三段。前端用它拼节点编号，也用它回传决策表，别改。"""

    morning = "morning"
    afternoon = "afternoon"
    evening = "evening"


class TransportMethod(str, Enum):
    taxi = "taxi"
    bike = "bike"
    walk = "walk"
    bus = "bus"
    subway = "subway"
    drive = "drive"
    train = "train"
    boat = "boat"
    plane = "plane"


class TripStatus(str, Enum):
    planning = "planning"
    completed = "completed"
    failed = "failed"


# 时段的默认中文名（后端没给 slotLabel 时由这里补上）
SLOT_LABEL = {Slot.morning: "上午", Slot.afternoon: "下午", Slot.evening: "晚上"}
# "HH:MM"：前端只显示、不做解析，所以不要 ISO、也不要带秒
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


# ==========================================================================
# 行程结构（自下而上：地点/交通 → 方案 → 节点 → 时段 → 天 → 整张行程）
# ==========================================================================
class TimeRange(WireModel):
    start: str = Field(pattern=HHMM)
    end: str = Field(pattern=HHMM)


class Poi(WireModel):
    poi_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    poi_type: str | None = None
    rating: float | None = None
    address: str | None = None
    # 坐标：有就算可达性。目录里的点一定有；从攻略正文抽出来的点没有（攻略不写经纬度），
    # 这时不报错，但校验器会记一条"可达性未校验"——标注局限，比硬失败或悄悄放过都好。
    lat: float | None = None
    lng: float | None = None
    season_best: str | None = None
    # 地点照片的**文件名**（图在 Travele/P 下，接口挂成 /photos/<文件名>）。
    # 只有一部分目录点有图，没有就是 None —— 前端那里退回手绘底色，不显示破图。
    photo: str | None = None


class TransportOption(WireModel):
    method: TransportMethod
    duration_min: int = Field(ge=0)
    price: int | None = Field(default=None, ge=0)
    recommend_reason: str | None = None
    recommended: bool = False


class ActivityOption(WireModel):
    """一个候选方案：地点 + 停留时长 + 每种交通的耗时价格 + 顺路可加的第二点。"""

    poi: Poi
    duration_min: int = Field(ge=0)
    transport_options: list[TransportOption] = Field(default_factory=list)
    combination_poi: Poi | None = None


class SourceRef(WireModel):
    """溯源：这段内容出自哪篇攻略的第几个块。"""

    doc: str
    chunk_index: int | None = Field(default=None, ge=0)


class ActivityItem(WireModel):
    """一个活动节点。前端就是按 options 逐个渲染成可点的方案卡。"""

    seq: int = Field(ge=1)
    activity_type: str = "poi"
    options: list[ActivityOption] = Field(min_length=1)   # 至少要有一个候选，否则前端没得选
    time_range: TimeRange | None = None
    unverified: list[str] = Field(default_factory=list)   # 模型没证实的事实，前端灰标
    source_ref: SourceRef | None = None
    locked: bool = False                                  # 锁定后前端禁止切换


class Segment(WireModel):
    slot: Slot
    slot_label: str = ""
    items: list[ActivityItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def fill_slot_label(self):
        """后端可以不填 slotLabel，这里按 slot 补上中文，省得前端自己映射。"""
        if not self.slot_label:
            self.slot_label = SLOT_LABEL[self.slot]
        return self


class DayPlan(WireModel):
    day_index: int = Field(ge=1)            # 从 1 开始，和样例一致
    # 注意：字段名就叫 date，所以类型必须写成 datetime.date —— 写 date 会被这个字段名挡住
    date: datetime.date | None = None
    theme: str = ""
    segments: list[Segment] = Field(default_factory=list)


class PlanMeta(WireModel):
    """这张行程的质量标记：校验过没有、还剩哪些硬问题、后端自己处理掉的有哪些、引用了哪些攻略。

    issues 与 notes 的分工（前端也按这个分两处显示）：
      issues —— 校验规则没通过的硬问题（用户该知道的"这版有毛病"）；
      notes  —— 后端**已经自己处理掉**的说明（模型编的酒店被剔除、要的交通方式没有改用别的…）。
    前者影响 validated，后者不影响 —— 但都要如实写出来，不能悄悄咽下去。
    """

    validated: bool
    issues: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    source_docs: list[str] = Field(default_factory=list)


class TripDestination(WireModel):
    province: str
    city: str


class Budget(WireModel):
    total_estimate: int | None = Field(default=None, ge=0)
    currency: str = "CNY"
    estimate: bool = True


class Lodging(WireModel):
    """住哪儿：一家酒店住几晚。

    名字是模型选的，地址/星级/价格/预订链接由后端拿 hotels 工具查到的真数据补
    （模型只挑，不编事实）—— 对不上真数据的酒店会被剔除。
    """

    name: str = Field(min_length=1)
    address: str = ""
    star_rating: float | None = None
    price_per_night: float | None = Field(default=None, ge=0)
    check_in: datetime.date | None = None
    check_out: datetime.date | None = None
    booking_url: str = ""
    reason: str = ""                       # 为什么选这家


class IntercityLeg(WireModel):
    """一段大交通：怎么从出发地到目的地（去程 / 回程）。

    这一版的时间和班次是**模型给的**，后端不去核对，所以 `verified` 是 false，
    前端据此显示"未核对"（跟节点上的 unverified 一个路子）。要核对就去查 12306 / 航班。
    """

    direction: str = Field(pattern="^(outbound|inbound)$")   # outbound=去程，inbound=回程
    method: TransportMethod
    from_place: str = Field(min_length=1)
    to_place: str = Field(min_length=1)
    date: datetime.date | None = None
    depart: str = ""                       # "HH:MM"
    arrive: str = ""
    detail: str = ""                       # 车次 / 航班号、席别
    price: int | None = Field(default=None, ge=0)
    verified: bool = False                 # 后端有没有跟车站/机场核对过


class TripDetail(WireModel):
    """完整的行程。planning 阶段 days 为 null，前端会显示"还在规划"。"""

    trip_id: str = Field(min_length=1)
    status: TripStatus
    title: str
    plan_meta: PlanMeta
    destination: TripDestination
    budget: Budget | None = None
    intercity: list[IntercityLeg] = Field(default_factory=list)   # 大交通：去程/回程
    lodging: list[Lodging] = Field(default_factory=list)          # 住宿：一家酒店一段
    days: list[DayPlan] | None = None
    created_at: datetime.datetime

    @model_validator(mode="after")
    def completed_must_have_days(self):
        if self.status is TripStatus.completed and not self.days:
            raise ValueError("status=completed 时必须给出 days；还在规划中应该是 status=planning + days=null")
        return self


# ==========================================================================
# 内部结构（不走 wire，所以用 snake_case，不加 camelCase 别名）
# ==========================================================================
class CatalogPlace(BaseModel):
    """目的地目录里的一条点，对应 data/destinations.json 的一条。

    也是"候选点"的统一形状：从攻略正文抽出来的点也用它，只是没有坐标、没有开放时间。
    """

    model_config = ConfigDict(extra="ignore")   # 目录里以后加字段不该炸

    place_id: str
    province: str
    city: str
    name: str
    poi_type: str | None = None
    rating: float | None = None
    address: str | None = None
    lat: float | None = None                    # 目录里的点必须有；从语料抽出来的没有
    lng: float | None = None
    season_best: str | None = None
    duration_min: int = 120                     # 常规游览时长，排段用
    open_hours: str = "全天"                    # "HH:MM-HH:MM"，或 "全天"
    intro: str = ""                             # 确认阶段回给前端的一句话介绍
    photo: str | None = None                    # 照片文件名（Travele/P 下的图）；没有就是 None


class PlanContext(BaseModel):
    """装配结果：把用户需求和目录候选点拼成"生成行程要用的全部输入"。"""

    model_config = ConfigDict(extra="ignore")

    province: str#省份
    city: str#城市
    departure_city: str = ""                # 用户从哪来（排"大交通"要用；没填就留空）
    dates: list[datetime.date]#时间
    adults: int#成人
    children: int#小孩
    is_self_driving: bool#是否自驾
    must_visit: list[str] = Field(default_factory=list)                    # 用户原话
    must_visit_places: list[CatalogPlace] = Field(default_factory=list)    # 在目录里匹配到的
    unmatched_must_visit: list[str] = Field(default_factory=list)          # 目录里找不到的
    themes: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    remarks: str = ""
    candidates: list[CatalogPlace] = Field(default_factory=list)           # 该城市全部候选项
    candidates_from: str = "catalog"                                       # 候选点哪来的：catalog / guides
    docs: list[DocChunk] = Field(default_factory=list)                     # 召回到的攻略块（编排的原料）
    recall_error: str | None = None                                        # 召回失败的原因（成功为 None）
    # 长期记忆的文本块（偏好 / 补充说明 / 去过的点），由 memory.memory_text() 渲染好后填进来。
    # 还没有记忆时是空串，提示词里会如实写"（暂无记忆）"。
    memory: str = ""
    # 外部事实（天气 / 酒店 / 车次）：由 agent.prefetch_facts() 并行查一次、渲染成文本放在这儿，
    # 两条路（Agent / 单次生成）和回喂那一轮共用，不重复查。
    facts: str = ""


# ==========================================================================
# 草稿：① Agent 模式 / ② 单次生成 的产出（模型填"编排"，后端补"事实"）
#
# 为什么要单独一套：让模型只做它擅长的（选点、排序、定主题、写理由），
# 事实类的东西（坐标、规范名、交通耗时价格、时段）一律由后端按目录和距离补齐。
# 这样模型编不出坐标，也不会再出现"坐标中途丢掉导致可达性算不出来"那个坑。
# ==========================================================================
class DraftModel(WireModel):
    """草稿的基类：**忽略**多余字段（和 WireModel 的 forbid 不同）。

    成品是我们吐给前端的，字段写错必须报错；而草稿是**模型给我们的**，
    它一定会多塞字段（实测它自己加了个 `id`）。为这种无害差异去失败、去重试，
    是拿自己的严格惩罚自己。真正该严格的地方是成品 TripDetail。
    """

    model_config = ConfigDict(extra="ignore")


class DraftPoi(DraftModel):
    """草稿里的地点：模型只要说是"哪一个"——poi_id 优先，退一步给名字让后端去对。"""

    poi_id: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def need_an_identity(self):
        if not self.poi_id and not self.name:
            raise ValueError("草稿里的地点至少要给 poiId 或 name")
        return self


class DraftTransport(DraftModel):
    """草稿里的交通：耗时和价格可以不给，后端按真实距离估算补上。"""

    method: TransportMethod
    duration_min: int | None = Field(default=None, ge=0)
    price: int | None = Field(default=None, ge=0)
    recommend_reason: str | None = None
    recommended: bool = False


class DraftOption(DraftModel):
    """一个候选方案。durationMin 不给就用目录里的常规游览时长。"""

    poi: DraftPoi
    duration_min: int | None = Field(default=None, ge=0)
    transport_options: list[DraftTransport] = Field(default_factory=list)
    combination_poi: DraftPoi | None = None


class DraftItem(DraftModel):
    seq: int = Field(default=1, ge=1)
    options: list[DraftOption] = Field(min_length=1)
    time_range: TimeRange | None = None          # 可以不给，后端按前后顺序补
    unverified: list[str] = Field(default_factory=list)
    source_ref: SourceRef | None = None          # 模型可以标出处；不标就由后端按攻略正文兜底


class DraftSegment(DraftModel):
    slot: Slot
    items: list[DraftItem] = Field(default_factory=list)


class DraftDay(DraftModel):
    day_index: int = Field(ge=1)
    date: datetime.date | None = None
    theme: str = ""
    segments: list[DraftSegment] = Field(default_factory=list)


class DraftBudget(DraftModel):
    total_estimate: int | None = Field(default=None, ge=0)


class DraftConfirmIntro(DraftModel):
    """解说里对某一个点的介绍：只写 poiId 和正文，名字与出处由后端补。"""

    poi_id: str
    intro: str


class ConfirmDraft(DraftModel):
    """解说 Agent 的产出：各点介绍 + 这趟行程的出行建议。"""

    spot_intros: list[DraftConfirmIntro] = Field(default_factory=list)
    travel_tips: list[str] = Field(default_factory=list)


class DraftLodging(DraftModel):
    """草稿里的住宿：模型只挑"住哪家、住哪几晚"，其余事实后端去查。"""

    name: str
    check_in: datetime.date | None = None
    check_out: datetime.date | None = None
    reason: str = ""


class DraftIntercity(DraftModel):
    """草稿里的一段大交通。这一版是模型自己填时刻，后端只做结构校验。"""

    direction: str = Field(pattern="^(outbound|inbound)$")
    method: TransportMethod
    from_place: str
    to_place: str
    date: datetime.date | None = None
    depart: str = ""
    arrive: str = ""
    detail: str = ""
    price: int | None = Field(default=None, ge=0)


class TripDraft(DraftModel):
    """规划 Agent / 单次生成的产出：**只填编排**（谁、哪天、什么主题、先后、为什么）。
    """

    title: str = ""
    destination: TripDestination | None = None
    budget: DraftBudget | None = None
    intercity: list[DraftIntercity] = Field(default_factory=list)   # 去程 / 回程
    lodging: list[DraftLodging] = Field(default_factory=list)       # 住哪家、住哪几晚
    days: list[DraftDay] = Field(default_factory=list)
