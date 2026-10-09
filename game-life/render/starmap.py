"""游戏星图生成器（可视化呈现层 · 模块 2）。

职责：把整个游戏库渲染成一张可交互的宇宙星图 -> 单文件 HTML
      -> 输出到 game-life/output/ -> webbrowser 自动打开。

图表库取舍：**纯 Canvas + 内嵌原生 JS，零外部依赖**。
* ECharts 完整包约 1MB，即使内联也会让单文件 HTML 体积失控；
* 星图所需能力（散点绘制 / 悬停详情 / 拖拽平移 / 滚轮缩放 / 图例筛选）
  用 Canvas 2D 原生实现完全够用；
* 因此本渲染器不依赖 CDN，断网双击即可打开，符合「单文件 HTML」核心原则。

坐标由 Python 侧确定性生成（同一份数据每次渲染位置一致），
视觉映射（大小 / 颜色 / 亮度 / 闪烁）在 JS 侧完成。

映射规则（04 文档 4.2）::

    游玩时长 -> 星星大小（sqrt 缩放）
    游戏类型 -> 星星颜色 + 星团位置（同类型聚成一簇）
    未玩     -> 暗淡尘埃（默认可见，可勾选隐藏）
    搁置     -> 呼吸闪烁
    在玩     -> 大星 + 高亮
    游戏名   -> 悬停显示详情卡片

数据接口（data 参数，缺省字段均有安全默认值）::

    {
        "user_name": str,                # 可选，默认「神秘玩家」
        "games": {                       # appid -> 游戏信息（也支持 list[dict]）
            "1245620": {
                "name": str,
                "status": str,           # 在玩 / 搁置 / 未玩
                "playtime": float,       # 游玩时长（小时）
                "genre": str,            # 类型（决定颜色与星团位置）
                "last_played": str,      # 可选
            },
        },
        "generated_at": str,             # 可选，默认今天 YYYY-MM-DD
    }

用法::

    from game_life.render import starmap
    starmap.generate_starmap(data)          # 渲染 + 写文件 + 打开浏览器
    starmap.generate_starmap(data, open_browser=False, output_path=...)
    python game-life/render/starmap.py --demo --no-open      # 示例数据跑通全流程
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import webbrowser
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
DEFAULT_OUTPUT_DIR = PACKAGE_DIR.parent / "output"


# ---------------------------------------------------------------- 游戏封面
#
# 页面图片一律走「产物目录内的 covers/ 相对路径」：由 core.render_bridge
# 把本地已有封面（data/covers 或本机 Steam librarycache，**不联网**）复制过去。
# 取不到封面的游戏保持 cover 为空，模板据此显示占位色块（首字母），绝不裂图。

def _attach_covers(items, out_dir) -> None:
    """就地给条目补上 ``cover``（相对产物目录的图片地址）；无封面则留空。"""
    appids: list = []
    for item in items or ():
        appid = (item or {}).get("appid")
        if appid in (None, "", 0):
            continue
        appids.append(appid)
    if not appids:
        return
    try:
        from core import render_bridge

        meta = render_bridge.prepare_covers_meta(appids, out_dir)
    except Exception as exc:  # 封面不可用不应影响页面生成
        logger.debug("封面不可用，页面显示占位块：%s", exc)
        return
    for item in items or ():
        row = meta.get(str((item or {}).get("appid"))) or {}
        url = row.get("url")
        if url:
            item["cover"] = url
            item["cover_kind"] = str(row.get("kind") or "unknown")

TWO_PI = math.pi * 2
GOLDEN_ANGLE = 2.399963229728653

STATUS_LABELS = {
    "playing": "在玩",
    "shelved": "搁置",
    "unplayed": "未玩",
}

PLAYING_ALIASES = {"在玩", "游玩中", "playing", "in_progress", "inprogress", "active"}
SHELVED_ALIASES = {"搁置", "搁置中", "shelved", "paused", "on_hold", "onhold"}
UNPLAYED_ALIASES = {"未玩", "未启动", "未开玩", "unplayed", "not_started", "notstarted", "new", "untouched"}

# 类型 -> 颜色（04 文档 4.2 的类型配色约定：动作=蓝、RPG=紫、策略=绿…）
GENRE_COLORS = [
    (("动作", "格斗", "射击", "action", "fighting", "shooter", "fps"), "#58a6ff"),
    (("rpg", "角色扮演", "魂系"), "#bc8cff"),
    (("策略", "战略", "strategy", "4x", "tactic", "卡牌", "自走棋"), "#3fb950"),
    (("模拟", "经营", "simulation", "sim", "manage", "建造", "沙盒"), "#d29922"),
    (("冒险", "剧情", "叙事", "adventure", "story", "narrative", "novel", "解谜", "puzzle"), "#39c5cf"),
    (("独立", "indie", "rogue", "肉鸽", "音乐", "rhythm", "休闲", "casual"), "#db61a2"),
]
GENRE_FALLBACK_COLOR = "#8b949e"


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_num(value: Any, decimals: int = 1) -> str:
    v = _to_float(value)
    s = f"{v:,.{decimals}f}"
    if s.endswith("." + "0" * decimals):
        s = s[: -(decimals + 1)]
    return s


def _hash_unit(text: Any) -> float:
    """FNV-1a 稳定哈希 -> [0, 1)，保证同一份数据每次渲染位置一致。"""
    h = 2166136261
    for ch in str(text):
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h / 0x100000000


def genre_color(genre: Any) -> str:
    g = str(genre or "").lower()
    for keywords, color in GENRE_COLORS:
        if any(k in g for k in keywords):
            return color
    return GENRE_FALLBACK_COLOR


def _classify_status(raw: Any, playtime: float) -> str:
    """playing / shelved / unplayed。"""
    s = str(raw or "").strip().lower()
    original = str(raw or "").strip()
    if s in PLAYING_ALIASES or original in PLAYING_ALIASES:
        return "playing"
    if s in SHELVED_ALIASES or original in SHELVED_ALIASES:
        return "shelved"
    if s in UNPLAYED_ALIASES or original in UNPLAYED_ALIASES:
        return "unplayed"
    return "playing" if playtime > 0 else "unplayed"


def _build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(("html", "xml")),
    )
    env.filters["h"] = lambda v: _fmt_num(v)
    env.filters["comma"] = lambda v: f"{_to_float(v):,.0f}"
    return env


def _assign_positions(stars: list) -> None:
    """按类型分簇布局：同类型聚成一簇，簇内用向日葵螺旋散开。

    结果写入每个 star 的 x / y（归一化到约 [-1, 1]，原点在画布中心）。
    """
    by_genre: dict = {}
    for star in stars:
        by_genre.setdefault(star["genre"], []).append(star)

    genres = sorted(by_genre)
    total = max(1, len(genres))
    for gi, genre in enumerate(genres):
        group = by_genre[genre]
        base_angle = (gi / total) * TWO_PI + (_hash_unit(genre) - 0.5) * 0.35
        cluster_r = 0.30 + 0.46 * _hash_unit(genre + "#r")
        cx = cluster_r * math.cos(base_angle)
        cy = cluster_r * math.sin(base_angle)

        count = len(group)
        spread = 0.10 + 0.16 * min(1.0, math.sqrt(count / 12.0))
        for i, star in enumerate(group):
            frac = (i + 1) / (count + 1)
            local_r = spread * math.sqrt(frac) * (0.72 + 0.5 * _hash_unit(star["appid"] + "#j"))
            local_angle = i * GOLDEN_ANGLE + _hash_unit(star["appid"] + "#a") * 0.6
            star["x"] = round(cx + local_r * math.cos(local_angle), 5)
            star["y"] = round(cy + local_r * math.sin(local_angle), 5)


def normalize_starmap_data(data: Mapping[str, Any]) -> dict:
    """把外部传入的 dict 清洗为模板可直接使用的结构。"""
    user_name = str(data.get("user_name") or data.get("name") or "神秘玩家")
    raw_games = data.get("games") or {}

    items = []
    if isinstance(raw_games, Mapping):
        for appid, info in raw_games.items():
            entry = dict(info or {})
            entry.setdefault("appid", str(appid))
            items.append(entry)
    else:
        for i, info in enumerate(raw_games):
            entry = dict(info or {})
            entry.setdefault("appid", str(entry.get("appid", i)))
            items.append(entry)

    stars = []
    for entry in items:
        appid = str(entry.get("appid"))
        name = str(entry.get("name") or f"游戏 {appid}")
        playtime = max(0.0, _to_float(entry.get("playtime")))
        genre = str(entry.get("genre") or "其他")
        status = _classify_status(entry.get("status"), playtime)
        stars.append(
            {
                "appid": appid,
                "name": name,
                "genre": genre,
                "status": status,
                "status_label": STATUS_LABELS[status],
                "playtime": round(playtime, 2),
                "hours": _fmt_num(playtime),
                "color": genre_color(genre),
            }
        )

    _assign_positions(stars)

    genre_map = {}
    for star in stars:
        g = genre_map.setdefault(star["genre"], {"name": star["genre"], "color": star["color"], "count": 0, "hours": 0.0})
        g["count"] += 1
        g["hours"] += star["playtime"]
    genres = sorted(genre_map.values(), key=lambda g: (-g["count"], g["name"]))
    for g in genres:
        g["hours"] = round(g["hours"], 1)

    status_counts = {key: 0 for key in STATUS_LABELS}
    for star in stars:
        status_counts[star["status"]] += 1

    total_hours = sum(s["playtime"] for s in stars)
    stats = {
        "total_games": len(stars),
        "total_hours": round(total_hours, 1),
        "genre_count": len(genres),
        "playing": status_counts["playing"],
        "shelved": status_counts["shelved"],
        "unplayed": status_counts["unplayed"],
        "max_playtime": max((s["playtime"] for s in stars), default=0.0),
    }

    return {
        "user_name": user_name,
        "stars": stars,
        "genres": genres,
        "stats": stats,
        "generated_at": str(data.get("generated_at") or date.today().isoformat()),
    }


def generate_starmap(
    data: Mapping[str, Any],
    output_path: Optional[Union[str, Path]] = None,
    open_browser: bool = True,
) -> Path:
    """生成游戏星图单文件 HTML，返回输出路径。

    :param data: 星图数据 dict（结构见模块 docstring）
    :param output_path: 输出路径；缺省为 ``game-life/output/starmap.html``
    :param open_browser: 生成后是否自动用浏览器打开（默认 True）
    """
    payload = normalize_starmap_data(data)
    out = Path(output_path) if output_path else DEFAULT_OUTPUT_DIR / "starmap.html"
    out.parent.mkdir(parents=True, exist_ok=True)

    _attach_covers(payload["stars"], out.parent)

    env = _build_env()
    html = env.get_template("starmap.html").render(payload=payload)
    out.write_text(html, encoding="utf-8")
    logger.info("游戏星图已生成：%s（%d 颗星 / %d 种类型）", out, len(payload["stars"]), len(payload["genres"]))

    if open_browser:
        webbrowser.open(out.resolve().as_uri())
    return out


def demo_data() -> dict:
    """示例数据：覆盖多种类型与 3 种状态，用于跑通全流程。"""
    playing = [
        ("1245620", "艾尔登法环", 86.4, "动作"), ("293100", "赛博朋克 2077", 42.8, "RPG"),
        ("292030", "巫师 3：狂猎", 152.4, "RPG"), ("570", "Dota 2", 1204.6, "射击"),
        ("413150", "星露谷物语", 96.1, "模拟"), ("105600", "泰拉瑞亚", 58.3, "独立"),
    ]
    shelved = [
        ("814380", "只狼：影逝二度", 0.3, "动作"), ("281990", "群星", 0.4, "策略"),
        ("8930", "文明 6", 156.3, "策略"), ("457140", "缺氧", 0.5, "模拟"),
        ("294100", "环世界", 22.7, "模拟"), ("1086940", "博德之门 3", 0.2, "策略"),
        ("1174180", "荒野大镖客 2", 12.3, "动作"), ("1190460", "死亡搁浅", 3.2, "动作"),
        ("501300", "艾迪芬奇的记忆", 0.9, "冒险"), ("379430", "全面战争：三国", 8.7, "策略"),
        ("255710", "城市：天际线", 31.5, "模拟"), ("47890", "模拟人生 4", 6.4, "模拟"),
        ("239140", "消逝的光芒", 2.1, "动作"), ("322330", "饥荒", 1.4, "模拟"),
    ]
    unplayed = [
        ("578080", "绝地求生", "射击"), ("236390", "战争雷霆", "射击"),
        ("892970", "英灵神殿", "冒险"), ("427520", "异星工厂", "模拟"),
        ("289070", "文明 5", "策略"), ("646570", "杀戮尖塔", "独立"),
        ("753640", "Outer Wilds", "冒险"), ("990080", "霍格沃茨之遗", "冒险"),
        ("1237970", "泰坦陨落 2", "射击"), ("1817070", "漫威蜘蛛侠", "动作"),
        ("1462040", "最终幻想 7 重制版", "RPG"), ("1593500", "战神", "动作"),
        ("1172620", "战神：诸神黄昏", "动作"), ("391220", "古墓丽影：崛起", "冒险"),
        ("219740", "饥荒", "模拟"), ("250900", "以撒的结合", "独立"),
    ]

    games = {}
    for appid, name, hours, genre in playing:
        games[appid] = {"name": name, "status": "在玩", "playtime": hours, "genre": genre, "last_played": "2026-09-18"}
    for appid, name, hours, genre in shelved:
        games[appid] = {"name": name, "status": "搁置", "playtime": hours, "genre": genre, "last_played": "2025-05-06"}
    for appid, name, genre in unplayed:
        games[appid] = {"name": name, "status": "未玩", "playtime": 0, "genre": genre, "last_played": ""}
    return {"user_name": "Neo", "games": games}


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="starmap",
        description="生成「游戏星图」单文件 HTML（示例：--demo 或 --data data.json）",
    )
    parser.add_argument("--data", type=Path, help="数据 JSON 文件路径（结构见 starmap.generate_starmap 文档）")
    parser.add_argument("--demo", action="store_true", help="使用内置示例数据（无真实数据时跑通全流程）")
    parser.add_argument("--output", type=Path, help="输出 HTML 路径（缺省为 game-life/output/starmap.html）")
    parser.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")
    args = parser.parse_args(argv)

    if args.data:
        with open(args.data, encoding="utf-8") as f:
            data = json.load(f)
        logger.info("已从 %s 读取数据", args.data)
    elif args.demo:
        data = demo_data()
        logger.info("使用内置示例数据")
    else:
        data = demo_data()
        logger.info("未指定 --data/--demo，默认使用示例数据")

    out = generate_starmap(data, output_path=args.output, open_browser=not args.no_open)
    print(f"[starmap] 已生成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
