"""整条链路的完整案例检验：把用户真正会走的每一步都跑一遍，并逐条断言。

怎么跑（先起服务，再执行；它会真跑一次模型，约 2 分钟）：
    .venv\\Scripts\\python.exe -m uvicorn Travele.api.main:app --port 8099
    $env:PYTHONIOENCODING='utf-8'; .venv\\Scripts\\python.exe tests\\full_case.py [端口]
退出码 0 = 全部通过，1 = 有断言没过（控制台会列出失败的那几条）。

覆盖：
  ① 静态页与资源      GET /  /app.js  /app.css  /data/spots.json
  ② 提交需求          POST /api/trips            → 202
  ③ 进度（SSE）       GET  /api/trips/{id}/events → 事件序列，结尾必须是 done
  ④ 取行程            GET  /api/trips/{id}        → completed + 大交通 + 住宿 + 每天
  ⑤ 用户改节点        本地选定一个备选方案（模拟前端零请求切换）
  ⑥ 确认 + 解说       POST /api/trips/{id}/confirm → 介绍 + 建议
  ⑦ 确认后的行程      GET  /api/trips/{id}        → 改过的那个节点被收窄并锁定
  ⑧ 历史列表          GET  /api/plans
  ⑨ 历史详情          GET  /api/plans/{id}        → 整份行程与④一致
  ⑩ 错误分支          未知城市 400 / 未知历史 404 / 坏决策 422 / 空决策改不动 422

注意：它会往长期记忆里写一条（确认过的行程），所以跑完历史列表会多一条。
"""
import datetime
import json
import pathlib
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]        # 仓库根目录（样例在那里）
PORT = sys.argv[1] if len(sys.argv) > 1 else "8099"
BASE = f"http://127.0.0.1:{PORT}"
results: list[tuple[bool, str]] = []


def check(ok: bool, text: str) -> bool:
    results.append((ok, text))
    print(("   ✔ " if ok else "   ✗ ") + text)
    return ok


body = json.loads((ROOT / "docs" / "examples" / "planning_request.example.json").read_text(encoding="utf-8"))
start = datetime.date.today() + datetime.timedelta(days=1)
body["schedule"]["tripStart"] = start.isoformat()
body["schedule"]["tripEnd"] = (start + datetime.timedelta(days=2)).isoformat()
body["destination"]["mustVisit"] = ["洱海"]
body["remarks"] = "带老人，节奏放慢，不爱爬山"

