"""规划功能的主要函数 —— 从"用户需求"到"一张能通过校验的行程"。

读的顺序（从上到下一路读完就懂整条规划）：
  一、读目的地目录
  二、装配：需求 → PlanContext（目的地目录 + 召回攻略 + 长期记忆）
  三、生成：交给模型排（agent.make_draft：Agent 模式优先，不行退到单次生成）
  三点五、草稿 → 成品：补事实（坐标、规范名、交通耗时价格、时段），顺手修掉时段撞车
  四、校验：10 类确定性规则；不过就先回喂模型重排一轮，还不过才写进 planMeta.issues
      —— 模型只填"编排"，坐标、交通耗时、价格这些事实一律由本文件补齐，不信模型给的数
  五、按决策表合并（用户点"确认"时用）
  六、确认返回：各点介绍 + 出行建议；同时把这趟写进长期记忆（偏好/补充说明/去过的点/历史方案）
"""
import datetime
import hashlib
import json
import logging
import math
import pathlib
from collections.abc import Callable, Iterable

from Travele.memory import memory
from Travele.planning import agent, tool
from Travele.planning.schemas import (
    ActivityItem,
    ActivityOption,
    Budget,
    CatalogPlace,
    DayPlan,
    IntercityLeg,
    Lodging,
    PlanContext,
    PlanMeta,
    Poi,
    Segment,
    Slot,
    SLOT_LABEL,
    SourceRef,
    TimeRange,
    TransportMethod,
    TransportOption,
    TripDestination,
    TripDetail,
    TripDraft,
    TripStatus,
)
from Travele.retrieval.recall import compose_keywords, retrieve
from Travele.retrieval.schemas import DocChunk

logger = logging.getLogger("travele")

CATALOG_FILE = pathlib.Path(__file__).resolve().parents[1] / "data" / "destinations.json"

# 三个时段的常规起始时间（从 0 点算的分钟数）
SEGMENT_ORDER = (Slot.morning, Slot.afternoon, Slot.evening)
SEGMENT_START = {Slot.morning: 9 * 60, Slot.afternoon: 13 * 60 + 30, Slot.evening: 18 * 60 + 30}
# 每个时段合理的开始时间区间（校验用）：给得宽松，只抓"下午 11:00 开始"这种明显拧着的
SLOT_WINDOW = {Slot.morning: (5 * 60, 12 * 60),
               Slot.afternoon: (11 * 60 + 30, 19 * 60),
               Slot.evening: (17 * 60, 24 * 60)}

# 中间那一天至少要有几个时段排上了内容（首末天可能在赶路，只要求 1 段）。
# 校验不达标会把问题回喂给模型重排 —— 这是"一天只剩上午有事"这类差评的兜底。
MIN_FILLED_SLOTS = 2

# 同一个地点整趟最多出现几次（同一天最多 1 次另算）。超了后端直接去掉并记一条说明：
# 实测"少思考"的模型很喜欢拿几个点来回转着填时段，等它自己想明白不如直接修掉。
MAX_PLACE_REPEAT = 2

# 一天能排到的最晚时刻（23:59）。超过这个点的安排属于"排到后半夜"，
# 时间格式（HH:MM）也表达不了 24:xx —— 所以这里是硬边界，不是偏好。
DAY_LAST_MINUTE = 23 * 60 + 59

# 解说的"出处"标签：攻略片段里没写到这个点时用它（目录才是这些事实的来源）
CATALOG_SOURCE_LABEL = "目的地目录"

# 步行/骑行/公交的速度（公里/小时）：这三种没有数据源，只能估。
# 打车与自驾不在这张表里 —— 它们的耗时来自高德（tool.amap_ride）。
SPEED_KMH = {"walk": 4.5, "bike": 12.0, "bus": 16.0}

# 交通方式的中文说法（给用户看的问题里要用；模型提到我们列表外的冷门方式时，就原样用它的词）
TRANSPORT_TEXT = {"walk": "步行", "bike": "骑行", "bus": "公交", "taxi": "打车", "drive": "自驾",
                  "subway": "地铁", "train": "火车", "high_speed_rail": "高铁"}


class CityNotFound(Exception):
    """要去的省市不在目录里 —— 接口层转成 400 CITY_NOT_FOUND。"""


class GenerationFailed(Exception):
    """模型这次没排出行程 —— 接口层转成 SSE 的 failed 事件，让用户重试。

    message 只写人能看懂的话，直接给用户看；技术原因记日志。
    """


class ContentFailed(Exception):
    """解说（各点介绍与出行建议）没生成出来 —— 接口层转成 500，让用户再试一次。

    内容生成不做兜底：不退回"攻略原句"那种规则版，也不给半成品。
    """


class UnknownNode(Exception):
    """决策表指向行程里不存在的节点 —— 接口层转成 422。"""


class UnknownChoice(Exception):
    """决策表选了一个这个节点上根本没有的方案/交通 —— 接口层转成 422。"""


# ==========================================================================
# 一、读目的地目录
# ==========================================================================
_catalog: list[CatalogPlace] | None = None


def load_catalog() -> list[CatalogPlace]:
    """读 data/destinations.json（进程内缓存一份，目录是静态数据）。"""
    global _catalog
    if _catalog is None:
        raw = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        _catalog = [CatalogPlace.model_validate(item) for item in raw["places"]]
    return _catalog


def candidates_of(province: str, city: str) -> list[CatalogPlace]:
    """这个城市在**目录**里有哪些候选点。目录里没有就返回空 —— 这不是错误。

    目录只负责"预置的坐标与元数据"，它**不是准入闸门**：全国几千个县不可能手写，
    目录没覆盖的城市改用从攻略正文抽点（见 extract_places）。
    """
    return [place for place in load_catalog()
            if place.province == province and place.city == city]


def check_city_available(province: str, city: str) -> None:
    """提交时的便宜预检：目录里有、或者攻略库里有这个城市，就认为排得出来。

    两点都没有就直接拒。注意这不是"用户填错了城市"，而是"这个城市还没有数据"，
    所以错误信息要说清缺的是什么 —— 缺语料，不是缺用户输入。
    """
    if candidates_of(province, city):
        return
    if retrieve(city, ""):
        return
    raise CityNotFound(
        f"「{province}{city}」既不在目的地目录里，也没有攻略语料，暂时排不出行程 —— "
        f"先在 Travele/ingest/corpus 放一篇这个城市的攻略（再用上传页入库），或补进 destinations.json")


# ==========================================================================
# 二、装配：需求 → PlanContext
# ==========================================================================
def expand_dates(trip_start: datetime.date, trip_end: datetime.date) -> list[datetime.date]:
    """起止日期 → 逐天列表（含首尾）。"""
    days = (trip_end - trip_start).days + 1
    return [trip_start + datetime.timedelta(days=offset) for offset in range(days)]


def match_place(candidates: list[CatalogPlace], name: str) -> CatalogPlace | None:
    """先精确匹配，再退一步做包含匹配（用户写"洱海"，目录里是"洱海生态廊道"）。"""
    for place in candidates:
        if place.name == name:
            return place
    for place in candidates:
        if name in place.name or place.name in name:
            return place
    return None


