"""扩充目的地目录：把"人写的景点名"交给高德核准并取回坐标、评分、开放时间。

"""
from __future__ import annotations

import json
import pathlib
import re
import time

from Travele.planning.tool import _config, _get_json

CATALOG = pathlib.Path(__file__).with_name("destinations.json")
THROTTLE_SECONDS = 0.4
KEEP_TYPES = ("风景名胜", "科教文化服务", "博物馆", "特色商业街", "步行街", "市场")
# 这几类是"街/夜市"性质的，高德给的类型是地名或购物服务，不强求景点类型（坐标照样是真的）
ANY_TYPE_NAMES = {"八廓街", "沙洲夜市", "山塘街", "屯溪老街", "回民街"}
ALIASES = {                                          # 高德那边的叫法和常用名不一样时补一手
    "黄龙风景区": ["黄龙", "黄龙景区"],
    "水上雅丹": ["乌素特水上雅丹", "水上雅丹地质公园"],
    "禾木村": ["禾木风景区", "禾木村景区", "禾木"],
}

# （城市, 省市, [景点名…]）—— 名字按攻略正文里的叫法写，别写"XX风景区-某门"这类子点位
WANTED: list[tuple[str, str, list[str]]] = [
    ("三亚", "海南", ["天涯海角", "南山文化旅游区", "蜈支洲岛", "大东海", "鹿回头风景区"]),
    ("九寨沟", "四川", ["黄龙风景区", "五花海", "诺日朗瀑布", "长海", "松潘古城"]),
    ("厦门", "福建", ["南普陀寺", "厦门大学", "胡里山炮台", "曾厝垵", "集美学村"]),
    ("大同", "山西", ["悬空寺", "华严寺", "善化寺", "九龙壁", "大同古城墙"]),
    ("张家界", "湖南", ["天门山", "袁家界", "天子山", "金鞭溪", "张家界大峡谷"]),
    ("拉萨", "西藏", ["大昭寺", "八廓街", "色拉寺", "哲蚌寺", "罗布林卡"]),
    ("敦煌", "甘肃", ["鸣沙山月牙泉", "玉门关", "雅丹国家地质公园", "阳关", "沙洲夜市"]),
    ("泰安", "山东", ["岱庙", "红门", "天外村", "南天门", "玉皇顶"]),
    ("海西", "青海", ["大柴旦翡翠湖", "察尔汗盐湖", "水上雅丹", "可鲁克湖", "茫崖翡翠湖"]),
    ("苏州", "江苏", ["狮子林", "留园", "平江路", "虎丘", "山塘街"]),
    ("阿勒泰", "新疆", ["禾木村", "五彩滩", "白哈巴", "可可托海", "观鱼台"]),
    ("香格里拉", "云南", ["松赞林寺", "独克宗古城", "纳帕海", "虎跳峡", "白水台"]),
    ("黄山", "安徽", ["宏村", "西递", "屯溪老街", "翡翠谷", "呈坎"]),
    ("黔东南", "贵州", ["镇远古镇", "肇兴侗寨", "加榜梯田", "青龙洞", "舞阳河"]),
    ("杭州", "浙江", ["雷峰塔", "西溪湿地", "河坊街"]),
    ("桂林", "广西", ["象鼻山", "阳朔西街", "遇龙河"]),
    ("丽江", "云南", ["拉市海", "蓝月谷", "木府"]),
    ("西安", "陕西", ["华清宫", "陕西历史博物馆", "西安钟楼", "回民街", "大明宫国家遗址公园"]),
    ("北京", "北京", ["天安门广场", "什刹海", "南锣鼓巷", "圆明园"]),
    ("大理", "云南", ["苍山", "双廊古镇", "小普陀"]),
    ("成都", "四川", ["锦里", "青城山", "人民公园"]),
]

LONG_TRIP_WORDS = ("山", "湖", "古镇", "古城", "风景区", "景区", "公园", "遗址", "湿地", "梯田", "溶洞", "岛")


def search(city: str, keyword: str) -> list[dict]:
    data = _get_json("https://restapi.amap.com/v3/place/text", {
        "keywords": keyword, "city": city, "citylimit": "true", "offset": 10, "page": 1,
        "extensions": "all", "key": _config("TRAVELE_AMAP_KEY"),
    }, "高德 POI 检索")
    return [poi for poi in (data.get("pois") or []) if poi.get("name") and poi.get("location")]


