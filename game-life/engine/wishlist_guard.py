"""反向愿望单（分析引擎层 · 模块 7：wishlist_guard）。

按《03_分析引擎层设计文档.md》第九章：
按 genres 分组统计"买了却玩得少"的游戏，给出"别再买这类"的警告。

判定：
  1. 每类统计 拥有数 / 总时长 / 平均时长 / 游玩率 / 吃灰率（未玩+搁置）；
  2. 满足【该类 >= 3 款 且 平均游玩 < 1 小时】的类型进入警告；
  3. **全部已知价格且均为免费**的类型不告警（"别再买"对免费游戏无意义；价格未知的类型仍告警）。

价格语义（与 engine.value_ranking.price_state 保持一致）：
  * is_free 为真 -> 免费；price > 0 -> 有价；带官方 "Free to Play" 类型 -> 免费；
  * 其余 -> 未知。**未知既不算免费，也不计入平均单价**（否则 avg_price_yuan 会被拉成 0）。

设计约定：
  - 纯函数、纯 Python、可单测；输出纯 dict，终端与 HTML 渲染通用；
  - 一款游戏可归入多个类型（多类型游戏在每个类型各计一次）；
  - 不 import 状态引擎：状态优先用调用方传入的 statuses，
    缺省走 stats 的兜底规则（用于吃灰率/通关统计）。
"""

from __future__ import annotations

import time
from typing import Any

from . import display
from .stats import load_rules, _as_list, _resolve_statuses, _to_int
from core import non_game  # noqa: E402  非游戏条目唯一口径


def _cached_games(appids) -> dict:
    """本地 games 缓存兜底（离线、只读）——CLI 未抓元数据时避免榜单空白。"""
    try:
        from core import store_api
    except Exception:
        return {}
    try:
        return store_api.cached_metadata(list(appids)) or {}
    except Exception:
        return {}

def _enrich_with_cache(games: dict, appids) -> dict:
    """用本地 games 缓存补全每款游戏缺失的 price / is_free（不覆盖已有非空值）。

    面板经 render_bridge.fill_metadata 拿到的元数据只有 price 列、没有 is_free；
    不补的话真免费游戏会被误判为「价格未知」。离线只读，缓存不可用时原样返回。
    """
    try:
        from core import store_api
        cached = store_api.cached_metadata(list(appids)) or {}
    except Exception:
        return games
    if not cached:
        return games
    for appid, meta in games.items():
        if not isinstance(meta, dict):
            continue
        row = cached.get(appid) or cached.get(str(appid)) or {}
        if not row:
            continue
        if not _to_int(meta.get("price", 0)) and _to_int(row.get("price", 0)):
            meta["price"] = _to_int(row.get("price", 0))
        if not meta.get("is_free") and row.get("is_free"):
            meta["is_free"] = True
    return games


WARN_MIN_COUNT = 3           # 告警的最少拥有数
WARN_MAX_AVG_HOURS = 1.0     # 告警的平均游玩时长上限（小时）
DUST_STATUSES = ("未玩", "搁置")
FREE_GENRES = frozenset({"free to play", "free-to-play", "free"})
_TRUTHY = (True, 1, "1", "true", "True", "yes", "是")


def _price_state(meta: dict | None) -> str:
    """价格三态："paid" / "free" / "unknown"（与 value_ranking.price_state 同源逻辑）。

    本模块不 import engine.value_ranking，避免循环依赖与耦合，故此处保持一致的小实现。
    """
    meta = meta or {}
    is_free = meta.get("is_free")
    if is_free is True or (is_free is not None and str(is_free).strip().lower() in ("1", "true", "yes", "是")):
        return "free"
    if _to_int(meta.get("price", 0)) > 0:
        return "paid"
    genres = meta.get("genres")
    if isinstance(genres, str):
        genres = [part.strip() for part in genres.split(",")]
    if isinstance(genres, (list, tuple, set)):
        for genre in genres:
            if str(genre).strip().lower() in FREE_GENRES:
                return "free"
    return "unknown"


def _warning_message(genre: str, owned: int, total_hours: float, avg_hours: float) -> str:
    """按 9.3 示例生成警告文案。"""
    if total_hours <= 0:
        return (
            f"{genre}类：你买了 {owned} 款，共玩 0h，买了没碰过。"
            f"建议：先通关其中一款再考虑买新的。"
        )
    return (
        f"{genre}类：你买了 {owned} 款，总游玩 {total_hours:.1f}h，"
        f"平均每款 {avg_hours * 60:.0f} 分钟。"
        f"下次看到「{genre}」标签打折，请冷静三秒。"
    )


