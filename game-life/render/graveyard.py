"""游戏墓园生成器（可视化呈现层 · 模块 3）。

职责：把「搁置」状态的游戏渲染成墓碑墙 -> 单文件 HTML（CSS/JS 内嵌）
      -> 输出到 game-life/output/ -> webbrowser 自动打开。

当前不依赖任何分析引擎模块（engine/ 尚未就绪），数据以纯 dict 接口传入，
后续由 CLI / 主 Agent 接入真实数据。

数据接口（data 参数，缺省字段均有安全默认值）::

    {
        "user_name": str,                # 可选，默认「神秘玩家」
        "games": {                       # appid -> 游戏信息（也支持 list[dict]）
            "1245620": {
                "name": str,             # 游戏名
                "status": str,           # "搁置" / "未玩"（也接受 shelved / unplayed）
                "playtime": float,       # 游玩时长（小时）
                "last_played": str,      # 最后游玩日期 "YYYY-MM-DD"（或 "YYYY-MM"）
                "genre": str,            # 主类型，用于死因判定
            },
        },
        "generated_at": str,             # 可选，默认今天 YYYY-MM-DD
    }

行为约定：
* 状态为「搁置」-> 墓碑墙，按卒年（last_played 年份）倒序分组；
* 状态为「未玩」-> 「未入土」区；
* 其他状态（在玩 / 通关）不进入墓园；
* status 缺失时按 playtime 推断：<=0 视为未玩，否则视为搁置。

死因判定（04 文档 5.3）::

    动作/格斗 且 < 30 分钟  -> 难度劝退
    剧情/叙事 且 < 1 小时   -> 剧情没吸引住你
    策略      且 < 30 分钟  -> 门槛太高
    模拟/经营 且 < 1 小时   -> 没耐心耕耘
    其他                    -> 缘分未到

用法::

    from game_life.render import graveyard
    graveyard.generate_graveyard(data)          # 渲染 + 写文件 + 打开浏览器
    graveyard.generate_graveyard(data, open_browser=False, output_path=...)
    python game-life/render/graveyard.py --demo --no-open    # 示例数据跑通全流程
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import webbrowser
from datetime import date, datetime
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


SHELVED_ALIASES = {"搁置", "搁置中", "shelved", "shelf", "paused", "on_hold", "onhold"}
UNPLAYED_ALIASES = {"未玩", "未启动", "未开玩", "unplayed", "not_started", "notstarted", "new", "untouched"}

_YEAR_RE = re.compile(r"^(\d{4})")


# ---------------------------------------------------------------- 工具函数

def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_num(value: Any, decimals: int = 1) -> str:
    """1247.5 -> "1,247.5"；1247.0 -> "1,247"。"""
    v = _to_float(value)
    s = f"{v:,.{decimals}f}"
    if s.endswith("." + "0" * decimals):
        s = s[: -(decimals + 1)]
    return s


def format_age(hours: Any) -> str:
    """享年：< 1 小时显示分钟，其余显示小时。"""
    h = _to_float(hours)
    if h <= 0:
        return "未启程"
    if h < 1:
        return f"{max(1, int(round(h * 60)))} 分钟"
    return f"{_fmt_num(h)} 小时"


def guess_cause(genre: Any, hours: Any) -> str:
    """死因判定（04 文档 5.3）。"""
    g = str(genre or "").lower()
    minutes = max(0.0, _to_float(hours) * 60)
    if any(k in g for k in ("动作", "格斗", "action", "fighting", "act")) and minutes < 30:
        return "难度劝退"
    if any(k in g for k in ("剧情", "叙事", "story", "narrative", "novel")) and minutes < 60:
        return "剧情没吸引住你"
    if any(k in g for k in ("策略", "strategy", "4x", "tactic")) and minutes < 30:
        return "门槛太高"
    if any(k in g for k in ("模拟", "经营", "simulation", "sim", "manage")) and minutes < 60:
        return "没耐心耕耘"
    return "缘分未到"


def _normalize_date(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value).strip()


def _extract_year(value: str) -> str:
    m = _YEAR_RE.match(value or "")
    return m.group(1) if m else "未知"


def _classify_status(raw: str, playtime: float, last_played: str) -> str:
    """返回 "shelved" / "unplayed" / "other"。"""
    s = (raw or "").strip().lower()
    if s in SHELVED_ALIASES or raw.strip() in SHELVED_ALIASES:
        return "shelved"
    if s in UNPLAYED_ALIASES or raw.strip() in UNPLAYED_ALIASES:
        return "unplayed"
    if not s:
        # 状态缺失：按游玩时长与是否有游玩记录推断
        if playtime <= 0 and not last_played:
            return "unplayed"
        if playtime <= 0:
            return "unplayed"
        return "shelved"
    return "other"


def _build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(("html", "xml")),
    )
    env.filters["h"] = lambda v: _fmt_num(v)
    env.filters["comma"] = lambda v: f"{_to_float(v):,.0f}"
    return env


# ---------------------------------------------------------------- 数据归一化

def normalize_graveyard_data(data: Mapping[str, Any]) -> dict:
    """把外部传入的 dict 清洗为模板可直接使用的结构。"""
    user_name = str(data.get("user_name") or data.get("name") or "神秘玩家")
    raw_games = data.get("games") or {}

    items: list[dict] = []
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

    graves: list[dict] = []
    unburied: list[dict] = []
    for entry in items:
        appid = str(entry.get("appid"))
        name = str(entry.get("name") or f"游戏 {appid}")
        playtime = _to_float(entry.get("playtime"))
        last_played = _normalize_date(entry.get("last_played"))
        genre = str(entry.get("genre") or "")
        status = _classify_status(str(entry.get("status") or ""), playtime, last_played)

        if status == "unplayed":
            unburied.append({"appid": appid, "name": name})
        elif status == "shelved":
            graves.append(
                {
                    "appid": appid,
                    "name": name,
                    "genre": genre,
                    "hours": playtime,
                    "age": format_age(playtime),
                    "last_played": last_played or "未知日期",
                    "year": _extract_year(last_played),
                    "cause": guess_cause(genre, playtime),
                }
            )

    # 按卒年倒序分组，年份未知的排最后
    groups: dict[str, list[dict]] = {}
    for g in graves:
        groups.setdefault(g["year"], []).append(g)

    def year_sort_key(year: str):
        return (1, 0) if year == "未知" else (0, -int(year))

    year_groups = []
    for year in sorted(groups, key=year_sort_key):
        games = sorted(groups[year], key=lambda g: g["last_played"], reverse=True)
        year_groups.append({"year": year, "games": games})

    unburied.sort(key=lambda g: g["name"])

    stats: dict[str, Any] = {"total": len(graves), "unburied": len(unburied)}
    if graves:
        total_hours = sum(g["hours"] for g in graves)
        shortest = min(graves, key=lambda g: g["hours"])
        longest = max(graves, key=lambda g: g["hours"])
        stats.update(
            {
                "total_hours": total_hours,
                "avg_age": format_age(total_hours / len(graves)),
                "shortest_name": shortest["name"],
                "shortest_age": shortest["age"],
                "longest_name": longest["name"],
                "longest_age": longest["age"],
            }
        )

    return {
        "user_name": user_name,
        "games": graves,
        "unburied": unburied,
        "year_groups": year_groups,
        "stats": stats,
        "generated_at": str(data.get("generated_at") or date.today().isoformat()),
    }


# ---------------------------------------------------------------- 主生成流程

def generate_graveyard(
    data: Mapping[str, Any],
    output_path: Optional[Union[str, Path]] = None,
    open_browser: bool = True,
) -> Path:
    """生成游戏墓园单文件 HTML，返回输出路径。

    :param data: 墓园数据 dict（结构见模块 docstring）
    :param output_path: 输出路径；缺省为 ``game-life/output/graveyard.html``
    :param open_browser: 生成后是否自动用浏览器打开（默认 True）
    """
    payload = normalize_graveyard_data(data)
    out = Path(output_path) if output_path else DEFAULT_OUTPUT_DIR / "graveyard.html"
    out.parent.mkdir(parents=True, exist_ok=True)

    grave_items = [g for group in payload["year_groups"] for g in group["games"]]
    _attach_covers(grave_items + list(payload["unburied"]), out.parent)

    env = _build_env()
    html = env.get_template("graveyard.html").render(payload=payload)
    out.write_text(html, encoding="utf-8")
    logger.info("游戏墓园已生成：%s（安息 %d 款 / 未入土 %d 款）", out, len(payload["games"]), len(payload["unburied"]))

    if open_browser:
        webbrowser.open(out.resolve().as_uri())
    return out


def demo_data() -> dict:
    """示例数据：覆盖 5 种死因 + 未入土区，用于跑通全流程。"""
    return {
        "user_name": "Neo",
        "games": {
            "1245620": {"name": "只狼：影逝二度", "status": "搁置", "playtime": 0.3, "last_played": "2024-03-11", "genre": "动作"},
            "292030": {"name": "巫师 3：狂猎", "status": "搁置", "playtime": 152.4, "last_played": "2025-11-02", "genre": "RPG"},
            "1091500": {"name": "赛博朋克 2077", "status": "搁置", "playtime": 8.5, "last_played": "2026-02-18", "genre": "动作"},
            "1174180": {"name": "荒野大镖客 2", "status": "搁置", "playtime": 12.3, "last_played": "2025-06-27", "genre": "动作"},
            "1190460": {"name": "死亡搁浅", "status": "搁置", "playtime": 3.2, "last_played": "2023-10-09", "genre": "动作"},
            "632470": {"name": "极乐迪斯科", "status": "搁置", "playtime": 0.6, "last_played": "2023-08-05", "genre": "剧情"},
            "501300": {"name": "艾迪芬奇的记忆", "status": "搁置", "playtime": 0.9, "last_played": "2025-04-20", "genre": "剧情"},
            "281990": {"name": "群星", "status": "搁置", "playtime": 0.4, "last_played": "2024-12-01", "genre": "策略"},
            "8930": {"name": "文明 6", "status": "搁置", "playtime": 156.3, "last_played": "2026-01-08", "genre": "策略"},
            "457140": {"name": "缺氧", "status": "搁置", "playtime": 0.5, "last_played": "2025-09-15", "genre": "模拟"},
            "294100": {"name": "环世界", "status": "搁置", "playtime": 22.7, "last_played": "2022-11-23", "genre": "模拟"},
            "1086940": {"name": "博德之门 3", "status": "搁置", "playtime": 0.2, "last_played": "2023-04-02", "genre": "策略"},
            "413150": {"name": "星露谷物语", "status": "未玩", "playtime": 0, "last_played": "", "genre": "模拟"},
            "620": {"name": "传送门 2", "status": "未玩", "playtime": 0, "last_played": "", "genre": "解谜"},
            "570": {"name": "Dota 2", "status": "未玩", "playtime": 0, "last_played": "", "genre": "MOBA"},
        },
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="graveyard",
        description="生成「游戏墓园」单文件 HTML（示例：--demo 或 --data data.json）",
    )
    parser.add_argument("--data", type=Path, help="数据 JSON 文件路径（结构见 graveyard.generate_graveyard 文档）")
    parser.add_argument("--demo", action="store_true", help="使用内置示例数据（无真实数据时跑通全流程）")
    parser.add_argument("--output", type=Path, help="输出 HTML 路径（缺省为 game-life/output/graveyard.html）")
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

    out = generate_graveyard(data, output_path=args.output, open_browser=not args.no_open)
    print(f"[graveyard] 已生成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

