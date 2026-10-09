"""游戏考古学家（可视化呈现层 · 模块 4）。

职责：把游玩历史整理成按月粒度的「游戏考古时间轴」单文件 HTML，
      年度节点高亮；事件按 04 文档 §6.4 的 4 类：开始游玩 / 持续游玩 /
      购买 / 搁置。

数据接口（纯 dict 参数，缺省字段均有安全默认值）::

    {
        "user_name": str,               # 可选，默认「神秘玩家」
        "events": [                     # 可选；缺省时由 play_stats / snapshots 推导
            {
                "date": "2026-08-15",   # "YYYY-MM-DD" 或 "YYYY-MM"
                "type": "started",      # started / played / purchased / shelved
                "name": "Counter-Strike 2",
                "hours": 45,            # 仅 played 使用（小时）
            },
        ],
        "play_stats": {...},            # 可选 {appid: {name?, playtime(分钟), last_played}}
        "snapshots": [...],             # 可选 [{record_date, appid, playtime, last_played, status}]
        "generated_at": str,            # 可选，默认今天
    }

事件类型（04 文档 §6.4）::

    started    开始游玩  你开始玩《XX》
    played     持续游玩  你在《XX》里花了 Xh
    purchased  购买      你买了《XX》（可能更早，仅供参考）
    shelved    搁置      你搁置了《XX》

用法::

    from game_life.render import archaeologist
    archaeologist.generate_timeline(data)                 # 渲染 + 写文件 + 打开浏览器
    archaeologist.generate_timeline(data, open_browser=False, output_path=...)
    python game-life/render/archaeologist.py --demo --no-open
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

# ------------------------------------------------------ 事件类型（04 文档 6.4）

TYPE_META = {
    "started":    {"label": "开始游玩", "icon": "▶",  "cls": "ev-started"},
    "played":     {"label": "持续游玩", "icon": "⏱",  "cls": "ev-played"},
    "purchased":  {"label": "购买",     "icon": "🛒",  "cls": "ev-purchased"},
    "shelved":    {"label": "搁置",     "icon": "🕸",  "cls": "ev-shelved"},
}

_TYPE_ALIASES = {
    "started": "started", "开始游玩": "started", "start": "started", "first_played": "started",
    "played": "played", "持续游玩": "played", "playing": "played", "continued": "played",
    "purchased": "purchased", "购买": "purchased", "bought": "purchased", "buy": "purchased",
    "shelved": "shelved", "搁置": "shelved", "shelf": "shelved", "paused": "shelved",
}


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
def _normalize_date(value: Any) -> str:
    """把 datetime / date / "YYYY-MM-DD" / unix 秒 归一为 "YYYY-MM-DD"。"""
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if s.isdigit() and len(s) >= 9:
        try:
            return datetime.fromtimestamp(int(s)).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return s
    return s


# unix 秒可接受的下界（约 2000-01-01）：挡掉 Steam 写入的 LastPlayed=0/1 这类脏值，
# 避免它们被当作 1970-01-01 静默混进时间轴。
_MIN_TIMESTAMP = 946684800


def _parse_iso_date(value: Any) -> Optional[date]:
    """严格解析 datetime / date / "YYYY-MM-DD" / unix 秒，无法解析时返回 None。

    与 :func:`_normalize_date` 的区别：畸形输入不再原样透传，而是显式失败（None），
    由调用方安全丢弃。这样 **不会** 把 ``LastPlayed=1`` 这类脏数据当成
    1970-01-01 渲染出去（静默错数据），而是让该条记录退出时间轴。

    unix 秒做范围校验：``_MIN_TIMESTAMP <= ts <= 现在 + 1 天``。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        # 纯数字一律按 unix 秒处理；过小（脏值）或过大（溢出）均视为无效
        try:
            ts = int(s)
        except (TypeError, ValueError):
            return None
        if not (_MIN_TIMESTAMP <= ts <= datetime.now().timestamp() + 86400):
            return None
        try:
            return datetime.fromtimestamp(ts).date()
        except (OverflowError, OSError, ValueError):
            return None
    # 只取前 10 位做严格校验，顺带容忍 "YYYY-MM-DDTHH:MM:SS" 这类带时间的 ISO 串；
    # 再退一步支持模块文档约定的 "YYYY-MM"（按当月 1 日处理）。
    for text, fmt in ((s[:10], "%Y-%m-%d"), (s, "%Y-%m")):
        try:
            return datetime.strptime(text, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _month_label(month: str) -> Optional[str]:
    """"08" -> "8月"；月份不是两位数字时返回 None（调用方据此丢弃，避免 int() 崩溃）。"""
    if len(month) != 2 or not month.isdigit():
        return None
    return f"{int(month)}月"


def _normalize_type(raw: Any) -> str:
    key = str(raw or "").strip().lower()
    return _TYPE_ALIASES.get(key, "started")


def _derive_events(data: Mapping[str, Any]) -> list[dict]:
    """从 play_stats / snapshots 推导最小事件流（开始游玩 + 持续游玩）。

    playtime 以分钟计，转为小时；仅提供兜底能力，完整事件建议直接传 events。
    """
    events: list[dict] = []
    play_stats = data.get("play_stats") or {}
    games = data.get("games") or {}
    if isinstance(play_stats, Mapping):
        stat_items = list(play_stats.items())
    else:
        stat_items = [(s.get("appid"), s) for s in play_stats if isinstance(s, Mapping)]

    for appid, stat in stat_items:
        info = stat if isinstance(stat, Mapping) else {}
        name = info.get("name")
        if not name and isinstance(games, Mapping):
            info_g = games.get(appid) or games.get(str(appid)) or {}
            name = info_g.get("name", "") if isinstance(info_g, Mapping) else ""
        name = str(name or "").strip()
        lp_date = _parse_iso_date(info.get("last_played"))
        if not name:
            continue
        if lp_date is None:
            logger.debug("archaeologist: 跳过非法 last_played=%r（appid=%s）", info.get("last_played"), appid)
            continue
        lp = lp_date.isoformat()
        hours = _to_float(info.get("playtime")) / 60.0
        if hours > 0:
            events.append({"date": lp, "type": "played", "name": name, "hours": hours, "appid": appid})
        events.append({"date": lp, "type": "started", "name": name, "hours": 0, "appid": appid})

    snapshots = data.get("snapshots") or []
    if isinstance(snapshots, list):
        for snap in snapshots:
            if not isinstance(snap, Mapping):
                continue
            rows = snap.get("games")
            if rows is None:
                rows = [snap]
            if isinstance(rows, Mapping):
                rows = list(rows.values())
            for g in rows:
                if not isinstance(g, Mapping):
                    continue
                name = str(g.get("name") or "").strip()
                raw_date = g.get("last_played") or snap.get("record_date")
                lp_date = _parse_iso_date(raw_date)
                if not name:
                    continue
                if lp_date is None:
                    logger.debug("archaeologist: 跳过非法快照日期=%r（appid=%s）", raw_date, g.get("appid"))
                    continue
                lp = lp_date.isoformat()
                events.append({"date": lp, "type": "started", "name": name, "hours": 0, "appid": g.get("appid")})
    return events


def _render_event(ev: Mapping[str, Any]) -> dict:
    """把内部事件补全为模板可直接使用的结构（含文案）。"""
    meta = TYPE_META.get(ev["type"], TYPE_META["started"])
    name = ev["name"]
    if ev["type"] == "played":
        text = f"你在《{name}》里花了 {_fmt_num(ev['hours'])}h"
    elif ev["type"] == "purchased":
        text = f"你买了《{name}》（可能更早，仅供参考）"
    elif ev["type"] == "shelved":
        text = f"你搁置了《{name}》"
    else:
        text = f"你开始玩《{name}》"
    return {
        "date": ev["date"],
        "type": ev["type"],
        "name": name,
        "appid": ev.get("appid"),
        "hours": _to_float(ev.get("hours", 0)),
        "label": meta["label"],
        "icon": meta["icon"],
        "cls": meta["cls"],
        "text": text,
    }
def _build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(("html", "xml")),
    )
    env.filters["h"] = lambda v: _fmt_num(v)
    return env


