"""① Agent 模式 / ② 单次生成 —— 让模型真正参与编排的那两档。

这个文件是**从零建**的：在这之前项目里只有 TripDraft（草稿结构）和 planner.to_trip_detail()
（草稿 → 成品），没有任何东西能产出草稿。这里补上产出者。

四块职责：

  一、模型接入 —— 已实现：python-dotenv 读 Travele/.env，init_chat_model 造模型
  二、工具     —— 已接：数据源层那五个（天气 / 打车 / 酒店 / 火车 / 航班）交给 create_agent
  三、提示词   —— 已实现：规则 + 长期记忆 + 输出格式（系统消息），需求 + 候选点 + 攻略片段（人话消息）
  四、两档产出 —— 已实现：① create_agent（带工具）② with_structured_output(json_mode)；
                  每档最多试 3 次；都不行 → 返回 None，由 planner 直接报错让用户重试
"""
import concurrent.futures
import datetime
import logging
import os
import pathlib
import time

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import StructuredTool

from Travele.planning import tool
from Travele.planning.schemas import ConfirmDraft, PlanContext, TripDraft

logger = logging.getLogger("travele")

ENV_FILE = pathlib.Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_FILE)      # 读 .env 用现成的轮子（python-dotenv），不自己解析文件

# 时间预算：**总共 3 分钟**（前端也按这个等，比它多留一点余量）。
# 超了就不再开新的一次调用，直接当"创建超时"报给用户 —— 不能让页面一直转下去。
TOTAL_BUDGET_SECONDS = 180
REQUEST_TIMEOUT_SECONDS = 120
MAX_ATTEMPTS = 3          # 每档最多试 3 次（调用失败、或结果解析不出来，都算失败）
MIN_ATTEMPT_SECONDS = 5   # 剩这么点时间就别再试了
MIN_REVISION_SECONDS = 45 # 剩不到这么多时间就不回喂重排了（回喂一轮要 30~60 秒）
# 解说（确认阶段那一次生成）：比规划轻，但它发生在用户点"确认"之后，
# 界面上没有进度条可看，所以别拖太久。
CONFIRM_TIMEOUT_SECONDS = 90
CONFIRM_ATTEMPTS = 2      # 解说最多问两次（json_mode 偶尔返回空，重问一次就出来了）


# ==========================================================================
# 一、模型接入
# ==========================================================================

THINKING_OFF = {"extra_body": {"thinking": {"type": "disabled"}}}


def build_model(timeout: float = REQUEST_TIMEOUT_SECONDS, thinking: bool = True, **extra):
    return init_chat_model(
        model=os.getenv("TRAVELE_LLM_MODEL"),
        model_provider="openai",
        api_key=os.getenv("TRAVELE_LLM_API_KEY"),
        base_url=os.getenv("TRAVELE_LLM_BASE_URL"),
        timeout=timeout,      # 配合重试预算：单次调用不许拖过前端能等的时间
        max_tokens=4096,      # 多日行程的结构化输出比较长，别被截断
        max_retries=0,        # 重试由下面的 try_stage 统一管，不让客户端自己偷偷重试
        **(THINKING_OFF if not thinking else {}),
        **extra,
    )


# ==========================================================================
# 二、工具 —— 交给 create_agent 的那五个（实现在 planning/tool.py）
#
# 这里只负责把工具递过去，并且给的是**容错副本**：工具报错时不崩整轮 Agent，
# 而是把原因当成工具输出还给模型，让它自己换个做法。
#
# 实测：这个模型**可以用工具**，而且会自己决定调不调；被服务端拒绝的只是"强制它必须调用"
# （tool_choice=required / 点名）。
# ==========================================================================
def _tolerant(item: StructuredTool) -> StructuredTool:
    """把一个工具包成"查不到就把原因说出来"的版本（**只给 Agent 用**）。
    """
    def call(**kwargs):
        try:
            return str(item.func(**kwargs))
        except Exception as error:
            logger.warning("工具 %s 这次没成（错误已还给模型）：%s", item.name, error)
            return f"（这个工具没查成：{error} —— 换个做法，不要编数据）"

    return StructuredTool.from_function(func=call, name=item.name, description=item.description,
                                        args_schema=item.args_schema)


