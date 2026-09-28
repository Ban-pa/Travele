# 开发日志（决策记录）

> 关键决策、踩坑、理由都记在这里，与 project-design.md 配套。
>
> 说明：下面按时间**倒序**记，越靠上越新。早期条目里提到的 `planning/fallback.py`、
> `planning/draft.py`、`agent_tools.py`、`interface/`、`state/`、`contracts/` 这些文件，
> 在后来的重组里已经没有了 —— 当前结构以 `code-map.md` 为准，历史条目只当决策记录看。

## 把 P 文件夹里的照片按名字挂到前端（2026）

- `Travele/P/` 里 26 张图，文件名就是地点名（`西湖.jpg`、`泰山.jpg`、`龙脊 梯田.jpg`、`故宫jpg.jpg`）。
  **按名字对**：去扩展名、去空格、去掉文件名里混进来的 `jpg` 尾巴，再精确匹配；
  匹配不上退一步互相包含（`长城` ↔ `八达岭长城`）。26 张**全部命中**，
  两边数据都写上了 `photo` 字段：`static/data/spots.json`（26/26）与 `data/destinations.json`（26/128）。
- 新增 `Travele/data/attach_photos.py`（可重跑：默认只看不写，`--write` 才落盘）。
  对不上的地方**不动**：前端没有 `photo` 就退回原来的手绘底色，所以永远不会出现破图。
- 图片怎么给前端：图**不搬家**，后端把 `Travele/P` 挂成 `/photos/<文件名>`（`api/main.py` 末尾一行），
  前端用 `<img src="/photos/西湖.jpg">`。文件名带中文和空格，前端统一 `encodeURIComponent`。
  顺手注册了 `image/webp`（这台机器的 mimetypes 不认，之前 webp 是按 `application/octet-stream` 发的）。
- 挂在哪几处：① 目的地页的**照片墙**（26 张宝丽来）；② 首页**封面照贴**；
  ③ 目的地页的**随机大卡**（大图）；④ **行程里每个方案**左边一张 56×56 小图；
  ⑤ **确认后的各点介绍**左边一张 64×64 小图。都带 `loading="lazy"`（随机大卡除外），
  统一压一点饱和加一点点 sepia，像夹在册子里的旧相片。
- 踩到两个坑，记下来：
  1. 随机大卡那张图的容器 `#visual` **里面还有省市标签和地点名**，一开始图省事写了
     `visual.innerHTML = "<img>"` —— 直接把它们冲掉了。改成 `insertAdjacentHTML("afterbegin")`
     （先把上一张 `.photo-img` 删掉），标签与地点名靠 z-index 压在照片上面。
  2. 回归时抓到一个**会打崩整单规划**的老 bug（跟照片无关，这次才被撞出来）：
     晚场节点收得晚 + 路上时间，会把下一段的开始时间顶到 `24:32`，而 `TimeRange` 的 HH:MM
     正则不接受 —— ValidationError 一出，整单规划就失败。修法：一天硬边界 `DAY_LAST_MINUTE = 23:59`，
     能压就压到 23:59，压不出 45 分钟就整段去掉并记一条说明。
- 验证：`/photos/西湖.jpg`、带空格的 `龙脊 梯田.jpg`、`丽江古城.webp`、`九寨沟.jpeg` 全部 200
  且 content-type 正确（不存在的图 404、不是 500）；前端 id 覆盖 64/64、JS 类名都有样式、`node --check` 过；
  `tests/full_case.py` 39 项断言全过。
- **用户反馈"没生效、依旧没有图片"—— 是浏览器缓存，不是代码**。查证据的办法很直接：
  看后端的访问日志。用户那次会话只有 `GET / → 304` 和一堆 `/api/plans`，
  **一次 `/photos/` 都没请求**，连 `app.js` / `app.css` 都没再来取 —— 说明浏览器拿的是缓存里的旧脚本，
  那份脚本里根本没有照片代码。两层修好：
  ① `index.html` 引用改成 `app.css?v=3` / `app.js?v=3`，`app.js` 里 fetch 改成 `spots.json?v=3`
  （改了前端文件就把版本号 +1，新地址必然绕开旧缓存）；
  ② 后端加了个中间件，给 `.html/.css/.js/.json` 和 `/` 加 `Cache-Control: no-cache, must-revalidate`，
  以后浏览器每次都会回来问一句（没变照样 304，不亏速度）。
  顺手给所有 `<img>` 加了 `onerror="this.remove()"`：图真取不到时留下手绘底色，而不是一个破图图标。
  这条已经写进 README 的"必须知道的坑"。
- 顺带修一处边界：去程落地时间**正好等于**某个时段窗口的上界（比如 12:00 落地、上午窗口到 12:00）时，
  原来只判"晚于"会留下这一段，排出来变成"上午 12:09 开始"，跟时段对不上。改成 `arrival >= 窗口上界` 就去掉这一段。

## 提速：并行预取 + 关掉思考 + 后端多修几类问题（2026）

- 用户反馈"运行时间太长"，要求加加载动画、工具并行执行。先量，再改 —— 量出来的结论跟直觉不一样：
  | 量的东西 | 结果 |
  |---|---|
  | 五个外部工具 | 串行 4.9 秒、并行 1.2 秒（**工具本身根本不是瓶颈**） |
  | 一次生成的 token | 输入 4.6k（缓存命中 4.4k）、输出 **22.6k**，其中 **19.6k 是 reasoning（思考）** |
  | 一次生成的耗时 | 101.6 秒 = 88 秒在思考、十几秒在写 JSON（正文只有 3k token） |
  | 两档生成 | 单次生成 176 秒 / Agent 模式 129 秒（噪声很大，谁快谁慢看运气） |
- 所以真正的三处提速：
  1. **外部事实并行预取**（`agent.prefetch_facts`）：天气 / 酒店 / 车次互不依赖，丢进线程池一起查，
     整体预算 25 秒（没回来的按"没查到"处理，`pool.shutdown(wait=False)` 不等它）。
     更要紧的是**省掉模型自己调工具的那几轮** —— 每轮工具调用就是一次完整的生成，实测二三十秒。
     查好的文本进 `PlanContext.facts`，两条路和回喂那一轮共用，不重复查。
  2. **规划这条路关掉思考**：`extra_body={"thinking": {"type": "disabled"}}`（`agent.THINKING_OFF`）。
     排行程本质是"照候选点清单和规则填 JSON"，事实由后端补、13 条校验兜底，不需要它想 20k token。
     实测同一个请求 **100 秒 → 15 秒**；解说不关（那是组织语言的活，输出短、本来就快）。
  3. **后端多修几类问题，少触发回喂那一轮**（回喂一轮要十几秒，还未必改得动）：
     时段撞车（原有）、开放时间（`_fit_open_hours`）、**落地时间**（去程 13:00 才到，第一天上午就别排了）、
     **重复排同一个点**（同一天两次、整趟超过 `MAX_PLACE_REPEAT = 2` 次的直接去掉并记说明）。
- 加载动画（前端）：进度面板里放了**罗盘在转 + 纸飞机沿虚线飞 + 四步指示（翻资料/排行程/做检查/再调整，
  跟着 SSE 事件点亮）+ 秒表**；`prefers-reduced-motion` 下全部不动、纸飞机摆中间。
- 关掉思考的代价，如实记：模型变"糙"了一点 —— 实测出现过同一天排两次同一个点、把 18:00 关门的点排到
  18:00 才开始、点名要"地铁/船"这种这边算不出耗时的方式。前两类已经由后端自修兜住（现在是
  `validated=True` + 一条说明），后一类汇总成一条说明（以前是逐条，六七条会把说明区刷屏）。
  想换回"慢慢想"的版本：把 `make_draft / agent_draft / single_shot_draft / revise_draft`
  里的 `thinking=False` 去掉即可。
- 实测结果（真跑，同一批请求）：
  - 西安 3 天：78~217 秒 → **29.6 秒**，`validated=True`，8/9 个时段有内容（第一天上午空=13:00 才落地）
  - 泰安 3 天：117 秒 → **18.3 秒**，`validated=True`，8/9 个时段
  - `tests/full_case.py`：**39 项断言全过**，规划 26.6 秒、解说 9.4 秒

## 修"一天里只剩上午或下午有内容"（2026）

- 用户报的问题：经常出现一天只有上午或下午有安排，其他时段空着。先**量**了一遍：拉出历史里 7 条已确认方案，
  逐天逐时段数"有没有内容"，规律很清楚 ——
  ① 第 1 天上午几乎总是空（到达日，合理）；② **薄目录城市整段甚至整天空**（西安第 3 天全空、
  北京第 2/3 天只剩上午）；③ 同一天同一个地点排两次（"古城 下午 + 古城 晚上"）。
