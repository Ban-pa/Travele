"""外部数据源。给 Agent 用（`build_tools` 递过去）也后端自己调（`.invoke(...)`）。

每个工具都是三段式，不需要的那段就不写：
    传入数据处理 → 工具实现 → 返回数据处理
工具之间用 `=` 的横线隔开。
"""
import asyncio
import datetime
import json
import logging
import os
import pathlib
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx
import httpx2                       # MCP 官方 SDK 用的 HTTP 客户端（它带进来的依赖）
from dotenv import load_dotenv
from langchain_core.tools import tool
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger("travele")

ENV_FILE = pathlib.Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_FILE)          # 跟 agent.py 读同一份

TIMEOUT_SECONDS = 20.0         # 单个外部请求的上限（规范给的区间是 15~30 秒；6 秒实测会被 TLS 握手拖爆）
MCP_TIMEOUT_SECONDS = 30.0     # MCP 服务可能冷启动，还得连外面的服务
ATTEMPTS = 3                   # 连不上就退避重试
BACKOFF_SECONDS = 1.0          # 退避 1 秒 → 2 秒 → 4 秒
MIN_CALL_GAP_SECONDS = 0.4     # 两次调用之间至少隔这么久（个人 key 的 QPS 很小）
AMAP_BUSY_CODE = "10021"       # 高德：CUQPS_HAS_EXCEEDED_THE_LIMIT —— 问得太快，等一下就好


# ==========================================================================
# 公共：配置、错误、MCP 通道
# ==========================================================================
class SourceError(Exception):
    """查不到：没配、网络不通、被限额、对方接口改了。

    消息写全给排错的人看；给用户看的话由接口层统一说。
    """


class RateLimited(Exception):
    """对方说"你问得太快，稍后再说"（高德的 CUQPS 就是这种）。

    这种等着重试就行，跟"key 不对""没这条路"不一样 —— 后者重试多少次都没用。
    """


def _config(name: str) -> str:
    """读一条必填配置，空值当没配。"""
    value = (os.getenv(name) or "").strip()
    if not value:
        raise SourceError(f"{name} 还没配（Travele/.env 里是空的）")
    return value


def _optional(name: str) -> str | None:
    """读一条选填配置（比如可选的 token）。"""
    return (os.getenv(name) or "").strip() or None