with httpx.Client(base_url=BASE, timeout=420) as client:
    print("① 静态页与资源")
    for path in ("/", "/app.js", "/app.css", "/data/spots.json"):
        check(client.get(path).status_code == 200, f"GET {path} → 200")

    print("\n② 提交需求")
    created = client.post("/api/trips", json=body)
    check(created.status_code == 202, f"POST /api/trips → {created.status_code}")
    trip_id = created.json()["tripId"]
    print("      tripId =", trip_id)

    print("\n③ 进度（SSE）")
    events: list[dict] = []
    began = time.monotonic()
    with client.stream("GET", f"/api/trips/{trip_id}/events") as stream:
        for line in stream.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                events.append(event)
                print(f"      [{time.monotonic() - began:6.1f}s] {event['event']}: {event['message']}")
    kinds = [event["event"] for event in events]
    check(bool(events) and kinds[-1] in ("done", "failed"), f"结尾是 {kinds[-1] if kinds else '（没有事件）'}")
    check(kinds[-1] == "done", "规划成功（不是 failed）")
    check("searching" in kinds and "drafting" in kinds and "validating" in kinds,
          "进度里查资料/排行程/做检查都在")

    print("\n④ 取行程")
    detail = client.get(f"/api/trips/{trip_id}").json()
    check(detail.get("status") == "completed", f"状态 completed")
    meta = detail.get("planMeta") or {}
    print(f"      planMeta：validated={meta.get('validated')} issues={meta.get('issues')}")
    days = detail.get("days") or []
    check(len(days) == 3, f"天数 = {len(days)}")
    nodes = [item for day in days for segment in day["segments"] for item in segment["items"]]
    check(len(nodes) >= 5, f"活动节点 {len(nodes)} 个")
    check(all(item["timeRange"] for item in nodes), "每个节点都有时间")
    lodging = detail.get("lodging") or []
    check(len(lodging) >= 1, f"住宿 {len(lodging)} 家：" + "、".join(item["name"] for item in lodging))
    check(all(item.get("pricePerNight") for item in lodging), "住宿带每晚价（真数据）")
    intercity = detail.get("intercity") or []
    check(len(intercity) >= 1, f"大交通 {len(intercity)} 段：" +
          "、".join(f"{leg['direction']}/{leg['method']}" for leg in intercity))
    planes = [leg for leg in intercity if leg["method"] == "plane"]
    check(all(leg.get("price") in (None, 0) for leg in planes),
          f"飞机那段的票价是空的（{len(planes)} 段）—— 没有可靠来源就不给数")
    check(all(leg.get("verified") is False for leg in intercity), "大交通如实标了未核对")
    poi_ids = {option["poi"]["poiId"] for item in nodes for option in item["options"]}
    check(any("erhai" in poi_id for poi_id in poi_ids), "必去点「洱海」排进了行程")

    print("\n⑤ 用户改一个节点（模拟前端本地切换）")
    target = None
    for day in days:
        for segment in day["segments"]:
            for item in segment["items"]:
                if len(item["options"]) >= 2:
                    target = (day["dayIndex"], segment["slot"], item["seq"], item)
                    break
            if target:
                break
        if target:
            break
    decision: dict = {}
    if target is not None:
        day_index, slot, seq, item = target
        picked = item["options"][1]
        decision = {"dayIndex": day_index, "slot": slot, "seq": seq,
                    "chosenPoiId": picked["poi"]["poiId"]}
        print(f"      第{day_index}天 {slot} 第{seq}个节点：换成「{picked['poi']['name']}」")
    else:
        for day in days:
            for segment in day["segments"]:
                for item in segment["items"]:
                    for option in item["options"]:
                        if len(option.get("transportOptions") or []) >= 2:
                            other = option["transportOptions"][1]["method"]
                            decision = {"dayIndex": day["dayIndex"], "slot": segment["slot"],
                                        "seq": item["seq"], "chosenTransport": other}
                            print(f"      第{day['dayIndex']}天 把交通换成 {other}")
                            break
                    if decision:
                        break
                if decision:
                    break
            if decision:
                break
    check(bool(decision), f"找到一个可改的节点：{decision or '没有（只有 1 个方案）'}")

    print("\n⑥ 确认 + 解说")
    confirm_began = time.monotonic()
    confirmed = client.post(f"/api/trips/{trip_id}/confirm", json={"decisions": [decision] if decision else []})
    took = time.monotonic() - confirm_began
    check(confirmed.status_code == 200, f"POST confirm → {confirmed.status_code}（{took:.1f} 秒）")
    payload = confirmed.json() if confirmed.status_code == 200 else {}
    intros = payload.get("spotIntros") or []
    tips = payload.get("travelTips") or []
    check(len(intros) >= 3, f"各点介绍 {len(intros)} 条")
    check(all(item.get("intro") and len(item["intro"]) >= 20 for item in intros), "每条介绍都有实质内容")
    check(all(item.get("sourceDoc") for item in intros), "每条介绍都带出处（后端按攻略匹配）")
    check(len(tips) >= 3, f"出行建议 {len(tips)} 条")
    for item in intros[:2]:
        print(f"      【{item['name']}】{item['intro'][:60]}…（出处 {item['sourceDoc']}）")
    for tip in tips[:2]:
        print(f"      - {tip[:70]}")

    print("\n⑦ 确认后的行程（改过的那个节点被收窄并锁定）")
    after = client.get(f"/api/trips/{trip_id}").json()
    if decision.get("chosenPoiId"):
        # 只看**改的那个节点**（别拿别的节点凑数：它们的 options[0] 可能是同一个点）
        node = None
        for day in after["days"]:
            for segment in day["segments"]:
                for item in segment["items"]:
                    if (day["dayIndex"], segment["slot"], item["seq"]) == (
                            decision["dayIndex"], decision["slot"], decision["seq"]):
                        node = item
        if node is None:
            check(False, "改过的那个节点在确认后的行程里找不到了")
        else:
            check(node["options"][0]["poi"]["poiId"] == decision["chosenPoiId"],
                  f"选中方案排到了第一位（{node['options'][0]['poi']['name']}）")
            check(node.get("locked") is True, "这个节点被锁定")
            check(len(node["options"]) == 1, f"候选项被收窄成 {len(node['options'])} 个")
    else:
        check(True, "这轮没有改点（走的是改交通那条路，跳过锁定断言）")

    print("\n⑧⑨ 历史方案")
    listed = client.get("/api/plans")
    check(listed.status_code == 200, f"GET /api/plans → {listed.status_code}")
    items = listed.json()
    check(any(item["planId"] == trip_id for item in items), f"新确认的这趟在列表里（共 {len(items)} 条）")
    one = client.get(f"/api/plans/{trip_id}")
    check(one.status_code == 200, f"GET /api/plans/{trip_id} → {one.status_code}")
    stored = one.json()
    check(len(stored.get("days") or []) == len(days), "存下来的天数与原行程一致")
    check(len(stored.get("lodging") or []) == len(lodging), "存下来的住宿一致")
    check(len(stored.get("intercity") or []) == len(intercity), "存下来的大交通一致")

    print("\n⑩ 错误分支")
    bad_city = dict(body)
    bad_city["destination"] = {"province": "火星省", "city": "不存在市", "centerPlace": None, "mustVisit": []}
    check(client.post("/api/trips", json=bad_city).status_code == 400, "未知城市 → 400")
    check(client.get("/api/plans/nope-nope").status_code == 404, "未知历史方案 → 404")
    check(client.get("/api/trips/nope-nope").status_code == 404, "未知行程 → 404")
    bad_decision = client.post(f"/api/trips/{trip_id}/confirm",
                               json={"decisions": [{"dayIndex": 9, "slot": "morning", "seq": 9,
                                                    "chosenPoiId": "x"}]})
    check(bad_decision.status_code == 422, f"指向不存在的节点 → {bad_decision.status_code}")
    nothing = client.post(f"/api/trips/{trip_id}/confirm",
                          json={"decisions": [{"dayIndex": 1, "slot": "morning", "seq": 1}]})
    check(nothing.status_code == 422, f"决策什么都没改 → {nothing.status_code}")
    check(client.post("/api/trips", json={"mode": "explicit"}).status_code == 422, "缺字段的请求体 → 422")

failed = [text for ok, text in results if not ok]
print("\n" + "=" * 70)
print(f"共 {len(results)} 项断言，通过 {len(results) - len(failed)} 项"
      + (f"，失败 {len(failed)} 项：{failed}" if failed else "，全部通过"))
sys.exit(1 if failed else 0)
