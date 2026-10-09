"""每小时单价榜（分析引擎层 · 模块 4：value_ranking）。

按《03_分析引擎层设计文档.md》第六章：
    单价(元/小时) = 价格(元) / (游玩小时 + 0.5)
  - +0.5 防止除零，同时避免"玩了 1 分钟"的极端数据；
  - **价格三态**（关键修正）：
      * 有价（price > 0）→ 参与性价比榜 / 最浪费榜；
      * 免费（is_free 为真，或带官方 "Free to Play" 类型）→ 进"零成本榜"；
      * **未知**（既没有正价格、也没有免费标记）→ **绝不当作免费**，
        不参与任何榜单，只计入 stats.unknown_price_count。
    历史实现把「缺失」当成 price=0 免费，导致 179 款里 174 款被误判为免费。
  - 游玩 < 10 分钟的不计入性价比榜，单独归入"最浪费榜"（按单价降序，
    并标记 barely_played=True）。

设计约定：
  - 纯函数、纯 Python、可单测；输出纯 dict，终端与 HTML 渲染通用；
  - 价格单位：数据底座层 games.price 为 Steam 初始价格（分），
    默认 price_divisor=100 换算为元；
  - 不 import 状态引擎；可选 statuses 用于为榜单条目补充状态字段。
"""

from __future__ import annotations

from typing import Any

from . import display
from .stats import load_rules, _to_int


def _cached_games(appids) -> dict:
    """本地 games 缓存兜底（离线、只读）。

    调用方（如 CLI 未加 --with-metadata）没给元数据时，用库里已缓存的价格/免费标记
    补上，避免榜单因「没有元数据」而空白。任何异常都降级为 {}。
    """
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


DEFAULT_TOP_N = 10          # 各榜单条数
MIN_PLAYTIME_MINUTES = 10   # 计入性价比榜的最小时长（分钟）
PRICE_DIVISOR = 100         # 价格单位：分 -> 元

# 非游戏条目（Steam 客户端/截图/壁纸引擎/性能工具等）：不参与任何榜单。
# 判定口径收敛到 core.non_game（全产品唯一口径）—— 此前这里只有 3 项黑名单、
# 不查 appinfo 类型，于是「Wallpaper Engine：壁纸引擎」(431960, Application)
# 混进了性价比榜第 5 名。
from core import non_game  # noqa: E402

NON_GAME_APPIDS = non_game.NON_GAME_APPIDS

# 官方 "Free to Play" 类型标签（用于在缺少 is_free 标记时确认免费）
FREE_GENRES = frozenset({"free to play", "free-to-play", "free"})

_TRUTHY = (True, 1, "1", "true", "True", "yes", "是")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "是") if value is not None else False


def price_state(meta: dict | None) -> str:
    """把一条游戏的元数据判成三态："paid" / "free" / "unknown"。

    这是全项目价格语义的**唯一判定入口**（wishlist_guard 直接复用）：
      * ``is_free`` 为真 -> free（商店明确标注免费）；
      * ``price`` > 0 -> paid；
      * 带官方 "Free to Play" 类型 -> free；
      * 其余（0 / 空 / 缺失）-> unknown，**不得当作免费**。
    """
    meta = meta or {}
    if _as_bool(meta.get("is_free")):
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


def _cost_per_hour(price_fen: int, playtime_minutes: int, price_divisor: int) -> float:
    """单价 = 价格(元) / (游玩小时 + 0.5)。"""
    hours = playtime_minutes / 60.0
    return round(price_fen / price_divisor / (hours + 0.5), 2)