# 从语料里抽地名时认这些后缀（"XX山""XX寺""XX古镇"…）
PLACE_SUFFIXES = ("山", "湖", "寺", "塔", "宫", "园", "馆", "城", "镇", "村", "寨",
                  "廊", "岛", "湾", "窟", "峰", "岭", "街", "巷", "桥", "泉", "海",
                  "江", "河", "谷", "原", "门", "楼", "观", "台", "庙", "祠", "关", "林")
# 介绍句的"定义模式"：地名后面紧跟这些字，说明这句是在介绍它
# （"龙门石窟在洛阳南郊""丽景门是老城的西门"）。这是区分真地名与误切片段最本质的信号。
INTRO_TAILS = ("在", "是", "的", "有", "为", "位", "距", "又", "也")
# 这些字打头的多半不是地名（"这一段""那座城"这种靠"出现两次以上"再过滤一道）
NOT_NAME_STARTS = set("这那一不了有是在的个把和与就都也很还只从对向为以再又更最真"
                      "座全半该此各两数沿整条")


def name_candidates(sentence: str) -> list[str]:
    """在一句话里按"地名后缀 + 长度 2-6 + 全是汉字"切出疑似地名。"""
    found: list[str] = []
    for start in range(len(sentence)):
        if sentence[start] in NOT_NAME_STARTS:
            continue
        for length in range(2, 7):
            piece = sentence[start:start + length]
            if len(piece) < length:
                break
            if piece[-1] in PLACE_SUFFIXES and all("\u4e00" <= ch <= "\u9fff" for ch in piece):
                found.append(piece)
    return found


# 通用说法，不是具体景点（攻略里满篇"老城""新区"，抽进来是噪声）
GENERIC_NAMES = {"老城", "新区", "市区", "城区", "老街", "广场", "公园", "古城", "古镇",
                 "车站", "机场", "景点", "博物馆", "遗址", "植物园", "小吃街"}


def extract_places(docs: list[DocChunk], province: str, city: str) -> list[CatalogPlace]:
    """目录里没有这个城市时，从召回的攻略正文里把地点名抠出来当候选点。

    规则很笨——按地名后缀切窗口，再用三条判据过滤——但胜在确定、可解释、离线可用。
    以后接了地理编码工具，这条路可以换成"让模型抽、工具补坐标"。

    抽出来的点**没有坐标**（攻略里不写经纬度），可达性校验会跳过并在 issues 里标注。
    坐标得靠地理编码工具（高德 BYOK）或行政区划中心点数据集来补 —— 这两样手上都没有时不编。
    """
    counts: dict[str, int] = {}
    starter_intro: dict[str, str] = {}     # 以它开头的那句（首选做介绍）
    first_mention: dict[str, str] = {}     # 退而求其次：第一次提到它的那句
    sentence_starters: set[str] = set()
    defined: set[str] = set()              # 出现过"X在…/X是…"这种介绍句式
    for chunk in docs:
        for sentence in split_sentences(chunk.text):
            for name in name_candidates(sentence):
                counts[name] = counts.get(name, 0) + 1
                first_mention.setdefault(name, sentence)
                if sentence.startswith(name):
                    sentence_starters.add(name)
                    starter_intro.setdefault(name, sentence)
                    if sentence[len(name):len(name) + 1] in INTRO_TAILS:
                        defined.add(name)

    # 判据（缺一条都会混进垃圾）：
    #   出现 ≥2 次            —— 只提一次的多半是误抽
    #   当过句首              —— 景点介绍通常是"龙门石窟在洛阳南郊…"
    #   有介绍句式 或 出现≥3次 —— 让"十字街人多"这种没介绍句式的也能留下
    #   不是"多切了一刀"的     —— "龙门石窟门"（把"门票"的门吃进来了）：它有前缀
    #                            "龙门石窟"，而"龙门石窟"本身就是被正介绍过的地名
    #   不是通用说法；长的优先、被长的包含的丢掉
    def over_cut(name: str) -> bool:
        return any(other != name and name.startswith(other) and other in defined
                   and counts[other] >= counts[name] for other in counts)

    kept: list[str] = []
    for name, count in sorted(counts.items(), key=lambda item: (-len(item[0]), -item[1])):
        if count < 2 or name not in sentence_starters:
            continue
        if name not in defined and count < 3:
            continue
        if over_cut(name) or name in GENERIC_NAMES:
            continue
        if any(name != other and name in other for other in kept):
            continue
        kept.append(name)
    kept = kept[:8]        # 一篇文章抽出来太多反而是噪声

    return [CatalogPlace(
        place_id="guide-" + hashlib.md5(name.encode("utf-8")).hexdigest()[:10],
        province=province,
        city=city,
        name=name,
        duration_min=120,
        open_hours="全天",       # 攻略里的开放时间没法可靠解析，按不限制处理
        intro=starter_intro.get(name) or first_mention.get(name, ""),
    ) for name in kept]


def build_context(request) -> PlanContext:
    """需求 → 装配结果。request 是接口层的请求对象，这里只按属性取用。

    候选点的来源分两种，**目录不是准入条件**：
      目录里这个城市有点 → 用目录的（有坐标、开放时间、常规时长，可达性算得出来）
      目录里没有         → 从召回的攻略正文里抽（没坐标，可达性会跳过并标注）
    两条都没有才真排不了 —— 那是缺数据，不是用户填错了。
    """
    province = request.destination.province
    city = request.destination.city

    # 召回攻略：既是编排的原料，也是"目录没覆盖时"的候选来源。
    # 召回失败不能让整条规划挂掉（降级不失败），记下原因按"没有攻略"处理。
    docs: list[DocChunk] = []
    recall_error: str | None = None
    try:
        keywords = compose_keywords(request.prefs.themes, request.destination.must_visit,
                                    request.destination.center_place, request.remarks)
        docs = retrieve(city, keywords)
    except Exception as error:
        logger.exception("攻略召回失败")
        recall_error = str(error)

    from_catalog = candidates_of(province, city)
    candidates = from_catalog or extract_places(docs, province, city)
    if not candidates:
        raise CityNotFound(
            f"「{province}{city}」既不在目的地目录里，也没有攻略语料，暂时排不出行程 —— "
            f"先在 Travele/ingest/corpus 放一篇这个城市的攻略（再用上传页入库），或补进 destinations.json")

    matched: list[CatalogPlace] = []
    unmatched: list[str] = []
    for name in request.destination.must_visit:
        place = match_place(candidates, name)
        if place is None:
            unmatched.append(name)
        elif place not in matched:
            matched.append(place)

    return PlanContext(
        province=province,
        city=city,
        departure_city=request.user.departure_city or "",
        dates=expand_dates(request.schedule.trip_start, request.schedule.trip_end),
        adults=request.user.adults,
        children=request.user.children,
        is_self_driving=request.is_self_driving,
        must_visit=list(request.destination.must_visit),
        must_visit_places=matched,
        unmatched_must_visit=unmatched,
        themes=list(request.prefs.themes),
        avoid=list(request.prefs.avoid),
        remarks=request.remarks,
        candidates=candidates,
        candidates_from="catalog" if from_catalog else "guides",
        docs=docs,
        recall_error=recall_error,
        memory=memory.memory_text(),        # 长期记忆：库里没有就空串，提示词那边会写"（暂无记忆）"
    )


