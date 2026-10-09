"""游戏人格测试（分析引擎层 · 模块 3：persona）。

按《03_分析引擎层设计文档.md》第五章实现：
分析游戏库游玩数据，输出 5 个维度得分（0-100）与人格标签、
一句话总结；供 CLI（persona 子命令）与 HTML 渲染共用。

维度与权重（5.2）：
    purchase  购买力 20%  总游戏数 / 库龄（年）
    execution 执行力 25%  已玩占比（玩过的游戏数 / 总游戏数）
    focus     专注度 20%  单款最高时长 / 中位数时长
    breadth   口味广度 15% 涉及类型数（按观察类型池上限估算）
    courage   勇气值 20%  高难度标签（Hard/Souls 等）占比

设计约定：
  - 纯函数、纯 Python、可单测；输出纯 dict，不依赖渲染层；
  - 不 import 状态引擎（classifier.py）：已玩占比来自 stats.compute_stats
    的 played_ratio（不依赖 HLTB 「通关」判定）；
  - 库龄无法从库数据直接得知，library_age_years 缺省按 1 年估算，
    调用方（如 main.py）可通过参数传入真实库龄。
"""

from __future__ import annotations

import json
from statistics import median
from typing import Any

from . import display
from .stats import compute_stats, load_rules

# 各维度归一化标定系数（将原始数值映射到 0-100，见各维度实现注释）
DIMENSION_WEIGHTS: dict[str, float] = {
    "purchase": 0.20,
    "execution": 0.25,
    "focus": 0.20,
    "breadth": 0.15,
    "courage": 0.20,
}

# 高难度标签（勇气值判定），匹配 Steam genres/categories 中的子串
HARD_TAGS = ("Hard", "Hardcore", "Souls", "Difficult")

# 标签判定阈值：>= HIGH 算高，< LOW 算低
HIGH_SCORE = 60.0
LOW_SCORE = 40.0

_DIMENSION_NAMES = {
    "purchase": "购买力",
    "execution": "执行力",
    "focus": "专注度",
    "breadth": "口味广度",
    "courage": "勇气值",
}


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_list(value: Any) -> list[str]:
    """genres/categories 字段兼容 list、JSON 字符串或逗号分隔字符串。"""
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                data = json.loads(text)
                if isinstance(data, list):
                    return [str(item) for item in data if str(item).strip()]
            except (ValueError, TypeError):
                pass
        return [item.strip() for item in text.split(",") if item.strip()]
    return []


def _is_hard_game(meta: dict) -> bool:
    values = _as_list(meta.get("genres")) + _as_list(meta.get("categories"))
    for value in values:
        lowered = value.lower()
        if any(tag.lower() in lowered for tag in HARD_TAGS):
            return True
    return False


def _score_purchase(total_games: int, library_age_years: float) -> float:
    """购买力：总游戏数 / 库龄。标定：每年 10 款为 100 分。"""
    if total_games <= 0:
        return 0.0
    age = max(0.5, library_age_years)
    return round(min(100.0, total_games / age * 10.0), 1)


def _score_focus(playtime_minutes: list[int]) -> float:
    """专注度：单款最高时长 / 中位数时长（仅统计玩过的游戏）。
    标定：比值 5 为 100 分；没有玩过任何游戏则 0 分。"""
    played = sorted(m for m in playtime_minutes if m > 0)
    if not played:
        return 0.0
    middle = median(played)
    if middle <= 0:
        return 0.0
    return round(min(100.0, max(played) / middle * 20.0), 1)


def _score_breadth(unique_genres: int) -> float:
    """口味广度：涉及类型数。库数据无法得知全局类型总数，
    按常见类型池约 13 类估算，即每个类型约 8 分。"""
    if unique_genres <= 0:
        return 0.0
    return round(min(100.0, unique_genres * 8.0), 1)


def _decide_labels(dimensions: dict[str, float]) -> list[str]:
    """按 5.3 规则判定人格标签（同维度排名靠前的优先，可多标签）。"""
    purchase = dimensions["purchase"]
    execution = dimensions["execution"]
    focus = dimensions["focus"]
    breadth = dimensions["breadth"]
    courage = dimensions["courage"]

    high = lambda v: v >= HIGH_SCORE
    low = lambda v: v < LOW_SCORE

    labels: list[str] = []
    if high(purchase) and low(execution):
        labels.append("收藏家型")
    if high(focus):
        labels.append("死忠粉型")
    if high(breadth):
        labels.append("杂食动物型")
    if high(courage):
        labels.append("硬核玩家型")
    if high(execution) and low(courage):
        labels.append("浅尝辄止型")
    if low(purchase) and high(execution):
        labels.append("精打细算型")
    if not labels:
        if all(LOW_SCORE <= v <= HIGH_SCORE for v in dimensions.values()):
            labels.append("六边形战士")
        else:
            labels.append("综合型玩家")
    return labels


