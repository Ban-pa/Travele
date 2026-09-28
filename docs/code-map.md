# 项目结构与文件职责（导航页）

> 组织原则：**按功能分目录**——一个功能一个目录，功能之间只调用对方的对外入口函数。
> 数据结构跟着**产出它的功能**走：行程与草稿在 `planning/schemas.py`、召回的块在 `retrieval/schemas.py`、
> 接口层自己的请求与响应在 `api/schemas.py`。没有单独的 `contracts/` 目录，也没有加一层"共用"的必要。

## 一、完整结构（每个文件干什么）

```
Travele/
├── api/                                  【功能】HTTP 接口（后端对外的门）
│   ├── main.py                           应用装配：6 个端点 + 统一错误体 + 托管前端静态页 + 任务表(内存)
│   └── schemas.py                        接口层自己的结构：请求、决策表、进度、回执、错误体
│
├── planning/                             【功能】规划生成
│   ├── schemas.py                        行程与草稿的全部数据结构（含 WireModel 基类、枚举）
│   ├── planner.py                        ★主文件：目录 → 装配 → 草稿转成品 → 校验 → 合并 → 确认
│   ├── agent.py                          模型那一档：模型接入、提示词、两档产出与重试
│   └── tool.py                           数据源层：天气 open-meteo / 打车·自驾 高德 / 酒店 RollingGo-MCP / 航班 Aviationstack / 火车 12306-MCP（用 `@tool` 装饰，但后端自己 `.invoke()` 调，**不交给 Agent**）
│
├── retrieval/                            【功能】召回
│   ├── recall.py                         唯一检索入口：城市过滤 + bge 语义检索 + 相关度阈值
│   └── schemas.py                        DocChunk（召回来的一块攻略正文）
│
├── memory/                               【功能】长期记忆
│   └── memory.py                         建表 + 写（确认行程时）+ 渲染（给提示词）+ 历史方案列表/详情：sqlite3 一个文件
│                                         数据落在 sqlite_data/memory.db（四张表：偏好/补充说明/去过的点/历史方案）
│
├── ingest/                               【功能】离线入库（加攻略走这里，用户原本的管道）
│   ├── flie_upload.py                    Streamlit 上传页（文件名拼写是历史遗留，不改）
│   ├── knowledge_base.py                 切分 + 向量化 + 写库（metadata 含 city/source/create_time）
│   ├── city_extract.py                   从正文提取城市（文件名印证 → 开头窗口 → 词频兜底）
│   │                                     ⚠️ 名单要和 destinations.json 的 city 一字不差，否则按城市过滤会静默漏掉
│   ├── config_data.py                    入库配置：集合名、切分参数、模型路径、md5 文件位置
│   ├── corpus/                           攻略原文（22 篇：目录里 21 个城市各一篇 + 洛阳）
│   ├── chroma_db/                        ← 向量库数据（集合 rag_cosine，现在 42 块 / 22 篇）
│   ├── model/bge-small-zh-v1.5/          ← 本地嵌入模型
│   └── md5.text                          已入库内容的指纹，用来跳过重复入库
│
├── P/                                    【数据】地点照片（26 张，按地点名命名，如 西湖.jpg / 龙脊 梯田.jpg）
│                                         后端挂在 /photos/<文件名>；挂着它的那一行在 api/main.py 末尾
│
├── static/                               【功能】前端（一个页面，四个视图，无构建步骤）
│   ├── index.html                        页面骨架：手账抬头 + 索引标签 + 四个视图（首页/目的地/路线/手记）+ 页脚
│   │                                     Tailwind CDN（配色与字体在文件头内联配置）+ Google Fonts + Lucide
│   ├── app.css                           手账质感与动效（纸张/胶带/宝丽来/印章/旧地图 + 胶带浮动 + 滚动出现）
│   ├── app.js                            全部交互，四个视图共用这一份脚本（配色、图标、照片也在这里接管）
│   └── data/spots.json                   随机发现卡片的数据（21 城 26 张；首页封面照贴与「照片墙」也用它）
│                                         每条带 photo = 文件名（没图的点前端退回手绘底色）
│
└── data/
    ├── destinations.json                 目的地目录（21 城 128 点，带坐标/开放时间/常规时长；26 个点有照片）
    ├── build_catalog.py                  离线扩充目录：名字人写 → 高德 POI 检索核坐标/评分/开放时间
    │                                     （`python -m Travele.data.build_catalog [--write]`，默认只看不写）
    └── attach_photos.py                  把 P/ 里的照片按名字挂到目录与 spots.json 上（`--write` 才落盘）

docs/
├── project-design.md                     总体设计（契约、流程、取舍、里程碑）
├── code-map.md                           本文件：结构与文件职责
├── dev-log.md                            开发日志与决策记录（含踩坑）
└── examples/                             三个 wire 数据样例
    ├── planning_request.example.json     提交请求样例
    ├── tripdetail.example.json           行程返回样例（行程契约的验收基准）
    └── confirm.example.json              确认返回样例

tests/
└── full_case.py                          整条链路的完整案例检验（纯 HTTP，先起服务再跑；退出码 0 = 全过）

根目录：pyproject.toml（依赖）、uv.lock（锁定）、.venv（虚拟环境）、README.md、Travele/.env（模型配置）
```

