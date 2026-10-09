"""游戏体检报告生成器（可视化呈现层 · 模块 5）。

职责：把游戏库统计包装成「医院体检报告」风格单页 HTML，
      每项指标附带「诊断结果 + 医生建议」。

数据接口（纯 dict 参数，与 graveyard/starmap 一致）::

    {
        "user_name": str,
        "stats": {
            "total_games": int,
            "total_playtime_hours": float,
            "avg_playtime_hours": float,
            "dust_rate": float,            # 0~1
            "played_ratio": float,         # 0~1
            "unplayed_count": int,
            "shelved_count": int,
            "top_genres": [...],
            "most_forgotten": [...],
        },
        "top_game": {"name": str, "hours": float},
        "longest_unplayed": {"name": str, "days": int},
        "generated_at": str,
    }

用法::

    from game_life.render import checkup
    checkup.generate_checkup(data)
    checkup.generate_checkup(data, open_browser=False, output_path=...)
    python game-life/render/checkup.py --demo --no-open
"""

from __future__ import annotations

import argparse
import json
import logging
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

# -------------------------------------------------------- 诊断规则（04 文档 7.3）

DIAGNOSIS_RULES = {
    "dust_rate": [
        (lambda v: v > 0.60, "晚期喜加一综合征", "每月通关 2 款搁置游戏", "danger"),
        (lambda v: v >= 0.30, "中度囤积倾向", "每月清理 1 款闲置游戏", "warning"),
        (lambda v: True, "健康玩家", "保持现状", "success"),
    ],
    "total_games": [
        (lambda v: v > 200, "库存偏大，建议减负", "建议将游戏库精简至 200 款以内", "warning"),
        (lambda v: v > 100, "库存适中", "维持现状，专注通关", "ok"),
        (lambda v: True, "库存健康", "数量合理，继续享受", "success"),
    ],
    "avg_playtime_hours": [
        (lambda v: v < 5, "浅尝辄止型", "尝试把一款搁置游戏玩到通关", "warning"),
        (lambda v: v < 20, "深度玩家", "继续保持投入", "ok"),
        (lambda v: True, " hardcore 玩家", "游戏已成为生活的核心", "success"),
    ],
}


# ---------------------------------------------------------------- 工具函数

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


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_date(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value).strip()


def _classify_dust_rate(v: float) -> dict:
    for cond, label, advice, level in DIAGNOSIS_RULES["dust_rate"]:
        if cond(v):
            return {"label": label, "advice": advice, "level": level}
    return {"label": "未知", "advice": "-", "level": "ok"}


def _classify_inventory(total: int) -> dict:
    for cond, label, advice, level in DIAGNOSIS_RULES["total_games"]:
        if cond(total):
            return {"label": label, "advice": advice, "level": level}
    return {"label": "未知", "advice": "-", "level": "ok"}


def _classify_avg_hours(avg: float) -> dict:
    for cond, label, advice, level in DIAGNOSIS_RULES["avg_playtime_hours"]:
        if cond(avg):
            return {"label": label, "advice": advice, "level": level}
    return {"label": "未知", "advice": "-", "level": "ok"}


def _build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(("html", "xml")),
    )
    env.filters["h"] = lambda v: _fmt_num(v)
    return env


# ------------------------------------------------------------ 数据归一化

