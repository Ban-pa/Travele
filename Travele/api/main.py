"""HTTP 接口 —— 6 个端点 + SSE 真流式 + 统一错误体 + 托管前端静态页。

前端（Travele/static/app.js）就调这几个：

    POST /api/trips                  提交需求 → 202 回执（回执里前端只读 tripId）
    GET  /api/trips/{id}/events      进度：SSE 真流式，末尾必发 done 或 failed
    GET  /api/trips/{id}             行程（权威结果；前端收到 done 之后再来取一次）
    POST /api/trips/{id}/confirm     决策表 → 各点介绍 + 出行建议（同时写长期记忆）
    GET  /api/plans                  历史旅游方案列表（用户确认过的）
    GET  /api/plans/{planId}         那一份完整的历史行程


"""
import asyncio
import datetime
import logging
import mimetypes
import os
import pathlib
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


def keep_temp_out_of_project() -> None:
    if pathlib.Path(tempfile.gettempdir()).resolve() != PROJECT_ROOT:
        return              # 正常机器上什么都不做
    temp_dir = PROJECT_ROOT / ".tmp"
    temp_dir.mkdir(exist_ok=True)
    tempfile.tempdir = str(temp_dir)
    logging.getLogger("travele").info("系统临时目录不可用，临时文件改放 %s", temp_dir)


keep_temp_out_of_project()

from fastapi import FastAPI, Request                       # noqa: E402
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from Travele.api.schemas import (
    ApiErrorBody,
    ConfirmRequest,
    ConfirmResponse,
    ErrorCode,
    PlanningRequest,
    ProgressEvent,
    ProgressEventType,
    SpotIntro,
    StoredPlan,
    TripReceipt,
)
from Travele.memory import memory
from Travele.planning import planner
from Travele.planning.schemas import (
    PlanMeta,
    TripDestination,
    TripDetail,
    TripStatus,
)
from Travele.retrieval import recall

logger = logging.getLogger("travele")

STATIC_DIR = pathlib.Path(__file__).resolve().parents[1] / "static"
# 模型这三行是必填（缺了就排不了行程）；其余数据源 key 缺了只是那一路不可用。
# 仓库里不带任何 key：模板是 Travele/.env.example，本地复制成 Travele/.env 再填。
REQUIRED_ENV = ("TRAVELE_LLM_API_KEY", "TRAVELE_LLM_BASE_URL", "TRAVELE_LLM_MODEL")
# 地点照片：单独一个文件夹（Travele/P），按地点名命名，这里挂成 /photos/<文件名>。
# 不改前端里那些数据的结构，只是让 <img src="/photos/西湖.jpg"> 能取到图。
PHOTO_DIR = pathlib.Path(__file__).resolve().parents[1] / "P"
HEARTBEAT_SECONDS = 15.0