# ==========================================================================
# 三、生成：plan 主入口，外加它要用到的距离／交通／时段换算
# ==========================================================================
def km_between(one, other) -> float | None:
    """两点直线距离（公里）。**任一点没有坐标就返回 None** —— 算不出来就说算不出来，不猜。

    城市内用它估交通耗时；接了地理编码工具后，这里换成路网距离即可，别处不用改。
    """
    if one.lat is None or one.lng is None or other.lat is None or other.lng is None:
        return None
    radius = 6371.0
    lat1, lat2 = math.radians(one.lat), math.radians(other.lat)
    delta_lat = lat2 - lat1
    delta_lng = math.radians(other.lng - one.lng)
    inner = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(inner))


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def minutes_of(text: str) -> int:
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def open_window(place: CatalogPlace) -> tuple[int, int] | None:
    """"HH:MM-HH:MM" → 分钟区间；"全天"或写法不认识都当没有限制。"""
    if "-" not in place.open_hours:
        return None
    start, end = place.open_hours.split("-", 1)
    try:
        return minutes_of(start.strip()), minutes_of(end.strip())
    except ValueError:
        return None


def fits_open_hours(place: CatalogPlace, start: int, duration: int) -> bool:
    window = open_window(place)
    return window is None or (start >= window[0] and start + duration <= window[1])


# 压到比这个还短就不像"去玩"了 —— 宁可留着让校验报出来
MIN_VISIT_MINUTES = 45


def _fit_open_hours(place: CatalogPlace, start: int, duration: int,
                    earliest: int) -> tuple[int, int]:
    """把一段活动修进开放时间里。返回修好的 (开始, 时长)。

    两步，都不改"去哪"：① 结束超出闭馆就整体往前挪（不早于 earliest）；
    ② 挪不动（前面就顶着上一段）就把游览时间压到刚好放得下，压到 MIN_VISIT_MINUTES 以下才放弃。
    """
    window = open_window(place)
    if window is None:
        return start, duration
    open_at, close_at = window
    if start >= open_at and start + duration <= close_at:
        return start, duration

    # ① 往前挪：让开始时间不早于"最早能开始"和开门时间
    latest_fit = close_at - duration
    start = max(earliest, open_at, min(start, latest_fit))
    if start + duration <= close_at:
        return start, duration

    # ② 压时长：还剩多少就是多少
    room = close_at - start
    if room >= MIN_VISIT_MINUTES:
        return start, room
    return start, duration


def ride_minutes(km: float, method: str) -> int:
    return max(1, round(km / SPEED_KMH[method] * 60))


def _road_facts(origin: CatalogPlace | None, target: CatalogPlace, need_ride: bool):
    """一段路要用到的事实：直线距离（公里）+ 需要时取高德的路网数据。

    origin 为 None（行程第一个点）或没有坐标 → 返回 None：这段本来就没有交通，不是"没查到"。
    """
    if origin is None:
        return None
    km = km_between(origin, target)
    if km is None:
        return None
    ride = None
    if need_ride:
        ride = tool.amap_ride.invoke({"origin": (origin.lat, origin.lng),
                                      "target": (target.lat, target.lng)})
    return km, ride


def option_for(method, km: float, ride, is_self_driving: bool) -> TransportOption | None:
    """按**指定的**交通方式算一条方案（模型点名时走这里）。

    步行/骑行/公交用距离和速度表就能算；打车/自驾的耗时距离用高德的。
    算不出来就返回 None：不认识这种方式（比如地铁），或者用户不是自驾却要自驾。
    """
    value = getattr(method, "value", str(method))
    if value == "walk":
        return TransportOption(method=TransportMethod.walk, duration_min=ride_minutes(km, "walk"),
                               price=0, recommend_reason="走过去")
    if value == "bike":
        return TransportOption(method=TransportMethod.bike, duration_min=ride_minutes(km, "bike"),
                               price=max(1, round(km / SPEED_KMH["bike"] * 2 * 1.5)),
                               recommend_reason="骑行比走路快，费用低")
    if value == "bus":
        return TransportOption(method=TransportMethod.bus, duration_min=ride_minutes(km, "bus"),
                               price=2, recommend_reason="便宜，但要等车")
    if value == "taxi" and ride is not None:
        return TransportOption(method=TransportMethod.taxi, duration_min=ride.duration_min,
                               price=int(13 + max(0.0, ride.distance_km - 3) * 2.6),
                               recommend_reason="价格与速度平衡")
    if value == "drive" and ride is not None and is_self_driving:
        return TransportOption(method=TransportMethod.drive, duration_min=ride.duration_min,
                               price=max(1, round(ride.distance_km * 0.8)),
                               recommend_reason="自驾，注意景区停车")
    return None


def transport_options(origin: CatalogPlace | None, target: CatalogPlace, is_self_driving: bool) -> list[TransportOption]:
    """**默认候选集**：按距离挑几种摆出来给用户选（模型没点名的时候用这个）。

    origin 为 None 表示这是行程里的第一个点（不知道用户从哪出发），那就**不给交通**。

    打车与自驾的距离、耗时来自高德（`tool.amap_ride`）—— 查不到就抛错、由上层报错。
    步行 / 骑行 / 公交用直线距离估算（走路骑车跟路网绕行关系不大，公交没有单独的数据源）。
    价格一律是估的：高德不给票价。
    """
    facts = _road_facts(origin, target, need_ride=True)     # 默认集里一定有打车，所以要高德
    if facts is None:
        return []
    km, ride = facts

    methods = []
    if km <= 1.5:
        methods.append("walk")
    if km <= 8:
        methods.append("bike")
    methods.append("taxi")
    if ride.distance_km > 4:
        methods.append("bus")
    if is_self_driving:
        methods.append("drive")

    options = [option for option in (option_for(method, km, ride, is_self_driving)
                                     for method in methods) if option is not None]
    for index, option in enumerate(options):
        option.recommended = index == 0      # 第一个就是默认推荐的
    return options


def to_poi(place: CatalogPlace) -> Poi:
    return Poi(poi_id=place.place_id, name=place.name, poi_type=place.poi_type,
               rating=place.rating, address=place.address, lat=place.lat, lng=place.lng,
               season_best=place.season_best, photo=place.photo)


def citation_for(place: CatalogPlace, docs: list[DocChunk]) -> SourceRef | None:
    """在召回的攻略里找提到这个地点的那一块，作为溯源出处。

    先按全名找（"洱海生态廊道"），找不到再退一步按前两个字找（"洱海"）。
    都找不到就返回 None —— 宁可不给出处，也不编一个出处。
    """
    for chunk in docs:
        if place.name and place.name in chunk.text:
            return SourceRef(doc=chunk.doc, chunk_index=chunk.chunk_index)

    short = place.name[:2] if place.name else ""
    if len(short) >= 2:
        for chunk in docs:
            if short in chunk.text:
                return SourceRef(doc=chunk.doc, chunk_index=chunk.chunk_index)
    return None