# ------------------------------------------------------------ 数据归一化

def normalize_archaeologist_data(data: Mapping[str, Any]) -> dict:
    """把外部传入的 dict 清洗为模板可直接使用的结构（按年-月倒序分组）。"""
    user_name = str(data.get("user_name") or data.get("name") or "神秘玩家")
    raw_events = data.get("events")
    if raw_events is None:
        raw_events = _derive_events(data)

    events: list[dict] = []
    if isinstance(raw_events, list):
        for raw in raw_events:
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("name") or "").strip()
            date_raw = raw.get("date") or raw.get("month") or ""
            if not name or not date_raw:
                continue
            d = _parse_iso_date(date_raw)
            if d is None:
                logger.debug("archaeologist: 跳过非法日期=%r（name=%s）", date_raw, name)
                continue
            events.append(
                {
                    "date": d.isoformat(),
                    "year": f"{d.year:04d}",
                    "month": f"{d.month:02d}",
                    "type": _normalize_type(raw.get("type")),
                    "name": name,
                    "hours": _to_float(raw.get("hours", 0)),
                    "appid": raw.get("appid"),
                }
            )

    events.sort(key=lambda e: e["date"], reverse=True)

    # 按年分组（年份倒序）-> 按月分组（月份倒序）
    years_map: dict[str, list[dict]] = {}
    for ev in events:
        years_map.setdefault(ev["year"], []).append(ev)

    year_groups: list[dict] = []
    total_hours = 0.0
    for year in sorted(years_map, reverse=True):
        months_map: dict[str, list[dict]] = {}
        for ev in years_map[year]:
            months_map.setdefault(ev["month"], []).append(ev)
        month_groups = []
        for month in sorted(months_map, reverse=True):
            label = _month_label(month)
            if label is None:
                logger.debug("archaeologist: 跳过非法月份=%r（共 %d 条事件）", month, len(months_map[month]))
                continue
            rendered = [_render_event(ev) for ev in months_map[month]]
            for ev in rendered:
                if ev["type"] == "played":
                    total_hours += ev["hours"]
            month_groups.append({"month": label, "events": rendered})
        year_groups.append({"year": year, "month_groups": month_groups})

    return {
        "user_name": user_name,
        "year_groups": year_groups,
        "stats": {
            "total": len(events),
            "years": len(year_groups),
            "total_hours": total_hours,
            "games": len({ev["name"] for ev in events}),
        },
        "generated_at": str(data.get("generated_at") or date.today().isoformat()),
    }


