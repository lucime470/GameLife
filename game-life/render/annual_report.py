"""年度游戏报告生成器（可视化呈现层 · 模块 1）。

职责：接收分析引擎产出的纯 dict 数据 -> Jinja2 渲染模板
      -> 输出单文件 HTML（CSS/JS 内嵌）-> webbrowser 自动打开。

当前不依赖任何分析引擎模块（engine/、stats.py 等尚未就绪），
数据以纯 dict 接口传入，后续由 CLI / 主 Agent 接入真实数据。

数据接口（data 参数，缺省字段均有安全默认值）::

    {
        "user": {"name": str, "year": int, "total_games": int},
        "stats": {
            "total_hours": float,          # 总游玩时长（小时）
            "total_games": int,            # 游戏总数
            "backlog_rate": float,         # 吃灰率，0~1 小数
        },
        "genres": [                        # 最爱类型（取前 3）
            {"name": str, "hours": float, "share": float},  # share 0~1，可缺省（自动计算）
        ],
        "top_games": [                     # 最爱游戏（取前 3）
            {"name": str, "hours": float, "genre": str, "status": str, "rank": int},
        ],
        "persona": {
            "tag": str,                    # 人格标签
            "summary": str,                # 人格描述
            "dimensions": [                # 5 维雷达图（0~100）
                {"name": str, "value": int},
            ],
        },
        "value_picks": [                   # 最性价比（取前 3）
            {"name": str, "price": float, "hours": float, "cost_per_hour": float},
        ],
        "year_summary": str,               # 年度总结语
        "generated_at": str,               # 可选，默认今天 YYYY-MM-DD
    }

用法::

    from game_life.render import annual_report
    annual_report.generate_report(data)          # 渲染 + 写文件 + 打开浏览器
    annual_report.generate_report(data, open_browser=False, output_path=...)
    python game-life/render/annual_report.py --demo --no-open   # 示例数据跑通全流程
"""

from __future__ import annotations

import argparse
import json
import logging
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

DEFAULT_YEAR_SUMMARY = "这一年，游戏陪你走过。继续探索，继续热爱。"
DEFAULT_PERSONA_TAG = "未知人格"
DEFAULT_PERSONA_SUMMARY = "本年度数据尚不足以生成人格画像，先好好享受游戏吧。"


# ---------------------------------------------------------------- 工具函数

def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _fmt_hours(value: Any) -> str:
    """1247.5 -> "1,247.5"；1247.0 -> "1,247"（去多余小数）。"""
    v = _to_float(value)
    s = f"{v:,.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _fmt_pct(value: Any) -> str:
    """0.71 -> "71%"；71 -> "71%"（兼容已按百分比传入的情况）。"""
    v = _to_float(value)
    if v > 1:
        s = f"{v:.1f}"
    else:
        s = f"{v * 100:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    return f"{s}%"


def _fmt_comma(value: Any) -> str:
    """287 -> "287"；12470 -> "12,470"。"""
    return f"{_to_float(value):,.0f}"


def _build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(("html", "xml")),
    )
    env.filters["h"] = _fmt_hours
    env.filters["pct"] = _fmt_pct
    env.filters["comma"] = _fmt_comma
    return env


# ---------------------------------------------------------------- 数据归一化

def normalize_data(data: Mapping[str, Any]) -> dict:
    """把外部传入的 dict 清洗为模板可直接使用的结构，缺失字段全部给默认值。"""
    user = dict(data.get("user") or {})
    stats = dict(data.get("stats") or {})

    year = _to_int(user.get("year"), date.today().year)
    name = str(user.get("name") or "神秘玩家")

    total_hours = _to_float(stats.get("total_hours"))
    total_games = _to_int(stats.get("total_games"), _to_int(user.get("total_games")))
    backlog_rate = _clamp(_to_float(stats.get("backlog_rate")), 0.0, 1.0)

    # 最爱类型：最多 3 条，share 缺失时按时长占比自动计算
    genres = []
    for i, g in enumerate(list(data.get("genres") or [])[:3]):
        g = dict(g or {})
        genres.append(
            {
                "name": str(g.get("name") or f"类型 {i + 1}"),
                "hours": _to_float(g.get("hours")),
                "share": _clamp(_to_float(g.get("share")), 0.0, 1.0),
            }
        )
    if genres:
        share_sum = sum(g["share"] for g in genres)
        if share_sum <= 0:
            hours_sum = sum(g["hours"] for g in genres) or total_hours or 1.0
            for g in genres:
                g["share"] = _clamp(g["hours"] / hours_sum, 0.0, 1.0)
        else:
            for g in genres:
                g["share"] = _clamp(g["share"] / share_sum, 0.0, 1.0)

    # 最爱游戏：最多 3 条
    top_games = []
    for i, g in enumerate(list(data.get("top_games") or [])[:3]):
        g = dict(g or {})
        top_games.append(
            {
                "appid": g.get("appid"),
                "name": str(g.get("name") or f"游戏 {i + 1}"),
                "hours": _to_float(g.get("hours")),
                "genre": str(g.get("genre") or "未知"),
                "status": str(g.get("status") or ""),
                "rank": _to_int(g.get("rank"), i + 1),
            }
        )

    # 人格：标签 / 描述 / 雷达维度（最多 6 维，0~100）
    persona_raw = dict(data.get("persona") or {})
    dimensions = []
    for i, d in enumerate(list(persona_raw.get("dimensions") or [])[:6]):
        d = dict(d or {})
        dimensions.append(
            {
                "name": str(d.get("name") or f"维度 {i + 1}"),
                "value": int(_clamp(_to_float(d.get("value")), 0.0, 100.0)),
            }
        )
    persona = {
        "tag": str(persona_raw.get("tag") or DEFAULT_PERSONA_TAG),
        "summary": str(persona_raw.get("summary") or DEFAULT_PERSONA_SUMMARY),
        "dimensions": dimensions,
    }

    # 最性价比：最多 3 条，cost_per_hour 缺失时按 价格/时长 计算
    value_picks = []
    for i, v in enumerate(list(data.get("value_picks") or [])[:3]):
        v = dict(v or {})
        price = _to_float(v.get("price"))
        hours = _to_float(v.get("hours"))
        cph = _to_float(v.get("cost_per_hour"))
        if cph <= 0 and hours > 0:
            cph = price / hours
        value_picks.append(
            {
                "appid": v.get("appid"),
                "name": str(v.get("name") or f"游戏 {i + 1}"),
                "price": price,
                "hours": hours,
                "cost_per_hour": cph,
            }
        )

    return {
        "user": {"name": name, "year": year, "total_games": total_games},
        "stats": {
            "total_hours": total_hours,
            "total_games": total_games,
            "backlog_rate": backlog_rate,
            "avg_hours": (total_hours / total_games) if total_games else 0.0,
        },
        "genres": genres,
        "top_games": top_games,
        "persona": persona,
        "value_picks": value_picks,
        "year_summary": str(data.get("year_summary") or DEFAULT_YEAR_SUMMARY),
        "generated_at": str(data.get("generated_at") or date.today().isoformat()),
    }


