"""长期记忆 —— 记什么、怎么读。

存储：标准库 `sqlite3`，一个文件 `Travele/sqlite_data/memory.db`，四张表（preferences / remarks / visited / plans）。
     不用中间件、不用 ORM、不做摘要压缩（内容很少）。库**不是预置的**：第一次"确认行程"时才建，
      没记过东西时 `memory_text()` 直接返回空串、连库都不建。

读取：`memory_text()` 渲染成**固定顺序**的几行中文，给系统提示词用 ——
     顺序固定是为了让提示词的缓存前缀稳定（同样的记忆每次渲染出同样的文本）。
"""
import datetime
import json
import logging
import pathlib
import sqlite3

logger = logging.getLogger("travele")

DB_DIR = pathlib.Path(__file__).resolve().parents[1] / "sqlite_data"
DB_FILE = DB_DIR / "memory.db"
LOCAL_USER = "local"

MIN_EVIDENCE = 2        # 同一条偏好见够几次才算"长期画像"
MAX_REMARKS = 5         # 提示词里最多带几条补充说明（挑最近的）
MAX_VISITED = 20        # 最多带几个去过的点（挑最近的）


def _connect() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS preferences (
            user_id     TEXT NOT NULL,
            kind        TEXT NOT NULL,          -- like / avoid
            value       TEXT NOT NULL,
            evidence    INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (user_id, kind, value)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS remarks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            text        TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS visited (
            user_id     TEXT NOT NULL,
            place_id    TEXT NOT NULL,
            name        TEXT NOT NULL,
            city        TEXT NOT NULL,
            trip_date   TEXT NOT NULL,
            PRIMARY KEY (user_id, place_id, trip_date)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plans (
            user_id     TEXT NOT NULL,
            trip_id     TEXT NOT NULL,
            title       TEXT NOT NULL,
            province    TEXT NOT NULL,
            city        TEXT NOT NULL,
            start_date  TEXT,
            end_date    TEXT,
            payload     TEXT NOT NULL,      -- 整份行程的 JSON（用户确认过的那一版）
            saved_at    TEXT NOT NULL,
            PRIMARY KEY (user_id, trip_id)
        )""")
    return conn


def _chosen_places(trip) -> list[tuple[str, str, str]]:
    """这趟行程里**确定选中的**点：(place_id, 名字, 日期)。

    用户在前端把方案收窄之后，选中的那个就是 options[0]；所以取它。
    """
    picked = []
    for day in trip.days or []:
        date = day.date.isoformat() if day.date else ""
        for segment in day.segments:
            for item in segment.items:
                if not item.options:
                    continue
                poi = item.options[0].poi
                picked.append((poi.poi_id, poi.name, date))
    return picked


def remember(request, trip) -> None:
    """记一次：偏好 + 补充说明 + 这趟确认过的点。

    偏好走"证据计数"：同一 kind+value 再来一次就 +1，够 MIN_EVIDENCE 次才会被当成长期画像。
    补充说明只存非空的原文；去过是按 (点, 日期) 去重的，同一趟重复排入只记一条。
    """
    now = datetime.datetime.now().isoformat(timespec="seconds")
    city = trip.destination.city if trip.destination else ""

    with _connect() as conn:
        for kind, values in (("like", request.prefs.themes), ("avoid", request.prefs.avoid)):
            for value in values:
                text = (value or "").strip()
                if not text:
                    continue
                conn.execute(
                    "INSERT INTO preferences (user_id, kind, value, evidence, updated_at) "
                    "VALUES (?, ?, ?, 1, ?) "
                    "ON CONFLICT(user_id, kind, value) DO UPDATE SET "
                    "evidence = evidence + 1, updated_at = excluded.updated_at",
                    (LOCAL_USER, kind, text, now))

        remarks = (request.remarks or "").strip()
        if remarks:
            # 同一句补充说明不重复入库（用户可能连续确认好几次，说的还是同一句话）
            conn.execute(
                "INSERT INTO remarks (user_id, text, created_at) "
                "SELECT ?, ?, ? WHERE NOT EXISTS "
                "(SELECT 1 FROM remarks WHERE user_id = ? AND text = ?)",
                (LOCAL_USER, remarks, now, LOCAL_USER, remarks))

        for place_id, name, date in _chosen_places(trip):
            conn.execute(
                "INSERT OR IGNORE INTO visited (user_id, place_id, name, city, trip_date) "
                "VALUES (?, ?, ?, ?, ?)", (LOCAL_USER, place_id, name, city, date))

    # 整份行程也留一份 —— 这是"历史旅游方案"那个标签的数据来源
    save_plan(trip)
    logger.info("长期记忆已更新（偏好 / 补充说明 / 去过的点 / 历史方案）")


def save_plan(trip) -> None:
    """存一份**用户确认过的完整行程**。

    同一趟重复确认不会产生重复记录（按 trip_id 覆盖）；行程 JSON 原样存（camelCase，跟接口一致），
    历史标签直接把它当 TripDetail 渲染，不用再转换一次。
    """
    dates = [day.date for day in (trip.days or []) if day.date is not None]
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO plans (user_id, trip_id, title, province, city, start_date, end_date,"
            " payload, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, trip_id) DO UPDATE SET "
            "title = excluded.title, payload = excluded.payload, saved_at = excluded.saved_at",
            (LOCAL_USER, trip.trip_id, trip.title,
             trip.destination.province if trip.destination else "",
             trip.destination.city if trip.destination else "",
             dates[0].isoformat() if dates else None,
             dates[-1].isoformat() if dates else None,
             trip.model_dump_json(by_alias=True, exclude_none=True), now))


def list_plans() -> list[dict]:
    """历史方案列表（只有列表页要用的那几列，不带整份行程）。"""
    if not DB_FILE.exists():
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT trip_id, title, province, city, start_date, end_date, saved_at FROM plans "
            "WHERE user_id = ? ORDER BY saved_at DESC", (LOCAL_USER,)).fetchall()
    return [{"plan_id": row[0], "title": row[1], "province": row[2], "city": row[3],
             "start_date": row[4], "end_date": row[5], "saved_at": row[6]} for row in rows]


def get_plan(plan_id: str) -> dict | None:
    """取一份完整的历史行程（JSON 反序列化回 dict，接口层直接当 TripDetail 用）。"""
    if not DB_FILE.exists():
        return None
    with _connect() as conn:
        row = conn.execute("SELECT payload FROM plans WHERE user_id = ? AND trip_id = ?",
                           (LOCAL_USER, plan_id)).fetchone()
    return json.loads(row[0]) if row else None


def memory_text() -> str:
    """给提示词用的记忆文本。没有记忆就返回空串（提示词那边会渲染成"（暂无记忆）"）。

    只输出有内容的段，顺序固定（喜欢 → 忌讳 → 补充说明 → 去过），
    这样同一个人的同一份画像每次渲染出来是同一段文本，提示词缓存才不会白费。
    """
    if not DB_FILE.exists():
        return ""                       # 还没记过任何东西，别为了读一次去建库

    with _connect() as conn:
        likes = [row[0] for row in conn.execute(
            "SELECT value FROM preferences WHERE user_id = ? AND kind = 'like' "
            "AND evidence >= ? ORDER BY evidence DESC, value", (LOCAL_USER, MIN_EVIDENCE))]
        avoids = [row[0] for row in conn.execute(
            "SELECT value FROM preferences WHERE user_id = ? AND kind = 'avoid' "
            "AND evidence >= ? ORDER BY evidence DESC, value", (LOCAL_USER, MIN_EVIDENCE))]
        remarks = [row[0] for row in conn.execute(
            "SELECT text FROM remarks WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (LOCAL_USER, MAX_REMARKS))]
        visited = [(row[0], row[1], row[2]) for row in conn.execute(
            "SELECT name, city, trip_date FROM visited WHERE user_id = ? "
            "ORDER BY trip_date DESC, name LIMIT ?", (LOCAL_USER, MAX_VISITED))]

    lines: list[str] = []
    if likes:
        lines.append("喜欢：" + "、".join(likes))
    if avoids:
        lines.append("忌讳：" + "、".join(avoids))
    for text in dict.fromkeys(remarks):        # 同样的补充说明只说一次
        lines.append(f"补充说明（用户原话）：{text}")
    if visited:
        shown = "、".join(f"{name}（{date[:7]}{'，' + city if city else ''}）"
                          for name, city, date in visited)
        lines.append("去过的点：" + shown)
    return "\n".join(lines)