# ------------------------------------------------------------ 主生成流程

def generate_timeline(
    data: Mapping[str, Any],
    output_path: Optional[Union[str, Path]] = None,
    open_browser: bool = True,
) -> Path:
    """生成「游戏考古时间轴」单文件 HTML，返回输出路径。

    :param data: 时间轴数据 dict（结构见模块 docstring）
    :param output_path: 输出路径；缺省为 ``game-life/output/timeline.html``
    :param open_browser: 生成后是否自动用浏览器打开（默认 True）
    """
    payload = normalize_archaeologist_data(data)
    out = Path(output_path) if output_path else DEFAULT_OUTPUT_DIR / "timeline.html"
    out.parent.mkdir(parents=True, exist_ok=True)

    events = [ev for year in payload["year_groups"] for mg in year["month_groups"] for ev in mg["events"]]
    _attach_covers(events, out.parent)

    env = _build_env()
    html = env.get_template("timeline.html").render(payload=payload)
    out.write_text(html, encoding="utf-8")
    logger.info("游戏考古时间轴已生成：%s（%d 条事件 / %d 年）", out, len(payload["year_groups"]) and payload["stats"]["total"], payload["stats"]["years"])

    if open_browser:
        webbrowser.open(out.resolve().as_uri())
    return out
# ---------------------------------------------------------------- 示例数据

def demo_data() -> dict:
    """示例数据：覆盖 4 类事件、4 个年份，用于跑通全流程。"""
    return {
        "user_name": "Neo",
        "events": [
            {"date": "2026-08-15", "type": "started", "name": "Counter-Strike 2"},
            {"date": "2026-08-15", "type": "played", "name": "Counter-Strike 2", "hours": 45},
            {"date": "2026-06-10", "type": "purchased", "name": "艾尔登法环"},
            {"date": "2026-06-02", "type": "played", "name": "博德之门 3", "hours": 12},
            {"date": "2026-05-01", "type": "shelved", "name": "群星"},
            {"date": "2026-02-01", "type": "started", "name": "博德之门 3"},
            {"date": "2026-02-01", "type": "played", "name": "博德之门 3", "hours": 6},
            {"date": "2026-01-08", "type": "shelved", "name": "文明 6"},
            {"date": "2025-12-25", "type": "purchased", "name": "荒野大镖客 2"},
            {"date": "2025-11-02", "type": "started", "name": "赛博朋克 2077"},
            {"date": "2025-11-02", "type": "played", "name": "赛博朋克 2077", "hours": 8.5},
            {"date": "2025-06-27", "type": "started", "name": "荒野大镖客 2"},
            {"date": "2025-04-20", "type": "played", "name": "艾迪芬奇的记忆", "hours": 0.9},
            {"date": "2025-02-10", "type": "purchased", "name": "博德之门 3"},
            {"date": "2024-12-01", "type": "shelved", "name": "缺氧"},
            {"date": "2024-06-15", "type": "started", "name": "死亡搁浅"},
            {"date": "2024-03-11", "type": "purchased", "name": "只狼：影逝二度"},
            {"date": "2023-11-23", "type": "started", "name": "环世界"},
        ],
    }


# ---------------------------------------------------------------- CLI 入口

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="archaeologist",
        description="生成「游戏考古时间轴」单文件 HTML（示例：--demo 或 --data data.json）",
    )
    parser.add_argument("--data", type=Path, help="数据 JSON 文件路径（结构见 archaeologist.generate_timeline 文档）")
    parser.add_argument("--demo", action="store_true", help="使用内置示例数据（无真实数据时跑通全流程）")
    parser.add_argument("--output", type=Path, help="输出 HTML 路径（缺省为 game-life/output/timeline.html）")
    parser.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")
    args = parser.parse_args(argv)

    if args.data:
        with open(args.data, encoding="utf-8") as f:
            data = json.load(f)
        logger.info("已从 %s 读取数据", args.data)
    else:
        data = demo_data()
        logger.info("未指定 --data，默认使用示例数据")

    out = generate_timeline(data, output_path=args.output, open_browser=not args.no_open)
    print(f"[archaeologist] 已生成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