- 根因三个，缺一个都解释不通：
  1. **数据层**：`destinations.json` 只有 36 个点 / 21 城，**其中 15 个城市只有 1 个点**。
     三天是 9 个时段，一个城市只有 1~2 个候选点，怎么排都填不满 —— 模型只能空着，或者把同一个点重复排。
  2. **提示词**：系统提示词里明写"排不下就把那一段留空"，等于给了偷懒的许可。
  3. **校验**：规则1 只检查"三个时段容器在不在"，**不检查容器里有没有内容** —— 交白卷也能 `validated=True`。
- 四处修：
  1. **目录扩容**（新增 `Travele/data/build_catalog.py`）：**名字由人写**（和攻略正文的叫法一致，这样
     "引用攻略"能对上），**坐标 / 评分 / 开放时间问高德 POI 检索要**（不手写坐标、不猜）。
     匹配规则：名字互相包含 + 类型只认"景点 / 科教文化 / 博物馆"（不然会挑到"XX村委会""XX码头"）
     + 同分取名字最短的 + 排除酒店餐馆公司 + 街道夜市类放宽类型。36 → **128 个点，每城 5~8 个**；
     高德没匹配上的 2 个（九寨沟的黄龙、海西的水上雅丹，都不在本市县境内）如实跳过。
  2. **提示词**：删掉"留空"那句，改成"每天三段都要有安排"，并给出排不满时的三级办法
     （① 同一片区域的两个点拆到上午/下午 ② 大景区按半天算 + 附近小点 ③ 真的在赶路才留空，
     并在那一段的 unverified 里写原因）；再加两条："同一天不重复同一个地点""整趟同一个点最多 2 次，
     候选点尽量都用上"。
  3. **校验 +3 条**（10 → 13）：规则11 时段覆盖（中间天至少 `MIN_FILLED_SLOTS = 2` 段有内容，首末天至少 1 段）、
     规则12 同一天同一个地点最多一次、规则13 整趟同一个地点最多 2 次。不达标就进"回喂重排"那一轮。
  4. **后端自修开放时间**（`_fit_open_hours`）：密度上来之后，"16:26 开始、闭馆 18:00"这类冲突会变多，
     不能拿它把整份行程判成不通过 —— 先整体往前挪（不早于上一段结束），挪不动再压游览时间，
     压到 45 分钟以下才留给校验报错。
- 顺带修两处（都是这轮真跑时撞出来的）：
  1. **新扩的目录点在攻略里没写到**，解说的"出处"会变成 null —— 现在如实写 **"目的地目录"**
     （事实确实来自目录：类型/评分/地址/开放时间），前端那行"（出处：X）"不再是空的。
  2. **`planMeta` 把"硬问题"和"后端已处理的说明"分成两个字段**：`issues` 只放校验规则没过的（影响 `validated`、
     才值得回喂一轮），`notes` 放"模型编的酒店被剔除""要的交通方式没有改用别的"这类已经替你处理掉的事。
     之前它们混在一个数组里，一份没毛病的行程也会显示"校验未通过"，还白花一轮回喂。前端多一块淡蓝色说明区。
  3. **酒店查询加缓存（真 bug）**：RollingGo 同一个条件连查三次，**每次返回的 5 家都不一样**（像随机采样）。
     而酒店会被问两遍：Agent 里模型自己调一次、后端 `resolve_lodging` 核对明细时又调一次 ——
     结果就是"模型从第一次的列表里挑了家酒店，后端拿第二次的列表核对说没有，把住宿整段剔掉"。
     实测复现：连查三次，只有 3 家是三次都有的；`大理晏清山居客栈` 第一、三次有、第二次没有。
     修法：`tool.hotels` 按"城市 + 日期 + 人数"缓存（TTL 10 分钟，防价格放太久），同一次规划里两次拿到同一份列表。
- 验证（都是真跑）：
  - **西安 3 天**：3/9 → **7/9** 个时段有内容（第 1 天上午空 = 上午才落地、第 3 天晚上空 = 要赶返程，都合理）；
    这一次 `validated=False` 是另一件事：模型编了个查不到的酒店名，被如实剔除并记了一条问题。
  - **泰安 3 天**（改之前只有 1 个候选点）：第一版 9/9 但把"天外村/红门/岱庙"来回转 → 加了规则13 之后
    **7/9、无重复超限、`validated=True`**，每个点最多出现 2 次。
  - 目录核对：128 点、每城 5~8 个、无同城重名、无坐标缺失或越界。
  - `tests/full_case.py`：修出处那一处断言后重跑通过。
- 取舍记一笔：`MIN_FILLED_SLOTS` 定 **2** 而不是 3。硬要"每段都填"，模型就会拿"回酒店休息/自由活动"
  或者反复重复几个点来凑数 —— 那比空着更难看。首末天各允许空一段，中间天至少两段，是既不空又不像凑数的线。
  要更严就改这一个常量（`planner.py` 顶部）。

## 前端改版：旧地图旅行手账（2026）

- 用户给的风格提示词是"旧地图旅行手账"（复古、纸张纹理、宝丽来、胶带、邮票、印章、旧地图、手写字体；
  牛皮纸黄 / 墨绿 / 砖红 / 褪色蓝；Tailwind CDN + Google Fonts + Lucide；纸张轻转、胶带浮动、滚动出现），
  并要求**内容和导航换成项目自己的**。所以提示词里的"邮票收集""订阅 CTA"没有真实内容，不做；
  改成真有的东西 —— 照片墙用 `spots.json` 的真实目录、CTA 是"去排行程"、
  印章用后端的"校验通过 / 引用攻略"、页脚写数据来源与如实说明。
