"""状态推断引擎（状态引擎层核心）。

根据游玩时长 / 最近游玩时间 / HLTB 元数据，按固定优先级推断游戏状态：
    未玩 -> 沉迷 -> 通关 -> 在玩 -> 搁置

规则阈值集中在 DEFAULT_RULES，可通过 rules 参数或 load_rules() 覆盖，
后续接入 game-life/rules.json 后即可热更新，无需改代码。

单位约定：playtime 与 hltb_time（HLTB_Main）均为分钟。
"""

import json
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path

# 内部状态键（稳定，供程序判断使用）
STATUS_NOT_PLAYED = "未玩"
STATUS_ADDICTED = "沉迷"
STATUS_COMPLETED = "通关"
STATUS_PLAYING = "在玩"
STATUS_BACKLOG = "搁置"

# 默认规则（分钟制）
DEFAULT_RULES = {
    "playtime": {
        "addicted_min": 3000,   # 沉迷阈值：>= 50 小时
        "recent_days": 30,      # 在玩判定：LastPlayed 距今 <= 30 天
        "hltb_ratio": 0.8,      # 通关判定：Playtime >= HLTB_Main * 0.8
    },
    "status_names": {
        STATUS_NOT_PLAYED: "未玩",
        STATUS_PLAYING: "在玩",
        STATUS_BACKLOG: "搁置",
        STATUS_ADDICTED: "沉迷",
        STATUS_COMPLETED: "已通关",   # 写回 Steam 分类时使用的展示名
    },
    "genre_rules": {
        "动作": ["Action"],
        "角色扮演": ["RPG", "Role-playing"],
        "策略": ["Strategy"],
        "模拟": ["Simulation"],
        "冒险": ["Adventure"],
        "独立": ["Indie"],
    },
    "tag_rules": {
        "肉鸽": ["Roguelike", "Roguelite"],
        "建造": ["Building", "Base-Building"],
        "剧情": ["Story Rich", "Atmospheric"],
        "多人": ["Multiplayer", "Online Co-Op"],
        "恐怖": ["Horror", "Survival Horror"],
    },
    "ignore_tags": ["Free to Play", "Casual", "Early Access", "Benchmark"],
}

RULES_PATH = Path(__file__).resolve().parent.parent / "rules.json"


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并规则：override 覆盖 base，未提及的键保留默认值。"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_rules(path: str = None) -> dict:
    """加载规则文件（默认 game-life/rules.json），文件不存在时返回默认规则。

    用户只需写要覆盖的键，例如 {"playtime": {"recent_days": 60}}。
    解析失败抛 ValueError，不静默降级。
    """
    rule_path = Path(path) if path else RULES_PATH
    rules = deepcopy(DEFAULT_RULES)
    if not rule_path.exists():
        return rules
    try:
        with open(rule_path, "r", encoding="utf-8") as f:
            user_rules = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"rules.json 解析失败: {e}") from e
    return _apply_user_rules(rules, user_rules)


def _apply_user_rules(base: dict, user_rules: dict) -> dict:
    """规范化用户规则（兼容 categories 容器布局）并按语义合并到 base。

    playtime 等键按需覆盖，缺省项保留 base 的默认值；
    status_names/genre_rules/tag_rules 属整表配置，用户一旦提供即整体替换默认表，
    避免默认分类（如 模拟/冒险）残留，导致用户的改名/删除配置失效。
    """
    user_rules = _normalize_rules(deepcopy(user_rules))
    merged = _deep_merge(base, user_rules)
    for key in ("status_names", "genre_rules", "tag_rules"):
        if key in user_rules:
            merged[key] = user_rules[key]
    return merged


def _normalize_rules(rules: dict) -> dict:
    """兼容两种 rules.json 布局，统一为内部扁平结构。

    主设计文档 5.2 与实际 rules.json 把 status_names/genre_rules/tag_rules
    放在 categories 容器下；02 文档展示为顶层键。这里把 categories 容器下的
    这几项提升到顶层（顶层已显式定义的键不被覆盖），保证用户配置两种写法都生效。
    """
    cats = rules.pop("categories", None)
    if isinstance(cats, dict):
        for key in ("status_names", "genre_rules", "tag_rules", "include_status"):
            if key in cats and key not in rules:
                rules[key] = cats[key]
    return rules


def _resolve_rules(rules: dict) -> dict:
    """把（可能不完整的）用户规则合并到默认规则上；None/空表示使用默认值。"""
    if not rules:
        return DEFAULT_RULES
    return _apply_user_rules(deepcopy(DEFAULT_RULES), rules)