def pick(city: str, name: str) -> dict | None:
    """在候选里挑最像"这个景点"的那一条；挑不到返回 None（宁可不加，也不加错的）。"""
    loose = name in ANY_TYPE_NAMES              # 街道/夜市这类不强求景点类型
    for keyword in ALIASES.get(name, []) + [name]:
        hits = [poi for poi in search(city, keyword)
                if (name in poi["name"] or poi["name"] in name or keyword in poi["name"])
                and (loose or any(code in str(poi.get("type", "")) for code in KEEP_TYPES))]
        if hits:
            # 先排除"名字里带这个地名、其实不是景点"的：酒店、餐馆、公司（实测"沙洲夜市"会挑到酒店）
            def rank(poi: dict) -> tuple[int, int]:
                bad = any(code in str(poi.get("type", "")) for code in ("住宿服务", "餐饮服务", "公司企业"))
                return (1 if bad else 0, len(poi["name"]))
            return min(hits, key=rank)
        time.sleep(THROTTLE_SECONDS)
    return None


def to_open_hours(raw) -> str:
    """高德的 open_time 可能是"24小时营业"、多个时段拼一起、或者空 —— 只取第一个合法时段。"""
    match = re.search(r"\d{2}:\d{2}-\d{2}:\d{2}", str(raw or ""))
    return match.group(0) if match else "全天"


def to_place(province: str, city: str, name: str, poi: dict) -> dict:
    lng, lat = (float(value) for value in poi["location"].split(","))
    biz = poi.get("biz_ext") if isinstance(poi.get("biz_ext"), dict) else {}
    rating = biz.get("rating")
    address = poi.get("address")
    if isinstance(address, list):
        address = ""
    return {
        "place_id": "amap-" + poi["id"],
        "province": province,
        "city": city,
        "name": name,                              # 用我们这边的叫法（和攻略正文一致）
        "poi_type": "人文" if any(word in str(poi.get("type", "")) for word in ("博物馆", "寺庙", "文化", "科教", "纪念馆", "遗址")) else "自然",
        "rating": float(rating) if rating not in (None, "", []) else None,
        "address": address or "",
        "lat": round(lat, 4),
        "lng": round(lng, 4),
        "season_best": "",
        "duration_min": 180 if any(word in name for word in LONG_TRIP_WORDS) else 120,
        "open_hours": to_open_hours(biz.get("open_time")),
        "intro": "",
    }


def main(write: bool) -> None:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    places = data["places"]
    existing = {place["name"] for place in places}
    added, skipped, missed = [], [], []

    for city, province, names in WANTED:
        have = sum(1 for place in places if place["city"] == city)
        for name in names:
            if any(name in other or other in name for other in existing):
                skipped.append(f"{city} {name}（目录里已有）")
                continue
            if name in {place["name"] for place in added}:
                continue
            poi = pick(city, name)
            time.sleep(THROTTLE_SECONDS)
            if poi is None:
                missed.append(f"{city} {name}")
                continue
            place = to_place(province, city, name, poi)
            added.append(place)
            print(f"  {city:<5}{name:<12}→ 高德「{poi['name']}」{place['lat']},{place['lng']} "
                  f"评分={place['rating']} 开放={place['open_hours']}")
        print(f"{city}：目录原有 {have} 个，本次新增 {sum(1 for p in added if p['city'] == city)} 个\n")

    print(f"合计新增 {len(added)} 个点｜已在目录里跳过 {len(skipped)} 个｜高德没匹配上 {len(missed)} 个")
    if missed:
        print("没匹配上的：", "、".join(missed))
    if not write:
        print("\n（这次只是看看，没写文件；要写就加 --write）")
        return

    data["places"] = places + added
    data["_说明"] += (f"\n其中 {len(added)} 个点是用 Travele/data/build_catalog.py 扩的："
                      f"名字由人写（和攻略正文的叫法一致），坐标/评分/开放时间来自高德 POI 检索。")
    CATALOG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n已写回 {CATALOG}：{len(places)} → {len(data['places'])} 个点")


if __name__ == "__main__":
    import sys

    main("--write" in sys.argv)