- 导航从"侧边栏三视图"改成**本子边上的索引标签四视图**：
  首页(#/home，封面标题 + 6 张照贴 + 最近 3 条手记) / 目的地(#/discover) / 路线(#/plan) / 手记(#/history)。
  新增的只有"首页"这一个视图，它用的全是已有数据（`spots.json`、`GET /api/plans`），没有新接口。
- 之前按用户要求"去掉全部目的地"，这次提示词里明确有"宝丽来照片墙"，所以把它做回**目的地页的下半部分**：
  26 张宝丽来，点任意一张就把省市填进路线那页的表单。不想要就删 `#spotWall` 那段（一个容器 + 两行 JS）。
- 质感全在 `static/app.css`：纸纹与旧地图等高线是**内联 SVG（feTurbulence + 椭圆）**，不引外部图片；
  胶带用 clip-path 扯边、宝来用白框加轻微旋转、印章是双描边方章。动效三个：胶带浮动（`@keyframes tapeFloat`）、
  随机卡与照片墙 hover 转正、滚动出现（IntersectionObserver；`prefers-reduced-motion` 下全关）。
  没有真照片，照片底用 CSS 变量 `--tint` 给四族色的褪色渐变，同一个地名每次一样。
- **逻辑一行没改**：`VIEW_NAMES` 加 `home`，其余（表单→请求体、SSE 进度、本地零请求切换、决策表、解说、
  手记只读）照旧；给用户看的文案也照旧（"未核对""票价无可靠来源，未提供""本轮第 N 个"…）。
- 怎么验的（没有浏览器，所以三层都垫上）：
  ① `node --check static/app.js` 过；自写脚本查 id 覆盖（JS 引用的 59 个 id 全在 HTML 里）、
     JS 拼出来的 43 个类名全在 app.css 里有样式、HTML 引用的本地资源都在；
  ② 写了个极简 DOM 桩在 node 里真跑 `app.js`：启动流程 + `spots.json` 渲染 + 行程渲染（含"未核对"、
     机票不给价、锁定节点、"未安排"时段）+ 解说卡片，**24 项断言全过**（桩与断言用完即删）；
  ③ 起真服务：`/`、`/app.js`、`/app.css`、`/data/spots.json` 都 200，页面里 Tailwind / 三个字体 / Lucide /
     四个 `data-view` 都在；`tests/full_case.py` **39 项断言照旧全过**（规划 85.3 秒、确认 6.2 秒）。
- **顺手抓到一个把提交接口打成 500 的真 bug**（不是这次前端改出来的）：
  `api/schemas.py` 的 `Schedule.trip_start_cnt_tolong` 写的是 `if self.trip_end-self.trip_start > 7`，
  timedelta 和 int 比较会抛 TypeError → 任何带日期的 `POST /api/trips` 都 500（先撞在 full_case 的 ②）。
  改成 `(self.trip_end - self.trip_start).days > 7`。语义注意：现在等于"最多 8 天（跨 7 晚）"，
  要严格 7 天就写 `.days + 1 > 7`。
- 已知限制：CDN 掉了页面只剩内容（无构建步骤的代价，接受）；视觉最终还得人眼过一遍。

## 把攻略语料补满：21 个目录城市都有了（2026）

- 用户要求"多弄几个攻略入向量库"。新增 18 篇 `Travele/ingest/corpus/*.txt`
  （西安、杭州、厦门、丽江、桂林、张家界、敦煌、拉萨、三亚、苏州、黄山、泰安、大同、
  香格里拉、九寨沟、黔东南、阿勒泰、海西），加上原有的 4 篇（北京、成都、大理、洛阳），
  现在是 **22 篇 / 42 块 / 22 个城市标签** —— `destinations.json` 里的 21 个城市**每个都有语料**。
  写法沿用原语料的样子：一~九节（总体 / 主体景点 / 周边 / 吃 / 季节穿衣 / 交通接驳 / 出行提示），
  结尾标"（示例语料）"；每篇 1000~1200 字，正好切成 2 块（切分 1000 / 重叠 100）。
- **入库前先修了一个会让新语料"进得去、召不回"的坑**：`city_extract.CITIES` 里缺 6 个目录城市名
  （大同、敦煌、泰安、黔东南、阿勒泰、海西）。少了它们，元数据会被打成邻居城市或空标签 ——
  泰安→济南、大同→太原、黔东南→凯里、阿勒泰→乌鲁木齐、海西→西宁；而在线检索是
  `filter={"city": 目录里的城市名}` **精确过滤**，标签一对不上就永远召不回来（还不会报错）。
  补进名单，并在注释里写明"要和 `destinations.json` 的 city 一字不差"。
  入库后逐篇核对了识别结果：22 篇全部识别正确。
- 实测（`retrieve` 真调用）：西安 0.60/0.54、泰安 0.65/0.60、海西 0.56/0.47、黔东南 0.46/0.42，
  都高于 `MIN_SCORE=0.25`；每篇两块的切分也合理（前半是总体与主景点、后半是季节交通与提示）。
- 端到端验了一遍**新城市**（西安）：SSE 第一屏就是"找到 2 篇相关攻略"，
  `planMeta.sourceDocs = ["西安攻略.txt"]`，解说的两条介绍出处也都是 `西安攻略.txt` ——
  语料确实进了装配上下文和解说 Agent，不只是"库里有"。
- 顺带发现两件事，如实记：
  ① 解说这次撞上 `ConfirmDraft from completion null`（json_mode 返回空），接口按设计回了
     500 + "生成各点介绍时出错了，请再试一次"，**再点一次就成功了** —— 这是"内容生成不做兜底"的代价。
     既然方案里本来就写了"输出层解析重试"，就在 `agent.make_confirm` 里加了最多两次的**重新问一遍**
     （不是拿别的内容兜底：两次都失败照样抛出去）。
  ② 西安的目录里只有 3 个点（兵马俑、城墙、大雁塔），3 天行程排出来只有 3 个活动节点、第 3 天是空的。
     原因在 `build_context`：`candidates = from_catalog or extract_places(...)` ——
     目录里有点就**不再合并**从攻略里抽的点。想排满得往 `destinations.json` 补点（华清宫、陕历博、回民坊…），
     这是数据问题，不是代码问题。
- 另一个坑值得记：8099 上还挂着我上一轮留下的旧后端（它启动早于这次入库，内存里是旧快照）。
  **改完语料要重启后端**，否则新语料在旧进程里看不见。已杀掉重启，当前 8099 是新进程。

## 整条链路的完整案例检验（2025）

- 用户要求：整条链路跑一遍完整案例。固化成一个可重复的脚本 `tests/full_case.py`
  （纯 HTTP，不 import 项目代码；先起服务再跑，退出码 0 = 全过），覆盖：
  静态页 → 提交 202 → SSE（结尾必须 done）→ 取行程（大交通/住宿/必去点/机票无票价）
  → 用户改一个节点 → 确认 + 解说（介绍带出处、建议非空）→ 改过的节点被收窄并锁定
  → 历史列表/详情与行程一致 → 错误分支（未知城市 400 / 未知历史 404 / 坏决策 422 / 空决策 422 / 缺字段 422）。
- **第一次跑就抓到一个真问题**：规划 31.8s 失败 → 66.9s 失败 → 第三次撞到 3 分钟预算报"创建超时"。
  看日志才明白：模型调了 `train_tickets`（沙箱里起不了子进程，WinError 5），**工具异常把整轮 Agent 打崩**，
  而之前"用 `handle_tool_error=True` 兜住"的结论是**我当时没验证就写下的错话**——`create_agent` 这一版不看这个字段。
  真修：`agent._tolerant()` 自己包一层（成功 `str(结果)` 给模型、失败把原因当工具输出），
  并且从日志能确认它在工作：`工具 train_tickets 这次没成（错误已还给模型）：…`，模型收到后继续排。
- 顺带验了失败路径的 SSE：拿"模型地址指向死端口"的服务器跑，事件流照常、结尾 `failed` 送达、流正常关闭（29.7 秒）——
  说明"创建超时/排不出来"这类失败用户是收得到的（之前那次"十分钟没输出"是我命令超时被 kill + Python 输出缓冲造成的假象）。
- 最终结果：**39 项断言全过**。规划 43.3 秒（`validated=True`、0 问题）、住宿 1 家带真价格、
  大交通 2 段（飞机价格为空、标"未核对"）、必去点已排入、确认 7.1 秒出 4 条介绍（都带出处）+ 5 条建议、
  改过的节点被收窄成 1 个并锁定、历史列表与详情一致、6 条错误分支全对。

## 划掉五件事：哪些是"有意不做"（2025）

- 用户明确表态，这五条不是"没做完"，是想清楚不做：
  ① **大交通不跟车站/机场核对** —— 个人项目里做不到"完美"（机票只有实时航班），如实标"未核对"比假装准确好；
  ② **机票票价不实现** —— 没有可靠来源；**落地做法**：`resolve_intercity` 里飞机那段的 price 一律置空
     （不给模型猜的数），前端对飞机那段写"票价无可靠来源，未提供"，其他方式的价格标"（参考）"；
  ③ **评测集暂缓**；④ **任务不落库** —— 重启丢掉"正在规划的"，但确认过的行程在历史里，够用；
  ⑤ **长期记忆不做前端面板** —— 记忆是给 Agent 看的，前端只要能看到历史行程。
- 文档跟着改了：README「现状」把"还没做"那列换成"有意不做（附理由）"、
  「建议的下一步」换成「以后想加可以加什么」（评测集 / 火车票价，12306 自带查价工具）。

## 解说 Agent：确认之后的介绍与建议由模型写（2025）

- 原来 `build_confirm` 是**规则版**：介绍取"攻略里提到这个点的那句话"，建议从行程数据推（步行里程、是否自驾）
  再挑几句带"建议/注意/避开"的攻略原句。用户要求把这一层做成解说 Agent。
- 分工跟规划一样：**后端备事实、模型只写文字**。
  `planner.confirm_materials(trip, docs)` 备四样：行程摘要（每天每段的时间与交通方式）、地点资料（目录里的
  类型/评分/地址/季节/常规时长）、攻略片段（带出处）、**那几天的天气（现查工具）**；
  `agent.make_confirm(...)` 只负责写，结构化走 `with_structured_output(ConfirmDraft, method="json_mode")`。
- 新增 `ConfirmDraft`（`spot_intros: [{poi_id, intro}]` + `travel_tips: [str]`）—— 模型只填 poiId 和正文。
  **名字与出处由后端补**：出处仍用 `guide_note()` 按攻略正文匹配，不让模型编；
  模型编了行程里没有的点就丢掉、漏了的点记日志。
- 失败处理：**不退回规则版**（按"内容生成不做兜底"的规矩），抛 `planner.ContentFailed` →
  接口层 500 + 人话「生成各点介绍时出错了，请再试一次」。规则版的 `travel_tips()` / `tips_from_docs()` 已删。
- 前端：确认按钮在等待期间变成「正在写介绍…」并禁用（解说要跑一次模型），按钮旁边写清"要等十几秒"。
- 实测（完整流程）：规划 97.2 秒（零问题）→ **确认只用了 5.7 秒**；四个点各写了 2~3 句，而且
  **引用的都是攻略里的真数据**（三塔离古城约 2 公里、喜洲约 20 公里、机场距古城 30 公里、
  才村—喜洲段最适合骑、12-15 点最晒、电动车续航），建议也贴着这趟行程
  （23 号中阵雨 80% 带伞且可顺延、15℃ 早晚凉给老人带薄外套、青石板路穿平底鞋、返程按 3 小时倒推）。
  桩测试另外验了：编造的点被丢掉、空建议被过滤、模型抛错时抛 `ContentFailed`。

## 历史旅游方案（新模块，2025）

- 用户要求：前端导航加一个「历史旅游方案」标签；**存一次完整用户确认的规划**（存在长期记忆里）。
- 存储：`memory.py` 加了第四张表 `plans(user_id, trip_id, title, province, city, start_date,
  end_date, payload, saved_at)`，`payload` 就是那趟行程的 **JSON 原样**（camelCase）。
  按 `trip_id` 覆盖，所以**重复确认同一趟不会产生重复记录**（实测：改标题再确认 → 还是 2 条）。
  写入点在原来的 `remember()` 里加一行 `save_plan(trip)` —— 也就是"确认行程"那唯一的写入点。
- 接口（4 个 → **6 个**，都是只读）：
  `GET /api/plans` 列表（新的在前）、`GET /api/plans/{planId}` 返回整份 TripDetail
  （存的就是 TripDetail 的 JSON，直接还回去，不用再转换）。读库阻塞，都走 `asyncio.to_thread`。
- 前端：`VIEW_NAMES` 加 `history`，导航加第三个标签；进标签就重新拉一次列表；
  点卡片取详情 —— **详情直接复用现有渲染函数**（它们的点击切换只绑在 `tripDays` 上，
  历史那两个容器没绑，所以天然只读）：实现上是临时把全局 `trip` 换成存下来的那一份，渲染完再换回来。
- 实测：存/列/取都通（`trip-demo-1` 取回 2 天、住宿、大交通齐全；取不存在的返回 null → 404 统一错误体）；
  HTTP 三个请求也都对（列表 200 + camelCase、详情 200、不存在 404 `TRIP_NOT_FOUND`）；
  页面里 `历史旅游方案`、`data-view="history"`、`view-history`、两个容器都在，`app.js` 语法用 `node --check` 验过。
- 测试数据已清（`Travele/sqlite_data/` 现在是空的），你第一次确认行程后历史标签才会有东西。

## 消红标：后端自修时段 + 回喂重排（2025）


- 目标：把"校验未通过"的红标消掉。分两半 —— 能自己修的别去麻烦模型，模型能修的才回喂。
- **后端自修（不花模型调用）**：`to_trip_detail` 里，模型给了时刻就先用它的**时长**，但起点往后挪到位：
  ① 不能和上一段撞车（撞了就是路上时间不够）；② 不能跑到这个时段之外（实跑出现过"下午 11:00 开始"）。
  **只往后挪、不往前挪** —— 它可能是故意早起看日出，别把 06:30 推成 09:00。
  实测：模型给"上午 09:00-11:00 + 09:30-11:00（撞车）+ 下午 11:00-13:30"，
  修完是 `09:00-11:00 / 11:12-12:42 / 13:05-15:35`，**时段类问题清零**。
- **回喂重排（`revising` 事件终于有人发了）**：校验不过就把"上一版草稿 JSON + 问题清单"回喂模型，
  让它只改冲突的地方，重新出一版完整草稿。只回喂**一轮**，而且：
  · 只有问题数真的变少了才采纳（没变好就保留原版）；
  · 时间不够就不试（`MIN_REVISION_SECONDS = 45`，`make_draft` 现在把剩余时间一起交出来）；
  · 回喂自己抛错也不影响出结果（保留原版）。
  实测（桩掉模型调用）：修好 → 问题 1 → 0、`validated: true`、采纳"修好版"，
  进度里出现 `revising:发现 1 处不太合理，正在调整…`；没修好 → 保留原版并说
  `revising:这几处没能调好，先按现在这版给你`；抛错 → 照样 completed。
- **真跑一次（带工具、带必去点"洱海"）**：85.6 秒，`planMeta = {validated: true, issues: []}` ——
  **零问题、绿标**，时刻里能看到自修的痕迹（09:14、13:38、18:55 都是挪出来的）。
- 代价与注意：回喂一轮要 30~60 秒，而总预算 180 秒。现在的策略是"剩余时间够才回喂"，
  所以最坏情况（Agent 重试三次 + 回喂一轮）有可能撞到预算 —— 真撞上会当"创建超时"报，不会挂在那儿。

## 长期记忆（M6.0，2025）

- 用户定的七条：① 存"去过的点 + 补充说明 + 偏好" ② 归属走本地 userId（后来明确：**前端完全不参与**）
  ③ 用标准库 `sqlite3` ④ 前端面板以后再说 ⑤ 不用中间件、不做摘要、内容少不用压缩，
  库放 `Travele/sqlite_data/` ⑥ 前端无需知道，链路只有"存储 + Agent 取记忆"
  ⑦ 同一条偏好出现 ≥2 次才算长期画像。
- 落成 `Travele/memory/memory.py`（一个文件：建表 + 写 + 渲染）：三张表
  `preferences(user_id, kind, value, evidence, updated_at)` / `remarks` / `visited`。
  **偏好走证据计数**：每确认一次同一条 +1，够 2 次才进画像；去过按 (点, 日期) 去重；
  补充说明只存非空原文、同一句不重复入库。
- 读取：`memory_text()` 渲染成**固定顺序**的几行中文（喜欢 → 忌讳 → 补充说明 → 去过）→
  填进 `PlanContext.memory` → 系统提示词「长期记忆」段。顺序固定是为了不破坏提示词的缓存前缀。
- 写入只有一处：确认行程之后（`planner.remember_trip`，接口层用 `asyncio.to_thread` 调）。
  **这是全项目唯一一处"出错不往上抛"**：记忆写不进去只记日志，不能把用户的"确认"也搞失败。
- 只有一个本地用户（没有登录、前端不参与），所以 `user_id` 固定 `"local"`；以后接账号只改这一处。
- 实测（两次确认）：第一次确认后偏好**不进**画像（evidence=1），只有补充说明与去过的点；
  第二次确认后 `喜欢：美食、自然｜忌讳：购物` 才出现；库里 3 条偏好都 evidence=2；
  去过两条按日期去重；`PlanContext.memory` 与提示词那一段都拿到了这段文本。

## 别让临时目录掉进项目根目录（2025）

- 现象：项目根目录里冒出一堆空的 `tmpXXXXXXXX`（Python `tempfile.mkdtemp()` 的默认命名），
  一次会话攒了十来个。
- 根因（实测三行证据）：沙箱把 `TEMP`/`TMP` 指到它自己的目录，**但那个目录对沙箱进程不可写**
  （`os.access(W_OK)` 在 Windows 上误报可写，真写一下才报 `PermissionError`）；
  Python 的 `tempfile` 候选表逐个试写，全失败后**退到候选表最后一项——当前目录**，
  于是 transformers / chromadb 这些库每建一个临时目录就在项目根目录留一个空壳。
- 两层保障：
  ① **跑之前就把 TEMP 指走**（我这边对所有命令生效）：`TEMP`/`TMP` 指向工作区里的 `.tmp\` ——
     拦在 Python 启动之前，任何库、任何脚本都逃不掉。
  ② **项目里一道保险**：`Travele/api/main.py` 顶部加了 `keep_temp_out_of_project()`，
     **只在"临时目录已经等于项目根目录"时**把 `tempfile.tempdir` 钉到项目下的 `.tmp/`，正常机器上什么都不做。
     踩坑：这行必须放在**重型库导入之前** —— torch / transformers / langchain 在 import 阶段就会建临时目录，
     写在下面会漏一个（实测 11 → 12）。
- 验证：导入 app 后 `tempfile.gettempdir()` 从项目根目录变成 `…\.tmp`，根目录不再新增空目录。

## 住宿与大交通进行程 + 工具交给 Agent（2025）


- 决策（用户要求）：**直接做"最完整的第③种"** —— 住宿和大交通不再是"建议文本"，而是行程里的正式字段；
  同时**把数据源那五个工具交给 Agent**（`build_tools()` 不再返回空列表），让模型自己去查。
- 契约（返回①）新增两节：`intercity: [{direction, method, fromPlace, toPlace, date, depart, arrive,
  detail, price, verified}]`（去程 outbound / 回程 inbound）和
  `lodging: [{name, address, starRating, pricePerNight, checkIn, checkOut, bookingUrl, reason}]`。
  草稿（模型填的）也加了 `intercity` / `lodging` 两节，所以格式说明（PydanticOutputParser 的 schema）自动带上它们。
- **模型只挑名字，事实后端补**：住宿走 `resolve_lodging` —— 拿 `hotels` 工具查一次，按名字匹配，
  地址/星级/价格/预订链接一律用查回来的；名字对不上就剔除并记账（跟"编造的景点被剔除"一个道理）。
  大交通这一版**只有结构校验**，时刻是模型给的，所以 `verified=false`，前端显示"未核对"（诚实标注，不假装核对过）。
- 工具接 Agent 的坑：**工具一报错，整轮 Agent 就崩**（实测：拿过期日期查天气 → open-meteo 400 → 三次重试全废）。
  当时以为 `BaseTool.model_copy(update={"handle_tool_error": True})` 就兜住了 ——
  **那是错的，后来被整条链路的案例检验戳穿**（见下面那条）：`create_agent` 这一版根本不看这个字段，
  工具异常照样往外抛。真修法是自己在 `agent._tolerant()` 里包一层（成功转成文本、失败把原因当工具输出），
  详见「整条链路案例检验」那一条。
- 顺带修/补的东西：
  ① 装配时**出发地没带进来**（`PlanContext` 少了 `departure_city`）→ 模型不知道用户从哪来，所以大交通永远是空的；已补。
  ② 天气工具**没挡过去的日期**（示例请求是 2025-08-01，机器时钟在 2026-09-18）→ open-meteo 400；现在过去的日子直接报错。
  ③ 单次生成那一档没有工具，所以**住宿和天气由后端先查好喂进消息**（跟候选点、攻略一个待遇）——
     查不到就如实说"这次没查到，别编"。
  ④ 时间预算放宽：总预算 150→240 秒、单次调用 60→120 秒（Agent 要自己调工具，慢得多）、前端等待 180→300 秒。
  ⑤ 新增三条校验：住宿要盖住整个行程、大交通日期与到达时间要跟行程对得上、
     **时刻不能跟时段拧着**（实跑真出现过"下午 11:00 开始"）。
- 实测（完整流程：提交 → SSE → 取行程 → 确认，**74 秒**）：
  住宿拿到了真数据（大理晏清山居客栈｜3.5 星｜273 元/晚｜带预订链接）；
  大交通这次是**"如实说查不到"**（模型写"没查到具体航班班次，需按购票平台确认"，没编班次 —— 新加的那条规则生效了）；
  确认返回 4 个点介绍 + 3 条建议。
- 遗留（下一步）：① 模型会对 23 公里的路说"步行"，我们如实算成 248 分钟、校验再抓住它 —— 该由**修订循环**回喂；
  ② 大交通还没核对源（火车可以查 12306、航班只能查今天/明天且无票价）。


## 外部数据源层：五个来源写进 tool.py（2025）
> 注：这一条下面写的"不接给 Agent"是当时的决定，后来改成"接给 Agent"了 —— 见上面更新的条目。

- 决策（用户给定）：数据源放 `planning/tool.py`，**不接给 Agent** —— 后端自己调，
  `build_tools()` 保持空列表（Agent 模式继续跳过）；接口配置放 `Travele/.env` 且**先不填值**。
- 五个来源各一个函数：天气 open-meteo（免费、不用 key）/ 打车·自驾 高德 `v3/direction/driving` /
  酒店 RollingGo 的 MCP 服务 / 航班 Aviationstack / 火车 12306 的 MCP 服务。
  （原来是酒店·机票都用 RapidAPI，用户后来换成 RollingGo + Aviationstack。）
- 每个工具按用户要求的**三段式**写，段与段之间用 `# ---- 传入数据处理 / 工具实现 / 返回数据处理 ----`
  标出来，工具之间用 `=` 横线隔开；没有那段就不写（酒店、火车是两个 MCP 调用，返回处理在公共段里共用）。
- 工具描述**就是 docstring**（模型看到的 description）：一句话说干什么 + 什么时候用 + `Args:` 逐参数说明，
  用 `@tool(parse_docstring=True)` 让框架把 `Args` 解析成参数说明。
  原来 docstring 里写的是"服务怎么起、地址填哪个变量"——那是给开发者看的，模型看了不知道什么时候该用它；
  这类话现在一律挪到函数上方的 `#` 注释里，docstring 只留模型需要的。

  踩坑：三个返回用的数据类必须提到公共段 —— `@tool` 在**导入时**就解析类型提示，
  写在后面的"返回数据处理"段里会报 `name 'WeatherDay' is not defined`。
- Aviationstack 的字段没法离线核对（文档页是 JS 渲染的，OpenAPI 站在 key 后面），
  所以它的返回处理是"按官方文档的 `data[]` 结构取，取不到就带着原始返回报错"，不静默给空。
  **它没有票价**：这一项只能给"那天有几班、几点起降"，机票价格仍无来源。
- MCP 那两个工具**用官方 `mcp` SDK**（`streamable_http_client` + `ClientSession`），不自己拼 JSON-RPC：
  用户给的 12306 接入方式是 **Streamable HTTP**（`mcp-12306` 起服务，默认 8000 端口 →
  `http://localhost:8000/mcp`），所以新增依赖 `mcp>=2.2.0`（Python 包；npm 那个 12306-mcp 是另一套，没用）。
  两个踩坑：① 这版 SDK 的流是**两个**值 `(read, write)`，不是老版本的三元组，按老写法会报
  `not enough values to unpack`；② anyio 会把错误包成 `ExceptionGroup`，外面只看得到
  "unhandled errors in a TaskGroup"，所以要剥到最里层再抛（`_leaf`），否则自己写的精确消息会被套上"连不上"的壳。
- MCP 这条链路**真验过**：临时用 SDK 起了个假 MCP 服务（工具 `get_tickets` / `search_hotels`，
  外加一个名字里没有关键字的 `get_current_date`），实测握手成功、按关键字挑对了工具、入参填对、
  返回摊平正确 —— 火车、酒店两条都通；服务没起时报的也是干净的一句 `ConnectError`。
- **酒店接上了 RollingGo 真服务**（`https://mcp.rollinggo.cn/mcp`，认证是 `Authorization: Bearer <key>`）。
  它有 3 个工具，我们只用 `searchHotels`。挑工具的规则因此改成"关键字全部命中 + 取名字最短的那个"：
  `getHotelSearchTags` 也同时含 search 和 hotel，按"第一个命中"会挑错它。
  入参有它自己的规矩（写在我们这侧的传入数据处理里）：只查城市要写成"大理 中国"、`placeType` 必须一致、
  住几晚用 `stayNights` 而不是离店日期；返回是 `{success, hotelInformationList:[...]}`，
  `success` 为 false 或列表为空就抛错。实测拿到 5 家真酒店（锦顺天华 164 元/晚、晏清山居 273 元/晚…）。
- **交通改成"模型点名的直接算给它"**（新增 `planner.option_for`）。原来是拿点名的方式去**默认候选集**里碰运气，
  默认集按距离阈值挑（步行只在 ≤1.5 公里时才进集），于是模型说"步行"时后端把它挡了，还记账 ——
  实跑一次 issues 里冒出 8 条「没能给出 步行」。现在步行/骑行/公交按距离和速度表算、打车/自驾问高德，
  只有真的算不出来（比如"地铁"）才记一条。改完实跑 issues 从 8 条降到 **1 条，而且是真矛盾**：
  模型让走 18 公里、只给这段留了 90 分钟，校验器抓住了。
- **高德限流**：实跑撞上 `CUQPS_HAS_EXCEEDED_THE_LIMIT（10021）`，而原来把"对方明确报错"当不可重试 →
  整次规划失败。现在分了两类：新增 `RateLimited`（问得太快，等一下就好）→ 退避重试；
  key 不对、没这条路这类照旧直接报。另外两次调用之间留 0.4 秒间隔，HTTP 429/5xx 也纳入重试。
- 又对着**真的 12306 服务**验了一遍（装了 `mcp-server-12306`，`mcp-12306` 起在 8000）：
  它一共 **7 个工具**，我们只用名字带 ticket 的 `query-tickets`（还有查票价、中转换乘、经停站、车站搜索等，
  现在没用上）。它的入参叫 `train_date`（不是 `date`），关键字映射自动对上了；返回是
  `{success, count, trains:[{train_no, start_time, arrive_time, duration, seats}]}`，
  而"查不到"也是 `success: false` —— 所以火车那个工具补了返回处理：`success` 为 false 就抛错，
  不返回空列表让人以为"查过了，就是没有"。
  实测三种情况：杭州东→北京南 拿到 29 趟（G814 06:50→13:07）；杭州东→大理 报
  「未找到该线路的余票」；站名写错报「车站名称无效」。
- 五个函数用 LangChain 的 `@tool` 装饰（框架现成的件，名字/说明/参数 schema 跟着函数走），
  但**不交给 Agent**：后端自己 `.invoke(...)` 调。实测异常不会被框架吃掉（`SourceError` 照常抛出来）、
  `invoke` 返回的就是函数原本的返回值（`Ride` / `list[WeatherDay]`）。
- 一条规矩：**查不到就抛 `SourceError`** —— 不返回 `None`、不返回空列表、不用估算顶替。
  （第一版写成"没配就返回 `None`、调用方退回估算"，用户否掉了：不要兜底。）
  **代价**：高德 key 没填时，算交通那一步直接抛错 → 整次规划失败，不再有"估算版"行程。
- 不猜别人的接口：两个 MCP 服务（RollingGo 酒店、12306 火车）都先 `tools/list` 拿到工具名与入参名，
  再按名字填值（这类服务换版本就会改工具名）；认不出入参就报错，不硬塞一个空参数过去。
- 接线：打车/自驾已经接进 `transport_options` —— 高德的**路网距离与耗时**替掉直线距离与速度表
  （`SPEED_KMH` 里 `taxi`/`drive` 两项随之删掉）；价格仍是估算（高德不给票价），
  步行/骑行/公交保持直线估算。
- 实测：天气真调用成功（大理未来三天：小毛毛雨 15.6~25.8℃、降雨概率 57%），16 天以外抛错；
  高德用桩响应验了解析（12345 米 / 1800 秒 → 12.3 公里 / 30 分钟，含缓存）与失败分支（status=0 → 抛错）；
  `.env` 五个配置位读出来都是空串（注释文字没被当成值）。
- 踩坑：open-meteo 自带的地理编码**不能**用来把中文城市名换坐标 —— 实测把"大理"查到了另一个同名村子
  （28.6, 105.4）。所以 `weather()` 收坐标、不收城市名。
- 踩坑（沙箱）：`npx -y 12306-mcp --port 8080` 在我这边起不来 —— npm 要 spawn 子进程时被沙箱按 EPERM 拒了
  （npm 缓存在工作区外也会先被拒）。**这条链路因此没能真跑通**，是照 MCP 协议写的，
  要在你自己的终端里把服务起起来再验。

## 去掉确定性兜底：排不出来就报错（2025）

- 决策（用户要求）：内容生成失败不再做"数据兜底"。删掉确定性排程 `plan_order` / `build_day` / `build_trip`，
  `planner.plan()` 只剩一条路 —— 让模型排（① Agent 模式 → ② 单次生成）；两档都不成抛 `GenerationFailed`，
  接口层发 SSE `failed`，消息是给用户看的人话：「AI 这次没能排出行程，请稍后再试」。
  **代价说清楚**：模型或网络出问题的时候，用户这次就是拿不到行程、得重试，不再有"机器凑一份顶上"。
- `plan()` 签名跟着瘦身成 `plan(request, trip_id, emit=None)`：`draft` / `engine` / `use_agent` 三个参数删掉。
  草稿是空壳（一个可用地点都没有）也算没生成出来，同样报错，不返回一张空行程。
- 接口层错误分开处理：`CityNotFound`、`GenerationFailed` 的消息直接给用户看；
  其它异常一律 `logger.exception` 记技术原因，前端只收到「排行程的时候出错了，请稍后再试」
  （以前是把异常文本拼进用户消息里）。
- 顺手清掉已经没人用的东西：SSE 的 `fallback` 事件（前端标签与 CSS 一起删）、
  `planMeta.engine` 字段与 `PlanEngine` 枚举（`rulesFallback` 再也不会出现，`partial` 从没用过）、
  前端引擎徽标及其样式。`planMeta` 现在只有 `validated` / `issues` / `sourceDocs`。
- 保留的确定性部分**不是兜底，是"事实由后端填"**：坐标与规范名按目录补、交通耗时按直线距离与 `SPEED_KMH` 估算、
  预算、以及 `validate()` 的几类硬规则（结构、时间连续、必去点覆盖、地点真实性、首个方案可达性、开放时间、天数）。
  模型只负责选点、顺序、时段意图。
- 验证：① 模型无产出、草稿是空壳两种失败都抛 `GenerationFailed`，消息与预期一致；
  ② 起真服务把 `TRAVELE_LLM_MODEL` 改成不存在的模型名 → SSE 前三步正常、结尾是
  `{"event":"failed","message":"AI 这次没能排出行程，请稍后再试"}`，没有兜底行程；
  ③ 换回真模型端到端 → `validated=true`、3 天、`issues=[]`，耗时 37.4 秒，SSE 结尾 `done`。

## 规划 Agent 改用 create_agent（2025）

- 用户明确偏好 LangChain 的 `create_agent`（本地 langchain 1.4.0 已内置）。据此把规划改成**三级降级**：
  ① `planning/agent.py`（`create_agent` + 工具，模型自主多步）
  ② `planning/draft.py`（单次生成：资料预先装配进提示词）
  ③ `planning/fallback.py`（确定性生成器，不依赖模型）
- 新增 `planning/agent_tools.py`：三个工具（**中文 docstring 即模型看到的说明书**）
  `search_guides(city, keywords)`（调 retrieval 召回）、`list_candidates(city)`（查目录候选与编号/坐标）、
  `get_weather(city, date)`（未接入，明确返回"不要编造天气"）。
- `create_agent(model, tools, system_prompt, response_format=TripDraft)`：用 `response_format` 直接产出草稿结构；
  草稿 → 行程的装配抽成 `planning/draft.py::to_trip_detail()`，两个 AI 实现共用。
- 前提：**Agent 模式要求模型支持 function calling**——当前 `.env` 用的 `deepseek-flash` 是思考模式，
  实测报 `Thinking mode does not support this tool_choice`，于是自动降到单次生成（仍出真 AI 行程，engine=agent）。
- 验证（真实 HTTP）：事件依次为「规划 Agent 正在工作…」→「Agent 模式不可用（真实报错）…改用单次生成」→「校验通过」→「规划完成」；
  结果 engine=agent、validated=true、title「北京 3 日行程」。

## 规划 Agent 真跑通 + 校验器接入（2025）

- 结果：`POST /api/trips` → 真 LLM 起草 → 校验器检查 → 返回行程，`engine=agent`、`validated=true`。真实输出示例：title「北京 3 日慢节奏行程」，第1天故宫（上午+下午）、第2天颐和园（上午+下午）。
- 修 3 个兼容问题（都是实测暴露的）：
  1. 缺依赖 `langchain-openai`（ImportError）→ 已 `uv add`；
  2. `.env` 里把说明文字一起粘进 URL（InvalidURL）→ `llm_model` 增加"只取第一个词 + 自动补 https://"的容错；
  3. 结构化输出兼容性：模型是思考型（thinking）→ **不支持 tool_choice**；json 模式要求提示词含 "json" 字样；纯文本路径模型会自己发明字段名（start_date/travelers）→ 对策：提示词给**确切 JSON 结构模板**（含 "json" 字样）+ 新增 `TripDraft` 草稿结构（只让模型填 title/destination/budget/days，后端字段由代码补齐）+ **三种结构化方式依次降级**（function_calling → json_mode → 文本抽 JSON）。
- 修两个隐藏 bug：① `from ... import LAST_ERROR` 按值导入，导致降级原因永远显示"未配置 LLM"（改读模块属性）；② SSE 只等 1.5 秒就关闭，真模型 20~60 秒会被截断（改为最多等 180 秒）。
- 校验器（`planning/validate.py`）：五类规则——结构完整性、时间连续性、必去点覆盖、**地点真实性（防编造）**、可达性（本地坐标 + 速度估算，不接高德）。硬违规写入 `planMeta.issues`，SSE 显示"校验发现 N 处需要修正"。
- 顺带修数据链路：`PoiBrief` 与 fallback 生成器原先丢了经纬度，导致可达性无从计算；已补 lat/lng 并在提示词候选里给出坐标。
- 验证：坏样例逐一被检出（时间重叠 / 编造 poi_id / 缺时段 / 必去点未排入 / 可达性「约需 251 分钟但只留 5 分钟」）；fallback 真实输出不误报。

## 规划 Agent 的提示词层 + 起草步骤（2025）

- 决策：先做**规划 Agent**（不做解说）；提示词用 LangChain 的 `ChatPromptTemplate`，**全中文**（用户：英文出错自己看不出来）。
- 提示词分区（`planning/prompts/draft.py`）：①任务与输出规范 ②硬约束 ③用户需求 ④目的地候选 ⑤攻略原文(召回，带出处) ⑥**长期记忆占位** ⑦输出字段说明。
- 上下文预算：候选 ≤800 字/最多 12 点、召回 ≤3000 字（单块截断 800 字、前 60 字去重、按 score 降序）、记忆 ≤400 字；裁剪顺序=先丢低分召回，硬约束与用户需求永不裁剪。
- 决策（用户要求）：memory **保留占位符**（`记忆画像`/`记忆历史`，空值走"（暂无记忆）"），防以后忘记接入；参数已在 build_messages 预留，memory 做完只需传值。
- 新增 `planning/draft.py`：`with_structured_output(TripDetail)` 直接产出契约对象；失败/未配置 → 返回 None，由 `api/routers/trips.py` 降级到 `planning/fallback.py`，并推 SSE `fallback` 事件（带原因）。模型调用放 `asyncio.to_thread`，不阻塞事件循环。
- 修用户 `model/llm_model.py` 三个问题：`model_api_key="openai"` 参数名错误→`model_provider="openai"`；temperature 1→0.2（规划要稳）；max_tokens 2048→4096（多日行程 JSON 会截断）；timeout 30→120。并加 `.env` 读取（TRAVELE_LLM_API_KEY / _BASE_URL / _MODEL，不引入 dotenv 依赖）。
- 修 `model/__init__.py` 裸 import（`from llm_model import ...` 作为包导入必炸）→ 绝对导入。
- 验证：模板拼装 2 条消息、各分区字数正常、记忆占位与召回出处均出现、预算裁剪生效；未配置 LLM 时 `has_llm_config=False` → HTTP 端到端 SSE 为 searching→fallback(未配置 LLM)→validating→done，结果 engine=rulesFallback、sourceDocs=[北京攻略测试.txt]。


## 代码重组：改为"按功能分目录"（2025）

- 决策：放弃"按技术分层"（原 interface/state/preprocessing/agent），改为**按功能**组织——一个功能一个目录；功能之间只调用对方对外入口函数，共享数据只放 contracts/。
- 新结构：`contracts/`（数据结构）、`api/`（HTTP）、`planning/`（装配+生成+合并）、`retrieval/`（召回）、`ingest/`（原 line_after_work，含模型与向量库）、`static/`、`data/`、`model/`、`tool/`、`memory/`。
- 文件改名：`_api.py→contracts/api_base.py`、`context_builder.py→planning/context.py`、`fallback_planner.py→planning/fallback.py`、`decision_merger.py→planning/decisions.py`、`preprocessing/retrieval.py→retrieval/recall.py`。
- 顺带简化：state + schemas 合并进 contracts 后，原先绕循环导入的**惰性 __getattr__ 删除**，contracts/__init__ 正常 import 即可。
- 注释规范（用户反馈"看不懂"）：每个包 __init__.py 写"我是谁 / 谁调我 / 我调谁"；关键文件头写调用链；新增 `docs/code-map.md` 导航页（5 条链路 + "想改什么去哪"）。
- 启动命令变更：`uvicorn Travele.api.app:app`。
- 验证（真实 HTTP，新结构）：静态页 200；POST 北京 → completed（sourceDocs=**仅** 北京攻略测试.txt，说明 city 过滤生效）；`retrieval.recall.retrieve_docs(city="成都")` 命中 2 块；确认决策表 → merge 后 intros=[颐和园, 故宫]。

## 入库与检索集合统一（2025）

- 问题：入库写 `rag`（L2 度量），检索读 `rag_cosine`（余弦）→ **新上传的攻略检索看不到**。
- 修法：`config_data.collection_name = "rag_cosine"`；`KnowledgeBaseService` 建库显式带 `collection_metadata={"hnsw:space": "cosine"}`（集合已存在则沿用原度量）；删除失效的 `similarity_threshold=2`（旧配置等于不设阈值）。
- 验证闭环：入库测试文本（"去杭州西湖…"）→ 识别城市"杭州" → 写入 rag_cosine → `retrieve_docs(city="杭州")` 立即可命中（0.519）→ 已清理测试数据（集合回到 4 块，md5 记录同步移除）。
- 遗留：旧 `rag` 集合（L2、4 块重复数据）已无人使用，**已删除**；现库中只剩 `rag_cosine`。

## 入库元数据：city 自动提取（2025）

- 新增 `line_after_work/city_extract.py`：从**正文**提取城市（名单匹配 + 启发式，不调模型）。
  证据优先级：① 文件名标注的城市 且 正文出现（互相印证）→ 采用；② 正文前 200 字里**最先出现**的城市；③ 全文词频加权（市级 3 / 省级 1）。
- `knowledge_base.py`：metadata 增加 `city`（原来是占位 `x`，会直接 NameError），入库返回值带"识别城市"提示。
- 回填：已有 4 块（集合 rag / rag_cosine）按来源文档聚合文本重新提取并 update metadata。
- 踩坑：纯词频法把"成都攻略"识别成**九寨沟**（正文多次提到九寨沟）→ 加"开头窗口优先 + 文件名印证"后正确。
- 效果（检索过滤已生效）：city=成都→只回成都块(0.55/0.39)；city=北京→只回北京块(0.47/0.43)；city=大理→空；不加过滤时成都/北京互相串(0.25x)。
- 已知限制：一篇攻略跨多城时只打一个 city 标签（后续可改多标签或按 chunk 粒度提取）。

## 攻略召回（RAG 检索）真实实现（2025）

- `preprocessing/retrieval.py` 由占位 → 真检索：**过滤（city，探测语料有无该元数据才启用）+ 语义（bge，query 侧加官方指令前缀）+ 阈值（min_score）**，返回带 score 的 DocChunk。
- 参数集（MVP 确认）：`retrieve_docs(city, themes, poi_names, top_k=5, min_score=0.25)`。
- 踩坑①：原 `rag` 集合默认 **L2 距离**，"1-距离"当相关度全被滤掉 → 建 **`rag_cosine`**（hnsw:space=cosine）并拷贝原 4 块，检索改用它。
- 踩坑②：bge 首次加载约 20s 且会阻塞事件循环 → app 启动加**后台预热线程**；仍可能和新请求竞争（首请求 ~20s，之后秒回）。
- 语料事实：当前 2 篇测试攻略（北京攻略测试.txt / 成都攻略测试.txt，共 4 chunk），**无 city/chunk_index 元数据** → city 精确过滤暂休眠（重入库带上后自动启用）。
- 可见性：fallback 行程的 `planMeta.sourceDocs` 输出召回命中的攻略 → 前端"引用攻略"可见。
- 验证：北京→北京攻略命中(0.47)；成都+美食→成都命中(0.49)；点名"王府井"→对应块最高(0.51)；库外城市→空（正确的没攻略兜底）。HTTP 端到端：POST 北京→sourceDocs=[北京攻略测试.txt, 成都攻略测试.txt]。


## 确认语义升级：决策表回传 + 前端切换 UI（2025）

- 之前"全量回传 TripDetail"方案废弃：体积大、篡改面大、语义脏。
- 现在：`ConfirmRequest = { decisions: [{dayIndex, slot, seq, chosenPoiId, chosenTransport?}] }`
  只传用户**改过的节点**；后端 `agent/decision_merger.py::apply_decisions` 在存储版行程上
  merge（定位节点→收窄 options 为所选方案+所选交通）→ 得到最终确认版，再走解说。
- 前端 `plan.html`：节点现在可**本地切换地点/交通**（改动记在 sel，重渲染零请求），
  确认时由 `buildDecisions` 比对默认值生成决策表；提供"确认提交/返回 JSON"查看。
- fallback_planner 升级为每节点 1~2 个候选 + 演示交通（taxi/bike）——让切换交互有真数据；
  真实多方案/交通数值由规划 Agent + Tool/规则产出时替换（引擎=rulesFallback 徽标仍正确）。
- 踩坑：换"交通"且未换过地点时拿不到当前地点 → 增加 eff 缓存（渲染时记录生效选择）供联动。
- 验证（真实 HTTP）：节点 2 候选+2 交通；空决策确认 intro=[洱海廊道,大理古城]；
  决策"第1天换大理古城+bike"后 intro 顺序翻转为[大理古城,洱海廊道]——merge 生效。


## 确认行程设计更正（2025）

- 原设计漏洞：confirm 只传 trip_id，隐含"后端存的行程=用户确认的行程"——与方案树"前端本地随便改"矛盾。
- 更正：**前端把用户确认的那张行程表全量回传**（`ConfirmRequest{itinerary: TripDetail}`），后端以提交版为准做召回与解说。
- 返回内容按用户要求**瘦身**：只回 `spotIntros`（行程中每个旅游点的简略介绍，带 sourceDoc 预留）+ `travelTips`（出行建议）；删除 playables/addOns/cautions 结构与 AddOnType 枚举。
- 解说三件套（攻略召回 / 天气 tool / 解说 Agent）未接：confirm 路由当前返回"占位介绍+空 tips"，路由内 TODO 标了接入顺序（按点召回 → 天气 → 解说 Agent）。
- 踩坑：`confirm → trip → state/__init__ → validation → trip` 循环导入 → state/__init__ 对 validation 导出改**惰性 __getattr__** 切断环路。
- 实现位置：路由 `interface/routers/trips.py::confirm_trip`；schema `interface/schemas/confirm.py`；前端 `static/plan.html`（确认行程按钮回传当前行程）。
- 验证（真实 HTTP）：POST→completed→confirm(回传行程)→200 `{tripId, spotIntros×2, travelTips:[]}`；未知任务 confirm→404。

## 移除 pace 字段（2025）

- 决策：`UserInfo.pace`（行程节奏 relaxed/normal/intensive）**删除**——语义是"每天排几个活动/密度"的规划约束，但当前没有任何消费者（fallback_planner 不看、Agent 未接），属于"收了没人用"的死字段。按"不收死字段"原则全库清除：schema/枚举/SoftConstraints/context_builder/前端表单/示例/设计文档。
- 同处境提醒：`budgetTier`（预算档）目前同样没有消费者，但保留——它是行程预算/工具筛选的近期输入，Agent 期会用（若你也要砍，说一声）。
- 验证：pace 字段现在会被 extra=forbid 拒绝（422）；示例解析/装配正常。


## 主链路跑通（2025）——真装配 + 最小生成器

- 决策：POST 现在走**真装配**（preprocessing → PlanningContext），不再返回固定示例。
- 新增 `agent/fallback_planner.py`：最小确定性生成器（engine=rulesFallback），用 PlanningContext 产出反映输入的简版行程（标题=省市+天数、每天上午一个目录点、必去点优先排）；它是"数据兜底"的第一版雏形，规划 Agent 接入后替换，路由不改。
- 决策：错误体统一**顶层**返回（400 CITY_NOT_FOUND / 404 TRIP_NOT_FOUND / 422 VALIDATION_FAILED），不用 HTTPException 的 {"detail":…} 包裹。
- 验证（真实 uvicorn + HTTP）：POST 202 → SSE [searching,drafting,validating,done] → GET completed（title=云南大理 3日简版行程、days=3、首天上午=洱海生态廊道、engine=rulesFallback）✓；未知城市 → 400 CITY_NOT_FOUND ✓；未知 trip → 404 ✓。
- 踩坑：忘了旧预览服务器还占着 8099，新服务器起不来（bind 10048）→ 起服务前先确认无残留 job。
- 现状简化（留给下一步）：一天只排一个活动、无交通选项、无预算明细——真 Agent 与校验器接入时补齐。


## 随机目的地功能（重做，前端本地随机）

- 决策：不做高德 API 导入、不做后端 discover 批次接口——随机卡片改**纯前端本地随机**。
- 决策：**随机卡片不含任何"好不好玩"断言**（主观+时效风险）——只展示客观信息（省/市/旅游点/描述/图片），喜不喜欢由用户判断，"换一个"即可。
- 数据：`static/data/spots.json`，一条 = {province, city, spot, description, image}（"表头"五列）；图片先占位 URL，后续放 `static/img/`。
- 前端：`static/index.html`——一次只显示一个旅游点：左=图片、右=省市地+描述；洗牌法随机（不重复，看完自动重洗）；"就它了"按钮暂为占位（后续接规划表单）。
- 后端：删除 routers/discover.py 与 schemas/discover.py；app.py 挂载 `static/` 到根路径（html=True）。
- 验证：GET / → 200 页面；GET /data/spots.json → 200（10 条）；POST /api/trips → 202 仍正常。
- 小瑕疵记录：静态挂载在根路径，`/api/*` 的"方法不匹配"会返回文件 404 而非标准 405（真实 POST/GET 流不受影响，可接受）。

## 工作区清理（2025）

- 删除：`src/pythonproject/`（uv 脚手架空包，从未使用）、`.uv-cache/`（沙箱缓存产物，可再生）。
- pyproject：项目名 `pythonproject` → `travele`；加 `[tool.uv] package=false`（应用形态，不构建本地包）；删除失效的 `[project.scripts]` 入口；uv.lock 已重新对齐。
- 保留：`docs/`（设计基线 + 日志 + 示例）、`Travele/model/llm_model.py`（当前无人调用，但 agent 接入 LLM 时会用）、`Travele/line_after_work`（离线管道及其模型权重/chroma_db 不动）。

## M0.1 工程环境（done）

- 决策：新增依赖 `langgraph 1.2.11`、`pydantic-settings`、`httpx` 到根 pyproject（uv 管理）。
- 决策：Travele 下各域目录补 `__init__.py`，使 `Travele.*` 可作为包导入。
- 踩坑：目录名 `pre-processing` 含连字符 → **Python 无法 import**（包名只允许字母/数字/下划线）→ 重命名为 `preprocessing`。
- 踩坑：uv 默认缓存目录在 `%LOCALAPPDATA%\uv\cache` 被沙箱拒绝 → 设置 `UV_CACHE_DIR=<workspace>\.uv-cache` 后成功。
- 验证：`.venv` 中 `import langgraph / langchain 1.4.0 / fastapi / httpx / pydantic_settings / Travele.*` 全部 OK。

## M0.2 数据契约（框架完成，等你实现 TODO + review）

- 决策：API 契约 → `interface/schemas/`（submit/trip/discover/progress/error/confirm）；领域中间数据 → `state/`（planning_context/validation）；枚举 → `state/enums.py` 共用。
- 决策：camelCase 用方案 B——`alias_generator=to_camel` + `populate_by_name=True` + `extra="forbid"`；FastAPI 默认 `response_model_by_alias=True` 输出 camelCase。
- 决策：草稿/成品共用同一套 trip 模型（`DraftItinerary = TripDetail` 别名）；MVP 取舍=接口形状即领域形状，state→interface 的单向 import 是显式例外，Agent 需要 wire 外字段时再拆 `state/domain/`。
- 决策：前端 static 用纯静态 HTML/JS（无构建步骤）。
- 踩坑修正：错误体曾误写在 progress.py，已拆出独立 `error.py`。
- 验证：camelCase 入参/内部 snake_case 属性/wire camelCase 输出/extra=forbid/枚举 wire 值 全部通过冒烟测试。
- 产出：`docs/examples/` 三个可执行示例（planning_request / tripdetail / confirm 的 wire JSON），并已用 schema 解析验证通过；可作你实现 TODO 与未来前端、测试的 fixtures。
- TODO-1/2 已由用户首次实现、我 review 并修正：原实现缺 `@model_validator/@field_validator` 装饰器（Pydantic 不执行）、判断逻辑写反、且覆盖 `__init__` 会毁掉 alias/默认值机制。修正后验证：正常 / end<start / adults=0 / children=-1 / 最小必填 五例全过。
- 待定（等你表态）：TimeRange 用 str vs time、extra=forbid 取舍、M0.3 的三个 TODO（SSE 真流式 / TaskStore / planning 态 GET 语义）。

## M0.3 FastAPI 传输层骨架（demo 可运行）

- 决策：路由只做"协议翻译"（interface/routers/），业务逻辑归 preprocessing/agent/tool。
- 决策：任务先存内存 TaskStore（GET 幂等可恢复的前提）；持久化 M1 落 SQLite（见文末"存储决策"）。
- 决策：异常统一映射——RequestValidationError→422+VALIDATION_FAILED；错误体 ApiErrorBody。
- 决策：config 用 pydantic-settings，env_file 用 `__file__` 推导绝对路径（不依赖 cwd）。
- 坑：TestClient 每个请求独立事件循环 → `asyncio.create_task` 的后台任务会被回收（SSE 只回放第一条）。真实 uvicorn 单进程无此问题；多 worker/多进程时任务存储与执行必须外置（M1 队列）。已在代码注释里留给用户实现真流式(Queue)方案。
- 验证（真实 uvicorn :8099 + HTTP）：POST 202 → SSE [searching,drafting,validating,done] → GET completed（1 天、首节点 2 候选）✓；422 统一错误体 ✓；404 ✓。
- 文件：interface/{app,config,tasks}.py + routers/{trips,discover}.py + schemas/task.py。
- TODO（等你实现）：① SSE 回放→真流式(asyncio.Queue+心跳+多订阅) ② TaskStore 并发/过期清理/接口化 ③ planning 态 GET 语义再确认。

## M1 切片 1：preprocessing 全链路打通（done，模式 A）

- catalog 查询三方法 + find_place_owner；context_builder 的 _expand_dates/_resolve_destination 已实现。
- 行为：示例 → dates=3 / total=3 / 大理 catalog_pois=2 / soft 映射全；centerPlace=故宫 自动带出省市并归一化为 place_id；未知城市 → PreprocessingError。
- 边界说明：mustVisit 是**精确名匹配**——示例里"洱海"≠目录名"洱海生态廊道"，故保留原文交给 Agent（模糊匹配留待后续，见切片讨论）。
- 验证输出：见上，8 项断言全过。

- D1 按默认执行：目的地目录 = 静态 JSON `Travele/data/destinations.json`（省→市→点三级 + snake_case 键 + Pydantic 模型）。
- D2 按默认执行：攻略检索先做占位 `preprocessing/retrieval.py`（签名固定，M1 后换真实现零改动）。
- 文件：`preprocessing/catalog.py`（加载+查询骨架）、`context_builder.py`（request→PlanningContext 骨架）、`retrieval.py`（占位）、`Travele/data/destinations.json`（样例：北京2点/大理2点）。
- 踩坑：目录 JSON 是内部数据不走 wire，键名用 snake_case，别套 camelCase alias。
- 验证：目录加载/导入链 ✓；TODO 如期抛出 NotImplementedError ✓。
- TODO（你来实现）：catalog 三个查询方法（find_city/find_place_by_name/search_places）+ `_expand_dates` + `_resolve_destination`（到市 guard 抛 PreprocessingError、centerPlace 命中以目录省市为准）。
- 验收：对 planning_request.example.json 跑 build_planning_context → PlanningContext（dates=3、大理、catalog_pois=2、must_visit 已解析）。

## 存储决策（已确认，替换 M0.3 中"落 PostgreSQL"的说法）

- memory（M6）：**SQLite + chunk 指针** `{doc, chunk_index}` 指向离线攻略库 chunk（与 sourceRef 同款），不复制正文 → 复用 + 一致 + 可溯源。
- 整站简化：**砍掉 PostgreSQL/pgvector 与 Redis**（用户未用过，且向量本在 Chroma）→ 业务/画像/历史全落单文件 SQLite；访问层标准库 sqlite3 起步，想练 ORM 再换 SQLAlchemy+aiosqlite。
- 同步更新：project-design.md §8/§10/§11；顺带修正 §10 前端行为纯静态 HTML/JS（M0.2 决策此前未同步）。