注：项目自己的目录里**没有 `__init__.py`**（靠命名空间包工作），所以命令都要在仓库根目录执行，
模块名写成 `Travele.api.main:app` 这种完整路径。

## 二、四条主要链路（谁调谁）

**① 离线入库**
```
ingest/flie_upload.py（Streamlit 上传页）
  → ingest/knowledge_base.py::KnowledgeBaseService.upload_by_str(正文, 文件名)
      ├─ md5 去重（读 md5.text）
      ├─ 切分（RecursiveCharacterTextSplitter，正文超过 1000 字才切）
      ├─ 城市：ingest/city_extract.py::extract_city(正文, 文件名)
      └─ 写 ingest/chroma_db 的 rag_cosine 集合
```
注意：`config_data.py` 里的 `./chroma_db`、`./md5.text` 是**相对启动目录**解析的，必须先从 `Travele/ingest` 启动。

**② 召回**
```
planning/planner.py::build_context → retrieval/recall.py::retrieve(city, keywords)
  ├─ langchain-chroma 语义检索（本地 bge，查询侧加官方前缀）
  ├─ filter={"city": city} 按城市精确过滤
  └─ 相关度低于 0.25 丢掉 → 结果写进 PlanContext.docs
```

**③ 规划（只有"让模型排"一条路，排不出来就报错）**
```
api/main.py::create_trip（POST /api/trips）
  ├─ planner.check_city_available：目录里有 或 语料里有，否则 400
  └─ 后台 run_planning → 线程里跑 planner.plan(request, tripId, emit)
       ├─ build_context          目录候选点（目录没覆盖 → 从攻略正文抽）+ 召回攻略 + 需求
       ├─ agent.make_draft       ① Agent 模式（工具非空才启用）→ ② 单次生成，每档最多 3 次
       ├─ planner.to_trip_detail 按目录补坐标与规范名、按距离估交通耗时与价格、补齐时段
       └─ planner.validate       7 类规则 → 写 planMeta.validated / issues
  emit 只发 searching / drafting / validating；结尾的 done / failed 由 api/main.py 在结果存好后发
```

**④ 确认**
```
static/app.js「确认行程」 → api/main.py::confirm_trip
  ├─ api/schemas.py::ConfirmRequest   决策表（只含用户改过的节点）
  ├─ planner.apply_decisions          在"刚生成的那份"上合并：收窄方案 → 收窄交通 → 锁定
  └─ planner.build_confirm            解说 Agent 写各点介绍与出行建议（出处由后端按攻略匹配）
```

## 三、想改什么，看哪个文件

| 需求 | 文件 |
|---|---|
| 表单字段 / 校验规则 | `api/schemas.py`（`PlanningRequest` 一整套） |
| 接口路径 / 状态码 / 错误体 | `api/main.py` |
| 行程结构与草稿结构 | `planning/schemas.py` |
| 排序与交通耗时估算、时段、预算口径 | `planning/planner.py`（打车/自驾的时间来自 `planning/tool.py`） |
| 校验规则 | `planning/planner.py::validate`（13 条；`MIN_FILLED_SLOTS` 管"每个时段得有内容"） |
| 提示词、模型参数、重试次数与时间预算 | `planning/agent.py`（提速那三处：`prefetch_facts` / `THINKING_OFF` / `FACTS_BUDGET_SECONDS`） |
| 召回精度（城市过滤、阈值、取几条） | `retrieval/recall.py` |
| 入库集合、切分参数、城市识别 | `ingest/config_data.py`、`ingest/city_extract.py` |
| 页面结构（四个视图的容器、索引标签）与配色字体 | `static/index.html` |
| 手账质感与动效（纸张 / 胶带 / 宝丽来 / 印章 / 滚动出现 / 加载态） | `static/app.css` |
| 页面交互（提交 / 进度 / 切换 / 确认 / 照片墙 / 手记） | `static/app.js` |
| 随机卡片数据（也是封面照贴与照片墙的来源） | `static/data/spots.json` |
| 地点照片（挂给前端、按名字对） | 图在 `Travele/P/`，对应关系用 `data/attach_photos.py` 生成；接口挂在 `api/main.py` 的 `/photos` |
| 目的地目录（坐标 / 开放时间 / 常规时长） | `data/destinations.json`（要加/补点就跑 `data/build_catalog.py`） |
| 外部数据源（天气、打车时间、酒店、航班、火车） | `planning/tool.py`（配置在 `Travele/.env`，现在留空） |
| 模型配置 | `Travele/.env` |