def build_tools(context: PlanContext) -> list:
    """交给 Agent 的工具：数据源层那五个（天气 / 打车 / 酒店 / 火车 / 航班）。
    """
    return [_tolerant(item) for item in
            (tool.weather, tool.amap_ride, tool.hotels, tool.train_tickets, tool.flights)]


SYSTEM_PROMPT = """你是旅行规划师。用户给出出行需求，你要排出一份"每天上午／下午／晚上各安排什么"的行程，并说清为什么这么排。

三种依据，都要参考：
1) 候选点清单（目的地目录的原数据）——每个点带编号、名称、类型、坐标、开放时间、常规游览时长。选点只能从这份清单里挑。
2) 攻略召回片段（带出处）——里面的顺序建议、注意事项、什么时间去最合适，是编排的主要参考。如果片段里写到了某个地点、而清单里没有它，说明这个点没有可用的坐标数据，不要选它。
3) 长期记忆（用户画像与历史）——反映用户的习惯、忌讳、去过的地方，安排时要照顾到。

你的判断力用在这些地方：
- 选哪些点、怎么组合：顺路走、错开开放时间与高峰、避免来回折返
- 每天排几个点：按同行人（老人／小孩）、用户说的节奏、当天怎么移动来定
- 什么时候去哪个点最合适：看日出、避开正午暴晒、夜市要傍晚去
- 每天给一个中文主题；每个方案给一句推荐理由
- 把用户写明的偏好和长期记忆真正落进安排里，而不是只体现在字段上

这些由后端补，你不要填（填了也会被覆盖）：地点的规范名称与经纬度、交通耗时与价格、开放时间、总预算。交通只给方式（method）。

输出必须严格按这条消息里给出的 JSON 格式，不要输出格式之外的任何文字：
- 只写候选点清单里的 poiId；名字拿不准就照抄清单里的规范名称
- 天数与用户给的日期区间一致；**每天三段（morning／afternoon／evening）都要有安排** ——
  一天里只剩上午或下午有事、其他空着，是最常见的差评
- 一天排不满时按这个顺序想办法：① 同一片区域的两个点分到上午和下午 ② 大景区按半天算，
  剩下的半天排附近的小点 ③ 只有当天确实在赶路（上午才落地、傍晚要赶车船）才让那一段空着，
  并在那个节点的 unverified 里写一句原因
- 同一个地点一天最多排一次；整趟行程里最多排 2 次（第二次要是"晚上再来吃饭/看夜景"这种说得通的），
  别把三天排成同样几个点转圈 —— 候选点能用的尽量都用上；实在不够就少排一段，**宁可空着也别重复凑数**
- 交通方式优先写 打车 / 公交 / 步行 / 骑行 / 自驾 —— 地铁、船、飞机这些这边算不出途中耗时，
  写了也只会被换掉
- 每个节点的时刻要照顾**这个点的开放时间**（清单里每行都有，`全天` 的除外）：
  排在开门之后、关门之前，别把 18:00 就关门的点排到 18:00 才开始
- 每个节点尽量给 2 个候选方案（至少 1 个），用户可以在页面上就地换
- 拿不准但又必须提醒用户的事，写进那个节点的 unverified
- 大交通：只要用户不是自驾、出发点也不是目的地，就**必须**给「去程」和「回程」两段（direction 写 outbound / inbound），从出发地到目的地，写清用什么交通、车次或航班、几点走几点到；火车和航班可以先用工具查一下再写（**机票票价没有可靠来源，飞机那段不用给 price**）
- 住宿：**必须给**（多日行程总有睡觉这一环），从给你的酒店里挑一家，把名字写进 lodging，绝对不要自己编酒店名；住哪几晚用 checkIn / checkOut 表示
- **查不到就别编**：工具报错、或者给你的资料里没有，就如实留空 / 写"这次没查到"，绝对不要自己造一个班次、酒店名或气温出来
"""

