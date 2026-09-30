"""从攻略正文里提取"城市"（用于入库 metadata，让检索能按城市精确过滤）。

为什么要做：向量相似度找"语义相近"，但"是不是这座城市"要靠元数据过滤；
语料入库时如果不把城市写进 metadata，检索时就没法只在大理的攻略里找。

实现取舍：
- 用**名单匹配 + 启发式打分**，不调模型——入库是离线批处理，快、可复现、可解释；
- **开头窗口优先**：攻略通常在开头就声明目的地（"D1天：上午到达成都…"），
  先只看前 HEAD_CHARS 字；窗口内有命中就以窗口为准，避免"顺带提到的次要城市"
  （如成都攻略里反复出现九寨沟）把主目的地挤掉；
- 窗口内没有地名 → 退化为全文加权计数：市级权重 3、省级 1，同分取先出现的；
- 只认名单里的地名，避免把任意名词当地名。

用法：extract_city("D1天：上午到达成都……") → "成都"
"""
from __future__ import annotations

# 开头窗口：目的地一般在前 200 字内出现
HEAD_CHARS = 200

# 直辖市 + 主要城市（市级；含常见旅游城市）。
# 注意：名字要和 Travele/data/destinations.json 里的 city 一字不差 —— 在线检索用
# filter={"city": 目录里的城市名} 做精确过滤，这里没收录就会入库成空标签、召回时漏掉。
CITIES: tuple[str, ...] = (
    "北京", "上海", "天津", "重庆",
    "哈尔滨", "长春", "沈阳", "大连", "呼和浩特", "石家庄", "太原", "大同", "济南", "青岛", "烟台", "威海", "泰安",
    "郑州", "洛阳", "开封", "西安", "咸阳", "兰州", "敦煌", "西宁", "海西", "银川", "乌鲁木齐", "阿勒泰", "喀什", "拉萨", "日喀则",
    "南京", "苏州", "无锡", "扬州", "镇江", "徐州", "常州", "南通", "连云港",
    "杭州", "宁波", "温州", "绍兴", "嘉兴", "湖州", "舟山", "金华", "台州", "丽水", "衢州",
    "合肥", "黄山", "芜湖", "蚌埠", "福州", "厦门", "泉州", "漳州", "龙岩", "武夷山",
    "南昌", "景德镇", "九江", "上饶", "赣州", "武汉", "宜昌", "襄阳", "恩施", "十堰",
    "长沙", "张家界", "岳阳", "衡阳", "湘潭", "凤凰",
    "广州", "深圳", "珠海", "佛山", "东莞", "中山", "惠州", "汕头", "潮州", "湛江", "肇庆", "韶关",
    "南宁", "桂林", "柳州", "北海", "阳朔", "海口", "三亚", "万宁", "琼海",
    "成都", "绵阳", "乐山", "峨眉山", "都江堰", "九寨沟", "稻城", "康定", "宜宾", "泸州", "南充", "西昌",
    "贵阳", "遵义", "安顺", "凯里", "黔东南", "昆明", "大理", "丽江", "香格里拉", "西双版纳", "腾冲", "泸沽湖", "玉溪", "曲靖",
    "香港", "澳门", "台北", "高雄", "花莲",
)

# 省级（含自治区/特别行政区）——作为兜底（正文只提省没提市时）
PROVINCES: tuple[str, ...] = (
    "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建", "江西", "山东",
    "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海",
    "台湾", "内蒙古", "广西", "西藏", "宁夏", "新疆",
)

CITY_WEIGHT = 3
PROVINCE_WEIGHT = 1


def _first_in(text: str, names: tuple[str, ...]) -> str | None:
    """返回最先出现的地名（位置优先——目的地通常先被提到）。"""
    best: str | None = None
    best_pos = len(text) + 1
    for name in names:
        pos = text.find(name)
        if pos != -1 and pos < best_pos:
            best, best_pos = name, pos
    return best


def _best_in(text: str, names: tuple[str, ...], weight: int) -> str | None:
    """在给定文本里按"词频×权重"挑最优地名（同分取先出现）。"""
    best_name: str | None = None
    best_score = 0
    best_pos = len(text) + 1
    for name in names:
        n = text.count(name)
        if n == 0:
            continue
        score = n * weight
        pos = text.find(name)
        if score > best_score or (score == best_score and pos < best_pos):
            best_name, best_score, best_pos = name, score, pos
    return best_name


def extract_city(text: str, filename: str = "") -> str | None:
    """返回最可能的城市名；只有省信息时返回省名；都没有返回 None。

    证据优先级：
    1. **文件名标了城市 且 正文里也出现** → 直接采用（作者标注 + 正文印证，最可靠）；
    2. 正文开头窗口（前 HEAD_CHARS 字）里**最先出现**的城市；
    3. 全文按词频加权（市级优先，其次省级）。
    """
    if not text and not filename:
        return None

    text = text or ""

    # 1) 文件名 + 正文互相印证
    for name in CITIES:
        if name in (filename or "") and name in text:
            return name

    # 2) 开头窗口：最先出现者为准
    head = text[:HEAD_CHARS]
    name = _first_in(head, CITIES)
    if name:
        return name

    # 3) 全文：市级优先，其次省级
    name = _best_in(text, CITIES, CITY_WEIGHT)
    if name:
        return name
    return _best_in(text, PROVINCES, PROVINCE_WEIGHT)