def plan(request, trip_id: str, emit: Callable[[str, str], None] | None = None) -> TripDetail:
    """规划主入口：装配 → 生成 → 校验。

    生成只有一条路：让模型排（agent.make_draft，Agent 模式不行就退到单次生成）。
    两条都不成，抛 GenerationFailed 让用户重试 —— 不拿一份机器凑的行程顶上。

    emit 是进度回调（接口层用它喂 SSE）。这里只发"进行中"的事件；
    结尾的 done / failed 由接口层在**结果存好之后**发，保证前端收到 done 就能取到行程。
    """
    notify = emit or (lambda event, message: None)

    notify("searching", "正在查这个城市的资料…")
    context = build_context(request)
    # 进度条上只说人话；技术细节（召回失败原因、候选点从哪来）一律记日志，不推给用户
    if context.recall_error:
        logger.warning("攻略召回失败：%s", context.recall_error)
        notify("searching", "攻略资料没查到，先按已有资料安排")
    elif context.docs:
        notify("searching", f"找到 {len(context.docs)} 篇相关攻略")
    else:
        notify("searching", "暂时没有这个城市的攻略，先按已有资料安排")
    if context.candidates_from == "guides":
        logger.info("目的地目录没有覆盖「%s」，候选点改从攻略正文里抽", context.city)

    notify("drafting", "正在安排每天的行程…")
    try:
        draft, remaining = agent.make_draft(context, request, notify)
    except agent.PlanTimeout as error:
        # 超时和"排不出来"要分开说：前者是等太久了，后者是模型没排好
        logger.warning("创建超时：%s", error)
        raise GenerationFailed("创建超时：3 分钟内没排完，请稍后再试") from error
    if draft is None:
        raise GenerationFailed("AI 这次没能排出行程，请稍后再试")

    trip, problems = to_trip_detail(draft, context, trip_id)
    if not any(item for day in trip.days or []
               for segment in day.segments for item in segment.items):
        # 草稿里一个能用的地点都没有，等于没生成出来，不拿一张空行程糊弄用户
        raise GenerationFailed("AI 排的行程里没有可用的地点，请稍后再试")

    notify("validating", "正在检查行程合不合理…")
    # problems 是"后端已经自己处理掉的事"（编的酒店被剔除、要的交通方式没有改用别的…），
    # 只当说明给用户看；真正决定"校验过没过"和要不要回喂的，只有 validate() 的规则问题。
    issues = validate(trip, context)
    notes = list(problems)

    # 校验不过就**回喂一轮**：把问题清单连同上一版草稿还给模型，让它只改冲突的地方。
    # 只回喂一次（不做多轮），而且只有确实比原来好的那一版才采纳；时间不够就不试。
    if issues and remaining > agent.MIN_REVISION_SECONDS:
        notify("revising", f"发现 {len(issues)} 处不太合理，正在调整…")
        better = None
        try:
            revised = agent.revise_draft(context, draft, issues, remaining)
            if revised is not None:
                better, better_problems = to_trip_detail(revised, context, trip_id)
                better_issues = validate(better, context)
                if not any(item for day in better.days or []
                           for segment in day.segments for item in segment.items):
                    better = None                      # 改完空掉了，不采纳
                elif len(better_issues) < len(issues):
                    logger.info("回喂重排：问题 %d 条 → %d 条", len(issues), len(better_issues))
                    trip, issues, notes = better, better_issues, list(better_problems)
                else:
                    logger.info("回喂重排没好多少（%d 条 → %d 条），保留原来那版",
                                len(issues), len(better_issues))
                    better = None
        except Exception:
            logger.exception("回喂重排没成，保留原来那版")
            better = None
        if better is None:
            notify("revising", "这几处没能调好，先按现在这版给你")

    trip.plan_meta.validated = not issues
    trip.plan_meta.issues = issues
    trip.plan_meta.notes = notes
    trip.plan_meta.source_docs = list(dict.fromkeys(chunk.doc for chunk in context.docs))
    return trip


# ==========================================================================
# 三点五、草稿 → 成品（① Agent / ② 单次生成的产出都要过这里）
#
# 原则：模型只填"编排"，事实由这里补齐 —— 坐标、规范名、交通耗时价格、时段。
# 模型提到的地点对不上目录的，直接剔除并记一条问题（幻觉防线）。
# ==========================================================================
def resolve_place(draft_poi, by_id: dict[str, CatalogPlace],
                  candidates: list[CatalogPlace]) -> CatalogPlace | None:
    """把草稿里的地点对到目录上：poi_id 优先，其次按名字（支持包含匹配）。"""
    if draft_poi is None:
        return None
    if draft_poi.poi_id and draft_poi.poi_id in by_id:
        return by_id[draft_poi.poi_id]
    if draft_poi.name:
        return match_place(candidates, draft_poi.name)
    return None


def merge_transport(draft_transports, origin: CatalogPlace | None, place: CatalogPlace,
                    is_self_driving: bool) -> tuple[list[TransportOption], str | None]:
    """
    模型点名的交通方式，**逐条按事实算出来**（耗时和价格一律由后端算，模型给的数字忽略）。

    算得出来就给（步行/骑行/公交用距离和速度表，打车/自驾用高德）；算不出来就返回那些方式的名字，
    由调用方汇总成一条说明（"地铁、船这类没有数据源的，已按能用的方式安排"）——不在这一层逐条写，
    否则一版行程里同一句话会出现六七次。返回 (交通方案, 给不出来的方式们)。
    """
    if not draft_transports:
        return transport_options(origin, place, is_self_driving), None      # 没点名：给默认候选集

    values = [getattr(draft.method, "value", str(draft.method)) for draft in draft_transports]
    facts = _road_facts(origin, place, need_ride=bool({"taxi", "drive"} & set(values)))
    if facts is None:
        return [], None        # 第一个点或没有坐标：这段本来就没有交通，不算"没能给出"
    km, ride = facts

    merged: list[TransportOption] = []
    dropped: list[str] = []
    for draft, value in zip(draft_transports, values):
        option = option_for(draft.method, km, ride, is_self_driving)
        if option is None:
            dropped.append(TRANSPORT_TEXT.get(value, value))    # 用户看中文，不给枚举值
            continue
        option.recommend_reason = draft.recommend_reason or option.recommend_reason
        option.recommended = draft.recommended
        merged.append(option)

    note: str | None = None
    if dropped:
        # 只记"哪个方式给不出来"，**不带地点名**：一版行程里同一种情况可能出现六七次，
        # 逐条写出来会把说明区刷屏（实测过），所以交给调用方按方式汇总成一条。
        note = "、".join(sorted(set(dropped)))

    if merged and not any(option.recommended for option in merged):
        merged[0].recommended = True
    return merged, note


def resolve_intercity(drafts) -> list[IntercityLeg]:
    """草稿里的大交通 → 成品。

    这一版**只有结构校验**：时刻和班次是模型给的，所以 verified 一律 false，前端显示"未核对"。
    核对要查 12306 / 航班，在个人项目里做不到"完美"（机票只有实时航班、没有票价来源），
    所以**有意不做核对**，如实标注比假装准确好。

    机票票价单独处理：**没有可靠来源，就不要模型猜的数**（price 置空），前端会注明这一点。
    火车/自驾这些的价格也仍是模型给的参考，同样带"未核对"标注。
    """
    return [IntercityLeg(direction=item.direction, method=item.method, from_place=item.from_place,
                         to_place=item.to_place, date=item.date, depart=item.depart,
                         arrive=item.arrive, detail=item.detail,
                         price=None if item.method is TransportMethod.plane else item.price)
            for item in drafts]