AGENT_SOURCE_HINT = ("候选点清单和攻略片段就在这条消息里，直接依据它们排；"
                     "【已经查好的外部事实】里的天气、酒店、车次也已经查好了，**不用再调工具问一遍**；"
                     "只有确实还缺别的（比如别的城市、别的日期）才去调工具。")
SINGLE_SHOT_SOURCE_HINT = ("候选点清单、攻略片段、以及【已经查好的外部事实】（天气、酒店、车次）都在这条消息里，"
                           "直接依据它们排；这一档没有工具，资料里没有的就如实留空。")
HUMAN_TEMPLATE = ("{request_text}\n\n【候选点清单（只能从这里选）】\n{candidates}\n\n"
                  "【已经查好的外部事实（后端并行查好的，不用再查）】\n{facts}\n\n"
                  "【攻略片段（带出处）】\n{guides}")
# 单次生成没有工具，所以住宿和天气由后端先查好、直接喂进消息（跟候选点、攻略一个待遇）——
# 现在两条路都走同一份【已经查好的外部事实】，不再各查一遍。
SINGLE_SHOT_TEMPLATE = HUMAN_TEMPLATE
# 回喂重排：在单次生成的资料后面，再挂上"你上一版怎么排的 + 校验挑出了什么问题"
REVISE_TEMPLATE = ("\n\n【你上一版的草稿（JSON）】\n{previous}\n\n"
                   "【校验发现的问题】\n{issues}\n\n"
                   "【要求】只改上面这些问题涉及的地方（时间安排、选点、交通方式），"
                   "其余保持原样；改完重新输出**完整**的 JSON。")

# ==========================================================================
# 解说（用户点"确认"之后那一次生成）
# ==========================================================================
CONFIRM_SYSTEM_PROMPT = """你是给用户写旅行介绍与出行建议的人。用户已经确认了行程，你要把每个地点讲清楚，并给几条这趟真的用得上的建议。

事实由后端给你，你不要自己造：
- 每个地点的资料（类型、评分、地址、季节）、攻略片段（带出处）、这趟行程的安排（哪天哪个时段去哪、怎么走、多少分钟）、住宿、大交通、那几天的天气，都在消息里
- **绝对不要编**：开放时间、票价、班次、电话号码、营业状态之类资料里没有的事，一律不写；拿不准就不提

怎么写：
- 每个地点 2~3 句：这是什么、为什么这趟值得去；不要"建议游玩"这种空话，也不要复述行程安排
- 出行建议 3~5 条，每条都要**针对这趟行程**（几月去、同行人有没有老人小孩、要走多少路、那几天的天气、住宿在哪），不要写放之四海皆准的话
- 说人话，句子短一点，不要用"赋能/打造/极致体验"这类词

输出必须严格按这条消息里给出的 JSON 格式，不要输出格式之外的任何文字：
- spotIntros 里只写**行程里出现过的** poiId，一个点一条；intro 就是那 2~3 句
- travelTips 里每条一句话
"""

CONFIRM_TEMPLATE = ("【这趟行程】\n{plan}\n\n【地点资料】\n{places}\n\n"
                    "【攻略片段（带出处）】\n{guides}\n\n【那几天的天气】\n{weather}")


def system_prompt(context: PlanContext, has_tools: bool) -> str:
    """系统提示词 = 规则 + 资料在哪 + 长期记忆 + 输出格式说明。
    """
    return "\n\n".join([
        SYSTEM_PROMPT,
        AGENT_SOURCE_HINT if has_tools else SINGLE_SHOT_SOURCE_HINT,
        "【长期记忆】\n" + memory_text(context),
        FORMAT_HINT,
    ])


# ==========================================================================
# 四、两档产出（已实现，每档最多 3 次）
# ==========================================================================
PARSER = PydanticOutputParser(pydantic_object=TripDraft)
FORMAT_HINT = PARSER.get_format_instructions()


