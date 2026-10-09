"""统计看板计算（分析引擎层 · 模块 1：stats）。

按《03_分析引擎层设计文档.md》指标清单实现统计指标：
  - 总游戏数 / 总游玩时长（小时）/ 平均每款时长
  - 各状态游戏数与占比 / 吃灰率 / 已玩占比（played_ratio）
  - 类型分布 TOP / 开发商 TOP / 发行年份分布 / 时长分布
  - 最久未玩 TOP / 最近游玩 TOP

设计约定：
  - 输入为数据 dict（play_stats、games），可选 statuses、rules；
  - 输出为纯 dict（终端与 HTML 渲染通用），本模块不打印、不写文件；
  - 函数式、纯 Python、可单测；
  - 不 import 状态引擎（classifier.py）：状态分布优先使用调用方传入的
    statuses（dict[appid, 状态名]），缺省时用内置简化规则兜底；
    兜底不判"通关"（需 HLTB 数据，属 classifier 职责）。
"""

from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path
from typing import Any

from . import display
from core import non_game  # noqa: E402  非游戏条目唯一口径（延迟取用，见下）

RULES_PATH = Path(__file__).resolve().parent.parent / "rules.json"

# 状态输出的固定顺序，未知状态追加在末尾
STATUS_ORDER = ["未玩", "在玩", "搁置", "沉迷", "通关"]

DEFAULT_RULES: dict[str, Any] = {
    "playtime": {
        "addicted_min": 3000,
        "recent_days": 30,
        "hltb_ratio": 0.8,
    },
    "stats": {
        "top_n": {"genres": 10, "developers": 10, "oldest": 10, "recent": 10},
        "playtime_buckets": [
            {"label": "未玩", "min_minutes": 0, "max_minutes": 0},
            {"label": "1-10小时", "min_minutes": 1, "max_minutes": 600},
            {"label": "10-50小时", "min_minutes": 601, "max_minutes": 3000},
            {"label": "50-100小时", "min_minutes": 3001, "max_minutes": 6000},
            {"label": "100小时以上", "min_minutes": 6001, "max_minutes": None},
        ],
    },
    "recommender": {
        "top_k": 5,
        "stale_days": 365,
        "hltb_time_factor": 1.2,
        "exclude_appids": [7, 760, 241100],
        "session_default_minutes": 60,
        "session_minutes": {
            "casual": 30,
            "indie": 40,
            "puzzle": 30,
            "free to play": 45,
            "action": 60,
            "adventure": 75,
            "simulation": 90,
            "strategy": 90,
            "rpg": 120,
            "massively multiplayer": 120,
            "software training": 20,
        },
        "time_fit": {
            "full_bonus": 2.0,
            "partial_bonus": 0.8,
            "partial_ratio": 1.5,
            "over_ratio": 2.5,
            "over_penalty": -1.5,
            "filter_ratio": None,
        },
        "score": {
            "playing": 2.0,
            "shelved": 1.5,
            "addicted": 0.6,
            "genre_match": 3.0,
            "stale_penalty": -1.0,
        },
    },
}


def load_rules(path: str | Path | None = None) -> dict[str, Any]:
    """加载 rules.json 并与 DEFAULT_RULES 合并；文件缺失/损坏时回退默认值。

    下划线开头的键（_comment 等说明元数据）会被忽略。
    """
    rules = copy.deepcopy(DEFAULT_RULES)
    config_path = Path(path) if path else RULES_PATH
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return rules
    if not isinstance(data, dict):
        return rules
    for section, values in data.items():
        if section.startswith("_") or not isinstance(values, dict):
            continue
        if isinstance(rules.get(section), dict):
            rules[section].update(
                {k: v for k, v in values.items() if not k.startswith("_")}
            )
        else:
            rules[section] = values
    return rules


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_list(value: Any) -> list[str]:
    """genres/developers 字段兼容 list、JSON 字符串或逗号分隔字符串。"""
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
            except json.JSONDecodeError:
                pass
        return [item.strip() for item in text.split(",") if item.strip()]
    return []