def resolve_lodging(drafts, context: PlanContext) -> tuple[list[Lodging], list[str]]:
    """草稿里的住宿 → 成品。返回 (住宿, 问题清单)。

    **模型只挑名字**：地址、星级、价格、预订链接一律拿 hotels 工具查到的真数据补，
    名字对不上查回来的酒店就剔除并记账 —— 跟"编造的景点被剔除"是一个道理。
    查不到酒店不算规划失败（住宿是选配），如实记一条问题就行。
    """
    if not drafts:
        return [], []

    problems: list[str] = []
    check_in = drafts[0].check_in or context.dates[0]
    check_out = drafts[0].check_out or context.dates[-1] + datetime.timedelta(days=1)
    try:
        offers = tool.hotels.invoke({"city": context.city, "check_in": check_in,
                                     "check_out": check_out, "adults": max(1, context.adults)})
    except tool.SourceError as error:
        logger.warning("住宿这次没查成：%s", error)
        return [], [f"这次没能查到 {context.city} 的酒店，住宿没排进行程"]

    lodging: list[Lodging] = []
    for draft in drafts:
        head = draft.name.strip()
        offer = next((item for item in offers if item.name == head), None)
        if offer is None and len(head) >= 4:      # 名字可能多个括号后缀，退一步按前几个字认
            offer = next((item for item in offers if head[:4] in item.name), None)
        if offer is None:
            problems.append(f"模型说的酒店「{draft.name}」不在查到的酒店里，已剔除")
            continue
        lodging.append(Lodging(name=offer.name, address=offer.address, star_rating=offer.star_rating,
                               price_per_night=offer.price_per_night,
                               check_in=draft.check_in or check_in,
                               check_out=draft.check_out or check_out,
                               booking_url=offer.booking_url, reason=draft.reason))
    return lodging, problems


def to_trip_detail(draft: TripDraft, context: PlanContext,
                   trip_id: str) -> tuple[TripDetail, list[str]]:
    """草稿 → 成品。返回 (行程, 转换过程中发现的问题)。

    模型负责"编排"，这里负责"事实"：坐标与规范名按目录补、交通耗时价格按距离算、
    时段缺了就按前后顺序推、日期按请求的日期区间对齐。
    """
    by_id = {place.place_id: place for place in context.candidates}
    problems: list[str] = []
    days: list[DayPlan] = []
    previous: CatalogPlace | None = None
    missing_methods: set[str] = set()      # 模型点了名、这边给不出耗时的交通方式（最后汇总成一条说明）
    trip_used: dict[str, int] = {}         # 整趟行程每个点排了几次（去重用）
    dropped_repeats: list[str] = []        # 因为重复被去掉的点名（最后汇总成一条说明）
    dropped_late: list[str] = []           # 因为排到后半夜被去掉的点名（同上）

    # 去程几点落地：第一天不许排在那之前（"12:40 才到、第一天 09:00 就开始玩"是硬矛盾）。
    # 跟开放时间一个待遇 —— 这是事实冲突，后端自己修，不留给用户看一条红字。
    arrival: int | None = None
    outbound = next((leg for leg in draft.intercity
                     if str(getattr(leg.direction, "value", leg.direction)) == "outbound"), None)
    if outbound is not None and _looks_like_hhmm(outbound.arrive or ""):
        arrival = minutes_of(outbound.arrive)

    if len(draft.days) != len(context.dates):
        problems.append(f"模型给了 {len(draft.days)} 天，请求的日期区间是 {len(context.dates)} 天，缺的按空天补齐")

    # 天数按**请求的日期区间**来（这是事实），草稿只负责往里填内容：
    # 草稿少给的天就是空天（问题清单里会写明），多给的天直接忽略。
    # 这样 days 永远不为空，也不会出现"草稿为空 → 构造行程就炸"的情况。
    for index, date in enumerate(context.dates, start=1):
        draft_day = draft.days[index - 1] if index <= len(draft.days) else None
        clock = SEGMENT_START[Slot.morning]
        segments: list[Segment] = []
        day_used: set[str] = set()         # 这一天已经排过的点（同一天不排两次）

        for slot in SEGMENT_ORDER:
            draft_segment = None
            if draft_day is not None:
                draft_segment = next((s for s in draft_day.segments if s.slot == slot), None)
            items: list[ActivityItem] = []

            # 第一天的时段如果整个落在落地时间之前（或刚好卡在它上面），那一段就排不进去：
            # 人还在路上，排出来只会得到"上午 12:09 开始"这种跟时段对不上的安排。
            if arrival is not None and index == 1 and arrival >= SLOT_WINDOW[slot][1] and draft_segment:
                if draft_segment and draft_segment.items:
                    problems.append(f"去程 {outbound.arrive} 才到，第一天的{SLOT_LABEL[slot]}已经空出来，"
                                    f"原来排在那儿的安排去掉了")
                draft_segment = None

            for draft_item in (draft_segment.items if draft_segment else []):
                resolved: list[tuple[CatalogPlace, ActivityOption]] = []

                for draft_option in draft_item.options:
                    place = resolve_place(draft_option.poi, by_id, context.candidates)
                    if place is None:
                        problems.append(
                            f"模型提到的「{draft_option.poi.poi_id or draft_option.poi.name}」"
                            f"不在 {context.province}{context.city} 的目的地目录里，已剔除")
                        continue
                    combination = resolve_place(draft_option.combination_poi, by_id, context.candidates)
                    transports, missing = merge_transport(draft_option.transport_options,
                                                          previous, place, context.is_self_driving)
                    if missing:
                        missing_methods.add(missing)
                    resolved.append((place, ActivityOption(
                        poi=to_poi(place),
                        duration_min=(draft_option.duration_min
                                      if draft_option.duration_min is not None else place.duration_min),
                        transport_options=transports,
                        combination_poi=to_poi(combination) if combination else None,
                    )))

                if not resolved:
                    continue        # 全是编的地点，这个节点就别留了

                head_place, head_option = resolved[0]
                # 同一天同一个点排两次、或者整趟排到第三次以上：直接去掉这一段（记一条说明）。
                # 宁可空一段，也不要"同一个地方来回转"凑满 -- 这是实测最常见的凑数方式。
                if head_place.place_id in day_used or trip_used.get(head_place.place_id, 0) >= MAX_PLACE_REPEAT:
                    dropped_repeats.append(head_place.name)
                    continue
                day_used.add(head_place.place_id)
                trip_used[head_place.place_id] = trip_used.get(head_place.place_id, 0) + 1
                travel = head_option.transport_options[0].duration_min if head_option.transport_options else 0
                # 第一天还要卡在落地时间之后（人到了才排得动）
                floor = max(clock + travel, SLOT_WINDOW[slot][0],
                            arrival if (arrival is not None and index == 1) else 0)

                if draft_item.time_range is not None:
                    # 模型给了时刻就先用它的**时长**，但起点要往后挪到位：
                    #   · 不能和上一段撞车（撞了说明路上时间不够）
                    #   · 不能跑到这个时段之外（实跑出现过"下午 11:00 开始"）
                    # 只往后挪、不往前挪 —— 它可能是故意早起看日出，别把 06:30 推成 09:00。
                    begin = minutes_of(draft_item.time_range.start)
                    duration = max(0, minutes_of(draft_item.time_range.end) - begin)
                    start = max(begin, floor)
                else:
                    # 模型没给时刻：按"上一段结束 + 路上时间"推，再和这个时段的常规起点取晚的那个
                    duration = head_option.duration_min
                    start = max(clock, SEGMENT_START[slot]) + travel

                # 开放时间自己修（跟"时段撞车"一个待遇，别为了这种事把整份行程判成不通过）：
                # 结束时间超出闭馆就往前挪到"刚好放得下"；挪不动就压缩游览时间，压到不像话才留着报错。
                start, duration = _fit_open_hours(head_place, start, duration, earliest=floor)

                # 一天只排到 23:59：前一段收得晚 + 路上时间，很容易把它顶到"24:32"这种
                # 不存在的时刻（时间格式本来也不允许，实测直接把整单规划打崩了）。
                # 能压就压到 23:59，压不出像样的一段就整段去掉 —— 宁可这天少一段。
                if start > DAY_LAST_MINUTE or DAY_LAST_MINUTE - start < MIN_VISIT_MINUTES:
                    dropped_late.append(head_place.name)
                    continue
                if start + duration > DAY_LAST_MINUTE:
                    duration = DAY_LAST_MINUTE - start

                time_range = TimeRange(start=hhmm(start), end=hhmm(start + duration))

                items.append(ActivityItem(seq=len(items) + 1,
                                          options=[option for _, option in resolved],
                                          time_range=time_range,
                                          unverified=list(draft_item.unverified),
                                          source_ref=(draft_item.source_ref
                                                      or citation_for(head_place, context.docs))))
                clock = minutes_of(time_range.end)
                previous = head_place

            segments.append(Segment(slot=slot, items=items))

        days.append(DayPlan(day_index=index, date=date,
                            theme=draft_day.theme if draft_day is not None else "",
                            segments=segments))

    budget = None
    if draft.budget is not None and draft.budget.total_estimate is not None:
        budget = Budget(total_estimate=draft.budget.total_estimate, currency="CNY", estimate=True)

    if missing_methods:
        names = "、".join(sorted(missing_methods))
        problems.append(f"有几段你点了 {names}，这边没有能算这种方式的耗时，已按能用的方式安排")

    if dropped_repeats:
        names = "、".join(sorted(set(dropped_repeats)))
        problems.append(f"「{names}」本来被排了不止一次，重复的那几段去掉了 —— 宁可空一段，也别来回转")

    if dropped_late:
        names = "、".join(sorted(set(dropped_late)))
        problems.append(f"「{names}」排到了半夜（一天只排到 23:59），那一段去掉了")

    lodging, lodging_problems = resolve_lodging(draft.lodging, context)
    problems.extend(lodging_problems)

    trip = TripDetail(
        trip_id=trip_id,
        status=TripStatus.completed,
        title=draft.title or f"{context.province}{context.city} {len(days)} 日行程",
        plan_meta=PlanMeta(validated=True, issues=[], source_docs=[]),
        destination=TripDestination(province=context.province, city=context.city),
        budget=budget,
        intercity=resolve_intercity(draft.intercity),
        lodging=lodging,
        days=days,
        created_at=datetime.datetime.now().astimezone(),
    )
    return trip, problems