def _get_json(url: str, params: dict, what: str) -> dict:
    """GET 一个返回 JSON 的接口，带指数退避重试。

    重试"没答上来"（超时、握手失败、连不上）和"你问得太快"（HTTP 429 / 5xx）。
    对方明确回了个别的错误状态码说明重试也没用，直接报。
    """
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = httpx.get(url, params=params, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status in (429, 500, 502, 503, 504) and attempt < ATTEMPTS:
                logger.warning("%s 第 %d/%d 次没成（HTTP %s，等一下再来）", what, attempt, ATTEMPTS, status)
                time.sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
                continue
            raise SourceError(f"{what} 没查成：{error}") from error
        except Exception as error:
            last = error
            logger.warning("%s 第 %d/%d 次没成：%s", what, attempt, ATTEMPTS, error)
            if attempt < ATTEMPTS:
                time.sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
    raise SourceError(f"{what} 试了 {ATTEMPTS} 次都没连上：{type(last).__name__}: {last}")


# ---- 工具返回的东西 ----
# 这三个放在这里、不放在各自的"返回数据处理"段里，是因为 @tool 在导入时就要解析类型提示，
# 写在后面会报 name 'WeatherDay' is not defined。
@dataclass
class WeatherDay:
    date: str
    summary: str
    high: float
    low: float
    rain_chance: int


@dataclass
class Ride:
    distance_km: float
    duration_min: int


@dataclass
class Flight:
    flight: str          # 航班号（带航司代码，如 CA1501）
    airline: str
    from_airport: str
    to_airport: str
    depart: str          # 计划起飞（对方给的是带时区的 ISO，原样带回来）
    arrive: str
    status: str


@dataclass
class Hotel:
    name: str
    address: str
    star_rating: float
    price_per_night: float | None    # 每晚最低价（人民币）；查不到价就是 None，不编
    price_note: str                  # 对方给的价格说明，原样带回来
    booking_url: str


def _leaf(error: BaseException) -> BaseException:
    """anyio 的报错是套娃（TaskGroup 里再包一层），剥到最里面那条才有用。"""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


def _mcp_stdio_command() -> list[str] | None:
    """把 MCP 服务当**子进程**起（stdio 模式）的命令，从 .env 读；没配就 None。

    命令写个名字就行（比如 `mcp-12306`）：找不到文件时去 venv 的 Scripts 目录找 ——
    app 是从那个 venv 跑的，`mcp-server-12306` 的命令行入口就装在那儿，不依赖 PATH。
    """
    line = _optional("TRAVELE_RAIL_MCP_CMD")
    if not line:
        return None
    parts = line.split()
    executable = pathlib.Path(parts[0])
    if not executable.exists():
        beside_python = pathlib.Path(sys.executable).parent / (parts[0] + ".exe")
        if beside_python.exists():
            return [str(beside_python), *parts[1:]]
    return [parts[0], *parts[1:]]


def _mcp_work(target, key: str | None, work):
    """连上 MCP 服务，把 session 交给 work 干活。

    target 有两种：**地址字符串**（Streamable HTTP，客户端去连你已经起好的服务）
    或 **(命令, 参数)**（stdio，客户端自己把服务当子进程起 —— 用户不用另外开一个终端）。

    协议（握手、会话、SSE 还是 JSON）全交给官方 SDK。SDK 是异步的、工具函数是同步的
    （planner 在 worker 线程里调它，那个线程没有事件循环），所以这里自己起一个。
    """
    label = target if isinstance(target, str) else " ".join(target)

    async def run():
        async with AsyncExitStack() as stack:
            if isinstance(target, str):
                client = None
                if key:
                    client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"},
                                                timeout=MCP_TIMEOUT_SECONDS)
                read, write = await stack.enter_async_context(
                    streamable_http_client(target, http_client=client))
            else:
                command, *args = target
                read, write = await stack.enter_async_context(
                    stdio_client(StdioServerParameters(command=command, args=args)))

            async with ClientSession(read, write) as session:
                await session.initialize()
                return await work(session)

    try:
        return asyncio.run(run())
    except SourceError:
        raise
    except Exception as error:
        # 我们自己的错误会被 anyio 包进组里，剥出来原样抛 —— 否则那条精确的消息会被套上"连不上"的壳
        leaf = _leaf(error)
        if isinstance(leaf, SourceError):
            raise leaf from error
        raise SourceError(f"MCP 服务没连上或没答对（{label}）：{type(leaf).__name__}: {leaf}") from error


def _mcp_pick(tools: list, *keywords: str):
    """按关键字挑工具：要求名字里**全部**关键字都出现；多个命中就挑名字最短的那个。

    为什么不硬写工具名：这类服务换版本就会改名。为什么要挑最短的：
    RollingGo 三个工具里 getHotelSearchTags 和 searchHotels 都含 search+hotel，
    但查列表的是后者，而它名字更短。
    """
    matched = [item for item in tools
               if all(word in (item.name or "").lower() for word in keywords)]
    if matched:
        return min(matched, key=lambda item: len(item.name or ""))
    names = "、".join(item.name or "" for item in tools) or "（一个都没有）"
    raise SourceError(f"这个 MCP 服务上没有名字含「{'/'.join(keywords)}」的工具，它有的是：{names}")


def _mcp_content(result) -> list[dict]:
    """把 MCP 的返回摊平：每块 text 可能是 JSON，也可能是给人看的文字。"""
    if getattr(result, "is_error", False):
        raise SourceError(f"MCP 那个工具自己报错了：{getattr(result, 'content', None)}")
    items: list[dict] = []
    for block in getattr(result, "content", None) or []:
        text = (getattr(block, "text", "") or "").strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            items.append({"text": text})
            continue
        items.extend(parsed if isinstance(parsed, list) else [parsed])
    if not items:
        raise SourceError(f"MCP 那边没返回内容：{result}")
    return items


