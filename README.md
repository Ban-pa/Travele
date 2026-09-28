# Travele · 智能旅行规划

说一句"想去哪、几天、和谁一起"，它就会召回合用的攻略、让模型排出每天的行程，
再由后端把**事实**补齐（坐标、规范名、交通耗时与价格、时段、天气、酒店）并做 13 类校验，
最后在前端呈现成一本可以就地涂改的「旧地图旅行手账」。

设计主线一句话：**模型负责创意与编排，系统负责正确性。**

> 仓库里**不含任何密钥**：模型与数据源的 key 都从 `Travele/.env` 读，
> 仓库只带一份模板 `Travele/.env.example`。

---

## 使用方式（从零到跑起来）

### 0. 环境要求

- Windows / macOS / Linux，**Python 3.12**
- [uv](https://docs.astral.sh/uv/)（依赖管理，仓库里带 `uv.lock`）
- 一个 OpenAI 兼容的模型 key（默认配置指向 DeepSeek）；Node.js 只在跑前端语法自检时才用得到

### 1. 克隆并装依赖

```bash
git clone <这个仓库的地址>
cd <仓库目录>

uv sync            # 建 .venv 并装好全部依赖
```

### 2. 下载本地嵌入模型（183MB，不进仓库）

在线检索和离线入库都用 `bge-small-zh-v1.5`，放到这个固定位置：

```
Travele/ingest/model/bge-small-zh-v1.5/
```

```bash
# 方式一：HuggingFace CLI
pip install -U "huggingface_hub[cli]"
huggingface-cli download BAAI/bge-small-zh-v1.5 --local-dir Travele/ingest/model/bge-small-zh-v1.5

# 方式二：直接从 https://huggingface.co/BAAI/bge-small-zh-v1.5 下载全部文件，
#        放进上面那个目录（国内网络可用 ModelScope 的镜像仓库）
```

### 3. 配置 key

```bash
# Windows
copy Travele\.env.example Travele\.env
# macOS / Linux
cp Travele/.env.example Travele/.env
```

然后把 `Travele/.env` 里的值填上（模板里有中文注释，逐个变量都写了用途）：

| 变量 | 必填 | 用途 |
|---|---|---|
| `TRAVELE_LLM_API_KEY` / `TRAVELE_LLM_BASE_URL` / `TRAVELE_LLM_MODEL` | **是** | 模型接入（OpenAI 兼容）。默认值指向 DeepSeek 的 `deepseek-v4-flash` |
| `TRAVELE_AMAP_KEY` | 选填 | 高德：打车/自驾的路网距离与耗时；`build_catalog.py` 扩目录点也用它 |
| `TRAVELE_AVIATIONSTACK_KEY` | 选填 | 航班班次（只有实时航班，**没有票价**） |
| `TRAVELE_ROLLINGGO_MCP_URL` + `TRAVELE_ROLLINGGO_API_KEY` | 选填 | 酒店（RollingGo 的 MCP 服务，HTTP 方式） |
| `TRAVELE_RAIL_MCP_CMD` / `TRAVELE_RAIL_MCP_URL` | 选填 | 火车（12306 的 MCP，默认 stdio；`uv sync` 已装好 `mcp-server-12306`） |

**选填的留空也能跑**：那一路查不到就如实说"没查到"，不估算顶替。
**只有模型那三行是硬要求** —— 没填的话，提交需求时会被直接拦下并告诉你缺什么。

### 4. 建一次向量库（第一次使用必做）

仓库里带的是攻略原文（`Travele/ingest/corpus/` 22 篇），向量库本身是构建产物，没有进仓库：

```bash
cd Travele/ingest
../../.venv/Scripts/python.exe -m streamlit run flie_upload.py      # Windows
# 打开页面上传 corpus 里的 txt（或直接把整批文件传上去），入库完成后再回来
cd ../..
```

入库会：切分 → 本地 bge 向量化 → 写进 `chroma_db`（集合 `rag_cosine`）→ 从正文识别城市写进 metadata → 按 md5 跳过重复内容。

### 5. 起服务并打开

```bash
# 必须在仓库根目录：模块路径是 Travele.api.main:app
.venv/Scripts/python.exe -m uvicorn Travele.api.main:app --host 127.0.0.1 --port 8099
```

打开 <http://127.0.0.1:8099/>

- 四个视图：`#/home` 首页 · `#/discover` 目的地 · `#/plan` 路线 · `#/history` 手记
- 启动时会先在后台线程里预热嵌入模型（本地 bge 第一次加载要十几秒），失败不拦启动
- 排一次行程实测 **十几秒到四十多秒**（进度条与秒表会一路显示到完成）

### 6. 自测（可选）

```bash
# 另开一个终端，先起服务再跑；纯 HTTP、不 import 项目代码，退出码 0 = 全过
.venv/Scripts/python.exe -u tests/full_case.py
```

覆盖：静态页与资源 → 提交 202 → SSE 结尾必须是 `done` → 取行程（大交通 / 住宿 / 必去点 / 机票无票价）
→ 本地改一个节点 → 确认 + 解说（介绍带出处）→ 改过的节点被收窄并锁定 → 历史与详情一致 → 错误分支（400 / 404 / 422）。
跑完会往长期记忆里写一条（历史里多一条），不想要就删掉 `Travele/sqlite_data/memory.db`。

---

## 功能

| 视图 | 做什么 |
|---|---|
| 首页 `#/home` | 手账封面：标题、随机 6 张照贴、最近 3 条手记入口 |
| 目的地 `#/discover` | 一次看一个目的地（可换、一轮内不重复），下面是 26 张宝丽来照片墙，点任意一张就把省市填进路线那页 |
| 路线 `#/plan` | 填需求 → 生成行程 → 就地换地点/换交通 → 确认 → 各点介绍与出行建议 |
| 手记 `#/history` | 确认过的整份行程（只读），点开看完整那一版 |

- **攻略召回**：按城市从本地向量库检索攻略片段（余弦度量 + 0.25 相关度阈值）。
- **行程生成**：Agent 模式（模型可自己调工具）→ 单次生成，两档各最多 3 次。
- **就地改**：换地点、换交通都是本地切换，**零请求**（数据在前端已全量预置）。
- **解说**：确认后由模型写各点介绍与出行建议，「出处」由后端按攻略正文匹配，不让模型编。
- **长期记忆**：偏好（同一条出现 ≥2 次才升级为画像）、补充说明、去过的点 —— 确认行程时写，规划时读进提示词。
- **地点照片**：`Travele/P/` 下 26 张图，按地点名挂到照片墙、随机卡、行程节点与各点介绍上。

## 技术栈

| 层 | 用了什么 |
|---|---|
| 后端 | Python 3.12、FastAPI、Pydantic v2（对外 camelCase / 内部 snake_case）、uvicorn、SSE |
| 模型编排 | LangChain：`init_chat_model`、`create_agent`、`PydanticOutputParser`、`ChatPromptTemplate`、`trim_messages` |
| 检索 | langchain-chroma + langchain-huggingface（本地 `bge-small-zh-v1.5`） |
| 离线入库 | `Travele/ingest/`：Streamlit 上传页 + 递归切分 + 向量化 + md5 去重 + 从正文识别城市 |
| 前端 | 纯静态 HTML/CSS/JS，**无构建步骤**；Tailwind CDN + Google Fonts + Lucide |
| 存储 | 长期记忆用标准库 `sqlite3`；正在规划的任务只在内存 |
| 依赖管理 | uv（`pyproject.toml` + `uv.lock`） |

## 目录结构

```
Travele/
├── api/          HTTP 接口：main.py（6 个端点 + 统一错误体 + 托管前端 + /photos）、schemas.py（请求/决策表/进度/错误/历史方案）
├── planning/     规划生成：planner.py（★主文件）、agent.py（模型那一档 + 外部事实并行预取）、schemas.py（数据结构）、tool.py（外部数据源）
├── retrieval/    召回：recall.py（城市过滤 + bge 语义 + 阈值）、schemas.py（DocChunk）
├── memory/       长期记忆：memory.py（sqlite3：偏好 / 补充说明 / 去过的点 / 历史方案）
├── ingest/       离线入库：flie_upload.py（Streamlit 上传页）+ knowledge_base.py + city_extract.py + config_data.py
│                 + corpus/（攻略原文 22 篇）+ chroma_db/（向量库，构建产物）+ model/（本地 bge，需自行下载）
├── static/       前端：index.html + app.css + app.js（一份脚本管四个视图）+ data/spots.json
├── P/            地点照片（26 张，按地点名命名），后端挂成 /photos/<文件名>
├── data/         目的地目录 destinations.json + 两个离线段脚本（build_catalog.py / attach_photos.py）
└── .env.example  配置模板（复制成 .env 再填；.env 不进仓库）
```

项目自己的目录里**没有 `__init__.py`**（靠命名空间包工作），所以命令都要在仓库根目录执行。

## 数据

| 数据 | 位置与规模 | 进仓库？ |
|---|---|---|
| 攻略语料 | `Travele/ingest/corpus/`，22 篇（目录里 21 个城市各一篇） | 是 |
| 向量库 | `Travele/ingest/chroma_db`，集合 `rag_cosine`，42 块 | 否（跑一次入库生成） |
| 本地嵌入模型 | `Travele/ingest/model/bge-small-zh-v1.5`，183MB | 否（按第 2 步下载） |
| 目的地目录 | `Travele/data/destinations.json`，21 城 128 点（坐标 / 开放时间 / 常规时长 / 评分） | 是 |
| 随机卡片 | `Travele/static/data/spots.json`，21 城 26 张（照片墙与封面照贴也用它） | 是 |
| 地点照片 | `Travele/P/`，26 张 | 是 |
| 长期记忆 | `Travele/sqlite_data/memory.db` | 否（第一次确认行程时才建） |

三件事各自怎么加：

```bash
# ① 加攻略：txt 丢进 Travele/ingest/corpus/，再从 ingest 目录起上传页入库
#    （config_data.py 里的 ./chroma_db、./md5.text 是相对启动目录解析的，必须从那儿起）
cd Travele/ingest && ../../.venv/Scripts/python.exe -m streamlit run flie_upload.py
#    入库后重启后端（向量库句柄缓存在进程里）

# ② 加目的地：编辑 Travele/data/destinations.json，或用脚本按"人写名字 + 高德核准坐标"扩
.venv/Scripts/python.exe -m Travele.data.build_catalog            # 只看结果
.venv/Scripts/python.exe -m Travele.data.build_catalog --write    # 写回

# ③ 加照片：图丢进 Travele/P/，**文件名写成地点名**（如 西湖.jpg），再同步到两份 json
.venv/Scripts/python.exe -m Travele.data.attach_photos --write
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/trips` | 提交需求 → 202 + `tripId`，后台线程开始规划 |
| GET | `/api/trips/{tripId}/events` | SSE 进度：`searching / drafting / validating / revising / done / failed` |
| GET | `/api/trips/{tripId}` | 取行程（`TripDetail`；还在规划时 `days` 为 null，刷新不丢） |
| POST | `/api/trips/{tripId}/confirm` | 提交决策表（只含用户改过的节点）→ 各点介绍 + 出行建议 |
| GET | `/api/plans` | 历史方案列表 |
| GET | `/api/plans/{planId}` | 历史方案详情（存下来的整份行程） |

错误统一返回 `{code, message, detail?}`：`VALIDATION_FAILED`(422) / `CITY_NOT_FOUND`(400) / `TRIP_NOT_FOUND`(404) / `INTERNAL_ERROR`(500)。

## 外部数据源（`planning/tool.py`）

| 来源 | 干什么 | 备注 |
|---|---|---|
| open-meteo | 那几天的天气 | 免费、不用 key，但需要坐标 |
| 高德 | 打车 / 自驾的路网距离与耗时；目录扩点时的 POI 检索 | key 从 `.env` 读 |
| RollingGo（MCP） | 酒店名字、地址、星级、每晚价与预订链接 | 同一条件两次查询结果可能不同，所以按城市+日期+人数缓存 |
| Aviationstack | 航班班次与起降时刻 | **没有票价**，入参是机场三字码 |
| 12306（MCP） | 火车车次 | 站名写中文站名 |

## 设计要点

**模型与后端的分工**

| 谁 | 做什么 |
|---|---|
| 模型 | 选哪些点、一天排几个、先后顺序、每天主题、每个方案的推荐理由、交通方式（只说 `method`） |
| 后端 | 坐标与规范名（按目录）、交通耗时与价格（按距离或高德）、时段、剔除目录外的地点、13 类校验、解说的「出处」 |

模型编不出坐标，也就不会出现"坐标中途丢掉、可达性算不出来"；耗时不给模型改的机会，
"写着 1 分钟、其实十几公里"这类数就没有来源。

**校验与修：分层，能自己修的别麻烦模型**

1. **后端自修**（不花模型调用）：时段撞车、开放时间放不下（先整体前移再压时长）、
   落地前不排活动、同一天重复同一个点、排到半夜（一天硬边界 23:59）。
2. **回喂重排**：把"上一版草稿 + 问题清单"还给模型，只回喂一轮，且只在问题数真的变少时采纳。
3. **如实标注**：还不过就写进 `planMeta.issues`（行程照给）；后端已经处理掉的写 `planMeta.notes`，两者分开显示。

**为什么快**

| 层 | 做法 |
|---|---|
| 外部事实 | 天气 / 酒店 / 车次**并行**预取（线程池，整体 25 秒预算），模型不必自己一轮轮调工具 |
| 模型 | 规划这条路关掉思考（`extra_body={"thinking": {"type": "disabled"}}`）：排行程是"照清单和规则填 JSON"，事实由后端补、校验兜底；解说保留思考 |
| 校验 | 能自己修的不触发回喂，省下一整轮生成 |

**如实标注**

- 大交通的时刻与班次**未跟车站/机场核对**，一律标"未核对"。
- **机票票价不提供**：没有可靠来源，后端不给模型猜的数，前端写"票价无可靠来源"。
- 语料是演示用示例语料；目录里少数点是高德 POI 检索结果（个别可能是景区子点位）。
- 长期记忆不做前端面板（它是给 Agent 看的）；正在规划的任务只在内存，重启即丢，但**确认过的行程在历史里**。

## 注意事项

- **命令都在仓库根目录执行**（没有 `__init__.py`，靠命名空间包）。
- **入库必须从 `Travele/ingest` 启动**，入库后重启后端。
- **向量库集合必须余弦度量**（`hnsw:space=cosine`）：在线检索用"1 - 距离"当相关度，L2 下这个算法不成立。
- 模型的 `tool_choice` **不能强制**（思考模式会被服务端拒），所以结构化输出走 `response_format=json_object` + Parser。
- 前端**无构建 + CDN**：离线打开会掉样式（只剩内容）。改了 `app.css` / `app.js` 记得把
  `index.html` 里的 `?v=` 版本号 +1（后端另加了 `Cache-Control: no-cache`，双保险）。
- 长期记忆库 `Travele/sqlite_data/memory.db` **不是预置的**：第一次"确认行程"时才建；
  想回到全新状态，把这个文件删掉即可。

## 上传 / 分享这个仓库时的注意

`.gitignore` 已经写好，**密钥、183MB 模型、向量库、记忆库都不会被提交**：

| 会进仓库 | 不会进仓库 |
|---|---|
| `Travele/**`（代码、语料、目录、前端、照片、`.env.example`）、`tests/`、`docs/`、`README.md`、`pyproject.toml`、`uv.lock`、`.gitignore` | `Travele/.env`（key）、`.venv/`、`.uv-cache/`、`.idea/`、`Travele/ingest/model/`、`Travele/ingest/chroma_db/`、`Travele/ingest/md5.text`、`Travele/sqlite_data/*.db` |

第一次上传：

```bash
git init
git add -A
git status                 # 扫一眼有没有不该进的文件（.env 应该不在列表里）
git commit -m "Travele: 智能旅行规划（旧地图旅行手账）"
git branch -M main
git remote add origin <你的仓库地址>
git push -u origin main
```

- 上传前可以再自查一遍：`git status --porcelain | Select-String "\.env"` 应该只看到 `.env.example`。
- **别用"把整个文件夹压缩再上传"这种方式**：压缩包会绕过 `.gitignore`，把 `Travele/.env`（key）
  和 183MB 的模型一起带进去。要么走 `git push`，要么打包时手动排除 `.env`、`.venv/`、`.uv-cache/`、
  `Travele/ingest/model/`、`Travele/ingest/chroma_db/`、`Travele/sqlite_data/`。
- 如果曾经把 key 提交过，**光删文件没用**（历史里还在）：要么改 key，要么重写历史
  （`git filter-repo`），最省事的是删掉仓库重传。

## 文档

| 文件 | 内容 |
|---|---|
| `docs/code-map.md` | 结构与文件职责（改什么看哪个文件） |
| `docs/project-design.md` | 总体设计：契约、流程、校验规则、技术选型、里程碑 |
| `docs/dev-log.md` | 决策与踩坑记录 |
| `docs/examples/*.json` | 请求 / 行程 / 确认三份 wire 数据样例 |