# ==========================================================================
# 四、校验：10 类确定性规则（不靠模型）
# ==========================================================================
def validate(trip: TripDetail, context: PlanContext) -> list[str]:
    """逐条检查，返回硬问题清单（空列表 = 通过）。问题原文会进 planMeta.issues 给用户看。"""
    #报错传回agent重试，而不是给用户看
    issues: list[str] = []
    known = {place.place_id: place for place in context.candidates}
    placed_ids: set[str] = set()
    missing_coords: set[str] = set()
    trip_places: dict[str, int] = {}        # 整趟行程每个地点排了几次（规则13 用）

    for day in trip.days or []:
        # 规则1：结构完整——三个时段都得在（说人话：上午/下午/晚上，不给用户看 morning 这种枚举值）
        present = {segment.slot for segment in day.segments}
        missing = [SLOT_LABEL[slot] for slot in SEGMENT_ORDER if slot not in present]
        if missing:
            issues.append(f"第 {day.day_index} 天缺时段：{'、'.join(missing)}")

        # 规则11：时段不能只是"在"，里面得有内容 —— 实测最常见的差评就是"一天只剩一个时段有事"。
        # 第一天可能在赶路（上午才落地）、最后一天可能要返程，所以首末天允许少一段；
        # 中间的天要求至少 MIN_FILLED_SLOTS 段有安排，不达标就把问题回喂给模型重排。
        filled = [segment for segment in day.segments if segment.items]
        required = 1 if day.day_index in (1, len(trip.days or [])) else MIN_FILLED_SLOTS
        if len(filled) < required:
            empty = [SLOT_LABEL[segment.slot] for segment in day.segments if not segment.items]
            if filled:
                issues.append(f"第 {day.day_index} 天只有{'、'.join(SLOT_LABEL[s.slot] for s in filled)}有安排，"
                              f"{'、'.join(empty)}都空着 —— 这一天太空了")
            else:
                issues.append(f"第 {day.day_index} 天整天都没有安排")

        previous_end: int | None = None
        day_places: dict[str, str] = {}      # 这一天已经排过的地点：poi_id -> 时段

        for segment in day.segments:
            for item in segment.items:
                head = item.options[0]

                # 规则4：地点真实性——目录里没有的点不允许出现
                for option in item.options:
                    if option.poi.poi_id not in known:
                        issues.append(
                            f"第 {day.day_index} 天「{option.poi.name}」不在 {context.province}{context.city} 的候选点里")
                    placed_ids.add(option.poi.poi_id)
                    if option.poi.lat is None or option.poi.lng is None:
                        missing_coords.add(option.poi.name)

                # 规则12：同一天同一个地点最多排一次（重复排会显得在凑数）
                first_slot = day_places.get(head.poi.poi_id)
                if first_slot is None:
                    day_places[head.poi.poi_id] = SLOT_LABEL[segment.slot]
                else:
                    issues.append(f"第 {day.day_index} 天「{head.poi.name}」排了两次"
                                  f"（{first_slot}和{SLOT_LABEL[segment.slot]}），一天里去一次就够")
                trip_places[head.poi.poi_id] = trip_places.get(head.poi.poi_id, 0) + 1

                if item.time_range is None:
                    continue
                start = minutes_of(item.time_range.start)
                end = minutes_of(item.time_range.end)

                # 规则2：时间连续性——结束必须晚于开始，且不许和上一段重叠
                if end <= start:
                    issues.append(f"第 {day.day_index} 天「{head.poi.name}」的结束时间不晚于开始时间")
                if previous_end is not None and start < previous_end:
                    issues.append(f"第 {day.day_index} 天「{head.poi.name}」和上一段时间重叠了")

                # 规则6：营业时间——活动要落在开放窗口内
                place = known.get(head.poi.poi_id)
                if place is not None and not fits_open_hours(place, start, end - start):
                    issues.append(
                        f"第 {day.day_index} 天「{place.name}」{item.time_range.start} 开始，超出它的开放时间 {place.open_hours}")

                # 规则5：可达性——留出来的时间够不够路上用。
                # 只看默认方案（options[0]）：时段是后端按它排的；备选方案的时间差属于
                # "前端切换时要重算时段"的问题，不该记成整张行程的硬错误。
                # （原来还有一条"速度快得不可能"的检查，因为耗时已经不由模型给、全部由后端
                #   按距离算，那种数字不可能再出现，所以删掉了。）
                if previous_end is not None:
                    gap = start - previous_end
                    fastest = min((t.duration_min for t in head.transport_options), default=0)
                    if fastest and gap < fastest:
                        issues.append(
                            f"第 {day.day_index} 天「{head.poi.name}」前只留了 {gap} 分钟，路上最快也要 {fastest} 分钟")

                previous_end = end

    # 规则13：整趟行程里同一个地点最多排 2 次 —— 兜底（后端已经先去重了一遍，
    # 这里防的是"去重之后又通过别的路径冒出来"）
    for poi_id, count in trip_places.items():
        if count > MAX_PLACE_REPEAT:
            name = known[poi_id].name if poi_id in known else poi_id
            issues.append(f"「{name}」整趟排了 {count} 次，太多了 —— 候选点还有别的可以用")

    # 坐标缺失：不是"违规"，但必须让用户知道这次可达性没校验（标注局限 > 悄悄放过）
    if missing_coords:
        names = sorted(missing_coords)
        shown = "、".join(names[:3]) + (f" 等 {len(names)} 个" if len(names) > 3 else "")
        issues.append(f"「{shown}」没有坐标，可达性这次没校验 —— 这些点是从攻略正文里抽的，"
                      f"要算距离得先接地理编码工具")

    # 规则7：天数要和请求的日期区间对得上
    expected_days = len(context.dates)
    if len(trip.days or []) != expected_days:
        issues.append(f"行程排了 {len(trip.days or [])} 天，请求的日期区间是 {expected_days} 天")

    # 规则3：必去点覆盖
    for place in context.must_visit_places:
        if place.place_id not in placed_ids:
            issues.append(f"必去地点「{place.name}」没有排进行程")
    for name in context.unmatched_must_visit:
        issues.append(f"必去地点「{name}」在目的地目录里找不到，没法排入")

    # 规则8：住宿要盖住整个行程（第一晚到最后一天）
    for item in trip.lodging:
        if item.check_in is not None and item.check_in > context.dates[0]:
            issues.append(f"住宿从 {item.check_in} 才开始，第一晚（{context.dates[0]}）没着落")
        if item.check_out is not None and item.check_out < context.dates[-1]:
            issues.append(f"住宿到 {item.check_out} 就结束了，最后一天（{context.dates[-1]}）没着落")

    # 规则9：大交通的日期与到达时间要跟行程对得上
    for leg in trip.intercity:
        if leg.date is None:
            continue
        if leg.direction == "outbound" and leg.date > context.dates[0]:
            issues.append(f"去程排在 {leg.date}，比行程第一天（{context.dates[0]}）还晚")
        if leg.direction == "inbound" and leg.date < context.dates[-1]:
            issues.append(f"回程排在 {leg.date}，比行程最后一天（{context.dates[-1]}）还早")

    outbound = next((leg for leg in trip.intercity if leg.direction == "outbound"), None)
    if outbound is not None and _looks_like_hhmm(outbound.arrive):
        first_start = _first_activity_start(trip)
        if first_start is not None and minutes_of(outbound.arrive) > first_start:
            issues.append(f"去程 {outbound.arrive} 才到，第一天 {hhmm(first_start)} 就开始排活动了")

    # 规则10：模型给的时刻不能跟时段拧着来（实测出现过"下午 11:00 开始"）
    for day in trip.days or []:
        for segment in day.segments:
            for item in segment.items:
                if item.time_range is None:
                    continue
                start = minutes_of(item.time_range.start)
                low, high = SLOT_WINDOW[segment.slot]
                if not low <= start <= high:
                    issues.append(f"第 {day.day_index} 天「{item.options[0].poi.name}」"
                                  f"排在{SLOT_LABEL[segment.slot]}的 {item.time_range.start}，"
                                  f"跟时段对不上")

    return issues