# ==========================================================================
# 工具一：天气
# ==========================================================================
# ---- 传入数据处理 ----
def _weather_params(lat: float, lng: float, start: datetime.date, end: datetime.date) -> dict:
    today = datetime.date.today()
    if start < today:
        raise SourceError(f"出发日期 {start} 已经过去了（今天 {today}）—— 这是天气预报，查不了过去的日子")
    if (start - today).days > 16:
        raise SourceError("open-meteo 只能预报未来 16 天，出发日期太远，这次查不了天气")
    return {"latitude": lat, "longitude": lng, "timezone": "Asia/Shanghai",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "start_date": start.isoformat(), "end_date": end.isoformat()}


@tool(parse_docstring=True)
def weather(lat: float, lng: float, start: datetime.date, end: datetime.date) -> list["WeatherDay"]:
    """查某个地点未来几天的天气：天气现象、最高最低温、降雨概率。

    什么时候用：想知道出行那几天会不会下雨、冷不冷的时候用它。它只能预报未来 16 天。

    Args:
        lat: 纬度（目的地坐标，从候选点清单里取）
        lng: 经度
        start: 起始日期，YYYY-MM-DD
        end: 结束日期，YYYY-MM-DD
    """
    params = _weather_params(lat, lng, start, end)      # 参数不对就当场报错，别混进"网络没通"里
    return _to_weather_days(_get_json("https://api.open-meteo.com/v1/forecast", params, "open-meteo 天气"))


# ---- 返回数据处理 ----
# open-meteo 给的是 WMO 天气码，翻成中文 —— 这句要直接给用户看
WMO_TEXT = {0: "晴", 1: "大致晴朗", 2: "多云", 3: "阴", 45: "有雾", 48: "雾凇",
            51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
            61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
            71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
            80: "阵雨", 81: "中阵雨", 82: "强阵雨", 85: "阵雪", 86: "强阵雪",
            95: "雷阵雨", 96: "雷阵雨伴小冰雹", 99: "雷阵雨伴大冰雹"}


def _to_weather_days(data: dict) -> list[WeatherDay]:
    columns = [(data.get("daily") or {}).get(name) for name in
               ("time", "weather_code", "temperature_2m_max", "temperature_2m_min",
                "precipitation_probability_max")]
    if not all(columns):
        raise SourceError(f"open-meteo 返回的字段不全：{str(data)[:150]}")
    return [WeatherDay(date=date, summary=WMO_TEXT.get(code, "天气情况未知"),
                       high=high, low=low, rain_chance=rain)
            for date, code, high, low, rain in zip(*columns)]


# ==========================================================================
# 工具二：打车 / 自驾的路网距离与耗时（高德）
# ==========================================================================
# ---- 传入数据处理 ----
def _amap_params(origin: tuple[float, float], target: tuple[float, float]) -> dict:
    # 参数是 (lat, lng)，高德要的是"经度,纬度" —— 反了会查到海里
    return {"origin": f"{origin[1]},{origin[0]}", "destination": f"{target[1]},{target[0]}",
            "extensions": "base", "key": _config("TRAVELE_AMAP_KEY")}


# ---- 工具实现 ----
_rides: dict[tuple, "Ride"] = {}       # 一次规划里同一段路会被问好几遍，问过就记住
_last_call_at = 0.0                    # 上一次外部调用的时刻，用来和下一次拉开间隔


def _throttle() -> None:
    """两次调用之间留一点间隔。个人 key 的 QPS 很小，一口气连打就会被限流。"""
    global _last_call_at
    wait = _last_call_at + MIN_CALL_GAP_SECONDS - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()