def compute_wishlist_guard(
    play_stats: dict,
    games: dict,
    statuses: dict | None = None,
    rules: dict[str, Any] | None = None,
    today: int | None = None,
    min_count: int | None = None,
    max_avg_hours: float | None = None,
) -> dict:
    """生成反向愿望单警告。

    参数：
        play_stats: dict[appid, {playtime: 分钟, last_played: unix 秒}]
        games:      dict[appid, {name, genres, price, ...}]
        statuses:   可选 dict[appid, 状态名]（用于吃灰率/游玩率）
        rules:      可选规则 dict（缺省自动读取 rules.json）
        today:      可选 unix 时间戳（便于测试）
        min_count:  告警的最少拥有数，缺省 3
        max_avg_hours: 告警的平均时长上限（小时），缺省 1.0

    返回：{"warnings": [...], "genres": [...全部类型统计...]}
          warnings 按平均时长升序、拥有数降序排序。
    """
    rules = rules or load_rules()
    today_ts = _to_int(today) or int(time.time())
    min_count = _to_int(min_count) or WARN_MIN_COUNT
    max_avg_hours = WARN_MAX_AVG_HOURS if max_avg_hours is None else float(max_avg_hours)

    play_stats = play_stats or {}
    games = games or {}
    if not games and play_stats:
        games = _cached_games(play_stats.keys())
    if play_stats:
        games = _enrich_with_cache(games, play_stats.keys())
    # 用户第 8 项：非游戏条目（壁纸引擎 / 镜像资讯这类软件）会带进
    # 「实用工具 / 软件培训 / 照片编辑」等**软件类型**，必须在类型统计前剔掉。
    # 口径统一走 core.non_game（类型未知一律放行，绝不误杀真游戏）。
    library = sorted(a for a in (set(play_stats) | set(games)) if not non_game.is_non_game(a))
    status_of = _resolve_statuses(play_stats, statuses, rules, today_ts)

    genre_stats: dict[str, dict[str, Any]] = {}

    def _bucket(genre: str) -> dict[str, Any]:
        return genre_stats.setdefault(
            genre,
            {
                "genre": genre,
                "owned_count": 0,
                "total_playtime_minutes": 0,
                "played_count": 0,
                "dust_count": 0,
                "free_count": 0,
                "paid_count": 0,
                "unknown_price_count": 0,
                "price_fen_total": 0,
            },
        )

    for appid in library:
        meta = games.get(appid) or {}
        minutes = _to_int((play_stats.get(appid) or {}).get("playtime", 0))
        status = status_of.get(appid, "未玩")
        price_fen = max(0, _to_int(meta.get("price", 0)))
        state = _price_state(meta)
        # 展示用中文类型；免费/价格判定仍走 _price_state(meta) 的官方英文类型
        for genre in display.display_genres(meta):
            bucket = _bucket(genre)
            bucket["owned_count"] += 1
            bucket["total_playtime_minutes"] += minutes
            if state == "unknown":
                # 价格未知：不计入平均单价，也不算免费
                bucket["unknown_price_count"] += 1
            else:
                bucket["price_fen_total"] += price_fen
                if state == "free" or price_fen <= 0:
                    bucket["free_count"] += 1
                else:
                    bucket["paid_count"] += 1
            if minutes > 0:
                bucket["played_count"] += 1
            if status in DUST_STATUSES:
                bucket["dust_count"] += 1

    genres = []
    for bucket in genre_stats.values():
        owned = bucket["owned_count"]
        total_hours = bucket["total_playtime_minutes"] / 60.0
        avg_hours = total_hours / owned if owned else 0.0
        genres.append(
            {
                "genre": bucket["genre"],
                "owned_count": owned,
                "total_playtime_minutes": bucket["total_playtime_minutes"],
                "total_playtime_hours": round(total_hours, 1),
                "avg_playtime_hours": round(avg_hours, 2),
                "avg_playtime_minutes": round(avg_hours * 60),
                "play_ratio": round(bucket["played_count"] / owned * 100, 1) if owned else 0.0,
                "dust_rate": round(bucket["dust_count"] / owned * 100, 1) if owned else 0.0,
                # 平均单价只按「已知价格」的款数求平均，未知价不计入分母
                "avg_price_yuan": round(
                    bucket["price_fen_total"] / (bucket["paid_count"] + bucket["free_count"]) / 100, 2
                ) if (bucket["paid_count"] + bucket["free_count"]) else 0.0,
                "free_count": bucket["free_count"],
                "paid_count": bucket["paid_count"],
                "unknown_price_count": bucket["unknown_price_count"],
            }
        )
    genres.sort(key=lambda item: (-item["owned_count"], item["avg_playtime_hours"]))

    warnings = []
    for item in genres:
        if item["owned_count"] < min_count:
            continue
        if item["avg_playtime_hours"] >= max_avg_hours:
            continue
        if item["free_count"] == item["owned_count"] and item["unknown_price_count"] == 0:
            continue  # 全部已知价格且均为免费 -> 不告警（价格未知的类型仍告警）
        warnings.append(
            {
                **item,
                "warning_message": _warning_message(
                    item["genre"],
                    item["owned_count"],
                    item["total_playtime_hours"],
                    item["avg_playtime_hours"],
                ),
            }
        )
    warnings.sort(key=lambda item: (item["avg_playtime_hours"], -item["owned_count"]))

    return {"warnings": warnings, "genres": genres}


def run(args: Any) -> int:
    """CLI 入口（供 main.py 调用）：要求 args 含 play_stats/games，
    可选 statuses，打印反向愿望单警告并返回退出码。"""
    result = compute_wishlist_guard(
        getattr(args, "play_stats", None) or {},
        getattr(args, "games", None) or {},
        statuses=getattr(args, "statuses", None),
    )
    print("== 反向愿望单提醒 ==")
    if not result["warnings"]:
        print("  暂无明显「买了不玩」的类型，保持冷静。")
        return 0
    for item in result["warnings"]:
        print(f"\n{item['warning_message']}")
        print(
            f"  （{item['owned_count']} 款，吃灰率 {item['dust_rate']}%，"
            f"平均单价 {item['avg_price_yuan']}元）"
        )
    return 0