def _looks_like_hhmm(text: str) -> bool:
    """是不是"HH:MM"。大交通的时刻是模型给的，不能假定它一定给了能解析的格式。"""
    return (len(text) == 5 and text[2] == ":"
            and text[:2].isdigit() and text[3:].isdigit())


def _first_activity_start(trip: TripDetail) -> int | None:
    """第一天第一个带时间的活动，从 0 点算的分钟数；没有就 None。"""
    for day in trip.days or []:
        for segment in day.segments:
            for item in segment.items:
                if item.time_range is not None:
                    return minutes_of(item.time_range.start)
        return None
    return None


# ==========================================================================
# 五、按决策表合并（确认时用）
# ==========================================================================
def apply_decisions(trip: TripDetail, decisions: Iterable) -> TripDetail:
    """把用户的决策表合并进行程。

    先深拷贝再改：传入的那份保持"刚生成的样子"，所以确认可以重复调用、结果是幂等的。
    合并方式（按设计）：定位节点 → 把候选项收窄成用户选的那个 → 交通也收窄 → 锁上。
    """
    merged = trip.model_copy(deep=True)

    index: dict[tuple[int, str, int], ActivityItem] = {}
    for day in merged.days or []:
        for segment in day.segments:
            for item in segment.items:
                index[(day.day_index, segment.slot.value, item.seq)] = item

    for decision in decisions:
        key = (decision.day_index, decision.slot.value, decision.seq)
        node = index.get(key)
        if node is None:
            raise UnknownNode(
                f"行程里没有第 {decision.day_index} 天 {decision.slot.value} 段的第 {decision.seq} 个节点")

        if decision.chosen_poi_id:
            picked = [o for o in node.options if o.poi.poi_id == decision.chosen_poi_id]
            if not picked:
                raise UnknownChoice(f"这个节点上没有「{decision.chosen_poi_id}」这个方案")
            node.options = picked

        if decision.chosen_transport:
            keep = [t for t in node.options[0].transport_options
                    if t.method.value == decision.chosen_transport.value]
            if not keep:
                raise UnknownChoice(
                    f"方案「{node.options[0].poi.name}」没有 {decision.chosen_transport.value} 这种交通")
            node.options[0].transport_options = keep

        node.locked = True      # 用户确认过的节点就锁上

    return merged


# ==========================================================================
# 六、确认返回：各点介绍 + 出行建议
# ==========================================================================
def split_sentences(text: str) -> list[str]:
    """把攻略正文切成句子：挑实用提醒、找某地点的介绍，都要按句来。

    顺序不能反：先按换行切行、再按句号断句。攻略里"小标题"和正文常常各占一行，
    要是先按句号切、把换行抹掉，标题和正文就会粘成一句 ——
    介绍里会冒出"大理攻略（示例语料）一、总体大理在云南西部…"这种东西。
    """
    pieces: list[str] = []
    for line in text.replace("；", "。").replace(";", "。").split("\n"):
        for part in line.split("。"):
            sentence = part.strip().strip("，、 ")
            if 8 <= len(sentence) <= 90:
                pieces.append(sentence)
    return pieces


