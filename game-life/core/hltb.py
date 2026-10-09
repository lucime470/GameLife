"""HLTB（HowLongToBeat）数据获取。

按游戏名调用 HLTB 搜索接口，返回 主线/主线+支线/全成就 三类时长；
限速（1s/次）+ SQLite 缓存（含负缓存：查不到也记一笔，7 天内不再请求）；
查不到或网络失败一律返回 None，不阻塞主流程。

字段约定（供 classifier/推荐使用）：
    main / extra / completionist —— 单位：分钟（整数）
    口径：HLTB 区间文本取下限，如 "12½ - 20 Hours" 取 12.5 小时；
    main=主线、extra=主线+支线、completionist=全成就(100%)。
"""
import re
import time
from datetime import date

import requests

from data.schema import _is_fresh, get_connection, init_db

SEARCH_URL = "https://howlongtobeat.com/api/search"
REQUEST_INTERVAL = 1.0     # HLTB 接口较敏感，两次请求至少间隔 1s
MAX_RETRIES = 3
RETRY_DELAY = 2
CACHE_DAYS = 7

_last_req: float = 0.0
_memory: dict[int, tuple] = {}


def _throttle():
    global _last_req
    wait = REQUEST_INTERVAL - (time.time() - _last_req)
    if wait > 0:
        time.sleep(wait)
    _last_req = time.time()


def _post_search(name: str):
    """调用 HLTB 搜索接口，失败返回 None。"""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://howlongtobeat.com/",
    }
    payload = {
        "searchType": "games",
        "searchTerms": [name],
        "searchPage": 1,
        "size": 5,
        "searchOptions": {
            "games": {
                "userId": 0,
                "platform": "",
                "sortCategory": "popular",
                "rangeCategory": "main",
                "rangeTime": {"min": 0, "max": 0},
                "gameplay": {"perspective": "", "flow": "", "genre": ""},
                "modifier": "",
            }
        },
    }
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            resp = requests.post(SEARCH_URL, json=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            print(f"  HLTB 请求失败({attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (2 ** attempt))
        except ValueError:
            return None  # 返回体不是 JSON
    return None


def _parse_hours(raw) -> float | None:
    """HLTB 时长文本转小时数（区间取下限），解析不了返回 None。"""
    if not raw:
        return None
    text = str(raw).strip()
    text = text.split("Hours")[0].strip()  # 去掉单位
    text = text.replace("\u00bd", ".5")    # ½ -> 0.5
    m = re.match(r"([\d.]+)", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _to_minutes(hours) -> int | None:
    if hours is None:
        return None
    return int(round(hours * 60))


def _search_times(name: str) -> dict | None:
    """按名称搜索，返回 {main, extra, completionist}（分钟），无数据返回 None。"""
    data = (_post_search(name) or {}).get("data") or []
    for item in data:
        if not isinstance(item, dict):
            continue
        times = {
            "main": _to_minutes(_parse_hours(item.get("comp_main"))),
            "extra": _to_minutes(_parse_hours(item.get("comp_plus"))),
            "completionist": _to_minutes(_parse_hours(item.get("comp_100"))),
        }
        if any(v is not None for v in times.values()):
            return times
    return None


def _cached(appid: int):
    """读缓存：返回 (times, found)；无缓存/过期返回 None。"""
    if appid in _memory:
        return _memory[appid]
    init_db()
    db = get_connection()
    try:
        row = db.execute(
            "SELECT main, extra, completionist, last_updated FROM hltb WHERE appid = ?",
            (appid,),
        ).fetchone()
    finally:
        db.close()
    if not row or not _is_fresh(row[3]):
        return None
    times = {"main": row[0], "extra": row[1], "completionist": row[2]}
    out = (times, any(v is not None for v in times.values()))
    _memory[appid] = out
    return out


def _save(appid: int, name: str, times: dict):
    init_db()
    db = get_connection()
    try:
        db.execute(
            "INSERT OR REPLACE INTO hltb (appid, name, main, extra, completionist, last_updated) VALUES (?,?,?,?,?,?)",
            (appid, name, times["main"], times["extra"], times["completionist"],
             date.today().isoformat()),
        )
        db.commit()
    finally:
        db.close()
    _memory[appid] = (times, any(v is not None for v in times.values()))


def get_hltb(appid: int, name: str) -> dict | None:
    """按游戏名查询 HLTB 时长（分钟）；查不到/失败返回 None。"""
    hit = _cached(appid)
    if hit is not None:
        times, found = hit
        return times if found else None

    times = _search_times(name)
    if times:
        _save(appid, name, times)
        return times
    # 负缓存：避免下轮重复请求
    _save(appid, name, {"main": None, "extra": None, "completionist": None})
    return None


def fill_hltb(games: dict) -> dict:
    """批量补齐 HLTB（供 CLI run 流程调用）。

    games: {appid: {"name": str, ...}} 或 {appid: str(name)}
    返回 {appid: {main, extra, completionist} | None}
    """
    result = {}
    count = 0
    for appid, info in games.items():
        name = info if isinstance(info, str) else (info or {}).get("name", "")
        if not name:
            result[appid] = None
            continue
        result[appid] = get_hltb(appid, name)
        count += 1
        if count % 20 == 0:
            print(f"HLTB 进度: {count}/{len(games)}")
    return result