def normalize_checkup_data(data: Mapping[str, Any]) -> dict:
    """把外部 dict 清洗为模板可直接使用的结构。"""
    user_name = str(data.get("user_name") or data.get("name") or "神秘玩家")
    stats = data.get("stats") or {}
    top_game = data.get("top_game") or {}
    longest_unplayed = data.get("longest_unplayed") or {}

    total_games = _to_int(stats.get("total_games", 0))
    total_hours = _to_float(stats.get("total_playtime_hours", 0))
    avg_hours = _to_float(stats.get("avg_playtime_hours", 0))
    dust_rate = _to_float(stats.get("dust_rate", 0))
    played_ratio = _to_float(stats.get("played_ratio", 0))
    unplayed_count = _to_int(stats.get("unplayed_count", 0))
    shelved_count = _to_int(stats.get("shelved_count", 0))

    # 需要配图的条目（逐年榜单的封面）：最长沉迷 + 最久未玩，
    # 统一交给 generate_checkup 里的 _attach_covers 就地补 cover；无图则占位块。
    top_game_entry = {
        "appid": top_game.get("appid"),
        "name": str(top_game.get("name") or "-"),
        "hours": top_game.get("hours", 0),
    }
    forgotten = []
    for item in list(stats.get("most_forgotten") or [])[:5]:
        item = dict(item or {})
        forgotten.append(
            {
                "appid": item.get("appid"),
                "name": str(item.get("name") or "-"),
                "last_played": str(item.get("last_played") or ""),
            }
        )
    cover_targets = [top_game_entry] + forgotten if top_game_entry["appid"] else list(forgotten)

    return {
        "user_name": user_name,
        "generated_at": str(data.get("generated_at") or date.today().isoformat()),
        "total_games": total_games,
        "total_hours": total_hours,
        "avg_hours": avg_hours,
        "dust_rate": dust_rate,
        "dust_rate_pct": round(dust_rate * 100, 1),
        "played_ratio": round(played_ratio * 100, 1),
        "unplayed_count": unplayed_count,
        "shelved_count": shelved_count,
        "top_game_name": top_game_entry["name"],
        "top_game_hours": _fmt_num(top_game.get("hours", 0)),
        "top_game": top_game_entry,
        "cover_targets": cover_targets,
        "longest_unplayed_name": str(longest_unplayed.get("name") or "-"),
        "longest_unplayed_days": _to_int(longest_unplayed.get("days", 0)),
        "top_genres": stats.get("top_genres", [])[:5],
        "most_forgotten": forgotten,
        "diagnosis_dust": _classify_dust_rate(dust_rate),
        "diagnosis_inventory": _classify_inventory(total_games),
        "diagnosis_avg": _classify_avg_hours(avg_hours),
    }


# ------------------------------------------------------------ 主生成流程

def generate_checkup(
    data: Mapping[str, Any],
    output_path: Optional[Union[str, Path]] = None,
    open_browser: bool = True,
) -> Path:
    """生成「游戏体检报告」单文件 HTML，返回输出路径。"""
    payload = normalize_checkup_data(data)
    out = Path(output_path) if output_path else DEFAULT_OUTPUT_DIR / "checkup.html"
    out.parent.mkdir(parents=True, exist_ok=True)

    _attach_covers(payload["cover_targets"], out.parent)

    env = _build_env()
    html = env.get_template("checkup.html").render(payload=payload)
    out.write_text(html, encoding="utf-8")
    logger.info("游戏体检报告已生成：%s", out)

    if open_browser:
        webbrowser.open(out.resolve().as_uri())
    return out


# ---------------------------------------------------------------- 示例数据

def demo_data() -> dict:
    return {
        "user_name": "Neo",
        "generated_at": "2026-09-19",
        "stats": {
            "total_games": 287,
            "total_playtime_hours": 1247.5,
            "avg_playtime_hours": 4.3,
            "dust_rate": 0.71,
            "played_ratio": 0.42,
            "unplayed_count": 43,
            "shelved_count": 159,
            "top_genres": [
                {"name": "射击", "hours": 620},
                {"name": "动作", "hours": 290},
                {"name": "RPG", "hours": 185},
            ],
            "most_forgotten": [
                {"name": "文明 6", "last_played": "2024-03-01"},
                {"name": "群星", "last_played": "2024-06-15"},
            ],
        },
        "top_game": {"name": "CS2", "hours": 450},
        "longest_unplayed": {"name": "博德之门 3", "days": 1245},
    }


# ---------------------------------------------------------------- CLI 入口

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="checkup",
        description="生成「游戏体检报告」单文件 HTML",
    )
    parser.add_argument("--data", type=Path, help="数据 JSON 文件路径")
    parser.add_argument("--demo", action="store_true", help="使用内置示例数据")
    parser.add_argument("--output", type=Path, help="输出 HTML 路径（缺省 game-life/output/checkup.html）")
    parser.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")
    args = parser.parse_args(argv)

    if args.data:
        with open(args.data, encoding="utf-8") as f:
            data = json.load(f)
    elif args.demo:
        data = demo_data()
    else:
        data = demo_data()

    out = generate_checkup(data, output_path=args.output, open_browser=not args.no_open)
    print(f"[checkup] 已生成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