def guide_note(place_name: str, docs: list[DocChunk]) -> tuple[str, str] | None:
    """在召回的攻略里找**正面介绍**这个地点的那句话，返回 (句子, 出处)；找不到返回 None。

    都找不到就返回 None，让调用方退回目录里的一句话。
    """
    for chunk in docs:
        for sentence in split_sentences(chunk.text):
            if place_name and sentence.startswith(place_name):
                return sentence, chunk.doc
    for chunk in docs:
        for sentence in split_sentences(chunk.text):
            if place_name and place_name in sentence:
                return sentence, chunk.doc
    return None


def remember_trip(request, trip: TripDetail) -> None:
    """用户确认行程之后，把这次的选择记进长期记忆 —— **唯一的写入点**。

    记忆是锦上添花的东西：写不进去不能把用户的"确认"也搞失败，所以这里只记日志。
    （这是唯一一处"出错了不往上抛"的地方，因为它的失败跟用户要看的结果无关。）
    """
    try:
        memory.remember(request, trip)
    except Exception:
        logger.exception("长期记忆没写进去（不影响这次确认）")


def build_confirm(trip: TripDetail) -> tuple[list[dict], list[str]]:
    """确认之后的内容：各点介绍 + 出行建议 —— 交给**解说 Agent**写（模型生成）。

    分工跟规划一样：后端备事实（行程摘要、地点资料、攻略片段、天气），模型只写文字；
    **出处由后端按攻略正文匹配**，不让模型编。模型没写到的点就少返回一条（记日志），
    模型编了行程里没有的点就丢掉。生成不出来直接抛 ContentFailed —— 不退回规则版。
    """
    docs: list[DocChunk] = []
    try:
        docs = retrieve(trip.destination.city)
    except Exception as error:
        logger.warning("确认阶段召回攻略失败，介绍只用行程与目录资料：%s", error)

    plan, places, guides, weather = confirm_materials(trip, docs)
    try:
        draft = agent.make_confirm(plan, places, guides, weather)
    except Exception as error:
        logger.exception("解说生成失败")
        raise ContentFailed("生成各点介绍时出错了，请再试一次") from error

    by_id: dict[str, Poi] = {}
    for day in trip.days or []:
        for segment in day.segments:
            for item in segment.items:
                for option in item.options:
                    by_id.setdefault(option.poi.poi_id, option.poi)

    intros: list[dict] = []
    used: set[str] = set()
    invented: list[str] = []
    for entry in draft.spot_intros:
        poi = by_id.get(entry.poi_id)
        if poi is None:
            invented.append(entry.poi_id)
            continue
        used.add(poi.poi_id)
        note = guide_note(poi.name, docs)
        intros.append({"poi_id": poi.poi_id, "name": poi.name,
                       "intro": entry.intro.strip(),
                       # 出处：攻略里写到过这个点就写文件名；没写到就如实说事实来自目录
                       # （类型/评分/地址/开放时间都是目录给的，不是编的）。不写 null 是因为
                       # 前端那行"（出处：X）"是用户判断可信度的依据，空着反而像漏了。
                       "source_doc": note[1] if note else CATALOG_SOURCE_LABEL})

    if invented:
        logger.warning("解说里出现了行程里没有的点，已丢掉：%s", invented)
    missing = [poi.name for poi_id, poi in by_id.items() if poi_id not in used]
    if missing:
        logger.warning("解说漏了这些点：%s", missing)

    tips = [tip.strip() for tip in draft.travel_tips if tip.strip()]
    return intros, tips


def confirm_materials(trip: TripDetail, docs: list[DocChunk]) -> tuple[str, str, str, str]:
    """给解说 Agent 备料：行程摘要 / 地点资料 / 攻略片段 / 天气（全是事实，模型只写字）。

    天气现查（要坐标，用行程里第一个点的）。查不到就如实写"没查到"，不编。
    """
    catalog = {place.place_id: place for place in load_catalog()}
    plan_lines: list[str] = []
    place_lines: list[str] = []
    seen: set[str] = set()
    first_poi = None

    for day in trip.days or []:
        plan_lines.append(f"{day.date or ''}（第 {day.day_index} 天）"
                          + (f" 主题：{day.theme}" if day.theme else ""))
        for segment in day.segments:
            for item in segment.items:
                if not item.options:
                    continue
                option = item.options[0]
                poi = option.poi
                first_poi = first_poi or poi
                travel = option.transport_options[0] if option.transport_options else None
                travel_text = ""
                if travel is not None:
                    method = TRANSPORT_TEXT.get(getattr(travel.method, "value", ""),
                                                getattr(travel.method, "value", ""))
                    travel_text = f"，{method} {travel.duration_min} 分钟"
                when = (f"{item.time_range.start}-{item.time_range.end}"
                        if item.time_range else "（时间未定）")
                plan_lines.append(f"  {segment.slot_label} {when} {poi.name}"
                                  f"（游览 {option.duration_min} 分钟{travel_text}）")

                if poi.poi_id in seen:
                    continue
                seen.add(poi.poi_id)
                place = catalog.get(poi.poi_id)
                bits = [poi.poi_type or "", f"评分 {poi.rating}" if poi.rating else "",
                        f"地址 {poi.address}" if poi.address else "",
                        f"最佳季节 {place.season_best}" if place and place.season_best else "",
                        f"常规游览 {place.duration_min} 分钟" if place else ""]
                place_lines.append(f"{poi.poi_id}｜{poi.name}｜" + "｜".join(b for b in bits if b))

    if trip.lodging:
        plan_lines.append("住宿：" + "；".join(
            f"{item.name}（{item.check_in}→{item.check_out}，"
            f"{item.price_per_night} 元/晚）" if item.price_per_night
            else f"{item.name}（{item.check_in}→{item.check_out}）" for item in trip.lodging))
    for leg in trip.intercity:
        direction = "去程" if leg.direction == "outbound" else "回程"
        plan_lines.append(f"{direction}：{leg.from_place}→{leg.to_place} {leg.date or ''} "
                          f"{leg.depart or ''}-{leg.arrive or ''} {leg.detail}".strip())

    guides = "\n\n".join(f"[{chunk.doc} 第{chunk.chunk_index}块] {chunk.text}"
                         for chunk in docs) or f"没有找到 {trip.destination.city} 的攻略资料。"

    weather = "（这次没查到天气，不要编气温）"
    dates = [day.date for day in trip.days or [] if day.date]
    if first_poi is not None and first_poi.lat is not None and first_poi.lng is not None and dates:
        try:
            days = tool.weather.invoke({"lat": first_poi.lat, "lng": first_poi.lng,
                                        "start": dates[0], "end": dates[-1]})
            weather = "\n".join(f"{item.date} {item.summary} {item.low}~{item.high}℃ "
                                f"降雨概率 {item.rain_chance}%" for item in days)
        except tool.SourceError as error:
            logger.info("确认阶段没查到天气：%s", error)

    return "\n".join(plan_lines), "\n".join(place_lines), guides, weather