# 开发者说明：高德 Web 服务 key 从 .env 读；同一段路在一次规划里会被问好几遍，问过就缓存。
@tool(parse_docstring=True)
def amap_ride(origin: tuple[float, float], target: tuple[float, float]) -> "Ride":
    """查两个地点之间驾车（打车、自驾）的路网距离与耗时。

    什么时候用：要算"从 A 到 B 打车多久、多远"的时候用它。它给距离和耗时，不给车费。

    Args:
        origin: 起点坐标 (纬度, 经度)
        target: 终点坐标 (纬度, 经度)
    """
    pair = (round(origin[0], 4), round(origin[1], 4), round(target[0], 4), round(target[1], 4))
    if pair in _rides:
        return _rides[pair]

    params = _amap_params(origin, target)               # key 没配在这里就报，不混进"网络没通"
    ride = None
    for attempt in range(1, ATTEMPTS + 1):
        _throttle()
        data = _get_json("https://restapi.amap.com/v3/direction/driving", params, "高德驾车路线")
        try:
            ride = _to_ride(data)
            break
        except RateLimited as error:                    # 它嫌我们问得太快 → 等一下再来
            if attempt == ATTEMPTS:
                raise SourceError(f"高德一直说问得太快（试了 {ATTEMPTS} 次）：{error}") from error
            logger.warning("高德说问得太快，等一下再来：%s", error)
            time.sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))

    _rides[pair] = ride
    return ride


# ---- 返回数据处理 ----
def _to_ride(data: dict) -> Ride:
    path = ((data.get("route") or {}).get("paths") or [{}])[0]
    if data.get("status") != "1" or not path.get("distance"):
        # 高德连"失败"也是 200，得看 status / info
        info, code = data.get("info"), data.get("infocode")
        if code == AMAP_BUSY_CODE:
            raise RateLimited(f"{info}（{code}）")
        raise SourceError(f"高德没给路线：{info}（{code}）")
    return Ride(distance_km=round(int(path["distance"]) / 1000, 1),
                duration_min=max(1, round(int(path["duration"]) / 60)))


# ==========================================================================
# 工具三：火车票（12306 的 MCP 服务）
# ==========================================================================
# ---- 传入数据处理 ----
def _rail_arguments(schema: dict, from_station: str, to_station: str, date: datetime.date) -> dict:
    """按这个工具自己声明的入参名填值：名字换版本就会变，所以现问现填。"""
    arguments: dict = {}
    for name in (schema.get("properties") or {}):
        lowered = name.lower()
        if "from" in lowered or "start" in lowered:
            arguments[name] = from_station
        elif "to" in lowered or "end" in lowered or "dest" in lowered:
            arguments[name] = to_station
        elif "date" in lowered or "time" in lowered:
            arguments[name] = date.isoformat()
    if not arguments:
        raise SourceError(f"12306 那个工具的入参名字对不上：{list(schema.get('properties') or {})}")
    return arguments


# ---- 工具实现 ----
# 开发者说明：12306 的 MCP 服务在你自己的终端里起：mcp-12306（默认 8000 端口），
# 然后把 http://localhost:8000/mcp 填进 .env 的 TRAVELE_RAIL_MCP_URL。
# 它一共 7 个工具，我们只用名字里带 ticket 的 query-tickets（其余是票价、中转换乘、经停站等）。
@tool(parse_docstring=True)
def train_tickets(from_station: str, to_station: str, date: datetime.date) -> list[dict]:
    """查两个车站之间某一天的火车车次。

    什么时候用：行程里要坐火车（往返或城市之间）的时候用它。站名写中文站名。

    Args:
        from_station: 出发站（如"杭州东"）
        to_station: 到达站（如"大理"）
        date: 乘车日期，YYYY-MM-DD
    """
    target = _mcp_stdio_command()
    if target is None:
        # 没给 stdio 命令就连地址（没配会在 _config 里报错）
        target = _config("TRAVELE_RAIL_MCP_URL")

    async def work(session):
        chosen = _mcp_pick((await session.list_tools()).tools, "ticket")
        result = await session.call_tool(chosen.name,
                                         _rail_arguments(chosen.input_schema or {},
                                                         from_station, to_station, date))
        return _to_trains(_mcp_content(result))

    return _mcp_work(target, None, work)