# 这台机器的 mimetypes 不认 webp/avif，会把它们当二进制流发出去（浏览器多半也能渲染，
# 但没必要让它猜）。注册一下，让照片按图片类型返回。
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动时预热召回：本地 bge 嵌入模型第一次加载要十几秒，别让第一个用户白等。

    放线程里做，不占事件循环；预热失败也不拦启动（planning 会降级成"只依据目录"）。
    """
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        logger.warning("这些配置还没填（%s）：照着 Travele/.env.example 复制一份 Travele/.env 再填上；"
                       "没填模型 key 的话，排行程会在提交时被拦下", "、".join(missing))
    else:
        logger.info("模型配置已就绪：%s", os.getenv("TRAVELE_LLM_MODEL"))
    if not os.getenv("TRAVELE_AMAP_KEY"):
        logger.warning("没配 TRAVELE_AMAP_KEY：打车/自驾的耗时、目录扩点会不可用"
                       "（这一路会如实报「没配置」，不估算顶替）")

    await asyncio.to_thread(recall.warm_up)
    yield


app = FastAPI(title="Travele", description="智能旅行规划", version="0.1.0", lifespan=lifespan)


def now() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


class TripNotFound(Exception):
    """任务不存在 —— 404 TRIP_NOT_FOUND。"""


class PlanningTask:
    """一次规划的内存记录：结果 + 已发生的进度 + 每个订阅者的队列。

    emit 可能从工作线程被调用（模型调用是阻塞的，会放进 to_thread），
    所以统一用 call_soon_threadsafe 回到事件循环里改状态，免得两个线程一起写坏它。
    """

    def __init__(self, trip_id: str, request: PlanningRequest, loop: asyncio.AbstractEventLoop):
        self.trip_id = trip_id
        self.request = request
        self.loop = loop
        self.generated: TripDetail | None = None   # 刚生成的样子（确认时以它为基准合并）
        self.trip: TripDetail | None = None        # 当前视图；确认之后就是合并版
        self.events: list[ProgressEvent] = []
        self.queues: list[asyncio.Queue] = []
        self.finished = False
        self.job: asyncio.Task | None = None       # 存住引用，否则后台任务可能被回收

    def emit(self, event: str, message: str) -> None:
        self.loop.call_soon_threadsafe(self._record, ProgressEventType(event), message)

    def _record(self, event: ProgressEventType, message: str) -> None:
        record = ProgressEvent(event=event, message=message)
        self.events.append(record)
        for queue in self.queues:
            queue.put_nowait(record)
        if event in (ProgressEventType.done, ProgressEventType.failed):
            self.finished = True
            for queue in self.queues:
                queue.put_nowait(None)     # None 是"流结束"的信号

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        for record in self.events:
            queue.put_nowait(record)       # 后连上来的也能补看已经发生的进度
        if self.finished:
            queue.put_nowait(None)
        self.queues.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self.queues:
            self.queues.remove(queue)


tasks: dict[str, PlanningTask] = {}


# ==========================================================================
# 端点①：提交需求
# ==========================================================================
@app.post("/api/trips", response_model=TripReceipt, status_code=202)
async def create_trip(request: PlanningRequest) -> TripReceipt:
    """立刻回 202，规划放后台跑，进度走 SSE。

    "这个城市一点数据都没有"这种一眼就知道不行的，先在这里拦掉直接回 400：
    提交就成功、再等进度流才告诉用户白填了，体验很差。
    注意这不是"用户填错了城市"——目录没覆盖没关系，有攻略就能排；两点都没有才算缺数据。
    """
    if not os.getenv("TRAVELE_LLM_API_KEY"):
        logger.warning("提交被拦下：没配 TRAVELE_LLM_API_KEY")
        return error_response(
            500, ErrorCode.internal_error.value,
            "服务还没配模型 key：把 Travele/.env.example 复制成 Travele/.env，"
            "填上 TRAVELE_LLM_API_KEY / TRAVELE_LLM_BASE_URL / TRAVELE_LLM_MODEL 再试")

    province = request.destination.province
    city = request.destination.city
    # to_thread 的用法是 to_thread(要调的函数, 传给它的参数…)：
    # 后面这两个就是传给 check_city_available 的 province / city，不是没传参
    await asyncio.to_thread(planner.check_city_available, province, city)

    trip_id = uuid.uuid4().hex #是 Python 中生成随机唯一标识符的常见写法
    task = PlanningTask(trip_id, request, asyncio.get_running_loop())#get_running_loop()是获取当前正在循环的事件
    tasks[trip_id] = task
    # 规划放线程里跑：它现在很快，但接了模型之后就是阻塞调用，不能占着事件循环
    task.job = asyncio.create_task(run_planning(task))
    return TripReceipt(trip_id=trip_id, status=TripStatus.planning, created_at=now())


async def run_planning(task: PlanningTask) -> None:
    """后台跑规划。

    结尾的 done 由**这里**在结果存好之后发 —— 这样前端一收到 done，
    紧接着 GET 一定能拿到 completed 的行程，不会取到空壳。
    """
    try:
        trip = await asyncio.to_thread(planner.plan, task.request, task.trip_id, task.emit)
    except planner.CityNotFound as error:
        task.emit("failed", str(error))
        return
    except planner.GenerationFailed as error:
        task.emit("failed", str(error))         # 模型没排出来，这句本来就是给用户看的
        return
    except Exception:                          # 规划炸了也得给前端一个交代
        # 技术原因（可能带路径、堆栈）只进日志，发给用户的只留一句能看懂的话
        logger.exception("规划失败")
        task.emit("failed", "排行程的时候出错了，请稍后再试")
        return

    task.generated = trip
    task.trip = trip
    task.emit("done", "规划完成")


# ==========================================================================
# 端点②：进度（SSE 真流式）
# ==========================================================================
@app.get("/api/trips/{trip_id}/events")
async def stream_events(trip_id: str) -> StreamingResponse:
    task = tasks.get(trip_id)
    if task is None:
        raise TripNotFound(f"没有这个任务：{trip_id}")
    return StreamingResponse(
        event_stream(task),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def event_stream(task: PlanningTask) -> AsyncIterator[str]:
    """有事件就推；没事件就每 15 秒发一行注释当心跳（EventSource 会忽略注释行）。"""
    queue = task.subscribe()
    try:
        while True:
            try:
                record = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": 心跳\n\n"
                continue
            if record is None:
                break
            yield f"data: {record.model_dump_json(by_alias=True)}\n\n"
    finally:
        task.unsubscribe(queue)


# ==========================================================================
# 端点③：取行程
# ==========================================================================
@app.get("/api/trips/{trip_id}", response_model=TripDetail)
async def get_trip(trip_id: str) -> TripDetail:
    """取行程。还没规划完就回 status=planning + days=null 的壳（契约就是这么定的）。"""
    task = tasks.get(trip_id)
    if task is None:
        raise TripNotFound(f"没有这个任务：{trip_id}")

    if task.trip is None:
        return TripDetail(
            trip_id=trip_id,
            status=TripStatus.planning,
            title="正在规划…",
            plan_meta=PlanMeta(validated=False),
            destination=TripDestination(province=task.request.destination.province,
                                        city=task.request.destination.city),
            days=None,
            created_at=now(),
        )
    return task.trip


# ==========================================================================
# 端点④：确认
# ==========================================================================
@app.post("/api/trips/{trip_id}/confirm", response_model=ConfirmResponse)
async def confirm_trip(trip_id: str, request: ConfirmRequest) -> ConfirmResponse:
    """在**刚生成的那份**行程上合并决策表（所以确认可以重复调用、结果一样），再回介绍与建议。"""
    task = tasks.get(trip_id)
    if task is None or task.generated is None:
        raise TripNotFound(f"还没有可确认的行程：{trip_id}")

    merged = planner.apply_decisions(task.generated, request.decisions)
    task.trip = merged                              # 之后 GET 拿到的就是确认版
    # 长期记忆的唯一写入点：用户确认之后记一次（偏好 / 补充说明 / 这趟选中的点 / 这份行程）
    await asyncio.to_thread(planner.remember_trip, task.request, merged)
    # 解说：由模型写各点介绍与出行建议（后端备事实）。要跑模型，所以放线程里。
    intros, tips = await asyncio.to_thread(planner.build_confirm, merged)
    return ConfirmResponse(
        trip_id=trip_id,
        spot_intros=[SpotIntro(**item) for item in intros],
        travel_tips=tips,
    )


# ==========================================================================
# 端点⑤⑥：历史旅游方案（用户确认过的行程）
# ==========================================================================
@app.get("/api/plans", response_model=list[StoredPlan])
async def list_plans() -> list[StoredPlan]:
    """历史方案列表（新的在前）。读库是阻塞的，放线程里。"""
    rows = await asyncio.to_thread(memory.list_plans)
    return [StoredPlan(**row) for row in rows]


@app.get("/api/plans/{plan_id}", response_model=TripDetail)
async def get_stored_plan(plan_id: str) -> TripDetail:
    """一份完整的历史行程 —— 存进去的就是 TripDetail 的 JSON，直接还回去。"""
    payload = await asyncio.to_thread(memory.get_plan, plan_id)
    if payload is None:
        raise TripNotFound(f"没有这份历史方案：{plan_id}")
    return TripDetail(**payload)


# ==========================================================================
# 统一错误体（顶层返回，不套 HTTPException 的 {"detail": ...}）
# ==========================================================================
def error_response(status: int, code: str, message: str, detail: dict | None = None) -> JSONResponse:
    body = ApiErrorBody(code=code, message=message, detail=detail or {})
    return JSONResponse(status_code=status, content=body.model_dump(by_alias=True, mode="json"))


@app.exception_handler(RequestValidationError)
async def on_invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
    """契约不合法 → 422，并且把"哪个字段错了"原样带上，方便前端和排错。"""
    problems = [
        {"loc": [str(part) for part in error["loc"]], "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return error_response(422, ErrorCode.validation_failed.value, "请求体不符合契约",
                          {"errors": problems})


@app.exception_handler(TripNotFound)
async def on_trip_not_found(_: Request, exc: TripNotFound) -> JSONResponse:
    return error_response(404, ErrorCode.trip_not_found.value, str(exc))


@app.exception_handler(planner.CityNotFound)
async def on_city_not_found(_: Request, exc: planner.CityNotFound) -> JSONResponse:
    return error_response(400, ErrorCode.city_not_found.value, str(exc))


@app.exception_handler(planner.UnknownNode)
@app.exception_handler(planner.UnknownChoice)
async def on_bad_decision(_: Request, exc: Exception) -> JSONResponse:
    return error_response(422, ErrorCode.validation_failed.value, str(exc))


@app.exception_handler(planner.ContentFailed)
async def on_content_failed(_: Request, exc: planner.ContentFailed) -> JSONResponse:
    """解说生成失败 —— 说人话让用户再试一次（不回退规则版）。"""
    return error_response(500, ErrorCode.internal_error.value, str(exc))


@app.exception_handler(Exception)
async def on_unexpected(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("未预期的错误")
    return error_response(500, ErrorCode.internal_error.value, f"服务内部错误：{exc}")


# 静态资源（页面 / 脚本 / 样式 / 前端读的那几个 json）一律"每次回来问一句"：
# StaticFiles 只给 Last-Modified + ETag，浏览器会按自己的启发式规则直接吃缓存 ——
# 实测踩过：改了 app.js，页面照样用缓存里的老脚本，连请求都不发，改了等于没改。
# no-cache 不是"不缓存"，是"每次带 If-None-Match 来问"，没变就还是 304，不亏速度。
NO_CACHE_SUFFIXES = (".html", ".css", ".js", ".json")


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.endswith(NO_CACHE_SUFFIXES):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# 托管前端：放最后挂，前面注册的 /api 路由优先匹配
if PHOTO_DIR.is_dir():
    app.mount("/photos", StaticFiles(directory=PHOTO_DIR), name="photos")
else:
    logger.warning("没有找到照片目录 %s，页面上的图片会退回手绘底色", PHOTO_DIR)

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