## 四、名词对照表（中文 ↔ 代码名 ↔ 文件）

记不住类名时查这张表：

| 中文概念 | 代码名 | 在哪 |
|---|---|---|
| 提交的出行需求 | `PlanningRequest` | `api/schemas.py` |
| 用户改过的节点（决策表一行） | `ConfirmDecision` | 同上 |
| 确认后返回 | `ConfirmResponse` / `SpotIntro` | 同上 |
| 进度事件 / 统一错误体 | `ProgressEvent` / `ApiErrorBody` | 同上 |
| 一次规划的内存记录 | `PlanningTask` / `tasks` | `api/main.py` |
| 装配结果（规划的输入） | `PlanContext` | `planning/schemas.py` |
| 目录里的一条点（也是一个候选点） | `CatalogPlace` | 同上 |
| 行程（成品） | `TripDetail` | 同上 |
| 一天 / 早中晚某一段 | `DayPlan` / `Segment` | 同上 |
| 活动节点 | `ActivityItem` | 同上 |
| 一个地点方案 | `ActivityOption` | 同上 |
| 一种交通方式 | `TransportOption` | 同上 |
| 行程里的地点 | `Poi` | 同上 |
| 时间段 | `TimeRange` | 同上 |
| 出处（溯源） | `SourceRef` | 同上 |
| 质量标记 | `PlanMeta`（validated / issues 硬问题 / notes 后端已处理的说明 / sourceDocs） | 同上 |
| 模型产出的草稿 | `TripDraft`（连带 `Draft*` 一整套） | 同上 |
| 召回来的攻略块 | `DocChunk` | `retrieval/schemas.py` |
| 模型的两档产出 | `agent.make_draft`（Agent 模式 / 单次生成） | `planning/agent.py` |
| 所有对外结构的基类（camelCase 转换） | `WireModel` / `DraftModel` | `planning/schemas.py` |

## 五、每个功能的"主文件"（先看它，再看零件）

| 功能 | 主文件（从这里开始读） | 它拼装/暴露什么 |
|---|---|---|
| 规划生成 | `planning/planner.py` | 三个入口：`build_context` / `plan` / 确认那两步（`apply_decisions` + `build_confirm`）；自修都在 `to_trip_detail`（时段撞车 / 开放时间 / 落地时间 / 重复点） |
| 模型那一档 | `planning/agent.py` | 唯一入口 `make_draft(context, request, notify)`；外部事实并行预取 `prefetch_facts()`；规划关思考 `THINKING_OFF` |
| HTTP 接口 | `api/main.py` | 六个端点（提交 / 进度 / 取行程 / 确认 / 历史列表 / 历史详情），只做协议翻译 |
| 召回 | `retrieval/recall.py` | `retrieve(city, keywords)`、`warm_up()` |
| 离线入库 | `ingest/knowledge_base.py` | `KnowledgeBaseService.upload_by_str(text, filename)` |
| 数据结构 | `planning/schemas.py` | 行程 + 草稿 + 装配结果（`PlanContext.facts` 放并行预取的外部事实） |
| 外部数据源 | `planning/tool.py` | 五个来源 + 配置与错误（`NotConfigured` / `SourceError`）；酒店查询带缓存（同参数返回同一份名单） |
| 前端 | `static/app.js` | 文件顶部就列了十一个部分的读法；加载态（罗盘 + 纸飞机 + 四步 + 秒表）也在里面；质感在 `static/app.css`，配色字体在 `static/index.html` 头部 |

## 六、状态速览

| 已实现 | 半成品 / 占位 | 待做 |
|---|---|---|
| 入库（城市识别 + md5 去重）、召回（城市过滤 + 阈值）、装配、模型排行程（① Agent 模式带工具 / ② 单次生成，每档 3 次重试、时间预算、超时单独报"创建超时"）、草稿转成品（后端补事实、剔除目录外地点与编造的酒店、自己修时段撞车）、住宿与大交通进行程、13 类校验（含时段覆盖与不重复）、**校验不过先回喂一轮重排**、**解说 Agent**（确认后写各点介绍与出行建议）、确认合并、单页前端（**四视图 / 旧地图旅行手账 / Tailwind CDN 无构建** + SSE 进度 + 零请求切换）、统一错误体、长期记忆（sqlite3）、历史旅游方案、数据源层（五个来源，查不到就抛错） | 大交通只有结构校验（未核对）；长期记忆没有前端面板；页面掉了 CDN 就只剩内容 | 真实票价、大交通核对、评测集、落库（现在全在内存，重启即丢） |