# ---- 返回数据处理 ----
def _to_trains(payloads: list[dict]) -> list[dict]:
    """12306 把结果包了一层：{success, count, trains:[{train_no, 时刻, duration, seats}]}。

    success 为 false 就是"这条线路查不到"（例如没有直达）—— 抛错，
    不返回一个空列表让人以为"查过了，就是没有"。
    """
    payload = payloads[0] if payloads else {}
    if not isinstance(payload, dict):
        raise SourceError(f"12306 返回的东西不认识：{str(payload)[:150]}")
    if payload.get("success") is False:
        raise SourceError(f"12306 没查到车次：{payload.get('error') or '（它没说原因）'}")
    trains = payload.get("trains")
    if not trains:
        raise SourceError(f"12306 的返回里没有车次：{str(payload)[:150]}")
    return trains


# ==========================================================================
# 工具四：酒店（RollingGo 的 MCP 服务）
# ==========================================================================
# ---- 传入数据处理 ----
def _hotel_arguments(city: str, check_in: datetime.date, check_out: datetime.date,
                     adults: int, size: int) -> dict:
    """按 RollingGo 那个工具声明的入参拼请求。

    它的规矩（写在 schema 的说明里）：只查城市时 place 要补国家（"大理 中国"），
    placeType 必须和 place 的实际内容一致；住几晚用 stayNights 表示，不给离店日期。
    """
    nights = max(1, (check_out - check_in).days)
    return {
        "originQuery": f"去{city}旅游，{check_in.isoformat()} 入住，住 {nights} 晚，"
                       f"{adults} 位成人，想住在方便逛的地方",
        "place": f"{city} 中国",
        "placeType": "城市",
        "checkInParam": {"checkInDate": check_in.isoformat(), "stayNights": nights,
                         "adultCount": adults},
        "size": size,
    }


# ---- 工具实现 ----
# 开发者说明：走 RollingGo 的 MCP 服务，地址在 .env 的 TRAVELE_ROLLINGGO_MCP_URL（含路径）；
# key 填了就按 Bearer 带过去。它有 3 个工具，我们只用查列表的 searchHotels。
#
# ⚠️ 实测：同一个条件连查三次，它每次返回的 5 家**不一样**（像是随机采样）。而酒店会被问两遍 ——
# Agent 模式里模型自己调一次，后端 resolve_lodging 核对明细时又调一次 —— 两次结果不同就会出现
# "模型从第一次的列表里挑了家酒店，后端拿第二次的列表一核对说没有，把住宿整段剔掉"。
# 所以这里按"城市 + 日期 + 人数"缓存，保证同一次规划里两次拿到的是同一份列表（TTL 只是防价格放太久）。
HOTEL_CACHE_SECONDS = 600
_hotels_cache: dict[tuple, tuple[float, list["Hotel"]]] = {}


@tool(parse_docstring=True)
def hotels(city: str, check_in: datetime.date, check_out: datetime.date, adults: int = 2) -> list["Hotel"]:
    """查某个城市某几天的酒店，给出名字、地址、星级和每晚最低价。

    什么时候用：要给"住哪儿"的建议时用它。城市写中文城市名。

    Args:
        city: 城市名（如"大理"）
        check_in: 入住日期，YYYY-MM-DD
        check_out: 离店日期，YYYY-MM-DD
        adults: 成人人数，默认 2
    """
    cache_key = (city, check_in.isoformat(), check_out.isoformat(), adults)
    cached = _hotels_cache.get(cache_key)
    if cached is not None and time.monotonic() - cached[0] < HOTEL_CACHE_SECONDS:
        return cached[1]

    url = _config("TRAVELE_ROLLINGGO_MCP_URL")
    key = _optional("TRAVELE_ROLLINGGO_API_KEY")

    async def work(session):
        chosen = _mcp_pick((await session.list_tools()).tools, "search", "hotel")
        result = await session.call_tool(chosen.name,
                                         _hotel_arguments(city, check_in, check_out, adults, 5))
        return _to_hotels(_mcp_content(result))

    offers = _mcp_work(url, key, work)
    _hotels_cache[cache_key] = (time.monotonic(), offers)
    return offers


