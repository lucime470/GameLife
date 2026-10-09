"""智能推荐引擎（分析引擎层 · 模块 2：recommender）。

按《03_分析引擎层设计文档.md》第四章实现：输入用户偏好（mood 心情 /
time 剩余时间 / genre 类型）与游戏库数据，按规则筛选候选池 → 加权打分
→ 生成推荐理由 → 输出 TOP5 推荐。

设计约定：
  - 纯函数、纯 Python、可单测；输出 dict（终端与 HTML 渲染通用）；
  - 不 import 状态引擎（classifier.py）：状态优先使用调用方传入的
    statuses（dict[appid, 状态名]），缺省时用内置简化规则兜底
    （与 stats.py 的兜底一致，不判"通关"）；
  - 时间匹配用「单局会话时长估计」：优先读条目内的 session_minutes，
    否则按类型查 rules.json 的 recommender.session_minutes 表；
    HLTB 总时长字段仅作长流程参考，缺失时不影响任何打分分支；
  - mood 真正参与打分：类型偏好 + 会话语速偏好 + 投入度偏好，
    而不是只影响推荐文案；
  - 同分固化由「确定性扰动」打破：种子 = mood + time + genre + 日期，
    同一天同一条件结果稳定，换条件即换排序；
  - 非游戏条目（Steam 客户端组件 / 工具 / 性能测试工具等）在候选池阶段剔除：
    判定口径**收敛到** ``core.non_game.is_non_game()``（全产品唯一口径，
    与面板 / 六张可视化页面 / 性价比榜 / 反向愿望单完全一致），本模块不再
    自带黑名单；``rules.json:recommender.exclude_appids`` 是**用户可配置**的
    额外排除项，语义不同，保留并与前者取并集；
  - "未玩超过 1 年"无购置时间数据，以 last_played 距今超过 stale_days
    判定并减分（从未启动过的不减分）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from typing import Any

from core import non_game

from . import display
from .stats import load_rules

MOODS = ("轻松", "挑战", "沉浸", "随便")
TIME_OPTIONS = ("30m", "1h", "2h", "不限")

# 剩余时间解析（分钟），"不限" 不做时间匹配
TIME_MINUTES: dict[str, int | None] = {
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "不限": None,
}

# 高难度标签（mood=挑战 时加分），匹配 genres/categories 子串
HARD_TAGS = ("hard", "hardcore", "souls", "difficult", "difficulty", "roguelike")

# 非游戏条目的判定口径：**全产品唯一一处**，见 :mod:`core.non_game`。
#
# 本模块曾自带一份 appinfo 类型集合与非游戏判定（A 线为修 onefile 冻结态
# 在此加了 ``_stable_data_dir()`` / ``_type_index_path()``），与面板
# （``webapp/api.py``）、六张可视化页面（``core/render_bridge.py``）、性价比榜
# （``engine/value_ranking.py``）、反向愿望单（``engine/wishlist_guard.py``）各留
# 一份，口径容易漂移。现已全部收敛到 :func:`core.non_game.is_non_game` —— 它自带
# 缓存、自带「类型未知一律放行」兜底，并已由 ``data.schema.data_dir()`` 正确定位
# 真实 ``data/appinfo_index.json``（冻结态同样可用），本模块不再重复实现。

# 内置默认推荐规则（与 rules.json 保持一致；rules.json 有值时以文件为准）
RECOMMENDER_DEFAULTS: dict[str, Any] = {
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
    "mood": {
        "轻松": {
            "genre_bonus": {
                "Casual": 1.6,
                "Puzzle": 1.2,
                "Indie": 0.8,
                "Simulation": 0.4,
                "Action": -0.4,
                "RPG": -0.8,
                "Massively Multiplayer": -1.2,
            },
            "tag_bonus": {"Relaxing": 0.6, "Cute": 0.5},
            "session_pref": {
                "mode": "short",
                "threshold_minutes": 60,
                "bonus": 1.2,
                "penalty": -1.0,
            },
            "playtime_penalty": {"min_minutes": 3000, "penalty": -1.0},
        },
        "挑战": {
            "genre_bonus": {
                "Action": 1.6,
                "Massively Multiplayer": 0.6,
                "RPG": 0.5,
                "Simulation": -0.6,
                "Indie": -0.3,
                "Casual": -1.0,
            },
            "hard_tag_bonus": 1.5,
            "session_pref": {
                "mode": "long",
                "threshold_minutes": 60,
                "bonus": 0.8,
                "penalty": -0.8,
            },
            "playtime_bonus": {"min_minutes": 3000, "bonus": 1.2},
        },
        "沉浸": {
            "genre_bonus": {
                "RPG": 1.6,
                "Adventure": 1.0,
                "Strategy": 0.5,
                "Massively Multiplayer": 0.4,
                "Free to Play": -0.4,
                "Casual": -0.8,
            },
            "tag_bonus": {"Story Rich": 1.4, "Atmospheric": 0.8, "Open World": 0.6},
            "session_pref": {
                "mode": "long",
                "threshold_minutes": 90,
                "bonus": 0.9,
                "penalty": -0.6,
            },
            "hltb_bonus": {"min_minutes": 1200, "bonus": 0.8},
        },
    },
    "score": {
        "playing": 2.0,
        "shelved": 1.5,
        "addicted": 0.6,
        "genre_match": 3.0,
        "stale_penalty": -1.0,
    },
}

# 推荐理由片段（按命中项拼装，保证"说什么就是什么"）
REASON_PARTS: dict[str, str] = {
    "playing": "你最近常玩它，趁热继续",
    "shelved": "搁置很久了，该捡起来了",
    "genre": "你喜欢的「{genre}」类型",
    "time": "单局时长刚好合适",
    "time_over": "单局偏长，最好留足整块时间",
    "stale": "有点久没碰了，也许正是捡起来的时候",
}
# 心情专属说法：按真正命中的具体项选词，避免无依据的夸张
MOOD_REASON_PARTS: dict[str, dict[str, str]] = {
    "轻松": {
        "mood_session": "单局短、低门槛，随时能停",
        "mood_genre": "休闲向类型，不用费脑子",
        "mood_tag": "轻松向的标签，适合放松",
    },
    "挑战": {
        "mood_hard": "硬核难度，正合挑战的心情",
        "mood_genre": "动作类型，正合挑战的心情",
        "mood_invest": "投入度够高，适合放开手脚挑战",
        "mood_session": "单局够长，能安排一场硬仗",
    },
    "沉浸": {
        "mood_long": "长流程剧情向，适合慢慢沉进去",
        "mood_genre": "剧情/角色扮演类型，适合沉浸",
        "mood_tag": "剧情与氛围到位，适合沉浸",
        "mood_session": "成片的时间才投入得进去",
    },
}
# 「核心类型」阈值：genre_bonus 权重 >= 该值的类型命中后，理由才可点名类型；
# 权重更低的只是轻微加权，不写进理由，避免"说一套做一套"
CORE_GENRE_THRESHOLD = 1.0

# 心情说法里点名的类型，必须与各 mood 的 genre_bonus 核心项一致：
#   轻松 -> Casual(1.6) / Puzzle(1.2)      挑战 -> Action(1.6)
#   沉浸 -> RPG(1.6) / Adventure(1.0)
# 命中项优先级（越靠前越先被选中）
MOOD_REASON_KEYS = ("mood_hard", "mood_long", "mood_invest", "mood_genre", "mood_tag", "mood_session")


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


def _deep_merge(base: dict, override: Any) -> dict:
    """递归合并：override 覆盖 base，嵌套 dict 逐层合并，'_' 开头的说明键忽略。"""
    result = copy.deepcopy(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if str(key).startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _rec_rules(rules: dict[str, Any] | None) -> dict[str, Any]:
    """推荐器完整的规则视图：内置默认 + rules.json 覆盖。"""
    provided = (rules or {}).get("recommender")
    return _deep_merge(RECOMMENDER_DEFAULTS, provided or {})


def _is_real_game(
    appid: int,
    meta: dict,
    rules: Any,
    type_index: dict[int, str] | None = None,
) -> bool:
    """真游戏判定：唯一口径 :func:`core.non_game.is_non_game` + 两个显式覆盖。

    四道判定按序求值，任一命中即排除；**只有第 3 道查本机 appinfo，也即「非游戏
    条目」的判定口径**（全产品唯一一处，见 :mod:`core.non_game`）：

      1. ``rules.json:recommender.exclude_appids`` —— **用户可配置**的额外排除项，
         用户想永久屏蔽某款游戏时用它，与「是不是游戏」无关；
      2. ``meta["type"]`` —— **调用方显式声明**的类型。声明了就按声明判定（不是
         ``"game"`` 即排除），沿用本模块的历史对外契约。注意这里**不列举任何
         非游戏类型名**，因此不与唯一口径的 ``NON_GAME_TYPES`` 重复维护词汇表；
         本产品自己的元数据来源都不产出该字段（``store_api.fetch_metadata`` /
         ``render_bridge.load_cached_metadata`` / ``core.appinfo`` 均无），只有
         外部数据源或合成数据才会带；
      3. :func:`core.non_game.is_non_game` —— 「**非游戏条目**」（Steam 客户端组件 /
         工具 / 性能测试工具…），唯一口径，类型未知一律放行（铁律）；
      4. ``type_index``（可选）—— 调用方显式覆盖的 ``{appid: 类型}``，供「不希望
         结果依赖本机 appinfo 快照」的场景（测试注入、对齐外部数据源）。它**不
         重新定义**哪些类型算非游戏，只用唯一口径的 ``NON_GAME_TYPES`` 比较；
         传 ``None`` / ``{}`` 即完全关闭。
    """
    excluded = {_to_int(a) for a in (rules.get("exclude_appids") or [])}
    if appid in excluded:
        return False
    declared_meta = str(meta.get("type") or "").strip().lower()
    if declared_meta:
        return declared_meta == "game"
    if non_game.is_non_game(appid):
        return False
    if type_index:
        declared = str(type_index.get(appid) or "").strip().lower()
        if declared and declared in non_game.NON_GAME_TYPES:
            return False
    return True


def _session_minutes(meta: dict, rules: Any) -> int:
    """单局会话时长估计（分钟）：显式字段 > 类型表（取最短）> 默认值。"""
    explicit = _to_int(meta.get("session_minutes"))
    if explicit > 0:
        return explicit
    table = rules.get("session_minutes") or {}
    default = _to_int(rules.get("session_default_minutes", 60)) or 60
    values = []
    for genre in _as_list(meta.get("genres")):
        minutes = _to_int(table.get(genre.lower()))
        if minutes > 0:
            values.append(minutes)
    return min(values) if values else default


def _hltb_minutes(meta: dict) -> int | None:
    """读取可选 HLTB 总时长字段（分钟），没有则返回 None。"""
    for key in ("hltb_minutes", "hltb_main", "hltb"):
        value = meta.get(key)
        if value is not None and _to_int(value) > 0:
            return _to_int(value)
    return None


def _infer_status(
    playtime_min: int, last_played: int, rules: dict[str, Any], today_ts: int
) -> str:
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
    play_stats: dict, statuses: dict | None, rules: dict[str, Any], today_ts: int
) -> dict[int, str]:
    result: dict[int, str] = {}
    for appid in play_stats:
        stat = play_stats[appid] or {}
        if statuses and appid in statuses:
            result[appid] = str(statuses[appid])
        else:
            result[appid] = _infer_status(
                _to_int(stat.get("playtime", 0)),
                _to_int(stat.get("last_played", 0)),
                rules,
                today_ts,
            )
    return result


def _genre_matches(game_genres: list[str], user_genre: str, rules: dict[str, Any]) -> bool:
    """匹配用户指定类型：支持直接匹配英文标签，或经
    categories.genre_rules 把中文类型（如"动作"）映射到 Steam 标签。
    用户选择"不限"时返回 False（不加分、不做理由）
    """
    if not user_genre or user_genre == "不限":
        return False
    targets = {user_genre.lower()}
    for genre, tags in ((rules.get("categories") or {}).get("genre_rules") or {}).items():
        if genre == user_genre:
            targets.update(str(tag).lower() for tag in tags)
    return bool(targets & {g.lower() for g in game_genres})


def _time_fit(session_min: int, time_minutes: int, rules: Any) -> tuple[float, str | None]:
    """会话时长是否吃得下：刚好加满分，明显超出则减分（或按配置直接剔除）。"""
    fit = rules.get("time_fit") or {}
    if time_minutes <= 0 or session_min <= 0:
        return 0.0, None
    partial_ratio = _to_float(fit.get("partial_ratio", 1.5)) or 1.5
    over_ratio = _to_float(fit.get("over_ratio", 2.5)) or 2.5
    ratio = session_min / float(time_minutes)
    if ratio <= 1.0:
        return _to_float(fit.get("full_bonus", 2.0)), "time"
    if ratio <= partial_ratio:
        return _to_float(fit.get("partial_bonus", 0.8)), "time"
    if ratio <= over_ratio:
        return 0.0, None
    return _to_float(fit.get("over_penalty", -1.5)), "time_over"


def _mood_score(
    meta: dict,
    mood: str,
    session_min: int,
    playtime_min: int,
    rules: Any,
) -> tuple[float, list[str]]:
    """心情对打分的真实影响：类型偏好 + 会话时长偏好 + 投入度偏好。"""
    profile = (rules.get("mood") or {}).get(mood)
    if mood == "随便" or not isinstance(profile, dict) or not profile:
        return 0.0, []

    values = _as_list(meta.get("genres")) + _as_list(meta.get("categories"))
    lowered = [v.lower() for v in values]
    lowered_set = set(lowered)
    score = 0.0
    keys: list[str] = []

    genre_bonus = 0.0
    core_genre_hit = False
    for target, weight in (profile.get("genre_bonus") or {}).items():
        if str(target).lower() not in lowered_set:
            continue
        value = _to_float(weight)
        genre_bonus += value
        if value >= CORE_GENRE_THRESHOLD:
            core_genre_hit = True
    if genre_bonus:
        score += genre_bonus
        if core_genre_hit:  # 轻微加权不写进理由，避免点名与实际命中不符
            keys.append("mood_genre")

    tag_bonus = sum(
        _to_float(weight)
        for target, weight in (profile.get("tag_bonus") or {}).items()
        if str(target).lower() in lowered_set
    )
    if tag_bonus:
        score += tag_bonus
        keys.append("mood_tag")

    hard_bonus = _to_float(profile.get("hard_tag_bonus", 0.0))
    if hard_bonus and any(tag in value for tag in HARD_TAGS for value in lowered):
        score += hard_bonus
        keys.append("mood_hard")

    session_pref = profile.get("session_pref") or {}
    mode = str(session_pref.get("mode") or "").lower()
    threshold = _to_int(session_pref.get("threshold_minutes", 0))
    bonus = _to_float(session_pref.get("bonus", 0.0))
    penalty = _to_float(session_pref.get("penalty", 0.0))
    if threshold > 0 and mode == "short":
        if session_min <= threshold:
            score += bonus
            keys.append("mood_session")
        elif session_min >= threshold * 3:
            score += penalty
    elif threshold > 0 and mode == "long":
        if session_min >= threshold:
            score += bonus
            keys.append("mood_session")
        elif session_min * 3 <= threshold:
            score += penalty

    invest = profile.get("playtime_bonus") or {}
    invest_min = _to_int(invest.get("min_minutes", 0))
    if invest and invest_min > 0 and playtime_min >= invest_min:
        score += _to_float(invest.get("bonus", 0.0))
        keys.append("mood_invest")

    heavy = profile.get("playtime_penalty") or {}
    heavy_min = _to_int(heavy.get("min_minutes", 0))
    if heavy and heavy_min > 0 and playtime_min >= heavy_min:
        score += _to_float(heavy.get("penalty", 0.0))
        keys.append("mood_heavy")

    long_form = profile.get("hltb_bonus") or {}
    long_min = _to_int(long_form.get("min_minutes", 0))
    hltb = _hltb_minutes(meta)
    if long_form and long_min > 0 and hltb is not None and hltb >= long_min:
        score += _to_float(long_form.get("bonus", 0.0))
        keys.append("mood_long")

    return score, keys


def _score_game(
    meta: dict,
    status: str,
    mood: str,
    time_minutes: int | None,
    genre_matched: bool,
    playtime_min: int,
    rules: dict[str, Any],
    rec: dict[str, Any],
    today_ts: int,
) -> tuple[float, list[str]]:
    """按 4.3 打分规则计算加分与命中项（reason_key 列表）。"""
    scorer = rec.get("score") or {}
    session_min = _session_minutes(meta, rec)
    score = 0.0
    keys: list[str] = []

    if status == "在玩":
        score += _to_float(scorer.get("playing", 2.0))
        keys.append("playing")
    elif status == "搁置":
        score += _to_float(scorer.get("shelved", 1.5))
        keys.append("shelved")
    elif status == "沉迷":
        score += _to_float(scorer.get("addicted", 0.6))
        keys.append("addicted")
    if genre_matched:
        score += _to_float(scorer.get("genre_match", 3.0))
        keys.append("genre")

    mood_score, mood_keys = _mood_score(meta, mood, session_min, playtime_min, rules=rec)
    score += mood_score
    keys.extend(mood_keys)

    if time_minutes is not None:
        fit_score, fit_key = _time_fit(session_min, time_minutes, rec)
        score += fit_score
        if fit_key:
            keys.append(fit_key)

    last_played = _to_int(meta.get("last_played", 0))
    stale_days = _to_int(rec.get("stale_days", 365))
    if last_played > 0 and stale_days > 0 and (today_ts - last_played) / 86400.0 > stale_days:
        score += _to_float(scorer.get("stale_penalty", -1.0))
        keys.append("stale")

    return score, keys


def _build_reason(
    keys: list[str],
    score: float,
    status: str,
    user_genre: str,
    mood: str,
) -> str:
    """按实际命中项拼装推荐理由，避免"说一套做一套"。"""
    parts: list[str] = []
    if "playing" in keys and status == "在玩":
        parts.append(REASON_PARTS["playing"])
    elif "shelved" in keys and status == "搁置":
        parts.append(REASON_PARTS["shelved"])

    if "genre" in keys:
        label = user_genre if user_genre and user_genre != "不限" else "这个"
        parts.append(REASON_PARTS["genre"].format(genre=label))

    # 心情说法：只说真正命中的那一项，不堆砌、不夸张
    mood_parts = MOOD_REASON_PARTS.get(mood) or {}
    for key in MOOD_REASON_KEYS:
        if key in keys and mood_parts.get(key):
            parts.append(mood_parts[key])
            break
    if "time" in keys:
        parts.append(REASON_PARTS["time"])
    if "time_over" in keys:
        parts.append(REASON_PARTS["time_over"])
    if "stale" in keys:
        parts.append(REASON_PARTS["stale"])

    if not parts:
        if "mood_heavy" in keys or score <= 0:
            return "条件不太匹配，仅作参考"
        return "符合你当前的心情，值得一试"
    return "；".join(parts)


def _jitter(seed: str, appid: int) -> float:
    """确定性扰动值 [0,1)：同种子同 appid 恒定，用于打破同分固化。"""
    digest = hashlib.md5(f"{seed}:{appid}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / float(0xFFFFFFFF)


def recommend(
    play_stats: dict,
    games: dict,
    mood: str = "随便",
    time_left: str = "不限",
    genre: str = "不限",
    statuses: dict | None = None,
    rules: dict[str, Any] | None = None,
    today: int | None = None,
    top_k: int | None = None,
    type_index: dict[int, str] | None = None,
) -> dict:
    """根据 mood / time / genre 从游戏库中推荐 TOP K 款游戏。

    参数：
        play_stats: dict[appid, {playtime: 分钟, last_played: unix 秒}]
        games:      dict[appid, {name, genres, categories, developers, ...，
                   可含 session_minutes（单局分钟）与 hltb 系列字段}]
        mood:       轻松 / 挑战 / 沉浸 / 随便
        time_left:  30m / 1h / 2h / 不限
        genre:      用户类型（中文如"动作"，或 Steam 英文标签），不限
        statuses:   可选 dict[appid, 状态名]（来自状态引擎 classifier）
        rules:      可选规则 dict（缺省自动读取 rules.json）
        today:      可选 unix 时间戳（便于测试）
        top_k:      返回条数，缺省用 rules["recommender"]["top_k"]
        type_index: 可选 dict[appid, 类型]，调用方显式覆盖（见 :func:`_is_real_game`）；
                    缺省 None，即完全交给唯一口径 core.non_game 决定

    非游戏条目一律由 :func:`_is_real_game` 剔除（唯一口径 core.non_game）。

    返回：{"mood", "time_left", "genre", "candidate_count",
           "recommendations": [{appid, name, status, score, reason}]}
    """
    rules = rules or load_rules()
    rec = _rec_rules(rules)
    today_ts = _to_int(today) or int(time.time())
    if mood not in MOODS:
        mood = "随便"
    if time_left not in TIME_OPTIONS:
        time_left = "不限"

    top_k = _to_int(top_k) or _to_int(rec.get("top_k", 5))

    play_stats = play_stats or {}
    games = games or {}
    library = sorted(set(play_stats) | set(games))
    status_of = _resolve_statuses(play_stats, statuses, rules, today_ts)
    time_minutes = TIME_MINUTES.get(time_left)
    filter_ratio = rec.get("time_fit", {}).get("filter_ratio")

    seed = f"{mood}|{time_left}|{genre}|{time.strftime('%Y-%m-%d', time.gmtime(today_ts))}"

    candidates = []
    for appid in library:
        meta = dict(games.get(appid) or {})
        meta.setdefault("last_played", _to_int((play_stats.get(appid) or {}).get("last_played", 0)))
        if not _is_real_game(appid, meta, rec, type_index):
            continue  # 非游戏条目（Steam 组件 / 工具 / DLC…）不进候选池
        game_genres = _as_list(meta.get("genres"))
        genre_matched = _genre_matches(game_genres, genre, rules)
        if genre and genre != "不限" and not genre_matched:
            continue  # 指定了类型才筛选候选池；"不限"时全部进入
        playtime_min = _to_int((play_stats.get(appid) or {}).get("playtime", 0))
        if time_minutes is not None and filter_ratio:
            session_min = _session_minutes(meta, rec)
            if session_min > time_minutes * _to_float(filter_ratio):
                continue
        status = status_of.get(appid, "未玩")
        score, keys = _score_game(
            meta, status, mood, time_minutes, genre_matched, playtime_min, rules, rec, today_ts
        )
        candidates.append(
            {
                "appid": appid,
                "name": display.display_name(appid, meta),
                "status": status,
                "score": round(score, 2),
                "reason": _build_reason(keys, score, status, genre, mood),
                "_tiebreak": _jitter(seed, appid),
            }
        )

    candidates.sort(key=lambda item: (-item["score"], -item["_tiebreak"], item["appid"]))
    recommendations = [
        {k: v for k, v in item.items() if not k.startswith("_")} for item in candidates[: max(0, top_k)]
    ]

    return {
        "mood": mood,
        "time_left": time_left,
        "genre": genre,
        "candidate_count": len(candidates),
        "recommendations": recommendations,
    }


def run(args: Any) -> int:
    """CLI 入口（供 main.py 调用）：要求 args 含 play_stats/games 与
    mood/time_left/genre，打印推荐结果并按结果返回退出码。"""
    play_stats = getattr(args, "play_stats", None) or {}
    games = getattr(args, "games", None) or {}
    result = recommend(
        play_stats,
        games,
        mood=getattr(args, "mood", "随便"),
        time_left=getattr(args, "time", "不限"),
        genre=getattr(args, "genre", "不限"),
        statuses=getattr(args, "statuses", None),
    )
    print(
        f"== 推荐（mood={result['mood']}, time={result['time_left']}, "
        f"genre={result['genre']}，候选 {result['candidate_count']} 款）=="
    )
    for i, item in enumerate(result["recommendations"], 1):
        print(f"{i}. {item['name']}  [{item['status']}]  得分 {item['score']}")
        print(f"   推荐理由：{item['reason']}")
    if not result["recommendations"]:
        print("候选池为空：试试放宽 genre 筛选。")
    return 0