def compute_value_ranking(
    play_stats: dict,
    games: dict,
    statuses: dict | None = None,
    rules: dict[str, Any] | None = None,
    top_n: int | None = None,
    price_divisor: int | None = None,
    min_playtime_minutes: int | None = None,
) -> dict:
    """计算每小时单价排行榜。

    参数：
        play_stats: dict[appid, {playtime: 分钟, last_played: unix 秒}]
        games:      dict[appid, {name, price(分), ...}]
        statuses:   可选 dict[appid, 状态名]（为条目补充状态字段）
        rules:      可选规则 dict（兼容 stats 风格；本模块未用其中键）
        top_n:      各榜单条数，缺省 10
        price_divisor: 价格换算除数，缺省 100（分 -> 元）
        min_playtime_minutes: 计入性价比榜的最小时长，缺省 10

    返回：{"value_top", "waste_top", "zero_cost_top", "stats", ...}
    """
    rules = rules or load_rules()
    top_n = _to_int(top_n) or DEFAULT_TOP_N
    divisor = _to_int(price_divisor) or PRICE_DIVISOR
    min_minutes = _to_int(min_playtime_minutes) or MIN_PLAYTIME_MINUTES

    play_stats = play_stats or {}
    games = games or {}
    if not games and play_stats:
        # CLI 未抓元数据：惰性回落到本地缓存（离线可算）
        games = _cached_games(play_stats.keys())
    if play_stats:
        # 用本地缓存补齐调用方元数据里缺失的 price / is_free（面板经 render_bridge
        # 只有 price 列，没有 is_free；不补的话真免费游戏会被误判为「价格未知」）
        games = _enrich_with_cache(games, play_stats.keys())
    library = sorted(set(play_stats) | set(games))

    value_items: list[dict] = []   # 有价且已玩 >= 最短时长的游戏
    waste_items: list[dict] = []   # 全部有价游戏（最浪费榜）
    free_items: list[dict] = []    # 确认为免费的游戏（零成本榜）
    unknown_items: list[dict] = []  # 价格未知：不参与任何榜单

    for appid in library:
        if non_game.is_non_game(appid):
            continue
        meta = games.get(appid) or {}
        minutes = _to_int((play_stats.get(appid) or {}).get("playtime", 0))
        price_fen = max(0, _to_int(meta.get("price", 0)))
        state = price_state(meta)
        item = {
            "appid": appid,
            "name": display.display_name(appid, meta),
            "price_fen": price_fen,
            "price_yuan": round(price_fen / divisor, 2) if divisor > 0 else 0.0,
            "price_state": state,
            "playtime_minutes": minutes,
            "playtime_hours": round(minutes / 60, 1),
        }
        if statuses and appid in statuses:
            item["status"] = str(statuses[appid])

        if state == "unknown" or divisor <= 0:
            # 价格未知 ≠ 免费：既不进零成本榜，也不参与性价比排序
            (unknown_items if state == "unknown" else free_items).append(item)
            continue
        if state == "free" or price_fen <= 0:
            free_items.append(item)
            continue
        item["cost_per_hour"] = _cost_per_hour(price_fen, minutes, divisor)
        waste_items.append(item)
        if minutes >= min_minutes:
            value_items.append(item)

    for item in waste_items:
        item["barely_played"] = item["playtime_minutes"] < min_minutes

    value_top = sorted(
        value_items, key=lambda item: (item["cost_per_hour"], -item["playtime_minutes"])
    )[: max(0, top_n)]
    waste_top = sorted(
        waste_items,
        key=lambda item: (-item["cost_per_hour"], item["playtime_minutes"]),
    )[: max(0, top_n)]
    zero_cost_top = sorted(
        free_items, key=lambda item: -item["playtime_minutes"]
    )[: max(0, top_n)]
    unknown_items.sort(key=lambda item: -item["playtime_minutes"])

    return {
        "price_unit": "元",
        "formula": "价格(元) / (游玩小时 + 0.5)",
        "min_playtime_minutes": min_minutes,
        "value_top": value_top,
        "waste_top": waste_top,
        "zero_cost_top": zero_cost_top,
        "stats": {
            "paid_count": len(waste_items),
            "free_count": len(free_items),
            # 价格未知：既非有价也非免费，未参与任何榜单（面板据此提示「价格数据不足」）
            "unknown_price_count": len(unknown_items),
            "library_count": len([a for a in library if not non_game.is_non_game(a)]),
            "barely_played_count": sum(1 for i in waste_items if i["barely_played"]),
        },
    }


def run(args: Any) -> int:
    """CLI 入口（供 main.py 调用）：要求 args 含 play_stats/games，
    可选 top_n/statuses，打印三个榜单并返回退出码。"""
    result = compute_value_ranking(
        getattr(args, "play_stats", None) or {},
        getattr(args, "games", None) or {},
        statuses=getattr(args, "statuses", None),
        top_n=getattr(args, "top_n", None),
    )

    def _line(item: dict) -> str:
        extra = f"（{item['playtime_hours']}h）" if item.get("playtime_hours") else ""
        return f"  {item['name']}  {item['price_yuan']}元 / {item['cost_per_hour']}元每小时{extra}"

    print("== 性价比 TOP（玩得久+买得便宜）==")
    for item in result["value_top"]:
        print(_line(item))
    print("== 最浪费 TOP（买得贵+玩得少）==")
    for item in result["waste_top"]:
        flag = "  [几乎没玩]" if item.get("barely_played") else ""
        print(_line(item) + flag)
    print("== 零成本 TOP（免费游戏时长榜）==")
    for item in result["zero_cost_top"]:
        print(f"  {item['name']}  已玩 {item['playtime_hours']}h")
    stats = result.get("stats") or {}
    if not result["value_top"] and not result["waste_top"]:
        print(f"（提示：{stats.get('unknown_price_count', 0)} 款游戏价格未知，"
              f"暂无法计算性价比。带 --with-metadata 运行一次即可联网取价："
              f"main.py price --with-metadata）")
    return 0