def _to_date(value):
    """统一转成 date：支持 date/datetime、unix 时间戳；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromtimestamp(int(value))
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def classify_game(play_time: int, last_played=None, hltb_time=None,
                  today=None, rules: dict = None) -> str:
    """推断单款游戏状态（纯函数，便于单测）。

    play_time   : 累计游玩时长（分钟）
    last_played : 最近一次游玩（unix 时间戳或 date/datetime）
    hltb_time   : HLTB_Main（分钟，可选）；为 None/0 时优雅跳过通关判定
    today       : 基准日期（date/datetime，默认今天），便于测试
    rules       : 规则覆盖（部分即可），None 用默认规则

    判定优先级：未玩 -> 沉迷 -> 通关 -> 在玩 -> 搁置，命中即停止。
    返回内部状态键（见 STATUS_* 常量）。
    """
    r = _resolve_rules(rules)
    playtime_rules = r["playtime"]
    addicted_min = playtime_rules["addicted_min"]
    recent_days = playtime_rules["recent_days"]
    hltb_ratio = playtime_rules["hltb_ratio"]

    play_time = play_time or 0

    # 1. 未玩：从未启动
    if play_time <= 0:
        return STATUS_NOT_PLAYED

    # 2. 沉迷：时长达到阈值
    if play_time >= addicted_min:
        return STATUS_ADDICTED

    # 3. 通关：达到 HLTB 主线时长的 hltb_ratio 倍；无 HLTB 数据则跳过
    if hltb_time and hltb_time > 0 and play_time >= hltb_time * hltb_ratio:
        return STATUS_COMPLETED

    # 4/5. 在玩 / 搁置：按最近游玩时间判定
    last_date = _to_date(last_played)
    base_date = _to_date(today) or date.today()
    if last_date is not None:
        days_since = (base_date - last_date).days
        if days_since <= recent_days:
            return STATUS_PLAYING

    # 无最近游玩记录，或距今超过 recent_days
    return STATUS_BACKLOG


def status_display(status: str, rules: dict = None) -> str:
    """把内部状态键映射为展示名（如 通关 -> 已通关）。"""
    names = _resolve_rules(rules)["status_names"]
    return names.get(status, status)


def to_statuses(classified: dict, key: str = "status") -> dict:
    """把 classify_games 的输出转成分析引擎约定的 statuses 映射。

    分析引擎层（stats / recommender / persona）约定：
        statuses = dict[appid, 状态名]
    状态名取 classifier 的状态键（未玩/在玩/搁置/沉迷/通关），与
    stats.STATUS_ORDER 及 completion_rate 判定一致，故缺省 key="status"；
    需要面向 Steam 的展示名（如 通关 -> 已通关）时传 key="status_name"。
    """
    return {
        appid: info[key]
        for appid, info in (classified or {}).items()
        if isinstance(info, dict) and key in info
    }


def _match_categories(values, rules_map: dict, ignore=()) -> list:
    """按 rules_map（分类名 -> 关键词列表）做不区分大小写的精确匹配。"""
    if not values:
        return []
    ignored = {str(t).strip().lower() for t in ignore}
    tokens = {str(v).strip().lower() for v in values}
    tokens -= ignored
    matched = []
    for category, keywords in rules_map.items():
        if any(str(k).strip().lower() in tokens for k in keywords):
            matched.append(category)
    return matched


def categorize_genres(genres, rules: dict = None) -> list:
    """根据 genre_rules 生成类型分类（如 Action -> 动作）。"""
    return _match_categories(genres, _resolve_rules(rules)["genre_rules"])


def categorize_tags(tags, rules: dict = None) -> list:
    """根据 tag_rules 生成标签分类，ignore_tags 中的标签先被过滤。"""
    r = _resolve_rules(rules)
    return _match_categories(tags, r["tag_rules"], r.get("ignore_tags", []))


def classify_games(games: dict, hltb_data: dict = None, today=None,
                   rules: dict = None) -> dict:
    """批量推断状态（纯函数）。

    games     : {appid: {playtime, last_played, genres, tags}}
    hltb_data : {appid: {"main": 分钟}}，可选；缺失的 appid 自动跳过通关判定
    today     : 基准日期，便于测试
    rules     : 规则覆盖，默认「仅状态」模式（不生成类型/标签分类）

    返回 {appid: {status, status_name, genre_categories, tag_categories}}。
    """
    hltb = hltb_data or {}
    result = {}
    for appid, info in games.items():
        info = info or {}
        play_time = info.get("playtime", 0)
        last_played = info.get("last_played")
        hltb_entry = hltb.get(appid) or {}
        hltb_time = hltb_entry.get("main") if isinstance(hltb_entry, dict) else hltb_entry

        status = classify_game(play_time, last_played, hltb_time, today, rules)

        genres = info.get("genres", [])
        tags = info.get("tags") or info.get("categories") or []
        result[appid] = {
            "status": status,
            "status_name": status_display(status, rules),
            "genre_categories": categorize_genres(genres, rules),
            "tag_categories": categorize_tags(tags, rules),
        }
    return result

# === 写回委托 ===

def classify_and_write(steam_path: str, account_id: str,
                         play_stats: dict, games: dict,
                         mode: str = "status", hltb_data: dict = None) -> dict:
    """一键分类并写回 sharedconfig.vdf（薄包装，委托 steam_writer）。

    状态判定只基于 play_stats（时长/最近游玩），games 仅作可选元数据（名称来源）；
    返回 {written: int, appids: [...]}。
    """
    if mode != "status":
        raise ValueError(f"不支持的 mode: {mode}")
    from core import steam_writer as _sw
    classified = classify_games(play_stats, hltb_data=hltb_data)
    categories = _sw.status_to_categories(classified)
    _sw.write_categories(steam_path, account_id, categories)
    return {"written": len(categories), "appids": list(categories.keys())}


def classify_and_remove(steam_path: str, account_id: str,
                          appids: list) -> str:
    """一键取消分类（薄包装，委托 steam_writer）。"""
    from core import steam_writer as _sw
    return _sw.remove_categories(steam_path, account_id, appids)