def request_text(context: PlanContext) -> str:
    """把需求写成一段给模型看的话（这些是事实，不用等提示词定稿）。"""
    return "\n".join([
        f"目的地：{context.province} {context.city}",
        f"出发地：{context.departure_city or '（没填）'}",
        f"日期：{context.dates[0]} 至 {context.dates[-1]}，共 {len(context.dates)} 天",
        f"人数：大人 {context.adults}，儿童 {context.children}",
        f"交通：{'自驾' if context.is_self_driving else '不自驾'}",
        f"必去地点：{'、'.join(context.must_visit) if context.must_visit else '（没有强制要求）'}",
        f"想要的主题：{'、'.join(context.themes) if context.themes else '（没特别说）'}",
        f"想避开：{'、'.join(context.avoid) if context.avoid else '（没特别说）'}",
        f"补充说明：{context.remarks or '（没有）'}",
    ])


def candidates_text(context: PlanContext) -> str:
    """候选点清单的文本形式。（工具定稿后会复用这个函数，不重复实现。）"""
    if not context.candidates:
        return f"没有 {context.city} 的候选点清单。"
    lines = []
    for place in context.candidates:
        coords = f"{place.lat},{place.lng}" if place.lat is not None else "无坐标"
        lines.append(f"{place.place_id}｜{place.name}｜{place.poi_type or '-'}｜{coords}｜"f"{place.open_hours}｜{place.duration_min} 分钟")
    return "\n".join(lines)


def guides_text(context: PlanContext) -> str:
    """召回片段带出处的文本形式。（工具定稿后会复用这个函数。）"""
    if not context.docs:
        return f"没有找到 {context.city} 的攻略资料。"
    return "\n\n".join(
        f"[{chunk.doc} 第{chunk.chunk_index}块 相关度{chunk.score}] {chunk.text}"
        for chunk in context.docs)


def memory_text(context: PlanContext) -> str:
    """系统提示词里「长期记忆」那一段的文本。
    """
    return context.memory.strip() or "（暂无记忆）"


# ==========================================================================
# 三点五、外部事实：**并行**预取，一次性塞进提示词
# ==========================================================================
FACTS_BUDGET_SECONDS = 25      # 预取整体预算：到点还没回来的就不等了，如实写"没查到"


def _fetch_weather(context: PlanContext) -> str:
    first = next((place for place in context.candidates if place.lat is not None), None)
    if first is None:
        return "天气：（没有可用坐标，没查）"
    days = tool.weather.invoke({"lat": first.lat, "lng": first.lng,
                                "start": context.dates[0], "end": context.dates[-1]})
    return "天气：\n" + "\n".join(
        f"  {day.date} {day.summary} {day.low}~{day.high}℃ 降雨概率 {day.rain_chance}%"
        for day in days)


def _fetch_hotels(context: PlanContext) -> str:
    offers = tool.hotels.invoke({"city": context.city, "check_in": context.dates[0],
                                 "check_out": context.dates[-1] + datetime.timedelta(days=1),
                                 "adults": max(1, context.adults)})
    return "酒店（住宿只能从这些里挑）：\n" + "\n".join(
        f"  {item.name}｜{item.star_rating} 星｜{item.price_per_night} 元/晚｜{item.address}"
        for item in offers)


def _fetch_trains(context: PlanContext) -> str:
    if not context.departure_city:
        return "火车：（没填出发地，没查）"
    rows = tool.train_tickets.invoke({"from_station": context.departure_city,
                                      "to_station": context.city, "date": context.dates[0]})
    if not rows:
        return "火车：（没查到车次）"
    return "火车（去程那天）：\n" + "\n".join("  " + "｜".join(f"{key}={value}" for key, value in row.items()
                                                              if value not in (None, "", []))
                                             for row in rows[:5])


FACT_JOBS = (("天气", _fetch_weather), ("酒店", _fetch_hotels), ("火车", _fetch_trains))