_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def _extract_year(release_date: Any) -> int | None:
    """从发行日期（如 \"2016年10月21日\" / \"21 Oct, 2016\"）提取年份。"""
    if not release_date:
        return None
    match = _YEAR_RE.search(str(release_date))
    return int(match.group()) if match else None


def _percent(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round(part / whole * 100, 1)


def _top_items(counter: dict[str, int], limit: int) -> list[dict]:
    """按数量降序、名称升序排序，取前 limit 条（limit <= 0 表示不限）。"""
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    if limit > 0:
        items = items[:limit]
    return [{"name": name, "count": count} for name, count in items]


def _fallback_status(playtime_min: int, last_played: int, rules: dict[str, Any], today_ts: int) -> str:
    """简化状态兜底：未玩/沉迷/在玩/搁置；不判"通关"（需 HLTB 数据）。"""
    if playtime_min <= 0:
        return "未玩"
    if playtime_min >= _to_int(rules["playtime"]["addicted_min"]):
        return "沉迷"
    recent_days = _to_int(rules["playtime"]["recent_days"])
    if last_played <= 0:
        return "搁置"
    days_since = max(0, (today_ts - last_played) / 86400.0)
    return "在玩" if days_since <= recent_days else "搁置"


def _resolve_statuses(
    play_stats: dict,
    statuses: dict | None,
    rules: dict[str, Any],
    today_ts: int,
) -> dict[int, str]:
    """每款游戏的最终状态：优先调用方传入的 statuses，否则走兜底规则。"""
    result: dict[int, str] = {}
    for appid in play_stats:
        stat = play_stats[appid] or {}
        if statuses and appid in statuses:
            result[appid] = str(statuses[appid])
        else:
            result[appid] = _fallback_status(
                _to_int(stat.get("playtime", 0)),
                _to_int(stat.get("last_played", 0)),
                rules,
                today_ts,
            )
    return result


def library_scope(play_stats: dict, games: dict) -> list:
    """库范围（appid 列表）：以 play_stats（本地 VDF 真实拥有）为准。

    games 只用于补充 name/genres/price 等元数据，绝不扩大库：
    Steam Store 元数据缓存（games 表）里可能残留别的账号或历史抓取多出的
    appid，若按「并集」计算会让总游戏数虚高、平均时长被拉低。

    仅当 play_stats 为空时才退回 games 的键，兼容「只有元数据、没有时长
    记录」的调用场景。

    非游戏条目（Steam 客户端 / 截图 / 壁纸引擎 / 性能工具，见
    :mod:`core.non_game`）也在这里剔除：它们是 Steam 写进 localconfig.vdf 的
    附带条目，会让「总游戏数 / 总时长」虚高，并与面板「游戏明细」的数字打架。
    **类型未知一律保留**（绝不用它误杀真游戏）。
    """
    keys = set(play_stats or {}) or set(games or {})
    return sorted(a for a in keys if not non_game.is_non_game(a))


def compute_stats(
    play_stats: dict,
    games: dict,
    statuses: dict | None = None,
    rules: dict[str, Any] | None = None,
    today: int | None = None,
) -> dict:
    """计算统计看板指标。

    参数：
        play_stats: dict[appid, {playtime: 分钟, last_played: unix 秒}]
        games:      dict[appid, {name, genres, developers, price, release_date}]
        statuses:   可选 dict[appid, 状态名]（来自状态引擎 classifier）
        rules:      可选规则 dict（缺省自动读取 rules.json）
        today:      可选 unix 时间戳，用于兜底状态的"近期"判断（便于测试）

    返回：包含全部统计指标的 dict；库范围见 :func:`library_scope`
    （以 play_stats 为准，games 只补元数据，绝不扩大库）。
    """
    rules = rules or load_rules()
    today_ts = _to_int(today) or int(time.time())

    play_stats = play_stats or {}
    games = games or {}
    library = library_scope(play_stats, games)

    status_of = _resolve_statuses(play_stats, statuses, rules, today_ts)
    playtime_minutes = {
        appid: _to_int((play_stats.get(appid) or {}).get("playtime", 0))
        for appid in library
    }

    total_games = len(library)
    total_minutes = sum(playtime_minutes.values())
    played_count = sum(1 for minutes in playtime_minutes.values() if minutes > 0)
    unplayed_count = total_games - played_count

    # 各状态游戏数与占比（固定顺序，未知状态追加末尾）
    raw_counts: dict[str, int] = {}
    for appid in library:
        status = status_of.get(appid, "未玩")
        raw_counts[status] = raw_counts.get(status, 0) + 1
    ordered = [s for s in STATUS_ORDER if s in raw_counts]
    ordered += [s for s in raw_counts if s not in STATUS_ORDER]
    status_counts = {s: raw_counts[s] for s in ordered}
    status_percent = {s: _percent(c, total_games) for s, c in status_counts.items()}

    dust_games = raw_counts.get("未玩", 0) + raw_counts.get("搁置", 0)
    dust_rate = _percent(dust_games, total_games)
    played_ratio = _percent(played_count, total_games)

    # 类型 / 开发商 / 发行年份聚合
    top_n = (rules.get("stats") or {}).get("top_n") or {}
    genre_counter: dict[str, int] = {}
    developer_counter: dict[str, int] = {}
    year_counter: dict[int, int] = {}
    for appid in library:
        meta = games.get(appid) or {}
        # 展示用中文类型（判定不经过这里：状态/库范围/时长分布都用不到类型名）
        for genre in display.display_genres(meta):
            genre_counter[genre] = genre_counter.get(genre, 0) + 1
        for developer in _as_list(meta.get("developers")):
            developer_counter[developer] = developer_counter.get(developer, 0) + 1
        year = _extract_year(meta.get("release_date"))
        if year is not None:
            year_counter[year] = year_counter.get(year, 0) + 1

    top_genres = _top_items(genre_counter, _to_int(top_n.get("genres", 10)))
    top_developers = _top_items(developer_counter, _to_int(top_n.get("developers", 10)))
    release_year_dist = {year: year_counter[year] for year in sorted(year_counter)}

    # 游玩时长分布（分桶）
    buckets = (rules.get("stats") or {}).get("playtime_buckets")
    if not buckets:
        buckets = DEFAULT_RULES["stats"]["playtime_buckets"]
    playtime_distribution = []
    for bucket in buckets:
        low = _to_int(bucket.get("min_minutes", 0))
        raw_high = bucket.get("max_minutes")
        high = None if raw_high is None else _to_int(raw_high)
        count = sum(
            1
            for minutes in playtime_minutes.values()
            if minutes >= low and (high is None or minutes <= high)
        )
        playtime_distribution.append(
            {
                "label": str(bucket.get("label", "")),
                "count": count,
                "percent": _percent(count, total_games),
            }
        )

    # 最久未玩 / 最近游玩（仅统计有 last_played 记录且玩过的游戏）
    played_records = []
    for appid in library:
        minutes = playtime_minutes[appid]
        last_played = _to_int((play_stats.get(appid) or {}).get("last_played", 0))
        if minutes <= 0 or last_played <= 0:
            continue
        meta = games.get(appid) or {}
        played_records.append(
            {
                "appid": appid,
                "name": display.display_name(appid, meta),
                "status": status_of.get(appid, ""),
                "playtime_minutes": minutes,
                "playtime_hours": round(minutes / 60, 2),
                "last_played": last_played,
            }
        )
    most_forgotten = sorted(played_records, key=lambda g: g["last_played"])[
        : _to_int(top_n.get("oldest", 10))
    ]
    recent_played = sorted(played_records, key=lambda g: g["last_played"], reverse=True)[
        : _to_int(top_n.get("recent", 10))
    ]

    return {
        "total_games": total_games,
        "total_playtime_minutes": total_minutes,
        "total_playtime_hours": round(total_minutes / 60, 2),
        "avg_playtime_hours": round(total_minutes / 60 / total_games, 2) if total_games else 0.0,
        "status_counts": status_counts,
        "status_percent": status_percent,
        "dust_rate": dust_rate,
        "played_ratio": played_ratio,
        "played_count": played_count,
        "unplayed_count": unplayed_count,
        "top_genres": top_genres,
        "top_developers": top_developers,
        "release_year_dist": release_year_dist,
        "playtime_distribution": playtime_distribution,
        "most_forgotten": most_forgotten,
        "recent_played": recent_played,
    }