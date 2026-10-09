"""成就数据获取（Steam）。

优先级：
  1. 有 API Key → 官方 ISteamUserStats 接口，精确个人完成度。
  2. 无 Key → 社区资料页（个人资料公开时），按解锁图统计个人进度。
  3. 均失败 → 社区全局成就页全球平均解锁率，仍不行则标"数据不足"。

特性：限速（300ms/次）+ 指数退避重试 + 内存缓存 + SQLite 双缓存。
"""
import json
import re
import time
from datetime import date

import requests

from data.schema import _is_fresh, get_connection, init_db

URL_PLAYER_ACH = "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/"
URL_PROFILE = "https://steamcommunity.com/profiles/{steam_id}/stats/{appid}"
URL_GLOBAL = "https://steamcommunity.com/stats/{appid}/achievements/"

REQUEST_INTERVAL = 0.3
MAX_RETRIES = 3
RETRY_DELAY = 2

_memory: dict[int, dict] = {}
_last_req: float = 0.0


def _throttle():
    global _last_req
    wait = REQUEST_INTERVAL - (time.time() - _last_req)
    if wait > 0:
        time.sleep(wait)
    _last_req = time.time()


def _get(url: str, timeout: int = 20):
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            print(f"  成就请求失败({attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (2 ** attempt))
    return None


def _pack(appid: int, achieved, total: int, source: str, rate=None) -> dict:
    if rate is None and total and achieved is not None:
        rate = round(achieved / total * 100, 2)
    row = {
        "appid": appid,
        "achieved_count": achieved,
        "total_count": total,
        "completion_rate": rate,
        "source": source,
    }
    _memory[appid] = row
    return row


def _write_cache(row: dict):
    init_db()
    db = get_connection()
    try:
        db.execute(
            "INSERT OR REPLACE INTO achievements (appid, source, achieved_count, total_count, completion_rate, last_updated) VALUES (?,?,?,?,?,?)",
            (
                row["appid"],
                row["source"],
                row["achieved_count"],
                row["total_count"],
                row["completion_rate"],
                date.today().isoformat(),
            ),
        )
        db.commit()
    finally:
        db.close()


def _local_row(appid: int, account_id: str | None = None, steam_path: str | None = None):
    """本机 Steam 客户端成就缓存（离线，大陆网络无需加速器）。"""
    try:
        from core import achievements_local
    except ImportError:
        return None
    try:
        data = achievements_local.local_achievement(
            appid, account_id=account_id, steam_path=steam_path
        )
    except Exception:
        return None
    if not data or not data.get("total_count"):
        return None
    return _pack(appid, data.get("achieved_count"), int(data["total_count"]), "steam_client")


def _cached(appid: int, account_id: str | None = None,
            steam_path: str | None = None) -> dict | None:
    if appid in _memory:
        return _memory[appid]
    init_db()
    db = get_connection()
    try:
        row = db.execute(
            "SELECT source, achieved_count, total_count, completion_rate, last_updated FROM achievements WHERE appid = ?",
            (appid,),
        ).fetchone()
    finally:
        db.close()
    if not row or not _is_fresh(row[4]):
        # 数据库没有 → 直接读本机 Steam 成就缓存（离线可用，无需联网）
        return _local_row(appid, account_id, steam_path)
    r = {
        "appid": appid,
        "achieved_count": row[1],
        "total_count": row[2],
        "completion_rate": row[3],
        "source": row[0],
    }
    _memory[appid] = r
    return r


def _via_api(appid: int, steam_id: str, api_key: str):
    url = f"{URL_PLAYER_ACH}?key={api_key}&steamid={steam_id}&appid={appid}"
    resp = _get(url)
    if resp is None:
        return None
    try:
        stats = resp.json().get("playerstats", {})
    except (ValueError, AttributeError):
        return None
    if not stats.get("success"):
        return None
    items = stats.get("achievements", [])
    if not items:
        return None
    achieved = sum(1 for it in items if it.get("achieved"))
    return _pack(appid, achieved, len(items), "api")


def _via_profile(appid: int, steam_id: str):
    url = URL_PROFILE.format(steam_id=steam_id, appid=appid)
    resp = _get(url)
    if resp is None:
        return None
    html = resp.text
    # 以 achieveRow 分块，每块一个成就行
    blocks = re.split(r"class=\"achieveRow", html)
    unlocked = total = 0
    for b in blocks[1:]:
        m = re.search(r"img_(\w+)", b)
        if m:
            total += 1
            if m.group(1) == "unlocked":
                unlocked += 1
    if total == 0:
        return None
    return _pack(appid, unlocked, total, "profile")


def _via_global(appid: int):
    url = URL_GLOBAL.format(appid=appid)
    resp = _get(url)
    if resp is None:
        return None
    html = resp.text
    m = re.search(r"g_rgGlobalStats\s*=\s*(\[.*?\])\s*;", html, re.S)
    if not m:
        return None
    try:
        stats = json.loads(m.group(1))
    except ValueError:
        return None
    percents = [float(s.get("percent", 0)) for s in stats if isinstance(s, dict)]
    if not percents:
        return None
    rate = round(sum(percents) / len(percents), 2)
    return _pack(appid, None, len(percents), "global", rate)


def fetch_achievements(appids: list[int], steam_id: str,
                       api_key: str | None = None,
                       account_id: str | None = None,
                       steam_path: str | None = None) -> dict[int, dict]:
    """对外主入口：本机缓存 → 官方 API → 社区资料页 → 全球统计页。

    本机 Steam 客户端缓存优先，意味着大陆网络下**不必开加速器**也能出完成度；
    只有本机没有该游戏成就时才回落到联网接口。
    """
    result = {}
    for appid in appids:
        cached = _cached(appid, account_id, steam_path)
        if cached is not None:
            result[appid] = cached
            continue

        row = _local_row(appid, account_id, steam_path)
        if row is None:
            if api_key and steam_id:
                row = _via_api(appid, steam_id, api_key)
            if row is None and steam_id:
                row = _via_profile(appid, steam_id)
            if row is None:
                row = _via_global(appid)
        if row is None:
            row = _pack(appid, None, 0, "", None)
            row["error"] = "数据不足（本机缓存与社区页均无数据）"
        _write_cache(row)
        result[appid] = row
    return result


def get_cached(appid: int) -> dict | None:
    """只查缓存，不发网络请求（缓存没有再读本机 Steam 成就文件）。"""
    return _cached(appid)


def get_cached_or_local(appid: int, account_id: str | None = None,
                        steam_path: str | None = None) -> dict | None:
    """缓存 → 本机成就文件，全程离线。"""
    return _cached(appid, account_id, steam_path)