# ---------------------------------------------------------------- 主生成流程

def generate_report(
    data: Mapping[str, Any],
    output_path: Optional[Union[str, Path]] = None,
    open_browser: bool = True,
) -> Path:
    """生成年度游戏报告单文件 HTML，返回输出路径。

    :param data: 报告数据 dict（结构见模块 docstring）
    :param output_path: 输出文件路径；缺省为
        ``game-life/output/annual_report_<年份>.html``
    :param open_browser: 生成后是否自动用浏览器打开（默认 True）
    """
    payload = normalize_data(data)
    out = (
        Path(output_path)
        if output_path
        else DEFAULT_OUTPUT_DIR / f"annual_report_{payload['user']['year']}.html"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    _attach_covers(list(payload["top_games"]) + list(payload["value_picks"]), out.parent)

    env = _build_env()
    template = env.get_template("annual_report.html")
    html = template.render(payload=payload)
    out.write_text(html, encoding="utf-8")
    logger.info("年度报告已生成：%s", out)

    if open_browser:
        webbrowser.open(out.resolve().as_uri())
    return out


def demo_data() -> dict:
    """示例数据：无真实数据时用于跑通全流程（模拟 Steam 库玩家画像）。"""
    year = date.today().year
    return {
        "user": {"name": "Neo", "year": year, "total_games": 287},
        "stats": {
            "total_hours": 1247.5,
            "total_games": 287,
            "backlog_rate": 0.71,
        },
        "genres": [
            {"name": "RPG", "hours": 486.2, "share": 0.39},
            {"name": "动作", "hours": 302.8, "share": 0.24},
            {"name": "策略", "hours": 187.4, "share": 0.15},
        ],
        "top_games": [
            {"name": "艾尔登法环", "hours": 238.6, "genre": "RPG", "status": "已通关", "rank": 1},
            {"name": "博德之门 3", "hours": 172.4, "genre": "RPG", "status": "在玩", "rank": 2},
            {"name": "文明 6", "hours": 156.3, "genre": "策略", "status": "搁置", "rank": 3},
        ],
        "persona": {
            "tag": "深度沉浸型玩家",
            "summary": "你偏爱慢节奏的宏大世界，一旦进入就难以自拔。257 款游戏里，你为挚爱作品倾注了大部分时间。",
            "dimensions": [
                {"name": "沉浸度", "value": 92},
                {"name": "肝度", "value": 78},
                {"name": "收集欲", "value": 54},
                {"name": "博爱度", "value": 38},
                {"name": "怀旧心", "value": 66},
            ],
        },
        "value_picks": [
            {"name": "巫师 3：狂猎", "price": 38.0, "hours": 154.2, "cost_per_hour": 0.25},
            {"name": "泰拉瑞亚", "price": 24.0, "hours": 89.6, "cost_per_hour": 0.27},
            {"name": "星露谷物语", "price": 28.0, "hours": 96.1, "cost_per_hour": 0.29},
        ],
        "year_summary": (
            f"{year} 年，你在游戏里度过了 1,248 个小时——"
            "相当于 52 个完整日夜。感谢每一个在虚拟世界里发光的瞬间，明年继续。"
        ),
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="annual_report",
        description="生成「年度游戏报告」单文件 HTML（示例：--demo 或 --data data.json）",
    )
    parser.add_argument(
        "--data",
        type=Path,
        help="数据 JSON 文件路径（结构见 annual_report.generate_report 文档）",
    )
    parser.add_argument("--demo", action="store_true", help="使用内置示例数据（无真实数据时跑通全流程）")
    parser.add_argument(
        "--output", type=Path, help="输出 HTML 路径（缺省为 game-life/output/annual_report_<年份>.html）"
    )
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

    out = generate_report(data, output_path=args.output, open_browser=not args.no_open)
    print(f"[annual_report] 已生成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