# ---- 返回数据处理 ----
def _to_hotels(payloads: list[dict]) -> list[Hotel]:
    """RollingGo 也是包了一层：{success, code, message, hotelInformationList:[...]}。

    success 为 false（或列表为空）就抛错，把它的 message 带出来 —— 不返回空列表让人以为查过了。
    """
    payload = payloads[0] if payloads else {}
    if not isinstance(payload, dict):
        raise SourceError(f"RollingGo 返回的东西不认识：{str(payload)[:150]}")
    rows = payload.get("hotelInformationList")
    if payload.get("success") is False or not rows:
        raise SourceError(f"RollingGo 没查到酒店：{payload.get('message') or str(payload)[:150]}")

    return [Hotel(name=row.get("name") or "",
                  address=row.get("address") or "",
                  star_rating=row.get("starRating") or 0.0,
                  price_per_night=(row.get("price") or {}).get("lowestPrice"),
                  price_note=(row.get("price") or {}).get("message") or "",
                  booking_url=row.get("bookingUrl") or "")
            for row in rows]


# ==========================================================================
# 工具五：航班时刻（Aviationstack）
# ==========================================================================
# ---- 传入数据处理 ----
# 这个订阅**不支持按日期查**（带 flight_date 会回 403 function_access_restricted），只给实时航班，
# 返回窗口实测只有今天和明天 —— 所以：一次拿满，日期在本地筛（见下面的返回数据处理）。
FETCH_LIMIT = 100


def _flight_params(dep_iata: str, arr_iata: str) -> dict:
    """必须是机场三字码：把"杭州"换成 HGH 需要另一份数据，这里不猜。"""
    params = {"access_key": _config("TRAVELE_AVIATIONSTACK_KEY"), "limit": FETCH_LIMIT}
    for label, code in (("dep_iata", dep_iata), ("arr_iata", arr_iata)):
        value = (code or "").strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise SourceError(f"{label} 要机场三字码（例如 HGH、PEK），收到的是「{code}」")
        params[label] = value
    return params


# ---- 工具实现 ----
# 开发者说明：Aviationstack 的 key 在 .env 的 TRAVELE_AVIATIONSTACK_KEY；它只有班次与时刻，没有票价。
@tool(parse_docstring=True)
def flights(dep_iata: str, arr_iata: str, date: datetime.date, limit: int = 10) -> list["Flight"]:
    """查两个机场之间某一天的航班班次与起降时刻。

    什么时候用：用户要坐飞机往返的时候用它。入参必须是机场三字码，不是城市名。
    注意：它没有票价；而且这个订阅只给实时航班（只能查到今天和明天），更远的日期查不到。

    Args:
        dep_iata: 出发机场三字码（如 HGH）
        arr_iata: 到达机场三字码（如 DLU）
        date: 航班日期，YYYY-MM-DD
        limit: 最多返回几条，默认 10
    """
    params = _flight_params(dep_iata, arr_iata)
    data = _get_json("https://api.aviationstack.com/v1/flights", params, "Aviationstack 航班")
    return _to_flights(data, date, limit)


# ---- 返回数据处理 ----
def _to_flights(data: dict, date: datetime.date, limit: int) -> list[Flight]:
    """按 data[] 结构取，再按日期本地筛（这个订阅不支持按日期查）。

    筛不到就报错，并把对方实际给的日期窗口写出来 —— 不然调用方会以为"那天没有航班"。
    """
    rows = (data or {}).get("data")
    if not isinstance(rows, list) or not rows:
        raise SourceError(f"Aviationstack 没给航班数据：{str(data)[:150]}")

    wanted = date.isoformat()
    picked = [row for row in rows if (row.get("flight_date") or "") == wanted]
    if not picked:
        window = "、".join(sorted({row.get("flight_date") or "？" for row in rows}))
        raise SourceError(f"Aviationstack 这个订阅只给实时航班（它这次返回的日期是 {window}），"
                          f"查不到 {wanted} 的航班")

    def part(row: dict, name: str) -> dict:
        return row.get(name) or {}

    return [Flight(flight=part(row, "flight").get("iata") or "",
                   airline=part(row, "airline").get("name") or "",
                   from_airport=part(row, "departure").get("airport") or "",
                   to_airport=part(row, "arrival").get("airport") or "",
                   depart=part(row, "departure").get("scheduled") or "",
                   arrive=part(row, "arrival").get("scheduled") or "",
                   status=row.get("flight_status") or "")
            for row in picked[:limit]]