def prefetch_facts(context: PlanContext) -> str:
    """三个外部来源并行查好，拼成"已经查好的外部事实"那一段。
    """
    if context.facts:
        return context.facts

    began = time.monotonic()
    texts: dict[str, str] = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(FACT_JOBS))
    futures = {pool.submit(job, context): name for name, job in FACT_JOBS}
    try:
        for future in concurrent.futures.as_completed(futures, timeout=FACTS_BUDGET_SECONDS):
            name = futures[future]
            try:
                texts[name] = future.result()
            except Exception as error:
                logger.warning("预取%s没成：%s", name, error)
    except concurrent.futures.TimeoutError:
        logger.warning("外部事实预取超了 %d 秒预算，没回来的按没查到处理", FACTS_BUDGET_SECONDS)
    finally:
        pool.shutdown(wait=False)      # 不等没回来的：线程自己在后台收尾，主流程先往下走

    logger.info("外部事实预取用了 %.1f 秒", time.monotonic() - began)
    context.facts = "\n".join(texts.get(name, f"{name}：（这次没查到）") for name, _ in FACT_JOBS)
    return context.facts


def human_text(context: PlanContext) -> str:
    """两条路共用的人类消息：需求 + 候选点 + 已查好的外部事实 + 攻略片段。"""
    return HUMAN_TEMPLATE.format(request_text=request_text(context),
                                 candidates=candidates_text(context),
                                 facts=prefetch_facts(context),
                                 guides=guides_text(context))


def last_message_text(result) -> str:
    """从 agent.invoke() 的结果里取最后一条消息的正文。"""
    messages = result.get("messages") if isinstance(result, dict) else None
    if not messages:
        raise RuntimeError("Agent 没有返回任何消息")

    content = messages[-1].content

    if isinstance(content, list):        # 有些模型把正文拆成内容块
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part)for part in content)
    return str(content or "")


def parse_draft(text: str) -> TripDraft:
    """把模型返回的正文解析成草稿。用框架的 PydanticOutputParser，不自己抠 JSON。"""
    if not text or not text.strip():
        raise RuntimeError("模型没有返回正文（思考 token 可能把 max_tokens 吃光了）")
    return PARSER.parse(text)


def agent_draft(context: PlanContext, timeout: float) -> TripDraft:
    """① Agent 模式：模型自己决定调几次工具、怎么排（事实已经预取好，通常一轮就够）。"""
    agent = create_agent(
        model=build_model(timeout, thinking=False),
        tools=build_tools(context),
        system_prompt=SystemMessage(content=system_prompt(context, has_tools=True)),
    )
    result = agent.invoke({"messages": [HumanMessage(content=human_text(context))]})
    return parse_draft(last_message_text(result))


def single_shot_draft(context: PlanContext, timeout: float) -> TripDraft:
    """② 单次生成：不带工具，资料（候选点 + 已查好的事实 + 攻略）直接塞进提示词。

    结构化走 json_mode（这个模型支持 response_format=json_object；
    function_calling 那条路会被思考模式拒掉）。
    """
    model = build_model(timeout, thinking=False)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "{system_text}"),
        ("human", SINGLE_SHOT_TEMPLATE),
    ])
    messages = prompt.format_messages(
        system_text=system_prompt(context, has_tools=False),
        request_text=request_text(context),
        candidates=candidates_text(context),
        facts=prefetch_facts(context),
        guides=guides_text(context),
    )
    messages = trim_messages(
        messages,
        max_tokens=6000,                  # 控长度用框架件，不自己算字符预算
        strategy="last",
        token_counter=count_tokens_approximately,
        include_system=True,
        allow_partial=False,
    )
    return model.with_structured_output(TripDraft, method="json_mode").invoke(messages)


class PlanTimeout(Exception):
    """时间预算用完（3 分钟），行程没排出来。
    """