def _build_summary(
    dimensions: dict[str, float],
    labels: list[str],
    total_games: int,
    top_genre: str | None,
) -> str:
    primary = labels[0] if labels else "综合型玩家"
    genre_text = top_genre or "各类类型"
    return (
        f"你的库里有 {total_games} 款游戏，已玩占比 {dimensions['execution']:.0f}%，"
        f"最常玩 {genre_text}，整体更接近「{primary}」。"
    )


def compute_persona(
    play_stats: dict,
    games: dict,
    stats_result: dict | None = None,
    statuses: dict | None = None,
    library_age_years: float | None = None,
    rules: dict[str, Any] | None = None,
    today: int | None = None,
) -> dict:
    """生成玩家人格画像。

    参数：
        play_stats: dict[appid, {playtime, last_played}]
        games:      dict[appid, {name, genres, categories, developers, ...}]
        stats_result: 可选，stats.compute_stats 的结果（避免重复计算）
        statuses:   可选 dict[appid, 状态名]（来自状态引擎 classifier）
        library_age_years: 库龄（年），缺省按 1 年估算
        rules:      可选规则（缺省自动读取 rules.json）
        today:      可选 unix 时间戳（便于测试）

    返回：{"dimensions", "dimension_weights", "labels", "primary_label",
           "summary"}
    """
    rules = rules or load_rules()
    stat = stats_result or compute_stats(play_stats, games, statuses=statuses, rules=rules, today=today)
    age = max(0.5, _to_float(library_age_years or 1.0))

    total_games = _to_int(stat.get("total_games", 0))
    playtime_minutes = [
        _to_int((play_stats.get(appid) or {}).get("playtime", 0))
        for appid in (play_stats or {})
    ]

    genres_seen: set[str] = set()
    hard_count = 0
    library = sorted(set(play_stats or {}) | set(games or {}))
    for appid in library:
        meta = games.get(appid) or {}
        genre_list = _as_list(meta.get("genres"))
        genres_seen.update(genre_list)
        if _is_hard_game(meta):
            hard_count += 1

    dimensions = {
        "purchase": _score_purchase(total_games, age),
        "execution": round(_to_float(stat.get("played_ratio", 0.0)), 1),
        "focus": _score_focus(playtime_minutes),
        "breadth": _score_breadth(len(genres_seen)),
        "courage": (
            round(hard_count / total_games * 100.0, 1) if total_games > 0 else 0.0
        ),
    }

    labels = _decide_labels(dimensions)
    top_genres = stat.get("top_genres") or []
    # stats 已把 top_genres 输出为中文展示名；这里再兜一层类型对照表，
    # 保证「最常玩 X」在 stats 未中文化（外部直接传 stats_result）时也是中文。
    top_genre = display.genre_display(top_genres[0]["name"]) if top_genres else None
    summary = _build_summary(dimensions, labels, total_games, top_genre)

    return {
        "dimensions": dimensions,
        "dimension_weights": dict(DIMENSION_WEIGHTS),
        "labels": labels,
        "primary_label": labels[0] if labels else "综合型玩家",
        "summary": summary,
    }


def run(args: Any) -> int:
    """CLI 入口（供 main.py 调用）：要求 args 含 play_stats/games 与
    可选 library_age_years，打印人格画像并返回退出码。"""
    play_stats = getattr(args, "play_stats", None) or {}
    games = getattr(args, "games", None) or {}
    result = compute_persona(
        play_stats,
        games,
        statuses=getattr(args, "statuses", None),
        library_age_years=getattr(args, "library_age_years", None),
    )

    print("== 游戏人格画像 ==")
    for key, value in result["dimensions"].items():
        weight = DIMENSION_WEIGHTS[key]
        print(f"  {_DIMENSION_NAMES[key]}({weight:.0%}): {value:.1f} / 100")
    print("  人格标签: " + " / ".join(result["labels"]))
    print(f"  一句话总结: {result['summary']}")
    return 0