def try_stage(label: str, attempt, report, deadline: float) -> TripDraft | None:
    """跑一档，最多试 MAX_ATTEMPTS 次。
    """
    last_error: Exception | None = None
    for attempt_no in range(1, MAX_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= MIN_ATTEMPT_SECONDS:
            logger.warning("%s：时间预算用完，不再重试", label)
            raise PlanTimeout(f"{label} 用完了 {TOTAL_BUDGET_SECONDS} 秒预算"
                              f"（上一次的报错：{last_error}）")
        try:
            return attempt(min(REQUEST_TIMEOUT_SECONDS, remaining))
        except Exception as error:
            last_error = error
            logger.warning("%s 第 %d/%d 次没成：%s", label, attempt_no, MAX_ATTEMPTS, error)
            if attempt_no < MAX_ATTEMPTS:
                report("drafting", f"这次没排好，正在重试（第 {attempt_no + 1} 次）")
    logger.error("%s 试了 %d 次都不行：%s", label, MAX_ATTEMPTS, last_error)
    return None


def make_draft(context: PlanContext, request, notify=None) -> tuple[TripDraft | None, float]:
    """
    两档依次尝试，每档最多 3 次。返回 (草稿, 还剩多少秒)。
    """
    report = notify or (lambda event, message: None)

    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    report("drafting", "正在用 AI 安排每天的行程…")

    # Agent 模式：带工具那一档。时间预算用完会抛 PlanTimeout（用户看到"创建超时"），不再往下试。
    if build_tools(context):
        draft = try_stage("Agent 模式", lambda timeout: agent_draft(context, timeout),
                          report, deadline)
        if draft is not None:
            return draft, deadline - time.monotonic()
    else:
        logger.info("跳过 Agent 模式：没有可用工具，它拿不到候选点和攻略")

    # 单次生成是最后一档：成了就交出去，没成就返回 None（报错由 planner 统一发）
    draft = try_stage("单次生成", lambda timeout: single_shot_draft(context, timeout),
                      report, deadline)
    return draft, deadline - time.monotonic()


def revise_draft(context: PlanContext, draft: TripDraft, issues: list[str],
                 remaining: float) -> TripDraft | None:
    """把校验发现的问题回喂模型，让它**只改冲突的地方**，重新出一版完整草稿。
    """
    model = build_model(min(REQUEST_TIMEOUT_SECONDS, max(1.0, remaining)), thinking=False)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "{system_text}"),
        ("human", SINGLE_SHOT_TEMPLATE + REVISE_TEMPLATE),
    ])
    messages = prompt.format_messages(
        system_text=system_prompt(context, has_tools=False),
        request_text=request_text(context),
        candidates=candidates_text(context),
        facts=prefetch_facts(context),
        guides=guides_text(context),
        previous=draft.model_dump_json(by_alias=True, exclude_none=True),
        issues="\n".join(f"{index}. {text}" for index, text in enumerate(issues, start=1)),
    )
    messages = trim_messages(
        messages,
        max_tokens=6000,
        strategy="last",
        token_counter=count_tokens_approximately,
        include_system=True,
        allow_partial=False,
    )
    return model.with_structured_output(TripDraft, method="json_mode").invoke(messages)


# ==========================================================================
# 五、解说（用户点"确认"之后那一次生成）
# ==========================================================================
def make_confirm(plan: str, places: str, guides: str, weather: str,
                 timeout: float = CONFIRM_TIMEOUT_SECONDS) -> ConfirmDraft:
    """写各点介绍与出行建议。
    """
    model = build_model(timeout)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "{system_text}"),
        ("human", CONFIRM_TEMPLATE),
    ])
    messages = prompt.format_messages(system_text=CONFIRM_SYSTEM_PROMPT, plan=plan,
                                      places=places, guides=guides, weather=weather)
    messages = trim_messages(
        messages,
        max_tokens=6000,
        strategy="last",
        token_counter=count_tokens_approximately,
        include_system=True,
        allow_partial=False,
    )
    writer = model.with_structured_output(ConfirmDraft, method="json_mode")

    last_error: Exception | None = None
    for attempt_no in range(1, CONFIRM_ATTEMPTS + 1):
        try:
            return writer.invoke(messages)
        except Exception as error:
            last_error = error
            logger.warning("解说第 %d/%d 次没解析出来：%s", attempt_no, CONFIRM_ATTEMPTS, error)
    raise last_error
